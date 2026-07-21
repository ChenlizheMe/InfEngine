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
    def _save_focused_document(wm, sfm, *, save_as: bool = False) -> None:
        from Infernux.engine.ui.closable_panel import ClosablePanel

        panel_id = ClosablePanel.get_active_panel_id()
        panel = wm.get_window_instance(panel_id) if panel_id else None
        handler = getattr(panel, "handle_save_command", None)
        if callable(handler) and bool(handler(save_as=save_as)):
            return
        if save_as:
            sfm.save_scene_as()
        else:
            sfm.save_current_scene()

    def _wire_menu_bar_callbacks(self, wm):
        """Wire C++ MenuBarPanel callbacks to Python managers."""
        mb = self.menu_bar
        sfm = self.scene_file_manager
        engine = self.engine

        # Scene file operations
        if sfm:
            mb.on_save = lambda: sfm.save_current_scene()
            mb.on_save_as = lambda: sfm.save_scene_as()
            mb.on_new_scene = lambda: sfm.new_scene()
            mb.on_request_close = lambda: sfm.request_close()

            mb.on_save_focused = lambda: self._save_focused_document(wm, sfm)
            mb.on_save_focused_as = lambda: self._save_focused_document(
                wm, sfm, save_as=True
            )

        # Undo
        def _undo():
            from Infernux.engine.undo import UndoManager
            mgr = UndoManager.instance()
            if mgr and mgr.can_undo:
                mgr.undo()
        def _redo():
            from Infernux.engine.undo import UndoManager
            mgr = UndoManager.instance()
            if mgr and mgr.can_redo:
                mgr.redo()
        def _can_undo():
            from Infernux.engine.undo import UndoManager
            mgr = UndoManager.instance()
            return bool(mgr and mgr.can_undo)
        def _can_redo():
            from Infernux.engine.undo import UndoManager
            mgr = UndoManager.instance()
            return bool(mgr and mgr.can_redo)

        mb.on_undo = _undo
        mb.on_redo = _redo
        mb.can_undo = _can_undo
        mb.can_redo = _can_redo

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
        def _get_open_windows():
            return wm.get_open_windows()

        mb.get_registered_types = _get_registered_types
        wm.add_type_change_listener(mb.invalidate_window_type_cache)
        mb.invalidate_window_type_cache()
        mb.get_open_windows = _get_open_windows
        mb.open_window = lambda tid: wm.open_window(tid)
        mb.close_window = lambda tid: wm.close_window(tid)
        mb.reset_layout = lambda: wm.reset_layout()

        # Close request from C++ engine
        native = engine.get_native_engine() if engine else None
        if native:
            mb.is_close_requested = lambda: native.is_close_requested()

        # Floating sub-panels (still rendered from Python)
        from Infernux.engine.ui.build_settings_panel import BuildSettingsPanel
        from Infernux.engine.ui.preferences_panel import PreferencesPanel
        from Infernux.engine.ui.tag_layer_settings import PhysicsLayerMatrixPanel
        from Infernux.engine.project_context import get_project_root
        self._build_settings = BuildSettingsPanel()
        self._preferences = PreferencesPanel()
        self._physics_layer_matrix = PhysicsLayerMatrixPanel()
        self._physics_layer_matrix.set_project_path(get_project_root() or "")

        mb.toggle_build_settings = lambda: (
            self._build_settings.close() if self._build_settings.is_open
            else self._build_settings.open()
        )
        mb.toggle_preferences = lambda: (
            self._preferences.close() if self._preferences.is_open
            else self._preferences.open()
        )
        mb.toggle_physics_layer_matrix = lambda: (
            self._physics_layer_matrix.close() if self._physics_layer_matrix.is_open
            else self._physics_layer_matrix.open()
        )
        mb.is_build_settings_open = lambda: self._build_settings.is_open
        mb.is_preferences_open = lambda: self._preferences.is_open
        mb.is_physics_layer_matrix_open = lambda: self._physics_layer_matrix.is_open

        # Register a secondary renderable that draws floating sub-panels and
        # asset/document confirmations after the menu bar.
        from Infernux.lib import InxGUIRenderable, InxGUIContext
        _bs = self._build_settings
        _pref = self._preferences
        _plm = self._physics_layer_matrix
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
                _dirty_panels.render(ctx)
                _project_delete.render(ctx)
                if _sfm:
                    _sfm.render_confirmation_popup(ctx)
                    _sfm.render_save_as_popup(ctx)

        self._menu_bar_floats = _MenuBarFloatingPanels()
        engine.register_gui("menu_bar_floats", self._menu_bar_floats)

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

