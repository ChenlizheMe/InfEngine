from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _bind_toolbar_scene_view(core) -> None:
    from Infernux.engine.interaction import PanelInteractionDescriptor
    from Infernux.engine.ui.core_panel_interactions import toolbar_panel_interaction

    core.panels.register_type("toolbar", toolbar_panel_interaction())
    core.panels.register_type("scene_view", PanelInteractionDescriptor())
    core.panels.bind_view("scene_view", "scene_view", object())


def test_view_state_command_is_undoable_without_dirtying_documents():
    from Infernux.engine.interaction import ViewCommandService
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_service = ViewCommandService._instance
    manager = UndoManager()
    service = ViewCommandService()
    state = {"tool": 0}
    try:
        assert service.set_value(
            0,
            2,
            lambda value: state.__setitem__("tool", value),
            description="Select Rotate Tool",
        )
        assert state["tool"] == 2
        entry = manager.action_journal.applied_entries()[0]
        assert entry.action.marks_dirty is False
        manager.undo()
        assert state["tool"] == 0
        manager.redo()
        assert state["tool"] == 2
    finally:
        service.shutdown()
        ViewCommandService._instance = previous_service
        UndoManager._instance = previous_manager


def test_view_state_still_applies_when_history_is_temporarily_disabled():
    from Infernux.engine.interaction import ViewCommandService
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_service = ViewCommandService._instance
    manager = UndoManager()
    manager.enabled = False
    service = ViewCommandService()
    state = {"path": "Assets"}
    try:
        assert service.set_value(
            "Assets",
            "Assets/Nested",
            lambda value: state.__setitem__("path", value),
            description="Navigate Project",
        )
        assert state["path"] == "Assets/Nested"
        assert manager.action_journal.entries == ()
    finally:
        service.shutdown()
        ViewCommandService._instance = previous_service
        UndoManager._instance = previous_manager


def test_tree_foldout_uses_the_global_non_dirty_view_history():
    from Infernux.engine.interaction import TreeViewStateService, ViewCommandService
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_view_service = ViewCommandService._instance
    previous_tree_service = TreeViewStateService._instance
    manager = UndoManager()
    view_service = ViewCommandService()
    tree_service = TreeViewStateService(view_service)
    state = {"expanded": ["root"]}
    try:
        assert tree_service.set_expanded(
            state["expanded"],
            "child",
            True,
            lambda values: state.__setitem__("expanded", values),
            description="Expand Test Tree",
        )
        assert state["expanded"] == ["root", "child"]
        entry = manager.action_journal.applied_entries()[0]
        assert entry.action.description == "Expand Test Tree"
        assert entry.action.marks_dirty is False

        manager.undo()
        assert state["expanded"] == ["root"]
        manager.redo()
        assert state["expanded"] == ["root", "child"]
    finally:
        tree_service.shutdown()
        view_service.shutdown()
        TreeViewStateService._instance = previous_tree_service
        ViewCommandService._instance = previous_view_service
        UndoManager._instance = previous_manager


def test_ui_panels_do_not_own_undo_manager_authority():
    ui_root = Path(__file__).resolve().parents[1] / "Infernux" / "engine" / "ui"
    offenders = []
    for path in ui_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "UndoManager" in source:
            offenders.append(path.name)
    assert offenders == []


def test_toolbar_camera_drag_commits_one_non_dirty_view_command():
    from Infernux.engine.bootstrap import EditorBootstrap
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_core = EditorInteractionCore._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    camera = SimpleNamespace(
        orthographic=False,
        fov=60.0,
        orthographic_size=5.0,
        rotation_speed=0.15,
        pan_speed=1.0,
        zoom_speed=1.0,
        move_speed=5.0,
        move_speed_boost=3.0,
    )
    native = SimpleNamespace(
        is_show_grid=lambda: True,
        set_show_grid=lambda _value: None,
    )
    engine = SimpleNamespace(
        _play_mode_manager=None,
        editor_camera=camera,
        get_native_engine=lambda: native,
    )
    toolbar = SimpleNamespace(
        get_camera_settings=lambda: {},
    )
    bootstrap = object.__new__(EditorBootstrap)
    bootstrap.interaction_core = core
    try:
        _bind_toolbar_scene_view(core)
        core.focus.activate_panel(
            "particle_graph_editor",
            view_id="particle_graph_editor",
            record_history=False,
        )
        bootstrap._wire_toolbar_callbacks_on(toolbar, engine)
        initial = toolbar.sync_camera_from_engine()
        changed = dict(initial, move_speed=12.0)

        toolbar.begin_camera_edit("toolbar.move_speed", initial)
        toolbar.apply_camera_to_engine(changed)
        toolbar.end_camera_edit("toolbar.move_speed", changed)

        assert camera.move_speed == 12.0
        entries = manager.action_journal.applied_entries()
        assert len(entries) == 1
        assert entries[0].action.description == "Change Scene Camera Move Speed"
        assert entries[0].action.marks_dirty is False
        assert entries[0].before_context.focus.active_view_id == "scene_view"
        assert entries[0].after_context.focus.active_view_id == "scene_view"
        assert core.focus.snapshot.active_view_id == "particle_graph_editor"

        manager.undo()
        assert camera.move_speed == 5.0
        manager.redo()
        assert camera.move_speed == 12.0
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager


def test_toolbar_grid_toggle_is_an_undoable_view_command():
    from Infernux.engine.bootstrap import EditorBootstrap
    from Infernux.engine.interaction import CommandSource, EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_core = EditorInteractionCore._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    state = {"grid": True}
    native = SimpleNamespace(
        is_show_grid=lambda: state["grid"],
        set_show_grid=lambda value: state.__setitem__("grid", bool(value)),
    )
    engine = SimpleNamespace(
        _play_mode_manager=None,
        editor_camera=None,
        get_native_engine=lambda: native,
    )
    toolbar = SimpleNamespace(get_camera_settings=lambda: {})
    bootstrap = object.__new__(EditorBootstrap)
    bootstrap.interaction_core = core
    bootstrap.engine = engine
    try:
        _bind_toolbar_scene_view(core)
        core.focus.activate_panel(
            "particle_graph_editor",
            view_id="particle_graph_editor",
            record_history=False,
        )
        bootstrap._wire_toolbar_callbacks_on(toolbar, engine)
        bootstrap._register_core_editor_commands(
            SimpleNamespace(
                get_window_instance=lambda _panel_id: None,
                get_registered_types=lambda: set(),
                reset_layout=lambda: None,
            ),
            SimpleNamespace(),
        )
        assert toolbar.is_show_grid()
        assert core.commands.execute(
            "scene.toggle_grid",
            source=CommandSource.TOOLBAR,
        ).accepted
        assert state["grid"] is False
        entries = manager.action_journal.applied_entries()
        assert len(entries) == 1
        assert entries[0].after_context.focus.active_view_id == "scene_view"
        assert core.focus.snapshot.active_view_id == "particle_graph_editor"
        manager.undo()
        assert state["grid"] is True
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager


def test_shared_node_graph_center_view_is_undoable_without_document_dirty():
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_core = EditorInteractionCore._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    view = SimpleNamespace(
        pan_x=15.0,
        pan_y=-7.0,
        zoom=0.5,
        graph=SimpleNamespace(nodes=[object()]),
    )

    def center_on_nodes():
        view.pan_x = 120.0
        view.pan_y = 80.0
        view.zoom = 1.0

    view.center_on_nodes = center_on_nodes
    panel = NodeGraphEditorPanel.__new__(NodeGraphEditorPanel)
    panel._view = view
    try:
        assert panel.command_graph_center_view()
        assert (view.pan_x, view.pan_y, view.zoom) == (120.0, 80.0, 1.0)
        entry = manager.action_journal.applied_entries()[0]
        assert entry.action.description == "Center Node Graph View"
        assert entry.action.marks_dirty is False

        manager.undo()
        assert (view.pan_x, view.pan_y, view.zoom) == (15.0, -7.0, 0.5)
        manager.redo()
        assert (view.pan_x, view.pan_y, view.zoom) == (120.0, 80.0, 1.0)
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager


def test_node_graph_view_gesture_commits_one_non_dirty_history_step():
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_core = EditorInteractionCore._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    view = SimpleNamespace(pan_x=10.0, pan_y=20.0, zoom=1.0)
    panel = NodeGraphEditorPanel.__new__(NodeGraphEditorPanel)
    panel._view = view
    try:
        view.pan_x = 48.0
        view.pan_y = -12.0
        assert panel._on_node_graph_view_gesture_committed(
            "pan",
            (10.0, 20.0, 1.0),
            (48.0, -12.0, 1.0),
        )

        entries = manager.action_journal.applied_entries()
        assert len(entries) == 1
        assert entries[0].action.description == "Pan Node Graph View"
        assert entries[0].action.marks_dirty is False
        manager.undo()
        assert (view.pan_x, view.pan_y, view.zoom) == (10.0, 20.0, 1.0)
        manager.redo()
        assert (view.pan_x, view.pan_y, view.zoom) == (48.0, -12.0, 1.0)
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager


def test_node_graph_view_groups_pan_and_wheel_into_completed_gestures():
    from Infernux.engine.ui.node_graph_view import NodeGraphView

    view = NodeGraphView()
    committed = []
    view.on_view_gesture_committed = lambda kind, before, after: committed.append(
        (kind, before, after)
    )

    pan_context = SimpleNamespace(
        get_mouse_pos_x=lambda: 0.0,
        get_mouse_pos_y=lambda: 0.0,
        is_mouse_button_down=lambda _button: False,
    )
    view._panning = True
    view._pan_gesture_before = (50.0, 50.0, 1.0)
    view.pan_x = 72.0
    view.pan_y = 14.0
    view._handle_interaction(pan_context, False, 400.0, 300.0)

    wheel = {"value": 1.0}
    zoom_context = SimpleNamespace(
        get_mouse_pos_x=lambda: 120.0,
        get_mouse_pos_y=lambda: 90.0,
        get_mouse_wheel_delta=lambda: wheel["value"],
    )
    view._canvas_window_hovered = True
    view._handle_interaction(zoom_context, False, 400.0, 300.0)
    assert len(committed) == 1
    wheel["value"] = 0.0
    view._handle_interaction(zoom_context, False, 400.0, 300.0)

    assert [item[0] for item in committed] == ["pan", "zoom"]
    assert committed[0][1] == (50.0, 50.0, 1.0)
    assert committed[0][2] == (72.0, 14.0, 1.0)
    assert committed[1][1] == (72.0, 14.0, 1.0)
    assert committed[1][2] == pytest.approx((68.16, 7.92, 1.08))


def test_timeline_scrub_and_preview_camera_use_non_dirty_view_history():
    from Infernux.core.animation_timeline import AnimationTimeline
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    previous_core = EditorInteractionCore._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    panel = AnimTimelineEditorPanel.__new__(AnimTimelineEditorPanel)
    panel._timeline = AnimationTimeline(name="View Test", duration=2.0)
    panel._playhead = 1.25
    panel._playhead_scrub_before = 0.25
    panel._cam_yaw = 0.8
    panel._cam_pitch = -0.2
    panel._cam_dist = 9.0
    panel._preview_orbit_before = (-0.6, 0.5, 6.0)
    panel._preview_zoom_before = None
    try:
        assert panel._commit_playhead_scrub()
        assert panel._commit_preview_view_gesture("orbit")
        entries = manager.action_journal.applied_entries()
        assert [entry.action.description for entry in entries] == [
            "Scrub Timeline Playhead",
            "Orbit Timeline Preview",
        ]
        assert all(entry.action.marks_dirty is False for entry in entries)

        manager.undo()
        assert (panel._cam_yaw, panel._cam_pitch, panel._cam_dist) == (
            -0.6,
            0.5,
            6.0,
        )
        manager.undo()
        assert panel._playhead == 0.25
        manager.redo()
        assert panel._playhead == 1.25
        manager.redo()
        assert (panel._cam_yaw, panel._cam_pitch, panel._cam_dist) == (
            0.8,
            -0.2,
            9.0,
        )
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager
