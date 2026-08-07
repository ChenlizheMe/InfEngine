from types import SimpleNamespace

from Infernux.engine._bootstrap_panels import BootstrapPanelsMixin
from Infernux.engine.interaction import (
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    KeyChord,
    SelectionService,
    ShortcutBinding,
    ShortcutEvent,
    ShortcutRouteStatus,
    ShortcutRouter,
    ShortcutScope,
    TransientInteractionService,
)


def _interaction_stack():
    focus = FocusService()
    selection = SelectionService()
    commands = EditorCommandRegistry(focus=focus, selection=selection)
    shortcuts = ShortcutRouter(commands, focus)
    transients = TransientInteractionService(focus)
    focus.activate_panel("particle_graph_editor", record_history=False)
    commands.register(
        EditorCommand(
            "interaction.cancel",
            lambda _context: transients.cancel_active(),
            can_execute=lambda _context: transients.can_cancel,
        )
    )
    shortcuts.register(
        ShortcutBinding(
            "interaction.cancel",
            KeyChord.parse("Escape"),
            ShortcutScope.CHILD_CONTEXT,
            transients.CONTEXT_ID,
            priority=10_000,
            allow_when_text_input=True,
            allow_when_modal=True,
            allow_when_captured=True,
            binding_id="test.transient.cancel",
        )
    )
    return focus, shortcuts, transients


def test_transient_escape_cancels_only_the_top_interaction():
    focus, shortcuts, transients = _interaction_stack()
    cancelled = []
    transients.begin(
        "particle_graph_editor",
        lambda: cancelled.append("outer"),
        kind="outer",
        token_id="outer",
    )
    transients.begin(
        "particle_graph_editor",
        lambda: cancelled.append("inner"),
        kind="inner",
        token_id="inner",
    )

    first = shortcuts.route(
        ShortcutEvent(
            KeyChord.parse("Escape"),
            text_input_active=True,
            modal_active=True,
        )
    )
    second = shortcuts.route(ShortcutEvent(KeyChord.parse("Escape")))

    assert first.status is ShortcutRouteStatus.EXECUTED
    assert second.status is ShortcutRouteStatus.EXECUTED
    assert cancelled == ["inner", "outer"]
    assert transients.active is None
    assert focus.snapshot.child_context_id == ""


def test_transient_context_restores_the_previous_child_context():
    focus, _shortcuts, transients = _interaction_stack()
    focus.set_child_context(
        "particle_graph_editor",
        "particle.canvas",
        record_history=False,
    )

    token = transients.begin(
        "particle_graph_editor",
        lambda: None,
        kind="rename",
    )
    assert focus.snapshot.child_context_id == transients.CONTEXT_ID

    assert transients.end(token)
    assert focus.snapshot.child_context_id == "particle.canvas"


def test_transient_context_is_not_persisted_in_action_history():
    focus, _shortcuts, transients = _interaction_stack()
    focus.set_child_context(
        "particle_graph_editor",
        "particle.parameters",
        record_history=False,
    )
    transients.begin(
        "particle_graph_editor",
        lambda: None,
        kind="rename",
    )
    focus.set_capture_owner("particle.rename")

    persistent = transients.persistent_focus_snapshot()

    assert persistent.active_panel_id == "particle_graph_editor"
    assert persistent.child_context_id == "particle.parameters"
    assert persistent.capture_owner_id == ""


def test_focus_change_cancels_transient_owner_once():
    core_focus, _shortcuts, transients = _interaction_stack()
    cancelled = []
    transients.begin(
        "particle_graph_editor",
        lambda: cancelled.append("rename"),
        kind="rename",
    )

    assert transients.cancel_owner("particle_graph_editor") == 1
    core_focus.activate_panel("project", record_history=False)

    assert cancelled == ["rename"]
    assert not transients.can_cancel


def test_native_panel_bridge_projects_and_cancels_one_local_token():
    focus = FocusService()
    focus.activate_panel("project", record_history=False)
    transients = TransientInteractionService(focus)

    class NativePanelStub:
        on_transient_begin = None
        on_transient_end = None

        def __init__(self):
            self.cancelled = []

        def cancel_transient(self, token):
            self.cancelled.append(token)
            self.on_transient_end(token)
            return True

    bootstrap = BootstrapPanelsMixin()
    bootstrap.interaction_core = SimpleNamespace(
        transient_interactions=transients,
    )
    panel = NativePanelStub()
    bootstrap._wire_native_transient_interactions(panel, "project")

    panel.on_transient_begin("rename", "inline_rename", 100)

    assert transients.active is not None
    assert transients.active.token_id == "native:project:rename"
    assert transients.active.owner_id == "project"
    assert focus.snapshot.child_context_id == transients.CONTEXT_ID

    assert transients.cancel_active()
    assert panel.cancelled == ["rename"]
    assert transients.active is None
    assert focus.snapshot.child_context_id == ""
