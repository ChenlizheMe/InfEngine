from __future__ import annotations

import pytest

from Infernux.engine.interaction import (
    ActionOrigin,
    ContextRestoreStatus,
    DocumentKey,
    DocumentKind,
    DocumentLocator,
    EditorActionJournal,
    EditorContextSnapshot,
    FocusSnapshot,
    SelectionSnapshot,
    SelectionTarget,
    WindowLocator,
)


class _DisposableAction:
    marks_dirty = False

    def __init__(self, name: str, disposed: list[str], *, merge: bool = False):
        self.name = name
        self.disposed = disposed
        self.merge_enabled = merge

    def can_merge(self, _other) -> bool:
        return self.merge_enabled

    def merge(self, _other) -> None:
        pass

    def dispose(self) -> None:
        self.disposed.append(self.name)


def test_editor_context_rejects_non_selection_payloads():
    with pytest.raises(TypeError, match="SelectionSnapshot"):
        EditorContextSnapshot().with_selection({"component": 7})

    snapshot = SelectionSnapshot()
    assert EditorContextSnapshot().with_selection(snapshot).selection == snapshot


def test_editor_context_selection_replacement_preserves_document_locator():
    locator = DocumentLocator(
        "particle-smoke",
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, "Assets/Smoke.particlegraph"),
        resource_path="Assets/Smoke.particlegraph",
        title="Smoke",
    )
    window = WindowLocator("particle/Smoke", "particle_graph_editor")
    scene = DocumentLocator(
        "scene-main",
        DocumentKey.resource(DocumentKind.SCENE, "Assets/Main.scene"),
        resource_path="Assets/Main.scene",
        title="Main",
    )
    context = EditorContextSnapshot(document=locator, window=window, scene=scene)

    replaced = context.with_selection(SelectionSnapshot())

    assert replaced.document == locator
    assert replaced.window == window
    assert replaced.scene == scene


from Infernux.engine.undo import UndoCommand, UndoManager


def test_undo_manager_exposes_only_the_global_journal_history():
    manager = UndoManager(EditorActionJournal())

    assert manager.action_journal is not None
    assert not hasattr(manager, "_undo_stack")
    assert not hasattr(manager, "_redo_stack")


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


def test_action_journal_disposes_redo_branch_overflow_and_clear():
    disposed = []
    journal = EditorActionJournal(max_entries=2)
    first = _DisposableAction("first", disposed)
    second = _DisposableAction("second", disposed)
    third = _DisposableAction("third", disposed)
    replacement = _DisposableAction("replacement", disposed)

    journal.record(first)
    journal.record(second)
    journal.record(third)
    assert disposed == ["first"]

    journal.commit_undo(journal.peek_undo())
    journal.record(replacement)
    assert disposed == ["first", "third"]

    journal.clear()
    assert disposed == ["first", "third", "second", "replacement"]


def test_action_journal_disposes_action_absorbed_by_merge():
    disposed = []
    journal = EditorActionJournal()
    journal.record(_DisposableAction("stored", disposed, merge=True))

    result = journal.record(_DisposableAction("incoming", disposed))

    assert result.merged
    assert disposed == ["incoming"]


def test_external_changes_never_enter_user_history():
    journal = EditorActionJournal()

    result = journal.record(
        ValueAction({"value": 0}, 0, 1),
        origin=ActionOrigin.EXTERNAL,
    )

    assert result.recorded is False
    assert journal.entries == ()


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


def test_failed_prepare_restore_does_not_replay_or_move_cursor():
    manager = UndoManager()
    state = {"value": 0}
    context = EditorContextSnapshot()
    manager.set_context_hooks(
        lambda: context,
        lambda _snapshot, phase: phase != "prepare_undo",
    )
    manager.execute(ValueAction(state, 0, 4))
    cursor = manager.action_journal.cursor

    manager.undo()

    assert state["value"] == 4
    assert manager.action_journal.cursor == cursor
    assert manager.can_undo


def test_unrestorable_entry_is_discarded_and_undo_continues_to_previous_action():
    manager = UndoManager()
    state = {"value": 0}
    valid_context = EditorContextSnapshot()
    discarded_context = EditorContextSnapshot()
    restore_calls = []

    def restore(snapshot, phase):
        restore_calls.append((snapshot, phase))
        if snapshot is discarded_context and phase == "prepare_undo":
            return ContextRestoreStatus.DISCARD
        return ContextRestoreStatus.READY

    manager.set_context_hooks(lambda: valid_context, restore)
    manager.execute(ValueAction(state, 0, 4), transaction_id="valid")
    manager.set_context_hooks(lambda: discarded_context, restore)
    manager.execute(ValueAction(state, 4, 9), transaction_id="discarded")
    manager.set_context_hooks(lambda: valid_context, restore)

    manager.undo()

    assert state["value"] == 0
    assert manager.action_journal.cursor == 0
    assert len(manager.action_journal.entries) == 1
    assert not manager.can_undo
    assert manager.can_redo
    assert restore_calls[0] == (discarded_context, "prepare_undo")


def test_pending_prepare_waits_without_replaying_or_moving_cursor():
    manager = UndoManager()
    state = {"value": 0}
    context = EditorContextSnapshot()
    ready = {"value": False}
    undo_calls = []

    class CountingAction(ValueAction):
        def undo(self):
            undo_calls.append("undo")
            super().undo()

    def restore(_snapshot, phase):
        if phase == "prepare_undo" and not ready["value"]:
            return ContextRestoreStatus.PENDING
        return ContextRestoreStatus.READY

    manager.set_context_hooks(lambda: context, restore)
    manager.execute(CountingAction(state, 0, 4))

    manager.undo()
    manager.undo()
    manager.process_pending_replay()

    assert state["value"] == 4
    assert undo_calls == []
    assert manager.action_journal.cursor == 1
    assert manager.is_replay_pending

    ready["value"] = True
    assert manager.process_pending_replay() is ContextRestoreStatus.READY
    assert state["value"] == 0
    assert undo_calls == ["undo"]
    assert manager.action_journal.cursor == 0
    assert not manager.is_replay_pending


def test_pending_final_restore_executes_action_once_then_commits_cursor():
    manager = UndoManager()
    state = {"value": 0}
    context = EditorContextSnapshot()
    final_ready = {"value": False}
    undo_calls = []

    class CountingAction(ValueAction):
        def undo(self):
            undo_calls.append("undo")
            super().undo()

    def restore(_snapshot, phase):
        if phase == "undo_complete" and not final_ready["value"]:
            return ContextRestoreStatus.PENDING
        return ContextRestoreStatus.READY

    manager.set_context_hooks(lambda: context, restore)
    manager.execute(CountingAction(state, 0, 4))

    manager.undo()
    manager.process_pending_replay()

    assert state["value"] == 0
    assert undo_calls == ["undo"]
    assert manager.action_journal.cursor == 1
    assert manager.is_replay_pending

    final_ready["value"] = True
    manager.process_pending_replay()
    assert undo_calls == ["undo"]
    assert manager.action_journal.cursor == 0
    assert not manager.is_replay_pending


def test_failed_final_restore_compensates_data_and_keeps_cursor():
    manager = UndoManager()
    state = {"value": 0}
    context = EditorContextSnapshot()
    calls = []

    class CountingAction(ValueAction):
        def undo(self):
            calls.append("undo")
            super().undo()

        def redo(self):
            calls.append("redo")
            super().redo()

    def restore(_snapshot, phase):
        if phase == "undo_complete":
            return ContextRestoreStatus.FAILED
        return ContextRestoreStatus.READY

    manager.set_context_hooks(lambda: context, restore)
    manager.execute(CountingAction(state, 0, 4))

    manager.undo()

    assert state["value"] == 4
    assert calls == ["undo", "redo"]
    assert manager.action_journal.cursor == 1
    assert not manager.is_replay_pending


def test_prepare_restore_exception_does_not_replay_or_move_cursor():
    manager = UndoManager()
    state = {"value": 0}
    context = EditorContextSnapshot()

    def fail_restore(_snapshot, phase):
        if phase == "prepare_undo":
            raise RuntimeError("document unavailable")

    manager.set_context_hooks(lambda: context, fail_restore)
    manager.execute(ValueAction(state, 0, 4))
    cursor = manager.action_journal.cursor

    manager.undo()

    assert state["value"] == 4
    assert manager.action_journal.cursor == cursor


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


def test_user_action_groups_multiple_commands_into_one_global_step():
    manager = UndoManager()
    state_a = {"value": 0}
    state_b = {"value": 0}

    with manager.user_action("Batch edit"):
        manager.execute(ValueAction(state_a, 0, 1))
        manager.execute(ValueAction(state_b, 0, 2))

    assert len(manager.action_journal.entries) == 1
    assert manager.undo_description == "Batch edit"
    manager.undo()
    assert state_a["value"] == 0
    assert state_b["value"] == 0
    assert not manager.can_undo


def test_context_only_user_action_is_one_replayable_history_entry():
    manager = UndoManager()
    before = EditorContextSnapshot(
        FocusSnapshot(active_panel_id="scene_view"),
        SelectionSnapshot.create(
            (SelectionTarget.scene_object(7),),
            owner_id="scene_view",
        ),
    )
    after = EditorContextSnapshot(
        FocusSnapshot(active_panel_id="project"),
        SelectionSnapshot.create(
            (SelectionTarget.asset("Assets/Test.mat"),),
            owner_id="project",
        ),
    )
    current = {"context": before}

    phases = []

    def restore(snapshot, phase):
        phases.append(phase)
        current["context"] = snapshot

    manager.set_context_hooks(lambda: current["context"], restore)
    with manager.user_action("Select Project Asset"):
        current["context"] = after

    assert len(manager.action_journal.entries) == 1
    assert manager.undo_description == "Select Project Asset"
    manager.undo()
    assert current["context"] == before
    manager.redo()
    assert current["context"] == after
    assert phases == [
        "prepare_undo",
        "undo_complete",
        "prepare_redo",
        "redo_complete",
    ]


def test_user_action_uses_outer_context_for_data_command():
    manager = UndoManager()
    before = EditorContextSnapshot(FocusSnapshot(active_panel_id="hierarchy"))
    after = EditorContextSnapshot(FocusSnapshot(active_panel_id="inspector"))
    current = {"context": before}
    state = {"value": 0}
    manager.set_context_hooks(
        lambda: current["context"],
        lambda snapshot, _phase: current.__setitem__("context", snapshot),
    )

    with manager.user_action("Edit and focus"):
        current["context"] = after
        manager.execute(ValueAction(state, 0, 1))

    entry = manager.action_journal.peek_undo()
    assert entry.before_context == before
    assert entry.after_context == after
    assert len(manager.action_journal.entries) == 1


def test_focus_transition_is_never_grouped_with_following_data_command():
    from Infernux.engine.undo import GlobalFocusCommand

    manager = UndoManager()
    hierarchy = EditorContextSnapshot(FocusSnapshot(active_panel_id="hierarchy"))
    graph = EditorContextSnapshot(
        FocusSnapshot(
            active_panel_id="particle_graph_editor",
            active_view_id="particle_graph_editor/Smoke",
        )
    )
    current = {"context": hierarchy}
    state = {"value": 0}
    manager.set_context_hooks(
        lambda: current["context"],
        lambda snapshot, _phase: current.__setitem__("context", snapshot),
    )

    with manager.user_action("Remove Particle Graph parameter"):
        current["context"] = graph
        manager.record(
            GlobalFocusCommand(hierarchy.focus, graph.focus),
            before_context=hierarchy,
            after_context=graph,
        )
        manager.execute(ValueAction(state, 0, 1))

    entries = manager.action_journal.entries
    assert len(entries) == 2
    assert isinstance(entries[0].action, GlobalFocusCommand)
    assert isinstance(entries[1].action, ValueAction)

    manager.undo()
    assert state["value"] == 0
    assert current["context"].focus == graph.focus
    manager.undo()
    assert current["context"].focus == hierarchy.focus


def test_scene_transform_undo_precedes_workspace_and_selection_restore():
    from Infernux.engine.undo import GlobalFocusCommand, GlobalSelectionCommand

    manager = UndoManager()
    empty = SelectionSnapshot()
    selected = SelectionSnapshot.create(
        (SelectionTarget.scene_object(42),),
        owner_id="hierarchy",
    )
    graph_focus = FocusSnapshot(
        active_panel_id="particle_graph_editor",
        active_view_id="particle_graph_editor",
    )
    scene_focus = FocusSnapshot(
        active_panel_id="scene_view",
        active_view_id="scene_view",
    )
    graph_empty = EditorContextSnapshot(graph_focus, empty)
    graph_selected = EditorContextSnapshot(graph_focus, selected)
    scene_selected = EditorContextSnapshot(scene_focus, selected)
    current = {"context": graph_empty}
    transform = {"value": 0}
    manager.set_context_hooks(
        lambda: current["context"],
        lambda snapshot, _phase: current.__setitem__("context", snapshot),
    )

    manager.record(
        GlobalSelectionCommand(empty, selected),
        before_context=graph_empty,
        after_context=graph_selected,
    )
    current["context"] = graph_selected
    manager.record(
        GlobalFocusCommand(graph_focus, scene_focus),
        before_context=graph_selected,
        after_context=scene_selected,
    )
    current["context"] = scene_selected
    transform["value"] = 3
    transform_command = ValueAction(transform, 0, 3)
    transform_command.before_selection_snapshot = selected
    transform_command.after_selection_snapshot = selected
    manager.record(
        transform_command,
        before_context=scene_selected,
        after_context=scene_selected,
    )

    assert [entry.action.description for entry in manager.action_journal.entries] == [
        "Change Selection",
        "Change Editor Focus",
        "Set Value",
    ]

    manager.undo()
    assert transform["value"] == 0
    assert current["context"] == scene_selected
    manager.undo()
    assert current["context"] == graph_selected
    manager.undo()
    assert current["context"] == graph_empty


def test_action_origin_scope_is_inherited_by_nested_editor_commands():
    from Infernux.engine.interaction import action_origin_scope, current_action_origin

    manager = UndoManager()
    state = {"value": 0}

    assert current_action_origin() is ActionOrigin.USER
    with action_origin_scope(ActionOrigin.AUTOMATION):
        assert current_action_origin() is ActionOrigin.AUTOMATION
        with manager.user_action("Automation batch"):
            manager.execute(ValueAction(state, 0, 1, "Automation One"))
            manager.execute(ValueAction(state, 1, 2, "Automation Two"))

    assert current_action_origin() is ActionOrigin.USER
    assert state["value"] == 2
    assert all(
        entry.origin is ActionOrigin.AUTOMATION
        for entry in manager.action_journal.applied_entries()
    )
