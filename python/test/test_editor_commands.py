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
