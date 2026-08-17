from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


def test_editor_panels_do_not_reintroduce_private_command_key_polling():
    """Command keys belong to EditorShortcutInput + ShortcutRouter.

    The allowlist contains only continuous gestures, selection modifiers,
    widget-local navigation, and Game View cursor-capture release.  Adding a
    command key read to another panel must go through a PanelInteractionDescriptor
    instead of expanding this list.
    """
    from pathlib import Path

    ui_root = Path("python/Infernux/engine/ui")
    allowed = {
        "_scene_view_camera.py",
        "_scene_view_gizmo.py",
        "_scene_view_picking.py",
        "asset_details_renderer.py",
        "game_view_panel.py",
        "imgui_keys.py",
        "node_graph_view.py",
        "ui_editor_shortcuts.py",
    }
    forbidden_tokens = (
        ".is_key_down(",
        ".is_key_pressed(",
        ".is_key_released(",
        "Input.get_key_down(",
        "Input.get_key_up(",
    )

    offenders = []
    for source_path in ui_root.rglob("*.py"):
        if source_path.name in allowed:
            continue
        source = source_path.read_text(encoding="utf-8-sig")
        if any(token in source for token in forbidden_tokens):
            offenders.append(source_path.as_posix())

    assert offenders == [], (
        "Editor panels must declare command shortcuts through the shared "
        f"interaction core; private polling found in: {offenders}"
    )

from Infernux.engine._bootstrap_wiring import BootstrapWiringMixin
from Infernux.engine.interaction import (
    ActionOrigin,
    CommandResult,
    CommandSource,
    CommandStatus,
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    InputContext,
    SelectionService,
    SelectionDomain,
    SelectionTarget,
    EditorInteractionCore,
    KeyChord,
    ShortcutEvent,
    ShortcutRouteStatus,
)
from Infernux.engine.ui.animtimeline_editor_panel import (
    _TIMELINE_PANEL_INTERACTION,
)
from Infernux.engine.ui.animclip2d_editor_panel import (
    _ANIMCLIP2D_PANEL_INTERACTION,
)
from Infernux.engine.ui.animfsm_editor_panel import (
    _ANIMFSM_PANEL_INTERACTION,
)
from Infernux.engine.ui.node_graph_editor_panel import (
    NODE_GRAPH_PANEL_INTERACTION,
)
from Infernux.engine.ui.core_panel_interactions import (
    console_panel_interaction,
    hierarchy_panel_interaction,
    inspector_panel_interaction,
    project_panel_interaction,
    scene_view_panel_interaction,
    ui_editor_panel_interaction,
)


def test_inspector_owns_sprite_subresource_selection_domain():
    descriptor = inspector_panel_interaction(lambda: SimpleNamespace())

    assert SelectionDomain.ASSET in descriptor.owned_selection_domains
    assert SelectionDomain.ASSET_SUBRESOURCE in descriptor.owned_selection_domains


class _SceneCommandStub:
    def __init__(self, panel_getter):
        self._panel_getter = panel_getter

    @staticmethod
    def has_selection(context):
        return bool(
            context.selection.domain is SelectionDomain.SCENE_OBJECT
            and context.selection.targets
        )

    def can_copy(self, context):
        return self.has_selection(context) and self._panel_getter() is not None

    def copy(self, _context, *, cut):
        panel = self._panel_getter()
        return bool(panel and panel.copy_selected(cut))

    def can_paste(self, _context):
        panel = self._panel_getter()
        return bool(panel and panel.has_clipboard_data())

    def paste(self, _context):
        panel = self._panel_getter()
        return bool(panel and panel.paste_clipboard())

    def duplicate(self, _context):
        panel = self._panel_getter()
        return bool(panel and panel.duplicate_selected())

    def delete(self, _context):
        panel = self._panel_getter()
        if panel is None:
            return False
        panel.delete_selected_objects()
        return True

    def can_external_drop(self, reference, parent_id=0, is_guid=False):
        del is_guid
        return bool(reference and parent_id >= 0 and self._panel_getter() is not None)

    def instantiate_prefab(self, reference, parent_id=0, is_guid=False):
        panel = self._panel_getter()
        return bool(
            panel
            and panel.instantiate_prefab(reference, parent_id, is_guid)
        )

    def create_model(self, reference, parent_id=0, is_guid=False):
        panel = self._panel_getter()
        return bool(panel and panel.create_model(reference, parent_id, is_guid))

    def rename(self, object_id, new_name):
        panel = self._panel_getter()
        return bool(panel and panel.rename_object(object_id, new_name))

    def move_hierarchy(self, object_ids, mode, target_id, after):
        panel = self._panel_getter()
        return bool(
            panel
            and panel.move_hierarchy(object_ids, mode, target_id, after)
        )


class _PrefabCommandStub:
    def __init__(self, calls):
        self.calls = calls

    def can_execute(self, action, *, object_id=0, path=""):
        if action == "exit":
            return True
        return object_id == 42 and bool(action) or bool(path)

    def create_from_object(self, object_id, current_path="", *, origin=None):
        self.calls.append(("prefab", object_id, "save_as"))
        return "C:/Project/Assets/Test.prefab"

    def locate(self, *, object_id=0, path="", record_history=True):
        self.calls.append(("prefab", object_id, "select"))
        return True

    def open(self, *, object_id=0, path="", origin=None):
        self.calls.append(("prefab", object_id, "open"))
        return True

    def apply(self, object_id, *, origin=None):
        self.calls.append(("prefab", object_id, "apply"))
        return True

    def revert(self, object_id, *, origin=None):
        self.calls.append(("prefab", object_id, "revert"))
        return True

    def unpack(self, object_id, *, origin=None):
        self.calls.append(("prefab", object_id, "unpack"))
        return True

    def exit(self, *, origin=None):
        self.calls.append(("prefab", 0, "exit"))
        return True

    def shutdown(self):
        pass


def _registry():
    focus = FocusService()
    selection = SelectionService()
    return EditorCommandRegistry(focus=focus, selection=selection), focus, selection


def _bind_panel(core, type_id, descriptor, instance, *, view_id=None):
    core.panels.register_type(type_id, descriptor, replace=True)
    core.panels.bind_view(view_id or type_id, type_id, instance)


def test_shortcut_router_retains_the_latest_authoritative_route_result():
    core = EditorInteractionCore()
    core.focus.activate_panel("project", view_id="project", record_history=False)

    first = core.shortcuts.route(ShortcutEvent(KeyChord.parse("F12")))
    second = core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("F12"), text_input_active=True)
    )

    assert first.status is ShortcutRouteStatus.NO_MATCH
    assert second.status is ShortcutRouteStatus.NO_MATCH
    assert core.shortcuts.route_revision == 2
    assert core.shortcuts.last_event == ShortcutEvent(
        KeyChord.parse("F12"), text_input_active=True
    )
    assert core.shortcuts.last_result is second

    core.shutdown()


def test_command_context_captures_authoritative_focus_selection_and_input_context():
    registry, focus, selection = _registry()
    focus.activate_panel(
        "hierarchy",
        view_id="hierarchy/main",
        document_id="scene:main",
        child_context_id="hierarchy.tree",
    )
    focus.input_contexts.push(InputContext("hierarchy.tree", "hierarchy", 20))
    selection.select(SelectionTarget.scene_object(42), owner_id="hierarchy")
    contexts = []
    registry.register(EditorCommand("edit.delete", contexts.append))

    result = registry.execute("edit.delete", source=CommandSource.MENU)

    assert result.status is CommandStatus.EXECUTED
    assert contexts[0].source is CommandSource.MENU
    assert contexts[0].focus.active_document_id == "scene:main"
    assert contexts[0].selection.primary == SelectionTarget.scene_object(42)
    assert contexts[0].input_context_ids == ("hierarchy.tree",)


def test_editor_panel_owned_command_publishes_view_and_document_before_routing():
    from Infernux.engine.ui.editor_panel import EditorPanel
    from Infernux.engine.ui.editor_services import EditorServices

    previous_services = EditorServices._instance
    core = EditorInteractionCore()
    services = EditorServices()
    services._interaction_core = core
    panel = EditorPanel("Timeline", "timeline/main")
    panel.set_panel_identity("animtimeline_editor", "timeline/main")
    panel._document_id = "document:timeline"
    observed = []
    core.commands.register(EditorCommand("probe.save", observed.append))
    core.focus.activate_panel("game_view", view_id="game_view", record_history=False)
    try:
        assert panel.execute_owned_command(
            "probe.save",
            source=CommandSource.TOOLBAR,
            payload={"revision": 7},
        )
        assert observed[0].focus.active_panel_id == "animtimeline_editor"
        assert observed[0].focus.active_view_id == "timeline/main"
        assert observed[0].focus.active_document_id == "document:timeline"
        assert observed[0].payload == {"revision": 7}
    finally:
        core.shutdown()
        EditorServices._instance = previous_services


def test_command_registration_rejects_duplicate_identity_without_explicit_replace():
    registry, _focus, _selection = _registry()
    registry.register(EditorCommand("file.save", lambda _context: None))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EditorCommand("file.save", lambda _context: None))

    registry.register(
        EditorCommand("file.save", lambda _context: "replacement"),
        replace=True,
    )
    assert registry.execute("file.save").value == "replacement"


def test_command_disabled_noop_failure_and_missing_results_are_structured():
    registry, _focus, _selection = _registry()
    registry.register(EditorCommand("disabled", lambda _context: None, can_execute=lambda _context: False))
    registry.register(EditorCommand("noop", lambda _context: False))
    registry.register(EditorCommand("failure", lambda _context: 1 / 0))

    assert registry.execute("disabled").status is CommandStatus.DISABLED
    assert registry.execute("noop").status is CommandStatus.NO_OP
    assert registry.execute("failure").status is CommandStatus.FAILED
    assert registry.execute("missing").status is CommandStatus.NOT_FOUND


def test_command_handler_cannot_return_result_for_another_command():
    registry, _focus, _selection = _registry()
    registry.register(
        EditorCommand(
            "edit.undo",
            lambda _context: CommandResult("edit.redo", CommandStatus.EXECUTED),
        )
    )

    result = registry.execute("edit.undo")

    assert result.status is CommandStatus.FAILED
    assert "another command" in result.message


def test_command_registry_wraps_context_changes_in_one_user_action():
    from Infernux.engine.interaction import EditorContextSnapshot
    from Infernux.engine.undo import UndoManager

    registry, focus, selection = _registry()
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(focus.snapshot, selection.snapshot),
        lambda context, _phase: (
            focus.apply_snapshot(context.focus, record_history=False),
            selection.apply_snapshot(context.selection, record_history=False),
        ),
    )
    focus.activate_panel("scene_view", record_history=False)
    selection.select(
        SelectionTarget.scene_object(7),
        owner_id="scene_view",
        record_history=False,
    )

    def select_asset(_context):
        focus.activate_panel("project")
        selection.select(
            SelectionTarget.asset("Assets/Test.mat"),
            owner_id="project",
        )
        return True

    registry.register(EditorCommand("project.select", select_asset, "Select Asset"))
    assert registry.execute("project.select").accepted

    assert len(manager.action_journal.entries) == 1
    assert manager.undo_description == "Select Asset"
    manager.undo()
    assert focus.snapshot.active_panel_id == "scene_view"
    assert selection.snapshot.primary == SelectionTarget.scene_object(7)


def test_bootstrap_registers_menu_and_shortcut_entries_against_same_commands():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    scene_files = SimpleNamespace(
        new_scene=lambda: calls.append("new"),
        save_current_scene=lambda: calls.append("save") or True,
        save_scene_as=lambda: calls.append("save_as") or True,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {"console": object()},
        open_window=lambda target_id: calls.append(("open", target_id)) or object(),
        open_window_from_user=lambda target_id: calls.append(("open", target_id))
        or object(),
        is_window_open=lambda target_id: target_id == "console",
        reset_layout=lambda: calls.append("reset_layout"),
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.interaction_core.saving.save_focused = (
        lambda **_kwargs: SimpleNamespace(
            accepted=bool(calls.append("save") is None)
        )
    )
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)

    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        scene_files,
    )

    registry = bootstrap.interaction_core.commands
    assert registry.execute("file.save", source=CommandSource.MENU).accepted
    routed = bootstrap.interaction_core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Ctrl+N"))
    )
    opened = registry.execute(
        "window.open",
        source=CommandSource.MENU,
        payload={"target_id": "console"},
    )
    window_context = registry.context(
        CommandSource.MENU,
        {"target_id": "console"},
    )

    assert routed.status is ShortcutRouteStatus.EXECUTED
    assert opened.accepted
    assert registry.is_checked("window.open", window_context)
    assert calls == ["save", "new", ("open", "console")]
    assert registry.get("file.save").default_shortcut == "Ctrl+S"


def test_window_toggle_uses_the_user_navigation_path_for_opening():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    open_windows = set()

    def open_from_user(target_id, *, reason="window_open_command"):
        calls.append(("open_from_user", target_id, reason))
        open_windows.add(target_id)
        return object()

    def close_window(target_id):
        calls.append(("close", target_id))
        open_windows.discard(target_id)

    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {"preferences": object()},
        open_window_from_user=open_from_user,
        close_window=close_window,
        is_window_open=lambda target_id: target_id in open_windows,
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        registry = bootstrap.interaction_core.commands
        context = registry.context(
            CommandSource.MENU,
            {"target_id": "preferences"},
        )

        assert registry.can_execute("window.toggle", context)
        assert not registry.is_checked("window.toggle", context)
        assert registry.execute(
            "window.toggle",
            source=CommandSource.MENU,
            payload={"target_id": "preferences"},
        ).accepted
        assert registry.is_checked("window.toggle", context)
        assert calls == [
            ("open_from_user", "preferences", "window_toggle_command")
        ]

        assert registry.execute(
            "window.toggle",
            source=CommandSource.MENU,
            payload={"target_id": "preferences"},
        ).accepted
        assert not registry.is_checked("window.toggle", context)
        assert calls[-1] == ("close", "preferences")
    finally:
        bootstrap.interaction_core.shutdown()


def test_window_toggle_cannot_close_permanent_toolbar_chrome():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {"toolbar": object()},
        open_window_from_user=lambda target_id, **_kwargs: calls.append(
            ("open", target_id)
        ),
        close_window=lambda target_id: calls.append(("close", target_id)),
        is_window_open=lambda _target_id: True,
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        context = bootstrap.interaction_core.commands.context(
            CommandSource.MENU,
            {"target_id": "toolbar"},
        )

        assert not bootstrap.interaction_core.commands.can_execute(
            "window.toggle",
            context,
        )
        assert not bootstrap.interaction_core.commands.execute(
            "window.toggle",
            source=CommandSource.MENU,
            payload={"target_id": "toolbar"},
        ).accepted
        assert calls == []
    finally:
        bootstrap.interaction_core.shutdown()


def test_project_document_open_uses_user_window_navigation_after_resource_open():
    from Infernux.engine.interaction import (
        DocumentOpenResult,
        DocumentOpenStatus,
    )

    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []

    def open_from_user(target_id, *, reason="window_open_command"):
        calls.append(("present", target_id, reason))
        return object()

    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        open_window_from_user=open_from_user,
        is_window_open=lambda _target_id: False,
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(
        _play_mode_manager=None,
        get_asset_database=lambda: None,
    )
    bootstrap.interaction_core.document_open.open_resource = (
        lambda kind, path, **kwargs: calls.append(
            ("open_resource", kind.value, path, kwargs["title"])
        )
        or DocumentOpenResult(DocumentOpenStatus.READY)
    )

    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        result = bootstrap.interaction_core.commands.execute(
            "project.open_document",
            source=CommandSource.POINTER,
            payload={
                "path": "C:/Project/Assets/Smoke.particlegraph",
                "document_kind": "particle_graph",
            },
        )

        assert result.accepted
        assert calls == [
            (
                "open_resource",
                "particle_graph",
                "C:/Project/Assets/Smoke.particlegraph",
                "Smoke.particlegraph",
            ),
            (
                "present",
                "particle_graph_editor",
                "project_open_document",
            ),
        ]
    finally:
        bootstrap.interaction_core.shutdown()


def test_failed_project_document_open_does_not_publish_window_navigation():
    from Infernux.engine.interaction import (
        DocumentOpenResult,
        DocumentOpenStatus,
    )

    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        open_window_from_user=lambda target_id, **_kwargs: calls.append(target_id),
        is_window_open=lambda _target_id: False,
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(
        _play_mode_manager=None,
        get_asset_database=lambda: None,
    )
    bootstrap.interaction_core.document_open.open_resource = (
        lambda *_args, **_kwargs: DocumentOpenResult(
            DocumentOpenStatus.FAILED,
            message="rejected",
        )
    )

    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        result = bootstrap.interaction_core.commands.execute(
            "project.open_document",
            source=CommandSource.POINTER,
            payload={
                "path": "C:/Project/Assets/Broken.animfsm",
                "document_kind": "animation_fsm",
            },
        )

        assert result.status is CommandStatus.NO_OP
        assert result.value is False
        assert calls == []
    finally:
        bootstrap.interaction_core.shutdown()


def test_scene_grid_toolbar_action_uses_one_global_undoable_command():
    from Infernux.engine.undo import UndoManager

    class BootstrapHarness(BootstrapWiringMixin):
        pass

    class NativeEngine:
        def __init__(self):
            self.show_grid = True

        def is_show_grid(self):
            return self.show_grid

        def set_show_grid(self, value):
            self.show_grid = bool(value)

    native = NativeEngine()
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(
        _play_mode_manager=None,
        get_native_engine=lambda: native,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: set(),
        reset_layout=lambda: None,
    )
    previous_manager = UndoManager.instance()
    manager = UndoManager(bootstrap.interaction_core.action_journal)
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        registry = bootstrap.interaction_core.commands
        context = registry.context(CommandSource.TOOLBAR)

        assert registry.can_execute("scene.toggle_grid", context)
        assert registry.is_checked("scene.toggle_grid", context)
        assert registry.execute(
            "scene.toggle_grid",
            source=CommandSource.TOOLBAR,
        ).accepted
        assert native.show_grid is False
        assert manager.undo_description == "Toggle Scene Grid"

        manager.undo()
        assert native.show_grid is True
    finally:
        UndoManager._instance = previous_manager
        bootstrap.interaction_core.shutdown()


def test_status_bar_console_navigation_is_one_global_command():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    selected = []
    panel = SimpleNamespace(select_entry=lambda uid: selected.append(uid))
    opened = []
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: panel,
        get_registered_types=lambda: {"console"},
        open_window_from_user=lambda target_id, *, reason: (
            opened.append((target_id, reason)) or panel
        ),
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        result = bootstrap.interaction_core.commands.execute(
            "console.open_entry",
            source=CommandSource.POINTER,
            payload={"uid": "42"},
        )

        assert result.accepted
        assert opened == [("console", "status_bar_console")]
        assert selected == [42]
    finally:
        bootstrap.interaction_core.shutdown()


def test_console_source_navigation_is_one_global_command(monkeypatch):
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    opened_sources = []
    monkeypatch.setattr(
        "Infernux.engine.ui.project_utils.open_file_with_system",
        lambda path, project_root="": (
            opened_sources.append((path, project_root)) or True
        ),
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {"console"},
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.project_path = "D:/Projects/ConsoleTest"
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        result = bootstrap.interaction_core.commands.execute(
            "console.open_source",
            source=CommandSource.POINTER,
            payload={"source_path": "D:/src/example.py", "source_line": 17},
        )

        assert result.accepted
        assert opened_sources == [
            ("D:/src/example.py", "D:/Projects/ConsoleTest")
        ]
    finally:
        bootstrap.interaction_core.shutdown()


def test_play_command_reveals_game_view_through_window_manager_only():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    class PlayModeStub:
        def __init__(self):
            self.is_playing = False
            self.calls = []

        def enter_play_mode(self):
            self.calls.append("enter")
            self.is_playing = True
            return True

        def exit_play_mode(self):
            self.calls.append("exit")
            self.is_playing = False
            return True

    play_mode = PlayModeStub()
    window_calls = []
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {"game_view": object()},
        open_window_from_user=lambda target_id, *, reason: window_calls.append(
            (target_id, reason)
        )
        or object(),
        is_window_open=lambda _target_id: True,
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=play_mode)

    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(new_scene=lambda: None),
    )

    registry = bootstrap.interaction_core.commands
    assert registry.execute("play.toggle").accepted
    assert play_mode.calls == ["enter"]
    assert window_calls == [("game_view", "play_mode_enter")]

    assert registry.execute("play.toggle").accepted
    assert play_mode.calls == ["enter", "exit"]
    assert window_calls == [("game_view", "play_mode_enter")]


def test_hierarchy_and_scene_edit_shortcuts_share_command_handlers():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = SimpleNamespace(
        copy_selected=lambda cut: calls.append(("copy", cut)) or True,
        paste_clipboard=lambda: calls.append("paste") or True,
        has_clipboard_data=lambda: True,
        delete_selected_objects=lambda: calls.append("delete"),
        duplicate_selected=lambda: calls.append("duplicate") or True,
        begin_rename_object=lambda object_id: calls.append(("rename", object_id)),
        instantiate_prefab=lambda reference, parent_id, is_guid: calls.append(
            ("instantiate_prefab", reference, parent_id, is_guid)
        ) or True,
        create_model=lambda reference, parent_id, is_guid: calls.append(
            ("create_model", reference, parent_id, is_guid)
        ) or True,
        rename_object=lambda object_id, new_name: calls.append(
            ("rename_object", object_id, new_name)
        ) or True,
        move_hierarchy=lambda object_ids, mode, target_id, after: calls.append(
            ("move_hierarchy", tuple(object_ids), mode, target_id, after)
        ) or True,
        get_expanded_object_ids=lambda: [],
        set_expanded_object_ids=lambda ids: calls.append(
            ("hierarchy_expanded", tuple(ids))
        ),
        request_search_focus=lambda: calls.append("focus_search"),
    )
    scene_commands = _SceneCommandStub(lambda: bootstrap.hierarchy)
    bootstrap.interaction_core.prefabs = _PrefabCommandStub(calls)
    creation_service = SimpleNamespace(
        can_create=lambda kind, parent_id=0: bool(kind) and parent_id >= 0,
        create=lambda kind, parent_id=0: calls.append(
            ("create", kind, parent_id)
        ) or {"id": 99},
        can_create_empty_parent=lambda object_ids: object_ids == [42],
        create_empty_parent=lambda object_ids: calls.append(
            ("create_empty_parent", tuple(object_ids))
        ) or {"id": 100},
    )
    _bind_panel(
        bootstrap.interaction_core,
        "hierarchy",
        hierarchy_panel_interaction(
            scene_commands,
            creation_service=creation_service,
        ),
        bootstrap.hierarchy,
    )
    scene_view = SimpleNamespace(
        _gizmo_tool_mode=0,
        _coord_space=0,
        _set_tool_mode=lambda _mode: None,
        _set_coordinate_space=lambda value: setattr(scene_view, "_coord_space", int(value)),
        _align_object_to_camera=lambda: calls.append("align_with_view") or True,
        can_frame_object_by_id=lambda object_id: int(object_id) == 42,
        frame_object_by_id=lambda object_id: calls.append(
            ("frame_selected", int(object_id))
        )
        or True,
    )
    _bind_panel(
        bootstrap.interaction_core,
        "scene_view",
        scene_view_panel_interaction(scene_commands),
        scene_view,
    )
    windows = SimpleNamespace(
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    scene_files = SimpleNamespace()
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        scene_files,
    )
    core = bootstrap.interaction_core
    core.focus.activate_panel("hierarchy", view_id="hierarchy")
    core.selection.select(
        SelectionTarget.scene_object(42),
        owner_id="hierarchy",
    )

    assert core.commands.execute(
        "hierarchy.set_expanded",
        source=CommandSource.POINTER,
        payload={"target_id": 42, "expanded": True},
    ).accepted
    assert calls[-1] == ("hierarchy_expanded", (42,))
    calls.clear()

    copied = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+C")))
    deleted = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Delete")))
    renamed = core.shortcuts.route(ShortcutEvent(KeyChord.parse("F2")))
    duplicated = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+D")))

    assert copied.status is ShortcutRouteStatus.EXECUTED
    assert deleted.status is ShortcutRouteStatus.EXECUTED
    assert renamed.status is ShortcutRouteStatus.EXECUTED
    assert duplicated.status is ShortcutRouteStatus.EXECUTED
    assert calls == [("copy", False), "delete", ("rename", 42), "duplicate"]

    calls.clear()
    focused = core.shortcuts.route(
        ShortcutEvent(
            KeyChord.parse("Ctrl+F"),
            game_view_captured=True,
            text_input_active=True,
        )
    )
    assert focused.status is ShortcutRouteStatus.EXECUTED
    assert calls == ["focus_search"]

    calls.clear()
    core.focus.activate_panel("scene_view", view_id="scene_view")
    aligned = core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Ctrl+Shift+F"))
    )
    find_is_not_scene_align = core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Ctrl+F"))
    )
    framed = core.shortcuts.route(ShortcutEvent(KeyChord.parse("F")))
    assert aligned.status is ShortcutRouteStatus.EXECUTED
    assert find_is_not_scene_align.status is ShortcutRouteStatus.NO_MATCH
    assert framed.status is ShortcutRouteStatus.EXECUTED
    assert calls == ["align_with_view", ("frame_selected", 42)]
    core.focus.activate_panel("hierarchy", view_id="hierarchy")

    assert core.commands.execute(
        "scene.create_object",
        source=CommandSource.CONTEXT_MENU,
        payload={"kind": "primitive.cube", "parent_id": 0},
    ).accepted
    assert calls[-1] == ("create", "primitive.cube", 0)
    assert core.commands.execute(
        "scene.create_empty_parent",
        source=CommandSource.CONTEXT_MENU,
    ).accepted
    assert calls[-1] == ("create_empty_parent", (42,))
    core.focus.activate_panel("project", view_id="project")
    assert core.commands.execute(
        "scene.instantiate_prefab",
        source=CommandSource.DRAG_DROP,
        payload={"reference": "prefab-guid", "parent_id": 7, "is_guid": True},
    ).accepted
    assert calls[-1] == ("instantiate_prefab", "prefab-guid", 7, True)
    assert core.commands.execute(
        "scene.create_model",
        source=CommandSource.DRAG_DROP,
        payload={"reference": "mesh-guid", "parent_id": 0, "is_guid": True},
    ).accepted
    assert calls[-1] == ("create_model", "mesh-guid", 0, True)
    assert core.commands.execute(
        "scene.rename_object",
        source=CommandSource.API,
        payload={"object_id": 42, "new_name": "Renamed"},
    ).accepted
    assert calls[-1] == ("rename_object", 42, "Renamed")
    assert core.commands.execute(
        "scene.move_hierarchy",
        source=CommandSource.DRAG_DROP,
        payload={
            "object_ids": [42],
            "mode": "parent",
            "target_id": 7,
            "after": False,
        },
    ).accepted
    assert calls[-1] == ("move_hierarchy", (42,), "parent", 7, False)
    assert core.focus.snapshot.active_view_id == "project"

    assert core.commands.execute(
        "scene.frame_selected",
        source=CommandSource.POINTER,
        payload={"object_id": 42},
    ).accepted
    assert calls[-1] == ("frame_selected", 42)
    assert core.focus.snapshot.active_view_id == "project"

    core.focus.activate_panel("hierarchy", view_id="hierarchy")

    for command_id, action in (
        ("prefab.save_as", "save_as"),
        ("prefab.select_asset", "select"),
        ("prefab.open", "open"),
        ("prefab.apply", "apply"),
        ("prefab.revert", "revert"),
        ("prefab.unpack", "unpack"),
    ):
        assert core.commands.execute(
            command_id,
            source=CommandSource.CONTEXT_MENU,
            payload={"object_id": 42},
        ).accepted
        assert calls[-1] == ("prefab", 42, action)
    assert core.commands.execute(
        "prefab.exit",
        source=CommandSource.TOOLBAR,
    ).accepted
    assert calls[-1] == ("prefab", 0, "exit")

    core.focus.activate_panel("scene_view", view_id="scene_view")
    framed = core.shortcuts.route(ShortcutEvent(KeyChord.parse("F")))
    assert framed.status is ShortcutRouteStatus.EXECUTED
    assert calls[-1] == ("frame_selected", 42)
    pasted = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+V")))
    assert pasted.status is ShortcutRouteStatus.EXECUTED
    assert calls[-1] == "paste"


def test_ui_editor_uses_global_scene_commands_and_selection_clear():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = SimpleNamespace(
        copy_selected=lambda cut: calls.append(("copy", cut)) or True,
        paste_clipboard=lambda: calls.append("paste") or True,
        has_clipboard_data=lambda: True,
        delete_selected_objects=lambda: calls.append("delete"),
        begin_rename_object=lambda _object_id: None,
    )
    scene_commands = _SceneCommandStub(lambda: bootstrap.hierarchy)
    creation_service = SimpleNamespace(
        can_create=lambda kind, parent_id=0: kind.startswith("ui.") and parent_id >= 0,
        create=lambda kind, **kwargs: calls.append(
            ("create_ui", kind, kwargs)
        ) or {"id": 99},
    )
    bootstrap.ui_editor = SimpleNamespace(
        can_nudge_selected=lambda: False,
        command_nudge_selected=lambda _dx, _dy: False,
    )
    _bind_panel(
        bootstrap.interaction_core,
        "ui_editor",
        ui_editor_panel_interaction(
            scene_commands,
            creation_service=creation_service,
        ),
        bootstrap.ui_editor,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )
    core = bootstrap.interaction_core
    core.focus.activate_panel("ui_editor", view_id="ui_editor")
    core.selection.select(
        SelectionTarget.scene_object(42),
        owner_id="ui_editor",
        record_history=False,
    )

    copied = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+C")))
    deleted = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Delete")))
    deselected = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Escape")))

    assert copied.status is ShortcutRouteStatus.EXECUTED
    assert deleted.status is ShortcutRouteStatus.EXECUTED
    assert deselected.status is ShortcutRouteStatus.EXECUTED
    assert calls == [("copy", False), "delete"]
    assert core.selection.snapshot.targets == ()

    assert core.commands.execute(
        "scene.create_object",
        source=CommandSource.POINTER,
        payload={"kind": "ui.text", "parent_id": 12},
    ).accepted
    assert calls[-1] == (
        "create_ui",
        "ui.text",
        {
            "parent_id": 12,
            "selection_owner_id": "ui_editor",
            "selection_reason": "ui_editor_create_text",
        },
    )

    core.shutdown()


def test_project_edit_shortcuts_use_the_same_commands_as_hierarchy():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    interactions = SimpleNamespace(
        can_copy=lambda paths: bool(paths),
        copy=lambda paths, *, cut: calls.append(
            ("copy_asset", tuple(paths), cut)
        )
        or True,
        can_paste=lambda destination: bool(destination),
        paste=lambda destination, *, origin: calls.append(
            ("paste_asset", destination, origin)
        )
        or ("C:/Project/Assets/Pasted.mat",),
        request_delete=lambda paths, *, origin: calls.append(
            ("delete_asset", tuple(paths), origin)
        )
        or True,
        can_create=lambda kind, directory, base, extension: bool(
            kind and directory and base
        ),
        create=lambda kind, directory, base, extension, variant="", *, origin: calls.append(
            ("create_asset", kind, directory, base, extension, variant, origin)
        )
        or os.path.normpath(os.path.join(directory, base + extension)),
        can_open=lambda kind, path: bool(kind and path),
        open=lambda kind, path: calls.append(("open_asset", kind, path)) or True,
        reveal=lambda path: calls.append(("reveal", path)) or True,
        transfer=lambda paths, destination, *, origin: calls.append(
            ("transfer", tuple(paths), destination, origin)
        )
        or tuple(paths),
    )
    bootstrap.project_panel = SimpleNamespace(
        begin_rename_selected_asset=lambda path="": calls.append(("rename_asset", path)) or True,
        can_rename_selected_asset=lambda path="": True,
        can_navigate_to_path=lambda path: bool(path),
        get_current_path=lambda: "C:/Project/Assets",
        set_current_path=lambda path: calls.append(("navigate", path)) or True,
        get_folder_expanded_paths=lambda: [],
        set_folder_expanded_paths=lambda paths: calls.append(
            ("folder_expanded", tuple(paths))
        ),
        get_model_expanded_paths=lambda: [],
        set_model_expanded_paths=lambda paths: calls.append(
            ("model_expanded", tuple(paths))
        ),
        request_search_focus=lambda: calls.append("focus_search"),
    )
    _bind_panel(
        bootstrap.interaction_core,
        "project",
        project_panel_interaction(
            interactions,
            bootstrap.interaction_core.navigation,
        ),
        bootstrap.project_panel,
    )
    windows = SimpleNamespace(get_registered_types=lambda: {}, reset_layout=lambda: None)
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )
    core = bootstrap.interaction_core
    core.focus.activate_panel("project", view_id="project")
    core.selection.select(
        SelectionTarget.asset("C:/Project/Assets/Smoke.mat"),
        owner_id="project",
    )

    for chord in (
        "Ctrl+C",
        "Ctrl+X",
        "Ctrl+V",
        "Delete",
        "F2",
        "Ctrl+D",
        "Ctrl+Shift+N",
    ):
        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse(chord))
        ).status is ShortcutRouteStatus.EXECUTED

    assert calls == [
        ("copy_asset", (os.path.normpath("C:/Project/Assets/Smoke.mat"),), False),
        ("copy_asset", (os.path.normpath("C:/Project/Assets/Smoke.mat"),), True),
        ("paste_asset", "C:/Project/Assets", ActionOrigin.USER),
        ("delete_asset", (os.path.normpath("C:/Project/Assets/Smoke.mat"),), ActionOrigin.USER),
        ("rename_asset", os.path.normpath("C:/Project/Assets/Smoke.mat")),
        ("copy_asset", (os.path.normpath("C:/Project/Assets/Smoke.mat"),), False),
        ("paste_asset", "C:/Project/Assets", ActionOrigin.USER),
        (
            "create_asset",
            "folder",
            "C:/Project/Assets",
            "NewFolder",
            "",
            "",
            ActionOrigin.USER,
        ),
        ("rename_asset", os.path.normpath("C:/Project/Assets/NewFolder")),
    ]

    created = core.commands.execute(
        "asset.create",
        payload={
            "kind": "material",
            "base_name": "NewMaterial",
            "extension": ".mat",
            "variant": "",
        },
    )
    assert created.accepted
    assert calls[-1] == (
        "rename_asset",
        os.path.normpath("C:/Project/Assets/NewMaterial.mat"),
    )
    opened = core.commands.execute(
        "asset.open",
        payload={"kind": "particle_graph", "path": "C:/Project/Assets/Smoke.particlegraph"},
    )
    assert opened.accepted
    assert calls[-1] == (
        "open_asset",
        "particle_graph",
        "C:/Project/Assets/Smoke.particlegraph",
    )
    revealed = core.commands.execute(
        "project.reveal_in_explorer",
        payload={"path": "C:/Project/Assets/Smoke.particlegraph"},
    )
    assert revealed.accepted
    assert calls[-1] == (
        "reveal",
        "C:/Project/Assets/Smoke.particlegraph",
    )

    # Pointer commands emitted by the native Project panel must keep their
    # concrete destination even if ImGui focus projection still names another
    # panel during the click frame.
    core.focus.activate_panel("inspector", view_id="inspector", record_history=False)
    navigated = core.commands.execute(
        "project.navigate_directory",
        source=CommandSource.POINTER,
        payload={"target_id": "C:/Project/Assets/Materials"},
    )
    assert navigated.accepted
    assert calls[-1] == ("navigate", os.path.normpath("C:/Project/Assets/Materials"))
    core.focus.activate_panel("project", view_id="project", record_history=False)

    assert core.commands.get("project.navigate_back") is not None
    assert core.commands.get("project.navigate_forward") is not None

    frozen_target = core.commands.context(
        CommandSource.CONTEXT_MENU,
        {"target_id": "C:/Project/Assets/RightClicked.mat"},
    )
    copied = core.commands.execute_context("edit.copy", frozen_target)

    assert copied.accepted
    assert calls[-1] == (
        "copy_asset",
        (os.path.normpath("C:/Project/Assets/RightClicked.mat"),),
        False,
    )

    # Native tree projections own generic-path stable IDs. They must remain
    # byte-for-byte intact instead of being rewritten to Windows separators.
    model_id = "C:/Project/Assets/Models/Hero.fbx"
    expanded = core.commands.execute(
        "project.set_model_expanded",
        source=CommandSource.POINTER,
        payload={"target_id": model_id, "expanded": True},
    )
    assert expanded.accepted
    assert calls[-1] == ("model_expanded", (model_id,))


def test_asset_rename_command_uses_project_asset_authority_and_command_origin():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    calls = []
    service = bootstrap.interaction_core.project_assets
    service.can_rename = lambda source, name: (
        source == "C:/Project/Assets/Old.mat" and name == "New.mat"
    )
    service.rename = lambda source, name, *, origin: (
        calls.append((source, name, origin)) or "C:/Project/Assets/New.mat"
    )
    windows = SimpleNamespace(get_registered_types=lambda: {}, reset_layout=lambda: None)
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )

    result = bootstrap.interaction_core.commands.execute(
        "asset.rename",
        source=CommandSource.AUTOMATION,
        payload={
            "source_path": "C:/Project/Assets/Old.mat",
            "new_name": "New.mat",
        },
    )

    assert result.accepted
    assert calls == [
        (
            "C:/Project/Assets/Old.mat",
            "New.mat",
            ActionOrigin.AUTOMATION,
        )
    ]


def test_external_asset_import_command_uses_project_asset_authority_and_origin():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    calls = []
    service = bootstrap.interaction_core.project_assets
    expected_paths = (
        "C:/External/One.png",
        "C:/External/Two.png",
    )
    destination = "C:/Project/Assets"
    service.can_import_external = lambda paths, target: (
        tuple(paths) == expected_paths and target == destination
    )
    service.import_external = lambda paths, target, *, origin, select_results: (
        calls.append((tuple(paths), target, origin, select_results))
        or ("C:/Project/Assets/One.png", "C:/Project/Assets/Two.png")
    )
    windows = SimpleNamespace(get_registered_types=lambda: {}, reset_layout=lambda: None)
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )

    result = bootstrap.interaction_core.commands.execute(
        "asset.import_external",
        source=CommandSource.AUTOMATION,
        payload={"paths": expected_paths, "destination": destination},
    )

    assert result.accepted
    assert calls == [
        (expected_paths, destination, ActionOrigin.AUTOMATION, True)
    ]


@pytest.mark.parametrize("panel_id", ["animfsm_editor", "particle_graph_editor"])
def test_graph_edit_shortcuts_route_through_the_global_command_registry(panel_id):
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    panel = SimpleNamespace(
        can_edit_copy=lambda: True,
        can_edit_cut=lambda: True,
        can_edit_paste=lambda: True,
        can_edit_delete=lambda: True,
        can_edit_duplicate=lambda: True,
        can_edit_rename=lambda: True,
        command_edit_copy=lambda: calls.append("copy") or True,
        command_edit_cut=lambda: calls.append("cut") or True,
        command_edit_paste=lambda: calls.append("paste") or True,
        command_edit_delete=lambda: calls.append("delete") or True,
        command_edit_duplicate=lambda: calls.append("duplicate") or True,
        command_edit_rename=lambda: calls.append("rename") or True,
        can_graph_center_view=lambda: True,
        can_graph_reset_zoom=lambda: True,
        command_graph_center_view=lambda: calls.append("center_view") or True,
        command_graph_reset_zoom=lambda: calls.append("reset_zoom") or True,
        can_graph_add_node=lambda _context: False,
        command_graph_add_node=lambda _context: False,
        can_graph_create_node=lambda _context: False,
        command_graph_create_node=lambda _context: False,
        can_graph_open_create=lambda _context: False,
        command_graph_open_create=lambda _context: False,
        can_graph_workspace_add=lambda _context: False,
        command_graph_workspace_add=lambda _context: False,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda requested: panel if requested == panel_id else None,
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.interaction_core.panels.register_type(
        panel_id,
        NODE_GRAPH_PANEL_INTERACTION,
    )
    bootstrap.interaction_core.panels.bind_view(panel_id, panel_id, panel)
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )
    core = bootstrap.interaction_core
    core.focus.activate_panel(panel_id, view_id=panel_id, document_id="graph:test")

    for chord in ("Ctrl+C", "Ctrl+X", "Ctrl+V", "Delete", "Ctrl+D"):
        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse(chord))
        ).status is ShortcutRouteStatus.EXECUTED

    assert calls == ["copy", "cut", "paste", "delete", "duplicate"]
    assert core.commands.execute(
        "graph.center_view", source=CommandSource.CONTEXT_MENU
    ).accepted
    assert core.commands.execute(
        "graph.reset_zoom", source=CommandSource.CONTEXT_MENU
    ).accepted
    assert calls[-2:] == ["center_view", "reset_zoom"]


def test_timeline_toolbar_and_shortcuts_share_command_handlers():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    panel = SimpleNamespace(
        command_new_timeline=lambda: calls.append("new_timeline") or True,
        command_toggle_playback=lambda: calls.append("play_pause") or True,
        command_stop_playback=lambda: calls.append("stop") or True,
        command_set_loop_preview=lambda value: calls.append(
            ("loop_preview", value)
        )
        or True,
        command_add_keyframe=lambda: calls.append("add_keyframe") or True,
        command_delete_selected_keyframe=lambda: calls.append("delete_keyframe") or True,
        can_delete_selected_keyframe=lambda: True,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda panel_id: (
            panel if panel_id == "animtimeline_editor" else None
        ),
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.interaction_core.panels.register_type(
        "animtimeline_editor",
        _TIMELINE_PANEL_INTERACTION,
    )
    bootstrap.interaction_core.panels.bind_view(
        "animtimeline_editor",
        "animtimeline_editor",
        panel,
    )
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )

    core = bootstrap.interaction_core
    core.focus.activate_panel(
        "animtimeline_editor",
        view_id="animtimeline_editor",
        document_id="timeline:test",
    )
    core.selection.select(
        SelectionTarget.timeline_element(
            "timeline:test",
            "keyframe:test",
            sub_kind="keyframe",
        ),
        owner_id="animtimeline_editor",
    )

    assert core.commands.execute(
        "timeline.new", source=CommandSource.TOOLBAR
    ).accepted
    assert core.commands.execute(
        "timeline.add_keyframe", source=CommandSource.TOOLBAR
    ).accepted
    assert core.commands.execute(
        "timeline.stop", source=CommandSource.TOOLBAR
    ).accepted
    assert core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Space"))
    ).status is ShortcutRouteStatus.EXECUTED
    assert core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Delete"))
    ).status is ShortcutRouteStatus.EXECUTED

    assert calls == [
        "new_timeline",
        "add_keyframe",
        "stop",
        "play_pause",
        "delete_keyframe",
    ]

    blocked = core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Space"), text_input_active=True)
    )
    assert blocked.status is ShortcutRouteStatus.BLOCKED


def test_animation_new_commands_route_through_focused_panel_and_can_execute():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    enabled = {"animclip2d": True, "animfsm": True}
    clip_panel = SimpleNamespace(
        command_new_clip_document=lambda: calls.append("animclip2d.new") or True,
        can_new_clip_document=lambda: enabled["animclip2d"],
        command_delete_selected_frame=lambda: True,
        can_delete_selected_frame=lambda: False,
        command_toggle_preview=lambda: True,
        command_stop_preview=lambda: True,
        command_previous_frame=lambda: True,
        command_next_frame=lambda: True,
        command_clear_sequence=lambda: True,
        command_add_frame=lambda _frame_id: True,
        can_preview=lambda: True,
        can_add_frame=lambda _frame_id: True,
    )
    graph_methods = {
        name: (lambda _name=name: True)
        for name in (
            "can_edit_copy",
            "can_edit_cut",
            "can_edit_paste",
            "can_edit_delete",
            "can_edit_rename",
            "can_edit_duplicate",
            "command_edit_copy",
            "command_edit_cut",
            "command_edit_paste",
            "command_edit_delete",
            "command_edit_rename",
            "command_edit_duplicate",
            "can_graph_center_view",
            "can_graph_reset_zoom",
            "command_graph_center_view",
            "command_graph_reset_zoom",
            "can_graph_add_node",
            "command_graph_add_node",
            "can_graph_create_node",
            "command_graph_create_node",
            "can_graph_open_create",
            "command_graph_open_create",
            "can_graph_workspace_add",
            "command_graph_workspace_add",
        )
    }
    fsm_panel = SimpleNamespace(
        **graph_methods,
        command_new_fsm=lambda: calls.append("animfsm.new") or True,
        can_new_fsm=lambda: enabled["animfsm"],
        command_switch_mode=lambda mode: calls.append(
            ("animfsm.switch_mode", mode)
        )
        or True,
        can_switch_mode=lambda mode: enabled["animfsm"] and mode == "3d",
    )
    windows = SimpleNamespace(
        get_window_instance=lambda panel_id: {
            "animclip2d_editor": clip_panel,
            "animfsm_editor": fsm_panel,
        }.get(panel_id),
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.interaction_core.panels.register_type(
        "animclip2d_editor",
        _ANIMCLIP2D_PANEL_INTERACTION,
    )
    bootstrap.interaction_core.panels.register_type(
        "animfsm_editor",
        _ANIMFSM_PANEL_INTERACTION,
    )
    bootstrap.interaction_core.panels.bind_view(
        "animclip2d_editor",
        "animclip2d_editor",
        clip_panel,
    )
    bootstrap.interaction_core.panels.bind_view(
        "animfsm_editor",
        "animfsm_editor",
        fsm_panel,
    )
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )
    core = bootstrap.interaction_core

    core.focus.activate_panel(
        "animclip2d_editor",
        view_id="animclip2d_editor",
        document_id="animclip2d:test",
    )
    assert core.commands.can_execute("animclip2d.new")
    assert core.commands.execute("animclip2d.new", source=CommandSource.TOOLBAR).accepted
    enabled["animclip2d"] = False
    assert not core.commands.can_execute("animclip2d.new")

    core.focus.activate_panel(
        "animfsm_editor",
        view_id="animfsm_editor",
        document_id="animfsm:test",
    )
    assert core.commands.can_execute("animfsm.new")
    assert core.commands.execute("animfsm.new", source=CommandSource.TOOLBAR).accepted

    assert calls == ["animclip2d.new", "animfsm.new"]

def test_inspector_component_menu_and_shortcuts_share_command_handlers():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    actions = SimpleNamespace(
        can_copy=lambda _target=None: True,
        copy=lambda _target=None: calls.append("copy_component") or True,
        can_paste_values=lambda _target=None: True,
        paste_values=lambda _target=None: calls.append("paste_values") or True,
        can_paste_as_new=lambda _target=None: True,
        paste_as_new=lambda _target=None: calls.append("paste_as_new") or True,
        paste_default=lambda _target=None: calls.append("paste_default") or True,
        can_remove=lambda _target=None: True,
        remove=lambda _target=None: calls.append("remove_component") or True,
        can_reset=lambda _target=None: True,
        reset=lambda _target=None: calls.append("reset_component") or True,
        can_move_up=lambda _target=None: True,
        move_up=lambda _target=None: calls.append("move_component_up") or True,
        can_move_down=lambda _target=None: True,
        move_down=lambda _target=None: calls.append("move_component_down") or True,
        can_reorder=lambda *args: bool(args),
        reorder=lambda *args: calls.append(("reorder_components", args)) or True,
        can_open_script=lambda _target=None: True,
        open_script=lambda _target=None: calls.append("open_script") or True,
        can_add=lambda *args: bool(args),
        add=lambda *args: calls.append(("add_component", args)) or True,
        can_set_enabled=lambda *args: bool(args),
        set_enabled=lambda *args: calls.append(("set_component_enabled", args)) or True,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.interaction_core.prefabs = _PrefabCommandStub(calls)
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    bootstrap._inspector_component_actions = actions
    _bind_panel(
        bootstrap.interaction_core,
        "inspector",
        inspector_panel_interaction(
            lambda: bootstrap._inspector_component_actions
        ),
        SimpleNamespace(),
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )
    core = bootstrap.interaction_core
    core.focus.activate_panel("inspector", view_id="inspector")
    core.selection.select(
        SelectionTarget.component(42, 701, sub_kind="native"),
        owner_id="inspector",
    )

    for chord in ("Ctrl+C", "Ctrl+V", "Delete"):
        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse(chord))
        ).status is ShortcutRouteStatus.EXECUTED

    for command_id in (
        "component.open_script",
        "component.copy_properties",
        "component.paste_properties",
        "component.paste_as_new",
        "component.remove",
        "component.reset",
        "component.move_up",
        "component.move_down",
    ):
        assert core.commands.execute(
            command_id,
            source=CommandSource.CONTEXT_MENU,
        ).accepted

    assert core.commands.execute(
        "component.reorder",
        source=CommandSource.DRAG_DROP,
        payload={
            "object_ids": [42, 43],
            "dragged_component_ids": [701, 801],
            "target_component_ids": [702, 802],
            "insert_after": True,
        },
    ).accepted
    core.focus.activate_panel("project", view_id="project")
    assert core.commands.can_execute(
        "component.add",
        context=core.commands.context(
            source=CommandSource.DRAG_DROP,
            payload={
                "type_name": "BoxCollider",
                "is_native": True,
                "script_path": "",
                "target_component_id": 702,
                "insert_after": False,
                "insert_at_start": False,
            },
        ),
    )
    assert core.commands.execute(
        "component.add",
        source=CommandSource.DRAG_DROP,
        payload={
            "type_name": "BoxCollider",
            "is_native": True,
            "script_path": "",
            "target_component_id": 702,
            "insert_after": False,
            "insert_at_start": False,
        },
    ).accepted
    assert core.focus.snapshot.active_view_id == "project"
    core.focus.activate_panel("inspector", view_id="inspector")
    assert core.commands.execute(
        "component.set_enabled",
        source=CommandSource.API,
        payload={
            "targets": [{"object_id": 42, "component_id": 701}],
            "enabled": False,
            "is_native": True,
        },
    ).accepted
    assert core.commands.execute(
        "component.set_enabled",
        source=CommandSource.API,
        payload={
            "targets": [
                {"object_id": 42, "component_id": 701},
                {"object_id": 43, "component_id": 801},
            ],
            "enabled": True,
            "is_native": True,
        },
    ).accepted
    for command_id, action in (
        ("prefab.select_asset", "select"),
        ("prefab.open", "open"),
        ("prefab.apply", "apply"),
        ("prefab.revert", "revert"),
    ):
        assert core.commands.execute(
            command_id,
            source=CommandSource.CONTEXT_MENU,
            payload={"object_id": 42},
        ).accepted

    assert calls == [
        "copy_component",
        "paste_default",
        "remove_component",
        "open_script",
        "copy_component",
        "paste_values",
        "paste_as_new",
        "remove_component",
        "reset_component",
        "move_component_up",
        "move_component_down",
        (
            "reorder_components",
            ((42, 43), (701, 801), (702, 802), True),
        ),
        ("add_component", ("BoxCollider", True, "", 702, False, False)),
        ("set_component_enabled", ((42,), (701,), False, True)),
        ("set_component_enabled", ((42, 43), (701, 801), True, True)),
        ("prefab", 42, "select"),
        ("prefab", 42, "open"),
        ("prefab", 42, "apply"),
        ("prefab", 42, "revert"),
    ]


def test_inspector_component_context_payload_keeps_the_original_target():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    captured = []
    actions = SimpleNamespace(
        can_copy=lambda target=None: target is not None,
        copy=lambda target=None: captured.append(target) or True,
        can_paste_values=lambda _target=None: False,
        paste_values=lambda _target=None: False,
        can_paste_as_new=lambda _target=None: False,
        paste_as_new=lambda _target=None: False,
        paste_default=lambda _target=None: False,
        can_remove=lambda _target=None: False,
        remove=lambda _target=None: False,
        can_reset=lambda _target=None: False,
        reset=lambda _target=None: False,
        can_move_up=lambda _target=None: False,
        move_up=lambda _target=None: False,
        can_move_down=lambda _target=None: False,
        move_down=lambda _target=None: False,
        can_reorder=lambda *_args: False,
        reorder=lambda *_args: False,
        can_open_script=lambda _target=None: False,
        open_script=lambda _target=None: False,
        can_add=lambda *_args: False,
        add=lambda *_args: False,
        can_set_enabled=lambda *_args: False,
        set_enabled=lambda *_args: False,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    core = bootstrap.interaction_core
    _bind_panel(
        core,
        "inspector",
        inspector_panel_interaction(lambda: actions),
        SimpleNamespace(),
    )
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        SimpleNamespace(
            get_window_instance=lambda _panel_id: None,
            get_registered_types=lambda: {},
            reset_layout=lambda: None,
        ),
        SimpleNamespace(),
    )
    core.focus.activate_panel("inspector", view_id="inspector")
    core.selection.select(
        SelectionTarget.component(99, 901, sub_kind="script"),
        owner_id="inspector",
    )

    result = core.commands.execute(
        "component.copy_properties",
        source=CommandSource.CONTEXT_MENU,
        payload={"object_id": 42, "component_id": 701, "is_native": True},
    )

    assert result.accepted
    assert captured == [(42, 701, True)]


def test_console_copy_shortcut_routes_through_the_focused_panel_adapter():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    counts = {"info": 1, "warning": 0, "error": 0}
    options = {
        "show_info": True,
        "show_warnings": True,
        "show_errors": True,
        "collapse": False,
        "clear_on_play": True,
        "error_pause": False,
        "follow": True,
    }
    view = {"search": "", "detail_height": 90.0}

    def _clear():
        calls.append("clear")
        counts.update(info=0, warning=0, error=0)

    panel = SimpleNamespace(
        has_selected_entry=lambda: True,
        copy_selected_entry=lambda: calls.append("copy_log") or True,
        clear=_clear,
        get_info_count=lambda: counts["info"],
        get_warning_count=lambda: counts["warning"],
        get_error_count=lambda: counts["error"],
        has_view_option=lambda option: option in options,
        get_view_option=lambda option: options[option],
        set_view_option=lambda option, enabled: options.__setitem__(
            option, bool(enabled)
        ),
        get_search_query=lambda: view["search"],
        set_search_query=lambda value: view.__setitem__("search", str(value)),
        get_detail_height=lambda: view["detail_height"],
        set_detail_height=lambda value: view.__setitem__(
            "detail_height", float(value)
        ),
        request_search_focus=lambda: calls.append("focus_search"),
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    _bind_panel(
        bootstrap.interaction_core,
        "console",
        console_panel_interaction(),
        panel,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    BootstrapWiringMixin._register_core_editor_commands(
        bootstrap,
        windows,
        SimpleNamespace(),
    )
    core = bootstrap.interaction_core
    core.focus.activate_panel("console", view_id="console")
    core.selection.select(
        SelectionTarget.diagnostic_entry("console", "log:42"),
        owner_id="console",
    )

    result = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+C")))

    assert result.status is ShortcutRouteStatus.EXECUTED
    assert calls == ["copy_log"]

    result = core.shortcuts.route(
        ShortcutEvent(KeyChord.parse("Ctrl+F"), game_view_captured=True)
    )
    assert result.status is ShortcutRouteStatus.EXECUTED
    assert calls == ["copy_log", "focus_search"]

    result = core.commands.execute("console.clear", source=CommandSource.POINTER)

    assert result.accepted
    assert calls == ["copy_log", "focus_search", "clear"]
    assert not core.commands.can_execute(
        "console.clear", core.commands.context(CommandSource.POINTER)
    )


def test_console_view_changes_use_non_dirty_global_history():
    from Infernux.engine.undo import UndoManager

    class BootstrapHarness(BootstrapWiringMixin):
        pass

    previous_manager = UndoManager.instance()
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    core = bootstrap.interaction_core
    manager = UndoManager(core.action_journal)
    options = {
        "show_info": True,
        "show_warnings": True,
        "show_errors": True,
        "collapse": False,
        "clear_on_play": True,
        "error_pause": False,
        "follow": True,
    }
    view = {"search": "smoke", "detail_height": 90.0}
    panel = SimpleNamespace(
        has_selected_entry=lambda: False,
        copy_selected_entry=lambda: False,
        clear=lambda: None,
        get_info_count=lambda: 0,
        get_warning_count=lambda: 0,
        get_error_count=lambda: 0,
        has_view_option=lambda option: option in options,
        get_view_option=lambda option: options[option],
        set_view_option=lambda option, enabled: options.__setitem__(
            option, bool(enabled)
        ),
        get_search_query=lambda: view["search"],
        set_search_query=lambda value: view.__setitem__("search", str(value)),
        get_detail_height=lambda: view["detail_height"],
        set_detail_height=lambda value: view.__setitem__(
            "detail_height", float(value)
        ),
        request_search_focus=lambda: None,
    )
    try:
        _bind_panel(
            core,
            "console",
            console_panel_interaction(core.view_commands),
            panel,
        )
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            SimpleNamespace(
                get_window_instance=lambda _panel_id: panel,
                get_registered_types=lambda: {"console"},
                reset_layout=lambda: None,
            ),
            SimpleNamespace(),
        )
        core.focus.activate_panel("console", view_id="console")

        result = core.commands.execute(
            "console.set_option",
            source=CommandSource.POINTER,
            payload={"option": "collapse", "enabled": True},
        )
        assert result.accepted
        assert options["collapse"] is True
        assert manager.undo_description == "Collapse Console Entries"

        manager.undo()
        assert options["collapse"] is False
        manager.redo()
        assert options["collapse"] is True

        # Native text editing has already applied the live value when the
        # commit command arrives; history still captures its start value.
        view["search"] = "shader"
        result = core.commands.execute(
            "console.set_search",
            source=CommandSource.POINTER,
            payload={"old_value": "smoke", "new_value": "shader"},
        )
        assert result.accepted
        manager.undo()
        assert view["search"] == "smoke"
    finally:
        UndoManager._instance = previous_manager
        core.shutdown()


def test_scene_tool_shortcuts_share_commands_and_respect_camera_capture():
    from Infernux.engine.undo import UndoManager

    class BootstrapHarness(BootstrapWiringMixin):
        pass

    previous_manager = UndoManager.instance()
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    modes = []

    def _set_mode(mode):
        modes.append(int(mode))
        bootstrap.scene_view._gizmo_tool_mode = int(mode)

    bootstrap.scene_view = SimpleNamespace(
        _gizmo_tool_mode=0,
        _coord_space=0,
        _set_tool_mode=_set_mode,
        _set_coordinate_space=lambda value: setattr(
            bootstrap.scene_view, "_coord_space", int(value)
        ),
        _align_object_to_camera=lambda: True,
        can_frame_object_by_id=lambda object_id: int(object_id) == 42,
        frame_object_by_id=lambda _object_id: True,
    )
    scene_commands = _SceneCommandStub(lambda: None)
    _bind_panel(
        bootstrap.interaction_core,
        "scene_view",
        scene_view_panel_interaction(scene_commands),
        bootstrap.scene_view,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    manager = UndoManager(bootstrap.interaction_core.action_journal)
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        core = bootstrap.interaction_core
        core.focus.activate_panel("scene_view", view_id="scene_view", record_history=False)

        assert core.commands.execute(
            "scene.set_coordinate_space",
            source=CommandSource.POINTER,
            payload={"value": 1},
        ).accepted
        assert bootstrap.scene_view._coord_space == 1
        assert manager.undo_description == "Set Scene Coordinate Space"
        manager.undo()
        assert bootstrap.scene_view._coord_space == 0

        core.focus.set_capture_owner("scene_view.camera")

        blocked = core.shortcuts.route(ShortcutEvent(KeyChord.parse("W")))
        assert blocked.status is ShortcutRouteStatus.BLOCKED
        assert bootstrap.scene_view._gizmo_tool_mode == 0

        core.focus.set_capture_owner("")
        moved = core.shortcuts.route(ShortcutEvent(KeyChord.parse("W")))
        assert moved.status is ShortcutRouteStatus.EXECUTED
        assert bootstrap.scene_view._gizmo_tool_mode == 1
        assert manager.undo_description == "Select Move Tool"

        selected = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Q")))
        assert selected.status is ShortcutRouteStatus.EXECUTED
        assert bootstrap.scene_view._gizmo_tool_mode == 0

        manager.undo()
        assert bootstrap.scene_view._gizmo_tool_mode == 1
    finally:
        UndoManager._instance = previous_manager
        bootstrap.interaction_core.shutdown()


def test_ui_editor_nudge_shortcuts_share_panel_commands_and_respect_capture():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.ui_editor = SimpleNamespace(
        can_nudge_selected=lambda: True,
        command_nudge_selected=lambda dx, dy: calls.append((dx, dy)) or True,
    )
    _bind_panel(
        bootstrap.interaction_core,
        "ui_editor",
        ui_editor_panel_interaction(_SceneCommandStub(lambda: None)),
        bootstrap.ui_editor,
    )
    windows = SimpleNamespace(
        get_window_instance=lambda _panel_id: None,
        get_registered_types=lambda: {},
        reset_layout=lambda: None,
    )
    try:
        BootstrapWiringMixin._register_core_editor_commands(
            bootstrap,
            windows,
            SimpleNamespace(),
        )
        core = bootstrap.interaction_core
        core.focus.activate_panel("ui_editor", view_id="ui_editor", record_history=False)

        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse("Left"))
        ).status is ShortcutRouteStatus.EXECUTED
        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse("Shift+Down"))
        ).status is ShortcutRouteStatus.EXECUTED
        assert calls == [(-1, 0), (0, 10)]

        core.focus.set_capture_owner("ui_editor.drag")
        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse("Right"))
        ).status is ShortcutRouteStatus.BLOCKED
        assert calls == [(-1, 0), (0, 10)]
    finally:
        bootstrap.interaction_core.shutdown()
