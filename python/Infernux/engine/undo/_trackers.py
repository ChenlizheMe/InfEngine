"""Inspector and Hierarchy undo trackers."""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional

from Infernux.debug import Debug
from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import (
    _get_active_scene, _preserve_ui_world_position, _invalidate_canvas_caches,
)


class InspectorSnapshotCommand(UndoCommand):
    """Snapshot-based undo for Inspector edits — one generic command for ALL
    component types."""

    MERGE_WINDOW: float = 0.3

    def __init__(self, target_key: str, old_snapshot: Any,
                 new_snapshot: Any,
                 restore_fn: Callable[[Any], None],
                 description: str = "",
                 marks_dirty: bool = True):
        super().__init__(description or "Inspector Edit")
        self.marks_dirty = marks_dirty
        self._target_key = target_key
        self._old_snapshot = copy.deepcopy(old_snapshot)
        self._new_snapshot = copy.deepcopy(new_snapshot)
        self._restore_fn = restore_fn

    def execute(self) -> None:
        self._restore_fn(self._new_snapshot)

    def undo(self) -> None:
        self._restore_fn(self._old_snapshot)

    def redo(self) -> None:
        self._restore_fn(self._new_snapshot)

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, InspectorSnapshotCommand):
            return False
        return (self._target_key == other._target_key
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other: InspectorSnapshotCommand) -> None:
        self._new_snapshot = copy.deepcopy(other._new_snapshot)
        self.timestamp = other.timestamp


class _TrackedEntry:
    __slots__ = ('pre_snapshot', 'snapshot_fn', 'restore_fn', 'description',
                 'seen_generation', 'marks_dirty')

    def __init__(self, pre_snapshot: Any,
                 snapshot_fn: Callable[[], Any],
                 restore_fn: Callable[[Any], None],
                 description: str,
                 marks_dirty: bool = True):
        self.pre_snapshot = copy.deepcopy(pre_snapshot)
        self.snapshot_fn = snapshot_fn
        self.restore_fn = restore_fn
        self.description = description
        self.seen_generation = 0
        self.marks_dirty = marks_dirty


class InspectorUndoTracker:
    """Automatic snapshot-based undo for the Inspector panel."""

    def __init__(self):
        self._entries: dict[str, _TrackedEntry] = {}
        self._was_active: bool = False
        self._is_active: bool = False
        self._frame_generation: int = 0
        self._all_active_current_frame: bool = False

    def begin_frame(self) -> None:
        self._frame_generation += 1
        self._all_active_current_frame = False

    def invalidate_all(self) -> None:
        self._entries.clear()
        self._all_active_current_frame = False
        self._was_active = False
        self._is_active = False

    def mark_all_active(self) -> None:
        self._all_active_current_frame = True

    def track(self, key: str, snapshot_fn: Callable[[], Any],
              restore_fn: Callable[[Any], None],
              description: str = "",
              marks_dirty: bool = True) -> None:
        existing = self._entries.get(key)
        if existing is not None:
            existing.seen_generation = self._frame_generation
            existing.snapshot_fn = snapshot_fn
            existing.restore_fn = restore_fn
            return
        try:
            pre = snapshot_fn()
        except Exception as exc:
            Debug.log_suppressed("undo._trackers.snapshot_fn(pre)", exc)
            return
        entry = _TrackedEntry(pre, snapshot_fn, restore_fn, description, marks_dirty)
        entry.seen_generation = self._frame_generation
        self._entries[key] = entry

    def end_frame(self, any_item_active: bool | None = None) -> None:
        do_compare = True
        if any_item_active is not None:
            self._was_active, self._is_active = self._is_active, any_item_active
            do_compare = any_item_active or self._was_active

        from Infernux.engine.undo._manager import UndoManager
        mgr = UndoManager.instance()
        if not mgr or not mgr.enabled:
            self._entries.clear()
            return

        if not self._all_active_current_frame:
            stale = [k for k, e in self._entries.items()
                     if e.seen_generation != self._frame_generation]
            for k in stale:
                del self._entries[k]

        if not do_compare:
            return

        for key, entry in self._entries.items():
            try:
                post = entry.snapshot_fn()
            except Exception as exc:
                Debug.log_suppressed("undo._trackers.snapshot_fn(post)", exc)
                continue
            if post != entry.pre_snapshot:
                cmd = InspectorSnapshotCommand(
                    key, entry.pre_snapshot, post,
                    entry.restore_fn, entry.description,
                    marks_dirty=entry.marks_dirty)
                mgr.record(cmd)
                entry.pre_snapshot = copy.deepcopy(post)


class HierarchyUndoTracker:
    """Unified undo interface for Hierarchy panel mutations."""

    @staticmethod
    def _mgr():
        from Infernux.engine.undo._manager import UndoManager
        return UndoManager.instance()

    def record_create(
        self,
        object_id: int,
        description: str = "Create GameObject",
        *,
        before_selection=None,
        after_selection=None,
    ) -> None:
        from Infernux.engine.undo._structural_commands import CreateGameObjectCommand
        mgr = self._mgr()
        if mgr:
            mgr.record(CreateGameObjectCommand(
                object_id,
                description,
                before_selection=before_selection,
                after_selection=after_selection,
            ))
            return
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm:
            sfm.mark_dirty()

    def record_delete(self, object_id: int,
                      description: str = "Delete GameObject") -> None:
        from Infernux.engine.undo._structural_commands import DeleteGameObjectCommand
        mgr = self._mgr()
        if mgr:
            mgr.execute(DeleteGameObjectCommand(object_id, description))
            return
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(object_id)
            if obj:
                scene.destroy_game_object(obj)
                from Infernux.engine.scene_manager import SceneFileManager
                sfm = SceneFileManager.instance()
                if sfm:
                    sfm.mark_dirty()

    def record_rename(self, object_id: int, old_name: str, new_name: str,
                      description: str = "Rename GameObject") -> None:
        old_name = str(old_name)
        new_name = str(new_name)
        if not new_name or old_name == new_name:
            return
        scene = _get_active_scene()
        obj = scene.find_by_id(object_id) if scene else None
        if obj is None:
            return
        mgr = self._mgr()
        if mgr and mgr.enabled:
            from Infernux.engine.undo._property_commands import SetPropertyCommand
            mgr.record(SetPropertyCommand(obj, "name", old_name, new_name, description))
            return
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm:
            sfm.mark_dirty()

    def record_reparent(self, object_id: int,
                        old_parent_id: Optional[int],
                        new_parent_id: Optional[int],
                        description: str = "Reparent") -> None:
        from Infernux.engine.undo._structural_commands import ReparentCommand
        mgr = self._mgr()
        if mgr:
            mgr.execute(ReparentCommand(
                object_id, old_parent_id, new_parent_id, description))
            return
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(object_id)
            if obj:
                parent = (scene.find_by_id(new_parent_id)
                          if new_parent_id is not None else None)
                old_parent = obj.get_parent()
                _preserve_ui_world_position(obj, parent)
                obj.set_parent(parent)
                _invalidate_canvas_caches(old_parent)
                _invalidate_canvas_caches(parent)
                from Infernux.engine.scene_manager import SceneFileManager
                sfm = SceneFileManager.instance()
                if sfm:
                    sfm.mark_dirty()

    def record_move(self, object_id: int,
                    old_parent_id: Optional[int],
                    new_parent_id: Optional[int],
                    old_sibling_index: int,
                    new_sibling_index: int,
                    description: str = "Move In Hierarchy") -> None:
        from Infernux.engine.undo._structural_commands import MoveGameObjectCommand
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(object_id)
            if obj:
                current_parent = obj.get_parent()
                current_parent_id = current_parent.id if current_parent else None
                transform = getattr(obj, 'transform', None)
                current_sibling_index = transform.get_sibling_index() if transform is not None else 0
                if current_parent_id == new_parent_id and current_sibling_index == int(new_sibling_index):
                    return
        mgr = self._mgr()
        if mgr:
            mgr.execute(MoveGameObjectCommand(
                object_id, old_parent_id, new_parent_id,
                old_sibling_index, new_sibling_index, description))
            return
        if scene:
            obj = scene.find_by_id(object_id)
            if obj:
                parent = (scene.find_by_id(new_parent_id)
                          if new_parent_id is not None else None)
                old_parent = obj.get_parent()
                _preserve_ui_world_position(obj, parent)
                obj.set_parent(parent)
                _invalidate_canvas_caches(old_parent)
                _invalidate_canvas_caches(parent)
                transform = getattr(obj, 'transform', None)
                if transform is not None:
                    transform.set_sibling_index(max(0, int(new_sibling_index)))
                from Infernux.engine.scene_manager import SceneFileManager
                sfm = SceneFileManager.instance()
                if sfm:
                    sfm.mark_dirty()
