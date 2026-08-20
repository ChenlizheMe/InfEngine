"""Camera MCP tools must share the editor's scene mutation authority."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from Infernux.mcp.tools import camera


class _FakeMcp:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


def test_camera_transform_fields_use_serialized_property_core(monkeypatch):
    import Infernux.engine.interaction as interaction

    class Transform:
        position = "before"

    class CameraObject:
        id = 17
        transform = Transform()

    class Transaction:
        def __init__(self, target, field):
            self.target = target
            self.field = field

        def commit_or_raise(self, value):
            setattr(self.target, self.field, value)
            return interaction.PropertyTransactionStatus.APPLIED

    calls = []

    def _factory(targets, field, **kwargs):
        calls.append((targets[0], field, kwargs))
        return Transaction(targets[0], field)

    monkeypatch.setattr(interaction, "make_attribute_property_transaction", _factory)

    obj = CameraObject()
    camera._set_transform_values(obj, {"position": "after"}, "Aim Camera")

    assert obj.transform.position == "after"
    assert calls[0][1] == "position"
    assert calls[0][2]["property_path"] == "Transform.position"
    assert calls[0][2]["description"] == "Aim Camera"


def test_camera_fields_use_serialized_property_core(monkeypatch):
    import Infernux.engine.interaction as interaction

    class CameraComponent:
        field_of_view = 60.0

    class Transaction:
        def __init__(self, target, field):
            self.target = target
            self.field = field

        def commit_or_raise(self, value):
            setattr(self.target, self.field, value)
            return interaction.PropertyTransactionStatus.APPLIED

    calls = []

    def _factory(targets, field, **kwargs):
        calls.append((targets[0], field, kwargs))
        return Transaction(targets[0], field)

    monkeypatch.setattr(
        interaction,
        "make_attribute_property_transaction",
        _factory,
    )

    component = CameraComponent()
    camera._set_camera_values(
        component,
        {"field_of_view": 72.0},
        "Configure Third Person Camera",
    )

    assert component.field_of_view == 72.0
    assert calls[0][1] == "field_of_view"
    assert calls[0][2]["property_path"] == "Camera.field_of_view"
    assert calls[0][2]["description"] == "Configure Third Person Camera"


def test_camera_reparent_uses_scene_service_and_preserves_local_state(monkeypatch):
    class Transform:
        local_position = "local-position"
        local_euler_angles = "local-rotation"
        local_scale = "local-scale"

    class Object:
        def __init__(self, object_id, parent=None):
            self.id = object_id
            self.transform = Transform()
            self._parent = parent

        def get_parent(self):
            return self._parent

    class SceneObjects:
        def __init__(self):
            self.moves = []

        def move_hierarchy(self, object_ids, mode, target_id):
            self.moves.append((tuple(object_ids), mode, target_id))
            return True

        def user_action(self, _description):
            return nullcontext()

    scene_objects = SceneObjects()
    edits = []
    cam = Object(17)
    target = Object(23)
    monkeypatch.setattr(camera, "_scene_object_service", lambda: scene_objects)
    monkeypatch.setattr(
        camera,
        "_set_transform_values",
        lambda _cam, values, description: edits.append((values, description)),
    )

    camera._reparent_camera(
        cam,
        target,
        world_position_stays=False,
        local_position=None,
        local_euler_angles=None,
        description="Attach Camera",
    )

    assert scene_objects.moves == [((17,), "parent", 23)]
    assert edits == [
        (
            {
                "local_position": "local-position",
                "local_euler_angles": "local-rotation",
                "local_scale": "local-scale",
            },
            "Attach Camera",
        )
    ]


def test_camera_user_action_delegates_to_scene_object_service(monkeypatch):
    class SceneObjects:
        def __init__(self):
            self.actions = []

        def user_action(self, description):
            self.actions.append(description)
            return nullcontext()

    scene_objects = SceneObjects()
    monkeypatch.setattr(camera, "_scene_object_service", lambda: scene_objects)

    result = camera._run_scene_user_action("Aim Camera", lambda: 42)

    assert result == 42
    assert scene_objects.actions == ["Aim Camera"]


def test_scene_object_user_action_owns_global_history_scope(monkeypatch):
    from Infernux.engine.interaction import SceneObjectCommandService
    from Infernux.engine.undo import UndoManager

    class Manager:
        enabled = True
        is_executing = False

        def __init__(self):
            self.descriptions = []

        def user_action(self, description):
            self.descriptions.append(description)
            return nullcontext()

    manager = Manager()
    monkeypatch.setattr(UndoManager, "instance", classmethod(lambda _cls: manager))
    service = object.__new__(SceneObjectCommandService)

    with service.user_action("Configure Camera"):
        pass

    assert manager.descriptions == ["Configure Camera"]


def test_set_main_camera_is_a_global_property_transaction(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        creation = HierarchyCreationService.instance()
        first = creation.create("rendering.camera", name="Camera A", select=False)
        second = creation.create("rendering.camera", name="Camera B", select=False)
        first_component = camera._find_component(
            scene.find_by_id(int(first["id"])),
            "Camera",
        )
        scene.main_camera = first_component
        manager.clear()

        assert camera._try_set_scene_main_camera(int(second["id"])) is True
        assert int(scene.main_camera.game_object.id) == int(second["id"])
        assert len(manager.action_journal.applied_entries()) == 1
        assert manager.undo_description == "Set Main Camera"

        manager.undo()
        assert int(scene.main_camera.game_object.id) == int(first["id"])
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_camera_field_transaction_replays_live_component(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        created = HierarchyCreationService.instance().create(
            "rendering.camera",
            name="Camera",
            select=False,
        )
        obj = scene.find_by_id(int(created["id"]))
        component = camera._find_component(obj, "Camera")
        old_fov = float(component.field_of_view)
        manager.clear()

        camera._set_camera_values(
            component,
            {"field_of_view": old_fov + 7.0},
            "Set Camera FOV",
        )

        assert float(component.field_of_view) == old_fov + 7.0
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert float(component.field_of_view) == old_fov
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_camera_setup_is_one_global_user_action(scene, monkeypatch):
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        monkeypatch.setattr(camera, "main_thread", lambda _name, fn, **_kwargs: fn())
        mcp = _FakeMcp()
        camera.register_camera_tools(mcp)

        result = mcp.tools["camera_setup_2d_card_game"](
            position={"x": 1.0, "y": 2.0, "z": 12.0},
            euler_angles={"x": 0.0, "y": 180.0, "z": 0.0},
            orthographic_size=9.0,
        )

        object_id = int(result["camera_id"])
        assert scene.find_by_id(object_id) is not None
        assert len(manager.action_journal.applied_entries()) == 1
        assert manager.undo_description == "Configure 2D Camera"

        manager.undo()
        assert scene.find_by_id(object_id) is None
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_camera_mcp_mutations_have_no_private_undo_or_dirty_bypass():
    source = Path(camera.__file__).read_text(encoding="utf-8")

    assert "SceneFileManager" not in source
    assert ".mark_dirty(" not in source
    assert "_record_property" not in source
    assert "UndoManager" not in source
    assert ".set_parent(" not in source
    assert "make_attribute_property_transaction" in source
    assert ".move_hierarchy(" in source
    assert "_run_scene_user_action" in source
