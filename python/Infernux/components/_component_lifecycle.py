"""ComponentLifecycleMixin — extracted from InxComponent."""
from __future__ import annotations

"""
InxComponent - Base class for all Python-defined components.

Provides Unity-style lifecycle methods and property injection.
Users inherit from this class to create custom game logic.

Example:
    from Infernux.components import InxComponent, serialized_field
    
    class PlayerController(InxComponent):
        speed: float = serialized_field(default=5.0)
        
        def start(self):
            print("Player started!")
        
        def update(self, delta_time: float):
            pos = self.transform.position
            self.transform.position = Vector3(pos.x + self.speed * delta_time, pos.y, pos.z)
"""

from typing import Optional, Dict, Any, Type, TYPE_CHECKING, List
import copy
import threading
import weakref
from collections import defaultdict

from Infernux.lib import GameObject


_RUNTIME_PHASE_NAMES = ("update", "fixed_update", "late_update")
_RUNTIME_SCHEDULER_PHASES = ("fixed_update", "update", "late_update")
_MISSING = object()


def _missing_runtime_phase(*_args):
    """No-op phase for lightweight mixins that omit optional callbacks."""
    return None


class RuntimeExecutionScheduler:
    """Shared on-demand runtime plan/cache used by Editor and Player.

    Native ``Scene`` remains the owner of the normal bound lifecycle path.
    Python-side execution uses :class:`RuntimeExecutionFrame` so a frame has
    one immutable component/dispatch snapshot. Structural events keep the
    revision and component set current, while steady native frames never
    prepare or inspect the plan.
    """

    _live_schedulers = weakref.WeakSet()

    def __init__(self, *, name: str = "runtime") -> None:
        self.name = str(name)
        self._components: dict[tuple[int, int], Any] = {}
        self._component_tokens: dict[int, tuple[int, int]] = {}
        self._phase_plan: dict[str, tuple[Any, ...]] = {
            phase: () for phase in _RUNTIME_SCHEDULER_PHASES
        }
        self._structure_revision = 0
        self._plan_revision = -1
        self._registry_scan_pending = True
        self._type_revisions: dict[type, int] = {}
        self._counters = defaultdict(int)
        self._live_schedulers.add(self)

    @classmethod
    def _notify_component_structure(cls, component: Any, *, present: bool) -> None:
        for scheduler in tuple(cls._live_schedulers):
            scheduler._on_component_structure(component, present=present)

    @classmethod
    def _notify_component_revision(cls, component: Any) -> None:
        for scheduler in tuple(cls._live_schedulers):
            scheduler._on_component_structure(component, present=True)

    @staticmethod
    def _component_token(component: Any) -> tuple[int, int]:
        component_id = int(getattr(component, "component_id", 0) or getattr(component, "_component_id", 0) or id(component))
        generation = int(getattr(component, "_native_generation", 0) or 0)
        return component_id, generation

    @staticmethod
    def _sort_key(component: Any) -> tuple[int, int, int]:
        return (
            int(getattr(component, "execution_order", getattr(component, "_execution_order", 0)) or 0),
            int(getattr(component, "component_id", getattr(component, "_component_id", 0)) or 0),
            int(getattr(component, "_native_generation", 0) or 0),
        )

    def _invalidate(self) -> None:
        self._structure_revision += 1
        self._counters["plan_invalidations"] += 1

    def _on_component_structure(self, component: Any, *, present: bool) -> None:
        self._registry_scan_pending = False
        if present:
            token = self._component_token(component)
            previous = self._component_tokens.get(id(component))
            self._component_tokens[id(component)] = token
            self._components[token] = component
            if previous is not None and previous != token:
                self._components.pop(previous, None)
        else:
            previous = self._component_tokens.pop(id(component), None)
            if previous is not None:
                self._components.pop(previous, None)
        self._invalidate()

    def register_component(self, component: Any) -> None:
        """Register a component for direct scheduler users and tests."""
        self._on_component_structure(component, present=True)

    def unregister_component(self, component: Any) -> None:
        self._on_component_structure(component, present=False)

    def mark_structure_changed(self, reason: str = "") -> None:
        """Invalidate the plan for topology/enabled/order changes."""
        del reason
        self._invalidate()

    def mark_type_body_reload(self, component_type: type) -> None:
        """Publish a new type dispatch generation without rebuilding the plan."""
        self._type_revisions[component_type] = self._type_revisions.get(component_type, 0) + 1
        self._counters["body_reload_updates"] += 1

    def mark_runtime_revision(self, revision: int) -> None:
        """Apply an external structural revision monotonically."""
        revision = int(revision)
        if revision > self._structure_revision:
            self._structure_revision = revision
            self._counters["plan_invalidations"] += 1

    def _sync_active_registry_once(self) -> None:
        if not self._registry_scan_pending:
            return
        self._registry_scan_pending = False
        try:
            from .component import InxComponent

            active = {}
            for values in tuple(getattr(InxComponent, "_active_instances", {}).values()):
                for component in tuple(values):
                    active[self._component_token(component)] = component
        except (ImportError, AttributeError, RuntimeError):
            active = {}
        if set(active) != set(self._components):
            self._components = active
            self._component_tokens = {id(component): token for token, component in active.items()}
            self._invalidate()

    @staticmethod
    def _has_phase(component: Any, phase: str) -> bool:
        dispatch = _runtime_phase_dispatch_for_type(type(component))
        index = _RUNTIME_PHASE_NAMES.index(phase)
        return dispatch[index][0] is not _missing_runtime_phase

    @staticmethod
    def _eligible(component: Any) -> bool:
        return not bool(getattr(component, "_is_destroyed", False)) and bool(
            getattr(component, "_enabled", True)
        )

    def _build_plan(self) -> None:
        components = sorted(
            (component for component in self._components.values() if self._eligible(component)),
            key=self._sort_key,
        )
        self._phase_plan = {
            phase: tuple(component for component in components if self._has_phase(component, phase))
            for phase in _RUNTIME_SCHEDULER_PHASES
        }
        self._plan_revision = self._structure_revision
        self._counters["plan_builds"] += 1

    def begin_frame(self) -> "RuntimeExecutionFrame":
        """Capture the executable plan at the only Python frame boundary.

        A body-only script reload replaces the class-owned invoker cache.  A
        running frame must not observe that replacement halfway through its
        fixed/update/late phases, so the frame owns the invoker tuples it will
        use.  The capture is deliberately separate from component execution:
        no Python lifecycle callback can mutate the snapshot while it is being
        assembled.
        """
        phase_plan = self.prepare_frame()
        components = {}
        for phase_components in phase_plan.values():
            for component in phase_components:
                component_key = id(component)
                if component_key in components:
                    continue
                invokers = component.__dict__.get("_runtime_phase_invokers_instance")
                if invokers is None:
                    invokers = _runtime_phase_invokers(component)
                    component.__dict__["_runtime_phase_invokers_instance"] = invokers
                components[component_key] = (component, invokers)

        frame = RuntimeExecutionFrame(
            self,
            phase_plan,
            components,
            structure_revision=self._structure_revision,
            type_revisions=dict(self._type_revisions),
        )
        self._counters["frame_begins"] += 1
        return frame

    def _execute_frame_phase(
        self,
        frame: "RuntimeExecutionFrame",
        phase: str,
        delta_time: float,
    ) -> None:
        phase_index = _RUNTIME_PHASE_NAMES.index(phase)
        for component in frame.phase_plan[phase]:
            if not self._eligible(component):
                self._counters["phase_skips"] += 1
                continue

            _component, invokers = frame.component_snapshots[id(component)]
            try:
                invokers[phase_index](component, delta_time)
            except Exception as exc:
                reporter = getattr(component, "_report_lifecycle_exception", None)
                if callable(reporter):
                    reporter(exc)
                else:
                    # Keep the scheduler useful for lightweight test/runtime
                    # component doubles without weakening real component error
                    # isolation.
                    self._counters["phase_errors"] += 1

            if phase == "update":
                coroutine_scheduler = component.__dict__.get(
                    "_runtime_coroutine_scheduler"
                )
                if coroutine_scheduler is not None:
                    coroutine_scheduler.tick_update(delta_time)
            elif phase == "fixed_update":
                coroutine_scheduler = component.__dict__.get(
                    "_runtime_coroutine_scheduler"
                )
                if coroutine_scheduler is not None:
                    coroutine_scheduler.tick_fixed_update(delta_time)
            else:
                coroutine_scheduler = component.__dict__.get(
                    "_runtime_coroutine_scheduler"
                )
                if coroutine_scheduler is not None:
                    coroutine_scheduler.tick_late_update(delta_time)
            self._counters["phase_dispatches"] += 1

    def execute_frame(
        self,
        fixed_delta_time: float,
        delta_time: float,
        late_delta_time: Optional[float] = None,
    ) -> None:
        """Execute all lifecycle phases against one dispatch snapshot.

        This is the explicit Python runtime entry point used by headless and
        test compositions.  The graphical editor and packaged Player still
        use the native Scene callback path, but they can share the same frame
        boundary when they opt into Python lifecycle execution.
        """
        frame = self.begin_frame()
        try:
            frame.execute_phase("fixed_update", fixed_delta_time)
            frame.execute_phase("update", delta_time)
            frame.execute_phase(
                "late_update",
                delta_time if late_delta_time is None else late_delta_time,
            )
        finally:
            frame.close()

    def prepare_frame(self) -> dict[str, tuple[Any, ...]]:
        """Explicitly build or return the plan for the current revision."""
        self._sync_active_registry_once()
        if self._plan_revision == self._structure_revision:
            self._counters["plan_hits"] += 1
        else:
            self._build_plan()
        self._counters["plan_prepare_calls"] += 1
        return self._phase_plan

    def phase_plan(self, phase: str) -> tuple[Any, ...]:
        if phase not in _RUNTIME_SCHEDULER_PHASES:
            raise ValueError(f"unknown runtime phase: {phase}")
        return self.prepare_frame()[phase]

    def execute_phase(self, phase: str, delta_time: float) -> None:
        """Explicit Python-side execution entry for headless/test composition.

        The graphical Editor and packaged Player use native Scene dispatch;
        this method exists for runtimes that intentionally compose phases in
        Python and uses the exact same component wrappers and error isolation.
        """
        if phase not in _RUNTIME_SCHEDULER_PHASES:
            raise ValueError(f"unknown runtime phase: {phase}")
        frame = self.begin_frame()
        try:
            frame.execute_phase(phase, delta_time)
        finally:
            frame.close()

    def profiler_snapshot(self) -> dict[str, int]:
        """Return cheap integer counters; no formatting or logging occurs."""
        return dict(self._counters)

    def reset_profiler(self) -> None:
        self._counters.clear()

    def clear(self) -> None:
        """Release plan references at runtime shutdown without a new service."""
        for frame in tuple(getattr(self, "_active_frames", ())):
            frame.close()
        self._components.clear()
        self._component_tokens.clear()
        self._phase_plan = {phase: () for phase in _RUNTIME_SCHEDULER_PHASES}
        self._registry_scan_pending = True
        self._invalidate()


class RuntimeExecutionFrame:
    """One immutable lifecycle execution view.

    The scheduler owns topology revisions; this object additionally owns the
    invoker tuples.  Keeping both together prevents a Play hot reload from
    changing ``update`` while a caller is still finishing ``late_update``.
    """

    def __init__(
        self,
        scheduler: RuntimeExecutionScheduler,
        phase_plan: dict[str, tuple[Any, ...]],
        component_snapshots: dict[int, tuple[Any, tuple[Any, ...]]],
        *,
        structure_revision: int,
        type_revisions: dict[type, int],
    ) -> None:
        self._scheduler = scheduler
        self.phase_plan = {phase: tuple(values) for phase, values in phase_plan.items()}
        self.component_snapshots = dict(component_snapshots)
        self.structure_revision = int(structure_revision)
        self.type_revisions = dict(type_revisions)
        self._closed = False
        active_frames = getattr(scheduler, "_active_frames", None)
        if active_frames is None:
            active_frames = weakref.WeakSet()
            scheduler._active_frames = active_frames
        active_frames.add(self)

    def execute_phase(self, phase: str, delta_time: float) -> None:
        if self._closed:
            raise RuntimeError("runtime execution frame is already closed")
        if phase not in _RUNTIME_SCHEDULER_PHASES:
            raise ValueError(f"unknown runtime phase: {phase}")
        self._scheduler._execute_frame_phase(self, phase, delta_time)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active_frames = getattr(self._scheduler, "_active_frames", None)
        if active_frames is not None:
            active_frames.discard(self)


def _build_runtime_phase_dispatch(component_type):
    """Build the immutable phase call table for one component class.

    Normal lifecycle methods are stored as unbound functions so the hot path
    can call ``fn(component, dt)`` without doing a string lookup or creating a
    bound method.  Static/class methods retain their old descriptor semantics
    through the small ``bind_instance`` flag; they are unusual, but should not
    silently change behavior just because dispatch was optimized.
    """
    dispatch = []
    for name in _RUNTIME_PHASE_NAMES:
        raw = _MISSING
        for base in component_type.__mro__:
            candidate = base.__dict__.get(name, _MISSING)
            if candidate is not _MISSING:
                raw = candidate
                break
        if raw is _MISSING:
            # Match normal attribute lookup for unusual mixins/proxies.
            dispatch.append((_missing_runtime_phase, True))
        elif isinstance(raw, staticmethod):
            dispatch.append((raw.__func__, False))
        elif isinstance(raw, classmethod):
            dispatch.append((getattr(component_type, name), False))
        else:
            dispatch.append((raw, True))
    return tuple(dispatch)


def _runtime_phase_dispatch(component):
    """Return a class-owned immutable dispatch table, building it once."""
    component_type = type(component)
    # Read the class' own entry rather than an inherited table.  This matters
    # for test doubles and for a body-only reload that publishes a new Python
    # class derived from an existing component type.
    dispatch = component_type.__dict__.get("_runtime_phase_dispatch")
    if dispatch is None:
        dispatch = _build_runtime_phase_dispatch(component_type)
        component_type._runtime_phase_dispatch = dispatch
    return dispatch


def _build_runtime_phase_invokers(component_type):
    """Build callables that erase descriptor branching from the frame path.

    The native proxy already caches the wrapper methods.  This second cache is
    deliberately per Python type: it turns the normal instance-method case
    into one callable invocation and keeps static/classmethod compatibility in
    the uncommon cases without inspecting descriptors during a frame.
    """
    invokers = []
    for method, bind_instance in _runtime_phase_dispatch_for_type(component_type):
        if bind_instance:
            def invoke(component, value, _method=method):
                return _method(component, value)
        else:
            def invoke(component, value, _method=method):
                return _method(value)
        invokers.append(invoke)
    return tuple(invokers)


def _runtime_phase_dispatch_for_type(component_type):
    """Return a type table without requiring a component instance."""
    dispatch = component_type.__dict__.get("_runtime_phase_dispatch")
    if dispatch is None:
        dispatch = _build_runtime_phase_dispatch(component_type)
        component_type._runtime_phase_dispatch = dispatch
    return dispatch


def _runtime_phase_invokers(component):
    """Return the cached invokers for *component*, lazily for test doubles."""
    component_type = type(component)
    invokers = component_type.__dict__.get("_runtime_phase_invokers")
    if invokers is None:
        invokers = _build_runtime_phase_invokers(component_type)
        component_type._runtime_phase_invokers = invokers
    return invokers


def refresh_runtime_dispatch_cache(component_type, instances=()):
    """Refresh body/lifecycle dispatch after an in-place script patch."""
    dispatch = _build_runtime_phase_dispatch(component_type)
    component_type._runtime_phase_dispatch = dispatch
    invokers = _build_runtime_phase_invokers(component_type)
    component_type._runtime_phase_invokers = invokers

    live_instances = list(instances)
    active_registry = getattr(component_type, "_active_instances", {})
    for active in active_registry.values():
        live_instances.extend(active)

    seen = set()
    for component in live_instances:
        if id(component) in seen or type(component) is not component_type:
            continue
        seen.add(id(component))
        component.__dict__["_runtime_phase_invokers_instance"] = invokers
        lifecycle_cache = component.__dict__.get("_lifecycle_dispatch_cache")
        if lifecycle_cache is not None:
            lifecycle_cache.clear()
        native = getattr(component, "_cpp_component", None)
        refresh_native = getattr(native, "refresh_python_lifecycle_dispatch", None)
        if callable(refresh_native):
            try:
                refresh_native()
            except (AttributeError, ReferenceError, RuntimeError):
                # A component can be detached while the editor drains a scene
                # transaction; the Python class patch itself remains valid.
                pass

    # A body-only patch changes the type-owned invoker generation, not the
    # scene topology.  Keep every existing scene plan and only publish the
    # new descriptor generation to schedulers that are observing this runtime.
    for scheduler in tuple(RuntimeExecutionScheduler._live_schedulers):
        scheduler.mark_type_body_reload(component_type)


def notify_runtime_component_added(component: Any) -> None:
    """Notify shared runtime plans about a newly bound component."""
    RuntimeExecutionScheduler._notify_component_structure(component, present=True)


def notify_runtime_component_removed(component: Any) -> None:
    """Notify shared runtime plans about a detached/destroyed component."""
    RuntimeExecutionScheduler._notify_component_structure(component, present=False)


def notify_runtime_component_changed(component: Any) -> None:
    """Invalidate plans after enable/order/topology changes."""
    RuntimeExecutionScheduler._notify_component_revision(component)


class ComponentLifecycleMixin:
    """ComponentLifecycleMixin method group for InxComponent."""

    def _safe_lifecycle_call(self, method_name: str, *args) -> bool:
        """Call *method_name* on self, catching and logging any exception."""
        try:
            # Lifecycle entry points are stable for a component instance. A
            # script reload publishes a new instance, so cache the bound
            # method and avoid allocating a new bound method on every phase.
            # Keep the owning class in the entry so unusual in-place class
            # replacement still refreshes the cache conservatively.
            cache = self.__dict__.get("_lifecycle_dispatch_cache")
            if cache is None:
                cache = {}
                self.__dict__["_lifecycle_dispatch_cache"] = cache
            entry = cache.get(method_name)
            component_type = type(self)
            if entry is None or entry[0] is not component_type:
                entry = (component_type, getattr(self, method_name))
                cache[method_name] = entry
            entry[1](*args)
            return True
        except Exception as exc:
            # Route to DebugConsole so the Console Panel shows the error.
            try:
                from Infernux.debug import debug
                debug.log_exception(exc, context=self)
            except (ImportError, RuntimeError):
                # Absolute fallback if debug itself cannot be imported.
                import traceback
                traceback.print_exc()
            return False

    def _report_lifecycle_exception(self, exc: Exception) -> None:
        """Route a phase exception without adding work to the success path."""
        try:
            from Infernux.debug import debug
            debug.log_exception(exc, context=self)
        except (ImportError, RuntimeError):
            import traceback
            traceback.print_exc()

    def _disable_after_awake_exception(self):
        """Unity disables a script component if its Awake throws."""
        self._enabled = False

        cpp_component = getattr(self, '_cpp_component', None)
        if cpp_component is None:
            return

        try:
            cpp_component.enabled = False
        except RuntimeError:
            self._invalidate_native_binding()

    def _call_awake(self):
        """Internal: Trigger awake lifecycle."""
        if self._awake_called:
            return
        self._awake_called = True
        if not self._safe_lifecycle_call("awake"):
            self._disable_after_awake_exception()

    def _call_start(self):
        """Internal: Trigger start lifecycle if not already called."""
        if self._has_started:
            return
        self._has_started = True
        self._safe_lifecycle_call("start")

    def _call_update(self, delta_time: float):
        """Internal: Trigger update lifecycle."""
        # The native proxy already checked its authoritative enabled bit before
        # entering Python.  Reading ``self.enabled`` here performed another
        # scene/handle lookup for every component and every frame.  The mirror
        # is updated by bind/enable/disable transitions, so this is sufficient
        # for direct Python callers as well.
        if not self._enabled:
            return
        try:
            invokers = self.__dict__.get("_runtime_phase_invokers_instance")
            if invokers is None:
                invokers = _runtime_phase_invokers(self)
                self.__dict__["_runtime_phase_invokers_instance"] = invokers
            invokers[0](self, delta_time)
        except Exception as exc:
            self._report_lifecycle_exception(exc)
        # Tick coroutines after user update (matching Unity order).  Keep the
        # scheduler local so the phase does not perform another attribute
        # lookup or call a second wrapper method.
        scheduler = self.__dict__.get("_runtime_coroutine_scheduler")
        if scheduler is not None:
            scheduler.tick_update(delta_time)

    def _call_fixed_update(self, fixed_delta_time: float):
        """Internal: Trigger fixed_update lifecycle."""
        if not self._enabled:
            return
        try:
            invokers = self.__dict__.get("_runtime_phase_invokers_instance")
            if invokers is None:
                invokers = _runtime_phase_invokers(self)
                self.__dict__["_runtime_phase_invokers_instance"] = invokers
            invokers[1](self, fixed_delta_time)
        except Exception as exc:
            self._report_lifecycle_exception(exc)
        scheduler = self.__dict__.get("_runtime_coroutine_scheduler")
        if scheduler is not None:
            scheduler.tick_fixed_update(fixed_delta_time)

    def _call_late_update(self, delta_time: float):
        """Internal: Trigger late_update lifecycle."""
        if not self._enabled:
            return
        try:
            invokers = self.__dict__.get("_runtime_phase_invokers_instance")
            if invokers is None:
                invokers = _runtime_phase_invokers(self)
                self.__dict__["_runtime_phase_invokers_instance"] = invokers
            invokers[2](self, delta_time)
        except Exception as exc:
            self._report_lifecycle_exception(exc)
        scheduler = self.__dict__.get("_runtime_coroutine_scheduler")
        if scheduler is not None:
            scheduler.tick_late_update(delta_time)

    def _call_on_destroy(self):
        """Internal: Trigger on_destroy lifecycle."""
        if self._is_destroyed:
            return  # Already destroyed, don't call again
        self._is_destroyed = True
        self._enabled = False
        # Stop all coroutines before on_destroy callback
        if self._coroutine_scheduler is not None:
            self._coroutine_scheduler.stop_all()
            self._sync_native_coroutine_scheduler_state()
            self._coroutine_scheduler = None
        # Remove from active-instances registry (safety net; _set_game_object(None)
        # should have done this already, but guard against missed calls)
        self._remove_from_active_registry()
        if self._awake_called:
            self._safe_lifecycle_call("on_destroy")
        dispatch_cache = self.__dict__.get("_lifecycle_dispatch_cache")
        if dispatch_cache is not None:
            dispatch_cache.clear()
        self.__dict__.pop("_runtime_phase_invokers_instance", None)
        self.__dict__["_runtime_coroutine_scheduler"] = None
        # Clear references to help garbage collection
        self._cpp_component = None
        self._game_object = None
        self._game_object_ref = None
        self._release_component_data_slot()

    def _release_component_data_slot(self):
        """Relinquish the numeric-field slot exactly once."""
        cds_slot = getattr(self, '_cds_slot', None)
        cds_class_id = getattr(self, '_cds_class_id', None)
        self._cds_slot = None
        self._cds_class_id = None
        if cds_slot is None or cds_class_id is None:
            return
        from ._cds_bridge import release_slot as _cds_free
        _cds_free(self.__class__, cds_slot, cds_class_id)

    def _call_on_enable(self):
        """Internal: Trigger on_enable lifecycle."""
        self._enabled = True
        notify_runtime_component_changed(self)
        self._safe_lifecycle_call("on_enable")

    def _call_on_disable(self):
        """Internal: Trigger on_disable lifecycle."""
        self._enabled = False
        notify_runtime_component_changed(self)
        self._safe_lifecycle_call("on_disable")

    def _call_on_validate(self):
        """Internal: Trigger on_validate lifecycle (editor only)."""
        self._safe_lifecycle_call("on_validate")

    def _call_reset(self):
        """Internal: Trigger reset lifecycle (editor only)."""
        self._safe_lifecycle_call("reset")

    def _call_on_after_deserialize(self):
        """Trigger the transactional post-deserialize hook and propagate failure."""
        self.on_after_deserialize()

    def _call_on_before_serialize(self):
        """Internal: Trigger on_before_serialize lifecycle."""
        self._safe_lifecycle_call("on_before_serialize")

