from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.ui.scene_view_panel import SceneViewPanel
from Infernux.engine.ui._scene_view_gizmo import SceneViewGizmoMixin
from Infernux.engine.ui.scene_view_panel import TOOL_TRANSLATE
from Infernux.engine.ui.selection_manager import SelectionManager
from Infernux.engine.undo import UndoManager
from Infernux.lib import Vector3


class _ShortcutContext:
    def want_text_input(self):
        return False

    def is_key_pressed(self, key):
        return key == SceneViewPanel.KEY_DELETE

    def is_key_down(self, key):
        return False


def test_scene_view_delete_uses_shared_structural_handler():
    panel = SceneViewPanel(engine=None)
    panel._delete_selected_callback = lambda: calls.append("delete")
    panel._copy_selected_callback = None
    panel._paste_clipboard_callback = None
    panel._has_clipboard_data_callback = None
    panel._is_window_or_child_focused = lambda _ctx: False
    calls = []

    panel._handle_object_clipboard_shortcuts(_ShortcutContext(), True)

    assert calls == ["delete"]


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
