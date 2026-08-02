from types import SimpleNamespace

from Infernux.core.animation_timeline import AnimationTimeline, TimelineKeyframe
from Infernux.engine.interaction import (
    DocumentCapability,
    DocumentKind,
    EditorActionJournal,
    EditorContextSnapshot,
    SelectionSnapshot,
    SelectionService,
    SelectionTarget,
)
from Infernux.engine.undo import (
    TimelineInsertKeyframeCommand,
    TimelinePropertyCommand,
    TimelineRemoveKeyframeCommand,
    UndoManager,
)


def _open_timeline(registry, timeline):
    controller = SimpleNamespace(_timeline=timeline)
    document = registry.create(
        DocumentKind.TIMELINE,
        "Timeline",
        document_id="timeline:test",
        capabilities=DocumentCapability.SAVE,
        controller=controller,
    )
    return document


def test_timeline_insert_is_exact_and_restores_selection_context(
    _reset_editor_interaction_state,
):
    registry = _reset_editor_interaction_state
    timeline = AnimationTimeline()
    document = _open_timeline(registry, timeline)
    manager = UndoManager(EditorActionJournal())
    manager.set_context_hooks(lambda: EditorContextSnapshot(), None)
    key = TimelineKeyframe(time=0.75)
    before_selection = SelectionSnapshot()
    after_selection = SelectionSnapshot.create(
        (
            SelectionTarget.timeline_element(
                document.document_id,
                key.stable_id,
                sub_kind="keyframe",
            ),
        ),
        owner_id="animtimeline_editor",
    )
    after_revision = registry.reserve_content_revision(document.document_id)
    command = TimelineInsertKeyframeCommand(
        timeline,
        document.document_id,
        key,
        0,
        document.revision,
        after_revision,
    )
    command.before_selection_snapshot = before_selection
    command.after_selection_snapshot = after_selection

    assert manager.execute(command)
    assert timeline.find_keyframe(key.stable_id) is not None
    assert document.revision == after_revision
    assert manager.action_journal.entries[0].after_context.selection == after_selection

    manager.undo()
    assert timeline.find_keyframe(key.stable_id) is None
    assert document.revision == 0

    manager.redo()
    assert timeline.find_keyframe(key.stable_id) is not None
    assert document.revision == after_revision


def test_timeline_remove_restores_original_index(_reset_editor_interaction_state):
    registry = _reset_editor_interaction_state
    first = TimelineKeyframe(time=0.0)
    removed = TimelineKeyframe(time=1.0)
    last = TimelineKeyframe(time=2.0)
    timeline = AnimationTimeline(keyframes=[first, removed, last])
    document = _open_timeline(registry, timeline)
    manager = UndoManager(EditorActionJournal())
    after_revision = registry.reserve_content_revision(document.document_id)

    assert manager.execute(
        TimelineRemoveKeyframeCommand(
            timeline,
            document.document_id,
            removed,
            1,
            document.revision,
            after_revision,
        )
    )
    assert [key.stable_id for key in timeline.keyframes] == [
        first.stable_id,
        last.stable_id,
    ]

    manager.undo()
    assert [key.stable_id for key in timeline.keyframes] == [
        first.stable_id,
        removed.stable_id,
        last.stable_id,
    ]


def test_timeline_property_edits_merge_and_cross_save_points(
    _reset_editor_interaction_state,
):
    registry = _reset_editor_interaction_state
    key = TimelineKeyframe(time=1.0)
    timeline = AnimationTimeline(keyframes=[key])
    document = _open_timeline(registry, timeline)
    manager = UndoManager(EditorActionJournal())

    first_revision = registry.reserve_content_revision(document.document_id)
    assert manager.execute(
        TimelinePropertyCommand(
            timeline,
            document.document_id,
            key.stable_id,
            "time",
            1.0,
            2.0,
            document.revision,
            first_revision,
            "Move Timeline Keyframe",
        )
    )
    second_revision = registry.reserve_content_revision(document.document_id)
    assert manager.execute(
        TimelinePropertyCommand(
            timeline,
            document.document_id,
            key.stable_id,
            "time",
            2.0,
            3.0,
            document.revision,
            second_revision,
            "Move Timeline Keyframe",
        )
    )

    assert len(manager.action_journal.entries) == 1
    assert key.time == 3.0
    registry.mark_saved(document.document_id)
    assert not document.is_dirty

    manager.undo()
    assert key.time == 1.0
    assert document.revision == 0
    assert document.saved_revision == second_revision
    assert document.is_dirty

    manager.redo()
    assert key.time == 3.0
    assert document.revision == second_revision
    assert not document.is_dirty


def test_timeline_panel_add_delete_share_one_history_and_selection_authority(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    previous_selection = SelectionService._instance
    selection = SelectionService()
    manager = UndoManager(EditorActionJournal())
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, _phase: selection.apply_snapshot(
            context.selection,
            reason="test_replay",
            record_history=False,
        ),
    )
    panel = AnimTimelineEditorPanel()
    try:
        panel.on_enable()
        _reset_editor_interaction_state.mark_saved(panel.document_id)
        panel._playhead = 0.5

        panel._add_keyframe_at_playhead()

        assert len(panel._timeline.keyframes) == 1
        key_id = panel._timeline.keyframes[0].stable_id
        assert selection.snapshot.primary == SelectionTarget.timeline_element(
            panel.document_id,
            key_id,
            sub_kind="keyframe",
        )
        assert len(manager.action_journal.entries) == 1

        panel._delete_selected_key()
        assert panel._timeline.keyframes == []
        assert selection.snapshot.is_empty
        assert len(manager.action_journal.entries) == 2

        manager.undo()
        assert panel._timeline.find_keyframe(key_id) is not None
        assert selection.snapshot.primary.target_id == key_id

        manager.undo()
        assert panel._timeline.keyframes == []
        assert selection.snapshot.is_empty

        manager.redo()
        assert panel._timeline.find_keyframe(key_id) is not None
        assert selection.snapshot.primary.target_id == key_id
    finally:
        panel.on_disable()
        SelectionService._instance = previous_selection


def test_timeline_panel_live_edit_commits_once_and_drops_no_op(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    manager = UndoManager(EditorActionJournal())
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(time=1.0)
    panel._timeline.keyframes.append(key)
    _reset_editor_interaction_state.mark_saved(panel.document_id)

    assert panel._set_live_property(
        "key.time",
        key.stable_id,
        "time",
        key.time,
        2.0,
        "Move Timeline Keyframe",
    )
    assert panel._set_live_property(
        "key.time",
        key.stable_id,
        "time",
        key.time,
        3.0,
        "Move Timeline Keyframe",
    )
    assert panel._finish_live_property_edit("key.time")

    assert key.time == 3.0
    assert len(manager.action_journal.entries) == 1
    manager.undo()
    assert key.time == 1.0

    manager.clear()
    clean_revision = panel._timeline_document().revision
    assert panel._set_live_property(
        "key.time.noop",
        key.stable_id,
        "time",
        key.time,
        4.0,
        "Move Timeline Keyframe",
    )
    assert panel._set_live_property(
        "key.time.noop",
        key.stable_id,
        "time",
        key.time,
        1.0,
        "Move Timeline Keyframe",
    )
    assert not panel._finish_live_property_edit("key.time.noop")
    assert key.time == 1.0
    assert panel._timeline_document().revision == clean_revision
    assert manager.action_journal.entries == ()
