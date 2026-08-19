from __future__ import annotations


def test_component_add_resolves_engine_python_and_native_targets(monkeypatch):
    import Infernux.components.registry as component_registry
    import Infernux.engine.undo as undo_module
    from Infernux.components.spirit_animator import SpiritAnimator
    from Infernux.engine.interaction import ComponentCommandService

    captured = []

    class FakeGameObject:
        id = 17

        @staticmethod
        def get_py_components():
            return []

        @staticmethod
        def get_add_component_blockers(_type_name):
            return []

    class FakeAddCommand:
        def __init__(self, _object_id, type_name, **kwargs):
            python_instance = kwargs.get("python_instance")
            self.result_component = (
                python_instance if python_instance is not None else object()
            )
            captured.append((type_name, python_instance))

    monkeypatch.setattr(
        undo_module,
        "AddComponentTransactionCommand",
        FakeAddCommand,
    )
    monkeypatch.setattr(
        component_registry,
        "ensure_engine_component_catalog_loaded",
        lambda: None,
    )
    monkeypatch.setattr(
        component_registry,
        "get_type",
        lambda type_name: SpiritAnimator if type_name == "SpiritAnimator" else None,
    )
    service = ComponentCommandService()
    monkeypatch.setattr(service, "_execute", lambda *_args, **_kwargs: None)
    try:
        python_component = service.add(FakeGameObject(), "SpiritAnimator")
        native_component = service.add(FakeGameObject(), "MeshRenderer")

        assert isinstance(python_component, SpiritAnimator)
        assert isinstance(captured[0][1], SpiritAnimator)
        assert captured[1] == ("MeshRenderer", None)
        assert native_component is not None
    finally:
        service.shutdown()


def test_component_document_edit_is_atomic_and_replayable():
    from Infernux.engine.interaction import ComponentCommandService
    from Infernux.engine.undo import UndoManager

    class Probe:
        def __init__(self):
            self.value = 1

        def serialize_document(self):
            return {"value": self.value}

        def deserialize_document(self, document):
            self.value = int(document["value"])
            return True

    previous_manager = UndoManager._instance
    manager = UndoManager()
    service = ComponentCommandService()
    probe = Probe()
    try:
        result = service.edit_document(
            probe,
            lambda: setattr(probe, "value", 7),
            description="Edit Probe",
            edit_key="value",
        )

        assert result.changed is True
        assert probe.value == 7
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert probe.value == 1
        manager.redo()
        assert probe.value == 7
    finally:
        service.shutdown()
        UndoManager._instance = previous_manager


def test_component_document_noop_does_not_enter_history():
    from Infernux.engine.interaction import ComponentCommandService
    from Infernux.engine.undo import UndoManager

    class Probe:
        value = 1

        def serialize_document(self):
            return {"value": self.value}

        def deserialize_document(self, document):
            self.value = int(document["value"])
            return True

    previous_manager = UndoManager._instance
    manager = UndoManager()
    service = ComponentCommandService()
    probe = Probe()
    try:
        result = service.edit_document(
            probe,
            lambda: None,
            description="No-op Probe",
        )
        assert result.changed is False
        assert not manager.action_journal.applied_entries()
    finally:
        service.shutdown()
        UndoManager._instance = previous_manager


def test_component_service_inherits_automation_origin():
    from Infernux.engine.interaction import (
        ActionOrigin,
        ComponentCommandService,
        action_origin_scope,
    )
    from Infernux.engine.undo import UndoManager

    class Probe:
        value = 1

    previous_manager = UndoManager._instance
    manager = UndoManager()
    service = ComponentCommandService()
    probe = Probe()
    try:
        with action_origin_scope(ActionOrigin.AUTOMATION):
            assert service.set_field(probe, "value", 2)
        entry = manager.action_journal.applied_entries()[0]
        assert entry.origin is ActionOrigin.AUTOMATION
    finally:
        service.shutdown()
        UndoManager._instance = previous_manager


def test_component_property_batch_is_one_atomic_history_entry():
    from Infernux.engine.interaction import ComponentCommandService
    from Infernux.engine.undo import UndoManager

    class Probe:
        x = 1
        y = 2

    previous_manager = UndoManager._instance
    manager = UndoManager()
    service = ComponentCommandService()
    probe = Probe()
    try:
        assert service.execute_property_changes(
            [
                (probe, "x", 1, 10, "Set x"),
                (probe, "y", 2, 20, "Set y"),
            ],
            description="Move Probe",
        )
        assert (probe.x, probe.y) == (10, 20)
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert (probe.x, probe.y) == (1, 2)
        manager.redo()
        assert (probe.x, probe.y) == (10, 20)
    finally:
        service.shutdown()
        UndoManager._instance = previous_manager


def test_rejected_live_component_batch_rolls_back_model():
    from Infernux.engine.interaction import ComponentCommandService
    from Infernux.engine.undo import UndoManager

    class Probe:
        value = 2

    previous_manager = UndoManager._instance
    manager = UndoManager()
    service = ComponentCommandService()
    probe = Probe()
    try:
        manager.record = lambda *_args, **_kwargs: False
        assert not service.record_applied_property_changes(
            [(probe, "value", 1, 2, "Set value")],
            description="Edit Probe",
        )
        assert probe.value == 1
        assert manager.action_journal.entries == ()
    finally:
        service.shutdown()
        UndoManager._instance = previous_manager


def test_mcp_component_mutations_do_not_own_private_undo_paths():
    from pathlib import Path
    import Infernux.mcp.tools.particle as particle_tools
    import Infernux.mcp.tools.scene as scene_tools

    scene_source = Path(scene_tools.__file__).read_text(encoding="utf-8")
    particle_source = Path(particle_tools.__file__).read_text(encoding="utf-8")
    forbidden = (
        "_record_property",
        "_notify_scene_modified",
        "AddComponentTransactionCommand",
        "RemoveNativeComponentCommand",
        "RemovePyComponentCommand",
        "GenericComponentCommand",
    )
    assert all(token not in scene_source for token in forbidden)
    assert "_notify_scene_modified" not in particle_source
    assert "core.components.edit_document(" in particle_source


def test_light_cpp_properties_are_not_python_document_fields():
    from Infernux.components.builtin.light import Light
    from Infernux.engine.interaction.components import ComponentCommandService

    light = Light()
    assert ComponentCommandService._is_python_component(light) is False
    assert ComponentCommandService._is_cpp_property_field(light, "intensity") is True
    assert ComponentCommandService._is_python_serialized_field(light, "intensity") is False
    assert ComponentCommandService._is_python_serialized_field(light, "range") is False
    assert ComponentCommandService._is_python_serialized_field(light, "enabled") is False
