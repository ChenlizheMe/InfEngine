"""Immutable runtime dispatch epochs for Python component execution.

The editor and Player publish one :class:`RuntimeRevisionEpoch` at an owner
safe point.  A frame keeps a strong reference to the epoch it captured, so a
body reload cannot change update/fixed_update/late_update halfway through a
frame.  This module is intentionally independent from the component lifecycle
mixins; it is the single owner of revision-aware dispatch state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
import inspect
import threading
import weakref
from typing import Any, Callable, Iterable, Mapping, Optional


RUNTIME_PHASE_NAMES = ("update", "fixed_update", "late_update")
_MISSING = object()


def _missing_runtime_phase(*_args: Any) -> None:
    return None


def _method_call_signature(raw: Any, kind: str) -> str:
    """Return the signature callers see after descriptor binding."""
    try:
        signature = inspect.signature(raw)
        if kind == "instance":
            parameters = tuple(signature.parameters.values())
            if parameters:
                signature = signature.replace(parameters=parameters[1:])
        return str(signature)
    except (TypeError, ValueError):
        return "<unknown>"


def _accepted_arg_counts(raw: Any, kind: str) -> tuple[int, ...]:
    """Precompute positional call forms used by runtime event dispatch.

    The result is bounded to the largest event payload (three values). This
    runs while a runtime epoch is built; event dispatch never reflects a
    signature or retries a call after a user exception.
    """
    try:
        signature = inspect.signature(raw)
        parameters = tuple(signature.parameters.values())
        if kind == "instance":
            if not parameters:
                return ()
            parameters = parameters[1:]

        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        if any(
            parameter.kind == inspect.Parameter.KEYWORD_ONLY
            and parameter.default is inspect.Parameter.empty
            for parameter in parameters
        ):
            return ()
        minimum = sum(
            parameter.default is inspect.Parameter.empty for parameter in positional
        )
        variadic = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
        maximum = 3 if variadic else min(3, len(positional))
        return tuple(range(minimum, maximum + 1)) if minimum <= maximum else ()
    except (TypeError, ValueError):
        return ()


@dataclass(frozen=True)
class RuntimeMethodDescriptor:
    """One method as it existed when its owning epoch was built."""

    name: str
    raw: Any
    kind: str
    signature: str = field(init=False)
    accepted_arg_counts: tuple[int, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signature", _method_call_signature(self.raw, self.kind))
        object.__setattr__(self, "accepted_arg_counts", _accepted_arg_counts(self.raw, self.kind))

    @property
    def bind_instance(self) -> bool:
        return self.kind == "instance"

    def invoke(self, instance: Any, *args: Any) -> Any:
        if self.kind == "instance":
            return self.raw(instance, *args)
        return self.raw(*args)

    def bind(self, instance: Any) -> Any:
        if self.kind == "instance":
            return self.raw.__get__(instance, type(instance))
        return self.raw

    def accepts_arg_count(self, count: int) -> bool:
        """Return whether the already-built descriptor accepts *count* args."""
        return int(count) in self.accepted_arg_counts

    def preferred_arg_count(self, maximum: int) -> Optional[int]:
        """Choose the largest accepted arity not exceeding *maximum*."""
        maximum = int(maximum)
        for count in reversed(self.accepted_arg_counts):
            if count <= maximum:
                return count
        return None


@dataclass(frozen=True)
class RuntimeTypeDispatchDescriptor:
    """Immutable phase and helper lookup table for one component type."""

    component_type: type
    epoch_id: int
    phase_methods: tuple[Optional[RuntimeMethodDescriptor], ...]
    methods: Mapping[str, RuntimeMethodDescriptor]
    _phase_dispatch: tuple[tuple[Any, bool], ...] = field(init=False, repr=False, compare=False)
    _phase_invokers: tuple[Any, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.phase_methods) != len(RUNTIME_PHASE_NAMES):
            raise ValueError("runtime phase descriptor has an invalid phase count")
        object.__setattr__(self, "methods", MappingProxyType(dict(self.methods)))
        dispatch = []
        invokers = []
        for method in self.phase_methods:
            if method is None:
                dispatch.append((_missing_runtime_phase, True))
                invokers.append(_missing_runtime_phase)
                continue

            dispatch.append((method.raw, method.bind_instance))

            def invoke(instance: Any, value: Any, _method=method) -> Any:
                return _method.invoke(instance, value)

            invokers.append(invoke)
        object.__setattr__(self, "_phase_dispatch", tuple(dispatch))
        object.__setattr__(self, "_phase_invokers", tuple(invokers))

    @property
    def phase_dispatch(self) -> tuple[tuple[Any, bool], ...]:
        return self._phase_dispatch

    @property
    def phase_presence(self) -> tuple[bool, bool, bool]:
        """Whether update/fixed_update/late_update exist in this descriptor."""
        return tuple(method is not None for method in self.phase_methods)  # type: ignore[return-value]

    @property
    def phase_invokers(self) -> tuple[Any, ...]:
        return self._phase_invokers

    def has_phase(self, phase: str) -> bool:
        try:
            index = RUNTIME_PHASE_NAMES.index(phase)
        except ValueError:
            raise ValueError(f"unknown runtime phase: {phase}") from None
        return self.phase_methods[index] is not None

    def resolve_method(self, instance: Any, name: str) -> Any:
        descriptor = self.methods.get(str(name))
        return None if descriptor is None else descriptor.bind(instance)


def _build_retired_type_descriptor(
    component_type: type,
    *,
    epoch_id: int,
) -> RuntimeTypeDispatchDescriptor:
    """Build the inert compatibility descriptor used after type retirement.

    A retired class object can remain alive through an old frame or callback,
    but a cache miss in the current epoch must never rediscover and execute
    its old class body.  The inert descriptor keeps that failure explicit.
    """
    return RuntimeTypeDispatchDescriptor(
        component_type=component_type,
        epoch_id=int(epoch_id),
        phase_methods=(None, None, None),
        methods={},
    )


@dataclass(frozen=True)
class RuntimeRevisionEpoch:
    """An immutable publication of all runtime dispatch descriptors."""

    epoch_id: int
    descriptors: Mapping[type, RuntimeTypeDispatchDescriptor]

    def __post_init__(self) -> None:
        object.__setattr__(self, "epoch_id", int(self.epoch_id))
        object.__setattr__(self, "descriptors", MappingProxyType(dict(self.descriptors)))

    def descriptor_for(self, component_type: type) -> Optional[RuntimeTypeDispatchDescriptor]:
        return self.descriptors.get(component_type)

    def has_phase(self, component_type: type, phase: str) -> bool:
        descriptor = self.descriptor_for(component_type)
        return bool(descriptor and descriptor.has_phase(phase))

    def resolve_method(self, instance: Any, name: str) -> Any:
        descriptor = self.descriptor_for(type(instance))
        return None if descriptor is None else descriptor.resolve_method(instance, name)


@dataclass(frozen=True)
class RuntimeOwnerIdentity:
    """Stable identity used by a reloadable bound callback."""

    component_id: int
    native_generation: int


@dataclass(frozen=True)
class CallbackResolution:
    """Explicit result of resolving a callback reference."""

    status: str
    callback: Optional[Callable[..., Any]] = None
    message: str = ""

    @property
    def resolved(self) -> bool:
        return self.status in {"resolved", "legacy_pinned"}


@dataclass(frozen=True)
class CallbackInvocation:
    """Result returned by a registry dispatch attempt."""

    reference: "ReloadableCallbackRef"
    status: str
    message: str = ""


@dataclass(frozen=True, eq=False)
class ReloadableCallbackRef:
    """A weak, epoch-aware reference to an InxComponent bound method.

    Bound method objects are deliberately not stored.  Resolution obtains the
    current epoch descriptor and creates a short-lived bound method only for
    the invocation.  Arbitrary functions, lambdas and closures remain pinned
    legacy callables with their creation epoch recorded for diagnostics.
    """

    owner_ref: Optional[weakref.ReferenceType[Any]]
    owner_identity: Optional[RuntimeOwnerIdentity]
    owner_type: Optional[type]
    method_name: str
    expected_signature: str
    registration_epoch: int
    legacy_callable: Optional[Callable[..., Any]] = None
    pinned_revision: Optional[int] = None
    fallback_descriptor: Optional[RuntimeTypeDispatchDescriptor] = None

    @property
    def is_legacy(self) -> bool:
        return self.legacy_callable is not None

    @classmethod
    def normalize(
        cls,
        callback: Callable[..., Any],
        *,
        expected_signature: Optional[str] = None,
        epoch: Optional[RuntimeRevisionEpoch] = None,
    ) -> "ReloadableCallbackRef":
        if not callable(callback):
            raise TypeError("callback must be callable")
        selected_epoch = current_runtime_epoch() if epoch is None else epoch
        registration_epoch = selected_epoch.epoch_id
        owner = getattr(callback, "__self__", None)
        function = getattr(callback, "__func__", None)
        if owner is None or function is None:
            return cls(
                owner_ref=None,
                owner_identity=None,
                owner_type=None,
                method_name="",
                expected_signature=str(expected_signature or _safe_signature(callback)),
                registration_epoch=registration_epoch,
                legacy_callable=callback,
                pinned_revision=registration_epoch,
            )

        # Only InxComponent instances have a stable runtime identity and an
        # epoch descriptor that can safely re-resolve their methods.  A plain
        # Python object's bound method remains a legacy pinned callback: this
        # preserves the old UI/event lifetime semantics and supports owners
        # that cannot be weak-referenced.
        try:
            from Infernux.components.component import InxComponent

            reloadable_owner = isinstance(owner, InxComponent)
        except (ImportError, TypeError):
            reloadable_owner = False
        if not reloadable_owner:
            return cls(
                owner_ref=None,
                owner_identity=None,
                owner_type=None,
                method_name=str(getattr(function, "__name__", "") or ""),
                expected_signature=str(expected_signature or _safe_signature(callback)),
                registration_epoch=registration_epoch,
                legacy_callable=callback,
                pinned_revision=registration_epoch,
            )

        try:
            owner_ref = weakref.ref(owner)
        except TypeError as exc:
            raise TypeError(
                "reloadable bound callbacks require a weak-referenceable owner"
            ) from exc
        method_name = str(getattr(function, "__name__", "") or "")
        if not method_name:
            raise TypeError("bound callback has no stable method name")
        descriptor = _descriptor_for_type(selected_epoch, type(owner))
        method = descriptor.methods.get(method_name)
        if method is None:
            raise ValueError(
                f"callback method '{method_name}' is not a reloadable method"
            )
        expected = str(expected_signature or method.signature)
        if method.signature != expected:
            raise ValueError(
                f"callback signature mismatch for '{method_name}': "
                f"expected {expected}, got {method.signature}"
            )
        return cls(
            owner_ref=owner_ref,
            owner_identity=_owner_identity(owner),
            owner_type=type(owner),
            method_name=method_name,
            expected_signature=expected,
            registration_epoch=registration_epoch,
            fallback_descriptor=descriptor,
        )

    def resolve(self, epoch: Optional[RuntimeRevisionEpoch] = None) -> CallbackResolution:
        if self.is_legacy:
            selected_epoch = current_runtime_epoch() if epoch is None else epoch
            message = ""
            if (
                self.pinned_revision is not None
                and selected_epoch.epoch_id != self.pinned_revision
            ):
                message = (
                    "legacy callback remains pinned to runtime epoch "
                    f"{self.pinned_revision} while epoch {selected_epoch.epoch_id} is active"
                )
            return CallbackResolution("legacy_pinned", self.legacy_callable, message)
        owner = self.owner_ref() if self.owner_ref is not None else None
        if owner is None:
            return CallbackResolution("owner_unavailable", message="callback owner was collected")
        if bool(getattr(owner, "_is_destroyed", False)):
            return CallbackResolution("owner_invalid", message="callback owner was destroyed")
        if _owner_identity(owner) != self.owner_identity:
            return CallbackResolution("owner_invalid", message="callback owner identity changed")
        selected_epoch = current_runtime_epoch() if epoch is None else epoch
        descriptor = selected_epoch.descriptor_for(type(owner))
        if descriptor is None:
            # A fallback is only valid for the exact epoch in which this
            # reference was created.  Once a newer epoch is selected, a
            # missing type is an explicit publication result, not permission
            # to execute an old body.  This keeps removed callback contracts
            # from silently surviving a reload.
            fallback = self.fallback_descriptor
            if (
                fallback is not None
                and selected_epoch.epoch_id == self.registration_epoch
            ):
                descriptor = fallback
        if descriptor is None or descriptor.component_type is not type(owner):
            return CallbackResolution(
                "method_missing",
                message=f"callback type '{type(owner).__name__}' has no runtime descriptor",
            )
        method = descriptor.methods.get(self.method_name)
        if method is None:
            return CallbackResolution(
                "method_missing",
                message=f"callback method '{self.method_name}' is unavailable",
            )
        callback = descriptor.resolve_method(owner, self.method_name)
        if method.signature != self.expected_signature:
            return CallbackResolution(
                "signature_mismatch",
                message=(
                    f"callback signature changed for '{self.method_name}': "
                    f"expected {self.expected_signature}, got {method.signature}"
                ),
            )
        return CallbackResolution("resolved", callback)

    def invoke(
        self,
        *args: Any,
        epoch: Optional[RuntimeRevisionEpoch] = None,
        propagate_exceptions: bool = False,
    ) -> CallbackInvocation:
        resolution = self.resolve(epoch)
        if not resolution.resolved:
            return CallbackInvocation(self, resolution.status, resolution.message)
        try:
            resolution.callback(*args)
        except Exception as exc:
            if propagate_exceptions:
                raise
            return CallbackInvocation(self, "exception", f"{type(exc).__name__}: {exc}")
        return CallbackInvocation(self, resolution.status, resolution.message)

    def matches(self, callback: Callable[..., Any]) -> bool:
        if self.is_legacy:
            return self.legacy_callable is callback or self.legacy_callable == callback
        owner = getattr(callback, "__self__", None)
        function = getattr(callback, "__func__", None)
        return (
            owner is not None
            and function is not None
            and _owner_identity(owner) == self.owner_identity
            and str(getattr(function, "__name__", "") or "") == self.method_name
        )

    def matches_owner_method(self, owner: Any, method_name: str) -> bool:
        """Match a persistent owner/path without inspecting the current body."""
        method_name = str(method_name or "")
        if self.is_legacy:
            callback = getattr(owner, method_name, None)
            return callable(callback) and (
                self.legacy_callable is callback or self.legacy_callable == callback
            )
        return (
            owner is not None
            and self.owner_identity == _owner_identity(owner)
            and self.owner_type is type(owner)
            and self.method_name == method_name
        )


class ReloadableCallbackRegistry:
    """Central adapter for runtime listener lists without strong owners."""

    _live_registries: "weakref.WeakSet[ReloadableCallbackRegistry]" = weakref.WeakSet()

    def __init__(self) -> None:
        self._callbacks: list[ReloadableCallbackRef] = []
        self._live_registries.add(self)

    def add_listener(
        self,
        callback: Callable[..., Any],
        *,
        expected_signature: Optional[str] = None,
    ) -> ReloadableCallbackRef:
        reference = ReloadableCallbackRef.normalize(
            callback,
            expected_signature=expected_signature,
        )
        for existing in self._callbacks:
            if existing.matches(callback):
                return existing
        self._callbacks.append(reference)
        return reference

    def remove_listener(self, callback: Callable[..., Any] | ReloadableCallbackRef) -> None:
        if isinstance(callback, ReloadableCallbackRef):
            self._callbacks = [item for item in self._callbacks if item != callback]
            return
        self._callbacks = [item for item in self._callbacks if not item.matches(callback)]

    def remove_all_listeners(self) -> None:
        self._callbacks.clear()

    def invoke(
        self,
        *args: Any,
        epoch: Optional[RuntimeRevisionEpoch] = None,
        propagate_exceptions: bool = False,
    ) -> tuple[CallbackInvocation, ...]:
        # Resolve the epoch once per batch.  Callers that already captured an
        # owner-safe-point epoch can pass it explicitly; legacy callers retain
        # the previous current-epoch behavior.
        selected_epoch = current_runtime_epoch() if epoch is None else epoch
        results = []
        for reference in tuple(self._callbacks):
            result = reference.invoke(
                *args,
                epoch=selected_epoch,
                propagate_exceptions=propagate_exceptions,
            )
            results.append(result)
            if result.status in {"owner_unavailable", "owner_invalid"}:
                self.remove_listener(reference)
        return tuple(results)

    @property
    def listener_count(self) -> int:
        return len(self._callbacks)

    def validate_epoch(
        self,
        epoch: RuntimeRevisionEpoch,
        component_types: Iterable[type],
    ) -> None:
        touched = set(component_types)
        for reference in tuple(self._callbacks):
            if reference.is_legacy or reference.owner_type not in touched:
                continue
            owner = reference.owner_ref() if reference.owner_ref is not None else None
            if owner is None or bool(getattr(owner, "_is_destroyed", False)):
                continue
            resolution = reference.resolve(epoch)
            if resolution.status in {"method_missing", "signature_mismatch"}:
                raise RuntimeError(resolution.message)


def _safe_signature(callback: Callable[..., Any]) -> str:
    try:
        return str(inspect.signature(callback))
    except (TypeError, ValueError):
        return "<unknown>"


def _owner_identity(owner: Any) -> RuntimeOwnerIdentity:
    return RuntimeOwnerIdentity(
        int(getattr(owner, "component_id", 0) or id(owner)),
        int(getattr(owner, "_native_generation", 0) or 0),
    )


def _descriptor_for_type(
    epoch: RuntimeRevisionEpoch,
    component_type: type,
) -> RuntimeTypeDispatchDescriptor:
    descriptor = epoch.descriptor_for(component_type)
    if descriptor is not None:
        return descriptor
    return build_type_dispatch_descriptor(component_type, epoch_id=epoch.epoch_id)


def validate_runtime_callback_bindings(
    epoch: RuntimeRevisionEpoch,
    component_types: Iterable[type],
) -> None:
    """Reject a publication that would orphan an active bound callback."""
    for registry in tuple(ReloadableCallbackRegistry._live_registries):
        registry.validate_epoch(epoch, component_types)


class RuntimeDispatchPublication:
    """Owner-side publication token that can restore the previous epoch."""

    __slots__ = (
        "before",
        "after",
        "_defer_commit",
        "_published",
        "_mirror_snapshots",
        "_mirror_types",
        "_retired_types",
        "_instances_by_type",
        "_sync_compatibility",
        "_scheduler_changes",
        "_scheduler_notified",
        "_rolled_back",
        "_committed",
    )

    def __init__(
        self,
        before: RuntimeRevisionEpoch,
        after: RuntimeRevisionEpoch,
        *,
        defer_commit: bool = False,
        published: bool = True,
        mirror_snapshots: tuple[object, ...] = (),
        mirror_types: tuple[type, ...] = (),
        retired_types: tuple[type, ...] = (),
        instances_by_type: Optional[Mapping[type, Iterable[Any]]] = None,
        sync_compatibility: bool = True,
        scheduler_changes: tuple[object, ...] = (),
    ) -> None:
        self.before = before
        self.after = after
        self._defer_commit = bool(defer_commit)
        self._published = bool(published)
        self._mirror_snapshots = mirror_snapshots
        self._mirror_types = mirror_types
        self._retired_types = retired_types
        self._instances_by_type = {
            component_type: tuple(values)
            for component_type, values in (instances_by_type or {}).items()
        }
        self._sync_compatibility = bool(sync_compatibility)
        self._scheduler_changes = scheduler_changes
        self._scheduler_notified = False
        self._rolled_back = False
        self._committed = False

    @property
    def committed(self) -> bool:
        return self._committed and not self._rolled_back

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    def commit(self) -> None:
        if self._rolled_back:
            raise RuntimeError("runtime dispatch publication has been rolled back")
        if self._committed:
            return
        if self._defer_commit:
            with _EPOCH_LOCK:
                if _current_epoch is not self.before:
                    raise RuntimeError(
                        "runtime dispatch publication lost its owner transaction ordering"
                    )
                try:
                    _apply_compatibility_publication(
                        self.after,
                        self._mirror_types,
                        self._retired_types,
                        self._instances_by_type,
                        sync_compatibility=self._sync_compatibility,
                    )
                    _set_current_epoch(self.after)
                    self._published = True
                except Exception:
                    _restore_mirror_snapshots(self._mirror_snapshots)
                    self._published = False
                    raise
        self._committed = True
        self._notify_scheduler_changes()
        _notify_coroutine_epoch(self.after)

    def _notify_scheduler_changes(self) -> None:
        if self._scheduler_notified:
            return
        for scheduler, component_type, phase_presence_changed in self._scheduler_changes:
            scheduler.mark_type_body_reload(
                component_type,
                phase_presence_changed=phase_presence_changed,
            )
        self._scheduler_notified = True

    def rollback(self) -> None:
        if self._rolled_back:
            return
        with _EPOCH_LOCK:
            # Publications are owner-serialized.  Do not overwrite a later
            # publication if a caller violates that contract.
            if self._published and _current_epoch is self.after:
                _set_current_epoch(self.before)
            _restore_mirror_snapshots(self._mirror_snapshots)
        if self._scheduler_notified:
            # The scheduler invalidation is deliberately symmetric: a
            # committed rollback is another topology/body publication from
            # the scheduler's perspective, including retired types.
            for scheduler, component_type, phase_presence_changed in self._scheduler_changes:
                try:
                    scheduler.mark_type_body_reload(
                        component_type,
                        phase_presence_changed=phase_presence_changed,
                    )
                except Exception:
                    # The immutable epoch and compatibility mirrors above are
                    # authoritative.  A failed observer must not strand a
                    # half-rolled-back script transaction; schedulers already
                    # notified by the forward commit remain invalidated, and
                    # untouched schedulers still own the original plan.
                    pass
            self._scheduler_notified = False
        self._committed = False
        self._rolled_back = True
        _notify_coroutine_epoch(self.before)


_EPOCH_LOCK = threading.RLock()
_current_epoch = RuntimeRevisionEpoch(0, {})
_owner_thread_id: Optional[int] = None


def current_runtime_epoch() -> RuntimeRevisionEpoch:
    """Return the current immutable epoch reference."""
    with _EPOCH_LOCK:
        return _current_epoch


def assert_runtime_dispatch_safe_point() -> None:
    """Reject publication while any scheduler still owns an execution frame."""
    global _owner_thread_id
    current_thread_id = threading.get_ident()
    with _EPOCH_LOCK:
        if _owner_thread_id is None:
            _owner_thread_id = current_thread_id
        elif _owner_thread_id != current_thread_id:
            raise RuntimeError(
                "runtime dispatch publication requires the owner thread; "
                f"owner={_owner_thread_id}, current={current_thread_id}"
            )
    try:
        from Infernux.components._component_lifecycle import RuntimeExecutionScheduler
    except ImportError:
        return
    for scheduler in tuple(RuntimeExecutionScheduler._live_schedulers):
        if getattr(scheduler, "_native_frame", None) is not None:
            raise RuntimeError(
                "runtime dispatch publication requires an owner safe point; "
                "a native frame is still active"
            )
        active_frames = getattr(scheduler, "_active_frames", ())
        if tuple(active_frames):
            raise RuntimeError(
                "runtime dispatch publication requires an owner safe point; "
                "an execution frame is still active"
            )


def build_type_dispatch_descriptor(
    component_type: type,
    *,
    epoch_id: int = 0,
) -> RuntimeTypeDispatchDescriptor:
    """Build a descriptor from the current stable class body, without publish."""
    phase_methods: list[Optional[RuntimeMethodDescriptor]] = []
    for phase in RUNTIME_PHASE_NAMES:
        raw = _find_raw_method(component_type, phase)
        phase_methods.append(
            None if raw is _MISSING else _make_method_descriptor(component_type, phase, raw)
        )

    methods: dict[str, RuntimeMethodDescriptor] = {}
    # First MRO hit matches normal Python attribute resolution.  Persistent
    # component callbacks may intentionally use a single leading underscore
    # (for example an asset invalidation hook), so only dunder implementation
    # names are excluded here.  This table is built at publication time; it
    # does not add reflection to the frame hot path.
    for base in component_type.__mro__:
        for name, raw in base.__dict__.items():
            if name in methods or name in RUNTIME_PHASE_NAMES:
                continue
            if name.startswith("__"):
                continue
            if isinstance(raw, property) or not isinstance(
                raw, (staticmethod, classmethod)
            ) and not callable(raw):
                continue
            methods[name] = _make_method_descriptor(component_type, name, raw)

    return RuntimeTypeDispatchDescriptor(
        component_type=component_type,
        epoch_id=int(epoch_id),
        phase_methods=tuple(phase_methods),
        methods=methods,
    )


def _find_raw_method(component_type: type, name: str) -> Any:
    for base in component_type.__mro__:
        candidate = base.__dict__.get(name, _MISSING)
        if candidate is not _MISSING:
            if bool(getattr(candidate, "__infernux_default_lifecycle__", False)):
                return _MISSING
            return candidate
    return _MISSING


def _make_method_descriptor(component_type: type, name: str, raw: Any) -> RuntimeMethodDescriptor:
    if isinstance(raw, staticmethod):
        return RuntimeMethodDescriptor(name, raw.__func__, "static")
    if isinstance(raw, classmethod):
        return RuntimeMethodDescriptor(name, getattr(component_type, name), "class")
    return RuntimeMethodDescriptor(name, raw, "instance")


def ensure_runtime_compatibility_mirror(
    component_type: type,
) -> RuntimeTypeDispatchDescriptor:
    """Lazily expose the current epoch descriptor to legacy callers.

    A few direct lifecycle/native proxy entry points still read the historic
    class-owned tables.  Lightweight mixins and test doubles do not pass
    through ``InxComponent.__init_subclass__``, so their first direct phase
    call must establish the mirror as well.  This is deliberately a
    cache-miss-only operation: steady-state phase dispatch reads the already
    published class table and performs no reflection or bound-method
    allocation.
    """
    class_dict = component_type.__dict__
    cached = class_dict.get("_runtime_dispatch_compatibility_descriptor")
    if cached is not None:
        return cached

    current = current_runtime_epoch()
    descriptor = current.descriptor_for(component_type)
    if descriptor is None:
        if bool(component_type.__dict__.get("_runtime_dispatch_retired", False)):
            descriptor = _build_retired_type_descriptor(
                component_type,
                epoch_id=current.epoch_id,
            )
        else:
            descriptor = build_type_dispatch_descriptor(
                component_type,
                epoch_id=current.epoch_id,
            )
    if (
        "_runtime_phase_dispatch" not in class_dict
        or "_runtime_phase_invokers" not in class_dict
    ):
        component_type._runtime_phase_dispatch = descriptor.phase_dispatch
        component_type._runtime_phase_invokers = descriptor.phase_invokers
    else:
        # A legacy class may have been initialized before it entered an epoch.
        # Preserve its already-published immutable table identity when the
        # helper is queried again, rather than rebuilding a second descriptor.
        object.__setattr__(descriptor, "_phase_dispatch", class_dict["_runtime_phase_dispatch"])
        object.__setattr__(descriptor, "_phase_invokers", class_dict["_runtime_phase_invokers"])
    component_type._runtime_dispatch_compatibility_descriptor = descriptor
    return descriptor


def _set_compatibility_mirrors(
    descriptor: RuntimeTypeDispatchDescriptor,
    instances: Iterable[Any],
) -> None:
    component_type = descriptor.component_type
    # These mirrors are retained for existing lifecycle/native bridge callers;
    # frame execution itself reads the immutable epoch descriptor.
    component_type._runtime_phase_dispatch = descriptor.phase_dispatch
    component_type._runtime_phase_invokers = descriptor.phase_invokers
    component_type._runtime_dispatch_compatibility_descriptor = descriptor
    invokers = component_type.__dict__.get("_runtime_phase_invokers")
    live_instances = list(instances)
    active_registry = getattr(component_type, "_active_instances", {})
    for active in active_registry.values():
        live_instances.extend(active)
    seen: set[int] = set()
    for component in live_instances:
        if id(component) in seen or type(component) is not component_type:
            continue
        seen.add(id(component))
        try:
            component.__dict__["_runtime_phase_invokers_instance"] = invokers
            lifecycle_cache = component.__dict__.get("_lifecycle_dispatch_cache")
            if lifecycle_cache is not None:
                lifecycle_cache.clear()
        except (AttributeError, TypeError):
            pass
        native = getattr(component, "_cpp_component", None)
        refresh_native = getattr(native, "refresh_python_lifecycle_dispatch", None)
        if callable(refresh_native):
            try:
                refresh_native()
            except (AttributeError, ReferenceError, RuntimeError):
                pass


def _restore_mirror_snapshots(snapshots: Iterable[object]) -> None:
    """Restore class/instance compatibility mirrors after owner rollback."""
    for entry in snapshots:
        try:
            component_type, class_snapshot, instance_snapshots = entry
        except (TypeError, ValueError):
            continue
        for name, (present, value) in class_snapshot.items():
            if present:
                setattr(component_type, name, value)
            elif name in component_type.__dict__:
                delattr(component_type, name)
        for component, snapshot in instance_snapshots:
            values = getattr(component, "__dict__", None)
            if values is None:
                continue
            for name, (present, value) in snapshot.items():
                if present:
                    values[name] = value
                else:
                    values.pop(name, None)
            native = getattr(component, "_cpp_component", None)
            refresh_native = getattr(native, "refresh_python_lifecycle_dispatch", None)
            if callable(refresh_native):
                try:
                    refresh_native()
                except (AttributeError, ReferenceError, RuntimeError):
                    pass


def _apply_compatibility_publication(
    epoch: RuntimeRevisionEpoch,
    types: Iterable[type],
    retired: Iterable[type],
    instances_by_type: Mapping[type, Iterable[Any]],
    *,
    sync_compatibility: bool,
) -> None:
    """Apply class/instance mirrors only at the owner transaction edge."""
    if sync_compatibility:
        for component_type in types:
            descriptor = epoch.descriptor_for(component_type)
            if descriptor is None:
                continue
            values = instances_by_type.get(component_type, ())
            _set_compatibility_mirrors(descriptor, values)
    for component_type in retired:
        class_dict = component_type.__dict__
        setattr(component_type, "_runtime_dispatch_retired", True)
        for name in (
            "_runtime_phase_dispatch",
            "_runtime_phase_invokers",
            "_runtime_dispatch_compatibility_descriptor",
        ):
            if name in class_dict:
                delattr(component_type, name)
        active_registry = getattr(component_type, "_active_instances", {})
        for active in tuple(active_registry.values()):
            for component in tuple(active):
                values = getattr(component, "__dict__", None)
                if values is not None:
                    values.pop("_runtime_phase_invokers_instance", None)
                    values.pop("_lifecycle_dispatch_cache", None)
    for component_type in types:
        if "_runtime_dispatch_retired" in component_type.__dict__:
            delattr(component_type, "_runtime_dispatch_retired")


def _set_current_epoch(epoch: RuntimeRevisionEpoch) -> None:
    global _current_epoch
    _current_epoch = epoch


def _notify_coroutine_epoch(epoch: RuntimeRevisionEpoch) -> None:
    """Publish committed epoch state to coroutine diagnostics."""
    try:
        from Infernux.coroutine import notify_runtime_epoch_published

        notify_runtime_epoch_published(epoch)
    except Exception:
        # Coroutine epoch observation is diagnostic/cache state, not the
        # publication authority.  It must never make commit/rollback partial.
        pass


def ensure_runtime_dispatch_types(component_types: Iterable[type]) -> RuntimeRevisionEpoch:
    """Publish descriptors for types not yet present in the current epoch."""
    types = tuple(dict.fromkeys(component_types))
    current = current_runtime_epoch()
    missing = tuple(
        component_type
        for component_type in types
        if component_type not in current.descriptors
        and not bool(
            component_type.__dict__.get("_runtime_dispatch_retired", False)
        )
    )
    if not missing:
        return current
    publication = publish_runtime_dispatch_epoch(missing)
    publication.commit()
    return publication.after


def publish_runtime_dispatch_epoch(
    component_types: Iterable[type],
    instances_by_type: Optional[Mapping[type, Iterable[Any]]] = None,
    *,
    sync_compatibility: bool = True,
    retired_types: Iterable[type] = (),
    defer_commit: bool = False,
) -> RuntimeDispatchPublication:
    """Atomically stage one owner-side epoch publication.

    ``retired_types`` removes types from the new epoch.  With
    ``defer_commit=True`` mirror state is snapshotted and the publication is
    staged, but mirrors and the current epoch pointer are applied only at
    ``publication.commit()``.  Script move/delete transactions use this so a
    failed transaction cannot retire a type early.
    """
    types = tuple(dict.fromkeys(component_types))
    retired = tuple(
        component_type
        for component_type in dict.fromkeys(retired_types)
        if component_type not in types
    )
    changed_types = tuple(dict.fromkeys((*types, *retired)))
    if changed_types:
        assert_runtime_dispatch_safe_point()
    mirror_snapshots = []
    if sync_compatibility or retired:
        for component_type in changed_types:
            values = () if instances_by_type is None else instances_by_type.get(component_type, ())
            live_instances = list(values)
            active_registry = getattr(component_type, "_active_instances", {})
            for active in active_registry.values():
                live_instances.extend(active)
            seen: set[int] = set()
            instance_snapshots = []
            for component in live_instances:
                if id(component) in seen or type(component) is not component_type:
                    continue
                seen.add(id(component))
                try:
                    values_dict = component.__dict__
                except AttributeError:
                    continue
                instance_snapshots.append(
                    (
                        component,
                        {
                            name: (name in values_dict, values_dict.get(name))
                            for name in (
                                "_runtime_phase_invokers_instance",
                                "_lifecycle_dispatch_cache",
                            )
                        },
                    )
                )
            mirror_snapshots.append(
                (
                    component_type,
                    {
                        name: (
                            name in component_type.__dict__,
                            component_type.__dict__.get(name),
                        )
                        for name in (
                            "_runtime_phase_dispatch",
                            "_runtime_phase_invokers",
                            "_runtime_dispatch_compatibility_descriptor",
                            "_runtime_dispatch_retired",
                        )
                    },
                    tuple(instance_snapshots),
                )
            )
    with _EPOCH_LOCK:
        before = _current_epoch
        if not changed_types:
            return RuntimeDispatchPublication(before, before, published=True)
        next_id = before.epoch_id + 1
        descriptors = dict(before.descriptors)
        for component_type in types:
            descriptor = build_type_dispatch_descriptor(component_type, epoch_id=next_id)
            descriptors[component_type] = descriptor
        for component_type in retired:
            descriptors.pop(component_type, None)
        after = RuntimeRevisionEpoch(next_id, descriptors)
        try:
            validate_runtime_callback_bindings(after, types)
            if not defer_commit:
                _apply_compatibility_publication(
                    after,
                    types,
                    retired,
                    instances_by_type or {},
                    sync_compatibility=sync_compatibility,
                )
            # The epoch pointer is the final owner-safe-point operation.  A
            # deferred owner transaction performs it only at commit.
            if not defer_commit:
                _set_current_epoch(after)
        except Exception:
            for component_type, class_snapshot, instance_snapshots in mirror_snapshots:
                for name, (present, value) in class_snapshot.items():
                    if present:
                        setattr(component_type, name, value)
                    elif name in component_type.__dict__:
                        delattr(component_type, name)
                for component, snapshot in instance_snapshots:
                    values = component.__dict__
                    for name, (present, value) in snapshot.items():
                        if present:
                            values[name] = value
                        else:
                            values.pop(name, None)
            raise

    # Body reload changes dispatch, not topology.  Scheduler invalidation is
    # stored in the publication and runs only after commit.
    scheduler_changes = []
    try:
        from Infernux.components._component_lifecycle import RuntimeExecutionScheduler

        for scheduler in tuple(RuntimeExecutionScheduler._live_schedulers):
            for component_type in changed_types:
                before_descriptor = before.descriptor_for(component_type)
                after_descriptor = after.descriptor_for(component_type)
                phase_changed = (
                    before_descriptor is None
                    or after_descriptor is None
                    or before_descriptor.phase_presence != after_descriptor.phase_presence
                )
                scheduler_changes.append(
                    (scheduler, component_type, phase_changed)
                )
    except (ImportError, AttributeError):
        pass
    return RuntimeDispatchPublication(
        before,
        after,
        defer_commit=defer_commit,
        published=not defer_commit,
        mirror_snapshots=tuple(mirror_snapshots),
        mirror_types=types,
        retired_types=retired,
        instances_by_type=instances_by_type,
        sync_compatibility=sync_compatibility,
        scheduler_changes=tuple(scheduler_changes),
    )


def resolve_runtime_method(
    instance: Any,
    name: str,
    *,
    epoch: Optional[RuntimeRevisionEpoch] = None,
) -> Any:
    """Resolve a public helper against a chosen epoch.

    Passing a frame's epoch makes helper lookup observe the same revision as
    its phase callbacks.  ``None`` uses the current owner-published epoch.
    """
    selected = current_runtime_epoch() if epoch is None else epoch
    return selected.resolve_method(instance, name)


def has_runtime_phase(component_type: type, phase: str) -> bool:
    epoch = current_runtime_epoch()
    descriptor = epoch.descriptor_for(component_type)
    if descriptor is not None:
        return descriptor.has_phase(phase)
    if bool(component_type.__dict__.get("_runtime_dispatch_retired", False)):
        return False
    return build_type_dispatch_descriptor(component_type).has_phase(phase)


def runtime_descriptor_diagnostic(component_type: type) -> dict[str, Any]:
    """Return a cheap current-epoch descriptor status for editor diagnostics."""
    epoch = current_runtime_epoch()
    descriptor = epoch.descriptor_for(component_type)
    if descriptor is None:
        return {
            "epoch_id": epoch.epoch_id,
            "type_name": getattr(component_type, "__qualname__", str(component_type)),
            "status": (
                "retired"
                if bool(component_type.__dict__.get("_runtime_dispatch_retired", False))
                else "absent"
            ),
            "phase_presence": (False, False, False),
            "method_count": 0,
        }
    return {
        "epoch_id": epoch.epoch_id,
        "type_name": getattr(component_type, "__qualname__", str(component_type)),
        "status": "live",
        "phase_presence": descriptor.phase_presence,
        "method_count": len(descriptor.methods),
    }
