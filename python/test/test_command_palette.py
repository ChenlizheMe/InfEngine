from pathlib import Path

from Infernux.engine.interaction import (
    COMMAND_PALETTE_CONTEXT_ID,
    COMMAND_PALETTE_MODAL_ID,
    CommandSource,
    CommandStatus,
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    KeyChord,
    ModalService,
    SelectionService,
    SelectionTarget,
    ShortcutBinding,
    ShortcutEvent,
    ShortcutRouteStatus,
    ShortcutRouter,
    ShortcutScope,
)
from Infernux.engine.interaction.command_palette import CommandPaletteService


def _palette():
    focus = FocusService()
    selection = SelectionService()
    commands = EditorCommandRegistry(focus=focus, selection=selection)
    shortcuts = ShortcutRouter(commands, focus)
    modals = ModalService()
    palette = CommandPaletteService(commands, shortcuts, focus, modals)
    palette.register_commands()
    modals.register(
        COMMAND_PALETTE_MODAL_ID,
        is_active=lambda: palette.is_active,
        render=lambda _ctx: None,
        cancel=palette.close,
    )
    return palette, commands, shortcuts, focus, selection, modals


def test_palette_filters_registry_metadata_and_hides_internal_commands():
    palette, commands, shortcuts, focus, _selection, _modals = _palette()
    commands.register(
        EditorCommand(
            "scene.frame_selected",
            lambda _context: True,
            display_name="Frame Selected",
            category="Scene",
            palette_keywords=("camera", "object"),
            creates_user_action=False,
        )
    )
    commands.register(
        EditorCommand(
            "internal.hidden",
            lambda _context: True,
            display_name="Hidden Command",
            palette_visible=False,
        )
    )
    shortcuts.register(
        ShortcutBinding(
            "scene.frame_selected",
            KeyChord.parse("F"),
            binding_id="scene.frame_selected",
        )
    )
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)

    assert palette.open(commands.context(CommandSource.SHORTCUT))
    assert palette.set_query("camera frame")

    assert tuple(entry.command_id for entry in palette.entries) == (
        "scene.frame_selected",
    )
    assert palette.entries[0].shortcut == "F"
    assert "internal.hidden" not in {
        entry.command_id for entry in palette.entries
    }


def test_palette_executes_against_the_context_that_opened_it():
    palette, commands, _shortcuts, focus, selection, modals = _palette()
    observed = []
    commands.register(
        EditorCommand(
            "scene.test_command",
            lambda context: observed.append(context) or True,
            display_name="Test Scene Command",
            category="Scene",
            creates_user_action=False,
        )
    )
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)
    selection.select(
        SelectionTarget.scene_object(42),
        owner_id="scene_view",
        record_history=False,
    )

    assert commands.execute(
        "command_palette.open",
        source=CommandSource.SHORTCUT,
    ).status is CommandStatus.EXECUTED
    assert COMMAND_PALETTE_CONTEXT_ID in {
        context.context_id for context in focus.input_contexts.ordered()
    }

    focus.activate_panel("project", view_id="project", record_history=False)
    selection.clear(record_history=False)
    result = palette.execute("scene.test_command")

    assert result.status is CommandStatus.EXECUTED
    assert observed[0].source is CommandSource.PALETTE
    assert observed[0].focus.active_panel_id == "scene_view"
    assert observed[0].selection.primary.target_id == "42"
    assert not palette.is_active
    assert modals.active_modal_id == ""
    assert COMMAND_PALETTE_CONTEXT_ID not in {
        context.context_id for context in focus.input_contexts.ordered()
    }


def test_palette_disables_unavailable_commands_without_closing():
    palette, commands, _shortcuts, focus, _selection, _modals = _palette()
    commands.register(
        EditorCommand(
            "scene.disabled",
            lambda _context: True,
            display_name="Disabled Scene Command",
            category="Scene",
            can_execute=lambda _context: False,
            disabled_reason=lambda _context: "Select a scene object",
            creates_user_action=False,
        )
    )
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)
    assert palette.open(commands.context())

    entry = next(item for item in palette.entries if item.command_id == "scene.disabled")
    result = palette.execute("scene.disabled")

    assert not entry.enabled
    assert entry.disabled_reason == "Select a scene object"
    assert result.status is CommandStatus.DISABLED
    assert palette.is_active


def test_palette_navigation_shortcuts_win_inside_modal_text_input():
    palette, commands, shortcuts, focus, _selection, _modals = _palette()
    for index in range(3):
        commands.register(
            EditorCommand(
                f"test.command_{index}",
                lambda _context: True,
                display_name=f"Command {index}",
                category="Test",
                creates_user_action=False,
            )
        )
    shortcuts.register(
        ShortcutBinding(
            "command_palette.next",
            KeyChord.parse("Down"),
            ShortcutScope.CHILD_CONTEXT,
            owner_id=COMMAND_PALETTE_CONTEXT_ID,
            allow_when_text_input=True,
            allow_when_modal=True,
            binding_id="palette.next",
        )
    )
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)
    assert palette.open(commands.context())

    result = shortcuts.route(
        ShortcutEvent(
            KeyChord.parse("Down"),
            text_input_active=True,
            modal_active=True,
        )
    )

    assert result.status is ShortcutRouteStatus.EXECUTED
    assert palette.selected_index == 1


def test_palette_enter_forwarder_returns_its_own_command_result():
    palette, commands, _shortcuts, focus, _selection, _modals = _palette()
    executed = []
    commands.register(
        EditorCommand(
            "window.preferences",
            lambda context: executed.append(context.source) or True,
            display_name="Preferences",
            category="Window",
            creates_user_action=False,
        )
    )
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)
    assert palette.open(commands.context(CommandSource.SHORTCUT))
    assert palette.set_query("Preferences")

    result = commands.execute(
        "command_palette.execute",
        source=CommandSource.SHORTCUT,
    )

    assert result.command_id == "command_palette.execute"
    assert result.status is CommandStatus.EXECUTED
    assert result.value.command_id == "window.preferences"
    assert result.value.status is CommandStatus.EXECUTED
    assert executed == [CommandSource.PALETTE]
    assert not palette.is_active


def test_palette_enter_remains_owned_while_active_with_no_matching_rows():
    palette, commands, _shortcuts, focus, _selection, _modals = _palette()
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)
    assert palette.open(commands.context(CommandSource.SHORTCUT))
    assert palette.set_query("definitely-not-a-command")
    assert palette.entries == ()

    context = commands.context(CommandSource.SHORTCUT)
    assert commands.can_execute("command_palette.execute", context)
    result = commands.execute(
        "command_palette.execute",
        source=CommandSource.SHORTCUT,
    )

    assert result.command_id == "command_palette.execute"
    assert result.status is CommandStatus.NO_OP
    assert palette.is_active


def test_native_shortcut_pump_publishes_palette_open_and_execute_edges():
    source = (
        Path(__file__).parents[2]
        / "cpp"
        / "infernux"
        / "function"
        / "editor"
        / "EditorShortcutInput.cpp"
    ).read_text(encoding="utf-8")

    assert 'dispatch("Ctrl+Shift+P")' in source
    assert 'dispatch("Enter")' in source
