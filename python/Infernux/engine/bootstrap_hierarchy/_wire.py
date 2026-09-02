"""Main wiring function for the C++ HierarchyPanel."""
from __future__ import annotations

from dataclasses import replace
import weakref
from typing import TYPE_CHECKING

from Infernux.debug import Debug
from Infernux.engine.bootstrap_hierarchy._helpers import (
    _get_children,
    _get_py_components,
)

if TYPE_CHECKING:
    from Infernux.engine.bootstrap import EditorBootstrap


class _Ctx:
    """Thin namespace shared across hierarchy wiring helpers."""


def _get_hierarchy_scene():
    from Infernux.lib import SceneManager as _SM
    return _SM.instance().get_active_scene()


# ═══════ UI structure queries ══════════════════════════════════

def _wire_canvas_queries(ctx):
    """Wire canvas and UI-component detection callbacks."""
    hp = ctx.hp

    query_cache = {
        "scene_ref": None,
        "scene_structure_version": -1,
        "canvas_list_token": 0,
        "canvas_object_ids": set(),
        "canvas_tree_ids": set(),
    }

    def _clear_query_cache():
        query_cache["scene_ref"] = None
        query_cache["scene_structure_version"] = -1
        query_cache["canvas_list_token"] = 0
        query_cache["canvas_object_ids"] = set()
        query_cache["canvas_tree_ids"] = set()

    def _ensure_query_cache(scene):
        if scene is None:
            _clear_query_cache()
            return query_cache

        from Infernux.engine.ui.runtime_canvas_snapshot import (
            collect_runtime_canvas_snapshot_with_go,
        )
        from Infernux.lib import SceneManager as _SM

        canvases_with_go = collect_runtime_canvas_snapshot_with_go(
            scene,
            _SM.instance().get_runtime_persistent_scene(),
        )
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

        for canvas_go, _canvas in canvases_with_go:
            if canvas_go is None or getattr(canvas_go, "scene", None) is not scene:
                continue

            canvas_go_id = int(getattr(canvas_go, "id", 0) or 0)
            if canvas_go_id:
                canvas_object_ids.add(canvas_go_id)

            pending = [canvas_go]
            while pending:
                current = pending.pop()
                current_id = int(getattr(current, "id", 0) or 0)
                if current_id in canvas_tree_ids:
                    continue
                if current_id:
                    canvas_tree_ids.add(current_id)
                pending.extend(_get_children(current))

        query_cache["scene_ref"] = scene
        query_cache["scene_structure_version"] = scene_structure_version
        query_cache["canvas_list_token"] = canvas_list_token
        query_cache["canvas_object_ids"] = canvas_object_ids
        query_cache["canvas_tree_ids"] = canvas_tree_ids
        return query_cache

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
        for comp in _get_py_components(go):
            if isinstance(comp, InxUIScreenComponent):
                return True
        return False

    def _parent_has_canvas_ancestor(oid):
        scene = _get_hierarchy_scene()
        if not scene:
            return False
        cache = _ensure_query_cache(scene)
        return int(oid) in cache["canvas_tree_ids"]

    hp.go_has_canvas = _go_has_canvas
    hp.go_has_ui_screen_component = _go_has_ui_screen_component
    hp.parent_has_canvas_ancestor = _parent_has_canvas_ancestor


# ═══════ Main entry point ══════════════════════════════════════

def wire_hierarchy_callbacks(bs: EditorBootstrap) -> None:
    """Wire C++ HierarchyPanel callbacks to Python managers."""
    hp = bs.hierarchy
    from Infernux.engine.interaction import SelectionService
    from Infernux.engine.i18n import t as _t
    from Infernux.engine.play_mode import PlayModeManager

    selection = SelectionService.instance()

    ctx = _Ctx()
    ctx.hp = hp
    ctx.bs = bs
    ctx.selection = selection
    ctx._t = _t

    # -- Selection integration --
    hp.is_selected = lambda oid: selection.is_scene_object_selected(oid)
    hp.select_id = lambda oid: selection.select_scene_object(
        oid, owner_id="hierarchy"
    )
    hp.toggle_id = lambda oid: selection.toggle_scene_object(
        oid, owner_id="hierarchy"
    )
    hp.range_select_id = lambda oid: selection.range_select_scene_object(
        oid, owner_id="hierarchy"
    )
    hp.clear_selection = lambda: selection.clear(reason="hierarchy_clear")
    hp.get_primary = selection.primary_scene_object_id
    hp.get_selected_ids = lambda: list(selection.scene_object_ids())
    hp.selection_count = lambda: len(selection.scene_object_ids())
    hp.is_selection_empty = lambda: not selection.scene_object_ids()
    hp.set_ordered_ids = lambda ids: selection.set_ordered_scene_objects("hierarchy", ids)

    hp_ref = weakref.ref(hp)

    def _push_selection_snapshot(_change=None):
        target = hp_ref()
        if target is None:
            selection.remove_listener(_push_selection_snapshot)
            return
        target.set_selection_snapshot(
            list(selection.scene_object_ids()),
            selection.primary_scene_object_id(),
        )

    selection.add_listener(_push_selection_snapshot)
    _push_selection_snapshot()

    # -- Panel focus sync --
    hp.on_panel_focused = bs.window_manager.native_panel_focus_callback(
        "hierarchy",
        view_id="hierarchy",
        source_instance=hp,
    )

    # -- Unified command routing --
    from Infernux.engine.interaction import CommandSource

    command_registry = bs.interaction_core.commands

    def _command_payload(command_id, argument):
        value = str(argument or "")
        if command_id == "scene.create_object":
            kind, separator, parent_id = value.partition("\t")
            if not separator:
                return {}
            try:
                resolved_parent_id = int(parent_id or 0)
            except ValueError:
                return {}
            return {
                "kind": kind.strip(),
                "parent_id": resolved_parent_id,
            }
        if command_id in {"scene.instantiate_prefab", "scene.create_model"}:
            parts = value.rsplit("\t", 2)
            if len(parts) != 3:
                return {}
            reference, parent_id, guid_flag = parts
            try:
                resolved_parent_id = int(parent_id or 0)
            except ValueError:
                return {}
            return {
                "reference": reference.strip(),
                "parent_id": resolved_parent_id,
                "is_guid": guid_flag == "1",
            }
        if command_id == "scene.rename_object":
            object_id, separator, new_name = value.partition("\t")
            if not separator:
                return {}
            try:
                resolved_object_id = int(object_id)
            except ValueError:
                return {}
            return {
                "object_id": resolved_object_id,
                "new_name": new_name,
            }
        if command_id == "scene.move_hierarchy":
            parts = value.split("\t")
            if len(parts) != 4:
                return {}
            object_ids, mode, target_id, after = parts
            try:
                resolved_ids = [int(item) for item in object_ids.split(",") if item]
                resolved_target_id = int(target_id or 0)
            except ValueError:
                return {}
            return {
                "object_ids": resolved_ids,
                "mode": mode,
                "target_id": resolved_target_id,
                "after": after == "1",
            }
        if command_id == "hierarchy.set_expanded":
            target_id, separator, expanded = value.rpartition("\t")
            if not separator or expanded not in {"0", "1"}:
                return {}
            try:
                resolved_target_id = int(target_id)
            except ValueError:
                return {}
            return {
                "target_id": resolved_target_id,
                "expanded": expanded == "1",
            }
        target_id = value.strip()
        return {"target_id": target_id} if target_id else {}

    def _execute_hierarchy_command(command_id, source, argument):
        """Route native pointer gestures through their actual owner view.

        Native widgets execute while ImGui is still publishing the new focus.
        Using the previous global focus here can route a Hierarchy gesture to
        Scene View (or another panel), which makes foldout clicks look inert.
        """
        command_context = command_registry.context(
            CommandSource(source),
            _command_payload(command_id, argument),
        )
        hierarchy_focus = replace(
            command_context.focus,
            active_panel_id="hierarchy",
            active_view_id="hierarchy",
            child_context_id="",
            capture_owner_id="",
        )
        return command_registry.execute_context(
            command_id,
            replace(command_context, focus=hierarchy_focus),
        ).accepted

    hp.execute_command = _execute_hierarchy_command
    def _render_context_menu(
        ctx_arg,
        target_id,
        target_is_prefab,
        create_parent_id,
    ):
        from Infernux.engine.interaction import ContextMenuBuilder
        from Infernux.engine.ui.core_context_menus import hierarchy_context_menu

        ContextMenuBuilder(command_registry).render(
            ctx_arg,
            hierarchy_context_menu(
                _t,
                target_id=int(target_id or 0),
                target_is_prefab=bool(target_is_prefab),
                create_parent_id=int(create_parent_id or 0),
            ),
        )

    hp.render_context_menu = _render_context_menu

    # -- Translation & warning --
    hp.translate = _t
    hp.show_warning = lambda msg: Debug.log_warning(msg)

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
        mgr = PlayModeManager.instance()
        if mgr is None:
            raise RuntimeError("Hierarchy runtime visibility requires PlayModeManager")
        return mgr.get_runtime_hidden_object_ids()

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
