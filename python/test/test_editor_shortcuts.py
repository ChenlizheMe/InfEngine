from __future__ import annotations

import pytest

from Infernux.engine.interaction import (
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    InputContext,
    KeyChord,
    SelectionService,
    ShortcutBinding,
    ShortcutEvent,
    ShortcutModifier,
    ShortcutPhase,
    ShortcutRouteStatus,
    ShortcutRouter,
    ShortcutScope,
)


def _router():
    focus = FocusService()
    selection = SelectionService()
    commands = EditorCommandRegistry(focus=focus, selection=selection)
    return ShortcutRouter(commands, focus), commands, focus


def test_key_chord_parses_aliases_into_one_normalized_identity():
    assert KeyChord.parse("control + shift + z") == KeyChord(
        "Z",
        ShortcutModifier.CTRL | ShortcutModifier.SHIFT,
    )
    assert KeyChord.parse("cmd+s").display_name() == "Super+S"
    with pytest.raises(ValueError):
        KeyChord.parse("Ctrl+S+Z")


def test_child_context_then_panel_then_global_binding_precedence():
    router, commands, focus = _router()
    executed = []
    for command_id in ("global", "panel", "child"):
        commands.register(EditorCommand(command_id, lambda _ctx, value=command_id: executed.append(value)))
    chord = KeyChord.parse("Delete")
    router.register(ShortcutBinding("global", chord, binding_id="global"))
    router.register(ShortcutBinding(
        "panel", chord, ShortcutScope.PANEL, "inspector", binding_id="panel"
    ))
    router.register(ShortcutBinding(
        "child", chord, ShortcutScope.CHILD_CONTEXT, "component.list", binding_id="child"
    ))
    focus.activate_panel("inspector", child_context_id="component.list")
    focus.input_contexts.push(InputContext("component.list", "inspector", 10))

    assert router.route(ShortcutEvent(chord)).command_id == "child"
    focus.set_child_context("inspector", "")
    focus.input_contexts.remove("component.list")
    assert router.route(ShortcutEvent(chord)).command_id == "panel"
    focus.activate_panel("scene")
    assert router.route(ShortcutEvent(chord)).command_id == "global"
    assert executed == ["child", "panel", "global"]


@pytest.mark.parametrize(
    ("event", "binding_flag"),
    [
        (ShortcutEvent(KeyChord.parse("Ctrl+S"), text_input_active=True), "allow_when_text_input"),
        (ShortcutEvent(KeyChord.parse("Ctrl+S"), modal_active=True), "allow_when_modal"),
        (ShortcutEvent(KeyChord.parse("Ctrl+S"), game_view_captured=True), "allow_when_captured"),
    ],
)
def test_text_modal_and_game_capture_block_by_default(event, binding_flag):
    router, commands, _focus = _router()
    executed = []
    commands.register(EditorCommand("file.save", lambda _ctx: executed.append(True)))
    router.register(ShortcutBinding("file.save", event.chord, binding_id="blocked"))

    assert router.route(event).status is ShortcutRouteStatus.BLOCKED
    assert executed == []

    router.unregister("blocked")
    router.register(ShortcutBinding(
        "file.save",
        event.chord,
        binding_id="allowed",
        **{binding_flag: True},
    ))
    assert router.route(event).status is ShortcutRouteStatus.EXECUTED
    assert executed == [True]


def test_capture_owner_in_focus_blocks_editor_binding_without_event_hint():
    router, commands, focus = _router()
    commands.register(EditorCommand("file.save", lambda _ctx: None))
    chord = KeyChord.parse("Ctrl+S")
    router.register(ShortcutBinding("file.save", chord))
    focus.set_capture_owner("game_view")

    assert router.route(ShortcutEvent(chord)).status is ShortcutRouteStatus.BLOCKED


def test_equal_context_conflict_is_reported_without_executing_either_command():
    router, commands, _focus = _router()
    executed = []
    commands.register(EditorCommand("edit.undo", lambda _ctx: executed.append("undo")))
    commands.register(EditorCommand("text.undo", lambda _ctx: executed.append("text")))
    chord = KeyChord.parse("Ctrl+Z")
    router.register(ShortcutBinding("edit.undo", chord, binding_id="undo"))
    router.register(ShortcutBinding("text.undo", chord, binding_id="text"))

    result = router.route(ShortcutEvent(chord))

    assert result.status is ShortcutRouteStatus.CONFLICT
    assert result.conflicts == ("edit.undo", "text.undo")
    assert executed == []


def test_phase_is_part_of_binding_identity_and_one_edge_executes_once():
    router, commands, _focus = _router()
    executed = []
    commands.register(EditorCommand("scene.fly", lambda _ctx: executed.append("press")))
    commands.register(EditorCommand("scene.fly.stop", lambda _ctx: executed.append("release")))
    chord = KeyChord.parse("W")
    router.register(ShortcutBinding("scene.fly", chord, phase=ShortcutPhase.PRESS))
    router.register(ShortcutBinding("scene.fly.stop", chord, phase=ShortcutPhase.RELEASE))

    assert router.route(ShortcutEvent(chord)).status is ShortcutRouteStatus.EXECUTED
    assert router.route(ShortcutEvent(chord, ShortcutPhase.REPEAT)).status is ShortcutRouteStatus.NO_MATCH
    assert router.route(ShortcutEvent(chord, ShortcutPhase.RELEASE)).status is ShortcutRouteStatus.EXECUTED
    assert executed == ["press", "release"]


def test_disabled_command_consumes_its_owned_shortcut_without_executing():
    router, commands, _focus = _router()
    commands.register(EditorCommand("edit.redo", lambda _ctx: None, can_execute=lambda _ctx: False))
    chord = KeyChord.parse("Ctrl+Shift+Z")
    router.register(ShortcutBinding("edit.redo", chord))

    result = router.route(ShortcutEvent(chord))

    assert result.status is ShortcutRouteStatus.DISABLED
    assert result.consumed is True


def test_shortcut_evaluates_command_enablement_once():
    router, commands, _focus = _router()
    checks = []
    commands.register(
        EditorCommand(
            "file.save",
            lambda _ctx: None,
            can_execute=lambda _ctx: checks.append(True) or True,
        )
    )
    chord = KeyChord.parse("Ctrl+S")
    router.register(ShortcutBinding("file.save", chord))

    result = router.route(ShortcutEvent(chord))

    assert result.status is ShortcutRouteStatus.EXECUTED
    assert checks == [True]
