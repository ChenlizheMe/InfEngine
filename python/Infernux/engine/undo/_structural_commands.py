"""Structural undo commands — object lifecycle and selection."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

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


def _validate_recreated_object(obj, expected_id: int):
    if obj is None:
        raise RuntimeError(
            f"GameObject undo restore returned no object for stable id {expected_id}"
        )
    actual_id = int(getattr(obj, "id", 0) or 0)
    if actual_id != int(expected_id):
        raise RuntimeError(
            f"GameObject undo restore expected stable id {expected_id}, got {actual_id}"
        )
    return obj


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
            restored = _recreate_game_object_from_document(
                self._document, self._parent_id, self._sibling_index)
            _validate_recreated_object(restored, self._object_id)
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
            restored = _recreate_game_object_from_document(
                self._document, self._parent_id, self._sibling_index)
            _validate_recreated_object(restored, self._object_id)
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
        restored = []
        try:
            for entry in sorted(self._entries, key=self._entry_order):
                obj = _recreate_game_object_from_document(
                    entry["document"], entry["parent_id"], entry["sibling_index"])
                restored.append(
                    _validate_recreated_object(obj, entry["object_id"])
                )
        except Exception:
            scene = _get_active_scene()
            if scene is not None:
                for obj in reversed(restored):
                    live = scene.find_by_id(int(obj.id))
                    if live is not None:
                        _destroy_game_object_immediately(scene, live)
            raise
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


class SceneHierarchyLayoutCommand(UndoCommand):
    """Atomically restore complete child ordering for affected parents.

    Multi-object hierarchy gestures are one user action. Storing one command
    per dragged object makes sibling indices depend on replay order and can
    leave a partially moved tree when one sub-command fails. This command
    captures the complete child list for every affected parent and applies the
    layout as one fail-closed transition.
    """

    def __init__(
        self,
        before_layout: Dict[Optional[int], Tuple[int, ...]],
        after_layout: Dict[Optional[int], Tuple[int, ...]],
        description: str = "Move GameObjects",
    ) -> None:
        super().__init__(description)
        self._before_layout = self._normalize(before_layout)
        self._after_layout = self._normalize(after_layout)
        if set(self._before_layout) != set(self._after_layout):
            raise ValueError("hierarchy layout parent sets must match")
        if self._before_layout == self._after_layout:
            raise ValueError("hierarchy layout command requires a real change")
        before_ids = self._layout_ids(self._before_layout)
        after_ids = self._layout_ids(self._after_layout)
        if before_ids != after_ids:
            raise ValueError("hierarchy layout object sets must match")

    @staticmethod
    def _normalize(layout):
        if not isinstance(layout, dict) or not layout:
            raise TypeError("hierarchy layout must be a non-empty mapping")
        normalized = {}
        for parent_id, object_ids in layout.items():
            key = None if parent_id in {None, 0} else int(parent_id)
            values = tuple(int(object_id) for object_id in object_ids)
            if any(object_id <= 0 for object_id in values):
                raise ValueError("hierarchy layout requires positive object ids")
            if len(values) != len(set(values)):
                raise ValueError("hierarchy layout contains duplicate siblings")
            normalized[key] = values
        return normalized

    @staticmethod
    def _layout_ids(layout) -> set[int]:
        result: set[int] = set()
        for object_ids in layout.values():
            overlap = result.intersection(object_ids)
            if overlap:
                raise ValueError(
                    f"hierarchy layout assigns objects to multiple parents: {sorted(overlap)}"
                )
            result.update(object_ids)
        return result

    @staticmethod
    def _children(scene, parent_id: Optional[int]):
        if parent_id is None:
            return list(scene.get_root_objects())
        parent = scene.find_by_id(parent_id)
        if parent is None:
            raise RuntimeError(f"hierarchy parent is unavailable: {parent_id}")
        return list(parent.get_children())

    @classmethod
    def _preflight(cls, scene, layout):
        objects = {}
        expected_ids = cls._layout_ids(layout)
        for object_id in expected_ids:
            obj = scene.find_by_id(object_id)
            if obj is None:
                raise RuntimeError(f"hierarchy object is unavailable: {object_id}")
            objects[object_id] = obj
        actual_ids = {
            int(obj.id)
            for parent_id in layout
            for obj in cls._children(scene, parent_id)
        }
        if actual_ids != expected_ids:
            raise RuntimeError(
                "hierarchy child sets changed outside this transaction"
            )
        return objects

    @classmethod
    def _apply_layout(cls, scene, layout) -> None:
        objects = cls._preflight(scene, layout)
        desired_parent = {
            object_id: parent_id
            for parent_id, object_ids in layout.items()
            for object_id in object_ids
        }
        for object_id, parent_id in desired_parent.items():
            obj = objects[object_id]
            current_parent = obj.get_parent()
            current_parent_id = current_parent.id if current_parent is not None else None
            if current_parent_id == parent_id:
                continue
            parent = scene.find_by_id(parent_id) if parent_id is not None else None
            _preserve_ui_world_position(obj, parent)
            obj.set_parent(parent)
            _invalidate_canvas_caches(current_parent)
            _invalidate_canvas_caches(parent)

        for parent_id, object_ids in layout.items():
            current = tuple(int(obj.id) for obj in cls._children(scene, parent_id))
            if set(current) != set(object_ids):
                raise RuntimeError(
                    f"hierarchy parent {parent_id or 0} rejected the requested child set"
                )
            for index, object_id in enumerate(object_ids):
                transform = getattr(objects[object_id], "transform", None)
                if transform is None:
                    raise RuntimeError(
                        f"hierarchy object has no Transform: {object_id}"
                    )
                transform.set_sibling_index(index)
            parent = scene.find_by_id(parent_id) if parent_id is not None else None
            _invalidate_canvas_caches(parent)

    @classmethod
    def _transition(cls, target, rollback) -> None:
        scene = _get_active_scene()
        if scene is None:
            raise RuntimeError("hierarchy layout requires an active scene")
        try:
            cls._apply_layout(scene, target)
        except Exception as original:
            try:
                cls._apply_layout(scene, rollback)
            except Exception as rollback_error:
                raise RuntimeError(
                    "hierarchy layout failed and rollback could not restore the tree"
                ) from rollback_error
            raise original

    def execute(self) -> None:
        self._transition(self._after_layout, self._before_layout)

    def undo(self) -> None:
        self._transition(self._before_layout, self._after_layout)

    def redo(self) -> None:
        self._transition(self._after_layout, self._before_layout)


class GlobalSelectionCommand(UndoCommand):
    """Record a typed selection transition without replay side effects."""

    marks_dirty: bool = False

    def __init__(self, old_snapshot, new_snapshot, description: str = ""):
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

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass


class GlobalFocusCommand(UndoCommand):
    """Record a focus transition without replay side effects."""

    marks_dirty: bool = False
    # A visible editor-window transition is one chronological user action.
    # It must never be absorbed into the data edit that happens immediately
    # before or after it.
    separates_history: bool = True

    def __init__(self, old_snapshot, new_snapshot, description: str = ""):
        from Infernux.engine.interaction import FocusSnapshot

        if not isinstance(old_snapshot, FocusSnapshot):
            raise TypeError("old_snapshot must be a FocusSnapshot")
        if not isinstance(new_snapshot, FocusSnapshot):
            raise TypeError("new_snapshot must be a FocusSnapshot")
        UndoCommand.__init__(self, description or "Change Editor Focus")
        self._old_snapshot = old_snapshot
        self._new_snapshot = new_snapshot

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass


class GlobalContextCommand(UndoCommand):
    """Represent a context-only history step.

    The journal entry owns context restoration. Keeping this command inert
    prevents focus and selection from being applied twice during replay.
    """

    marks_dirty: bool = False

    def __init__(self, before_context, after_context, description: str = ""):
        from Infernux.engine.interaction import EditorContextSnapshot

        if not isinstance(before_context, EditorContextSnapshot):
            raise TypeError("before_context must be an EditorContextSnapshot")
        if not isinstance(after_context, EditorContextSnapshot):
            raise TypeError("after_context must be an EditorContextSnapshot")
        UndoCommand.__init__(self, description or "Change Editor Context")
        self._before_context = before_context
        self._after_context = after_context

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass


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
            raise RuntimeError("Prefab Mode requires an active SceneFileManager")
        if self._enter_mode:
            succeeded = sfm.open_prefab_mode(
                self._prefab_path,
                preserve_undo_history=True,
            )
        else:
            succeeded = sfm._do_exit_prefab_mode(preserve_undo_history=True)
        if not succeeded:
            raise RuntimeError(f"{self.description} was rejected")

    def undo(self) -> None:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if not sfm:
            raise RuntimeError("Prefab Mode requires an active SceneFileManager")
        if self._enter_mode:
            succeeded = sfm._do_exit_prefab_mode(preserve_undo_history=True)
        else:
            succeeded = sfm.open_prefab_mode(
                self._prefab_path,
                preserve_undo_history=True,
            )
        if not succeeded:
            raise RuntimeError(f"Undo {self.description} was rejected")

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


class PrefabApplyOverridesCommand(UndoCommand):
    """Atomically apply one Prefab asset edit and all live instance projections."""

    def __init__(self, capture_state, apply_overrides, restore_state,
                 description: str = "Apply Prefab Overrides"):
        super().__init__(description)
        if not callable(capture_state) or not callable(apply_overrides):
            raise TypeError("Prefab Apply command requires capture and apply callbacks")
        if not callable(restore_state):
            raise TypeError("Prefab Apply command requires a restore callback")
        self._capture_state = capture_state
        self._apply_overrides = apply_overrides
        self._restore_state = restore_state
        self._before_state = None
        self._after_state = None

    def execute(self) -> None:
        if self._before_state is not None:
            raise RuntimeError("Prefab Apply command has already executed; use redo")
        self._before_state = self._capture_state()
        try:
            if not self._apply_overrides():
                raise RuntimeError("Prefab Apply transaction was rejected")
            self._after_state = self._capture_state()
        except Exception:
            self._restore_state(self._before_state)
            self._before_state = None
            self._after_state = None
            raise

    def undo(self) -> None:
        if self._before_state is None:
            raise RuntimeError("Prefab Apply command has not executed")
        self._restore_state(self._before_state)

    def redo(self) -> None:
        if self._after_state is None:
            raise RuntimeError("Prefab Apply command has no committed result")
        self._restore_state(self._after_state)
