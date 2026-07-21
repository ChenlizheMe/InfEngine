"""Structural undo commands — object lifecycle and selection."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import (
    _get_active_scene, _get_current_selection_ids,
    _destroy_game_object_immediately,
    _bump_inspector_structure, _notify_gizmos_scene_changed,
    _preserve_ui_world_position, _invalidate_canvas_caches,
)


def _snapshot_object(obj) -> dict:
    from Infernux.engine.component_restore import (
        serialize_game_object_document_authoritatively,
    )

    return serialize_game_object_document_authoritatively(obj)


class CreateGameObjectCommand(UndoCommand):
    """Undo destroys the object; redo recreates from a document snapshot."""

    _selection_restore_fn: Optional[Callable[[List[int]], None]] = None

    def __init__(self, object_id: int, description: str = "Create GameObject"):
        super().__init__(description)
        self._object_id = object_id
        self._document: Optional[dict] = None
        self._parent_id: Optional[int] = None
        self._sibling_index: int = 0
        self._post_create_ids: List[int] = _get_current_selection_ids()

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(self._object_id)
            if obj:
                self._document = _snapshot_object(obj)
                parent = obj.get_parent()
                self._parent_id = parent.id if parent else None
                t = getattr(obj, "transform", None)
                self._sibling_index = t.get_sibling_index() if t else 0
                _destroy_game_object_immediately(scene, obj)
        fn = type(self)._selection_restore_fn
        if fn:
            fn([])

    def redo(self) -> None:
        if self._document is not None:
            from Infernux.engine.undo._recreate import _recreate_game_object_from_document
            _recreate_game_object_from_document(
                self._document, self._parent_id, self._sibling_index)
            _bump_inspector_structure()
            _notify_gizmos_scene_changed()
            fn = type(self)._selection_restore_fn
            if fn and self._post_create_ids:
                fn(self._post_create_ids)


class DeleteGameObjectCommand(UndoCommand):
    """Undo recreates from a document snapshot; redo re-destroys."""

    _selection_restore_fn: Optional[Callable[[List[int]], None]] = None

    def __init__(self, object_id: int, description: str = "Delete GameObject"):
        super().__init__(description)
        self._object_id = object_id
        self._document: Optional[dict] = None
        self._parent_id: Optional[int] = None
        self._sibling_index: int = 0
        self._pre_delete_selection_ids: List[int] = []

        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(object_id)
            if obj:
                self._document = _snapshot_object(obj)
                parent = obj.get_parent()
                self._parent_id = parent.id if parent else None
                t = getattr(obj, "transform", None)
                self._sibling_index = t.get_sibling_index() if t else 0
        self._pre_delete_selection_ids = _get_current_selection_ids()

    def execute(self) -> None:
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(self._object_id)
            if obj:
                _destroy_game_object_immediately(scene, obj)

    def undo(self) -> None:
        if self._document is not None:
            from Infernux.engine.undo._recreate import _recreate_game_object_from_document
            _recreate_game_object_from_document(
                self._document, self._parent_id, self._sibling_index)
            _bump_inspector_structure()
            _notify_gizmos_scene_changed()
            fn = type(self)._selection_restore_fn
            if fn and self._pre_delete_selection_ids:
                fn(self._pre_delete_selection_ids)

    def redo(self) -> None:
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(self._object_id)
            if obj:
                self._document = _snapshot_object(obj)
                _destroy_game_object_immediately(scene, obj)
        fn = type(self)._selection_restore_fn
        if fn:
            fn([])


class DeleteGameObjectsCommand(UndoCommand):
    """Delete a selection as one hierarchy transaction.

    Selected descendants are covered by their selected ancestor's document and
    therefore must not be snapshotted or recreated independently.  Restoring
    roots in ascending sibling order preserves the exact hierarchy ordering.
    """

    _selection_restore_fn: Optional[Callable[[List[int]], None]] = None

    def __init__(self, object_ids: List[int], description: str = "Delete GameObjects"):
        super().__init__(description)
        self._entries: List[dict] = []
        self._pre_delete_selection_ids = _get_current_selection_ids()

        scene = _get_active_scene()
        if not scene:
            return

        selected_ids = {int(object_id) for object_id in object_ids}
        roots = []
        for object_id in object_ids:
            obj = scene.find_by_id(int(object_id))
            if obj is None:
                continue
            parent = obj.get_parent()
            has_selected_ancestor = False
            while parent is not None:
                if int(parent.id) in selected_ids:
                    has_selected_ancestor = True
                    break
                parent = parent.get_parent()
            if not has_selected_ancestor:
                roots.append(obj)

        seen = set()
        for obj in roots:
            object_id = int(obj.id)
            if object_id in seen:
                continue
            seen.add(object_id)
            parent = obj.get_parent()
            transform = getattr(obj, "transform", None)
            self._entries.append({
                "object_id": object_id,
                "document": _snapshot_object(obj),
                "parent_id": int(parent.id) if parent else None,
                "sibling_index": int(transform.get_sibling_index()) if transform else 0,
            })

    @staticmethod
    def _entry_order(entry: dict) -> tuple[int, int]:
        parent_id = entry["parent_id"]
        return (-1 if parent_id is None else int(parent_id), int(entry["sibling_index"]))

    def execute(self) -> None:
        scene = _get_active_scene()
        if not scene:
            return
        # Destroy from the end of each sibling list so earlier indices do not
        # shift while the transaction is being applied.
        for entry in sorted(self._entries, key=self._entry_order, reverse=True):
            obj = scene.find_by_id(entry["object_id"])
            if obj is not None:
                _destroy_game_object_immediately(scene, obj)

    def undo(self) -> None:
        from Infernux.engine.undo._recreate import _recreate_game_object_from_document

        for entry in sorted(self._entries, key=self._entry_order):
            _recreate_game_object_from_document(
                entry["document"], entry["parent_id"], entry["sibling_index"])
        if self._entries:
            _bump_inspector_structure()
            _notify_gizmos_scene_changed()
        fn = type(self)._selection_restore_fn
        if fn and self._pre_delete_selection_ids:
            fn(self._pre_delete_selection_ids)

    def redo(self) -> None:
        self.execute()
        fn = type(self)._selection_restore_fn
        if fn:
            fn([])


class ReparentCommand(UndoCommand):
    """Undo/redo changing the parent of a GameObject."""

    def __init__(self, object_id: int,
                 old_parent_id: Optional[int],
                 new_parent_id: Optional[int],
                 description: str = "Reparent"):
        super().__init__(description)
        self._object_id = object_id
        self._old_parent_id = old_parent_id
        self._new_parent_id = new_parent_id

    def execute(self) -> None:
        self._apply(self._new_parent_id)

    def undo(self) -> None:
        self._apply(self._old_parent_id)

    def redo(self) -> None:
        self._apply(self._new_parent_id)

    def _apply(self, parent_id: Optional[int]) -> None:
        scene = _get_active_scene()
        if not scene:
            return
        obj = scene.find_by_id(self._object_id)
        if not obj:
            return
        new_parent = scene.find_by_id(parent_id) if parent_id is not None else None
        old_parent = obj.get_parent()
        _preserve_ui_world_position(obj, new_parent)
        obj.set_parent(new_parent)
        _invalidate_canvas_caches(old_parent)
        _invalidate_canvas_caches(new_parent)


class MoveGameObjectCommand(UndoCommand):
    """Undo/redo moves that change both parent and sibling order."""

    def __init__(self, object_id: int,
                 old_parent_id: Optional[int], new_parent_id: Optional[int],
                 old_sibling_index: int, new_sibling_index: int,
                 description: str = "Move In Hierarchy"):
        super().__init__(description)
        self._object_id = object_id
        self._old_parent_id = old_parent_id
        self._new_parent_id = new_parent_id
        self._old_sibling_index = int(old_sibling_index)
        self._new_sibling_index = int(new_sibling_index)

    def execute(self) -> None:
        self._apply(self._new_parent_id, self._new_sibling_index)

    def undo(self) -> None:
        self._apply(self._old_parent_id, self._old_sibling_index)

    def redo(self) -> None:
        self._apply(self._new_parent_id, self._new_sibling_index)

    def _apply(self, parent_id: Optional[int], sibling_index: int) -> None:
        scene = _get_active_scene()
        if not scene:
            return
        obj = scene.find_by_id(self._object_id)
        if not obj:
            return
        parent = scene.find_by_id(parent_id) if parent_id is not None else None
        current_parent = obj.get_parent()
        if current_parent is not parent:
            _preserve_ui_world_position(obj, parent)
            obj.set_parent(parent)
            _invalidate_canvas_caches(current_parent)
            _invalidate_canvas_caches(parent)
        transform = getattr(obj, "transform", None)
        if transform is not None:
            transform.set_sibling_index(max(0, int(sibling_index)))


class SelectionCommand(UndoCommand):
    """Record a selection change.  Does not mark the scene dirty."""

    marks_dirty: bool = False

    def __init__(self, old_ids: List[int], new_ids: List[int],
                 apply_fn: Callable[[List[int]], None],
                 description: str = ""):
        super().__init__(description or "Change Selection")
        self._old_ids = list(old_ids)
        self._new_ids = list(new_ids)
        self._apply_fn = apply_fn

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        self._apply_fn(self._old_ids)

    def redo(self) -> None:
        self._apply_fn(self._new_ids)


class EditorSelectionCommand(UndoCommand):
    """Record editor selection state across hierarchy and project panels."""

    marks_dirty: bool = False

    def __init__(self,
                 old_object_ids: List[int], old_file_path: str,
                 new_object_ids: List[int], new_file_path: str,
                 apply_fn: Callable[[List[int], str], None],
                 description: str = ""):
        super().__init__(description or "Change Selection")
        self._old_object_ids = list(old_object_ids)
        self._old_file_path = old_file_path or ""
        self._new_object_ids = list(new_object_ids)
        self._new_file_path = new_file_path or ""
        self._apply_fn = apply_fn

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        self._apply_fn(self._old_object_ids, self._old_file_path)

    def redo(self) -> None:
        self._apply_fn(self._new_object_ids, self._new_file_path)


class PrefabModeCommand(UndoCommand):
    """Undoable enter/exit transition for Prefab Mode."""

    marks_dirty: bool = False

    def __init__(self, prefab_path: str, enter_mode: bool):
        action = "Enter Prefab Mode" if enter_mode else "Exit Prefab Mode"
        super().__init__(action)
        self._prefab_path = prefab_path or ""
        self._enter_mode = bool(enter_mode)

    def execute(self) -> None:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if not sfm:
            return
        if self._enter_mode:
            sfm.open_prefab_mode(self._prefab_path, preserve_undo_history=True)
        else:
            sfm._do_exit_prefab_mode(preserve_undo_history=True)

    def undo(self) -> None:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if not sfm:
            return
        if self._enter_mode:
            sfm._do_exit_prefab_mode(preserve_undo_history=True)
        else:
            sfm.open_prefab_mode(self._prefab_path, preserve_undo_history=True)

    def redo(self) -> None:
        self.execute()


class PrefabUnpackCommand(UndoCommand):
    """Undoable removal of prefab linkage from one complete instance tree."""

    def __init__(self, object_id: int, description: str = "Unpack Prefab"):
        super().__init__(description)
        self._object_id = int(object_id)
        self._linkage: list[tuple[int, str, bool]] = []
        scene = _get_active_scene()
        root = scene.find_by_id(self._object_id) if scene else None
        if root is not None:
            pending = [root]
            while pending:
                obj = pending.pop()
                self._linkage.append((
                    int(obj.id),
                    getattr(obj, "prefab_guid", "") or "",
                    bool(getattr(obj, "prefab_root", False)),
                ))
                pending.extend(obj.get_children())

    def execute(self) -> None:
        self._apply(restored=False)

    def undo(self) -> None:
        self._apply(restored=True)

    def redo(self) -> None:
        self._apply(restored=False)

    def _apply(self, *, restored: bool) -> None:
        scene = _get_active_scene()
        if scene is None:
            return
        for object_id, prefab_guid, prefab_root in self._linkage:
            obj = scene.find_by_id(object_id)
            if obj is None:
                continue
            obj.prefab_guid = prefab_guid if restored else ""
            obj.prefab_root = prefab_root if restored else False
        _bump_inspector_structure()


class PrefabRevertCommand(UndoCommand):
    """Undoable in-place replacement of one prefab instance subtree."""

    def __init__(self, object_id: int, before_document: dict,
                 reverted_document: dict, asset_database=None,
                 description: str = "Revert Prefab Overrides"):
        super().__init__(description)
        self._object_id = int(object_id)
        self._before_document = before_document
        self._reverted_document = reverted_document
        self._asset_database = asset_database

    def execute(self) -> None:
        self._apply(self._reverted_document, preserve_document_ids=False)

    def undo(self) -> None:
        self._apply(self._before_document, preserve_document_ids=True)

    def redo(self) -> None:
        self._apply(self._reverted_document, preserve_document_ids=False)

    def _apply(self, document: dict, *, preserve_document_ids: bool) -> None:
        scene = _get_active_scene()
        obj = scene.find_by_id(self._object_id) if scene else None
        if obj is None:
            raise RuntimeError(f"Prefab instance root {self._object_id} is unavailable")
        from Infernux.engine.component_restore import (
            deserialize_game_object_document_transactionally,
        )
        if not deserialize_game_object_document_transactionally(
            obj,
            document,
            self._asset_database,
            preserve_document_ids=preserve_document_ids,
        ):
            raise RuntimeError("Prefab ObjectGraph transaction failed")
        _bump_inspector_structure()
