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
    EditorEventBus,
    EditorEvent,
    PanelRegistry,
    editor_panel,
)
from Infernux.engine.ui import panel_state as _panel_state


class BootstrapWiringMixin:
    """BootstrapWiringMixin method group for EditorBootstrap."""

    @staticmethod
    def _save_focused_document(wm, sfm, *, save_as: bool = False) -> bool:
        from Infernux.engine.interaction import (
            DocumentActionStatus,
            DocumentRegistry,
            FocusService,
        )

        focus = FocusService.instance().snapshot
        if focus.active_document_id:
            document = DocumentRegistry.instance().get(focus.active_document_id)
            if document is not None:
                result = DocumentRegistry.instance().request_save(
                    document.document_id,
                    save_as=save_as,
                )
                if result.status in {
                    DocumentActionStatus.FAILED,
                    DocumentActionStatus.REJECTED,
                }:
                    from Infernux.debug import Debug

                    Debug.log_warning(
                        f"Could not save focused document '{document.title}': "
                        f"{result.message or result.status.value}"
                    )
                return result.accepted

        from Infernux.engine.ui.closable_panel import ClosablePanel

        panel_id = ClosablePanel.get_active_panel_id()
        panel = wm.get_window_instance(panel_id) if panel_id else None
        handler = getattr(panel, "handle_save_command", None)
        if callable(handler) and bool(handler(save_as=save_as)):
            return True
        if save_as:
            return bool(sfm.save_scene_as())
        return bool(sfm.save_current_scene())

    def _register_core_editor_commands(self, wm, sfm) -> None:
        from Infernux.engine.interaction import (
            EditorCommand,
            KeyChord,
            ShortcutBinding,
        )
        from Infernux.engine.undo import UndoManager
        from Infernux.engine.ui.closable_panel import ClosablePanel

        registry = self.interaction_core.commands
        shortcuts = self.interaction_core.shortcuts
        pmm = self.engine._play_mode_manager if self.engine else None

        def _undo(_context):
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
            ClosablePanel.focus_panel_by_id("game_view")
            if self.engine:
                self.engine.select_docked_window("game_view")
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

        def _window_target(context) -> str:
            return str(context.payload.get("target_id", "") or "").strip()

        def _open_window(context):
            target_id = _window_target(context)
            if not target_id:
                return False
            return wm.open_window(target_id) is not None

        def _reset_layout(_context):
            wm.reset_layout()
            return True

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
                lambda _context: self._save_focused_document(wm, sfm),
                display_name="Save",
                category="File",
                default_shortcut="Ctrl+S",
            ),
            EditorCommand(
                "file.save_as",
                lambda _context: self._save_focused_document(wm, sfm, save_as=True),
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
                    UndoManager.instance() and UndoManager.instance().can_undo
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
                "window.reset_layout",
                _reset_layout,
                display_name="Reset Layout",
                category="Window",
            ),
        )
        for command in commands:
            registry.register(command, replace=True)

        bindings = (
            ("file.new_scene", "Ctrl+N", "default.file.new_scene"),
            ("file.save", "Ctrl+S", "default.file.save"),
            ("file.save_as", "Ctrl+Shift+S", "default.file.save_as"),
            ("edit.undo", "Ctrl+Z", "default.edit.undo"),
            ("edit.redo", "Ctrl+Shift+Z", "default.edit.redo.shift"),
            ("edit.redo", "Ctrl+Y", "default.edit.redo.y"),
        )
        for command_id, chord, binding_id in bindings:
            shortcuts.register(
                ShortcutBinding(command_id, KeyChord.parse(chord), binding_id=binding_id),
                replace=True,
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
        mb.route_shortcut = lambda chord, text_input, modal: shortcut_router.route(
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

        # Floating sub-panels (still rendered from Python)
        from Infernux.engine.ui.build_settings_panel import BuildSettingsPanel
        from Infernux.engine.ui.preferences_panel import PreferencesPanel
        from Infernux.engine.ui.tag_layer_settings import PhysicsLayerMatrixPanel
        from Infernux.engine.ui.environment_settings_panel import EnvironmentSettingsPanel
        from Infernux.engine.project_context import get_project_root
        self._build_settings = BuildSettingsPanel()
        self._preferences = PreferencesPanel()
        self._physics_layer_matrix = PhysicsLayerMatrixPanel()
        self._physics_layer_matrix.set_project_path(get_project_root() or "")
        self._environment_settings = EnvironmentSettingsPanel()

        def _toggle_floating_panel(panel):
            panel.close() if panel.is_open else panel.open()
            return True

        for command_id, display_name, panel in (
            ("window.toggle.build_settings", "Build Settings", self._build_settings),
            ("window.toggle.preferences", "Preferences", self._preferences),
            (
                "window.toggle.physics_layers",
                "Physics Layer Matrix",
                self._physics_layer_matrix,
            ),
            (
                "window.toggle.environment",
                "Environment Settings",
                self._environment_settings,
            ),
        ):
            command_registry.register(
                EditorCommand(
                    command_id,
                    lambda _context, target=panel: _toggle_floating_panel(target),
                    display_name=display_name,
                    category="Window",
                    is_checked=lambda _context, target=panel: bool(target.is_open),
                ),
                replace=True,
            )

        # Floating utility windows participate in the normal panel layer.
        # Global confirmations are registered separately at overlay priority
        # so dynamically-created or undocked editors can never cover them.
        from Infernux.lib import InxGUIRenderable, InxGUIContext
        _bs = self._build_settings
        _pref = self._preferences
        _plm = self._physics_layer_matrix
        _env = self._environment_settings
        _sfm = sfm
        from Infernux.engine.ui.dirty_panel_confirmation import (
            DirtyPanelConfirmationCoordinator,
        )
        from Infernux.engine.ui.project_delete_confirmation import (
            ProjectDeleteConfirmationCoordinator,
        )
        _dirty_panels = DirtyPanelConfirmationCoordinator.instance()
        _project_delete = ProjectDeleteConfirmationCoordinator.instance()

        class _MenuBarFloatingPanels(InxGUIRenderable):
            def on_render(self, ctx: InxGUIContext):
                _bs.render(ctx)
                _pref.render(ctx)
                _plm.render(ctx)
                _env.render(ctx)

        class _EditorGlobalOverlays(InxGUIRenderable):
            def on_render(self, ctx: InxGUIContext):
                _dirty_panels.render(ctx)
                _project_delete.render(ctx)
                if _sfm:
                    _sfm.render_save_as_popup(ctx)

        self._menu_bar_floats = _MenuBarFloatingPanels()
        engine.register_gui("menu_bar_floats", self._menu_bar_floats)
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

    def _wire_project_callbacks(self):
        """Wire C++ ProjectPanel callbacks to Python managers."""
        from Infernux.engine.bootstrap_project import wire_project_callbacks
        wire_project_callbacks(self)

    def _wire_inspector_callbacks(self):
        """Wire C++ InspectorPanel callbacks to Python managers."""
        from Infernux.engine.bootstrap_inspector import wire_inspector_callbacks
        wire_inspector_callbacks(self)

    def _wire_ui_editor(self):
        ui_editor = self.ui_editor
        hierarchy = self.hierarchy
        scene_view = self.scene_view
        game_view = self.game_view
        from Infernux.engine.ui.event_bus import EditorEvent, EditorEventBus

        def on_ui_mode_request(enter: bool):
            hierarchy.set_ui_mode(enter)

        ui_editor.set_on_request_ui_mode(on_ui_mode_request)

        def on_ui_editor_selected(go):
            if go is not None:
                hierarchy.set_selected_object_by_id(go.id)
            else:
                hierarchy.clear_selection_and_notify()

        ui_editor.set_on_selection_changed(on_ui_editor_selected)

        def on_hierarchy_ui_sync(oid):
            """C++ sends uint64_t; resolve to object for UIEditorPanel."""
            obj = None
            if oid:
                from Infernux.lib import SceneManager
                scene = SceneManager.instance().get_active_scene()
                obj = scene.find_by_id(oid) if scene else None
            ui_editor.notify_hierarchy_selection(obj)

        hierarchy.on_selection_changed_ui_editor = on_hierarchy_ui_sync

        def on_panel_focused(panel_id: str):
            if self.window_manager is not None:
                self.window_manager.note_panel_focus(panel_id)

        bus = EditorEventBus.instance()
        previous = getattr(self, "_panel_focus_event_handler", None)
        if previous is not None:
            bus.unsubscribe(EditorEvent.PANEL_FOCUSED, previous)
        self._panel_focus_event_handler = on_panel_focused
        bus.subscribe(EditorEvent.PANEL_FOCUSED, on_panel_focused)

