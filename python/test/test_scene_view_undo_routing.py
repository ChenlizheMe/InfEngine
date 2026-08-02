from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.ui.scene_view_panel import SceneViewPanel
from Infernux.engine.ui._scene_view_gizmo import SceneViewGizmoMixin
from Infernux.engine.ui.scene_view_panel import TOOL_TRANSLATE
from Infernux.engine.ui.selection_manager import SelectionManager
from Infernux.engine.undo import UndoManager
from Infernux.lib import Vector3


def test_scene_view_has_no_private_structural_shortcut_handler():
    assert not hasattr(SceneViewPanel, "_handle_object_clipboard_shortcuts")


def test_gizmo_drag_orders_selection_primary_first():
    old = SelectionManager._instance
    selection = SelectionManager()
    SelectionManager._instance = selection
    try:
        selection.set_ids([10, 20])
        objects = {
            10: SimpleNamespace(id=10),
            20: SimpleNamespace(id=20),
        }
        scene = SimpleNamespace(find_by_id=lambda object_id: objects.get(object_id))
        mixin = object.__new__(SceneViewGizmoMixin)

        result = mixin._get_gizmo_drag_objects(scene, selection.get_primary())

        assert [obj.id for obj in result] == [20, 10]
    finally:
        SelectionManager._instance = old


def test_particle_system_owner_gizmo_drag_records_direct_transform_undo(scene):
    from Infernux.components import ParticleSystem

    previous_manager = UndoManager.instance()
    manager = UndoManager()
    manager._sync_dirty = lambda: None
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
