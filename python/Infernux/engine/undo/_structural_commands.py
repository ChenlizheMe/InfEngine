"""Structural undo commands — object lifecycle and selection."""

from __future__ import annotations

from typing import List, Optional

from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import (
    _get_active_scene,
    _destroy_game_object_immediately,
    _bump_inspector_structure, _notify_gizmos_scene_changed,
    _preserve_ui_world_position, _invalidate_canvas_caches,
)


def _object_tree_ids(root) -> set[int]:
    result: set[int] = set()
    pending = [root] if root is not None else []
    while pending:
        obj = pending.pop()
        object_id = int(getattr(obj, "id", 0) or 0)
        if object_id <= 0 or object_id in result:
            continue
        result.add(object_id)
        try:
            pending.extend(obj.get_children())
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    return result


def _prune_destroyed_selection(object_ids: set[int], reason: str) -> None:
    if not object_ids:
        return
    from Infernux.engine.interaction import (
        SelectionDomain,
        SelectionService,
        SelectionSnapshot,
    )

    selection = SelectionService.instance()
    before = selection.snapshot
    kept = []
    for target in before.targets:
        if (
            target.domain is SelectionDomain.SCENE_OBJECT
            and target.scene_object_id() in object_ids
        ):
            continue
        if (
            target.domain is SelectionDomain.COMPONENT
            and target.component_ids()[0] in object_ids
        ):
            continue
        kept.append(target)
    if len(kept) == len(before.targets):
        return
    snapshot = SelectionSnapshot.create(
        kept,
        owner_id=before.owner_id if kept else "",
        primary=before.primary if before.primary in kept else None,
        anchor=before.anchor if before.anchor in kept else None,
    )
    selection.apply_snapshot(
        snapshot,
        reason=reason,
        record_history=False,
    )


def _snapshot_object(obj) -> dict:
    from Infernux.engine.component_restore import (
        serialize_game_object_document_authoritatively,
    )

    return serialize_game_object_document_authoritatively(obj)


class CreateGameObjectCommand(UndoCommand):
    """Undo destroys the object; redo recreates from a document snapshot."""

    def __init__(
        self,
        object_id: int,
        description: str = "Create GameObject",
        *,
        before_selection=None,
        after_selection=None,
    ):
        super().__init__(description)
        self._object_id = object_id
        self._document: Optional[dict] = None
        self._parent_id: Optional[int] = None
        self._sibling_index: int = 0
        if before_selection is not None and after_selection is not None:
            self.before_selection_snapshot = before_selection
            self.after_selection_snapshot = after_selection

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(self._object_id)
            if obj:
                destroyed_ids = _object_tree_ids(obj)
                self._document = _snapshot_object(obj)
                parent = obj.get_parent()
                self._parent_id = parent.id if parent else None
                t = getattr(obj, "transform", None)
                self._sibling_index = t.get_sibling_index() if t else 0
                _destroy_game_object_immediately(scene, obj)
                _prune_destroyed_selection(
                    destroyed_ids,
                    "undo_create_game_object",
                )

    def redo(self) -> None:
        if self._document is not None:
            from Infernux.engine.undo._recreate import _recreate_game_object_from_document
            _recreate_game_object_from_document(
                self._document, self._parent_id, self._sibling_index)
            _bump_inspector_structure()
            _notify_gizmos_scene_changed()


class DeleteGameObjectCommand(UndoCommand):
    """Undo recreates from a document snapshot; redo re-destroys."""

    def __init__(self, object_id: int, description: str = "Delete GameObject"):
        super().__init__(description)
        self._object_id = object_id
        self._document: Optional[dict] = None
        self._parent_id: Optional[int] = None
        self._sibling_index: int = 0

        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(object_id)
            if obj:
                self._document = _snapshot_object(obj)
                parent = obj.get_parent()
                self._parent_id = parent.id if parent else None
                t = getattr(obj, "transform", None)
                self._sibling_index = t.get_sibling_index() if t else 0

    def execute(self) -> None:
        scene = _get_active_scene()
        if scene:
            obj = scene.find_by_id(self._object_id)
            if obj:
                destroyed_ids = _object_tree_ids(obj)
                _destroy_game_object_immediately(scene, obj)
                _prune_destroyed_selection(destroyed_ids, "delete_game_object")

    def undo(self) -> None:
        if self._document is not None:
            from Infernux.engine.undo._recreate import _recreate_game_object_from_document
            _recreate_game_object_from_document(
                self._document, self._parent_id, self._sibling_index)
            _bump_inspector_structure()
            _notify_gizmos_scene_changed()

    def redo(self) -> None:
        self.execute()


class DeleteGameObjectsCommand(UndoCommand):
    """Delete a selection as one hierarchy transaction.

    Selected descendants are covered by their selected ancestor's document and
    therefore must not be snapshotted or recreated independently.  Restoring
    roots in ascending sibling order preserves the exact hierarchy ordering.
    """

    def __init__(self, object_ids: List[int], description: str = "Delete GameObjects"):
        super().__init__(description)
        self._entries: List[dict] = []

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
        destroyed_ids: set[int] = set()
        for entry in sorted(self._entries, key=self._entry_order, reverse=True):
            obj = scene.find_by_id(entry["object_id"])
            if obj is not None:
                destroyed_ids.update(_object_tree_ids(obj))
                _destroy_game_object_immediately(scene, obj)
        _prune_destroyed_selection(destroyed_ids, "delete_game_objects")

    def undo(self) -> None:
        from Infernux.engine.undo._recreate import _recreate_game_object_from_document

        for entry in sorted(self._entries, key=self._entry_order):
            _recreate_game_object_from_document(
                entry["document"], entry["parent_id"], entry["sibling_index"])
        if self._entries:
            _bump_inspector_structure()
            _notify_gizmos_scene_changed()

    def redo(self) -> None:
        self.execute()


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


class GlobalSelectionCommand(UndoCommand):
    """Replay a typed global selection without dirtying its document."""

    marks_dirty: bool = False

    def __init__(self, old_snapshot, new_snapshot, apply_fn, description: str = ""):
        from Infernux.engine.interaction import SelectionSnapshot

        if not isinstance(old_snapshot, SelectionSnapshot):
            raise TypeError("old_snapshot must be a SelectionSnapshot")
        if not isinstance(new_snapshot, SelectionSnapshot):
            raise TypeError("new_snapshot must be a SelectionSnapshot")
        UndoCommand.__init__(self, description or "Change Selection")
        self._old_snapshot = old_snapshot
        self._new_snapshot = new_snapshot
        self.before_selection_snapshot = old_snapshot
        self.after_selection_snapshot = new_snapshot
        self._apply_fn = apply_fn

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        self._apply_fn(self._old_snapshot)

    def redo(self) -> None:
        self._apply_fn(self._new_snapshot)


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
