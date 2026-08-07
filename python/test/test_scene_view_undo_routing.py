from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.ui.scene_view_panel import SceneViewPanel
from Infernux.engine.ui._scene_view_gizmo import SceneViewGizmoMixin
from Infernux.engine.ui.scene_view_panel import TOOL_ROTATE, TOOL_TRANSLATE
from Infernux.engine.interaction import (
    ContinuousEditService,
    FocusService,
    SelectionService,
    TransientInteractionService,
    ViewCommandService,
)
from Infernux.engine.undo import UndoManager
from Infernux.lib import Vector3


class _EditorCameraStub:
    def __init__(self):
        self.position = Vector3(0.0, 0.0, -10.0)
        self.rotation = (0.0, 0.0)
        self.focus_distance = 10.0
        self.fov = 60.0

    def restore_state(
        self,
        px,
        py,
        pz,
        _fx,
        _fy,
        _fz,
        distance,
        yaw,
        pitch,
    ):
        self.position = Vector3(px, py, pz)
        self.focus_distance = float(distance)
        self.rotation = (float(yaw), float(pitch))


def _install_gizmo_interaction_services():
    previous = (
        UndoManager.instance(),
        FocusService._instance,
        ContinuousEditService._instance,
        TransientInteractionService._instance,
    )
    manager = UndoManager()
    focus = FocusService()
    focus.activate_panel(
        "scene_view",
        view_id="scene_view",
        record_history=False,
    )
    ContinuousEditService()
    transients = TransientInteractionService(focus)
    return previous, manager, transients


def _restore_gizmo_interaction_services(previous):
    (
        UndoManager._instance,
        FocusService._instance,
        ContinuousEditService._instance,
        TransientInteractionService._instance,
    ) = previous


def _begin_test_gizmo_drag(panel, owner):
    panel._is_gizmo_dragging = True
    panel._gizmo_drag_obj_id = int(owner.id)
    panel._gizmo_drag_items = {
        int(owner.id): panel._snapshot_gizmo_object(owner),
    }
    panel._begin_gizmo_drag_transaction(TOOL_TRANSLATE)


def test_scene_view_has_no_private_structural_shortcut_handler():
    assert not hasattr(SceneViewPanel, "_handle_object_clipboard_shortcuts")


def test_frame_selected_camera_state_is_undoable_without_dirtying_scene():
    previous_manager = UndoManager.instance()
    previous_view_commands = ViewCommandService.instance()
    manager = UndoManager()
    ViewCommandService()
    camera = _EditorCameraStub()
    panel = SceneViewPanel(engine=SimpleNamespace(editor_camera=camera))
    panel._compute_object_bounds = lambda _obj: ((5.0, 2.0, 1.0), 2.0)
    try:
        assert panel.fly_to_object(SimpleNamespace(id=17))
        assert manager.undo_description == "Frame Selected"
        entry = manager.action_journal.peek_undo()
        assert entry is not None
        assert not entry.action.marks_dirty

        panel._tick_fly_to(1.0)
        assert tuple(camera.position) != (0.0, 0.0, -10.0)

        manager.undo()
        panel._tick_fly_to(1.0)
        assert tuple(camera.position) == (0.0, 0.0, -10.0)

        manager.redo()
        panel._tick_fly_to(1.0)
        assert tuple(camera.position) != (0.0, 0.0, -10.0)
    finally:
        UndoManager._instance = previous_manager
        ViewCommandService._instance = previous_view_commands


def test_gizmo_drag_orders_selection_primary_first():
    old = SelectionService._instance
    selection = SelectionService()
    try:
        selection.replace_scene_objects(
            [10, 20], owner_id="scene_view", record_history=False
        )
        objects = {
            10: SimpleNamespace(id=10),
            20: SimpleNamespace(id=20),
        }
        scene = SimpleNamespace(find_by_id=lambda object_id: objects.get(object_id))
        mixin = object.__new__(SceneViewGizmoMixin)

        result = mixin._get_gizmo_drag_objects(
            scene, selection.primary_scene_object_id()
        )

        assert [obj.id for obj in result] == [20, 10]
    finally:
        SelectionService._instance = old


def test_particle_system_owner_gizmo_drag_records_direct_transform_undo(scene):
    from Infernux.components import ParticleSystem

    previous_manager = UndoManager.instance()
    manager = UndoManager()
    try:
        owner = scene.create_game_object("Particle Gizmo Undo")
        owner.add_py_component(ParticleSystem())
        panel = SceneViewPanel(engine=None)
        panel._gizmo_drag_obj_id = int(owner.id)
        panel._gizmo_drag_items = {
            int(owner.id): panel._snapshot_gizmo_object(owner),
        }

        owner.transform.position = Vector3(3.0, 2.0, 1.0)
        panel._record_gizmo_undo(TOOL_TRANSLATE)

        assert manager.undo_description == "Translate"
        manager.undo()
        assert tuple(owner.transform.position) == (0.0, 0.0, 0.0)
        manager.redo()
        assert tuple(owner.transform.position) == (3.0, 2.0, 1.0)
    finally:
        UndoManager._instance = previous_manager


def test_gizmo_transform_history_preserves_pointer_down_selection(scene):
    from Infernux.engine.interaction import ContextRestoreStatus, EditorContextSnapshot

    previous_manager = UndoManager.instance()
    previous_selection = SelectionService._instance
    manager = UndoManager()
    selection = SelectionService()
    SelectionService.install(selection)
    def restore_context(context, _phase):
        selection.apply_snapshot(
            context.selection,
            reason="test_gizmo_restore",
            record_history=False,
        )
        return ContextRestoreStatus.READY

    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        restore_context,
    )
    try:
        owner = scene.create_game_object("Selected Gizmo Target")
        selection.replace_scene_objects(
            [int(owner.id)],
            owner_id="hierarchy",
            record_history=False,
        )
        panel = SceneViewPanel(engine=None)
        panel._gizmo_drag_obj_id = int(owner.id)
        panel._gizmo_drag_items = {
            int(owner.id): panel._snapshot_gizmo_object(owner),
        }
        panel._gizmo_drag_selection_snapshot = selection.snapshot

        owner.transform.position = Vector3(4.0, 0.0, 0.0)
        selection.clear(reason="late_frame_selection_noise", record_history=False)
        panel._record_gizmo_undo(TOOL_TRANSLATE)

        entry = manager.action_journal.peek_undo()
        assert entry is not None
        assert entry.before_context.selection.primary.scene_object_id() == int(owner.id)
        assert entry.after_context.selection.primary.scene_object_id() == int(owner.id)

        manager.undo()
        assert selection.primary_scene_object_id() == int(owner.id)
        assert tuple(owner.transform.position) == (0.0, 0.0, 0.0)
    finally:
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_gizmo_tool_switch_commits_live_transform_once(scene):
    previous, manager, _transients = _install_gizmo_interaction_services()
    try:
        owner = scene.create_game_object("Tool Switch Gizmo")
        panel = SceneViewPanel(engine=None)
        _begin_test_gizmo_drag(panel, owner)
        owner.transform.position = Vector3(5.0, 1.0, 0.0)
        panel._update_gizmo_drag_transaction()

        panel._set_tool_mode(TOOL_ROTATE)

        assert not panel._is_gizmo_dragging
        assert manager.undo_description == "Translate"
        manager.undo()
        assert tuple(owner.transform.position) == (0.0, 0.0, 0.0)
        manager.redo()
        assert tuple(owner.transform.position) == (5.0, 1.0, 0.0)
    finally:
        _restore_gizmo_interaction_services(previous)


def test_gizmo_focus_loss_commits_through_transient_owner(scene):
    previous, manager, transients = _install_gizmo_interaction_services()
    try:
        owner = scene.create_game_object("Focus Loss Gizmo")
        panel = SceneViewPanel(engine=None)
        _begin_test_gizmo_drag(panel, owner)
        owner.transform.position = Vector3(2.0, 3.0, 4.0)
        panel._update_gizmo_drag_transaction()

        assert transients.cancel_owner("scene_view") == 1

        assert not panel._is_gizmo_dragging
        assert manager.undo_description == "Translate"
        manager.undo()
        assert tuple(owner.transform.position) == (0.0, 0.0, 0.0)
    finally:
        _restore_gizmo_interaction_services(previous)


def test_gizmo_panel_close_commits_live_transform(scene):
    previous, manager, _transients = _install_gizmo_interaction_services()
    try:
        owner = scene.create_game_object("Panel Close Gizmo")
        panel = SceneViewPanel(engine=None)
        _begin_test_gizmo_drag(panel, owner)
        owner.transform.position = Vector3(7.0, 0.0, 0.0)
        panel._update_gizmo_drag_transaction()

        panel.on_disable()

        assert not panel._is_gizmo_dragging
        assert manager.undo_description == "Translate"
        manager.undo()
        assert tuple(owner.transform.position) == (0.0, 0.0, 0.0)
    finally:
        _restore_gizmo_interaction_services(previous)


def test_gizmo_interruption_rolls_back_when_history_rejects_commit(scene):
    previous, manager, _transients = _install_gizmo_interaction_services()
    try:
        owner = scene.create_game_object("Rejected Gizmo")
        panel = SceneViewPanel(engine=None)
        _begin_test_gizmo_drag(panel, owner)
        owner.transform.position = Vector3(9.0, 0.0, 0.0)
        panel._update_gizmo_drag_transaction()
        manager.enabled = False

        panel._set_tool_mode(TOOL_ROTATE)

        assert not panel._is_gizmo_dragging
        assert tuple(owner.transform.position) == (0.0, 0.0, 0.0)
        assert manager.undo_description == ""
    finally:
        _restore_gizmo_interaction_services(previous)
