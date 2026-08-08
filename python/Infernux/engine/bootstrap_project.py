"""
Project panel callback wiring — extracted from EditorBootstrap.

Provides :func:`wire_project_callbacks` which attaches all Python-side
callbacks to a C++ ``ProjectPanel`` instance.
"""

from __future__ import annotations

import json
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

    def _command_payload(command_id: str, argument: str):
        if command_id == "asset.create":
            parts = str(argument or "").split("\t", 3)
            parts.extend([""] * (4 - len(parts)))
            return {
                "kind": parts[0],
                "base_name": parts[1],
                "extension": parts[2],
                "variant": parts[3],
            }
        if command_id == "asset.open":
            kind, separator, path = str(argument or "").partition("\t")
            return {"kind": kind, "path": path} if separator else {}
        if command_id == "asset.rename":
            source_path, separator, new_name = str(argument or "").partition("\t")
            return (
                {"source_path": source_path, "new_name": new_name}
                if separator
                else {}
            )
        if command_id == "asset.import_external":
            try:
                payload = json.loads(str(argument or ""))
            except (TypeError, ValueError):
                return {}
            if not isinstance(payload, dict):
                return {}
            paths = payload.get("paths", ())
            destination = payload.get("destination", "")
            if not isinstance(paths, list) or not isinstance(destination, str):
                return {}
            return {
                "paths": tuple(str(path) for path in paths),
                "destination": destination,
            }
        if command_id == "asset.transfer":
            try:
                payload = json.loads(str(argument or ""))
            except (TypeError, ValueError):
                return {}
            if not isinstance(payload, dict):
                return {}
            paths = payload.get("paths", ())
            destination = payload.get("destination", "")
            if not isinstance(paths, list) or not isinstance(destination, str):
                return {}
            return {
                "paths": tuple(str(path) for path in paths),
                "destination": destination,
            }
        if command_id == "prefab.save_as":
            object_id, separator, current_path = str(argument or "").partition("\t")
            if not separator:
                return {}
            try:
                resolved_object_id = int(object_id)
            except ValueError:
                return {}
            return {
                "object_id": resolved_object_id,
                "current_path": current_path,
            }
        if command_id in {
            "project.set_folder_expanded",
            "project.set_model_expanded",
        }:
            target_id, separator, expanded = str(argument or "").rpartition("\t")
            if not separator or not target_id or expanded not in {"0", "1"}:
                return {}
            return {"target_id": target_id, "expanded": expanded == "1"}
        target_id = str(argument or "").strip()
        return {"target_id": target_id} if target_id else {}

    def _execute_project_command(command_id, source, argument):
        return bs.interaction_core.commands.execute(
            command_id,
            source=CommandSource(source),
            payload=_command_payload(command_id, argument),
        ).accepted

    pp.execute_command = _execute_project_command

    def _render_project_context_menu(
        ctx_arg,
        target_path,
        reveal_path,
        current_path,
    ):
        from Infernux.engine.interaction import ContextMenuBuilder
        from Infernux.engine.ui.core_context_menus import project_context_menu

        ContextMenuBuilder(bs.interaction_core.commands).render(
            ctx_arg,
            project_context_menu(
                _t,
                target_path=str(target_path or ""),
                reveal_path=str(reveal_path or ""),
                current_path=str(current_path or ""),
            ),
            payload={"directory": str(current_path or "")},
        )

    pp.render_context_menu = _render_project_context_menu

    project_assets = bs.interaction_core.project_assets

    # -- Asset database access (via engine) --
    adb = bs.engine.get_asset_database()
    project_assets.configure(bs.project_path, adb)
    project_assets.add_change_listener(pp.invalidate_dir_cache)

    def _remap_project_directory(change) -> None:
        from Infernux.engine.interaction import (
            AssetMutationKind,
            iter_asset_mutations,
        )
        from Infernux.engine.path_utils import (
            is_path_within,
            relative_path,
            same_path,
        )

        current_path = str(pp.get_current_path() or "")
        if not current_path:
            return
        for mutation in iter_asset_mutations(change):
            if mutation.kind is not AssetMutationKind.MOVED:
                continue
            source = str(mutation.source_path or "")
            destination = str(mutation.destination_path or "")
            if same_path(current_path, source):
                remapped = destination
            elif is_path_within(current_path, source, allow_root=False):
                remapped = os.path.join(
                    destination,
                    relative_path(current_path, source),
                )
            else:
                continue
            pp.set_current_path(remapped)
            return

    bs.interaction_core.asset_mutations.add_listener(_remap_project_directory)

    pp.get_guid_from_path = lambda path: (
        adb.get_guid_from_path(path) if adb else ""
    )
    pp.get_path_from_guid = lambda guid: (
        adb.get_path_from_guid(guid) if adb else ""
    )

    # -- File operation callbacks --
    from Infernux.debug import Debug

    def _safe_project_path(cb, *args):
        try:
            return cb(*args) or ""
        except Exception as exc:
            Debug.log_error(f"ProjectPanel path operation failed: {exc}")
            return ""

    def _create_asset(kind, cur, name, variant):
        creators = {
            "folder": ("Create Folder", file_ops.create_folder, (cur, name)),
            "script": ("Create Script", file_ops.create_script, (cur, name, adb)),
            "shader": (
                "Create Shader",
                file_ops.create_shader,
                (cur, name, variant, adb),
            ),
            "material": (
                "Create Material",
                file_ops.create_material,
                (cur, name, adb),
            ),
            "physic_material": (
                "Create Physic Material",
                file_ops.create_physic_material,
                (cur, name, adb),
            ),
            "scene": ("Create Scene", file_ops.create_scene, (cur, name, adb)),
            "animation_clip": (
                "Create Animation Clip",
                file_ops.create_animclip,
                (cur, name, adb),
            ),
            "animation_clip3d": (
                "Create 3D Animation Clip",
                file_ops.create_animclip3d,
                (cur, name, adb),
            ),
            "animation_fsm": (
                "Create Animation State Machine",
                file_ops.create_animfsm,
                (cur, name, adb),
            ),
            "particle_graph": (
                "Create Particle Graph",
                file_ops.create_particlegraph,
                (cur, name, adb),
            ),
            "render_effect": (
                "Create Render Effect",
                file_ops.create_render_effect,
                (cur, name, variant, adb),
            ),
            "render_effect_group": (
                "Create Render Effect Group",
                file_ops.create_render_effect_group,
                (cur, name, adb),
            ),
            "animation_timeline": (
                "Create Timeline",
                file_ops.create_animtimeline,
                (cur, name, adb),
            ),
            "timeline_fsm": (
                "Create Timeline State Machine",
                file_ops.create_timelinefsm,
                (cur, name, adb),
            ),
        }
        spec = creators.get(str(kind or "").strip())
        if spec is None:
            return False, f"Unknown asset kind: {kind}"
        _description, callback, args = spec
        return callback(*args)

    # -- Unified asset-open adapter --

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
                return False

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

            accepted = bool(play_mode.exit_play_mode(on_complete=_on_stop))
            if not accepted:
                Debug.log_warning(
                    "Failed to stop Play Mode before opening scene")
            return accepted

        sfm = SceneFileManager.instance()
        if sfm:
            return bool(sfm.open_scene(file_path))
        Debug.log_warning("SceneFileManager not initialized")
        return False

    def _open_project_document(file_path, document_kind):
        return bs.interaction_core.commands.execute(
            "project.open_document",
            source=CommandSource.API,
            payload={
                "path": str(file_path or ""),
                "document_kind": str(document_kind or ""),
            },
        ).accepted

    def _open_asset(kind, file_path):
        kind = str(kind or "").strip()
        file_path = str(file_path or "").strip()
        if not kind or not file_path:
            return False
        if kind == "system":
            return bool(
                project_utils.open_file_with_system(
                    file_path, project_root=bs.project_path
                )
            )
        if kind == "scene":
            return _open_scene(file_path)
        if kind == "prefab":
            return bool(bs.interaction_core.prefabs.open(path=file_path))
        document_kinds = {
            "animation_clip": "animation_clip",
            "animation_fsm": "animation_fsm",
            "particle_graph": "particle_graph",
            "timeline": "timeline",
            "timeline_fsm": "animation_fsm",
        }
        document_kind = document_kinds.get(kind)
        return bool(
            document_kind
            and _open_project_document(file_path, document_kind)
        )

    from Infernux.engine.ui.project_delete_confirmation import (
        ProjectDeleteConfirmationCoordinator,
    )

    def _reveal_project_asset(path: str) -> bool:
        project_utils.reveal_in_file_explorer(path)
        return True

    project_asset_interactions = bs.interaction_core.project_asset_interactions
    project_asset_interactions.configure(
        unique_name=file_ops.get_unique_name,
        create=_create_asset,
        open_asset=_open_asset,
        reveal=_reveal_project_asset,
        read_external_clipboard=pp.get_os_clipboard_files,
        request_delete=ProjectDeleteConfirmationCoordinator.instance().request,
    )

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
    # The OS event is process-wide. Interaction Core resolves its pointer
    # owner before Project View is allowed to submit asset.import_external.
    from Infernux.lib import InxGUIRenderable, InxGUIContext, InputManager
    from Infernux.engine.interaction import ExternalDropKind

    class _ExternalDropForwarder(InxGUIRenderable):
        def on_render(self, ctx: InxGUIContext):
            try:
                im = InputManager.instance()
                if im is None or not im.has_dropped_files():
                    return
                if not bs.interaction_core.external_drops.accepts(
                    "project",
                    ExternalDropKind.FILES,
                ):
                    return
                files = im.get_dropped_files()
                if files and pp.get_current_path():
                    pp.receive_dropped_files(files)
            except Exception as exc:
                Debug.log_suppressed("bootstrap_project.ExternalDropForwarder.on_render", exc)

    bs._external_drop_forwarder = _ExternalDropForwarder()
    # External drops are edge-triggered and survive only this input frame.
    # Render after native panels so ProjectPanel has published the current
    # frame's visibility/hover state before Interaction Core resolves ownership.
    bs.engine.register_gui(
        "project_drop_forwarder",
        bs._external_drop_forwarder,
        priority=100,
    )

    # -- Panel focus sync --
    pp.on_panel_focused = bs.window_manager.native_panel_focus_callback(
        "project",
        view_id="project",
        source_instance=pp,
    )
