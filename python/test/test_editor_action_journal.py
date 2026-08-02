from __future__ import annotations

import pytest

from Infernux.engine.interaction import (
    ActionOrigin,
    EditorActionJournal,
    EditorContextSnapshot,
)
from Infernux.engine.undo import UndoCommand, UndoManager


class ValueAction(UndoCommand):
    def __init__(self, state, old_value, new_value, description="Set Value"):
        super().__init__(description)
        self.state = state
        self.old_value = old_value
        self.new_value = new_value

    def execute(self):
        self.state["value"] = self.new_value

    def undo(self):
        self.state["value"] = self.old_value

    def can_merge(self, other):
        return isinstance(other, ValueAction) and self.state is other.state

    def merge(self, other):
        self.new_value = other.new_value


def test_action_journal_uses_one_cursor_and_discards_redo_branch():
    journal = EditorActionJournal()
    first = ValueAction({"value": 0}, 0, 1)
    second = ValueAction({"value": 0}, 0, 2)
    journal.record(first)
    journal.record(second)
    journal.commit_undo(journal.peek_undo())

    replacement = ValueAction({"value": 0}, 0, 3)
    result = journal.record(replacement)

    assert journal.cursor == 2
    assert [entry.action for entry in journal.entries] == [first, replacement]
    assert result.discarded_redo[0].action is second


def test_external_changes_never_enter_user_history():
    journal = EditorActionJournal()

    result = journal.record(
        ValueAction({"value": 0}, 0, 1),
        origin=ActionOrigin.EXTERNAL,
    )

    assert result.recorded is False
    assert journal.entries == ()


def test_merge_changes_dirty_signature_at_save_point():
    journal = EditorActionJournal()
    state = {"value": 0}
    journal.record(ValueAction(state, 0, 1))
    saved = journal.dirty_signature()

    result = journal.record(ValueAction(state, 1, 2))

    assert result.merged is True
    assert journal.dirty_signature() != saved


def test_undo_manager_restores_context_around_replay():
    manager = UndoManager()
    state = {"value": 0}
    context = EditorContextSnapshot()
    restored = []
    manager.set_context_hooks(
        lambda: context,
        lambda snapshot, phase: restored.append((snapshot, phase)),
    )
    manager.execute(ValueAction(state, 0, 4))

    manager.undo()

    assert state["value"] == 0
    assert [phase for _, phase in restored] == ["prepare_undo", "undo_complete"]


def test_undo_manager_executes_external_change_without_recording_it():
    manager = UndoManager()
    state = {"value": 0}

    assert manager.execute(
        ValueAction(state, 0, 4),
        origin=ActionOrigin.EXTERNAL,
    )

    assert state["value"] == 4
    assert manager.action_journal.entries == ()
    assert not manager.can_undo


def test_failed_undo_keeps_the_global_cursor_in_place():
    manager = UndoManager()
    state = {"value": 0}

    class FailingUndoAction(ValueAction):
        def undo(self):
            raise RuntimeError("undo failed")

    manager.execute(FailingUndoAction(state, 0, 1))
    manager.undo()

    assert state["value"] == 1
    assert manager.action_journal.cursor == 1
    assert manager.can_undo
    assert not manager.can_redo


def test_failed_redo_keeps_the_global_cursor_in_place():
    manager = UndoManager()
    state = {"value": 0}

    class FailingRedoAction(ValueAction):
        def redo(self):
            raise RuntimeError("redo failed")

    manager.execute(FailingRedoAction(state, 0, 1))
    manager.undo()
    manager.redo()

    assert state["value"] == 0
    assert manager.action_journal.cursor == 0
    assert not manager.can_undo
    assert manager.can_redo


def test_editor_transaction_rolls_back_on_failure():
    manager = UndoManager()
    state = {"value": 0}

    class FailingAction(ValueAction):
        def execute(self):
            raise RuntimeError("failed")

    with pytest.raises(RuntimeError, match="failed"):
        with manager.transaction("Atomic edit") as transaction:
            transaction.execute(ValueAction(state, 0, 1))
            transaction.execute(FailingAction(state, 1, 2))

    assert state["value"] == 0
    assert not manager.can_undo


def test_editor_transaction_records_one_action():
    manager = UndoManager()
    state_a = {"value": 0}
    state_b = {"value": 0}

    with manager.transaction("Two values") as transaction:
        transaction.execute(ValueAction(state_a, 0, 1))
        transaction.execute(ValueAction(state_b, 0, 2))

    assert len(manager.action_journal.entries) == 1
    manager.undo()
    assert state_a["value"] == 0
    assert state_b["value"] == 0
