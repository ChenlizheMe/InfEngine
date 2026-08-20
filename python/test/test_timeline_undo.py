from types import SimpleNamespace

import pytest

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
    controller = SimpleNamespace(
        timeline_authoring_model=lambda: timeline,
        on_timeline_authoring_applied=lambda: None,
    )
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


def test_unsaved_timeline_document_can_be_rebound_after_panel_close(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    registry = _reset_editor_interaction_state
    panel = AnimTimelineEditorPanel()
    panel._timeline.keyframes.append(TimelineKeyframe(time=0.75))
    registry.mark_changed(panel.document_id)
    document_id = panel.document_id
    locator = registry.locate(document_id)
    assert locator is not None

    panel.unbind_document()
    assert registry.get(document_id) is None
    assert panel.restore_dormant_document(locator) is True

    assert panel.document_id == document_id
    assert panel._timeline.keyframes[0].time == 0.75
    assert registry.require(document_id).controller.view is panel


def test_unsaved_timeline_context_restores_through_document_open_adapter(
    _reset_editor_interaction_state,
):
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import DocumentOpenStatus
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    registry = _reset_editor_interaction_state
    panel = AnimTimelineEditorPanel()
    panel._timeline.keyframes.append(TimelineKeyframe(time=1.25))
    registry.mark_changed(panel.document_id)
    document_id = panel.document_id
    locator = registry.locate(document_id)
    assert locator is not None
    panel.unbind_document()

    class _WindowManager:
        @staticmethod
        def open_window(panel_id):
            assert panel_id == "animtimeline_editor"
            panel.open()
            return panel

    bootstrap = BootstrapSelectionMixin()
    bootstrap.window_manager = _WindowManager()
    bootstrap.interaction_core = SimpleNamespace(documents=registry)
    bootstrap._focus_navigation_panel = lambda *args, **kwargs: pytest.fail(
        "document resolution must not mutate focus or presentation"
    )

    result = bootstrap._restore_panel_document(
        locator,
        panel_id="animtimeline_editor",
    )

    assert result.status is DocumentOpenStatus.READY
    assert result.document is not None
    assert result.document.document_id == document_id
    assert panel.document_id == document_id
    assert panel._timeline.keyframes[0].time == 1.25


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
        lambda context, _phase: (
            selection.apply_snapshot(
                context.selection,
                reason="test_replay",
                record_history=False,
            ),
            True,
        )[1],
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


def test_timeline_panel_projects_selection_without_a_private_listener(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    selection = SelectionService()
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(time=0.25)
    panel._timeline.keyframes.append(key)

    selection.select(
        SelectionTarget.timeline_element(
            panel.document_id,
            key.stable_id,
            sub_kind="keyframe",
        ),
        owner_id="animtimeline_editor",
        record_history=False,
    )
    assert panel._selected_key_id == key.stable_id
    assert panel._current_sel_key() is key

    selection.select(
        SelectionTarget.asset("Assets/Other.mat"),
        owner_id="project",
        record_history=False,
    )
    assert panel._selected_key_id == ""
    assert panel._current_sel_key() is None

    selection.select(
        SelectionTarget.timeline_element(
            panel.document_id,
            "missing-key",
            sub_kind="keyframe",
        ),
        owner_id="automation",
        record_history=False,
    )
    assert panel._current_sel_key() is None
    assert selection.snapshot.is_empty


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


def test_selected_key_inspector_does_not_own_timeline_playhead(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(time=1.0)
    panel._timeline.keyframes.append(key)
    panel._playhead = 2.5

    assert not panel._apply_inspector_key_time(key, 1.0)
    assert panel._playhead == 2.5
    assert key.time == 1.0


def test_timeline_preview_loop_is_view_state_and_wraps_without_stopping(
    _reset_editor_interaction_state,
    monkeypatch,
):
    from Infernux.engine.ui import animtimeline_editor_panel as timeline_module
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    panel = AnimTimelineEditorPanel()
    panel._timeline.duration = 2.0
    panel._loop_preview = True
    panel._playing = True
    panel._playhead_at_play_start = 1.5
    panel._play_wall_start = 10.0
    monkeypatch.setattr(timeline_module.time, "perf_counter", lambda: 11.0)

    panel._advance_playback()

    assert panel._playing
    assert panel._playhead == 0.5
    state = panel.save_state()
    assert state["values"]["loop_preview"] is True
    assert "loop" not in panel._timeline.to_dict()


def test_timeline_preview_without_loop_stops_at_the_end(
    _reset_editor_interaction_state,
    monkeypatch,
):
    from Infernux.engine.ui import animtimeline_editor_panel as timeline_module
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    panel = AnimTimelineEditorPanel()
    panel._timeline.duration = 2.0
    panel._loop_preview = False
    panel._playing = True
    panel._playhead_at_play_start = 1.5
    panel._play_wall_start = 10.0
    monkeypatch.setattr(timeline_module.time, "perf_counter", lambda: 11.0)

    panel._advance_playback()

    assert not panel._playing
    assert panel._playhead == 2.0


def test_timeline_panel_rejects_discrete_edits_without_active_history(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    previous_manager = UndoManager._instance
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(time=1.0)
    panel._timeline.keyframes.append(key)
    document = panel._timeline_document()
    initial_revision = document.revision
    try:
        UndoManager._instance = None

        assert not panel._apply_discrete_property(
            key.stable_id,
            "time",
            2.0,
            "Move Timeline Keyframe",
        )
        assert key.time == 1.0
        assert document.revision == initial_revision
    finally:
        UndoManager._instance = previous_manager


def test_timeline_panel_rejects_live_edits_without_active_history(
    _reset_editor_interaction_state,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    previous_manager = UndoManager._instance
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(time=1.0)
    panel._timeline.keyframes.append(key)
    document = panel._timeline_document()
    initial_revision = document.revision
    try:
        UndoManager._instance = None

        assert not panel._set_live_property(
            "key.time",
            key.stable_id,
            "time",
            key.time,
            2.0,
            "Move Timeline Keyframe",
        )
        assert key.time == 1.0
        assert document.revision == initial_revision
    finally:
        UndoManager._instance = previous_manager


def test_timeline_document_replacement_waits_for_dirty_resolution(
    _reset_editor_interaction_state,
    monkeypatch,
):
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel
    from Infernux.engine.ui.dirty_panel_confirmation import (
        DirtyPanelConfirmationCoordinator,
    )

    pending = {}

    class _Coordinator:
        @staticmethod
        def request_document_replace(
            document_id,
            on_complete,
            on_cancel=None,
            *,
            owner_id="",
        ):
            pending.update(
                document_id=document_id,
                on_complete=on_complete,
                on_cancel=on_cancel,
                owner_id=owner_id,
            )
            return True

    monkeypatch.setattr(
        DirtyPanelConfirmationCoordinator,
        "instance",
        classmethod(lambda cls: _Coordinator()),
    )
    panel = AnimTimelineEditorPanel()
    # Replacement confirmation is required only after a real authored edit;
    # opening the blank editor itself is a clean presentation action.
    _reset_editor_interaction_state.mark_changed(
        panel.document_id,
        view_id=panel.window_id,
    )
    original = panel._timeline

    assert panel.command_new_timeline()
    assert panel._timeline is original
    assert pending["document_id"] == panel.document_id
    assert pending["owner_id"] == panel.window_id

    pending["on_complete"]()
    assert panel._timeline is not original
    assert panel._timeline.name == "Timeline"
    assert panel._timeline_document().is_dirty
