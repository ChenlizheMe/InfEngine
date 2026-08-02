"""
Project panel callback wiring — extracted from EditorBootstrap.

Provides :func:`wire_project_callbacks` which attaches all Python-side
callbacks to a C++ ``ProjectPanel`` instance.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Infernux.engine.bootstrap import EditorBootstrap


def wire_project_callbacks(bs: EditorBootstrap) -> None:
    """Wire C++ ProjectPanel callbacks to Python managers."""
    pp = bs.project_panel
    from Infernux.engine.i18n import t as _t
    from Infernux.engine.ui import project_file_ops as file_ops
    from Infernux.engine.ui import project_utils
    from Infernux.engine.scene_manager import SceneFileManager

    # -- Engine subsystems --
    native_engine = bs.engine.get_native_engine()
    if native_engine:
        pp.setup_from_engine(native_engine)

    pp.set_root_path(bs.project_path)

    import Infernux.resources as _resources
    pp.set_icons_directory(_resources.file_type_icons_dir)

    # -- Translation --
    pp.translate = _t

    from Infernux.engine.interaction import CommandSource

    def _command_payload(argument: str):
        target_id = str(argument or "").strip()
        return {"target_id": target_id} if target_id else {}

    def _activate_project_command_context():
        bs.interaction_core.focus.activate_panel("project", view_id="project")

    def _execute_project_command(command_id, source, argument):
        _activate_project_command_context()
        return bs.interaction_core.commands.execute(
            command_id,
            source=CommandSource(source),
            payload=_command_payload(argument),
        ).accepted

    def _can_execute_project_command(command_id, argument):
        _activate_project_command_context()
        return bs.interaction_core.commands.can_execute(
            command_id,
            bs.interaction_core.commands.context(
                CommandSource.CONTEXT_MENU,
                _command_payload(argument),
            ),
        )

    pp.execute_command = _execute_project_command
    pp.can_execute_command = _can_execute_project_command

    # -- Asset database access (via engine) --
    adb = bs.engine.get_asset_database()

    pp.get_guid_from_path = lambda path: (
        adb.get_guid_from_path(path) if adb else ""
    )
    pp.get_path_from_guid = lambda guid: (
        adb.get_path_from_guid(guid) if adb else ""
    )

    # -- File operation callbacks --
    from Infernux.debug import Debug

    def _safe_project_create(cb, *args):
        try:
            return cb(*args)
        except Exception as exc:
            Debug.log_error(f"ProjectPanel create failed: {exc}")
            return False, str(exc)

    def _safe_project_path(cb, *args):
        try:
            return cb(*args) or ""
        except Exception as exc:
            Debug.log_error(f"ProjectPanel path operation failed: {exc}")
            return ""

    pp.create_folder = lambda cur, name: _safe_project_create(
        file_ops.create_folder, cur, name)
    pp.create_script = lambda cur, name: _safe_project_create(
        file_ops.create_script, cur, name, adb)
    pp.create_shader = lambda cur, name, typ: _safe_project_create(
        file_ops.create_shader, cur, name, typ, adb)
    pp.create_material = lambda cur, name: _safe_project_create(
        file_ops.create_material, cur, name, adb)
    pp.create_physic_material = lambda cur, name: _safe_project_create(
        file_ops.create_physic_material, cur, name, adb)
    pp.create_scene = lambda cur, name: _safe_project_create(
        file_ops.create_scene, cur, name, adb)
    pp.create_animclip = lambda cur, name: _safe_project_create(
        file_ops.create_animclip, cur, name, adb)
    pp.create_animclip3d = lambda cur, name: _safe_project_create(
        file_ops.create_animclip3d, cur, name, adb)
    pp.create_animfsm = lambda cur, name: _safe_project_create(
        file_ops.create_animfsm, cur, name, adb)
    pp.create_particlegraph = lambda cur, name: _safe_project_create(
        file_ops.create_particlegraph, cur, name, adb)
    pp.create_render_effect = lambda cur, name, feature_type: _safe_project_create(
        file_ops.create_render_effect, cur, name, feature_type, adb)
    pp.create_render_effect_group = lambda cur, name: _safe_project_create(
        file_ops.create_render_effect_group, cur, name, adb)
    pp.create_animtimeline = lambda cur, name: _safe_project_create(
        file_ops.create_animtimeline, cur, name, adb)
    pp.create_timelinefsm = lambda cur, name: _safe_project_create(
        file_ops.create_timelinefsm, cur, name, adb)
    def _rename_asset(old_path: str, new_name: str) -> str:
        from Infernux.engine.interaction import (
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )
        from Infernux.engine.path_utils import same_path
        from Infernux.engine.undo import ProjectAssetRenameCommand, UndoManager

        before_context = bs.interaction_core.capture_context()
        new_path = _safe_project_path(
            file_ops.do_rename,
            old_path,
            new_name,
            adb,
        )
        if not new_path or same_path(old_path, new_path):
            return new_path

        manager = UndoManager.instance()
        if manager is None or manager.is_executing:
            return new_path

        next_snapshot = SelectionSnapshot.create(
            (SelectionTarget.asset(new_path),),
            owner_id="project",
        )
        SelectionService.instance().apply_snapshot(
            next_snapshot,
            reason="project_asset_rename",
            record_history=False,
        )
        # The native panel publishes the same selection after CommitRename.
        # Advance the compatibility snapshot now so that publication is a no-op
        # instead of a second selection-only history entry.
        with manager.suppress():
            bs._record_selection_snapshot(next_snapshot)

        manager.record(
            ProjectAssetRenameCommand(
                old_path,
                new_path,
                asset_database=adb,
                on_changed=pp.invalidate_dir_cache,
            ),
            before_context=before_context,
            after_context=bs.interaction_core.capture_context(),
        )
        return new_path

    pp.do_rename = _rename_asset
    pp.get_unique_name = lambda cur, base, ext: (
        file_ops.get_unique_name(cur, base, ext)
    )
    pp.move_item_to_directory = lambda item, dest: _safe_project_path(
        file_ops.move_item_to_directory, item, dest, adb)
    pp.copy_item_to_path = lambda item, dest: _safe_project_path(
        file_ops.copy_path_as_new_asset, item, dest, adb)

    # -- Delete (through an Editor-owned semantic modal) --
    def _delete_items(paths):
        valid = []
        seen = set()
        for p in paths or []:
            if not p or not os.path.exists(p) or p in seen:
                continue
            seen.add(p)
            valid.append(p)
        if not valid:
            return

        from Infernux.engine.ui.project_delete_confirmation import (
            ProjectDeleteConfirmationCoordinator,
        )

        def _delete_confirmed(confirmed_paths: list[str]) -> bool:
            for item_path in sorted(
                confirmed_paths,
                key=lambda p: (p.count(os.sep), len(p)),
                reverse=True,
            ):
                if os.path.exists(item_path) and not file_ops.delete_item(item_path, adb):
                    return False
            pp.clear_selection()
            pp.invalidate_dir_cache()
            return not any(os.path.exists(path) for path in confirmed_paths)

        ProjectDeleteConfirmationCoordinator.instance().request(valid, _delete_confirmed)

    pp.delete_items = _delete_items

    # -- Create prefab from hierarchy drag --
    def _create_prefab_from_hierarchy(obj_id, current_path):
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        if not scene:
            Debug.log_warning("Failed to create prefab: no active scene")
            return
        game_object = scene.find_by_id(obj_id)
        if not game_object:
            Debug.log_warning(f"Failed to create prefab: object {obj_id} not found")
            return
        if not current_path:
            Debug.log_warning("Failed to create prefab: no target folder")
            return
        # Detect canvas parent for UI prefabs
        source_canvas_name = ""
        cur = game_object.get_parent() if hasattr(game_object, 'get_parent') else None
        while cur is not None:
            try:
                from Infernux.ui import UICanvas as _UCCls
                for comp in (cur.get_py_components() or []):
                    if isinstance(comp, _UCCls):
                        source_canvas_name = cur.name
                        break
            except Exception:
                pass
            if source_canvas_name:
                break
            cur = cur.get_parent()

        ok, result = file_ops.create_prefab_from_gameobject(
            game_object, current_path, adb,
            source_canvas_name=source_canvas_name)
        if not ok:
            from Infernux.debug import Debug
            Debug.log_warning(f"Failed to create prefab: {result}")
        else:
            # Invalidate the project panel cache so the new file appears
            # immediately, and request full-speed rendering so the panel
            # refreshes without waiting for the idle-throttle cooldown.
            pp.invalidate_dir_cache()
            native = bs.engine.get_native_engine()
            if native:
                native.request_full_speed_frame()

    pp.create_prefab_from_hierarchy = _create_prefab_from_hierarchy

    # -- Open callbacks --
    pp.open_file = lambda path: project_utils.open_file_with_system(
        path, project_root=bs.project_path)

    def _open_scene(file_path):
        from Infernux.debug import Debug
        from Infernux.engine.deferred_task import DeferredTaskRunner
        from Infernux.engine.play_mode import PlayModeManager

        def _open_after_stop():
            sfm = SceneFileManager.instance()
            if sfm:
                return bool(sfm.open_scene(file_path))
            Debug.log_warning("SceneFileManager not initialized")
            return False

        play_mode = PlayModeManager.instance()
        if play_mode and play_mode.is_playing:
            runner = DeferredTaskRunner.instance()
            if runner.is_busy:
                Debug.log_warning(
                    "Cannot open scene while another deferred task is running")
                return

            def _on_stop(ok):
                if not ok:
                    Debug.log_warning(
                        "Play Mode stop did not complete; scene open cancelled")
                    return
                try:
                    from Infernux.lib import SceneManager as NativeSM
                    nsm = NativeSM.instance()
                except Exception:
                    nsm = None
                if play_mode.is_playing:
                    Debug.log_warning(
                        "Scene open cancelled — Play Mode still active")
                    return
                if nsm and nsm.is_playing():
                    Debug.log_warning(
                        "Scene open cancelled — native Play Mode still active")
                    return
                _open_after_stop()

            if not play_mode.exit_play_mode(on_complete=_on_stop):
                Debug.log_warning(
                    "Failed to stop Play Mode before opening scene")
            return

        sfm = SceneFileManager.instance()
        if sfm:
            sfm.open_scene(file_path)
        else:
            Debug.log_warning("SceneFileManager not initialized")

    pp.open_scene = _open_scene

    pp.open_prefab_mode = lambda path: (
        SceneFileManager.instance().open_prefab_mode_with_undo(path)
        if SceneFileManager.instance() else None
    )

    def _open_anim_clip(file_path):
        from Infernux.engine.ui.window_manager import WindowManager
        from Infernux.engine.ui.closable_panel import ClosablePanel
        wm = WindowManager.instance()
        if wm is None:
            return
        panel = wm.open_window("animclip2d_editor")
        if panel is not None and hasattr(panel, '_open_animclip'):
            panel._open_animclip(file_path)
            ClosablePanel.focus_panel_by_id("animclip2d_editor")
            try:
                wm._engine.select_docked_window("animclip2d_editor")
            except Exception:
                pass

    pp.open_anim_clip = _open_anim_clip

    def _open_anim_fsm(file_path):
        from Infernux.engine.ui.window_manager import WindowManager
        from Infernux.engine.ui.closable_panel import ClosablePanel
        wm = WindowManager.instance()
        if wm is None:
            return
        panel = wm.open_window("animfsm_editor")
        if panel is not None and hasattr(panel, '_open_animfsm'):
            panel._open_animfsm(file_path)
            ClosablePanel.focus_panel_by_id("animfsm_editor")
            try:
                wm._engine.select_docked_window("animfsm_editor")
            except Exception:
                pass

    pp.open_anim_fsm = _open_anim_fsm

    def _open_particle_graph(file_path):
        from Infernux.engine.ui.window_manager import WindowManager
        from Infernux.engine.ui.closable_panel import ClosablePanel
        wm = WindowManager.instance()
        if wm is None:
            return
        panel = wm.open_window("particle_graph_editor")
        if panel is not None and hasattr(panel, "_open_particlegraph"):
            panel._open_particlegraph(file_path)
            ClosablePanel.focus_panel_by_id("particle_graph_editor")
            try:
                wm._engine.select_docked_window("particle_graph_editor")
            except Exception:
                pass

    pp.open_particle_graph = _open_particle_graph

    def _open_anim_timeline(file_path):
        from Infernux.engine.ui.window_manager import WindowManager
        from Infernux.engine.ui.closable_panel import ClosablePanel
        wm = WindowManager.instance()
        if wm is None:
            return
        panel = wm.open_window("animtimeline_editor")
        if panel is not None and hasattr(panel, '_open_timeline'):
            panel._open_timeline(file_path)
            ClosablePanel.focus_panel_by_id("animtimeline_editor")
            try:
                wm._engine.select_docked_window("animtimeline_editor")
            except Exception:
                pass

    pp.open_anim_timeline = _open_anim_timeline

    def _open_timeline_fsm(file_path):
        from Infernux.engine.ui.window_manager import WindowManager
        from Infernux.engine.ui.closable_panel import ClosablePanel
        wm = WindowManager.instance()
        if wm is None:
            return
        panel = wm.open_window("animfsm_editor")
        if panel is not None and hasattr(panel, '_open_animfsm'):
            panel._open_animfsm(file_path)
            ClosablePanel.focus_panel_by_id("animfsm_editor")
            try:
                wm._engine.select_docked_window("animfsm_editor")
            except Exception:
                pass

    pp.open_timeline_fsm = _open_timeline_fsm

    pp.reveal_in_explorer = lambda path: (
        project_utils.reveal_in_file_explorer(path)
    )

    # -- Script validation for drag-drop --
    def _validate_script_component(file_path):
        try:
            from Infernux.components.script_loader import (
                load_component_from_file, ScriptLoadError)
            load_component_from_file(file_path)
            return True
        except Exception as exc:
            Debug.log_suppressed(
                f"bootstrap_project.validate_script_component[{os.path.basename(file_path)}]",
                exc,
            )
            return False

    pp.validate_script_component = _validate_script_component

    # -- Inspector invalidation --
    def _invalidate_asset_inspector(path):
        try:
            from Infernux.engine.ui.asset_details_renderer import (
                invalidate_asset)
            invalidate_asset(path)
        except Exception as exc:
            Debug.log_suppressed("bootstrap_project.invalidate_asset_inspector", exc)

    pp.invalidate_asset_inspector = _invalidate_asset_inspector

    # -- External file drop from OS (e.g. Windows Explorer drag) ------
    # Register a lightweight per-frame renderable that forwards OS-level
    # file drops to the ProjectPanel when it is the focused window.
    from Infernux.lib import InxGUIRenderable, InxGUIContext, InputManager

    class _ExternalDropForwarder(InxGUIRenderable):
        def on_render(self, ctx: InxGUIContext):
            try:
                im = InputManager.instance()
                if im is None or not im.has_dropped_files():
                    return
                files = im.get_dropped_files()
                if files and pp.get_current_path():
                    pp.receive_dropped_files(files)
            except Exception as exc:
                Debug.log_suppressed("bootstrap_project.ExternalDropForwarder.on_render", exc)

    bs._external_drop_forwarder = _ExternalDropForwarder()
    bs.engine.register_gui("project_drop_forwarder", bs._external_drop_forwarder)

    # -- Panel focus sync --
    def _on_project_focus_changed(focused: bool):
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focused:
            focus.activate_panel("project", view_id="project")
        else:
            focus.deactivate_panel("project")

    pp.on_project_panel_focused = _on_project_focus_changed
