"""Main wiring function for the C++ HierarchyPanel."""
from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

from Infernux.debug import Debug
from Infernux.engine.bootstrap_hierarchy._helpers import (
    _get_children_safe,
    _get_py_components_safe,
)

if TYPE_CHECKING:
    from Infernux.engine.bootstrap import EditorBootstrap


class _Ctx:
    """Thin namespace shared across hierarchy wiring helpers."""


def _get_hierarchy_scene():
    from Infernux.lib import SceneManager as _SM
    return _SM.instance().get_active_scene()


# ═══════ Canvas / UI-mode queries ══════════════════════════════

def _wire_canvas_queries(ctx):
    """Wire canvas and UI-component detection callbacks."""
    hp = ctx.hp

    query_cache = {
        "scene_ref": None,
        "scene_structure_version": -1,
        "canvas_list_token": 0,
        "canvas_object_ids": set(),
        "canvas_tree_ids": set(),
        "canvas_ancestor_ids": set(),
        "canvas_root_ids": set(),
    }

    def _clear_query_cache():
        query_cache["scene_ref"] = None
        query_cache["scene_structure_version"] = -1
        query_cache["canvas_list_token"] = 0
        query_cache["canvas_object_ids"] = set()
        query_cache["canvas_tree_ids"] = set()
        query_cache["canvas_ancestor_ids"] = set()
        query_cache["canvas_root_ids"] = set()

    def _ensure_query_cache(scene):
        if scene is None:
            _clear_query_cache()
            return query_cache

        from Infernux.ui.ui_canvas_utils import collect_canvases_with_go

        canvases_with_go = collect_canvases_with_go(scene)
        canvas_list_token = id(canvases_with_go)
        scene_structure_version = int(getattr(scene, "structure_version", -1))

        if (
            query_cache["scene_ref"] is scene
            and query_cache["scene_structure_version"] == scene_structure_version
            and query_cache["canvas_list_token"] == canvas_list_token
        ):
            return query_cache

        canvas_object_ids = set()
        canvas_tree_ids = set()
        canvas_ancestor_ids = set()
        canvas_root_ids = set()

        for canvas_go, _canvas in canvases_with_go:
            if canvas_go is None:
                continue

            canvas_go_id = int(getattr(canvas_go, "id", 0) or 0)
            if canvas_go_id:
                canvas_object_ids.add(canvas_go_id)

            cur = canvas_go
            top_root_id = 0
            while cur is not None:
                cur_id = int(getattr(cur, "id", 0) or 0)
                if cur_id:
                    canvas_ancestor_ids.add(cur_id)
                    top_root_id = cur_id
                try:
                    cur = cur.get_parent()
                except Exception as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    cur = None

            if top_root_id:
                canvas_root_ids.add(top_root_id)

            pending = [canvas_go]
            while pending:
                current = pending.pop()
                current_id = int(getattr(current, "id", 0) or 0)
                if current_id in canvas_tree_ids:
                    continue
                if current_id:
                    canvas_tree_ids.add(current_id)
                pending.extend(_get_children_safe(current))

        query_cache["scene_ref"] = scene
        query_cache["scene_structure_version"] = scene_structure_version
        query_cache["canvas_list_token"] = canvas_list_token
        query_cache["canvas_object_ids"] = canvas_object_ids
        query_cache["canvas_tree_ids"] = canvas_tree_ids
        query_cache["canvas_ancestor_ids"] = canvas_ancestor_ids
        query_cache["canvas_root_ids"] = canvas_root_ids
        return query_cache

    def _get_canvas_root_ids():
        scene = _get_hierarchy_scene()
        if not scene:
            return []
        cache = _ensure_query_cache(scene)
        return list(cache["canvas_root_ids"])

    def _go_has_canvas(oid):
        scene = _get_hierarchy_scene()
        if not scene:
            return False
        cache = _ensure_query_cache(scene)
        return int(oid) in cache["canvas_object_ids"]

    def _go_has_ui_screen_component(oid):
        from Infernux.ui.inx_ui_screen_component import InxUIScreenComponent
        scene = _get_hierarchy_scene()
        if not scene:
            return False
        go = scene.find_by_id(oid)
        if not go:
            return False
        for comp in _get_py_components_safe(go):
            if isinstance(comp, InxUIScreenComponent):
                return True
        return False

    def _parent_has_canvas_ancestor(oid):
        scene = _get_hierarchy_scene()
        if not scene:
            return False
        cache = _ensure_query_cache(scene)
        return int(oid) in cache["canvas_tree_ids"]

    def _has_canvas_descendant(oid):
        scene = _get_hierarchy_scene()
        if not scene:
            return False
        cache = _ensure_query_cache(scene)
        return int(oid) in cache["canvas_ancestor_ids"]

    hp.go_has_canvas = _go_has_canvas
    hp.go_has_ui_screen_component = _go_has_ui_screen_component
    hp.parent_has_canvas_ancestor = _parent_has_canvas_ancestor
    hp.has_canvas_descendant = _has_canvas_descendant
    hp.get_canvas_root_ids = _get_canvas_root_ids


# ═══════ External drop & delete ════════════════════════════════

def _wire_drop_and_delete(ctx):
    """Wire prefab/model drop and delete callbacks."""
    hp = ctx.hp
    bs = ctx.bs
    sel = ctx.sel
    undo = ctx.undo

    def _instantiate_prefab(ref, parent_id, is_guid):
        from Infernux.lib import SceneManager, AssetRegistry
        from Infernux.engine.prefab_manager import instantiate_prefab, read_prefab_source_canvas
        from Infernux.engine.undo import (
            CompoundCommand,
            CreateGameObjectCommand,
            UndoManager,
        )
        from Infernux.engine.interaction import SelectionService
        from Infernux.ui import UICanvas as _UICanvasCls
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        scene = SceneManager.instance().get_active_scene()
        if not scene:
            return
        selection = SelectionService.instance()
        before_selection = selection.snapshot
        adb = None
        registry = AssetRegistry.instance()
        if registry:
            adb = registry.get_asset_database()
        parent = scene.find_by_id(parent_id) if parent_id else None
        created_canvas = None

        def _rollback_implicit_canvas() -> None:
            nonlocal created_canvas
            if created_canvas is None:
                return
            try:
                scene.destroy_game_object(created_canvas)
                scene.process_pending_destroys()
                invalidate_canvas_cache()
            except Exception as cleanup_exc:
                Debug.log_suppressed(
                    "hierarchy.prefab_drop.canvas_rollback",
                    cleanup_exc,
                )
            finally:
                created_canvas = None

        try:
            if parent is None:
                canvas_name = read_prefab_source_canvas(
                    file_path=ref if not is_guid else None,
                    guid=ref if is_guid else None,
                    asset_database=adb,
                )
                if canvas_name:
                    for root_obj in scene.get_root_objects():
                        if root_obj.name != canvas_name:
                            continue
                        for comp in _get_py_components_safe(root_obj):
                            if isinstance(comp, _UICanvasCls):
                                parent = root_obj
                                break
                        if parent is not None:
                            break
                    if parent is None:
                        created_canvas = scene.create_game_object(canvas_name)
                        if created_canvas is None:
                            raise RuntimeError(
                                f"Failed to create Canvas '{canvas_name}' for prefab"
                            )
                        if created_canvas.add_py_component(_UICanvasCls()) is None:
                            raise RuntimeError(
                                f"Failed to attach UICanvas to '{canvas_name}'"
                            )
                        invalidate_canvas_cache()
                        parent = created_canvas

            if is_guid:
                new_obj = instantiate_prefab(guid=ref, scene=scene, parent=parent, asset_database=adb)
            else:
                new_obj = instantiate_prefab(file_path=ref, scene=scene, parent=parent, asset_database=adb)
        except Exception as exc:
            _rollback_implicit_canvas()
            Debug.log_error(f"Prefab instantiation failed: {exc}")
            return
        if new_obj is None:
            _rollback_implicit_canvas()
            Debug.log_error("Prefab instantiation failed: no GameObject was created")
            return

        sel.select(new_obj.id, record_history=False)
        after_selection = selection.snapshot
        commands = []
        if created_canvas is not None:
            commands.append(
                CreateGameObjectCommand(
                    created_canvas.id,
                    "Instantiate Prefab",
                )
            )
        commands.append(
            CreateGameObjectCommand(
                new_obj.id,
                "Instantiate Prefab",
                before_selection=before_selection,
                after_selection=after_selection,
            )
        )
        command = (
            commands[0]
            if len(commands) == 1
            else CompoundCommand(commands, "Instantiate Prefab")
        )
        manager = UndoManager.instance()
        if manager is not None and manager.enabled:
            manager.record(command)
        else:
            command.dispose()
            scene_file_manager = getattr(bs, "scene_file_manager", None)
            if scene_file_manager is not None:
                scene_file_manager.mark_dirty()

    def _create_model_object(ref, parent_id, is_guid):
        from Infernux.lib import SceneManager, AssetRegistry
        scene = SceneManager.instance().get_active_scene()
        if not scene:
            return
        guid = ref if is_guid else ""
        if not guid:
            registry = AssetRegistry.instance()
            adb = registry.get_asset_database() if registry else None
            if not adb:
                return
            guid = adb.get_guid_from_path(ref)
        if not guid:
            return
        new_obj = scene.create_from_model(guid)
        if new_obj:
            from Infernux.lib import SceneManager as _SM2
            _finalize_drop(new_obj, parent_id, "Create Model",
                           sel, undo, hp, _SM2)

    hp.instantiate_prefab = _instantiate_prefab
    hp.create_model_object = _create_model_object

    def _delete_selected_objects_impl(ids):
        from Infernux.lib import SceneManager
        from Infernux.engine.undo import DeleteGameObjectsCommand, UndoManager
        scene = SceneManager.instance().get_active_scene()
        if not scene:
            return
        if not ids:
            return
        mgr = UndoManager.instance()
        if mgr:
            mgr.execute(DeleteGameObjectsCommand(ids))
        else:
            for oid in ids:
                obj = scene.find_by_id(oid)
                if obj:
                    scene.destroy_game_object(obj)
            sfm2 = bs.scene_file_manager
            if sfm2:
                sfm2.mark_dirty()
            sel.clear(record_history=False)

    def _delete_selected_objects():
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        ids = list(sel.get_ids())
        if scene is None or not ids:
            return
        valid_ids = []
        for object_id in ids:
            obj = scene.find_by_id(object_id)
            if obj is not None:
                valid_ids.append(object_id)
        if valid_ids:
            _delete_selected_objects_impl(valid_ids)

    hp.delete_selected_objects = _delete_selected_objects
    # Keep the action on the bootstrap object for non-panel callers. Scene and
    # Hierarchy keyboard/menu surfaces route through EditorCommandRegistry.
    bs._delete_selected_objects = _delete_selected_objects


def _finalize_drop(new_obj, parent_id, description, sel, undo, hp, SceneManager):
    """Parent, select, and record undo for a newly created object."""
    if parent_id and parent_id != 0:
        scene = SceneManager.instance().get_active_scene()
        if scene:
            parent = scene.find_by_id(parent_id)
            if parent:
                new_obj.set_parent(parent)
    from Infernux.engine.interaction import SelectionService

    selection = SelectionService.instance()
    before_selection = selection.snapshot
    sel.select(new_obj.id, record_history=False)
    undo.record_create(
        new_obj.id,
        description,
        before_selection=before_selection,
        after_selection=selection.snapshot,
    )


# ═══════ Main entry point ══════════════════════════════════════

def wire_hierarchy_callbacks(bs: EditorBootstrap) -> None:
    """Wire C++ HierarchyPanel callbacks to Python managers."""
    hp = bs.hierarchy
    from Infernux.engine.ui.selection_manager import SelectionManager
    from Infernux.engine.i18n import t as _t
    from Infernux.engine.play_mode import PlayModeManager
    from Infernux.engine.ui import EditorEventBus

    sel = SelectionManager.instance()

    ctx = _Ctx()
    ctx.hp = hp
    ctx.bs = bs
    ctx.sel = sel
    ctx._t = _t
    ctx.EditorEventBus = EditorEventBus

    # -- Selection integration --
    hp.is_selected = lambda oid: sel.is_selected(oid)
    hp.select_id = lambda oid: sel.select(oid)
    hp.toggle_id = lambda oid: sel.toggle(oid)
    hp.range_select_id = lambda oid: sel.range_select(oid)
    hp.clear_selection = lambda: sel.clear()
    hp.get_primary = lambda: sel.get_primary()
    hp.get_selected_ids = lambda: sel.get_ids()
    hp.selection_count = lambda: sel.count()
    hp.is_selection_empty = lambda: sel.is_empty()
    hp.set_ordered_ids = lambda ids: sel.set_ordered_ids(ids)

    hp_ref = weakref.ref(hp)

    def _push_selection_snapshot():
        target = hp_ref()
        if target is None:
            sel.remove_listener(_push_selection_snapshot)
            return
        target.set_selection_snapshot(sel.get_ids(), sel.get_primary())

    sel.add_listener(_push_selection_snapshot)
    _push_selection_snapshot()

    # -- Panel focus sync --
    def _on_hierarchy_focus_changed(focused: bool):
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focused:
            focus.activate_panel("hierarchy", view_id="hierarchy")
        else:
            focus.deactivate_panel("hierarchy")

    hp.on_hierarchy_panel_focused = _on_hierarchy_focus_changed

    # -- Unified command routing --
    from Infernux.engine.interaction import CommandSource

    command_registry = bs.interaction_core.commands

    def _command_payload(argument):
        target_id = str(argument or "").strip()
        return {"target_id": target_id} if target_id else {}

    hp.execute_command = lambda command_id, source, argument: (
        command_registry.execute(
            command_id,
            source=CommandSource(source),
            payload=_command_payload(argument),
        ).accepted
    )
    hp.can_execute_command = lambda command_id, argument: (
        command_registry.can_execute(
            command_id,
            command_registry.context(
                CommandSource.CONTEXT_MENU,
                _command_payload(argument),
            ),
        )
    )

    # -- Translation & warning --
    hp.translate = _t
    hp.show_warning = lambda msg: Debug.log_warning(msg)

    # -- Undo --
    from Infernux.engine.undo import HierarchyUndoTracker
    undo = HierarchyUndoTracker()
    ctx.undo = undo
    hp.undo_record_create = lambda oid, desc: undo.record_create(oid, desc)
    hp.undo_record_delete = lambda oid, desc: undo.record_delete(oid, desc)
    hp.undo_record_rename = lambda oid, old, new: undo.record_rename(oid, old, new)
    hp.undo_record_move = lambda oid, opid, npid, oidx, nidx: undo.record_move(oid, opid, npid, oidx, nidx)

    # -- Scene info --
    def _get_scene_display_name():
        sfm = bs.scene_file_manager
        return sfm.get_display_name() if sfm else ""

    def _is_prefab_mode():
        sfm = bs.scene_file_manager
        return bool(sfm and sfm.is_prefab_mode)

    def _get_prefab_display_name():
        sfm = bs.scene_file_manager
        if sfm:
            name = sfm.get_display_name()
            return _t("hierarchy.prefab_mode_header").format(name=name)
        return "Prefab"

    hp.get_scene_display_name = _get_scene_display_name
    hp.is_prefab_mode = _is_prefab_mode
    hp.get_prefab_display_name = _get_prefab_display_name

    # -- Runtime hidden IDs --
    def _get_runtime_hidden_ids():
        try:
            mgr = PlayModeManager.instance()
            if mgr is not None:
                return mgr.get_runtime_hidden_object_ids()
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return set()

    hp.get_runtime_hidden_ids = _get_runtime_hidden_ids

    runtime_manager = PlayModeManager.instance()
    if runtime_manager is not None:
        def _push_runtime_hidden_snapshot():
            target = hp_ref()
            if target is None:
                runtime_manager.remove_runtime_hidden_listener(_push_runtime_hidden_snapshot)
                return
            target.set_runtime_hidden_ids(runtime_manager.get_runtime_hidden_object_ids())

        runtime_manager.add_runtime_hidden_listener(_push_runtime_hidden_snapshot)
        _push_runtime_hidden_snapshot()

    # -- Delegate to sub-wirers --
    _wire_canvas_queries(ctx)

    from Infernux.engine.bootstrap_hierarchy._creation import wire_creation_callbacks
    wire_creation_callbacks(ctx)

    from Infernux.engine.bootstrap_hierarchy._prefab_clipboard import (
        wire_prefab_actions, wire_clipboard,
    )
    wire_prefab_actions(ctx)
    wire_clipboard(ctx)

    _wire_drop_and_delete(ctx)
