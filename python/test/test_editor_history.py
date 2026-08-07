from Infernux.engine.interaction import (
    ActionOrigin,
    CommandSource,
    EditorActionJournal,
    EditorCommand,
    EditorCommandRegistry,
    EditorContextSnapshot,
    FocusService,
    FocusSnapshot,
    HistoryModel,
    SelectionService,
    SelectionSnapshot,
    SelectionTarget,
)
from Infernux.engine.undo import LambdaCommand, UndoManager
from Infernux.engine.ui.history_panel import HistoryPanel


def _command(description: str):
    return LambdaCommand(description, undo_fn=lambda: None, redo_fn=lambda: None)


def test_history_projection_preserves_order_and_filters_all_metadata():
    journal = EditorActionJournal()
    scene_selection = SelectionSnapshot.create(
        (SelectionTarget.scene_object(42),),
        owner_id="scene_view",
    )
    context = EditorContextSnapshot(
        focus=FocusSnapshot(active_panel_id="scene_view", active_view_id="scene_view"),
        selection=scene_selection,
    )
    journal.record(
        _command("Move Camera"),
        before_context=context,
        after_context=context,
        origin=ActionOrigin.USER,
        command_id="scene.move",
    )
    journal.record(
        _command("Rename Asset"),
        origin=ActionOrigin.AUTOMATION,
        command_id="project.rename",
    )
    journal.commit_undo(journal.peek_undo())
    model = HistoryModel(journal)

    snapshot = model.snapshot
    assert [row.description for row in snapshot.entries] == [
        "Move Camera",
        "Rename Asset",
    ]
    assert snapshot.entries[0].target == "scene_object:42"
    assert snapshot.entries[0].context == "scene_view"
    assert snapshot.entries[0].state == "applied"
    assert snapshot.entries[1].state == "redo"

    assert model.set_query("scene.move 42 applied")
    filtered = model.snapshot
    assert [row.description for row in filtered.entries] == ["Move Camera"]
    assert filtered.cursor == 1
    assert filtered.total == 2


def test_history_snapshot_rebuilds_only_when_query_or_journal_changes():
    journal = EditorActionJournal()
    model = HistoryModel(journal)
    first = model.snapshot
    assert model.snapshot is first

    journal.record(_command("Create Cube"), command_id="scene.create")
    second = model.snapshot
    assert second is not first
    assert model.snapshot is second

    model.set_query("cube")
    third = model.snapshot
    assert third is not second
    assert model.snapshot is third


def test_command_registry_records_command_identity_in_global_history():
    focus = FocusService()
    selection = SelectionService()
    journal = EditorActionJournal()
    manager = UndoManager(journal)
    commands = EditorCommandRegistry(focus=focus, selection=selection)
    commands.register(
        EditorCommand(
            "scene.create_cube",
            lambda _context: manager.execute(_command("Create Cube")),
            display_name="Create Cube",
            category="GameObject",
        )
    )

    result = commands.execute("scene.create_cube", source=CommandSource.MENU)

    assert result.accepted
    assert len(journal.entries) == 1
    assert journal.entries[0].command_id == "scene.create_cube"
    assert journal.entries[0].action.description == "Create Cube"


def test_history_panel_is_a_registered_nondocument_editor_surface():
    assert HistoryPanel.WINDOW_TYPE_ID == "history"
    assert HistoryPanel.WINDOW_TITLE_KEY == "panel.history"
    assert HistoryPanel.PANEL_INTERACTION is not None
    assert not HistoryPanel.PANEL_INTERACTION.document_backed
