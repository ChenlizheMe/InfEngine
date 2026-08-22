from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def ui_editor_view_services():
    from Infernux.engine.interaction import ContinuousEditService, ViewCommandService
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_edits = ContinuousEditService._instance
    previous_views = ViewCommandService._instance
    manager = UndoManager()
    edits = ContinuousEditService()
    views = ViewCommandService()
    try:
        yield manager, edits
    finally:
        edits.cancel_owner("ui_editor:view")
        views.shutdown()
        UndoManager._instance = previous_manager
        ContinuousEditService._instance = previous_edits
        ViewCommandService._instance = previous_views


def _panel_with_persistence_spy():
    from Infernux.engine.ui.ui_editor_panel import UIEditorPanel

    panel = UIEditorPanel()
    writes = []
    panel._save_view_settings = lambda: writes.append(panel._capture_view_state())
    return panel, writes


def test_visible_ui_editor_owns_hierarchy_mode_independent_of_focus():
    from Infernux.engine.ui.ui_editor_panel import UIEditorPanel

    panel = UIEditorPanel()
    mode_requests = []
    panel._load_view_settings = lambda: None
    panel.set_on_request_ui_mode(mode_requests.append)

    panel._on_visible_pre(None)
    panel._on_visible_pre(None)
    assert mode_requests == [True]

    panel._on_not_visible(None)
    assert mode_requests == [True, False]

    panel._on_visible_pre(None)
    assert mode_requests == [True, False, True]


def test_ui_editor_pan_is_one_non_dirty_command_without_frame_writes(
    ui_editor_view_services,
):
    manager, _edits = ui_editor_view_services
    panel, writes = _panel_with_persistence_spy()
    deltas = iter(((8.0, -3.0), (5.0, 7.0)))
    current = [0.0, 0.0]

    def next_x(_button):
        current[:] = next(deltas)
        return current[0]

    ctx = SimpleNamespace(
        get_mouse_drag_delta_x=next_x,
        get_mouse_drag_delta_y=lambda _button: current[1],
        reset_mouse_drag_delta=lambda _button: None,
    )
    inp = SimpleNamespace(
        wants_pan=True,
        pan_drag_button=2,
    )

    panel._process_workspace_pan(ctx, inp)
    panel._process_workspace_pan(ctx, inp)

    assert (panel._pan_x, panel._pan_y) == (13.0, 4.0)
    assert writes == []
    assert manager.action_journal.applied_entries() == ()

    inp.wants_pan = False
    panel._process_workspace_pan(ctx, inp)

    entries = manager.action_journal.applied_entries()
    assert len(entries) == 1
    assert entries[0].action.description == "Pan UI Editor View"
    assert entries[0].action.marks_dirty is False
    assert len(writes) == 1

    manager.undo()
    assert (panel._pan_x, panel._pan_y) == (0.0, 0.0)
    manager.redo()
    assert (panel._pan_x, panel._pan_y) == (13.0, 4.0)


def test_ui_editor_zoom_commits_when_panel_closes_without_frame_writes(
    ui_editor_view_services,
):
    manager, edits = ui_editor_view_services
    panel, writes = _panel_with_persistence_spy()
    initial = panel._capture_view_state()
    inp = SimpleNamespace(
        wheel_delta=1.0,
        mouse_x=320.0,
        mouse_y=180.0,
    )

    panel._process_zoom_input(inp, 20.0, 30.0)
    panel._process_zoom_input(inp, 20.0, 30.0)

    assert writes == []
    assert edits.get(panel._view_edit_key("zoom")) is not None
    assert manager.action_journal.applied_entries() == ()

    panel.on_disable()

    entry = manager.action_journal.peek_undo()
    assert entry is not None
    assert entry.action.description == "Zoom UI Editor View"
    assert entry.action.marks_dirty is False
    assert len(writes) == 1
    zoomed = panel._capture_view_state()
    assert zoomed != initial

    manager.undo()
    assert panel._capture_view_state() == initial
    manager.redo()
    assert panel._capture_view_state() == zoomed


def test_ui_editor_wheel_idle_ends_the_zoom_gesture(ui_editor_view_services):
    manager, edits = ui_editor_view_services
    panel, writes = _panel_with_persistence_spy()
    inp = SimpleNamespace(wheel_delta=-1.0, mouse_x=240.0, mouse_y=160.0)

    panel._process_zoom_input(inp, 0.0, 0.0)
    session = edits.get(panel._view_edit_key("zoom"))
    assert session is not None
    session.last_update_at -= 1.0

    inp.wheel_delta = 0.0
    panel._process_zoom_input(inp, 0.0, 0.0)

    assert edits.get(panel._view_edit_key("zoom")) is None
    assert len(writes) == 1
    entry = manager.action_journal.peek_undo()
    assert entry is not None
    assert entry.action.description == "Zoom UI Editor View"
    assert entry.action.marks_dirty is False


def test_ui_editor_canvas_drag_and_fit_restore_view_state(
    ui_editor_view_services,
):
    manager, _edits = ui_editor_view_services
    panel, writes = _panel_with_persistence_spy()
    canvas_go = SimpleNamespace(id=17)
    canvas = SimpleNamespace(reference_width=400.0, reference_height=200.0)
    canvases = [(canvas_go, canvas)]
    panel._canvas_panel_positions = {17: [0.0, 0.0]}
    panel._dragging_canvas = True
    panel._drag_canvas_id = 17
    panel._drag_canvas_start_mx = 10.0
    panel._drag_canvas_start_my = 20.0
    panel._drag_canvas_start_wx = 0.0
    panel._drag_canvas_start_wy = 0.0
    drag = SimpleNamespace(lmb_down=True, mouse_x=70.0, mouse_y=50.0)

    panel._process_canvas_drag_input(drag, canvases)
    drag.mouse_x = 90.0
    drag.mouse_y = 70.0
    panel._process_canvas_drag_input(drag, canvases)

    assert panel._canvas_panel_positions[17] == [80.0, 50.0]
    assert writes == []
    drag.lmb_down = False
    panel._process_canvas_drag_input(drag, canvases)

    canvas_entry = manager.action_journal.peek_undo()
    assert canvas_entry is not None
    assert canvas_entry.action.description == "Move UI Editor Canvas"
    assert canvas_entry.action.marks_dirty is False
    assert len(writes) == 1

    manager.undo()
    assert panel._canvas_panel_positions[17] == [0.0, 0.0]
    manager.redo()
    assert panel._canvas_panel_positions[17] == [80.0, 50.0]

    before_fit = panel._capture_view_state()
    panel._get_all_canvases = lambda: canvases
    ctx = SimpleNamespace(
        get_content_region_avail_width=lambda: 1000.0,
        get_content_region_avail_height=lambda: 600.0,
    )
    panel._fit_zoom(ctx, canvas)

    fit_state = panel._capture_view_state()
    assert fit_state != before_fit
    fit_entry = manager.action_journal.peek_undo()
    assert fit_entry is not None
    assert fit_entry.action.description == "Fit UI Editor View"
    assert fit_entry.action.marks_dirty is False

    manager.undo()
    assert panel._capture_view_state() == before_fit
    manager.redo()
    assert panel._capture_view_state() == fit_state


def test_ui_editor_canvas_focus_is_explicit_non_dirty_view_history(
    ui_editor_view_services,
):
    manager, _edits = ui_editor_view_services
    panel, _writes = _panel_with_persistence_spy()
    published = []
    panel.publish_child_context = lambda context_id, **kwargs: (
        published.append((context_id, kwargs)),
        True,
    )[1]

    assert panel._set_focused_canvas_id(
        42,
        record_history=True,
        description="Focus UI Canvas",
    )
    assert panel._focused_canvas_id == 42
    assert panel.current_child_context_id() == "canvas:42"
    entry = manager.action_journal.peek_undo()
    assert entry is not None
    assert entry.action.description == "Focus UI Canvas"
    assert entry.action.marks_dirty is False

    manager.undo()
    assert panel._focused_canvas_id == 0
    manager.redo()
    assert panel._focused_canvas_id == 42
    assert published[-1][0] == "canvas:42"


def test_ui_editor_focused_canvas_query_has_no_state_side_effect(
    ui_editor_view_services,
):
    panel, _writes = _panel_with_persistence_spy()
    canvas = SimpleNamespace(id=17)
    panel._focused_canvas_id = 0

    assert panel._get_focused_canvas([(canvas, "canvas")]) == (canvas, "canvas")
    assert panel._focused_canvas_id == 0
