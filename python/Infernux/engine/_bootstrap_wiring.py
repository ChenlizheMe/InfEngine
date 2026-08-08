"""BootstrapWiringMixin — extracted from EditorBootstrap."""
from __future__ import annotations

"""
EditorBootstrap — structured editor initialization.

Breaks the monolithic ``release_engine()`` startup path into explicit
startup steps. Each step is a separate method, closures become instance
methods, and panel/manager references live on the bootstrap instance.
"""


import logging
import os
import pathlib
from typing import Optional

from Infernux.lib import TagLayerManager
import Infernux.resources as _resources
from Infernux.engine.engine import Engine, LogLevel
from Infernux.engine.resources_manager import ResourcesManager
from Infernux.engine.play_mode import PlayModeManager, PlayModeState
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.ui import (
    SceneViewPanel,
    GameViewPanel,
    WindowManager,
    TagLayerSettingsPanel,
    BuildSettingsPanel,
    UIEditorPanel,
    EditorPanel,
    EditorServices,
    PanelRegistry,
    editor_panel,
)
from Infernux.engine.ui import panel_state as _panel_state


class BootstrapWiringMixin:
    """BootstrapWiringMixin method group for EditorBootstrap."""

    def _register_core_editor_commands(self, wm, sfm) -> None:
        from Infernux.engine.interaction import (
            ActionOrigin,
            ContinuousEditService,
            EditorCommand,
            KeyChord,
            SelectionDomain,
            ShortcutBinding,
            ShortcutScope,
        )
        from Infernux.engine._bootstrap_panels import (
            PERMANENT_EDITOR_WINDOW_TYPE_IDS,
        )
        from Infernux.engine.undo import UndoManager
        registry = self.interaction_core.commands
        shortcuts = self.interaction_core.shortcuts
        panel_interactions = self.interaction_core.panels
        pmm = self.engine._play_mode_manager if self.engine else None

        def _native_engine():
            getter = getattr(self.engine, "get_native_engine", None)
            return getter() if callable(getter) else None

        def _undo(_context):
            ContinuousEditService.instance().commit_all()
            manager = UndoManager.instance()
            if not manager or not manager.can_undo:
                return False
            manager.undo()
            return True

        def _redo(_context):
            manager = UndoManager.instance()
            if not manager or not manager.can_redo:
                return False
            manager.redo()
            return True

        def _toggle_play(_context):
            if not pmm:
                return False
            if pmm.is_playing:
                pmm.exit_play_mode()
                return True
            if not pmm.enter_play_mode():
                return False
            wm.open_window_from_user(
                "game_view",
                reason="play_mode_enter",
            )
            return True

        def _new_scene(_context):
            sfm.new_scene()
            return True

        def _pause(_context):
            pmm.toggle_pause()
            return True

        def _step(_context):
            pmm.step_frame()
            return True

        def _toggle_scene_grid(_context) -> bool:
            native = _native_engine()
            if native is None:
                return False
            current = bool(native.is_show_grid())
            return self.interaction_core.view_commands.set_value(
                current,
                not current,
                lambda value: native.set_show_grid(bool(value)),
                description="Toggle Scene Grid",
                owner_view_id="scene_view",
            )

        def _is_scene_grid_visible(_context) -> bool:
            native = _native_engine()
            return bool(native is not None and native.is_show_grid())

        def _window_target(context) -> str:
            return str(context.payload.get("target_id", "") or "").strip()

        def _open_window(context):
            target_id = _window_target(context)
            if not target_id:
                return False
            return wm.open_window_from_user(target_id) is not None

        def _toggle_window_target(target_id: str) -> bool:
            target = str(target_id or "").strip()
            if not target or target not in wm.get_registered_types():
                return False
            if target in PERMANENT_EDITOR_WINDOW_TYPE_IDS:
                return False
            if wm.is_window_open(target):
                wm.close_window(target)
                return True
            return wm.open_window_from_user(
                target,
                reason="window_toggle_command",
            ) is not None

        def _toggle_window(context) -> bool:
            return _toggle_window_target(_window_target(context))

        def _reset_layout(_context):
            return wm.reset_layout() is not False

        def _open_console_entry(context) -> bool:
            try:
                uid = int(context.payload.get("uid", 0) or 0)
            except (TypeError, ValueError):
                return False
            panel = wm.open_window_from_user(
                "console",
                reason="status_bar_console",
            )
            if panel is None:
                return False
            selector = getattr(panel, "select_entry", None)
            if not callable(selector):
                return False
            selector(uid)
            return True

        def _console_source_path(context) -> str:
            return str(context.payload.get("source_path", "") or "").strip()

        def _open_console_source(context) -> bool:
            source_path = _console_source_path(context)
            if not source_path:
                return False
            from Infernux.engine.ui import project_utils

            return bool(
                project_utils.open_file_with_system(
                    source_path,
                    project_root=self.project_path,
                )
            )

        def _owns_panel_command(context, command_id: str) -> bool:
            return panel_interactions.owns_active(context, command_id)

        def _can_panel_command(context, command_id: str) -> bool:
            return panel_interactions.can_execute_active(context, command_id)

        def _invoke_panel_command(context, command_id: str) -> bool:
            return panel_interactions.execute_active(context, command_id)

        def _prefab_target(context) -> tuple[int, str, str]:
            payload = context.payload
            try:
                object_id = int(
                    payload.get("object_id", 0)
                    or payload.get("target_id", 0)
                    or 0
                )
            except (TypeError, ValueError):
                object_id = 0
            if object_id <= 0 and context.selection.primary is not None:
                primary = context.selection.primary
                if primary.domain is SelectionDomain.SCENE_OBJECT:
                    object_id = primary.scene_object_id()
                elif primary.domain is SelectionDomain.COMPONENT:
                    object_id = primary.component_ids()[0]
            return (
                object_id,
                str(payload.get("path", "") or "").strip(),
                str(payload.get("current_path", "") or "").strip(),
            )

        def _command_origin(context) -> ActionOrigin:
            from Infernux.engine.interaction import CommandSource

            return (
                ActionOrigin.AUTOMATION
                if context.source is CommandSource.AUTOMATION
                else ActionOrigin.USER
            )

        def _can_prefab_command(context, action: str) -> bool:
            object_id, path, _current_path = _prefab_target(context)
            service_action = {
                "save_as": "create",
                "select": "locate",
            }.get(action, action)
            return self.interaction_core.prefabs.can_execute(
                service_action,
                object_id=object_id,
                path=path,
            )

        def _execute_prefab_command(context, action: str) -> bool:
            object_id, path, current_path = _prefab_target(context)
            service = self.interaction_core.prefabs
            origin = _command_origin(context)
            if action == "save_as":
                return bool(
                    service.create_from_object(
                        object_id,
                        current_path,
                        origin=origin,
                    )
                )
            if action == "select":
                return service.locate(object_id=object_id, path=path)
            if action == "open":
                return service.open(object_id=object_id, path=path, origin=origin)
            if action == "exit":
                return service.exit(origin=origin)
            if action == "apply":
                return service.apply(object_id, origin=origin)
            if action == "revert":
                return service.revert(object_id, origin=origin)
            if action == "unpack":
                return service.unpack(object_id, origin=origin)
            return False

        def _can_target_panel_command(
            context,
            view_id: str,
            command_id: str,
        ) -> bool:
            return panel_interactions.can_execute_view(
                view_id,
                context,
                command_id,
            )

        def _invoke_target_panel_command(
            context,
            view_id: str,
            command_id: str,
        ) -> bool:
            return panel_interactions.execute_view(
                view_id,
                context,
                command_id,
            )

        def _clear_edit_selection(context) -> bool:
            if _owns_panel_command(context, "edit.deselect"):
                return _invoke_panel_command(context, "edit.deselect")
            return self.interaction_core.selection.clear(
                reason="edit_deselect",
                record_history=True,
            )

        def _copy_edit_selection(context, *, cut: bool):
            command_id = "edit.cut" if cut else "edit.copy"
            return _invoke_panel_command(context, command_id)

        def _paste_edit_selection(context):
            return _invoke_panel_command(context, "edit.paste")

        def _delete_edit_selection(context):
            return _invoke_panel_command(context, "edit.delete")

        def _rename_edit_selection(context):
            return _invoke_panel_command(context, "edit.rename")

        def _can_copy_edit_selection(context) -> bool:
            return _can_panel_command(context, "edit.copy")

        def _can_cut_edit_selection(context) -> bool:
            return _can_panel_command(context, "edit.cut")

        def _can_delete_edit_selection(context) -> bool:
            return _can_panel_command(context, "edit.delete")

        def _can_paste_edit_selection(context) -> bool:
            return _can_panel_command(context, "edit.paste")

        def _can_rename_edit_selection(context) -> bool:
            return _can_panel_command(context, "edit.rename")

        def _create_project_folder(context):
            return _invoke_target_panel_command(
                context, "project", "project.create_folder"
            )

        def _asset_rename_target(context) -> tuple[str, str]:
            return (
                str(context.payload.get("source_path", "") or "").strip(),
                str(context.payload.get("new_name", "") or "").strip(),
            )

        def _can_rename_project_asset(context) -> bool:
            source_path, new_name = _asset_rename_target(context)
            return self.interaction_core.project_assets.can_rename(
                source_path,
                new_name,
            )

        def _rename_project_asset(context) -> bool:
            source_path, new_name = _asset_rename_target(context)
            return bool(
                self.interaction_core.project_assets.rename(
                    source_path,
                    new_name,
                    origin=_command_origin(context),
                )
            )

        def _asset_import_target(context) -> tuple[tuple[str, ...], str]:
            raw_paths = context.payload.get("paths", ())
            if isinstance(raw_paths, str):
                raw_paths = (raw_paths,)
            paths = tuple(
                str(path).strip()
                for path in raw_paths
                if str(path or "").strip()
            )
            destination = str(
                context.payload.get("destination", "") or ""
            ).strip()
            return paths, destination

        def _can_import_external_assets(context) -> bool:
            paths, destination = _asset_import_target(context)
            return self.interaction_core.project_assets.can_import_external(
                paths,
                destination,
            )

        def _import_external_assets(context) -> bool:
            paths, destination = _asset_import_target(context)
            return bool(
                self.interaction_core.project_assets.import_external(
                    paths,
                    destination,
                    origin=_command_origin(context),
                    select_results=True,
                )
            )

        def _open_project_document(context) -> bool:
            from Infernux.engine.interaction import (
                DocumentKind,
                DocumentOpenStatus,
            )

            path = str(context.payload.get("path", "") or "").strip()
            kind_name = str(
                context.payload.get("document_kind", "") or ""
            ).strip()
            if not path or not kind_name:
                return False
            try:
                kind = DocumentKind(kind_name)
            except ValueError:
                return False
            guid = ""
            asset_database = self.engine.get_asset_database() if self.engine else None
            if asset_database is not None:
                guid = str(asset_database.get_guid_from_path(path) or "")
            result = self.interaction_core.document_open.open_resource(
                kind,
                path,
                guid=guid,
                title=os.path.basename(path),
            )
            if result.status is DocumentOpenStatus.FAILED:
                return False

            panel_id = {
                DocumentKind.ANIMATION_CLIP: "animclip2d_editor",
                DocumentKind.ANIMATION_FSM: "animfsm_editor",
                DocumentKind.TIMELINE: "animtimeline_editor",
                DocumentKind.PARTICLE_GRAPH: "particle_graph_editor",
            }.get(kind)
            if not panel_id:
                return False

            # DocumentOpenService owns resource replacement and registration;
            # WindowManager owns the user-visible navigation edge. Calling
            # this after open_resource lets a newly registered dynamic panel
            # defer focus until register_gui completes, while the history
            # context already points at the opened document.
            return wm.open_window_from_user(
                panel_id,
                reason="project_open_document",
            ) is not None

        def _object_property_target(context):
            payload = context.payload
            try:
                object_id = int(payload.get("object_id", 0) or 0)
            except (TypeError, ValueError):
                return None
            property_name = str(payload.get("property", "") or "").strip()
            if object_id <= 0 or not property_name or "value" not in payload:
                return None
            return object_id, property_name, payload["value"]

        def _can_set_object_property(context) -> bool:
            target = _object_property_target(context)
            return bool(
                target is not None
                and self.interaction_core.scene_objects.can_set_object_property(*target)
            )

        def _set_object_property(context) -> bool:
            target = _object_property_target(context)
            return bool(
                target is not None
                and self.interaction_core.scene_objects.set_object_property(*target)
            )

        def _transform_targets(context):
            payload = context.payload
            object_ids = payload.get("object_ids", ())
            transforms = payload.get("transforms", ())
            if isinstance(object_ids, (str, bytes)) or isinstance(
                transforms, (str, bytes)
            ):
                return None
            try:
                return tuple(object_ids), tuple(transforms)
            except TypeError:
                return None

        def _can_set_transforms(context) -> bool:
            target = _transform_targets(context)
            return bool(
                target is not None
                and self.interaction_core.scene_objects.can_set_transforms(*target)
            )

        def _set_transforms(context) -> bool:
            target = _transform_targets(context)
            return bool(
                target is not None
                and self.interaction_core.scene_objects.set_transforms(*target)
            )

        commands = (
            EditorCommand(
                "file.new_scene",
                _new_scene,
                display_name="New Scene",
                category="File",
                default_shortcut="Ctrl+N",
            ),
            EditorCommand(
                "file.save",
                lambda _context: self.interaction_core.saving.save_focused().accepted,
                display_name="Save",
                category="File",
                default_shortcut="Ctrl+S",
            ),
            EditorCommand(
                "file.save_as",
                lambda _context: self.interaction_core.saving.save_focused(
                    save_as=True
                ).accepted,
                display_name="Save As",
                category="File",
                default_shortcut="Ctrl+Shift+S",
            ),
            EditorCommand(
                "edit.undo",
                _undo,
                display_name="Undo",
                category="Edit",
                can_execute=lambda _context: bool(
                    ContinuousEditService.instance().active_count
                    or (UndoManager.instance() and UndoManager.instance().can_undo)
                ),
                default_shortcut="Ctrl+Z",
            ),
            EditorCommand(
                "edit.redo",
                _redo,
                display_name="Redo",
                category="Edit",
                can_execute=lambda _context: bool(
                    UndoManager.instance() and UndoManager.instance().can_redo
                ),
                default_shortcut="Ctrl+Shift+Z",
            ),
            EditorCommand(
                "edit.copy",
                lambda context: _copy_edit_selection(context, cut=False),
                display_name="Copy",
                category="Edit",
                can_execute=_can_copy_edit_selection,
                default_shortcut="Ctrl+C",
            ),
            EditorCommand(
                "edit.cut",
                lambda context: _copy_edit_selection(context, cut=True),
                display_name="Cut",
                category="Edit",
                can_execute=_can_cut_edit_selection,
                default_shortcut="Ctrl+X",
            ),
            EditorCommand(
                "edit.paste",
                _paste_edit_selection,
                display_name="Paste",
                category="Edit",
                can_execute=_can_paste_edit_selection,
                default_shortcut="Ctrl+V",
            ),
            EditorCommand(
                "edit.delete",
                _delete_edit_selection,
                display_name="Delete",
                category="Edit",
                can_execute=_can_delete_edit_selection,
                default_shortcut="Delete",
            ),
            EditorCommand(
                "interaction.cancel",
                lambda _context: self.interaction_core.cancel_active_interaction(),
                display_name="Cancel Current Interaction",
                category="Edit",
                can_execute=lambda _context: self.interaction_core.can_cancel_active_interaction,
                default_shortcut="Escape",
            ),
            EditorCommand(
                "edit.deselect",
                _clear_edit_selection,
                display_name="Deselect All",
                category="Edit",
                can_execute=lambda context: bool(context.selection.targets),
                default_shortcut="Escape",
            ),
            EditorCommand(
                "edit.rename",
                _rename_edit_selection,
                display_name="Rename",
                category="Edit",
                can_execute=_can_rename_edit_selection,
                default_shortcut="F2",
            ),
            EditorCommand(
                "edit.duplicate",
                lambda context: _invoke_panel_command(context, "edit.duplicate"),
                display_name="Duplicate",
                category="Edit",
                can_execute=lambda context: _can_panel_command(
                    context, "edit.duplicate"
                ),
                default_shortcut="Ctrl+D",
            ),
            EditorCommand(
                "component.open_script",
                lambda context: _invoke_panel_command(
                    context, "component.open_script"
                ),
                display_name="Open Script",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.open_script"
                ),
            ),
            EditorCommand(
                "component.copy_properties",
                lambda context: _invoke_panel_command(
                    context, "component.copy_properties"
                ),
                display_name="Copy Component",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.copy_properties"
                ),
            ),
            EditorCommand(
                "component.paste_properties",
                lambda context: _invoke_panel_command(
                    context, "component.paste_properties"
                ),
                display_name="Paste Component Values",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.paste_properties"
                ),
            ),
            EditorCommand(
                "component.paste_as_new",
                lambda context: _invoke_panel_command(
                    context, "component.paste_as_new"
                ),
                display_name="Paste Component As New",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.paste_as_new"
                ),
            ),
            EditorCommand(
                "component.remove",
                lambda context: _invoke_panel_command(context, "component.remove"),
                display_name="Remove Component",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.remove"
                ),
            ),
            EditorCommand(
                "component.reset",
                lambda context: _invoke_panel_command(context, "component.reset"),
                display_name="Reset Component",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.reset"
                ),
            ),
            EditorCommand(
                "component.move_up",
                lambda context: _invoke_panel_command(context, "component.move_up"),
                display_name="Move Component Up",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.move_up"
                ),
            ),
            EditorCommand(
                "component.move_down",
                lambda context: _invoke_panel_command(
                    context, "component.move_down"
                ),
                display_name="Move Component Down",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.move_down"
                ),
            ),
            EditorCommand(
                "component.reorder",
                lambda context: _invoke_panel_command(
                    context, "component.reorder"
                ),
                display_name="Reorder Components",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.reorder"
                ),
            ),
            EditorCommand(
                "component.add",
                lambda context: _invoke_target_panel_command(
                    context, "inspector", "component.add"
                ),
                display_name="Add Component",
                category="Component",
                can_execute=lambda context: _can_target_panel_command(
                    context, "inspector", "component.add"
                ),
            ),
            EditorCommand(
                "component.set_enabled",
                lambda context: _invoke_panel_command(
                    context, "component.set_enabled"
                ),
                display_name="Set Component Enabled",
                category="Component",
                can_execute=lambda context: _can_panel_command(
                    context, "component.set_enabled"
                ),
            ),
            EditorCommand(
                "prefab.save_as",
                lambda context: _execute_prefab_command(context, "save_as"),
                display_name="Save As Prefab",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "save_as"),
            ),
            EditorCommand(
                "prefab.select_asset",
                lambda context: _execute_prefab_command(context, "select"),
                display_name="Select Prefab Asset",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "select"),
            ),
            EditorCommand(
                "prefab.open",
                lambda context: _execute_prefab_command(context, "open"),
                display_name="Open Prefab",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "open"),
            ),
            EditorCommand(
                "prefab.apply",
                lambda context: _execute_prefab_command(context, "apply"),
                display_name="Apply Prefab Overrides",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "apply"),
            ),
            EditorCommand(
                "prefab.revert",
                lambda context: _execute_prefab_command(context, "revert"),
                display_name="Revert Prefab Overrides",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "revert"),
            ),
            EditorCommand(
                "prefab.unpack",
                lambda context: _execute_prefab_command(context, "unpack"),
                display_name="Unpack Prefab",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "unpack"),
            ),
            EditorCommand(
                "prefab.exit",
                lambda context: _execute_prefab_command(context, "exit"),
                display_name="Exit Prefab Mode",
                category="Prefab",
                can_execute=lambda context: _can_prefab_command(context, "exit"),
            ),
            EditorCommand(
                "project.create_folder",
                _create_project_folder,
                display_name="Create Folder",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "project.create_folder"
                ),
                default_shortcut="Ctrl+Shift+N",
            ),
            EditorCommand(
                "asset.create",
                lambda context: _invoke_target_panel_command(
                    context, "project", "asset.create"
                ),
                display_name="Create Asset",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "asset.create"
                ),
            ),
            EditorCommand(
                "asset.open",
                lambda context: _invoke_target_panel_command(
                    context, "project", "asset.open"
                ),
                display_name="Open Asset",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "asset.open"
                ),
            ),
            EditorCommand(
                "asset.rename",
                _rename_project_asset,
                display_name="Rename Asset",
                category="Assets",
                can_execute=_can_rename_project_asset,
            ),
            EditorCommand(
                "asset.import_external",
                _import_external_assets,
                display_name="Import External Assets",
                category="Assets",
                can_execute=_can_import_external_assets,
            ),
            EditorCommand(
                "asset.transfer",
                lambda context: _invoke_target_panel_command(
                    context, "project", "asset.transfer"
                ),
                display_name="Move Assets",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "asset.transfer"
                ),
            ),
            EditorCommand(
                "project.reveal_in_explorer",
                lambda context: _invoke_target_panel_command(
                    context, "project", "project.reveal_in_explorer"
                ),
                display_name="Reveal in File Explorer",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "project.reveal_in_explorer"
                ),
            ),
            EditorCommand(
                "project.navigate_directory",
                lambda context: _invoke_target_panel_command(
                    context, "project", "project.navigate_directory"
                ),
                display_name="Navigate Project",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "project.navigate_directory"
                ),
            ),
            EditorCommand(
                "project.locate_asset",
                lambda context: _invoke_target_panel_command(
                    context, "project", "project.locate_asset"
                ),
                display_name="Locate Asset",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "project.locate_asset"
                ),
            ),
            EditorCommand(
                "project.set_folder_expanded",
                lambda context: _invoke_target_panel_command(
                    context, "project", "project.set_folder_expanded"
                ),
                display_name="Set Project Folder Expanded",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "project.set_folder_expanded"
                ),
            ),
            EditorCommand(
                "project.set_model_expanded",
                lambda context: _invoke_target_panel_command(
                    context, "project", "project.set_model_expanded"
                ),
                display_name="Set Model Contents Expanded",
                category="Assets",
                can_execute=lambda context: _can_target_panel_command(
                    context, "project", "project.set_model_expanded"
                ),
            ),
            EditorCommand(
                "project.open_document",
                _open_project_document,
                display_name="Open Asset",
                category="Assets",
                can_execute=lambda context: bool(
                    context.payload.get("path")
                    and context.payload.get("document_kind")
                ),
            ),
            EditorCommand(
                "timeline.new",
                lambda context: _invoke_panel_command(context, "timeline.new"),
                display_name="New Timeline",
                category="Timeline",
                can_execute=lambda context: _can_panel_command(
                    context, "timeline.new"
                ),
            ),
            EditorCommand(
                "animclip2d.new",
                lambda context: _invoke_panel_command(context, "animclip2d.new"),
                display_name="New 2D Animation Clip",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.new"
                ),
            ),
            EditorCommand(
                "animclip2d.play_pause",
                lambda context: _invoke_panel_command(
                    context, "animclip2d.play_pause"
                ),
                display_name="Play / Pause 2D Animation Preview",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.play_pause"
                ),
                default_shortcut="Space",
            ),
            EditorCommand(
                "animclip2d.stop",
                lambda context: _invoke_panel_command(context, "animclip2d.stop"),
                display_name="Stop 2D Animation Preview",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.stop"
                ),
            ),
            EditorCommand(
                "animclip2d.previous_frame",
                lambda context: _invoke_panel_command(
                    context, "animclip2d.previous_frame"
                ),
                display_name="Previous 2D Animation Frame",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.previous_frame"
                ),
            ),
            EditorCommand(
                "animclip2d.next_frame",
                lambda context: _invoke_panel_command(
                    context, "animclip2d.next_frame"
                ),
                display_name="Next 2D Animation Frame",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.next_frame"
                ),
            ),
            EditorCommand(
                "animclip2d.clear_sequence",
                lambda context: _invoke_panel_command(
                    context, "animclip2d.clear_sequence"
                ),
                display_name="Clear 2D Animation Sequence",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.clear_sequence"
                ),
            ),
            EditorCommand(
                "animclip2d.add_frame",
                lambda context: _invoke_panel_command(
                    context, "animclip2d.add_frame"
                ),
                display_name="Add 2D Animation Frame",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animclip2d.add_frame"
                ),
            ),
            EditorCommand(
                "animfsm.new",
                lambda context: _invoke_panel_command(context, "animfsm.new"),
                display_name="New Animation State Machine",
                category="Animation",
                can_execute=lambda context: _can_panel_command(
                    context, "animfsm.new"
                ),
            ),
            EditorCommand(
                "build.start",
                lambda context: _invoke_panel_command(context, "build.start"),
                display_name="Build Player",
                category="Build",
                can_execute=lambda context: _can_panel_command(
                    context, "build.start"
                ),
            ),
            EditorCommand(
                "build.start_and_run",
                lambda context: _invoke_panel_command(
                    context, "build.start_and_run"
                ),
                display_name="Build and Run Player",
                category="Build",
                can_execute=lambda context: _can_panel_command(
                    context, "build.start_and_run"
                ),
            ),
            EditorCommand(
                "build.cancel",
                lambda context: _invoke_panel_command(context, "build.cancel"),
                display_name="Cancel Build",
                category="Build",
                can_execute=lambda context: _can_panel_command(
                    context, "build.cancel"
                ),
            ),
            EditorCommand(
                "timeline.play_pause",
                lambda context: _invoke_panel_command(
                    context, "timeline.play_pause"
                ),
                display_name="Play / Pause Timeline",
                category="Timeline",
                can_execute=lambda context: _can_panel_command(
                    context, "timeline.play_pause"
                ),
                default_shortcut="Space",
            ),
            EditorCommand(
                "timeline.stop",
                lambda context: _invoke_panel_command(context, "timeline.stop"),
                display_name="Stop Timeline",
                category="Timeline",
                can_execute=lambda context: _can_panel_command(
                    context, "timeline.stop"
                ),
            ),
            EditorCommand(
                "timeline.set_loop_preview",
                lambda context: _invoke_panel_command(
                    context, "timeline.set_loop_preview"
                ),
                display_name="Toggle Timeline Loop Preview",
                category="Timeline",
                can_execute=lambda context: _can_panel_command(
                    context, "timeline.set_loop_preview"
                ),
                palette_visible=False,
            ),
            EditorCommand(
                "timeline.add_keyframe",
                lambda context: _invoke_panel_command(
                    context, "timeline.add_keyframe"
                ),
                display_name="Add Timeline Keyframe",
                category="Timeline",
                can_execute=lambda context: _can_panel_command(
                    context, "timeline.add_keyframe"
                ),
            ),
            EditorCommand(
                "scene.create_object",
                lambda context: _invoke_panel_command(
                    context, "scene.create_object"
                ),
                display_name="Create GameObject",
                category="GameObject",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.create_object"
                ),
            ),
            EditorCommand(
                "scene.create_empty_parent",
                lambda context: _invoke_panel_command(
                    context, "scene.create_empty_parent"
                ),
                display_name="Create Empty Parent",
                category="GameObject",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.create_empty_parent"
                ),
            ),
            EditorCommand(
                "scene.instantiate_prefab",
                lambda context: _invoke_target_panel_command(
                    context, "hierarchy", "scene.instantiate_prefab"
                ),
                display_name="Instantiate Prefab",
                category="GameObject",
                can_execute=lambda context: _can_target_panel_command(
                    context, "hierarchy", "scene.instantiate_prefab"
                ),
            ),
            EditorCommand(
                "scene.create_model",
                lambda context: _invoke_target_panel_command(
                    context, "hierarchy", "scene.create_model"
                ),
                display_name="Create Model",
                category="GameObject",
                can_execute=lambda context: _can_target_panel_command(
                    context, "hierarchy", "scene.create_model"
                ),
            ),
            EditorCommand(
                "scene.rename_object",
                lambda context: _invoke_target_panel_command(
                    context, "hierarchy", "scene.rename_object"
                ),
                display_name="Rename GameObject",
                category="GameObject",
                can_execute=lambda context: _can_target_panel_command(
                    context, "hierarchy", "scene.rename_object"
                ),
            ),
            EditorCommand(
                "scene.set_object_property",
                _set_object_property,
                display_name="Set GameObject Property",
                category="GameObject",
                can_execute=_can_set_object_property,
            ),
            EditorCommand(
                "scene.set_transforms",
                _set_transforms,
                display_name="Edit Transform",
                category="GameObject",
                can_execute=_can_set_transforms,
            ),
            EditorCommand(
                "scene.move_hierarchy",
                lambda context: _invoke_target_panel_command(
                    context, "hierarchy", "scene.move_hierarchy"
                ),
                display_name="Move GameObject",
                category="GameObject",
                can_execute=lambda context: _can_target_panel_command(
                    context, "hierarchy", "scene.move_hierarchy"
                ),
            ),
            EditorCommand(
                "scene.tool.select",
                lambda context: _invoke_panel_command(
                    context, "scene.tool.select"
                ),
                display_name="Select Tool",
                category="Scene",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.tool.select"
                ),
                default_shortcut="Q",
            ),
            EditorCommand(
                "ui.nudge.left",
                lambda context: _invoke_panel_command(context, "ui.nudge.left"),
                display_name="Nudge UI Element Left",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.left"
                ),
                default_shortcut="Left",
            ),
            EditorCommand(
                "ui.nudge.right",
                lambda context: _invoke_panel_command(context, "ui.nudge.right"),
                display_name="Nudge UI Element Right",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.right"
                ),
                default_shortcut="Right",
            ),
            EditorCommand(
                "ui.nudge.up",
                lambda context: _invoke_panel_command(context, "ui.nudge.up"),
                display_name="Nudge UI Element Up",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.up"
                ),
                default_shortcut="Up",
            ),
            EditorCommand(
                "ui.nudge.down",
                lambda context: _invoke_panel_command(context, "ui.nudge.down"),
                display_name="Nudge UI Element Down",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.down"
                ),
                default_shortcut="Down",
            ),
            EditorCommand(
                "ui.nudge.left.fast",
                lambda context: _invoke_panel_command(
                    context, "ui.nudge.left.fast"
                ),
                display_name="Nudge UI Element Left (Fast)",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.left.fast"
                ),
                default_shortcut="Shift+Left",
            ),
            EditorCommand(
                "ui.nudge.right.fast",
                lambda context: _invoke_panel_command(
                    context, "ui.nudge.right.fast"
                ),
                display_name="Nudge UI Element Right (Fast)",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.right.fast"
                ),
                default_shortcut="Shift+Right",
            ),
            EditorCommand(
                "ui.nudge.up.fast",
                lambda context: _invoke_panel_command(
                    context, "ui.nudge.up.fast"
                ),
                display_name="Nudge UI Element Up (Fast)",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.up.fast"
                ),
                default_shortcut="Shift+Up",
            ),
            EditorCommand(
                "ui.nudge.down.fast",
                lambda context: _invoke_panel_command(
                    context, "ui.nudge.down.fast"
                ),
                display_name="Nudge UI Element Down (Fast)",
                category="UI Editor",
                can_execute=lambda context: _can_panel_command(
                    context, "ui.nudge.down.fast"
                ),
                default_shortcut="Shift+Down",
            ),
            EditorCommand(
                "scene.tool.move",
                lambda context: _invoke_panel_command(context, "scene.tool.move"),
                display_name="Move Tool",
                category="Scene",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.tool.move"
                ),
                default_shortcut="W",
            ),
            EditorCommand(
                "scene.tool.rotate",
                lambda context: _invoke_panel_command(
                    context, "scene.tool.rotate"
                ),
                display_name="Rotate Tool",
                category="Scene",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.tool.rotate"
                ),
                default_shortcut="E",
            ),
            EditorCommand(
                "scene.tool.scale",
                lambda context: _invoke_panel_command(
                    context, "scene.tool.scale"
                ),
                display_name="Scale Tool",
                category="Scene",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.tool.scale"
                ),
                default_shortcut="R",
            ),
            EditorCommand(
                "scene.align_to_camera",
                lambda context: _invoke_panel_command(
                    context, "scene.align_to_camera"
                ),
                display_name="Align With View",
                category="Scene",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.align_to_camera"
                ),
                default_shortcut="Ctrl+F",
            ),
            EditorCommand(
                "scene.frame_selected",
                lambda context: _invoke_target_panel_command(
                    context, "scene_view", "scene.frame_selected"
                ),
                display_name="Frame Selected",
                category="Scene",
                can_execute=lambda context: _can_target_panel_command(
                    context, "scene_view", "scene.frame_selected"
                ),
                default_shortcut="F",
            ),
            EditorCommand(
                "scene.toggle_grid",
                _toggle_scene_grid,
                display_name="Toggle Scene Grid",
                category="Scene",
                can_execute=lambda _context: _native_engine() is not None,
                is_checked=_is_scene_grid_visible,
            ),
            EditorCommand(
                "scene.set_coordinate_space",
                lambda context: _invoke_panel_command(
                    context, "scene.set_coordinate_space"
                ),
                display_name="Set Scene Coordinate Space",
                category="Scene",
                can_execute=lambda context: _can_panel_command(
                    context, "scene.set_coordinate_space"
                ),
            ),
            EditorCommand(
                "play.toggle",
                _toggle_play,
                display_name="Play / Stop",
                category="Play",
                can_execute=lambda _context: pmm is not None,
            ),
            EditorCommand(
                "play.pause",
                _pause,
                display_name="Pause / Resume",
                category="Play",
                can_execute=lambda _context: bool(pmm and pmm.is_playing),
            ),
            EditorCommand(
                "play.step",
                _step,
                display_name="Step",
                category="Play",
                can_execute=lambda _context: bool(pmm and pmm.is_paused),
            ),
            EditorCommand(
                "window.open",
                _open_window,
                display_name="Open Window",
                category="Window",
                can_execute=lambda context: _window_target(context)
                in wm.get_registered_types(),
                is_checked=lambda context: bool(
                    _window_target(context)
                    and wm.is_window_open(_window_target(context))
                ),
            ),
            EditorCommand(
                "window.toggle",
                _toggle_window,
                display_name="Toggle Window",
                category="Window",
                can_execute=lambda context: _window_target(context)
                in wm.get_registered_types()
                and _window_target(context)
                not in PERMANENT_EDITOR_WINDOW_TYPE_IDS,
                is_checked=lambda context: bool(
                    _window_target(context)
                    and wm.is_window_open(_window_target(context))
                ),
            ),
            EditorCommand(
                "window.reset_layout",
                _reset_layout,
                display_name="Reset Layout",
                category="Window",
            ),
            EditorCommand(
                "console.open_entry",
                _open_console_entry,
                display_name="Open Console Entry",
                category="Window",
                can_execute=lambda context: str(
                    context.payload.get("uid", "")
                ).strip().isdigit(),
            ),
            EditorCommand(
                "console.open_source",
                _open_console_source,
                display_name="Open Console Source",
                category="Console",
                can_execute=lambda context: bool(_console_source_path(context)),
            ),
            EditorCommand(
                "console.clear",
                lambda context: _invoke_target_panel_command(
                    context, "console", "console.clear"
                ),
                display_name="Clear Console",
                category="Console",
                can_execute=lambda context: _can_target_panel_command(
                    context, "console", "console.clear"
                ),
            ),
            EditorCommand(
                "console.set_option",
                lambda context: _invoke_target_panel_command(
                    context, "console", "console.set_option"
                ),
                display_name="Set Console Option",
                category="Console",
                can_execute=lambda context: _can_target_panel_command(
                    context, "console", "console.set_option"
                ),
            ),
            EditorCommand(
                "console.set_search",
                lambda context: _invoke_target_panel_command(
                    context, "console", "console.set_search"
                ),
                display_name="Search Console",
                category="Console",
                can_execute=lambda context: _can_target_panel_command(
                    context, "console", "console.set_search"
                ),
            ),
            EditorCommand(
                "console.set_detail_height",
                lambda context: _invoke_target_panel_command(
                    context, "console", "console.set_detail_height"
                ),
                display_name="Resize Console Details",
                category="Console",
                can_execute=lambda context: _can_target_panel_command(
                    context, "console", "console.set_detail_height"
                ),
            ),
        )
        for command in commands:
            registry.register(command, replace=True)

        # ObjectField context actions are ordinary global commands.  The
        # popup freezes its field model in the command payload, so all asset
        # drawers share one enablement and execution contract.
        from Infernux.engine.interaction import register_asset_reference_commands

        register_asset_reference_commands(registry)

        bindings = (
            ("file.new_scene", "Ctrl+N", "default.file.new_scene"),
            ("file.save", "Ctrl+S", "default.file.save"),
            ("file.save_as", "Ctrl+Shift+S", "default.file.save_as"),
            ("edit.undo", "Ctrl+Z", "default.edit.undo"),
            ("edit.redo", "Ctrl+Shift+Z", "default.edit.redo.shift"),
            ("edit.redo", "Ctrl+Y", "default.edit.redo.y"),
            (
                "command_palette.open",
                "Ctrl+Shift+P",
                "default.command_palette.open",
            ),
        )
        default_shortcuts = [
            ShortcutBinding(command_id, KeyChord.parse(chord), binding_id=binding_id)
            for command_id, chord, binding_id in bindings
        ]
        default_shortcuts.append(
            ShortcutBinding(
                "interaction.cancel",
                KeyChord.parse("Escape"),
                ShortcutScope.GLOBAL,
                priority=10_000,
                allow_when_text_input=True,
                allow_when_modal=True,
                allow_when_captured=True,
                binding_id="default.interaction.cancel",
            )
        )
        default_shortcuts.extend(
            (
                ShortcutBinding(
                    "command_palette.previous",
                    KeyChord.parse("Up"),
                    ShortcutScope.CHILD_CONTEXT,
                    owner_id="command_palette",
                    priority=10_000,
                    allow_when_text_input=True,
                    allow_when_modal=True,
                    binding_id="default.command_palette.previous",
                ),
                ShortcutBinding(
                    "command_palette.next",
                    KeyChord.parse("Down"),
                    ShortcutScope.CHILD_CONTEXT,
                    owner_id="command_palette",
                    priority=10_000,
                    allow_when_text_input=True,
                    allow_when_modal=True,
                    binding_id="default.command_palette.next",
                ),
                ShortcutBinding(
                    "command_palette.execute",
                    KeyChord.parse("Enter"),
                    ShortcutScope.CHILD_CONTEXT,
                    owner_id="command_palette",
                    priority=10_000,
                    allow_when_text_input=True,
                    allow_when_modal=True,
                    binding_id="default.command_palette.execute",
                ),
            )
        )
        default_shortcuts.extend(panel_interactions.iter_shortcut_bindings())
        self.interaction_core.preferences.bind_shortcuts(
            default_shortcuts,
            shortcuts,
        )

    def _wire_menu_bar_callbacks(self, wm):
        """Wire C++ MenuBarPanel callbacks to Python managers."""
        mb = self.menu_bar
        sfm = self.scene_file_manager
        engine = self.engine

        self._register_core_editor_commands(wm, sfm)
        command_registry = self.interaction_core.commands
        shortcut_router = self.interaction_core.shortcuts
        from Infernux.engine.interaction import (
            CommandSource,
            EditorCommand,
            KeyChord,
            ShortcutEvent,
        )

        def _payload(argument):
            target_id = str(argument or "").strip()
            return {"target_id": target_id} if target_id else {}

        mb.execute_command = lambda command_id, source, argument: (
            command_registry.execute(
                command_id,
                source=CommandSource(source),
                payload=_payload(argument),
            ).accepted
        )
        mb.can_execute_command = lambda command_id, argument: (
            command_registry.can_execute(
                command_id,
                command_registry.context(CommandSource.MENU, _payload(argument)),
            )
        )
        mb.is_command_checked = lambda command_id, argument: (
            command_registry.is_checked(
                command_id,
                command_registry.context(CommandSource.MENU, _payload(argument)),
            )
        )
        self.shortcut_input.route_shortcut = lambda chord, text_input, modal: shortcut_router.route(
            ShortcutEvent(
                KeyChord.parse(chord),
                text_input_active=bool(text_input),
                modal_active=bool(modal),
            )
        ).consumed

        # Scene file operations
        if sfm:
            mb.on_request_close = lambda: sfm.request_close()

        # Window management
        from Infernux.lib import WindowTypeInfo
        def _get_registered_types():
            types = wm.get_registered_types()
            result = []
            seen = set()
            for type_id, info in types.items():
                wti = WindowTypeInfo()
                wti.type_id = type_id
                wti.display_name = info.display_name
                wti.menu_path = info.menu_path
                wti.singleton = info.singleton
                result.append(wti)
                seen.add(type_id)

            # Safety net: keep core editor panels discoverable from Window menu
            # even if registration order/state gets out of sync.
            essential = {
                "inspector": "Inspector",
                "project": "Project",
            }
            for type_id, label in essential.items():
                if type_id in seen:
                    continue
                wti = WindowTypeInfo()
                wti.type_id = type_id
                wti.display_name = label
                wti.menu_path = "Window"
                wti.singleton = True
                result.append(wti)
            return result
        mb.get_registered_types = _get_registered_types
        wm.add_type_change_listener(mb.invalidate_window_type_cache)
        mb.invalidate_window_type_cache()

        # Close request from C++ engine
        native = engine.get_native_engine() if engine else None
        if native:
            mb.is_close_requested = lambda: native.is_close_requested()

        # Utility settings are normal WindowManager-owned panel surfaces. The
        # project menu only dispatches commands; it never owns their instances
        # or renders a parallel panel list.
        for command_id, display_name, panel_id in (
            ("window.toggle.build_settings", "Build Settings", "build_settings"),
            ("window.toggle.preferences", "Preferences", "preferences"),
            ("window.toggle.history", "History", "history"),
            (
                "window.toggle.physics_layers",
                "Physics Layer Matrix",
                "physics_settings",
            ),
            (
                "window.toggle.environment",
                "Environment Settings",
                "environment_settings",
            ),
        ):
            command_registry.register(
                EditorCommand(
                    command_id,
                    lambda _context, target=panel_id: _toggle_window_target(target),
                    display_name=display_name,
                    category="Window",
                    is_checked=lambda _context, target=panel_id: wm.is_window_open(target),
                ),
                replace=True,
            )

        # Global confirmations are registered at overlay priority so
        # dynamically-created or undocked editors can never cover them.
        from Infernux.lib import InxGUIRenderable, InxGUIContext
        from Infernux.engine.ui.dirty_panel_confirmation import (
            DirtyPanelConfirmationCoordinator,
        )
        from Infernux.engine.ui.project_delete_confirmation import (
            ProjectDeleteConfirmationCoordinator,
        )
        from Infernux.engine.ui.external_document_conflict import (
            ExternalDocumentConflictCoordinator,
        )
        _dirty_panels = DirtyPanelConfirmationCoordinator.instance()
        _project_delete = ProjectDeleteConfirmationCoordinator.instance()
        _external_conflicts = ExternalDocumentConflictCoordinator.instance()
        from Infernux.engine.ui.command_palette import CommandPalettePresenter

        self._command_palette_presenter = CommandPalettePresenter(
            self.interaction_core.command_palette,
            self.interaction_core.modals,
        )
        from Infernux.engine.ui.modal_portal import ModalPortal

        _modal_portal = ModalPortal(self.interaction_core.modals)

        class _EditorGlobalOverlays(InxGUIRenderable):
            def on_render(self, ctx: InxGUIContext):
                _external_conflicts.poll()
                _modal_portal.on_render(ctx)

        self._editor_global_overlays = _EditorGlobalOverlays()
        engine.register_gui(
            "editor_global_overlays",
            self._editor_global_overlays,
            priority=1000,
        )

    def _wire_toolbar_callbacks(self, engine):
        """Wire C++ ToolbarPanel callbacks to Python PlayModeManager."""
        self._wire_toolbar_callbacks_on(self.toolbar, engine)

    def _wire_status_bar_listener(self):
        """Wire the native status bar to the shared EngineStatus state."""
        sb = self.status_bar
        from Infernux.engine.i18n import t as _t

        # Fold status expiry/synchronization into the existing Python frame tick.
        # A separate Python ImGui renderable would add another C++/Python virtual
        # dispatch every frame even though status text changes rarely.
        from Infernux.engine.ui.engine_status import EngineStatus

        last_status = [None]
        last_hierarchy_header = [None]

        def _sync_engine_status():
            state = EngineStatus.get()
            if state != last_status[0]:
                last_status[0] = state
                text, progress, kind = state
                sb.set_engine_status(text, progress, kind)

            sfm = self.scene_file_manager
            prefab_mode = bool(sfm and sfm.is_prefab_mode)
            scene_name = sfm.get_display_name() if sfm else ""
            prefab_name = (
                _t("hierarchy.prefab_mode_header").format(name=scene_name)
                if prefab_mode else ""
            )
            header_state = (scene_name, prefab_mode, prefab_name)
            if header_state != last_hierarchy_header[0]:
                last_hierarchy_header[0] = header_state
                self.hierarchy.set_scene_header_snapshot(*header_state)

        self.engine._editor_frame_sync_callback = _sync_engine_status
        _sync_engine_status()

    def _wire_hierarchy_callbacks(self):
        """Wire C++ HierarchyPanel callbacks to Python managers."""
        from Infernux.engine.bootstrap_hierarchy import wire_hierarchy_callbacks
        wire_hierarchy_callbacks(self)
        self._wire_native_transient_interactions(self.hierarchy, "hierarchy")

    def _wire_project_callbacks(self):
        """Wire C++ ProjectPanel callbacks to Python managers."""
        from Infernux.engine.bootstrap_project import wire_project_callbacks
        wire_project_callbacks(self)
        self._wire_native_transient_interactions(self.project_panel, "project")

    def _wire_inspector_callbacks(self):
        """Wire C++ InspectorPanel callbacks to Python managers."""
        from Infernux.engine.bootstrap_inspector import wire_inspector_callbacks
        wire_inspector_callbacks(self)
        self._wire_native_transient_interactions(self.inspector_panel, "inspector")

    def _wire_ui_editor(self):
        ui_editor = self.ui_editor
        hierarchy = self.hierarchy
        scene_view = self.scene_view
        game_view = self.game_view
        def on_ui_mode_request(enter: bool):
            hierarchy.set_ui_mode(enter)

        ui_editor.set_on_request_ui_mode(on_ui_mode_request)

        from Infernux.engine.interaction import SelectionService

        selection = SelectionService.instance()
        previous_service = getattr(self, "_ui_editor_selection_service", None)
        previous_listener = getattr(self, "_ui_editor_selection_listener", None)
        if previous_service is not None and callable(previous_listener):
            previous_service.remove_listener(previous_listener)

        def on_global_selection_changed(change):
            self._project_ui_editor_selection(change.after)

        self._ui_editor_selection_service = selection
        self._ui_editor_selection_listener = on_global_selection_changed
        selection.add_listener(on_global_selection_changed)
        self._project_ui_editor_selection(selection.snapshot)

    def _project_ui_editor_selection(self, snapshot) -> None:
        from Infernux.engine.interaction import SelectionDomain

        primary = snapshot.primary
        object_id = 0
        if primary is not None:
            if primary.domain is SelectionDomain.SCENE_OBJECT:
                object_id = primary.scene_object_id()
            elif primary.domain is SelectionDomain.COMPONENT:
                object_id = primary.component_ids()[0]

        obj = None
        if object_id:
            from Infernux.lib import SceneManager

            scene = SceneManager.instance().get_active_scene()
            obj = scene.find_by_id(object_id) if scene else None
        self.ui_editor.project_global_selection(obj)

