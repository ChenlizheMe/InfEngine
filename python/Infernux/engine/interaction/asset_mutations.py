"""Typed asset path mutations shared by every editor surface."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import uuid
from typing import Callable, Iterable, Optional, TypeAlias

from Infernux.engine.path_utils import resolved_path, same_path

from .action_journal import ActionOrigin
from .documents import DocumentRegistry
from ..runtime_dispatch import (
    ReloadableCallbackRef,
    ReloadableCallbackRegistry,
    current_runtime_epoch,
)
from .selection import SelectionService


class AssetMutationKind(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    MOVED = "moved"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class AssetMutation:
    """One authoritative asset identity/path change.

    The filesystem and AssetDatabase have already committed when this value is
    published. Editor projections consume it as a consequence of that single
    mutation; they must not create another Undo action.
    """

    kind: AssetMutationKind
    source_path: str
    destination_path: str = ""
    guid: str = ""
    origin: ActionOrigin = ActionOrigin.SYSTEM
    operation_id: str = ""

    def __post_init__(self) -> None:
        source = resolved_path(self.source_path)
        kind = AssetMutationKind(self.kind)
        destination = resolved_path(self.destination_path) if self.destination_path else ""
        if not source:
            raise ValueError("asset mutation requires a source path")
        if kind is AssetMutationKind.MOVED:
            if not destination or same_path(source, destination):
                raise ValueError("asset move requires two different paths")
        elif destination:
            raise ValueError(f"{kind.value} asset mutation must not have a destination")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source_path", source)
        object.__setattr__(self, "destination_path", destination)
        object.__setattr__(self, "guid", str(self.guid or "").strip())
        object.__setattr__(self, "origin", ActionOrigin(self.origin))
        object.__setattr__(
            self,
            "operation_id",
            str(self.operation_id or uuid.uuid4().hex),
        )

    @property
    def path(self) -> str:
        """Return the current path after this mutation has committed."""
        return self.destination_path if self.kind is AssetMutationKind.MOVED else self.source_path

    @property
    def previous_path(self) -> str:
        return self.source_path if self.kind is AssetMutationKind.MOVED else ""


@dataclass(frozen=True, slots=True)
class AssetMutationChange:
    mutation: AssetMutation
    remapped_document_ids: tuple[str, ...] = ()
    selection_changed: bool = False


@dataclass(frozen=True, slots=True)
class AssetContentChange:
    """One committed create/reimport/delete notification.

    Content notifications never own Undo. They are consequences of an asset
    database transaction and only invalidate live projections.
    """

    mutation: AssetMutation
    revision: int

    def __post_init__(self) -> None:
        if self.mutation.kind is AssetMutationKind.MOVED:
            raise ValueError("asset moves must use AssetRelocationChange")
        if self.revision <= 0:
            raise ValueError("asset content change requires a positive revision")

    @property
    def mutations(self) -> tuple[AssetMutation, ...]:
        return (self.mutation,)


@dataclass(frozen=True, slots=True)
class AssetRelocationPlan:
    """Preflighted, immutable description of one editor asset operation."""

    mutations: tuple[AssetMutation, ...]
    operation_id: str
    origin: ActionOrigin

    def __post_init__(self) -> None:
        if not self.mutations:
            raise ValueError("asset relocation plan requires at least one mutation")
        operation_id = str(self.operation_id or "").strip()
        if not operation_id:
            raise ValueError("asset relocation plan requires an operation id")
        if any(mutation.operation_id != operation_id for mutation in self.mutations):
            raise ValueError("asset relocation mutations must share one operation id")
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "origin", ActionOrigin(self.origin))

    def inverse(self, *, origin: ActionOrigin | None = None) -> "AssetRelocationPlan":
        inverse_origin = self.origin if origin is None else ActionOrigin(origin)
        operation_id = uuid.uuid4().hex
        mutations = tuple(
            AssetMutation(
                mutation.kind,
                mutation.destination_path,
                mutation.source_path,
                mutation.guid,
                inverse_origin,
                operation_id,
            )
            for mutation in reversed(self.mutations)
        )
        return AssetRelocationPlan(mutations, operation_id, inverse_origin)


@dataclass(frozen=True, slots=True)
class AssetRelocationChange:
    """One publication barrier for a file rename, move, or directory move."""

    plan: AssetRelocationPlan
    changes: tuple[AssetMutationChange, ...]

    @property
    def operation_id(self) -> str:
        return self.plan.operation_id

    @property
    def mutations(self) -> tuple[AssetMutation, ...]:
        return self.plan.mutations


AssetMutationNotification: TypeAlias = AssetContentChange | AssetRelocationChange


def iter_asset_mutations(
    change: AssetMutationNotification | AssetMutationChange | AssetMutation,
) -> tuple[AssetMutation, ...]:
    """Flatten every public mutation notification into typed mutations."""
    if isinstance(change, AssetMutation):
        return (change,)
    if isinstance(change, AssetMutationChange):
        return (change.mutation,)
    if isinstance(change, AssetContentChange):
        return change.mutations
    if isinstance(change, AssetRelocationChange):
        return change.mutations
    raise TypeError(f"unsupported asset mutation notification: {type(change).__name__}")


class AssetMutationService:
    """Project-session authority for propagating asset identity changes."""

    _instance: Optional["AssetMutationService"] = None

    def __init__(
        self,
        documents: DocumentRegistry,
        selection: SelectionService,
    ) -> None:
        self._documents = documents
        self._selection = selection
        # Editor observers are deliberately pinned.  They are not game
        # callbacks and must keep their creation-time semantics.
        self._listeners: list[Callable[[AssetMutationNotification], None]] = []
        # Component-owned callbacks use weak owners and resolve their method
        # body through the current runtime epoch at notification time.
        self._component_listeners = ReloadableCallbackRegistry()
        # One-shot work is intentionally pinned until the next publication;
        # it is a transaction callback, not a persistent runtime binding.
        self._one_shot_listeners: list[
            Callable[[AssetMutationNotification], None]
        ] = []
        self._reported_component_states: set[tuple[ReloadableCallbackRef, int, str]] = set()
        self._paths_by_guid: dict[str, str] = {}
        self._prepared: dict[str, AssetRelocationPlan] = {}
        self._revision = 0
        AssetMutationService._instance = self

    @classmethod
    def instance(cls) -> Optional["AssetMutationService"]:
        return cls._instance

    @property
    def revision(self) -> int:
        return self._revision

    def add_observer(
        self,
        callback: Callable[[AssetMutationNotification], None],
    ) -> None:
        """Register a pinned editor observer.

        This path is for panels, caches, and ordinary functions.  It never
        enters the game runtime epoch and keeps the historical strong-callable
        behavior used by editor code.
        """
        if not callable(callback):
            raise TypeError("asset observer must be callable")
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_observer(
        self,
        callback: Callable[[AssetMutationNotification], None],
    ) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    # Compatibility aliases for existing editor observers.
    add_listener = add_observer
    remove_listener = remove_observer

    def add_component_listener(
        self,
        callback: Callable[[AssetMutationNotification], None],
        *,
        expected_signature: Optional[str] = None,
    ) -> ReloadableCallbackRef:
        """Register an ``InxComponent`` bound method as a persistent callback.

        The explicit API prevents ordinary editor callbacks from accidentally
        becoming runtime-reloadable.  The registry holds only a weak owner
        reference, so destroy/GC naturally removes the subscription.
        """
        owner = getattr(callback, "__self__", None)
        function = getattr(callback, "__func__", None)
        try:
            from Infernux.components.component import InxComponent
        except ImportError as exc:
            raise RuntimeError("InxComponent is unavailable") from exc
        if not isinstance(owner, InxComponent) or function is None:
            raise TypeError(
                "persistent asset callbacks must be InxComponent bound methods"
            )
        return self._component_listeners.add_listener(
            callback,
            expected_signature=expected_signature,
        )

    def remove_component_listener(
        self,
        callback: Callable[[AssetMutationNotification], None] | ReloadableCallbackRef,
    ) -> None:
        self._component_listeners.remove_listener(callback)
        if isinstance(callback, ReloadableCallbackRef):
            self._reported_component_states = {
                item
                for item in self._reported_component_states
                if item[0] != callback
            }

    def add_one_shot_listener(
        self,
        callback: Callable[[AssetMutationNotification], None],
    ) -> None:
        """Run a pinned transaction callback once on the next publication."""
        if not callable(callback):
            raise TypeError("one-shot asset callback must be callable")
        self._one_shot_listeners.append(callback)

    @property
    def listener_diagnostics(self) -> dict[str, int]:
        """Return subscription counts without exposing mutable registries."""
        return {
            "editor_observers": len(self._listeners),
            "component_callbacks": self._component_listeners.listener_count,
            "one_shot_callbacks": len(self._one_shot_listeners),
        }

    def resolve_path_hint(self, guid: str, fallback: str = "") -> str:
        return self._paths_by_guid.get(str(guid or "").strip(), str(fallback or ""))

    def prepare_relocation(
        self,
        entries: Iterable[tuple[str, str, str]],
        *,
        origin: ActionOrigin = ActionOrigin.SYSTEM,
        operation_id: str = "",
    ) -> AssetRelocationPlan:
        """Validate every editor projection before the workspace is mutated."""
        action_origin = ActionOrigin(origin)
        operation = str(operation_id or uuid.uuid4().hex)
        mutations = tuple(
            AssetMutation(
                AssetMutationKind.MOVED,
                source,
                destination,
                guid,
                action_origin,
                operation,
            )
            for source, destination, guid in entries
        )
        plan = AssetRelocationPlan(mutations, operation, action_origin)
        source_keys = {mutation.source_path.casefold() for mutation in mutations}
        destination_keys = {mutation.destination_path.casefold() for mutation in mutations}
        if len(source_keys) != len(mutations) or len(destination_keys) != len(mutations):
            raise ValueError("asset relocation contains duplicate source or destination paths")
        self._documents.preflight_resource_remaps(
            (mutation.source_path, mutation.destination_path) for mutation in mutations
        )
        if operation in self._prepared:
            raise RuntimeError(f"asset relocation operation is already prepared: {operation}")
        self._prepared[operation] = plan
        return plan

    def abort_relocation(self, plan: AssetRelocationPlan) -> None:
        if self._prepared.get(plan.operation_id) is plan:
            self._prepared.pop(plan.operation_id, None)

    def commit_relocation(self, plan: AssetRelocationPlan) -> AssetRelocationChange:
        """Publish a preflighted relocation once, after the workspace commit."""
        if self._prepared.get(plan.operation_id) is not plan:
            raise RuntimeError("asset relocation plan is not prepared by this session")

        applied: list[AssetMutation] = []
        changes: list[AssetMutationChange] = []
        try:
            for mutation in plan.mutations:
                if mutation.guid:
                    self._paths_by_guid[mutation.guid] = mutation.destination_path
                document_ids = self._documents.remap_resource_path(
                    mutation.source_path,
                    mutation.destination_path,
                    guid=mutation.guid,
                    title=os.path.splitext(os.path.basename(mutation.destination_path))[0],
                )
                selection_changed = self._selection.remap_asset_path(
                    mutation.source_path,
                    mutation.destination_path,
                    reason="asset_moved",
                )
                changes.append(AssetMutationChange(mutation, document_ids, selection_changed))
                applied.append(mutation)
        except Exception:
            for mutation in reversed(applied):
                self._documents.remap_resource_path(
                    mutation.destination_path,
                    mutation.source_path,
                    guid=mutation.guid,
                    title=os.path.splitext(os.path.basename(mutation.source_path))[0],
                )
                self._selection.remap_asset_path(
                    mutation.destination_path,
                    mutation.source_path,
                    reason="asset_move_rollback",
                )
                if mutation.guid:
                    self._paths_by_guid[mutation.guid] = mutation.source_path
            raise
        finally:
            self._prepared.pop(plan.operation_id, None)

        relocation = AssetRelocationChange(plan, tuple(changes))
        self._revision += 1
        self._notify(relocation)
        return relocation

    def publish_content_change(
        self,
        path: str,
        kind: AssetMutationKind,
        *,
        guid: str = "",
        origin: ActionOrigin = ActionOrigin.SYSTEM,
        operation_id: str = "",
    ) -> AssetContentChange:
        """Publish one committed create, reimport, or delete consequence."""
        mutation_kind = AssetMutationKind(kind)
        if mutation_kind is AssetMutationKind.MOVED:
            raise ValueError("asset moves require source and destination paths")
        mutation = AssetMutation(
            mutation_kind,
            path,
            guid=guid,
            origin=origin,
            operation_id=operation_id,
        )
        if mutation.guid:
            if mutation.kind is AssetMutationKind.DELETED:
                self._paths_by_guid.pop(mutation.guid, None)
            else:
                self._paths_by_guid[mutation.guid] = mutation.path
        if (
            mutation.origin is ActionOrigin.EXTERNAL
            and mutation.kind in {
                AssetMutationKind.MODIFIED,
                AssetMutationKind.DELETED,
            }
        ):
            self._documents.publish_external_resource_change(
                mutation.path,
                deleted=mutation.kind is AssetMutationKind.DELETED,
            )
        self._revision += 1
        change = AssetContentChange(mutation, self._revision)
        self._notify(change)
        return change

    def _notify(self, change: AssetMutationNotification) -> None:
        # Capture once for the whole publication.  A callback cannot observe
        # two different runtime bodies while the same asset event is being
        # dispatched.
        epoch = current_runtime_epoch()
        for callback in tuple(self._listeners):
            try:
                callback(change)
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("AssetMutationService.listener", exc)

        results = self._component_listeners.invoke(change, epoch=epoch)
        for result in results:
            reference = result.reference
            if result.status in {"owner_unavailable", "owner_invalid"}:
                self._reported_component_states = {
                    item
                    for item in self._reported_component_states
                    if item[0] != reference
                }
                continue
            if result.status == "exception":
                from Infernux.debug import Debug

                Debug.log_suppressed(
                    "AssetMutationService.component_listener",
                    RuntimeError(result.message),
                )
                continue
            if result.status in {"method_missing", "signature_mismatch"}:
                diagnostic = (reference, epoch.epoch_id, result.status)
                if diagnostic not in self._reported_component_states:
                    self._reported_component_states.add(diagnostic)
                    from Infernux.debug import Debug

                    Debug.log_warning(
                        "AssetMutationService persistent component callback skipped: "
                        f"{reference.method_name}: {result.message}"
                    )

        one_shot = tuple(self._one_shot_listeners)
        for callback in one_shot:
            try:
                callback(change)
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("AssetMutationService.one_shot", exc)
            finally:
                try:
                    self._one_shot_listeners.remove(callback)
                except ValueError:
                    pass

    def publish_move(
        self,
        source_path: str,
        destination_path: str,
        *,
        guid: str = "",
        origin: ActionOrigin = ActionOrigin.SYSTEM,
        operation_id: str = "",
    ) -> AssetMutationChange:
        plan = self.prepare_relocation(
            ((source_path, destination_path, guid),),
            origin=origin,
            operation_id=operation_id,
        )
        return self.commit_relocation(plan).changes[0]

    def shutdown(self) -> None:
        self._listeners.clear()
        self._component_listeners.remove_all_listeners()
        self._one_shot_listeners.clear()
        self._reported_component_states.clear()
        self._prepared.clear()
        self._paths_by_guid.clear()
        if AssetMutationService._instance is self:
            AssetMutationService._instance = None
