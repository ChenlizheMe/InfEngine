"""Integration tests — Scene management and GameObject hierarchy (real engine)."""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from Infernux.lib import AssetRegistry, GameObject, InxMaterial, SceneManager, Vector3, PrimitiveType, quatf
from Infernux.components import (
    FieldType,
    InxComponent,
    SerializableObject,
    list_field,
    serialized_field,
)
from Infernux.components._cds_bridge import get_class_info
from Infernux.components.decorators import disallow_multiple, require_component
from Infernux.components.ref_wrappers import ComponentRef, GameObjectRef
from Infernux.engine.component_restore import (
    PythonComponentRestoreError,
    clone_game_object_transactionally,
    deserialize_scene_document_transactionally,
    deserialize_game_object_document_transactionally,
    instantiate_game_object_document_transactionally,
    instantiate_prepared_game_object_document,
    preflight_game_object_python_components,
    preflight_scene_python_components,
)
from Infernux.engine.scene_document_transaction import (
    SceneDocumentTransaction,
    SceneDocumentTransactionState,
)
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.prefab_manager import PrefabDocumentError, _strip_prefab_runtime_fields
from Infernux.instantiate import Instantiate


class _ExplodingSerializationComponent(InxComponent):
    def _serialize_fields_document(self) -> dict:
        raise RuntimeError("intentional serialization failure")


class _StrictSceneComponent(InxComponent):
    value: int = 7


class _ReplacementLifecycleSource(InxComponent):
    value: int = 13
    destroy_calls = 0

    def on_destroy(self):
        type(self).destroy_calls += 1


class _ReplacementLifecycleTarget(InxComponent):
    value: int = 0


class _AdditiveSceneComponent(InxComponent):
    value: int = 7
    label: str = "default"


class _ExplodingAfterDeserializeComponent(InxComponent):
    value: int = 11

    def on_after_deserialize(self):
        raise RuntimeError("intentional on_after_deserialize failure")


class _PublishBarrierComponent(InxComponent):
    value: int = 0
    _events = []

    def on_after_deserialize(self):
        if self._awake_called:
            raise RuntimeError("Awake ran before on_after_deserialize")
        if self._cpp_component.component_id != self.component_id:
            raise RuntimeError("stable component ID was not installed before callback")
        scene = self.game_object.scene
        published_count = sum(
            len(obj.get_py_components())
            for obj in scene.get_all_objects()
        )
        if published_count != 2:
            raise RuntimeError("Python graph was not fully attached before callback")
        type(self)._events.append(("after", self.value))

    def awake(self):
        type(self)._events.append(("awake", self.value))

    def reset(self):
        type(self)._events.append(("reset", self.value))


def _native_record(object_document: dict, type_name: str) -> dict:
    type_id = f"native:infernux.{type_name}"
    return next(record for record in object_document["components"] if record["type_id"] == type_id)


def _python_records(object_document: dict) -> list[dict]:
    return [
        record for record in object_document["components"]
        if str(record.get("type_id", "")).startswith("python:")
    ]


def _python_type_id(instance: InxComponent) -> str:
    component_type = type(instance)
    return (
        f"python:{instance._script_guid}:{component_type._get_type_guid()}:"
        f"{component_type.__module__}:{component_type.__qualname__}"
    )


@require_component("Rigidbody")
class _RequiresRigidbodyComponent(InxComponent):
    value: int = 1


@disallow_multiple
class _SingleInstanceSceneComponent(InxComponent):
    value: int = 1


def _cds_alive_count(component_type) -> int:
    class_info = get_class_info(component_type)
    assert class_info is not None
    from Infernux import lib
    return lib._cds_alive_count(class_info[0])


class _ObjectRefSceneComponent(InxComponent):
    target = serialized_field(default=None, field_type=FieldType.GAME_OBJECT)


class _ObjectGraphRefsComponent(InxComponent):
    target_object = serialized_field(default=None, field_type=FieldType.GAME_OBJECT)
    target_component = serialized_field(default=None, field_type=FieldType.COMPONENT)


class _CloneSettings(SerializableObject):
    gain: float = 1.0
    label: str = "default"


class _RichCloneComponent(InxComponent):
    count: int = 1
    title: str = "source"
    values: list = list_field(element_type=FieldType.INT, default=[])
    settings: _CloneSettings = serialized_field(default=_CloneSettings())
    target_object = serialized_field(default=None, field_type=FieldType.GAME_OBJECT)
    target_component = serialized_field(default=None, field_type=FieldType.COMPONENT)


def _capture_exception(callback, errors):
    try:
        callback()
    except Exception as exc:
        errors.append(exc)


# ═══════════════════════════════════════════════════════════════════════════
# Scene creation & querying
# ═══════════════════════════════════════════════════════════════════════════

class TestSceneLifecycle:
    def test_create_scene(self, scene):
        assert scene is not None
        assert scene.name == "pytest_scene"

    def test_active_scene(self, scene):
        sm = SceneManager.instance()
        assert sm.get_active_scene() is scene

    def test_scene_starts_empty(self, scene):
        assert len(scene.get_root_objects()) == 0
        assert len(scene.get_all_objects()) == 0


# ═══════════════════════════════════════════════════════════════════════════
# GameObject CRUD
# ═══════════════════════════════════════════════════════════════════════════

class TestGameObject:
    def test_python_component_serialization_failure_aborts_entire_document(self, scene):
        game_object = scene.create_game_object("BrokenWriter")
        game_object.add_py_component(_ExplodingSerializationComponent())

        with pytest.raises(RuntimeError, match="intentional serialization failure"):
            game_object.serialize_document()
        with pytest.raises(RuntimeError, match="intentional serialization failure"):
            scene.serialize_document()

    def test_create_game_object(self, scene):
        go = scene.create_game_object("TestObj")
        assert go.name == "TestObj"
        assert go.active is True

    def test_unique_ids(self, scene):
        a = scene.create_game_object("A")
        b = scene.create_game_object("B")
        assert a.id != b.id

    def test_find_by_name(self, scene):
        go = scene.create_game_object("Searchable")
        found = scene.find("Searchable")
        assert found is not None
        assert found.id == go.id

    def test_find_by_id(self, scene):
        go = scene.create_game_object("ById")
        found = scene.find_by_id(go.id)
        assert found is not None
        assert found.name == "ById"

    def test_object_handles_reject_rebuilt_scene_lifetimes(self, scene):
        go = scene.create_game_object("HandleOwner")
        rigidbody = go.add_component("Rigidbody")
        reference = GameObjectRef(go)
        component_reference = ComponentRef(go_id=go.id, component_type="Rigidbody")
        game_object_handle = go.handle
        transform_handle = go.transform.handle
        component_handle = rigidbody.handle

        assert game_object_handle.is_valid
        assert game_object_handle.world_id == scene.world_id
        assert scene.resolve_game_object(game_object_handle) is go
        assert scene.resolve_component(transform_handle) is go.transform
        assert scene.resolve_component(component_handle).handle == component_handle
        assert component_reference.resolve().handle == component_handle

        document = scene.serialize_document()
        assert scene._commit_document(document) is True

        restored = scene.find_by_id(game_object_handle.id)
        assert restored is not None
        assert restored.handle.generation != game_object_handle.generation
        assert restored.transform.handle.generation != transform_handle.generation
        assert restored.get_component("Rigidbody").handle.generation != component_handle.generation
        assert scene.resolve_game_object(game_object_handle) is None
        assert scene.resolve_component(transform_handle) is None
        assert scene.resolve_component(component_handle) is None
        assert reference.resolve() is restored
        assert component_reference.resolve().handle == restored.get_component("Rigidbody").handle

    def test_object_handles_are_scoped_to_the_scene_world(self, scene):
        go = scene.create_game_object("WorldScoped")
        wrong_world = type(go.handle)(go.id, go.handle.generation, scene.world_id + 1)

        assert scene.resolve_game_object(wrong_world) is None

    def test_find_nonexistent_returns_none(self, scene):
        assert scene.find("$$$nonexistent$$$") is None

    def test_get_all_objects(self, scene):
        scene.create_game_object("X")
        scene.create_game_object("Y")
        assert len(scene.get_all_objects()) == 2

    def test_destroy_game_object(self, scene):
        go = scene.create_game_object("Temp")
        scene.destroy_game_object(go)
        scene.process_pending_destroys()
        assert scene.find("Temp") is None

    def test_deactivate_game_object(self, scene):
        go = scene.create_game_object("Toggle")
        go.active = False
        assert go.active is False
        go.active = True
        assert go.active is True


# ═══════════════════════════════════════════════════════════════════════════
# Hierarchy (parent / child)
# ═══════════════════════════════════════════════════════════════════════════

class TestHierarchy:
    def test_set_parent(self, scene):
        parent = scene.create_game_object("Parent")
        child = scene.create_game_object("Child")
        child.set_parent(parent)
        assert child.get_parent().id == parent.id
        assert len(parent.get_children()) == 1

    def test_unparent(self, scene):
        parent = scene.create_game_object("P")
        child = scene.create_game_object("C")
        child.set_parent(parent)
        child.set_parent(None)
        assert child.get_parent() is None

    def test_root_objects_exclude_children(self, scene):
        parent = scene.create_game_object("Root")
        child = scene.create_game_object("Leaf")
        child.set_parent(parent)
        roots = scene.get_root_objects()
        root_ids = {o.id for o in roots}
        assert parent.id in root_ids
        assert child.id not in root_ids

    def test_multiple_children(self, scene):
        parent = scene.create_game_object("P")
        for i in range(5):
            c = scene.create_game_object(f"C{i}")
            c.set_parent(parent)
        assert len(parent.get_children()) == 5


# ═══════════════════════════════════════════════════════════════════════════
# Transform
# ═══════════════════════════════════════════════════════════════════════════

class TestTransform:
    def test_default_position_is_origin(self, scene):
        go = scene.create_game_object("T")
        pos = go.transform.position
        assert (pos.x, pos.y, pos.z) == pytest.approx((0, 0, 0))

    def test_set_position(self, scene):
        go = scene.create_game_object("T")
        go.transform.position = Vector3(1, 2, 3)
        pos = go.transform.position
        assert (pos.x, pos.y, pos.z) == pytest.approx((1, 2, 3))

    def test_local_vs_world_position(self, scene):
        parent = scene.create_game_object("P")
        parent.transform.position = Vector3(10, 0, 0)
        child = scene.create_game_object("C")
        child.set_parent(parent)
        child.transform.local_position = Vector3(0, 5, 0)
        world = child.transform.position
        assert world.x == pytest.approx(10)
        assert world.y == pytest.approx(5)

    def test_scale(self, scene):
        go = scene.create_game_object("S")
        go.transform.local_scale = Vector3(2, 3, 4)
        s = go.transform.local_scale
        assert (s.x, s.y, s.z) == pytest.approx((2, 3, 4))

    def test_rotation_euler(self, scene):
        go = scene.create_game_object("R")
        go.transform.euler_angles = Vector3(0, 90, 0)
        angles = go.transform.euler_angles
        assert angles.y == pytest.approx(90, abs=0.5)

    def test_component_writeback_on_position(self, scene):
        go = scene.create_game_object("Writeback")
        go.transform.position = Vector3(1, 2, 3)
        go.transform.position.x += 4.0
        go.transform.position.y = 9.0
        pos = go.transform.position
        assert (pos.x, pos.y, pos.z) == pytest.approx((5, 9, 3))

    def test_inplace_add_writeback_on_position(self, scene):
        go = scene.create_game_object("Inplace")
        go.transform.position = Vector3(1, 0, 0)
        go.transform.position += Vector3(2, 3, 4)
        pos = go.transform.position
        assert (pos.x, pos.y, pos.z) == pytest.approx((3, 3, 4))


# ═══════════════════════════════════════════════════════════════════════════
# Primitives
# ═══════════════════════════════════════════════════════════════════════════

class TestPrimitives:
    @staticmethod
    def _triangle_areas(positions, indices):
        areas = []
        for offset in range(0, len(indices), 3):
            a, b, c = (positions[indices[offset + corner]] for corner in range(3))
            ab = tuple(b[axis] - a[axis] for axis in range(3))
            ac = tuple(c[axis] - a[axis] for axis in range(3))
            cross = (
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            )
            areas.append(0.5 * math.sqrt(sum(component * component for component in cross)))
        return areas

    @pytest.mark.parametrize("ptype", [
        PrimitiveType.Cube,
        PrimitiveType.Sphere,
        PrimitiveType.Plane,
        PrimitiveType.Cylinder,
        PrimitiveType.Capsule,
    ])
    def test_create_primitive(self, scene, ptype):
        go = scene.create_primitive(ptype, f"Prim_{ptype.name}")
        assert go is not None
        comps = [c.type_name for c in go.get_components()]
        assert "Transform" in comps
        assert "MeshRenderer" in comps
        expected_collider = {
            PrimitiveType.Cube: "BoxCollider",
            PrimitiveType.Sphere: "SphereCollider",
            PrimitiveType.Capsule: "CapsuleCollider",
            PrimitiveType.Cylinder: "MeshCollider",
            PrimitiveType.Plane: "MeshCollider",
        }[ptype]
        assert expected_collider in comps

    def test_primitive_has_mesh_data(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "Cube")
        mr = cube.get_component("MeshRenderer")
        positions = mr.get_positions()
        indices = mr.get_indices()
        assert len(positions) > 0
        assert len(indices) > 0

    @pytest.mark.parametrize(
        "primitive_type,expected_mesh_name",
        [
            (PrimitiveType.Cube, "Cube"),
            (PrimitiveType.Quad, "Quad"),
        ],
    )
    def test_custom_named_primitives_serialize_as_compact_builtins(
        self, scene, primitive_type, expected_mesh_name
    ):
        primitive = scene.create_primitive(primitive_type, "CustomObjectName")
        document = primitive.get_component("MeshRenderer").serialize_document()

        assert document["inlineMeshName"] == expected_mesh_name
        assert document["inlineMeshBuiltin"] is True
        assert "inlineVertices" not in document
        assert "inlineIndices" not in document

    def test_batch_primitives_serialize_as_compact_builtins(self, scene):
        cubes = scene.create_primitives_batch(PrimitiveType.Cube, 3, "WaveCube")

        assert [cube.name for cube in cubes] == ["WaveCube_0", "WaveCube_1", "WaveCube_2"]
        for cube in cubes:
            document = cube.get_component("MeshRenderer").serialize_document()
            assert document["inlineMeshName"] == "Cube"
            assert document["inlineMeshBuiltin"] is True
            assert "inlineVertices" not in document
            assert "inlineIndices" not in document

    def test_sphere_is_uniform_geodesic_mesh_without_degenerate_poles(self, scene):
        sphere = scene.create_primitive(PrimitiveType.Sphere, "GeodesicSphere")
        renderer = sphere.get_component("MeshRenderer")
        positions = renderer.get_positions()
        indices = renderer.get_indices()

        radii = [math.sqrt(sum(component * component for component in position)) for position in positions]
        areas = self._triangle_areas(positions, indices)
        assert min(radii) == pytest.approx(0.5, abs=1e-5)
        assert max(radii) == pytest.approx(0.5, abs=1e-5)
        assert min(areas) > 1e-6
        assert max(areas) / min(areas) < 1.5

    def test_capsule_has_closed_hemispheres_and_nonzero_cylinder_section(self, scene):
        capsule = scene.create_primitive(PrimitiveType.Capsule, "TrueCapsule")
        renderer = capsule.get_component("MeshRenderer")
        positions = renderer.get_positions()
        indices = renderer.get_indices()
        areas = self._triangle_areas(positions, indices)

        assert min(position[1] for position in positions) == pytest.approx(-1.0, abs=1e-5)
        assert max(position[1] for position in positions) == pytest.approx(1.0, abs=1e-5)
        assert min(areas) > 1e-6
        assert max(indices) < len(positions)

        cylinder_triangles = 0
        for offset in range(0, len(indices), 3):
            ys = {round(positions[indices[offset + corner]][1], 5) for corner in range(3)}
            if 0.5 in ys and -0.5 in ys:
                cylinder_triangles += 1
        assert cylinder_triangles == 64

    @pytest.mark.parametrize(
        "primitive_type,expected_minimum,expected_maximum",
        [
            (PrimitiveType.Cube, (-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)),
            (PrimitiveType.Cylinder, (-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)),
            (PrimitiveType.Plane, (-0.5, 0.0, -0.5), (0.5, 0.0, 0.5)),
            (PrimitiveType.Quad, (-0.5, -0.5, 0.0), (0.5, 0.5, 0.0)),
        ],
    )
    def test_other_primitive_meshes_have_documented_unit_bounds_and_no_degenerate_triangles(
        self, scene, primitive_type, expected_minimum, expected_maximum
    ):
        primitive = scene.create_primitive(primitive_type, f"Audited{primitive_type.name}")
        renderer = primitive.get_component("MeshRenderer")
        positions = renderer.get_positions()
        indices = renderer.get_indices()
        minimum = tuple(min(position[axis] for position in positions) for axis in range(3))
        maximum = tuple(max(position[axis] for position in positions) for axis in range(3))

        assert minimum == pytest.approx(expected_minimum, abs=1e-5)
        assert maximum == pytest.approx(expected_maximum, abs=1e-5)
        assert min(self._triangle_areas(positions, indices)) > 1e-6
        assert max(indices) < len(positions)


# ═══════════════════════════════════════════════════════════════════════════
# Instantiate (clone)
# ═══════════════════════════════════════════════════════════════════════════

class TestInstantiate:
    def test_clone_game_object(self, scene):
        original = scene.create_game_object("Original")
        original.transform.position = Vector3(5, 5, 5)
        clone = scene._clone_game_object(original)
        assert clone is not None
        assert "Clone" in clone.name
        pos = clone.transform.position
        assert pos.x == pytest.approx(5)

    def test_clone_with_components(self, scene):
        go = scene.create_game_object("WithRB")
        rb = go.add_component("Rigidbody")
        rb.mass = 7.5
        clone = scene._clone_game_object(go)
        clone_rb = clone.get_component("Rigidbody")
        assert clone_rb is not None
        assert clone_rb.mass == pytest.approx(7.5)

    def test_transactional_clone_preserves_live_python_fields(self, scene):
        original = scene.create_game_object("PythonCloneSource")
        component = original.add_py_component(_StrictSceneComponent())
        component.value = 31

        clone = clone_game_object_transactionally(scene, original)

        assert clone is not None
        clone_component = clone.get_py_component(_StrictSceneComponent)
        assert clone_component is not None
        assert clone_component.value == 31

    def test_string_lookup_resolves_the_component_attached_to_each_object(self, scene):
        first_type = type("AttachedLookupTwin", (InxComponent,), {
            "__module__": "attached_lookup_first",
        })
        second_type = type("AttachedLookupTwin", (InxComponent,), {
            "__module__": "attached_lookup_second",
        })
        first_object = scene.create_game_object("FirstLookupOwner")
        second_object = scene.create_game_object("SecondLookupOwner")
        first_component = first_object.add_py_component(first_type())
        second_component = second_object.add_py_component(second_type())

        assert first_object.get_component("AttachedLookupTwin") is first_component
        assert second_object.get_component("AttachedLookupTwin") is second_component
        assert first_object.get_components("AttachedLookupTwin") == [first_component]
        assert second_object.get_components("AttachedLookupTwin") == [second_component]

    def test_transactional_clone_preserves_rich_fields_and_remaps_references(self, scene):
        original = scene.create_game_object("RichCloneSource")
        child = scene.create_game_object("RichCloneChild")
        child.set_parent(original)
        child.add_py_component(_StrictSceneComponent())
        component = original.add_py_component(_RichCloneComponent())
        component.count = 17
        component.title = "edited"
        component.values = [2, 3, 5, 8]
        component.settings = _CloneSettings(gain=2.5, label="nested")
        component.target_object = GameObjectRef(child)
        component.target_component = ComponentRef(
            go_id=child.id,
            component_type="_StrictSceneComponent",
        )

        clone = clone_game_object_transactionally(scene, original)

        clone_child = clone.get_child(0)
        restored = clone.get_py_component(_RichCloneComponent)
        assert restored.count == 17
        assert restored.title == "edited"
        assert restored.values == [2, 3, 5, 8]
        assert restored.settings.gain == pytest.approx(2.5)
        assert restored.settings.label == "nested"
        assert restored.target_object is clone_child
        assert restored.target_component is clone_child.get_py_component(_StrictSceneComponent)

    def test_transactional_clone_preserves_references_outside_the_cloned_subtree(self, scene):
        external = scene.create_game_object("ExternalCloneTarget")
        external_component = external.add_py_component(_StrictSceneComponent())
        original = scene.create_game_object("ExternalReferenceSource")
        component = original.add_py_component(_RichCloneComponent())
        component.target_object = GameObjectRef(external)
        component.target_component = ComponentRef(
            go_id=external.id,
            component_type="_StrictSceneComponent",
        )

        clone = clone_game_object_transactionally(scene, original)

        restored = clone.get_py_component(_RichCloneComponent)
        assert restored.target_object is external
        assert restored.target_component is external_component

    def test_unified_instantiate_for_game_object_ref_preserves_fields_and_parent(self, scene):
        parent = scene.create_game_object("InstantiateReferenceParent")
        original = scene.create_game_object("InstantiateReferenceSource")
        component = original.add_py_component(_RichCloneComponent())
        component.count = 610
        component.values = [3, 5, 8]
        component.settings = _CloneSettings(gain=1.25, label="reference")

        clone = Instantiate(GameObjectRef(original), parent=parent)

        assert clone is not None
        assert clone.get_parent() is parent
        restored = clone.get_py_component(_RichCloneComponent)
        assert restored.count == 610
        assert restored.values == [3, 5, 8]
        assert restored.settings.gain == pytest.approx(1.25)
        assert restored.settings.label == "reference"

    def test_game_object_instantiate_parent_overloads_follow_unity_transform_space(self, scene):
        source_parent = scene.create_game_object("InstantiateSourceParent")
        source_parent.transform.position = Vector3(10.0, 0.0, 0.0)
        source = scene.create_game_object("InstantiateTransformSource")
        source.set_parent(source_parent, False)
        source.transform.local_position = Vector3(2.0, 3.0, 4.0)

        target_parent = scene.create_game_object("InstantiateTargetParent")
        target_parent.transform.position = Vector3(20.0, 0.0, 0.0)

        detached = GameObject.instantiate(source)
        assert detached.get_parent() is None
        assert detached.transform.position.to_tuple() == pytest.approx((12.0, 3.0, 4.0))

        explicit_root = GameObject.instantiate(source, parent=None)
        assert explicit_root.get_parent() is None
        assert explicit_root.transform.position.to_tuple() == pytest.approx((2.0, 3.0, 4.0))

        unified_detached = Instantiate(source)
        assert unified_detached.get_parent() is None
        assert unified_detached.transform.position.to_tuple() == pytest.approx((12.0, 3.0, 4.0))

        local_clone = GameObject.instantiate(source, target_parent)
        assert local_clone.get_parent() is target_parent
        assert local_clone.transform.local_position.to_tuple() == pytest.approx((2.0, 3.0, 4.0))
        assert local_clone.transform.position.to_tuple() == pytest.approx((22.0, 3.0, 4.0))

        keyword_clone = GameObject.instantiate(source, parent=target_parent)
        assert keyword_clone.transform.local_position.to_tuple() == pytest.approx((2.0, 3.0, 4.0))

        world_clone = GameObject.instantiate(source, target_parent, True)
        assert world_clone.get_parent() is target_parent
        assert world_clone.transform.position.to_tuple() == pytest.approx((12.0, 3.0, 4.0))

        positioned = GameObject.instantiate(
            source,
            Vector3(30.0, 5.0, 6.0),
            quatf(),
            target_parent,
        )
        assert positioned.get_parent() is target_parent
        assert positioned.transform.position.to_tuple() == pytest.approx((30.0, 5.0, 6.0))

    def test_clipboard_document_pipeline_preserves_rich_python_fields(self, scene):
        original = scene.create_game_object("ClipboardSource")
        component = original.add_py_component(_RichCloneComponent())
        component.count = 23
        component.title = "clipboard"
        component.values = [13, 21]
        component.settings = _CloneSettings(gain=4.0, label="copied")
        document = copy.deepcopy(original.serialize_document())
        _strip_prefab_runtime_fields(document)
        prepared = preflight_game_object_python_components(
            document,
            preserve_document_ids=False,
        )

        pasted = instantiate_prepared_game_object_document(
            scene,
            document,
            prepared,
        )

        restored = pasted.get_py_component(_RichCloneComponent)
        assert restored.count == 23
        assert restored.title == "clipboard"
        assert restored.values == [13, 21]
        assert restored.settings.gain == pytest.approx(4.0)
        assert restored.settings.label == "copied"

    def test_hierarchy_copy_paste_preserves_rich_fields_and_references(self, scene):
        original = scene.create_game_object("HierarchyClipboardSource")
        child = scene.create_game_object("HierarchyClipboardChild")
        child.set_parent(original)
        child.add_py_component(_StrictSceneComponent())
        component = original.add_py_component(_RichCloneComponent())
        component.count = 29
        component.title = "ctrl-cv"
        component.values = [34, 55]
        component.settings = _CloneSettings(gain=6.5, label="hierarchy")
        component.target_object = GameObjectRef(child)
        component.target_component = ComponentRef(
            go_id=child.id,
            component_type="_StrictSceneComponent",
        )

        class Selection:
            def __init__(self):
                self.ids = [original.id]

            def get_ids(self):
                return list(self.ids)

            def get_primary(self):
                return self.ids[-1] if self.ids else 0

            def count(self):
                return len(self.ids)

            def set_ids(self, ids):
                self.ids = list(ids)

            def clear(self):
                self.ids.clear()

        selection = Selection()
        hierarchy = SimpleNamespace(on_selection_changed=None)
        bootstrap = SimpleNamespace(
            scene_file_manager=None,
            scene_view=None,
            engine=None,
        )
        from Infernux.engine.bootstrap_hierarchy._prefab_clipboard import wire_clipboard

        wire_clipboard(SimpleNamespace(hp=hierarchy, bs=bootstrap, sel=selection))
        assert hierarchy.copy_selected(False) is True
        assert hierarchy.paste_clipboard() is True

        pasted = scene.find_by_id(selection.get_primary())
        pasted_child = pasted.get_child(0)
        restored = pasted.get_py_component(_RichCloneComponent)
        assert restored.count == 29
        assert restored.title == "ctrl-cv"
        assert restored.values == [34, 55]
        assert restored.settings.gain == pytest.approx(6.5)
        assert restored.settings.label == "hierarchy"
        assert restored.target_object is pasted_child
        assert restored.target_component is pasted_child.get_py_component(_StrictSceneComponent)

    def test_hierarchy_copy_paste_preserves_external_python_references(self, scene):
        external = scene.create_game_object("ClipboardExternalTarget")
        external_component = external.add_py_component(_StrictSceneComponent())
        original = scene.create_game_object("ClipboardExternalSource")
        component = original.add_py_component(_RichCloneComponent())
        component.target_object = GameObjectRef(external)
        component.target_component = ComponentRef(
            go_id=external.id,
            component_type="_StrictSceneComponent",
        )

        class Selection:
            def __init__(self):
                self.ids = [original.id]

            def get_ids(self): return list(self.ids)
            def get_primary(self): return self.ids[-1] if self.ids else 0
            def count(self): return len(self.ids)
            def set_ids(self, ids): self.ids = list(ids)
            def clear(self): self.ids.clear()

        selection = Selection()
        hierarchy = SimpleNamespace(on_selection_changed=None)
        bootstrap = SimpleNamespace(scene_file_manager=None, scene_view=None, engine=None)
        from Infernux.engine.bootstrap_hierarchy._prefab_clipboard import wire_clipboard

        wire_clipboard(SimpleNamespace(hp=hierarchy, bs=bootstrap, sel=selection))
        assert hierarchy.copy_selected(False) is True
        assert hierarchy.paste_clipboard() is True

        pasted = scene.find_by_id(selection.get_primary())
        restored = pasted.get_py_component(_RichCloneComponent)
        assert restored.target_object is external
        assert restored.target_component is external_component

    def test_multi_root_clipboard_remaps_cross_root_python_references(self, scene):
        first = scene.create_game_object("ClipboardGroupFirst")
        first_refs = first.add_py_component(_RichCloneComponent())
        second = scene.create_game_object("ClipboardGroupSecond")
        second_component = second.add_py_component(_StrictSceneComponent())
        first_refs.target_object = GameObjectRef(second)
        first_refs.target_component = ComponentRef(
            go_id=second.id,
            component_type="_StrictSceneComponent",
        )

        class Selection:
            def __init__(self): self.ids = [first.id, second.id]
            def get_ids(self): return list(self.ids)
            def get_primary(self): return self.ids[-1] if self.ids else 0
            def count(self): return len(self.ids)
            def set_ids(self, ids): self.ids = list(ids)
            def clear(self): self.ids.clear()

        selection = Selection()
        hierarchy = SimpleNamespace(on_selection_changed=None)
        bootstrap = SimpleNamespace(scene_file_manager=None, scene_view=None, engine=None)
        from Infernux.engine.bootstrap_hierarchy._prefab_clipboard import wire_clipboard

        wire_clipboard(SimpleNamespace(hp=hierarchy, bs=bootstrap, sel=selection))
        assert hierarchy.copy_selected(False) is True
        assert hierarchy.paste_clipboard() is True

        copied_first = scene.find_by_id(selection.ids[0])
        copied_second = scene.find_by_id(selection.ids[1])
        copied_refs = copied_first.get_py_component(_RichCloneComponent)
        assert copied_refs.target_object is copied_second
        assert copied_refs.target_object is not second
        assert copied_refs.target_component is copied_second.get_py_component(
            _StrictSceneComponent
        )
        assert copied_refs.target_component is not second_component

    def test_component_clipboard_preserves_fields_without_copying_component_id(self, scene):
        source_object = scene.create_game_object("ComponentClipboardSource")
        source = source_object.add_py_component(_RichCloneComponent())
        source.count = 144
        source.values = [8, 13, 21]
        source.settings = _CloneSettings(gain=9.5, label="component-copy")
        target_object = scene.create_game_object("ComponentClipboardTarget")
        target = target_object.add_py_component(_RichCloneComponent())
        target_component_id = target.component_id

        from Infernux.engine.bootstrap_inspector._wire import (
            _apply_python_component_clipboard_document,
            _python_component_clipboard_document,
        )

        payload = _python_component_clipboard_document(source)
        assert "__component_id__" not in payload
        _apply_python_component_clipboard_document(target, payload)

        assert target.component_id == target_component_id
        assert target.count == 144
        assert target.values == [8, 13, 21]
        assert target.settings.gain == pytest.approx(9.5)
        assert target.settings.label == "component-copy"
        assert target.settings is not source.settings

    def test_component_clipboard_undo_targets_exact_duplicate_component(self, scene):
        owner = scene.create_game_object("DuplicateComponentClipboardTarget")
        first = owner.add_py_component(_RichCloneComponent())
        second = owner.add_py_component(_RichCloneComponent())
        first.count = 1
        second.count = 2
        source_owner = scene.create_game_object("DuplicateComponentClipboardSource")
        source = source_owner.add_py_component(_RichCloneComponent())
        source.count = 233

        from Infernux.engine.bootstrap_inspector._wire import (
            _python_component_clipboard_document,
        )
        from Infernux.engine.undo import PythonComponentDocumentCommand

        command = PythonComponentDocumentCommand(
            second,
            _python_component_clipboard_document(second),
            _python_component_clipboard_document(source),
            "Paste duplicate component properties",
        )
        command.execute()
        assert first.count == 1
        assert second.count == 233

        command.undo()
        assert first.count == 1
        assert second.count == 2

        command.redo()
        assert first.count == 1
        assert second.count == 233

    def test_property_undo_targets_exact_duplicate_python_component(self, scene):
        owner = scene.create_game_object("DuplicatePropertyTarget")
        first = owner.add_py_component(_RichCloneComponent())
        second = owner.add_py_component(_RichCloneComponent())
        first.count = 3
        second.count = 5

        from Infernux.engine.undo import SetPropertyCommand

        command = SetPropertyCommand(second, "count", 5, 8, "Set second count")
        command.execute()
        assert first.count == 3
        assert second.count == 8
        command.undo()
        assert first.count == 3
        assert second.count == 5

    def test_python_component_add_undo_redo_preserves_loaded_type_and_fields(self, scene):
        owner = scene.create_game_object("PythonComponentUndoOwner")
        component = owner.add_py_component(_RichCloneComponent())
        component.count = 377
        component.values = [34, 55, 89]
        component.settings = _CloneSettings(gain=12.5, label="redo")

        from Infernux.engine.undo import AddPyComponentCommand

        command = AddPyComponentCommand(owner.id, component, "Add rich component")
        command.undo()
        assert owner.get_py_component(_RichCloneComponent) is None

        command.redo()
        restored = owner.get_py_component(_RichCloneComponent)
        assert type(restored) is type(component)
        assert restored.count == 377
        assert restored.values == [34, 55, 89]
        assert restored.settings.gain == pytest.approx(12.5)
        assert restored.settings.label == "redo"

    def test_repeated_asset_script_clone_preserves_fields_across_module_reload(
        self, scene, tmp_path
    ):
        script_path = tmp_path / "pipe_probe.py"
        script_path.write_text(
            "from Infernux.components import InxComponent\n\n"
            "class PipeProbe(InxComponent):\n"
            "    speed: float = 2.8\n"
            "    wrap_distance: float = 18.0\n",
            encoding="utf-8",
        )

        class AssetDatabase:
            @staticmethod
            def get_guid_from_path(path):
                return "pipe-probe-guid" if str(path) == str(script_path) else ""

            @staticmethod
            def get_path_from_guid(guid):
                return str(script_path) if guid == "pipe-probe-guid" else ""

        from Infernux.components.script_loader import load_and_create_component

        original = scene.create_game_object("AssetScriptCloneSource")
        source_component = load_and_create_component(
            str(script_path),
            asset_database=AssetDatabase(),
            type_name="PipeProbe",
            script_guid="pipe-probe-guid",
        )
        original.add_py_component(source_component)
        source_component.speed = 6.25
        source_component.wrap_distance = 42.0

        for _ in range(4):
            clone = GameObject.instantiate(original)
            restored_components = list(clone.get_py_components() or ())
            assert len(restored_components) == 1, [
                (type(item).__name__, getattr(item, "_broken_error", ""))
                for item in restored_components
            ]
            restored = restored_components[0]
            assert type(restored) is type(source_component)
            assert clone.get_component("PipeProbe") is restored, (
                type(restored).__name__,
                getattr(restored, "type_name", ""),
            )
            assert restored.speed == pytest.approx(6.25)
            assert restored.wrap_distance == pytest.approx(42.0)

        from Infernux.engine.prefab_manager import (
            _PREFAB_TEMPLATE_CACHE,
            instantiate_prefab,
            save_prefab,
        )

        prefab_path = tmp_path / "pipe_probe.prefab"
        _PREFAB_TEMPLATE_CACHE.clear()
        assert save_prefab(original, str(prefab_path)) is True
        for _ in range(3):
            instance = instantiate_prefab(
                file_path=str(prefab_path),
                scene=scene,
                asset_database=AssetDatabase(),
            )
            restored = list(instance.get_py_components() or ())[0]
            assert restored.speed == pytest.approx(6.25)
            assert restored.wrap_distance == pytest.approx(42.0)

    def test_structural_undo_restores_rich_python_fields(self, scene):
        original = scene.create_game_object("UndoRichSource")
        original_id = original.id
        component = original.add_py_component(_RichCloneComponent())
        component.count = 89
        component.title = "undo"
        component.values = [1, 1, 2, 3, 5]
        component.settings = _CloneSettings(gain=7.5, label="restored")

        from Infernux.engine.undo import DeleteGameObjectCommand

        command = DeleteGameObjectCommand(original_id, "Delete rich source")
        command.execute()
        scene.process_pending_destroys()
        assert scene.find_by_id(original_id) is None

        command.undo()

        restored_object = scene.find_by_id(original_id)
        restored = restored_object.get_py_component(_RichCloneComponent)
        assert restored.count == 89
        assert restored.title == "undo"
        assert restored.values == [1, 1, 2, 3, 5]
        assert restored.settings.gain == pytest.approx(7.5)
        assert restored.settings.label == "restored"


# ═══════════════════════════════════════════════════════════════════════════
# Scene serialization
# ═══════════════════════════════════════════════════════════════════════════

class TestSceneSerialization:
    def test_native_play_snapshot_restores_scene_without_python_document(self, scene):
        source = scene.create_game_object("NativeSnapshotSource")
        source_id = source.id
        source.transform.position = Vector3(1.0, 2.0, 3.0)
        snapshot = scene._capture_play_mode_snapshot()

        source.name = "NativeSnapshotMutated"
        source.transform.position = Vector3(9.0, 8.0, 7.0)
        transaction = SceneDocumentTransaction(
            scene,
            document=snapshot,
            borrow_document=True,
        )

        assert transaction.run_to_completion() is True
        restored = scene.find_by_id(source_id)
        assert restored is not None
        assert restored.name == "NativeSnapshotSource"
        position = restored.transform.position
        assert (position.x, position.y, position.z) == pytest.approx((1.0, 2.0, 3.0))
        assert transaction.phase_timings_ms["resources"] >= 0.0
        assert transaction.phase_timings_ms["native_commit"] >= 0.0

    def test_scene_document_transaction_has_explicit_owner_thread_phases(self, scene):
        existing = scene.create_game_object("TransactionPhaseSource")
        document = scene.serialize_document()
        document["objects"][0]["name"] = "TransactionPhaseTarget"
        transaction = SceneDocumentTransaction(scene, document=document)

        transaction.start()
        assert transaction.state is SceneDocumentTransactionState.DOCUMENT_READY
        assert transaction.poll() is False
        assert transaction.state is SceneDocumentTransactionState.RESOURCES_READY
        assert scene.find("TransactionPhaseSource") is existing
        assert transaction.poll() is False
        assert transaction.state is SceneDocumentTransactionState.READY_TO_COMMIT
        assert scene.find("TransactionPhaseSource") is existing

        assert transaction.poll() is True
        assert transaction.state is SceneDocumentTransactionState.COMPLETED
        assert scene.find("TransactionPhaseSource") is None
        assert scene.find("TransactionPhaseTarget") is not existing

    def test_path_scene_transaction_reads_and_validates_on_worker(self, scene, tmp_path):
        scene.create_game_object("WorkerReadSource")
        document = scene.serialize_document()
        document["objects"][0]["name"] = "WorkerReadTarget"
        path = tmp_path / "worker-read.scene"
        path.write_text(json.dumps(document), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is True
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.COMPLETED
        assert scene.find("WorkerReadTarget") is not None

    def test_worker_structural_failure_preserves_live_scene(self, scene, tmp_path):
        existing = scene.create_game_object("WorkerFailureExisting")
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        candidate["legacy"] = True
        path = tmp_path / "invalid-worker.scene"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.FAILED
        assert "unknown field 'legacy'" in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("WorkerFailureExisting") is existing

    @pytest.mark.parametrize(
        "corruption",
        [
            "transform_missing_scale",
            "rigidbody_mass",
            "box_size",
            "sphere_radius",
            "capsule_direction",
            "mesh_convex",
        ],
    )
    def test_worker_type_validator_rejects_physics_document_before_publish(
        self,
        scene,
        tmp_path,
        corruption,
    ):
        existing = scene.create_game_object("WorkerTypedValidationExisting")
        component_type = None
        if corruption == "rigidbody_mass":
            component_type = "Rigidbody"
        elif corruption == "box_size":
            component_type = "BoxCollider"
        elif corruption == "sphere_radius":
            component_type = "SphereCollider"
        elif corruption == "capsule_direction":
            component_type = "CapsuleCollider"
        elif corruption == "mesh_convex":
            component_type = "MeshCollider"
        component = existing.transform if component_type is None else existing.add_component(component_type)
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        object_document = candidate["objects"][0]

        if corruption == "transform_missing_scale":
            object_document["transform"].pop("scale")
            expected_path = "Scene.objects[0].transform"
        else:
            component_document = object_document["components"][0]["data"]
            expected_path = "Scene.objects[0].components[0]"
            if corruption == "rigidbody_mass":
                component_document["mass"] = "heavy"
            elif corruption == "box_size":
                component_document["size"] = [1.0, 0.0, 1.0]
            elif corruption == "sphere_radius":
                component_document["radius"] = 0.0
            elif corruption == "capsule_direction":
                component_document["direction"] = 9
            else:
                component_document["convex"] = "yes"

        path = tmp_path / f"invalid-{corruption}.scene"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.FAILED
        assert transaction.document is None
        assert expected_path in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("WorkerTypedValidationExisting") is existing
        if component_type is None:
            assert existing.transform is component
        else:
            assert existing.get_component(component_type) is component

    def test_worker_accepts_current_schema_for_every_registered_native_component(self, scene, tmp_path):
        component_types = [
            "Camera",
            "Light",
            "AudioListener",
            "AudioSource",
            "BoxCollider",
            "SphereCollider",
            "CapsuleCollider",
            "MeshCollider",
            "MeshRenderer",
            "SkinnedMeshRenderer",
            "SpriteRenderer",
        ]
        for component_type in component_types:
            owner = scene.create_game_object(f"Validated{component_type}")
            owner.add_component(component_type)
        rigidbody_owner = scene.create_game_object("ValidatedRigidbody")
        rigidbody_owner.add_component("BoxCollider")
        rigidbody_owner.add_component("Rigidbody")

        document = scene.serialize_document()
        path = tmp_path / "all-native-component-schemas.scene"
        path.write_text(json.dumps(document), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is True
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.COMPLETED
        for component_type in component_types:
            assert scene.find(f"Validated{component_type}").get_component(component_type) is not None
        assert scene.find("ValidatedRigidbody").get_component("Rigidbody") is not None

    @pytest.mark.parametrize(
        "component_type,corruption",
        [
            ("Camera", "camera_clips"),
            ("Light", "light_type"),
            ("AudioSource", "audio_tracks"),
            ("MeshRenderer", "mesh_material"),
            ("SpriteRenderer", "sprite_color"),
        ],
    )
    def test_worker_type_validator_rejects_render_and_audio_documents(
        self,
        scene,
        tmp_path,
        component_type,
        corruption,
    ):
        existing = scene.create_game_object("WorkerRenderAudioExisting")
        component = existing.add_component(component_type)
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        component_document = candidate["objects"][0]["components"][0]["data"]

        if corruption == "camera_clips":
            component_document["farClip"] = component_document["nearClip"]
        elif corruption == "light_type":
            component_document["lightType"] = 99
        elif corruption == "audio_tracks":
            component_document["track_count"] = 2
        elif corruption == "mesh_material":
            component_document["materials"] = [{"material": "not-a-document"}]
        else:
            component_document["spriteColor"] = [1.0, 1.0, 1.0]

        path = tmp_path / f"invalid-{corruption}.scene"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.FAILED
        assert transaction.document is None
        assert "Scene.objects[0].components[0]" in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("WorkerRenderAudioExisting") is existing
        assert existing.get_component(component_type) is component

    def test_worker_type_validator_rejects_removed_ordinary_field(self, scene, tmp_path):
        existing = scene.create_game_object("WorkerRemovedField")
        existing.add_component("SkinnedMeshRenderer")
        candidate = json.loads(json.dumps(scene.serialize_document()))
        component_document = candidate["objects"][0]["components"][0]["data"]
        component_document["sourceModelGuid"] = "removed"
        path = tmp_path / "removed-skinned-field.scene"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.FAILED
        loaded = scene.find("WorkerRemovedField").get_component("SkinnedMeshRenderer")
        assert loaded is not None
        assert "sourceModelGuid" not in loaded.serialize_document()

    @pytest.mark.parametrize("failure", ["missing_guid", "wrong_type", "embedded_material"])
    def test_resource_preflight_failure_preserves_live_scene(self, scene, tmp_path, failure):
        existing = scene.create_game_object("ResourcePreflightExisting")
        renderer = existing.add_component("MeshRenderer")
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        renderer_document = candidate["objects"][0]["components"][0]["data"]

        if failure == "missing_guid":
            renderer_document["meshAssetGuid"] = "missing-scene-resource-guid"
        elif failure == "wrong_type":
            asset_database = AssetRegistry.instance().get_asset_database()
            asset_path = Path(asset_database.assets_root) / f"{tmp_path.name}-wrong-type.physicMaterial"
            asset_path.write_text(
                json.dumps(
                    {
                        "friction": 0.5,
                        "bounciness": 0.0,
                        "friction_combine": 0,
                        "bounce_combine": 0,
                    }
                ),
                encoding="utf-8",
            )
            renderer_document["meshAssetGuid"] = asset_database.import_asset(str(asset_path)).guid
        else:
            renderer_document["materials"] = [{"material": {}}]

        path = tmp_path / f"resource-{failure}.scene"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.ran_on_worker is True
        assert transaction.state is SceneDocumentTransactionState.FAILED
        if failure == "embedded_material":
            assert transaction.document is None
        else:
            assert transaction.document is not None
        assert "Scene.objects[0].components[0]" in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("ResourcePreflightExisting") is existing
        assert existing.get_component("MeshRenderer") is renderer

    def test_resource_preflight_accepts_matching_asset_type(self, scene, tmp_path):
        existing = scene.create_game_object("ResourcePreflightSuccess")
        existing.add_component("BoxCollider")
        asset_database = AssetRegistry.instance().get_asset_database()
        asset_path = Path(asset_database.assets_root) / f"{tmp_path.name}-valid.physicMaterial"
        asset_path.write_text(
            json.dumps(
                {
                    "friction": 0.65,
                    "bounciness": 0.1,
                    "friction_combine": 2,
                    "bounce_combine": 3,
                }
            ),
            encoding="utf-8",
        )
        material_guid = asset_database.import_asset(str(asset_path)).guid
        document = scene.serialize_document()
        document["objects"][0]["components"][0]["data"]["physic_material_guid"] = material_guid
        path = tmp_path / "valid-resource.scene"
        path.write_text(json.dumps(document), encoding="utf-8")
        transaction = SceneDocumentTransaction(scene, path=path)

        assert transaction.run_to_completion(raise_on_failure=False) is True
        assert transaction.state is SceneDocumentTransactionState.COMPLETED
        restored = scene.find("ResourcePreflightSuccess").get_component("BoxCollider")
        assert restored.serialize_document()["physic_material_guid"] == material_guid

    def test_resource_preflight_accepts_explicitly_cleared_embedded_texture(self, scene):
        owner = scene.create_game_object("ClearedEmbeddedTexture")
        renderer = owner.add_component("MeshRenderer")
        material = InxMaterial.create_default_unlit()
        material.set_texture("texSampler", "white")
        material.clear_texture("texSampler")
        renderer.material = material

        transaction = SceneDocumentTransaction(scene, document=scene.serialize_document())

        assert transaction.run_to_completion(raise_on_failure=False) is True
        restored = scene.find("ClearedEmbeddedTexture").get_component("MeshRenderer").material
        assert restored.get_texture("texSampler") == ""

    def test_scene_file_manager_polls_path_transaction_across_frames(
        self,
        scene,
        tmp_path,
        monkeypatch,
    ):
        from Infernux.engine.project_context import get_project_root, set_project_root

        previous_root = get_project_root()
        previous_manager = SceneFileManager._instance
        project_root = tmp_path / "Project"
        assets = project_root / "Assets"
        assets.mkdir(parents=True)
        scene.create_game_object("DeferredManagerSource")
        document = scene.serialize_document()
        document["objects"][0]["name"] = "DeferredManagerTarget"
        path = assets / "deferred.scene"
        path.write_text(json.dumps(document), encoding="utf-8")

        try:
            set_project_root(str(project_root))
            manager = SceneFileManager()
            monkeypatch.setattr(manager, "_prepare_native_scene_swap", lambda: None)
            monkeypatch.setattr(manager, "_reset_undo_history", lambda **_kwargs: None)
            monkeypatch.setattr(manager, "_restore_camera_state", lambda _path: None)
            monkeypatch.setattr(manager, "_remember_last_scene", lambda _path: None)
            monkeypatch.setattr(manager, "sync_all_prefab_instances", lambda _scene: None)

            manager._begin_deferred_open(str(path))
            manager.poll_deferred_load()
            assert manager.is_loading is True
            assert manager.current_scene_path is None

            deadline = time.monotonic() + 2.0
            while manager.is_loading and time.monotonic() < deadline:
                manager.poll_deferred_load()
                time.sleep(0.001)

            assert manager.is_loading is False
            assert manager.current_scene_path == os.path.abspath(path)
            assert scene.find("DeferredManagerTarget") is not None
        finally:
            SceneFileManager._instance = previous_manager
            set_project_root(previous_root)

    def test_scene_file_manager_rejects_second_open_while_first_is_deferred(self):
        previous_manager = SceneFileManager._instance
        try:
            manager = SceneFileManager()

            assert manager.open_scene("first.scene") is True
            assert manager.is_loading is True
            assert manager.open_scene("second.scene") is False
            assert manager._deferred_load_path == "first.scene"
        finally:
            SceneFileManager._instance = previous_manager

    def test_runtime_scene_manager_retains_pending_transaction_until_commit(
        self,
        scene,
        tmp_path,
        monkeypatch,
    ):
        from Infernux.engine.project_context import get_project_root, set_project_root
        from Infernux.scene import SceneManager as RuntimeSceneManager

        previous_root = get_project_root()
        previous_manager = SceneFileManager._instance
        project_root = tmp_path / "RuntimeProject"
        assets = project_root / "Assets"
        assets.mkdir(parents=True)
        scene.create_game_object("RuntimeDeferredSource")
        document = scene.serialize_document()
        document["objects"][0]["name"] = "RuntimeDeferredTarget"
        path = assets / "runtime-deferred.scene"
        path.write_text(json.dumps(document), encoding="utf-8")

        try:
            set_project_root(str(project_root))
            manager = SceneFileManager()
            reset_undo_calls = []
            remembered_paths = []
            monkeypatch.setattr(manager, "_prepare_native_scene_swap", lambda: None)
            monkeypatch.setattr(manager, "_reset_undo_history", lambda **kwargs: reset_undo_calls.append(kwargs))
            monkeypatch.setattr(manager, "_restore_camera_state", lambda _path: None)
            monkeypatch.setattr(manager, "_remember_last_scene", lambda value: remembered_paths.append(value))
            monkeypatch.setattr(manager, "sync_all_prefab_instances", lambda _scene: None)

            RuntimeSceneManager._pending_scene_load = str(path)
            RuntimeSceneManager.process_pending_load()
            assert RuntimeSceneManager._active_scene_transaction is not None
            assert manager.current_scene_path is None

            deadline = time.monotonic() + 2.0
            while (
                RuntimeSceneManager._active_scene_transaction is not None
                and time.monotonic() < deadline
            ):
                RuntimeSceneManager.process_pending_load()
                time.sleep(0.001)

            assert RuntimeSceneManager._active_scene_transaction is None
            assert manager.current_scene_path == os.path.abspath(path)
            assert scene.find("RuntimeDeferredTarget") is not None
            assert scene.is_playing() is True
            assert reset_undo_calls == []
            assert remembered_paths == []
        finally:
            transaction = RuntimeSceneManager._active_scene_transaction
            if transaction is not None and not transaction.is_complete:
                transaction.cancel()
            RuntimeSceneManager._pending_scene_load = None
            RuntimeSceneManager._active_scene_transaction = None
            RuntimeSceneManager._active_scene_load_path = None
            RuntimeSceneManager._active_scene_file_manager = None
            SceneFileManager._instance = previous_manager
            set_project_root(previous_root)

    def test_scene_transaction_cancel_and_owner_thread_guard(self, scene):
        existing = scene.create_game_object("CancelledTransactionExisting")
        transaction = SceneDocumentTransaction(scene, document=scene.serialize_document())
        transaction.start()
        errors = []

        thread = threading.Thread(target=lambda: _capture_exception(transaction.poll, errors))
        thread.start()
        thread.join(timeout=2.0)

        assert len(errors) == 1
        assert "owner thread" in str(errors[0])
        assert transaction.cancel() is True
        assert transaction.state is SceneDocumentTransactionState.CANCELLED
        assert transaction.poll() is True
        assert scene.find("CancelledTransactionExisting") is existing

    def test_scene_transaction_cancel_discards_prepared_component_slots(self, scene):
        owner = scene.create_game_object("CancelPreparedGraph")
        owner.add_py_component(_StrictSceneComponent())
        alive_before = _cds_alive_count(_StrictSceneComponent)
        transaction = SceneDocumentTransaction(scene, document=scene.serialize_document())

        transaction.start()
        assert transaction.poll() is False
        assert transaction.state is SceneDocumentTransactionState.RESOURCES_READY
        assert transaction.poll() is False
        assert transaction.state is SceneDocumentTransactionState.READY_TO_COMMIT
        assert _cds_alive_count(_StrictSceneComponent) == alive_before + 1

        assert transaction.cancel() is True
        assert transaction.state is SceneDocumentTransactionState.CANCELLED
        assert _cds_alive_count(_StrictSceneComponent) == alive_before

    def test_serialize_produces_json(self, scene):
        scene.create_game_object("SerObj")
        json_str = scene.serialize()
        assert len(json_str) > 0
        assert "SerObj" in json_str

    def test_save_and_load(self, scene):
        go = scene.create_game_object("Persistent")
        go.transform.position = Vector3(42, 0, 0)
        json_str = scene.serialize()

        # Create a new scene and deserialize
        sm = SceneManager.instance()
        scene2 = sm.create_scene("loaded_scene")
        sm.set_active_scene(scene2)
        assert deserialize_scene_document_transactionally(scene2, json.loads(json_str)) is True
        assert not hasattr(scene2, "deserialize")
        assert not hasattr(scene2, "load_from_file")
        found = scene2.find("Persistent")
        assert found is not None
        assert found.transform.position.x == pytest.approx(42)

    def test_save_to_file_atomically_replaces_existing_content(self, scene, tmp_path):
        scene.create_game_object("AtomicSceneObject")
        scene_path = tmp_path / "atomic.scene"
        scene_path.write_text("old scene content", encoding="utf-8")

        assert scene.save_to_file(str(scene_path)) is True

        document = json.loads(scene_path.read_text(encoding="utf-8"))
        assert document["objects"][0]["name"] == "AtomicSceneObject"
        assert list(tmp_path.glob("atomic.scene.tmp.*")) == []

    def test_large_scene_uses_dense_diffable_storage(self, scene, tmp_path):
        for index in range(512):
            scene.create_game_object(f"Dense_{index}")

        scene_path = tmp_path / "dense.scene"
        assert scene.save_to_file(str(scene_path)) is True

        content = scene_path.read_text(encoding="utf-8")
        document = json.loads(content)
        pretty = json.dumps(document, indent=2)

        assert len(document["objects"]) == 512
        assert content.count("\n") >= 512
        assert len(content) < len(pretty) * 0.7
        assert all(
            line.startswith("    {")
            for line in content.splitlines()
            if '"Dense_' in line
        )

    def test_scene_file_manager_serializes_save_as_name_before_writing(
        self,
        scene,
        tmp_path,
        monkeypatch,
    ):
        from Infernux.engine.project_context import get_project_root, set_project_root

        previous_root = get_project_root()
        previous_manager = SceneFileManager._instance
        project_root = tmp_path / "Project"
        scene_path = project_root / "Assets" / "RaceTrack.scene"
        scene_path.parent.mkdir(parents=True)

        try:
            set_project_root(str(project_root))
            manager = SceneFileManager()
            monkeypatch.setattr(manager, "_save_camera_state", lambda _path: None)
            imported_paths = []

            class _AssetDatabase:
                @staticmethod
                def contains_path(_path):
                    return False

            from Infernux.core.assets import AssetManager

            manager._asset_database = _AssetDatabase()
            monkeypatch.setattr(
                AssetManager,
                "import_asset",
                classmethod(lambda _cls, path, **_kwargs: imported_paths.append(path) or True),
            )

            assert manager._do_save_inner(str(scene_path)) is True

            document = json.loads(scene_path.read_text(encoding="utf-8"))
            assert document["name"] == "RaceTrack"
            assert scene.name == "RaceTrack"
            assert imported_paths == [str(scene_path.resolve())]
        finally:
            SceneFileManager._instance = previous_manager
            set_project_root(previous_root)

    def test_runtime_scene_publish_rebuilds_scaled_collider_from_current_world_transform(self, scene):
        from Infernux.lib import Physics

        platform = scene.create_game_object("RuntimeScaledPlatform")
        platform.transform.position = Vector3(0.0, 0.4, 16.0)
        platform.transform.local_scale = Vector3(5.0, 0.8, 18.0)
        platform.add_component("BoxCollider")
        document = scene.serialize_document()

        sm = SceneManager.instance()
        sm.play()
        transaction = SceneDocumentTransaction(scene, document=document)
        assert transaction.run_to_completion() is True

        sm._start_active_scene_for_play()

        edge_hits = Physics.overlap_box(
            Vector3(0.0, 0.4, 24.0),
            Vector3(0.1, 0.1, 0.1),
        )
        assert any(collider.game_object.name == "RuntimeScaledPlatform" for collider in edge_hits)

    def test_scene_deserialize_advances_game_object_id_allocator(self, scene):
        scene.create_game_object("PersistedHighId")
        document = scene.serialize_document()
        persisted_id = 9_000_000
        document["objects"][0]["id"] = persisted_id

        assert scene._commit_document(document) is True

        created_after_load = scene.create_game_object("CreatedAfterLoad")
        assert scene.find_by_id(persisted_id).name == "PersistedHighId"
        assert created_after_load.id > persisted_id
        assert scene.find_by_id(created_after_load.id) is created_after_load

    def test_empty_scene_document_uses_current_format(self, scene):
        from Infernux.engine.scene_manager import _empty_scene_document

        document = _empty_scene_document("BlankEditorScene")
        assert set(document) == {"name", "isPlaying", "objects"}
        assert scene._commit_document(document) is True
        assert scene.name == "BlankEditorScene"
        assert scene.get_root_objects() == []

    def test_retained_world_rejects_unknown_field_without_destroying_live_graph(self, scene):
        existing = scene.create_game_object("KeepAliveOnBadSchema")
        transform = existing.transform
        document = {"unknown": 1, "name": "Bad", "isPlaying": False, "objects": []}

        assert scene._commit_document_retaining_world(document) is None
        assert scene.find("KeepAliveOnBadSchema") is existing
        assert existing.transform is transform

    def test_retained_world_restores_exact_instances_when_native_commit_fails(self, scene):
        existing = scene.create_game_object("RetainedNativeFailure")
        rigidbody = existing.add_component("Rigidbody")
        collider = existing.add_component("BoxCollider")
        transform = existing.transform
        document = scene.serialize_document()
        document["unknown"] = 1

        assert scene._commit_document_retaining_world(document) is None
        assert scene.find("RetainedNativeFailure") is existing
        assert existing.transform is transform
        assert existing.get_component("Rigidbody") is rigidbody
        assert existing.get_component("BoxCollider") is collider

    def test_deserialize_rejects_invalid_component_without_partial_scene(self, scene):
        existing = scene.create_game_object("ExistingSceneState")
        existing.transform.position = Vector3(17, 3, -2)
        valid_object = json.loads(scene.create_game_object("Valid").serialize())
        invalid_object = json.loads(scene.create_game_object("Invalid").serialize())
        original_document = scene.serialize_document()
        document = dict(original_document)
        invalid_object["components"].append(
            {
                "component_id": 999999,
                "type_id": "native:infernux.MissingNativeComponent",
                "enabled": True,
                "execution_order": 0,
                "data": {},
            }
        )
        document["objects"] = [valid_object, invalid_object]

        assert scene._commit_document(document) is False
        assert scene.serialize_document() == original_document
        restored_existing = scene.find("ExistingSceneState")
        assert restored_existing is existing
        assert restored_existing.transform.position.x == pytest.approx(17)

    @pytest.mark.parametrize(
        "corruption",
        [
            "duplicate_object_id",
            "duplicate_component_id",
            "invalid_component_field",
            "invalid_main_camera",
            "unknown_scene_field",
            "unknown_object_field",
            "invalid_layer",
            "invalid_python_descriptor",
            "empty_python_script_guid",
        ],
    )
    def test_transactional_deserialize_preserves_live_graph(self, scene, corruption):
        first = scene.create_game_object("TransactionFirst")
        second = scene.create_game_object("TransactionSecond")
        first_rb = first.add_component("Rigidbody")
        first.add_py_component(_StrictSceneComponent())
        second.add_component("Rigidbody")
        first_rb.mass = 6.5

        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        first_doc, second_doc = candidate["objects"]

        if corruption == "duplicate_object_id":
            second_doc["id"] = first_doc["id"]
        elif corruption == "duplicate_component_id":
            second_doc["transform"]["component_id"] = first_doc["transform"]["component_id"]
        elif corruption == "invalid_component_field":
            _native_record(first_doc, "Rigidbody")["data"]["mass"] = "heavy"
        elif corruption == "invalid_main_camera":
            candidate["mainCameraComponentId"] = first_doc["components"][0]["component_id"]
        elif corruption == "unknown_scene_field":
            candidate["legacy"] = True
        elif corruption == "unknown_object_field":
            first_doc["legacy"] = True
        elif corruption == "invalid_layer":
            first_doc["layer"] = 32
        elif corruption == "invalid_python_descriptor":
            _python_records(first_doc)[0]["legacy"] = True
        elif corruption == "empty_python_script_guid":
            descriptor = _python_records(first_doc)[0]
            descriptor["type_id"] = descriptor["type_id"].replace("python:", "python::", 1)

        assert scene._commit_document(candidate) is False
        assert scene.serialize_document() == original_document
        assert scene.find("TransactionFirst") is first
        assert scene.find("TransactionSecond") is second
        assert first.get_component("Rigidbody") is first_rb
        assert first_rb.mass == pytest.approx(6.5)

    def test_python_field_preflight_runs_before_native_scene_commit(self, scene):
        existing = scene.create_game_object("PythonPreflightExisting")
        existing.add_py_component(_StrictSceneComponent())
        alive_before = _cds_alive_count(_StrictSceneComponent)
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        _python_records(candidate["objects"][0])[0]["data"]["value"] = "not-an-int"

        with pytest.raises(PythonComponentRestoreError, match="INT field requires an integer"):
            deserialize_scene_document_transactionally(scene, candidate)

        assert scene.serialize_document() == original_document
        assert scene.find("PythonPreflightExisting") is existing
        assert _cds_alive_count(_StrictSceneComponent) == alive_before

    def test_python_publish_callback_failure_rolls_back_committed_native_scene(self, scene):
        existing = scene.create_game_object("RollbackSource")
        original_component = _StrictSceneComponent()
        original_component.value = 29
        existing.add_py_component(original_component)
        original_document = scene.serialize_document()
        original_component_id = original_component.component_id
        strict_alive_before = _cds_alive_count(_StrictSceneComponent)

        candidate = json.loads(json.dumps(original_document))
        candidate["objects"][0]["name"] = "RollbackCandidate"
        descriptor = _python_records(candidate["objects"][0])[0]
        exploding = _ExplodingAfterDeserializeComponent()
        exploding._component_id = original_component_id
        fields = exploding._serialize_fields_document()
        descriptor["type_id"] = _python_type_id(exploding)
        fields.pop("__type_name__")
        fields.pop("__component_id__")
        descriptor["data"] = fields
        exploding._call_on_destroy()
        assert _cds_alive_count(_ExplodingAfterDeserializeComponent) == 0

        transaction = SceneDocumentTransaction(scene, document=candidate)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.state is SceneDocumentTransactionState.FAILED
        assert transaction.rolled_back is True
        assert transaction.rollback_error == ""
        assert "intentional on_after_deserialize failure" in transaction.error
        assert scene.serialize_document() == original_document
        restored_object = scene.find("RollbackSource")
        restored_component = restored_object.get_py_component(_StrictSceneComponent)
        assert restored_object is existing
        assert restored_component is original_component
        assert restored_component.component_id == original_component_id
        assert restored_component.value == 29
        assert scene.find("RollbackCandidate") is None
        assert scene.has_pending_py_components() is False
        assert _cds_alive_count(_StrictSceneComponent) == strict_alive_before
        assert _cds_alive_count(_ExplodingAfterDeserializeComponent) == 0

    def test_after_publish_hook_failure_rolls_back_complete_candidate_graph(self, scene):
        existing = scene.create_game_object("AfterPublishSource")
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        candidate["objects"][0]["name"] = "AfterPublishCandidate"

        def fail_after_publish():
            raise RuntimeError("intentional after_publish failure")

        transaction = SceneDocumentTransaction(
            scene,
            document=candidate,
            after_publish=fail_after_publish,
        )

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert transaction.rolled_back is True
        assert "intentional after_publish failure" in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("AfterPublishSource") is existing
        assert scene.find("AfterPublishCandidate") is None

    def test_python_publish_uses_attach_callback_activate_barrier(self, scene):
        first = scene.create_game_object("PublishBarrierFirst")
        second = scene.create_game_object("PublishBarrierSecond")
        first_component = _PublishBarrierComponent()
        first_component.value = 1
        second_component = _PublishBarrierComponent()
        second_component.value = 2
        first.add_py_component(first_component)
        second.add_py_component(second_component)
        document = scene.serialize_document()
        _PublishBarrierComponent._events.clear()

        transaction = SceneDocumentTransaction(scene, document=document)

        assert transaction.run_to_completion(raise_on_failure=False) is True
        assert _PublishBarrierComponent._events == [
            ("after", 1),
            ("after", 2),
            ("awake", 1),
            ("awake", 2),
        ]

    def test_python_preflight_rejects_missing_required_component_without_auto_add(self, scene):
        owner = scene.create_game_object("StrictRequiredComponent")
        owner.add_component("Rigidbody")
        owner.add_py_component(_RequiresRigidbodyComponent())
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        candidate["objects"][0]["components"] = [
            component
            for component in candidate["objects"][0]["components"]
            if component["type_id"] != "native:infernux.Rigidbody"
        ]

        transaction = SceneDocumentTransaction(scene, document=candidate)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert "requires missing component 'Rigidbody'" in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("StrictRequiredComponent") is owner

    def test_play_snapshot_preserves_native_required_component_type_names(self, scene):
        owner = scene.create_game_object("PlaySnapshotRequiredComponent")
        owner.add_component("Rigidbody")
        owner.add_py_component(_RequiresRigidbodyComponent())

        prepared = preflight_scene_python_components(scene._capture_play_mode_snapshot())

        assert len(prepared.components) == 1
        assert prepared.components[0].type_name == "_RequiresRigidbodyComponent"
        prepared.discard()

    def test_python_preflight_rejects_duplicate_disallow_multiple_component(self, scene):
        owner = scene.create_game_object("StrictDisallowMultiple")
        owner.add_py_component(_SingleInstanceSceneComponent())
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        duplicate = json.loads(json.dumps(_python_records(candidate["objects"][0])[0]))
        duplicate_id = duplicate["component_id"] + 100000
        duplicate["component_id"] = duplicate_id
        candidate["objects"][0]["components"].append(duplicate)

        transaction = SceneDocumentTransaction(scene, document=candidate)

        assert transaction.run_to_completion(raise_on_failure=False) is False
        assert "disallows multiple instances" in transaction.error
        assert scene.serialize_document() == original_document
        assert scene.find("StrictDisallowMultiple") is owner

    def test_preflighted_python_graph_is_published_after_native_commit(self, scene):
        existing = scene.create_game_object("PythonPreflightSuccess")
        original_component = _StrictSceneComponent()
        original_component.value = 19
        existing.add_py_component(original_component)
        original_component_id = original_component.component_id
        candidate = json.loads(json.dumps(scene.serialize_document()))

        assert deserialize_scene_document_transactionally(scene, candidate) is True

        restored_object = scene.find("PythonPreflightSuccess")
        restored_component = restored_object.get_py_component(_StrictSceneComponent)
        assert restored_object is not existing
        assert restored_component is not original_component
        assert restored_component.component_id == original_component_id
        assert restored_component.value == 19
        assert scene.has_pending_py_components() is False

    def test_scene_restore_uses_default_for_new_python_field(self, scene):
        root = scene.create_game_object("AdditivePythonField")
        component = _AdditiveSceneComponent()
        component.value = 19
        component.label = "saved"
        root.add_py_component(component)
        document = json.loads(json.dumps(scene.serialize_document()))
        _python_records(document["objects"][0])[0]["data"].pop("label")

        assert deserialize_scene_document_transactionally(scene, document) is True
        restored = scene.find("AdditivePythonField").get_py_component(
            _AdditiveSceneComponent
        )
        assert restored.value == 19
        assert restored.label == "default"

    def test_scene_restore_rejects_removed_python_field(self, scene):
        root = scene.create_game_object("RemovedPythonField")
        root.add_py_component(_StrictSceneComponent())
        document = json.loads(json.dumps(scene.serialize_document()))
        _python_records(document["objects"][0])[0]["data"]["removed_field"] = 1

        with pytest.raises(PythonComponentRestoreError, match="serialized fields mismatch"):
            deserialize_scene_document_transactionally(scene, document)

    def test_python_component_document_uses_stable_script_and_type_guids(self, scene):
        root = scene.create_game_object("StablePythonIdentity")
        component = _StrictSceneComponent()
        root.add_py_component(component)

        descriptor = _python_records(root.serialize_document())[0]
        script_guid, type_guid, module_name, qualified_name = descriptor["type_id"][len("python:"):].split(":")

        assert script_guid == component._script_guid
        assert type_guid == component.__class__._get_type_guid()
        assert module_name == component.__class__.__module__
        assert qualified_name == component.__class__.__qualname__
        assert len(script_guid) == 32
        assert len(type_guid) == 32

    def test_python_component_replacement_preserves_native_identity_without_destroy(self, scene):
        root = scene.create_game_object("IdentityPreservingReplacement")
        source = root.add_py_component(_ReplacementLifecycleSource())
        source.execution_order = 42
        source.enabled = False
        original_id = source.component_id
        original_handle = source._cpp_component.handle
        _ReplacementLifecycleSource.destroy_calls = 0

        target = _ReplacementLifecycleTarget()
        target.value = source.value
        published = root.replace_py_component(source, target)

        assert published is target
        assert root.get_py_components() == [target]
        assert source._cpp_component is None
        assert source._is_destroyed is True
        assert _ReplacementLifecycleSource.destroy_calls == 0
        assert target.component_id == original_id
        assert target._cpp_component.handle == original_handle
        assert target.execution_order == 42
        assert target.enabled is False
        assert target.value == 13

    def test_unified_component_records_preserve_order_and_python_execution_order(self, scene):
        root = scene.create_game_object("UnifiedComponentOrder")
        root.add_component("BoxCollider")
        script = root.add_py_component(_StrictSceneComponent())
        script.execution_order = 37
        root.add_component("Light")

        document = scene.serialize_document()
        records = document["objects"][0]["components"]
        original_type_ids = [record["type_id"] for record in records]
        assert original_type_ids[0] == "native:infernux.BoxCollider"
        assert original_type_ids[1].startswith("python:")
        assert original_type_ids[2] == "native:infernux.Light"
        assert records[1]["execution_order"] == 37

        assert deserialize_scene_document_transactionally(scene, document) is True

        restored = scene.find("UnifiedComponentOrder")
        restored_records = restored.serialize_document()["components"]
        assert [record["type_id"] for record in restored_records] == original_type_ids
        assert restored.get_py_component(_StrictSceneComponent).execution_order == 37

    def test_python_preflight_replaces_unresolvable_script_with_missing_placeholder(self, scene):
        root = scene.create_game_object("MismatchedPythonIdentity")
        component = _StrictSceneComponent()
        root.add_py_component(component)
        original_document = root.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        descriptor = _python_records(candidate)[0]
        parts = descriptor["type_id"].split(":")
        parts[2] = "f" * 32
        descriptor["type_id"] = ":".join(parts)

        assert deserialize_game_object_document_transactionally(root, candidate) is True
        restored = root.get_py_components()[0]
        assert getattr(restored, "_is_broken", False) is True
        assert restored._script_guid == parts[1]
        assert "Missing script" in (getattr(restored, "_broken_error", "") or "")

    def test_native_pending_python_record_uses_structured_fields_document(self, scene):
        root = scene.create_game_object("StructuredPendingRecord")
        component = _StrictSceneComponent()
        component.value = 23
        root.add_py_component(component)
        document = json.loads(json.dumps(scene.serialize_document()))

        assert scene._commit_document(document) is True

        pending = scene.get_pending_py_components()
        assert len(pending) == 1
        descriptor = _python_records(document["objects"][0])[0]
        expected_fields = {
            "__type_name__": "_StrictSceneComponent",
            "__component_id__": descriptor["component_id"],
            **descriptor["data"],
        }
        assert pending[0].fields_document == expected_fields
        assert pending[0].type_guid == descriptor["type_id"].split(":")[2]
        assert not hasattr(pending[0], "fields_json")
        scene.take_pending_py_components()

    def test_scene_restore_rejects_python_component_id_owned_by_another_scene(self, scene):
        source = scene.create_game_object("LiveSourceSceneObject")
        source_component = _StrictSceneComponent()
        source.add_py_component(source_component)
        document = json.loads(json.dumps(scene.serialize_document()))
        manager = SceneManager.instance()
        target = manager.create_scene("PythonIdCollisionTarget")
        target.create_game_object("TargetState")
        original_target_document = target.serialize_document()

        assert deserialize_scene_document_transactionally(target, document) is False

        assert target.serialize_document() == original_target_document
        assert source.get_py_component(_StrictSceneComponent) is source_component

    def test_scene_preflight_rejects_duplicate_python_component_id(self, scene):
        first = scene.create_game_object("FirstPythonId")
        second = scene.create_game_object("SecondPythonId")
        first_component = _StrictSceneComponent()
        second_component = _StrictSceneComponent()
        first.add_py_component(first_component)
        second.add_py_component(second_component)
        original_document = scene.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        first_descriptor = _python_records(candidate["objects"][0])[0]
        second_descriptor = _python_records(candidate["objects"][1])[0]
        second_descriptor["component_id"] = first_descriptor["component_id"]

        with pytest.raises(PythonComponentRestoreError, match="duplicate component_id"):
            deserialize_scene_document_transactionally(scene, candidate)

        assert scene.serialize_document() == original_document

    def test_game_object_python_preflight_preserves_live_subtree(self, scene):
        root = scene.create_game_object("ObjectPreflightExisting")
        component = _StrictSceneComponent()
        root.add_py_component(component)
        original_document = root.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        _python_records(candidate)[0]["data"]["value"] = "not-an-int"

        with pytest.raises(PythonComponentRestoreError, match="INT field requires an integer"):
            deserialize_game_object_document_transactionally(root, candidate)

        assert scene.find("ObjectPreflightExisting") is root
        assert root.serialize_document() == original_document
        assert root.get_py_component(_StrictSceneComponent) is component

    def test_prefab_conversion_rejects_reference_outside_subtree(self, scene):
        external = scene.create_game_object("ExternalReferenceTarget")
        source = scene.create_game_object("IdlessReferenceSource")
        component = _ObjectRefSceneComponent()
        component.target = GameObjectRef(persistent_id=external.id)
        source.add_py_component(component)
        document = json.loads(json.dumps(source.serialize_document()))
        before_ids = {obj.id for obj in scene.get_all_objects()}

        with pytest.raises(PrefabDocumentError, match="outside its subtree"):
            _strip_prefab_runtime_fields(document)

        assert {obj.id for obj in scene.get_all_objects()} == before_ids

    def test_idless_object_graph_publishes_prepared_component(self, scene):
        source = scene.create_game_object("IdlessPreparedSource")
        component = _StrictSceneComponent()
        component.value = 31
        source.add_py_component(component)
        document = json.loads(json.dumps(source.serialize_document()))
        _strip_prefab_runtime_fields(document)

        created = instantiate_game_object_document_transactionally(scene, document)

        restored = created.get_py_component(_StrictSceneComponent)
        assert created is not source
        assert restored.component_id != component.component_id
        assert restored.value == 31
        assert scene.has_pending_py_components() is False

    def test_game_object_restore_preserves_python_component_id(self, scene):
        root = scene.create_game_object("StablePythonComponent")
        original = _StrictSceneComponent()
        root.add_py_component(original)
        original_id = original.component_id
        candidate = json.loads(json.dumps(root.serialize_document()))

        assert deserialize_game_object_document_transactionally(root, candidate) is True

        restored = root.get_py_component(_StrictSceneComponent)
        assert restored is not original
        assert restored.component_id == original_id

    def test_game_object_restore_rejects_mismatched_python_component_id(self, scene):
        root = scene.create_game_object("MismatchedPythonComponent")
        original = _StrictSceneComponent()
        root.add_py_component(original)
        original_document = root.serialize_document()
        candidate = json.loads(json.dumps(original_document))
        _python_records(candidate)[0]["data"]["__component_id__"] = 999999

        with pytest.raises(PythonComponentRestoreError, match="exact ComponentRecord|reserved"):
            deserialize_game_object_document_transactionally(root, candidate)

        assert root.serialize_document() == original_document
        assert root.get_py_component(_StrictSceneComponent) is original

    def test_idless_object_graph_remaps_internal_python_references(self, scene):
        source = scene.create_game_object("LocalReferenceRoot")
        child = scene.create_game_object("LocalReferenceChild")
        child.set_parent(source)
        child.add_py_component(_StrictSceneComponent())
        refs = _ObjectGraphRefsComponent()
        refs.target_object = GameObjectRef(child)
        refs.target_component = ComponentRef(
            go_id=child.id,
            component_type="_StrictSceneComponent",
        )
        source.add_py_component(refs)
        document = json.loads(json.dumps(source.serialize_document()))
        _strip_prefab_runtime_fields(document)

        created = instantiate_game_object_document_transactionally(scene, document)

        created_child = created.get_child(0)
        restored = created.get_py_component(_ObjectGraphRefsComponent)
        assert restored.target_object is created_child
        assert restored.target_component is created_child.get_py_component(_StrictSceneComponent)

    @pytest.mark.parametrize(
        "corruption",
        ["unknown_component", "invalid_component_field", "missing_name", "unknown_field", "invalid_layer"],
    )
    def test_game_object_preflight_preserves_live_subtree(self, scene, corruption):
        root = scene.create_game_object("LiveObject")
        child = scene.create_game_object("DetachedChild")
        child.set_parent(root)
        rigidbody = root.add_component("Rigidbody")
        rigidbody.mass = 4.25
        original_document = root.serialize_document()
        candidate = json.loads(json.dumps(original_document))

        if corruption == "unknown_component":
            candidate["components"].append(
                {
                    "component_id": 999998,
                    "type_id": "native:infernux.RemovedNativeComponent",
                    "enabled": True,
                    "execution_order": 0,
                    "data": {},
                }
            )
        elif corruption == "invalid_component_field":
            candidate["components"][0]["data"]["mass"] = "not-a-number"
        elif corruption == "missing_name":
            candidate.pop("name")
        elif corruption == "unknown_field":
            candidate["legacy"] = True
        else:
            candidate["layer"] = -1

        assert root._commit_document(candidate) is False
        assert root.serialize_document() == original_document
        assert scene.find("LiveObject") is root
        assert root.get_children()[0] is child
        assert root.get_component("Rigidbody") is rigidbody
        assert rigidbody.mass == pytest.approx(4.25)

    @pytest.mark.parametrize("collision", ["game_object_id", "component_id", "python_component_id"])
    def test_game_object_commit_rejects_ids_owned_outside_subtree(self, scene, collision):
        root = scene.create_game_object("CollisionCheckedRoot")
        rigidbody = root.add_component("Rigidbody")
        outside = scene.create_game_object("OutsideOwner")
        outside_collider = outside.add_component("BoxCollider")
        root_python = _StrictSceneComponent()
        outside_python = _StrictSceneComponent()
        root.add_py_component(root_python)
        outside.add_py_component(outside_python)
        original_document = root.serialize_document()
        candidate = json.loads(json.dumps(original_document))

        if collision == "game_object_id":
            candidate["id"] = outside.id
        elif collision == "component_id":
            candidate["components"][0]["component_id"] = outside_collider.component_id
        else:
            _python_records(candidate)[0]["component_id"] = outside_python.component_id

        if collision == "python_component_id":
            assert deserialize_game_object_document_transactionally(root, candidate) is False
        else:
            assert root._commit_document(candidate) is False
        assert root.serialize_document() == original_document
        assert scene.find("CollisionCheckedRoot") is root
        assert root.get_component("Rigidbody") is rigidbody
        assert scene.find("OutsideOwner") is outside
        assert outside.get_component("BoxCollider") is outside_collider

    def test_game_object_commit_adopts_staged_native_graph_once(self, scene):
        root = scene.create_game_object("StagedRoot")
        old_child = scene.create_game_object("OldChild")
        old_child.set_parent(root)
        old_rigidbody = root.add_component("Rigidbody")
        old_child_id = old_child.id
        old_rigidbody_id = old_rigidbody.component_id
        candidate = json.loads(json.dumps(root.serialize_document()))
        candidate["name"] = "CommittedRoot"
        candidate["components"][0]["data"]["mass"] = 7.5
        candidate["children"][0]["name"] = "CommittedChild"
        structure_version = scene.structure_version

        assert deserialize_game_object_document_transactionally(root, candidate) is True

        assert scene.find("CommittedRoot") is root
        assert scene.find_by_id(old_child_id) is root.get_child(0)
        assert root.get_child(0).name == "CommittedChild"
        assert root.serialize_document()["components"][0]["data"]["mass"] == pytest.approx(7.5)
        restored_rigidbody = root.get_component("Rigidbody")
        assert restored_rigidbody is not old_rigidbody
        assert old_rigidbody.is_valid is False
        assert restored_rigidbody.component_id == old_rigidbody_id
        assert restored_rigidbody.mass == pytest.approx(7.5)
        assert scene.structure_version == structure_version + 1

    def test_game_object_commit_clears_removed_main_camera(self, scene):
        root = scene.create_game_object("CameraOwner")
        camera = root.add_component("Camera")
        scene.main_camera = camera
        candidate = json.loads(json.dumps(root.serialize_document()))
        candidate["components"] = []

        assert root._commit_document(candidate) is True
        assert scene.main_camera is None
