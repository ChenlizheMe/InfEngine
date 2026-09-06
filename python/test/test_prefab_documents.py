"""Strict prefab documents and typed pybind serialization boundaries."""

from __future__ import annotations

import json

import pytest

from Infernux.components import FieldType, InxComponent, serialized_field
from Infernux.components.builtin import BoxCollider
from Infernux.components.ref_wrappers import ComponentRef, GameObjectRef
from Infernux.components.value_document import make_game_object_ref
from Infernux.engine.prefab_manager import (
    PrefabDocumentError,
    _PREFAB_TEMPLATE_CACHE,
    _link_created_prefab_source,
    _read_prefab_document,
    instantiate_prefab,
    save_prefab,
)
from Infernux.engine.prefab_overrides import (
    _build_reverted_prefab_document,
    apply_overrides_to_prefab,
    build_prefab_apply_command,
    build_prefab_revert_command,
    compute_overrides,
    resolve_prefab_instance_root,
    revert_overrides,
)
from Infernux.engine.undo import (
    BuiltinPropertyCommand,
    PrefabRevertCommand,
    UndoManager,
)
from Infernux.math import Vector3
from Infernux.engine.component_restore import serialize_game_object_document_authoritatively


class _PrefabTargetComponent(InxComponent):
    value: int = 19


class _PrefabReferenceComponent(InxComponent):
    target_object = serialized_field(default=None, field_type=FieldType.GAME_OBJECT)
    target_component = serialized_field(default=None, field_type=FieldType.COMPONENT)


def test_link_created_prefab_source_stamps_root_and_children(scene, tmp_path):
    root = scene.create_game_object("CheckpointGate")
    child = scene.create_game_object("LeftPost")
    child.set_parent(root)
    prefab_path = str(tmp_path / "CheckpointGate.prefab")

    class AssetDatabase:
        @staticmethod
        def get_guid_from_path(path):
            return "checkpoint-guid" if path == prefab_path else ""

    assert _link_created_prefab_source(root, prefab_path, AssetDatabase()) is True
    assert root.prefab_guid == "checkpoint-guid"
    assert root.prefab_root is True
    assert child.prefab_guid == "checkpoint-guid"
    assert child.prefab_root is False


def test_prefab_save_fails_when_asset_registration_fails(scene, tmp_path, monkeypatch):
    from Infernux.core.assets import AssetManager

    class FailedMutation:
        error = "registration rejected"

        def __bool__(self):
            return False

    monkeypatch.setattr(
        AssetManager,
        "import_asset",
        staticmethod(lambda _path, *, database: FailedMutation()),
    )
    root = scene.create_game_object("StrictPrefab")

    assert save_prefab(root, str(tmp_path / "strict.prefab"), object()) is False


def test_authoritative_object_snapshot_overlays_live_component_data():
    class _Component:
        component_id = 7
        enabled = True
        execution_order = 3
        _cpp_type_name = "BoxCollider"

        @staticmethod
        def serialize_document():
            return {
                "type": "BoxCollider",
                "component_id": 7,
                "enabled": True,
                "execution_order": 3,
                "is_trigger": True,
            }

    class _Object:
        @staticmethod
        def serialize_document():
            return {
                "components": [{
                    "component_id": 7,
                    "type_id": "native:BoxCollider",
                    "enabled": True,
                    "execution_order": 0,
                    "data": {"is_trigger": False},
                }],
                "children": [],
            }

        @staticmethod
        def get_components():
            return [_Component()]

        @staticmethod
        def get_children():
            return []

    snapshot = serialize_game_object_document_authoritatively(_Object())
    assert snapshot["components"][0]["data"] == {"is_trigger": True}
    assert snapshot["components"][0]["execution_order"] == 3


def test_authoritative_object_snapshot_strips_python_identity_metadata():
    class _Component:
        component_id = 11
        enabled = False
        execution_order = -2

        @staticmethod
        def _serialize_fields_document():
            return {
                "__type_name__": "RaceController",
                "__component_id__": 11,
                "lap_count": 4,
            }

    class _Object:
        @staticmethod
        def serialize_document():
            return {
                "components": [{
                    "component_id": 11,
                    "type_id": "python:script:type:race:RaceController",
                    "enabled": True,
                    "execution_order": 0,
                    "data": {"lap_count": 1},
                }],
                "children": [],
            }

        @staticmethod
        def get_components():
            return [_Component()]

        @staticmethod
        def get_children():
            return []

    snapshot = serialize_game_object_document_authoritatively(_Object())
    record = snapshot["components"][0]
    assert record["data"] == {"lap_count": 4}
    assert record["enabled"] is False
    assert record["execution_order"] == -2


def _assert_runtime_ids_removed(document: dict) -> None:
    assert "id" not in document
    assert type(document["local_id"]) is int and document["local_id"] > 0
    assert "component_id" not in document["transform"]
    assert "instance_guid" not in document["transform"]
    for component in document["components"]:
        assert type(component["component_id"]) is int and component["component_id"] > 0
        assert "instance_guid" not in component
    for child in document["children"]:
        _assert_runtime_ids_removed(child)


def test_typed_scene_document_bridge_and_instantiation(scene):
    root = scene.create_game_object("DocumentRoot")
    root.add_component("Rigidbody")
    child = scene.create_game_object("DocumentChild")
    child.set_parent(root)

    document = root.serialize_document()
    assert isinstance(document, dict)
    assert isinstance(document["children"], list)
    assert document["children"][0]["name"] == "DocumentChild"

    clone = scene._instantiate_document(document)
    assert clone is not None
    assert clone.id != root.id
    assert clone.get_child(0).id != child.id


def test_prefab_save_is_strict_typed_and_atomic(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    root = scene.create_game_object("PrefabRoot")
    root.add_component("BoxCollider")
    child = scene.create_game_object("PrefabChild")
    child.add_component("Rigidbody")
    child.set_parent(root)

    path = tmp_path / "nested" / "typed.prefab"
    assert save_prefab(root, str(path), source_canvas_name="HUD") is True

    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert set(envelope) == {"root_object", "source_canvas_name"}
    assert envelope["source_canvas_name"] == "HUD"
    _assert_runtime_ids_removed(envelope["root_object"])
    assert list(path.parent.glob("typed.prefab.tmp.*")) == []

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    assert instance is not None
    assert instance.id != root.id
    assert instance.get_child(0).id != child.id


def test_prefab_remaps_internal_python_references(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    root = scene.create_game_object("ReferencePrefabRoot")
    child = scene.create_game_object("ReferencePrefabChild")
    child.set_parent(root)
    child.add_py_component(_PrefabTargetComponent())
    references = _PrefabReferenceComponent()
    references.target_object = GameObjectRef(child)
    references.target_component = ComponentRef(
        go_id=child.id,
        component_type="_PrefabTargetComponent",
    )
    root.add_py_component(references)
    path = tmp_path / "reference.prefab"

    assert save_prefab(root, str(path)) is True
    document = json.loads(path.read_text(encoding="utf-8"))["root_object"]
    reference_record = next(
        record for record in document["components"]
        if record["type_id"].endswith(":_PrefabReferenceComponent")
    )
    assert reference_record["data"]["target_object"] == make_game_object_ref(
        document["children"][0]["local_id"]
    )

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance_child = instance.get_child(0)
    restored = instance.get_py_component(_PrefabReferenceComponent)
    assert restored.target_object is instance_child
    assert restored.target_component is instance_child.get_py_component(_PrefabTargetComponent)


def test_prefab_overrides_use_typed_documents(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("OverridePrefab")
    path = tmp_path / "override.prefab"
    assert save_prefab(source, str(path)) is True

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance.transform.local_scale = Vector3(2.0, 2.0, 2.0)
    assert any(override.key == "transform.scale" for override in compute_overrides(instance, str(path)))

    assert revert_overrides(instance, str(path)) is True
    assert instance.transform.local_scale.x == pytest.approx(1.0)

    instance.transform.local_scale = Vector3(2.0, 2.0, 2.0)
    assert apply_overrides_to_prefab(instance, str(path)) is True
    assert _read_prefab_document(str(path))["root_object"]["transform"]["scale"][0] == pytest.approx(2.0)


def test_prefab_child_overrides_resolve_instance_root(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("CheckpointGate")
    child = scene.create_game_object("LeftPost")
    child.add_component("BoxCollider")
    child.set_parent(source)
    path = tmp_path / "checkpoint.prefab"
    assert save_prefab(source, str(path)) is True

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance.prefab_guid = "checkpoint-guid"
    instance.prefab_root = True
    instance_child = instance.get_child(0)
    instance_child.prefab_guid = "checkpoint-guid"

    assert resolve_prefab_instance_root(instance_child) is instance
    instance.name = "CheckpointGate_B"
    instance.active = False
    instance.is_static = True
    instance.tag = "Checkpoint"
    instance.layer = 3
    instance.transform.position = Vector3(5.0, 0.0, 0.0)
    assert compute_overrides(instance_child, str(path)) == []

    instance_child.get_component("BoxCollider").is_trigger = True
    overrides = compute_overrides(instance_child, str(path))
    assert any("BoxCollider" in override.key for override in overrides)

    assert revert_overrides(instance_child, str(path)) is True
    assert instance.name == "CheckpointGate_B"
    assert instance.active is False
    assert instance.is_static is True
    assert instance.tag == "Checkpoint"
    assert instance.layer == 3
    assert instance.transform.position.x == pytest.approx(5.0)
    assert instance.get_child(0).get_component("BoxCollider").is_trigger is False
    assert compute_overrides(instance, str(path)) == []


def test_prefab_instance_requires_an_explicit_root_marker():
    class LinkedObject:
        prefab_guid = "checkpoint-guid"
        prefab_root = False

        @staticmethod
        def get_parent():
            return None

    with pytest.raises(LookupError, match="checkpoint-guid"):
        resolve_prefab_instance_root(LinkedObject())


def test_prefab_revert_command_restores_complete_subtree_on_undo(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("CheckpointGate")
    child = scene.create_game_object("LeftPost")
    child.add_component("BoxCollider")
    child.set_parent(source)
    path = tmp_path / "checkpoint-revert-undo.prefab"
    assert save_prefab(source, str(path)) is True

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance.prefab_guid = "checkpoint-guid"
    instance.prefab_root = True
    instance.get_child(0).prefab_guid = "checkpoint-guid"
    instance.get_child(0).get_component("BoxCollider").is_trigger = True

    before_document = serialize_game_object_document_authoritatively(instance)
    before_data = before_document["children"][0]["components"][0]["data"]
    assert before_data["is_trigger"] is True
    assert "component_id" not in before_data
    reverted_document = _build_reverted_prefab_document(instance, str(path))
    command = PrefabRevertCommand(
        instance.id,
        before_document,
        reverted_document,
    )

    command.execute()
    assert instance.get_child(0).get_component("BoxCollider").is_trigger is False
    command.undo()
    assert instance.get_child(0).get_component("BoxCollider").is_trigger is True
    command.redo()
    assert instance.get_child(0).get_component("BoxCollider").is_trigger is False


def test_prefab_revert_undo_captures_inspector_builtin_wrapper_edit(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("CheckpointGate")
    source_child = scene.create_game_object("LeftPost")
    source_child.add_component("BoxCollider")
    source_child.set_parent(source)
    path = tmp_path / "checkpoint-inspector-revert-undo.prefab"
    assert save_prefab(source, str(path)) is True

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance.prefab_guid = "checkpoint-guid"
    instance.prefab_root = True
    child = instance.get_child(0)
    child.prefab_guid = "checkpoint-guid"
    raw_collider = child.get_component("BoxCollider")
    collider = BoxCollider._get_or_create_wrapper(raw_collider, child)

    previous_manager = UndoManager._instance
    manager = UndoManager()
    try:
        assert manager.execute(BuiltinPropertyCommand(
            collider,
            "is_trigger",
            False,
            True,
            "Set is_trigger",
        )) is True
        assert collider.is_trigger is True
        assert compute_overrides(instance, str(path))

        assert manager.execute(build_prefab_revert_command(instance, str(path))) is True
        assert instance.get_child(0).get_component("BoxCollider").is_trigger is False

        manager.undo()
        restored = instance.get_child(0).get_component("BoxCollider")
        assert restored.is_trigger is True
        assert compute_overrides(instance, str(path))
    finally:
        UndoManager._instance = previous_manager


def test_apply_child_override_does_not_bake_root_scene_placement(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("CheckpointGate")
    child = scene.create_game_object("LeftPost")
    child.add_component("BoxCollider")
    child.set_parent(source)
    path = tmp_path / "checkpoint-apply.prefab"
    assert save_prefab(source, str(path)) is True

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance.prefab_guid = "checkpoint-guid"
    instance.prefab_root = True
    instance_child = instance.get_child(0)
    instance_child.prefab_guid = "checkpoint-guid"
    instance.transform.position = Vector3(8.0, 0.0, 0.0)
    instance_child.get_component("BoxCollider").is_trigger = True

    assert apply_overrides_to_prefab(instance_child, str(path)) is True
    saved_root = _read_prefab_document(str(path))["root_object"]
    assert saved_root["transform"]["position"][0] == pytest.approx(0.0)
    saved_collider = saved_root["children"][0]["components"][0]
    assert saved_collider["data"]["is_trigger"] is True
    assert compute_overrides(instance, str(path)) == []


def test_apply_propagates_to_existing_instances_and_preserves_overrides(scene, tmp_path):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("CheckpointGate")
    child = scene.create_game_object("LeftPost")
    child.add_component("BoxCollider")
    child.set_parent(source)
    path = tmp_path / "checkpoint-propagate.prefab"
    assert save_prefab(source, str(path)) is True

    first = instantiate_prefab(file_path=str(path), scene=scene)
    second = instantiate_prefab(file_path=str(path), scene=scene)
    for instance in (first, second):
        instance.prefab_guid = "checkpoint-guid"
        instance.prefab_root = True
        instance.get_child(0).prefab_guid = "checkpoint-guid"

    first.name = "CheckpointGate_A"
    first.transform.position = Vector3(1.0, 0.0, 0.0)
    first_collider = first.get_child(0).get_component("BoxCollider")
    first_collider.size = Vector3(2.0, 1.0, 1.0)

    second.name = "CheckpointGate_B"
    second.transform.position = Vector3(5.0, 0.0, 0.0)
    second.get_child(0).get_component("BoxCollider").is_trigger = True

    assert apply_overrides_to_prefab(second, str(path)) is True

    refreshed_first = first.get_child(0).get_component("BoxCollider")
    refreshed_second = second.get_child(0).get_component("BoxCollider")
    assert refreshed_first.is_trigger is True
    assert refreshed_first.size.x == pytest.approx(2.0)
    assert refreshed_second.is_trigger is True
    assert first.name == "CheckpointGate_A"
    assert first.transform.position.x == pytest.approx(1.0)
    assert second.name == "CheckpointGate_B"
    assert second.transform.position.x == pytest.approx(5.0)
    first_overrides = compute_overrides(first, str(path))
    assert any("BoxCollider" in override.key for override in first_overrides)
    assert compute_overrides(second, str(path)) == []


def test_apply_overrides_transaction_restores_asset_and_instances_on_undo(
    scene,
    tmp_path,
):
    _PREFAB_TEMPLATE_CACHE.clear()
    source = scene.create_game_object("TransactionalPrefab")
    source.add_component("BoxCollider")
    path = tmp_path / "transactional.prefab"
    assert save_prefab(source, str(path)) is True

    instance = instantiate_prefab(file_path=str(path), scene=scene)
    instance.prefab_guid = "transactional-guid"
    instance.prefab_root = True
    instance.get_component("BoxCollider").is_trigger = True
    previous_manager = UndoManager._instance
    manager = UndoManager()
    try:
        assert manager.execute(build_prefab_apply_command(instance, str(path))) is True
        saved = _read_prefab_document(str(path))["root_object"]
        assert saved["components"][0]["data"]["is_trigger"] is True

        manager.undo()
        saved = _read_prefab_document(str(path))["root_object"]
        assert saved["components"][0]["data"]["is_trigger"] is False
        assert instance.get_component("BoxCollider").is_trigger is True

        manager.redo()
        saved = _read_prefab_document(str(path))["root_object"]
        assert saved["components"][0]["data"]["is_trigger"] is True
        assert instance.get_component("BoxCollider").is_trigger is True
    finally:
        UndoManager._instance = previous_manager


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.pop("root_object"),
        lambda document: document.__setitem__("unknown", True),
        lambda document: document["root_object"].__setitem__("unknown", True),
    ],
)
def test_prefab_reader_rejects_non_current_documents(scene, tmp_path, mutation):
    root = scene.create_game_object("StrictPrefab")
    valid_path = tmp_path / "valid.prefab"
    assert save_prefab(root, str(valid_path)) is True
    document = json.loads(valid_path.read_text(encoding="utf-8"))
    mutation(document)

    invalid_path = tmp_path / "invalid.prefab"
    invalid_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PrefabDocumentError):
        _read_prefab_document(str(invalid_path))
