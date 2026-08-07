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
from dataclasses import dataclass
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
    PreferencesPanel,
    HistoryPanel,
    PhysicsLayerMatrixPanel,
    EnvironmentSettingsPanel,
    UIEditorPanel,
    AnimClip2DEditorPanel,
    AnimFSMEditorPanel,
    AnimTimelineEditorPanel,
    ParticleGraphEditorPanel,
    EditorPanel,
    EditorServices,
    PanelRegistry,
    editor_panel,
)
from Infernux.engine.ui import panel_state as _panel_state
from Infernux.engine._bootstrap_trace import bootstrap_checkpoint


@dataclass(frozen=True, slots=True)
class NativeBuiltinWindowType:
    """One native singleton governed by the formal editor window manifest."""

    type_id: str
    display_name: str
    title_key: str
    factory_name: str
    menu_path: str = "Window"
    user_closable: bool = True


NATIVE_BUILTIN_WINDOW_TYPES = (
    NativeBuiltinWindowType(
        "toolbar",
        "Toolbar",
        "panel.toolbar",
        "_create_native_toolbar",
        menu_path="",
        user_closable=False,
    ),
    NativeBuiltinWindowType(
        "hierarchy",
        "Hierarchy",
        "panel.hierarchy",
        "_create_native_hierarchy",
    ),
    NativeBuiltinWindowType(
        "inspector",
        "Inspector",
        "panel.inspector",
        "_create_native_inspector",
    ),
    NativeBuiltinWindowType(
        "project",
        "Project",
        "panel.project",
        "_create_native_project_panel",
    ),
    NativeBuiltinWindowType(
        "console",
        "Console",
        "panel.console",
        "_create_native_console",
    ),
)

PERMANENT_EDITOR_WINDOW_TYPE_IDS = frozenset(
    spec.type_id for spec in NATIVE_BUILTIN_WINDOW_TYPES if not spec.user_closable
)

BUILTIN_EDITOR_WINDOW_TYPE_IDS = frozenset(
    {
        *(spec.type_id for spec in NATIVE_BUILTIN_WINDOW_TYPES),
        "scene_view",
        "game_view",
        "ui_editor",
    }
)

PERMANENT_EDITOR_SURFACE_TYPE_IDS = frozenset({"menu_bar", "status_bar"})


def _native_builtin_type(spec: NativeBuiltinWindowType):
    from Infernux.lib import (
        ConsolePanel,
        HierarchyPanel,
        InspectorPanel,
        ProjectPanel,
        ToolbarPanel,
    )

    return {
        "toolbar": ToolbarPanel,
        "hierarchy": HierarchyPanel,
        "inspector": InspectorPanel,
        "project": ProjectPanel,
        "console": ConsolePanel,
    }[spec.type_id]


def register_native_builtin_window_types(bootstrap, window_manager) -> None:
    """Register every native singleton from one authoritative manifest."""
    for spec in NATIVE_BUILTIN_WINDOW_TYPES:
        factory = getattr(bootstrap, spec.factory_name)
        window_manager.register_window_type(
            type_id=spec.type_id,
            window_class=_native_builtin_type(spec),
            display_name=spec.display_name,
            factory=factory,
            singleton=True,
            title_key=spec.title_key,
            menu_path=spec.menu_path,
        )


def enforce_native_builtin_window_policy(type_id: str, instance: object) -> None:
    """Apply manifest policy that cannot be represented by WindowInfo yet."""
    spec = next(
        (item for item in NATIVE_BUILTIN_WINDOW_TYPES if item.type_id == type_id),
        None,
    )
    if spec is None:
        raise KeyError(f"unknown native builtin window type: {type_id}")
    if spec.user_closable:
        return
    if not hasattr(instance, "on_request_close"):
        raise TypeError(f"permanent editor window has no close-intent bridge: {type_id}")
    instance.on_request_close = lambda: False


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

        from Infernux.engine.interaction import SelectionDomain
        from Infernux.engine.ui.core_panel_interactions import (
            console_panel_interaction,
            hierarchy_panel_interaction,
            inspector_panel_interaction,
            project_panel_interaction,
            scene_view_panel_interaction,
            passive_editor_surface_interaction,
            toolbar_panel_interaction,
            ui_editor_panel_interaction,
        )

        panel_interactions = self.interaction_core.panels
        scene_commands = self.interaction_core.scene_objects
        from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

        panel_interactions.register_type(
            "hierarchy",
            hierarchy_panel_interaction(
                scene_commands,
                creation_service=HierarchyCreationService.instance(),
                tree_views=self.interaction_core.tree_views,
            ),
            replace=True,
        )
        panel_interactions.register_type(
            "project",
            project_panel_interaction(
                self.interaction_core.project_asset_interactions,
                self.interaction_core.navigation,
                self.interaction_core.tree_views,
            ),
            replace=True,
        )
        panel_interactions.register_type(
            "inspector",
            inspector_panel_interaction(
                lambda: getattr(self, "_inspector_component_actions", None)
            ),
            replace=True,
        )
        panel_interactions.register_type(
            "scene_view",
            scene_view_panel_interaction(scene_commands),
            replace=True,
        )
        panel_interactions.register_type(
            "ui_editor",
            ui_editor_panel_interaction(
                scene_commands,
                creation_service=HierarchyCreationService.instance(),
            ),
            replace=True,
        )
        panel_interactions.register_type(
            "console",
            console_panel_interaction(self.interaction_core.view_commands),
            replace=True,
        )
        panel_interactions.register_type(
            "toolbar",
            toolbar_panel_interaction(),
            replace=True,
        )
        for type_id in sorted(PERMANENT_EDITOR_SURFACE_TYPE_IDS):
            panel_interactions.register_type(
                type_id,
                passive_editor_surface_interaction(),
                replace=True,
            )

        register_native_builtin_window_types(self, wm)

        # Panels with runtime dependencies provide factories in one explicit
        # application step; the static registry remains immutable.
        _factories = {
            "scene_view":         lambda: SceneViewPanel(engine=engine),
            "game_view":          lambda: GameViewPanel(engine=engine),
            "console":            lambda: self._create_native_console(),
            "tag_layer_settings": lambda: self._create_tag_layer_panel(),
            "physics_settings":   lambda: self._create_physics_settings_panel(),
        }
        PanelRegistry.apply_all(
            wm,
            self.interaction_core.panels,
            factory_overrides=_factories,
        )
        panel_interactions.require_types(
            BUILTIN_EDITOR_WINDOW_TYPE_IDS | PERMANENT_EDITOR_SURFACE_TYPE_IDS
        )

    def _create_tag_layer_panel(self):
        panel = TagLayerSettingsPanel()
        panel.set_project_path(self.project_path)
        return panel

    def _create_physics_settings_panel(self):
        panel = PhysicsLayerMatrixPanel()
        panel.set_project_path(self.project_path)
        return panel

    def _wire_native_transient_interactions(self, panel, panel_id: str) -> None:
        """Project native temporary UI state into Interaction Core.

        Native panels own the concrete cancellation operation, while the
        global service owns ordering, focus context, and Escape routing.
        """
        owner_id = str(panel_id or "").strip()
        if not owner_id:
            raise ValueError("native transient panel requires a panel_id")
        service = self.interaction_core.transient_interactions
        prefix = f"native:{owner_id}:"

        def _begin(local_token, kind, priority):
            local = str(local_token or "").strip()
            if not local:
                return
            service.begin(
                owner_id,
                lambda _panel=panel, _local=local: _panel.cancel_transient(_local),
                kind=str(kind or "native_interaction"),
                priority=int(priority),
                token_id=prefix + local,
            )

        def _end(local_token):
            local = str(local_token or "").strip()
            if local:
                service.end(prefix + local)

        panel.on_transient_begin = _begin
        panel.on_transient_end = _end

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
        self._wire_console_command_callbacks(panel)
        panel.on_request_focus = lambda: self.window_manager.open_window("console")
        panel.on_selection_changed = self._on_console_selection_changed
        panel.on_panel_focused = self.window_manager.native_panel_focus_callback(
            "console",
            view_id="console",
            source_instance=panel,
        )
        self._wire_native_transient_interactions(panel, "console")
        return panel

    def _wire_console_command_callbacks(self, panel):
        from Infernux.engine.interaction import CommandSource

        commands = self.interaction_core.commands

        def _source(value):
            try:
                return CommandSource(value)
            except ValueError:
                return CommandSource.POINTER

        def _payload(command_id, argument):
            value = str(argument or "")
            if command_id == "console.open_source":
                source_path, separator, source_line = value.rpartition("\t")
                if not separator or not source_path:
                    return {}
                try:
                    line = max(int(source_line or 0), 0)
                except ValueError:
                    return {}
                return {"source_path": source_path, "source_line": line}
            if command_id == "console.set_option":
                option, separator, enabled = value.rpartition("\t")
                if not separator or enabled not in {"0", "1"}:
                    return {}
                return {"option": option, "enabled": enabled == "1"}
            if command_id == "console.set_search":
                old_value, separator, new_value = value.partition("\n")
                if not separator:
                    return {}
                return {"old_value": old_value, "new_value": new_value}
            if command_id == "console.set_detail_height":
                old_value, separator, new_value = value.partition("\t")
                if not separator:
                    return {}
                return {"old_value": old_value, "new_value": new_value}
            return {}

        panel.execute_command = lambda command_id, source, argument: (
            commands.execute(
                command_id,
                source=_source(source),
                payload=_payload(command_id, argument),
            ).accepted
        )
        panel.can_execute_command = lambda command_id, argument: (
            commands.can_execute(
                command_id,
                commands.context(
                    CommandSource.POINTER,
                    _payload(command_id, argument),
                ),
            )
        )

    @staticmethod
    def _on_console_selection_changed(uid, record_history):
        from Infernux.engine.interaction import (
            SelectionDomain,
            SelectionService,
            SelectionTarget,
        )

        selection = SelectionService.instance()
        entry_uid = int(uid or 0)
        if entry_uid > 0:
            selection.select(
                SelectionTarget.diagnostic_entry(
                    "console",
                    str(entry_uid),
                    sub_kind="log",
                ),
                owner_id="console",
                reason="console_select_entry",
                record_history=bool(record_history),
            )
            return
        if (
            selection.snapshot.domain is SelectionDomain.DIAGNOSTIC_ENTRY
            and selection.snapshot.owner_id == "console"
        ):
            selection.clear(
                reason="console_clear_selection",
                record_history=bool(record_history),
            )

    def _create_native_hierarchy(self):
        """Create a fresh C++ HierarchyPanel with all callbacks wired."""
        from Infernux.lib import HierarchyPanel as NativeHierarchyPanel
        hp = NativeHierarchyPanel()
        old = self.hierarchy
        self.hierarchy = hp
        self._wire_hierarchy_callbacks()
        self.hierarchy = old
        return hp

    def _create_native_toolbar(self):
        """Create a fresh C++ ToolbarPanel with all callbacks wired."""
        from Infernux.lib import ToolbarPanel as NativeToolbarPanel
        from Infernux.engine.i18n import t as _t
        tb = NativeToolbarPanel()
        tb.translate = _t
        self._wire_toolbar_callbacks_on(tb, self.engine)
        return tb

    def _create_panels(self):
        engine = self.engine
        wm = self.window_manager

        # A native adapter samples physical key edges; Interaction Core owns
        # routing and command semantics. The menu bar is presentation only.
        from Infernux.lib import (
            EditorShortcutInput as NativeEditorShortcutInput,
            MenuBarPanel as NativeMenuBarPanel,
        )
        from Infernux.engine.i18n import t as _t
        self.shortcut_input = NativeEditorShortcutInput()
        self.menu_bar = NativeMenuBarPanel()
        self.menu_bar.translate = _t
        self._wire_menu_bar_callbacks(wm)
        self.interaction_core.panels.bind_view(
            "menu_bar",
            "menu_bar",
            self.menu_bar,
        )
        engine.register_gui("editor_shortcut_input", self.shortcut_input, priority=-100)
        engine.register_gui("menu_bar", self.menu_bar)

        # Toolbar (native C++ panel)
        from Infernux.lib import ToolbarPanel as NativeToolbarPanel, PlayState
        self.toolbar = NativeToolbarPanel()
        wm.register_existing_window("toolbar", self.toolbar, "toolbar")
        enforce_native_builtin_window_policy("toolbar", self.toolbar)
        self.toolbar.translate = _t
        self._wire_toolbar_callbacks(engine)

        ts = _panel_state.get("toolbar")
        if ts:
            cam_settings = ts.get("camera_settings")
            if cam_settings:
                self.toolbar.set_camera_settings(cam_settings)

        # Hierarchy (native C++ panel)
        from Infernux.lib import HierarchyPanel as NativeHierarchyPanel
        self.hierarchy = NativeHierarchyPanel()
        self._wire_hierarchy_callbacks()
        wm.register_existing_window("hierarchy", self.hierarchy, "hierarchy")

        # Inspector (native C++ panel)
        from Infernux.lib import InspectorPanel as NativeInspectorPanel
        self.inspector_panel = NativeInspectorPanel()
        self._wire_inspector_callbacks()
        wm.register_existing_window("inspector", self.inspector_panel, "inspector")

        # Project (native C++ panel)
        from Infernux.lib import ProjectPanel as NativeProjectPanel
        self.project_panel = NativeProjectPanel()
        self._wire_project_callbacks()
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
        self._wire_console_command_callbacks(self.console)
        # Bridge Python Debug.log() → C++ ConsolePanel
        DebugConsole.instance().set_native_console(self.console)
        self.console.on_request_focus = lambda: wm.open_window("console")
        self.console.on_selection_changed = self._on_console_selection_changed
        self.console.on_panel_focused = wm.native_panel_focus_callback(
            "console",
            view_id="console",
            source_instance=self.console,
        )
        self._wire_native_transient_interactions(self.console, "console")
        wm.register_existing_window("console", self.console, "console")

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

        # Status bar (native C++ panel)
        from Infernux.lib import StatusBarPanel as NativeStatusBarPanel
        self.status_bar = NativeStatusBarPanel()
        self.status_bar.set_console_panel(self.console)
        from Infernux.engine.interaction import CommandSource

        self.status_bar.execute_command = lambda command_id, source, argument: (
            self.interaction_core.commands.execute(
                command_id,
                source=CommandSource(source),
                payload={"uid": argument},
            ).accepted
        )
        self._wire_status_bar_listener()
        self.interaction_core.panels.bind_view(
            "status_bar",
            "status_bar",
            self.status_bar,
        )
        engine.register_gui("status_bar", self.status_bar)

        # Scene view
        self.scene_view = SceneViewPanel(engine=engine)
        self.scene_view.set_window_manager(wm)
        if engine._play_mode_manager is not None:
            self.scene_view.set_play_mode_manager(engine._play_mode_manager)
        wm.register_existing_window("scene_view", self.scene_view, "scene_view")

        # Game view
        self.game_view = GameViewPanel(engine=engine)
        self.game_view.set_window_manager(wm)
        wm.register_existing_window("game_view", self.game_view, "game_view")

        # UI Editor
        self.ui_editor = UIEditorPanel()
        self.ui_editor.set_window_manager(wm)
        self.ui_editor.set_engine(engine)
        wm.register_existing_window("ui_editor", self.ui_editor, "ui_editor")

        # During startup restore, suppress state-changed callbacks so we don't
        # overwrite persisted panel payloads with default/empty panel data.
        self._suspend_persist_state = True
        from Infernux.engine.interaction import DocumentRegistry

        try:
            DocumentRegistry.instance().queue_session_restore(
                _panel_state.get("document_session")
            )
        except (TypeError, ValueError) as exc:
            # Session snapshots are private editor state, not user assets.
            # Destructive schema changes discard them instead of retaining a
            # compatibility parser that could resurrect invalid documents.
            from Infernux.debug import Debug

            Debug.log_warning(f"Discarded incompatible document session: {exc}")
            _panel_state.delete("document_session")
        ws = _panel_state.get("window_manager")
        if ws:
            wm.load_state(ws)
        else:
            # Initial dock presentation is requested through WindowManager so
            # native ImGui focus and FocusService share one authority.
            wm.focus_window("scene_view")

        documents = DocumentRegistry.instance()
        # Window restoration reconciles pending document Views against the
        # persisted open/closed topology. Commit that one-time pruning now so
        # an abnormal shutdown cannot resurrect a draft the user had already
        # closed or discarded. Normal layout changes still avoid recapturing
        # large authoring snapshots.
        reconciled_session = documents.capture_session_state()
        if reconciled_session["documents"]:
            _panel_state.put("document_session", reconciled_session)
        else:
            _panel_state.delete("document_session")
        _panel_state.prune_document_view_states(
            is_document_backed=lambda view_id: wm.is_document_backed_view(
                view_id,
                wm.window_type_id(view_id),
            ),
            has_restorable_document=lambda view_id: (
                documents.document_for_view(view_id) is not None
                or documents.has_pending_session_document(view_id)
            ),
        )

        # Restore individual panel states for every tracked window id
        seen_restore: set[str] = set()
        for wid in set(wm._default_instances.keys()) | set(wm._window_instances.keys()):
            if wid in seen_restore:
                continue
            seen_restore.add(wid)
            inst = wm._window_instances.get(wid) or wm._default_instances.get(wid)
            if inst is None:
                continue
            restore_view_state = getattr(
                inst,
                "_load_persisted_view_state_once",
                None,
            )
            if callable(restore_view_state):
                restore_view_state()
            elif hasattr(inst, "load_state") and callable(inst.load_state):
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

