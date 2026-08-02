"""Undo / Redo system — command-pattern undo stack for the editor.

Provides :class:`UndoManager` (the stack) and a family of
:class:`UndoCommand` subclasses for property edits, structural changes
(create/delete/reparent), component add/remove, material edits, and
render-stack mutations.

Example::

    from Infernux.engine.undo import UndoManager, SetPropertyCommand

    mgr = UndoManager.instance()
    mgr.execute(SetPropertyCommand(comp, "speed", old, new, "Set speed"))
    mgr.undo()
    mgr.redo()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional


# ── Base command ──────────────────────────────────────────────────────

class UndoCommand(ABC):
    """Abstract base for all undoable editor operations."""

    description: str
    timestamp: float
    supports_redo: bool
    marks_dirty: bool
    before_selection_snapshot: Any
    after_selection_snapshot: Any

    def __init__(self, description: str = "") -> None: ...

    @abstractmethod
    def execute(self) -> None: ...

    @abstractmethod
    def undo(self) -> None: ...

    def redo(self) -> None:
        """Default redo delegates to ``execute()``."""
        ...

    def can_merge(self, other: UndoCommand) -> bool:
        """Return ``True`` if *other* can be merged into this command."""
        ...

    def merge(self, other: UndoCommand) -> None: ...
    def dispose(self) -> None: ...


class LambdaCommand(UndoCommand):
    """One-shot undoable operation defined by callable undo/redo functions.

    Useful for recording arbitrary operations (e.g. ShaderGraph edits)
    without defining a full ``UndoCommand`` subclass::

        cmd = LambdaCommand(
            undo_fn=lambda: restore_state(snapshot),
            redo_fn=lambda: apply_change(new_data),
            description="Edit shader node",
        )
        UndoManager.instance().execute(cmd)
    """

    def __init__(
        self,
        description: str,
        undo_fn: Callable[[], None],
        redo_fn: Callable[[], None],
        marks_dirty: bool = True,
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


# ── Property commands ─────────────────────────────────────────────────

class SetPropertyCommand(UndoCommand):
    """Set a serialized-field property on a component."""

    MERGE_WINDOW: float

    def __init__(
        self,
        target: Any,
        prop_name: str,
        old_value: Any,
        new_value: Any,
        description: str = "",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: SetPropertyCommand) -> None: ...


class GenericComponentCommand(UndoCommand):
    """Snapshot-based undo for a full native-component document."""

    MERGE_WINDOW: float

    def __init__(
        self,
        comp: Any,
        old_document: dict[str, Any],
        new_document: dict[str, Any],
        description: str = "",
        *,
        mergeable: bool = True,
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: GenericComponentCommand) -> None: ...

class PythonComponentDocumentCommand(UndoCommand):
    """Snapshot-based undo for a complete Python-component field document."""

    MERGE_WINDOW: float

    def __init__(
        self,
        comp: Any,
        old_document: dict[str, Any],
        new_document: dict[str, Any],
        description: str = "",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: PythonComponentDocumentCommand) -> None: ...


class BuiltinPropertyCommand(UndoCommand):
    """Set a C++ built-in component property via its setter."""

    MERGE_WINDOW: float

    def __init__(
        self,
        comp: Any,
        cpp_attr: str,
        old_value: Any,
        new_value: Any,
        description: str = "",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: BuiltinPropertyCommand) -> None: ...


# ── Structural commands ──────────────────────────────────────────────

class CreateGameObjectCommand(UndoCommand):
    """Records the creation of a new GameObject."""

    supports_redo: bool

    def __init__(
        self,
        object_id: int,
        description: str = "Create GameObject",
        *,
        before_selection: Any = None,
        after_selection: Any = None,
    ) -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


class DeleteGameObjectCommand(UndoCommand):
    """Records the deletion of a GameObject (restorable on undo)."""

    def __init__(self, object_id: int, description: str = "Delete GameObject") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


class DeleteGameObjectsCommand(UndoCommand):
    """Records an atomic multi-selection hierarchy deletion."""

    def __init__(self, object_ids: list[int], description: str = "Delete GameObjects") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


class ReparentCommand(UndoCommand):
    """Records a parent-change operation on a GameObject."""

    def __init__(
        self,
        object_id: int,
        old_parent_id: Optional[int],
        new_parent_id: Optional[int],
        description: str = "Reparent",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


class MoveGameObjectCommand(UndoCommand):
    """Records a parent-change *and* sibling-order change."""

    def __init__(
        self,
        object_id: int,
        old_parent_id: Optional[int],
        new_parent_id: Optional[int],
        old_sibling_index: int,
        new_sibling_index: int,
        description: str = "Move",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


# ── Material commands ─────────────────────────────────────────────────

class MaterialDocumentCommand(UndoCommand):
    """Snapshot-based undo for a material document."""

    MERGE_WINDOW: float
    marks_dirty: bool

    def __init__(
        self,
        material: Any,
        old_document: dict[str, Any],
        new_document: dict[str, Any],
        description: str = "",
        refresh_callback: Optional[Callable[[Any], None]] = None,
        edit_key: str = "",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: MaterialDocumentCommand) -> None: ...

class ResourceDocumentCommand(UndoCommand):
    marks_dirty: bool
    def __init__(self, resource: Any, old_document: dict[str, Any], new_document: dict[str, Any],
                 description: str = ..., publish_callback: Optional[Callable[[Any], None]] = ...,
                 edit_key: str = ...) -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: ResourceDocumentCommand) -> None: ...


# ── RenderStack commands ──────────────────────────────────────────────

class RenderStackSetPipelineCommand(UndoCommand):
    def __init__(self, stack: Any, old_pipeline: str, new_pipeline: str, description: str = "") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class RenderStackFieldCommand(UndoCommand):
    MERGE_WINDOW: float
    def __init__(self, stack: Any, target: Any, field_name: str, old_value: Any, new_value: Any, description: str = "") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: RenderStackFieldCommand) -> None: ...

class RenderStackTogglePassCommand(UndoCommand):
    MERGE_WINDOW: float
    def __init__(self, stack: Any, pass_name: str, old_enabled: bool, new_enabled: bool, description: str = "") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: RenderStackTogglePassCommand) -> None: ...

class RenderStackMovePassCommand(UndoCommand):
    def __init__(self, stack: Any, old_orders: dict[str, int], new_orders: dict[str, int], description: str = "") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class RenderStackAddPassCommand(UndoCommand):
    def __init__(self, stack: Any, effect_cls: type, description: str = "Add Effect") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class RenderStackRemovePassCommand(UndoCommand):
    def __init__(self, stack: Any, pass_name: str, description: str = "Remove Effect") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


# ── Component add/remove commands ─────────────────────────────────────

class AddNativeComponentCommand(UndoCommand):
    def __init__(self, object_id: int, type_name: str, comp_ref: Any = None, description: str = "Add Component") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class RemoveNativeComponentCommand(UndoCommand):
    def __init__(self, object_id: int, type_name: str, comp_ref: Any, description: str = "Remove Component") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class AddPyComponentCommand(UndoCommand):
    supports_redo: bool

    def __init__(self, object_id: int, py_comp_ref: Any, description: str = "Add Script") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...

class RemovePyComponentCommand(UndoCommand):
    supports_redo: bool

    def __init__(self, object_id: int, py_comp_ref: Any, description: str = "Remove Script") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...


class ProjectAssetRenameCommand(UndoCommand):
    """Records a GUID-stable rename in the global editor history."""

    marks_dirty: bool

    def __init__(
        self,
        old_path: str,
        new_path: str,
        *,
        asset_database: Any = None,
        on_changed: Optional[Callable[[], None]] = None,
        move_fn: Optional[Callable[[str, str, Any], Optional[str]]] = None,
        description: str = "Rename Asset",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class ProjectAssetDeleteCommand(UndoCommand):
    marks_dirty: bool
    def __init__(self, paths: List[str], *, project_root: str = "", backup_root: str = "", asset_database: Any = ..., on_deleted: Optional[Callable[[], None]] = ..., on_restored: Optional[Callable[[], None]] = ..., delete_fn: Optional[Callable[[str, Any], bool]] = ..., import_fn: Optional[Callable[[str, Any], bool]] = ..., description: str = "Delete Assets") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def dispose(self) -> None: ...

class ProjectAssetMoveCommand(UndoCommand):
    marks_dirty: bool
    def __init__(self, source_path: str, destination_path: str, *, asset_database: Any = ..., move_fn: Optional[Callable[[str, str, Any], Optional[str]]] = ..., description: str = "Move Asset") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...

class ProjectAssetCopyCommand(UndoCommand):
    marks_dirty: bool
    def __init__(self, source_path: str, destination_path: str, *, project_root: str = "", backup_root: str = "", asset_database: Any = ..., copy_fn: Optional[Callable[[str, str, Any], Optional[str]]] = ..., delete_fn: Optional[Callable[[str, Any], bool]] = ..., import_fn: Optional[Callable[[str, Any], bool]] = ..., description: str = "Copy Asset") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def dispose(self) -> None: ...

class ProjectAssetPasteCommand(UndoCommand):
    marks_dirty: bool
    def __init__(self, commands: List[UndoCommand], result_paths: List[str], *, on_applied: Optional[Callable[[List[str]], None]] = ..., on_reverted: Optional[Callable[[], None]] = ..., description: str = "Paste Assets") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def dispose(self) -> None: ...

class GlobalSelectionCommand(UndoCommand):
    """Records a typed global editor selection change."""

    marks_dirty: bool

    def __init__(
        self,
        old_snapshot: Any,
        new_snapshot: Any,
        apply_fn: Callable[[Any], None],
        description: str = "",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...


class CompoundCommand(UndoCommand):
    """Groups multiple commands into a single undo step."""

    supports_redo: bool

    def __init__(self, commands: List[UndoCommand], description: str = "") -> None: ...
    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def dispose(self) -> None: ...


# ── Inspector undo helpers ────────────────────────────────────────────

class InspectorSnapshotCommand(UndoCommand):
    """Snapshot-based undo for inspector edit sessions."""

    MERGE_WINDOW: float
    marks_dirty: bool

    def __init__(
        self,
        target_key: str,
        old_snapshot: str,
        new_snapshot: str,
        restore_fn: Callable[[str], None],
        description: str = "",
    ) -> None: ...

    def execute(self) -> None: ...
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_merge(self, other: UndoCommand) -> bool: ...
    def merge(self, other: InspectorSnapshotCommand) -> None: ...


class InspectorUndoTracker:
    """Per-frame undo tracking for multi-widget inspector edits."""

    def __init__(self) -> None: ...

    def begin_frame(self) -> None:
        """Call at the start of each inspector render frame."""
        ...

    def mark_all_active(self) -> None: ...

    def track(
        self,
        key: str,
        snapshot_fn: Callable[[], str],
        restore_fn: Callable[[str], None],
        description: str = "",
    ) -> None:
        """Register a trackable widget.

        Args:
            key: Unique identifier for this tracked item.
            snapshot_fn: Returns the current JSON state.
            restore_fn: Applies a JSON state string.
            description: Human-readable undo label.
        """
        ...

    def end_frame(self, any_item_active: Optional[bool] = None) -> None:
        """Call at the end of each inspector render frame to commit changes."""
        ...


class HierarchyUndoTracker:
    """Convenience methods for recording structural hierarchy changes."""

    def record_create(self, object_id: int, description: str = "Create GameObject") -> None: ...
    def record_delete(self, object_id: int, description: str = "Delete GameObject") -> None: ...
    def record_rename(self, object_id: int, old_name: str, new_name: str, description: str = "Rename GameObject") -> None: ...
    def record_reparent(self, object_id: int, old_parent_id: Optional[int], new_parent_id: Optional[int], description: str = "Reparent") -> None: ...
    def record_move(self, object_id: int, old_parent_id: Optional[int], new_parent_id: Optional[int], old_index: int, new_index: int, description: str = "Move") -> None: ...


class TimelinePropertyCommand(UndoCommand):
    def __init__(self, timeline: Any, document_id: str, target_id: str, field_name: str, old_value: Any, new_value: Any, before_revision: int, after_revision: int, description: str = "Edit Timeline") -> None: ...


class TimelineInsertKeyframeCommand(UndoCommand):
    def __init__(self, timeline: Any, document_id: str, keyframe: Any, index: int, before_revision: int, after_revision: int, description: str = "Add Timeline Keyframe") -> None: ...
    @property
    def stable_id(self) -> str: ...


class TimelineRemoveKeyframeCommand(UndoCommand):
    def __init__(self, timeline: Any, document_id: str, keyframe: Any, index: int, before_revision: int, after_revision: int, description: str = "Delete Timeline Keyframe") -> None: ...
    @property
    def stable_id(self) -> str: ...


# ── RenderStack snapshot helpers ──────────────────────────────────────

def snapshot_renderstack(stack: Any) -> dict[str, Any]:
    """Snapshot a RenderStack as a document."""
    ...

def restore_renderstack(stack: Any, document: dict[str, Any]) -> None:
    """Restore a RenderStack from a document snapshot."""
    ...


# ── UndoManager ──────────────────────────────────────────────────────

class UndoManager:
    """The central undo/redo stack.

    Example::

        mgr = UndoManager.instance()
        mgr.execute(SetPropertyCommand(comp, "speed", 1.0, 2.0, "Set speed"))
        mgr.undo()
        mgr.redo()
    """

    MAX_STACK_DEPTH: int

    @classmethod
    def instance(cls) -> Optional[UndoManager]:
        """Return the singleton, or ``None`` if not yet created."""
        ...

    def __init__(self, journal: Any = None) -> None: ...

    @property
    def action_journal(self) -> Any: ...

    def set_context_hooks(
        self,
        provider: Optional[Callable[[], Any]],
        restorer: Optional[Callable[[Any, str], None]],
    ) -> None: ...

    @contextmanager
    def transaction(self, description: str) -> Iterator[Any]: ...

    @contextmanager
    def suppress(self) -> Iterator[None]:
        """Context manager: suppress undo recording inside the block."""
        ...

    @contextmanager
    def suppress_property_recording(self) -> Iterator[None]:
        """Context manager: suppress only serialized-field will_change hooks."""
        ...

    @property
    def is_executing(self) -> bool:
        """``True`` while an undo/redo operation is in progress."""
        ...

    @property
    def can_undo(self) -> bool: ...

    @property
    def can_redo(self) -> bool: ...

    @property
    def undo_description(self) -> str:
        """Human-readable label for the next undo operation."""
        ...

    @property
    def redo_description(self) -> str:
        """Human-readable label for the next redo operation."""
        ...

    @property
    def enabled(self) -> bool: ...

    @enabled.setter
    def enabled(self, value: bool) -> None: ...

    def execute(
        self,
        cmd: UndoCommand,
        *,
        origin: Any = ...,
        transaction_id: str = "",
    ) -> bool:
        """Execute *cmd* and push it onto the undo stack.

        Args:
            cmd: The command to execute. If it can merge with the stack
                 top, the merge is performed instead of pushing a new entry.

        Returns:
            ``True`` when execution succeeded, otherwise ``False``.
        """
        ...

    def record(
        self,
        cmd: UndoCommand,
        *,
        origin: Any = ...,
        transaction_id: str = "",
        before_context: Any = None,
        after_context: Any = None,
    ) -> None:
        """Push *cmd* without executing it (for externally-applied changes)."""
        ...

    def undo(self) -> None:
        """Undo the most recent command."""
        ...

    def redo(self) -> None:
        """Redo the most recently undone command."""
        ...

    def clear(self, scene_is_dirty: bool = False) -> None:
        """Clear both undo and redo stacks."""
        ...

    def set_scene_dirty_baseline(self, scene_is_dirty: bool) -> None:
        """Update scene dirty baseline without clearing stacks."""
        ...

    def mark_save_point(self) -> None:
        """Record the current stack depth as the "saved" point."""
        ...
    @property
    def is_at_save_point(self) -> bool:
        """``True`` when the undo depth matches the last save point."""
        ...
    def sync_dirty_state(self) -> None:
        """Push dirty/clean state to the :class:`SceneFileManager`."""
        ...

    def set_on_state_changed(self, cb: Optional[Callable[[], None]]) -> None:
        """Register a callback fired whenever the undo state changes."""
        ...
