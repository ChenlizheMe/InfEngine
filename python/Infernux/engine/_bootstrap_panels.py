"""BootstrapPanelsMixin — extracted from EditorBootstrap."""
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
    AnimClip2DEditorPanel,
    AnimFSMEditorPanel,
    AnimTimelineEditorPanel,
    ParticleGraphEditorPanel,
    EditorPanel,
    EditorServices,
    EditorEventBus,
    EditorEvent,
    PanelRegistry,
    editor_panel,
)
from Infernux.engine.ui import panel_state as _panel_state
from Infernux.engine._bootstrap_trace import bootstrap_checkpoint


class BootstrapPanelsMixin:
    """BootstrapPanelsMixin method group for EditorBootstrap."""

    def _register_window_types(self):
        """Register all @editor_panel-decorated panels with WindowManager.

        Panels that require constructor arguments have their factory
        overridden here before apply_all flushes them into the
        WindowManager.
        """
        wm = self.window_manager
        engine = self.engine
        project_path = self.project_path

        from Infernux.lib import InspectorPanel as NativeInspectorPanel

        wm.register_window_type(
            type_id="inspector",
            window_class=NativeInspectorPanel,
            display_name="Inspector",
            factory=self._create_native_inspector,
            singleton=True,
            title_key="panel.inspector",
        )

        from Infernux.lib import ProjectPanel as NativeProjectPanel

        wm.register_window_type(
            type_id="project",
            window_class=NativeProjectPanel,
            display_name="Project",
            factory=self._create_native_project_panel,
            singleton=True,
            title_key="panel.project",
        )

        from Infernux.lib import ConsolePanel as NativeConsolePanel

        wm.register_window_type(
            type_id="console",
            window_class=NativeConsolePanel,
            display_name="Console",
            factory=self._create_native_console,
            singleton=True,
            title_key="panel.console",
        )

        # Override factories for panels that need runtime dependencies.
        # Panels with no-arg constructors use the default factory (cls()).
        _factories = {
            "scene_view":         lambda: SceneViewPanel(engine=engine),
            "game_view":          lambda: GameViewPanel(engine=engine),
            "project":            lambda: self._create_native_project_panel(),
            "toolbar":            lambda: self._create_native_toolbar(engine),
            "console":            lambda: self._create_native_console(),
            "hierarchy":          lambda: self._create_native_hierarchy(),
            "tag_layer_settings": lambda: self._create_tag_layer_panel(),
        }
        for reg in PanelRegistry.get_registrations():
            if reg.type_id in _factories:
                reg.factory = _factories[reg.type_id]

        PanelRegistry.apply_all(wm)

    def _create_tag_layer_panel(self):
        panel = TagLayerSettingsPanel()
        panel.set_project_path(self.project_path)
        return panel

    def _create_native_inspector(self):
        """Create a fresh C++ InspectorPanel with all callbacks wired."""
        from Infernux.lib import InspectorPanel as NativeInspectorPanel
        ip = NativeInspectorPanel()
        old = self.inspector_panel
        self.inspector_panel = ip
        self._wire_inspector_callbacks()
        self.inspector_panel = old
        return ip

    def _create_native_project_panel(self):
        """Create a fresh C++ ProjectPanel with all callbacks wired."""
        from Infernux.lib import ProjectPanel as NativeProjectPanel
        pp = NativeProjectPanel()
        # Re-use the same wiring logic; temporarily swap so the method
        # wires the new panel, then restore.
        old = self.project_panel
        self.project_panel = pp
        self._wire_project_callbacks()
        self.project_panel = old
        return pp

    def _create_native_console(self):
        """Create a fresh C++ ConsolePanel for WindowManager re-open."""
        from Infernux.lib import ConsolePanel as NativeConsolePanel
        panel = NativeConsolePanel()
        panel.on_request_focus = lambda: self.window_manager.open_window("console")
        _project_path = self.project_path
        def _on_dbl(source_file, source_line):
            if not source_file:
                return
            from Infernux.engine.ui import project_utils
            project_utils.open_file_with_system(
                source_file, project_root=_project_path)
        panel.on_double_click_entry = _on_dbl
        return panel

    def _create_native_hierarchy(self):
        """Create a fresh C++ HierarchyPanel with all callbacks wired."""
        from Infernux.lib import HierarchyPanel as NativeHierarchyPanel
        hp = NativeHierarchyPanel()
        old = self.hierarchy
        self.hierarchy = hp
        self._wire_hierarchy_callbacks()
        self.hierarchy = old
        return hp

    def _create_native_toolbar(self, engine):
        """Create a fresh C++ ToolbarPanel with all callbacks wired."""
        from Infernux.lib import ToolbarPanel as NativeToolbarPanel
        from Infernux.engine.i18n import t as _t
        tb = NativeToolbarPanel()
        tb.translate = _t
        self._wire_toolbar_callbacks_on(tb, engine)
        return tb

    def _create_panels(self):
        engine = self.engine
        wm = self.window_manager

        # Menu bar (native C++ panel)
        from Infernux.lib import MenuBarPanel as NativeMenuBarPanel
        from Infernux.engine.i18n import t as _t
        self.menu_bar = NativeMenuBarPanel()
        self.menu_bar.translate = _t
        self._wire_menu_bar_callbacks(wm)
        engine.register_gui("menu_bar", self.menu_bar)

        # Toolbar (native C++ panel)
        from Infernux.lib import ToolbarPanel as NativeToolbarPanel, PlayState
        self.toolbar = NativeToolbarPanel()
        self.toolbar.translate = _t
        self._wire_toolbar_callbacks(engine)
        engine.register_gui("toolbar", self.toolbar)
        wm.register_existing_window("toolbar", self.toolbar, "toolbar")

        ts = _panel_state.get("toolbar")
        if ts:
            cam_settings = ts.get("camera_settings")
            if cam_settings:
                self.toolbar.set_camera_settings(cam_settings)

        # Hierarchy (native C++ panel)
        from Infernux.lib import HierarchyPanel as NativeHierarchyPanel
        self.hierarchy = NativeHierarchyPanel()
        self._wire_hierarchy_callbacks()
        engine.register_gui("hierarchy", self.hierarchy)
        wm.register_existing_window("hierarchy", self.hierarchy, "hierarchy")

        # Inspector (native C++ panel)
        from Infernux.lib import InspectorPanel as NativeInspectorPanel
        self.inspector_panel = NativeInspectorPanel()
        self._wire_inspector_callbacks()
        engine.register_gui("inspector", self.inspector_panel)
        wm.register_existing_window("inspector", self.inspector_panel, "inspector")

        # Project (native C++ panel)
        from Infernux.lib import ProjectPanel as NativeProjectPanel
        self.project_panel = NativeProjectPanel()
        self._wire_project_callbacks()
        engine.register_gui("project", self.project_panel)
        wm.register_existing_window("project", self.project_panel, "project")

        ps = _panel_state.get("project")
        if ps:
            path = ps.get("current_path", "")
            if path:
                self.project_panel.set_current_path(path)

        # Console (native C++ panel)
        from Infernux.lib import ConsolePanel as NativeConsolePanel
        from Infernux.debug import DebugConsole
        self.console = NativeConsolePanel()
        # Bridge Python Debug.log() → C++ ConsolePanel
        DebugConsole.instance().set_native_console(self.console)
        engine.register_gui("console", self.console)
        wm.register_existing_window("console", self.console, "console")
        self.console.on_request_focus = lambda: wm.open_window("console")

        cs = _panel_state.get("console")
        if cs:
            self.console.show_info = cs.get("show_info", True)
            self.console.show_warnings = cs.get("show_warnings", True)
            self.console.show_errors = cs.get("show_errors", True)
            self.console.collapse = cs.get("collapse", False)
            self.console.clear_on_play = cs.get("clear_on_play", True)
            self.console.error_pause = cs.get("error_pause", False)
            self.console.auto_scroll = cs.get("auto_scroll", True)

        # Wire play-mode clear-on-play
        if engine._play_mode_manager is not None:
            _native_console = self.console
            _play_mode_manager = engine._play_mode_manager
            def _on_play_clear(event):
                from Infernux.engine.play_mode import PlayModeState
                if event.new_state == PlayModeState.PLAYING and _native_console.clear_on_play:
                    _native_console.clear()
            engine._play_mode_manager.add_state_change_listener(_on_play_clear)

            def _on_console_error_pause():
                from Infernux.engine.play_mode import PlayModeState
                if _play_mode_manager.state == PlayModeState.PLAYING:
                    _play_mode_manager.pause()

            self.console.on_error_pause = _on_console_error_pause

        # Wire console double-click → open source file
        _console_project_path = self.project_path
        def _on_console_double_click(source_file, source_line):
            if not source_file:
                return
            from Infernux.engine.ui import project_utils
            project_utils.open_file_with_system(
                source_file, project_root=_console_project_path)
        self.console.on_double_click_entry = _on_console_double_click

        # Status bar (native C++ panel)
        from Infernux.lib import StatusBarPanel as NativeStatusBarPanel
        self.status_bar = NativeStatusBarPanel()
        self.status_bar.set_console_panel(self.console)
        self._wire_status_bar_listener()
        engine.register_gui("status_bar", self.status_bar)

        # Scene view
        self.scene_view = SceneViewPanel(engine=engine)
        self.scene_view.set_window_manager(wm)
        if engine._play_mode_manager is not None:
            self.scene_view.set_play_mode_manager(engine._play_mode_manager)
        engine.register_gui("scene_view", self.scene_view)
        wm.register_existing_window("scene_view", self.scene_view, "scene_view")

        # Game view
        self.game_view = GameViewPanel(engine=engine)
        self.game_view.set_window_manager(wm)
        engine.register_gui("game_view", self.game_view)
        wm.register_existing_window("game_view", self.game_view, "game_view")

        # UI Editor
        self.ui_editor = UIEditorPanel()
        self.ui_editor.set_window_manager(wm)
        self.ui_editor.set_hierarchy_panel(self.hierarchy)
        self.ui_editor.set_engine(engine)
        engine.register_gui("ui_editor", self.ui_editor)
        wm.register_existing_window("ui_editor", self.ui_editor, "ui_editor")

        # During startup restore, suppress state-changed callbacks so we don't
        # overwrite persisted panel payloads with default/empty panel data.
        self._suspend_persist_state = True
        ws = _panel_state.get("window_manager")
        if ws:
            wm.load_state(ws)

        # Restore individual panel states for every tracked window id
        seen_restore: set[str] = set()
        for wid in set(wm._default_instances.keys()) | set(wm._window_instances.keys()):
            if wid in seen_restore:
                continue
            seen_restore.add(wid)
            inst = wm._window_instances.get(wid) or wm._default_instances.get(wid)
            if inst is None:
                continue
            if hasattr(inst, "load_state") and callable(inst.load_state):
                data = _panel_state.get(f"panel:{wid}")
                if data:
                    try:
                        inst.load_state(data)
                    except Exception:
                        pass

        self._suspend_persist_state = False
        self.project_panel.on_state_changed = self._persist_editor_state
        wm.set_on_state_changed(self._persist_editor_state)
        try:
            self.engine.set_before_exit_callback(
                lambda: self._persist_editor_state(include_scene_draft=True)
            )
        except Exception:
            pass

        self._persist_editor_state()

