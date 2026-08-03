from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.engine._bootstrap_wiring import BootstrapWiringMixin
from Infernux.engine.interaction import (
    CommandResult,
    CommandSource,
    CommandStatus,
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    InputContext,
    SelectionService,
    SelectionTarget,
    EditorInteractionCore,
    KeyChord,
    ShortcutEvent,
    ShortcutRouteStatus,
)


def _registry():
    focus = FocusService()
    selection = SelectionService()
    return EditorCommandRegistry(focus=focus, selection=selection), focus, selection


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
        is_window_open=lambda target_id: target_id == "console",
        reset_layout=lambda: calls.append("reset_layout"),
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
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
        begin_rename_object=lambda object_id: calls.append(("rename", object_id)),
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

    copied = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+C")))
    deleted = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Delete")))
    renamed = core.shortcuts.route(ShortcutEvent(KeyChord.parse("F2")))

    assert copied.status is ShortcutRouteStatus.EXECUTED
    assert deleted.status is ShortcutRouteStatus.EXECUTED
    assert renamed.status is ShortcutRouteStatus.EXECUTED
    assert calls == [("copy", False), "delete", ("rename", 42)]

    core.focus.activate_panel("scene_view", view_id="scene_view")
    pasted = core.shortcuts.route(ShortcutEvent(KeyChord.parse("Ctrl+V")))
    assert pasted.status is ShortcutRouteStatus.EXECUTED
    assert calls[-1] == "paste"


def test_project_edit_shortcuts_use_the_same_commands_as_hierarchy():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = SimpleNamespace(
        has_selected_assets=lambda: True,
        copy_selected_assets=lambda cut: calls.append(("copy_asset", cut)) or True,
        paste_assets=lambda: calls.append("paste_asset") or True,
        request_delete_selected_assets=lambda: calls.append("delete_asset") or True,
        begin_rename_selected_asset=lambda path="": calls.append(("rename_asset", path)) or True,
        can_rename_selected_asset=lambda path="": True,
        can_paste_assets=lambda: True,
        create_folder_from_command=lambda: calls.append("create_folder") or True,
        get_current_path=lambda: "C:/Project/Assets",
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

    for chord in ("Ctrl+C", "Ctrl+X", "Ctrl+V", "Delete", "F2", "Ctrl+Shift+N"):
        assert core.shortcuts.route(
            ShortcutEvent(KeyChord.parse(chord))
        ).status is ShortcutRouteStatus.EXECUTED

    assert calls == [
        ("copy_asset", False),
        ("copy_asset", True),
        "paste_asset",
        "delete_asset",
        ("rename_asset", ""),
        "create_folder",
    ]


def test_timeline_toolbar_and_shortcuts_share_command_handlers():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    panel = SimpleNamespace(
        command_new_timeline=lambda: calls.append("new_timeline") or True,
        command_toggle_playback=lambda: calls.append("play_pause") or True,
        command_stop_playback=lambda: calls.append("stop") or True,
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


def test_inspector_component_menu_and_shortcuts_share_command_handlers():
    class BootstrapHarness(BootstrapWiringMixin):
        pass

    calls = []
    actions = SimpleNamespace(
        can_copy=lambda: True,
        copy=lambda: calls.append("copy_component") or True,
        can_paste_values=lambda: True,
        paste_values=lambda: calls.append("paste_values") or True,
        can_paste_as_new=lambda: True,
        paste_as_new=lambda: calls.append("paste_as_new") or True,
        paste_default=lambda: calls.append("paste_default") or True,
        can_remove=lambda: True,
        remove=lambda: calls.append("remove_component") or True,
        can_move_up=lambda: True,
        move_up=lambda: calls.append("move_component_up") or True,
        can_move_down=lambda: True,
        move_down=lambda: calls.append("move_component_down") or True,
        can_reorder=lambda *args: bool(args),
        reorder=lambda *args: calls.append(("reorder_components", args)) or True,
        can_open_script=lambda: True,
        open_script=lambda: calls.append("open_script") or True,
    )
    bootstrap = BootstrapHarness()
    bootstrap.interaction_core = EditorInteractionCore()
    bootstrap.engine = SimpleNamespace(_play_mode_manager=None)
    bootstrap.hierarchy = None
    bootstrap.project_panel = None
    bootstrap._inspector_component_actions = actions
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
            "object_id": 42,
            "dragged_component_id": 701,
            "target_component_id": 702,
            "insert_after": True,
        },
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
        "move_component_up",
        "move_component_down",
        ("reorder_components", (42, 701, 702, True)),
    ]
