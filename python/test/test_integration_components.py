"""Integration tests — Components, rendering objects, assets (real engine)."""
from __future__ import annotations

import importlib
import gc
import json
import sys
import time
from pathlib import Path

import pytest

from Infernux.components import InxComponent, serialized_field, FieldType
from Infernux.components.ref_wrappers import ComponentRef
from Infernux.components.value_document import make_game_object_ref
from Infernux.components.builtin import BoxCollider as BoxColliderComponent
from Infernux.components.builtin import Camera as CameraComponent
from Infernux.components.builtin import PhysicsMaterialCombine
from Infernux.core.assets import AssetManager
from Infernux.core.material import Material
from Infernux.renderstack.render_stack import RenderStack
from Infernux.renderstack.render_stack_pipeline import RenderStackPipeline
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.core.asset_ref import RenderEffectRef

from Infernux.lib import (
    SceneManager,
    Vector3,
    PrimitiveType,
    TextureLoader,
    InxMaterial,
    InxPhysicMaterial,
    LightType,
    LightShadows,
    Physics,
    AssetRegistry,
    ResourceType,
)


def test_mesh_cpu_payload_prepares_on_worker_and_rejects_stale_publish(engine):
    registry = AssetRegistry.instance()
    asset_database = registry.get_asset_database()
    source = Path(asset_database.assets_root) / "async-cpu-artifact.obj"
    source.write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        encoding="ascii",
    )
    guid = asset_database.import_asset(str(source)).guid
    assert guid
    artifact = Path(asset_database.get_runtime_artifact_path(guid, ResourceType.Mesh))
    skin_artifact = (
        Path(asset_database.assets_root).parent
        / "Library"
        / "Artifacts"
        / "SkinnedMesh"
        / f"{guid}.inxskin"
    )
    assert artifact.is_file()
    assert artifact.read_bytes().startswith(b"INXMESH")
    assert skin_artifact.read_bytes().startswith(b"INXSKIN")
    asset_database.flush_derived_index()
    index_document = json.loads(Path(asset_database.asset_index_path).read_text(encoding="utf-8"))
    indexed = next(item for item in index_document["entries"] if item["guid"] == guid)
    assert indexed["artifact_path"] == f"Library/Artifacts/Mesh/{guid}.inxmesh"

    try:
        source.write_text("this source is intentionally invalid\n", encoding="ascii")
        assert registry.get_asset_version(guid) == 0
        ticket = registry.begin_load_mesh_by_guid(guid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not registry.try_commit_asset_load(ticket):
            time.sleep(0.001)
        assert ticket.committed is True
        assert ticket.produced_on_worker is True
        mesh = registry.get_mesh(guid)
        assert mesh is not None
        assert mesh.vertex_count == 3
        assert mesh.has_skinned_data is False
        assert registry.get_asset_version(guid) == 1
        runtime_record = next(record for record in engine.asset_runtime_records if record.guid == guid)
        assert runtime_record.runtime_version == 1
        assert runtime_record.cpu_resident is True
        assert runtime_record.cpu_bytes > 0
        assert runtime_record.gpu_resident_bytes == 0
        assert runtime_record.gpu_version_synchronized is True

        registry.invalidate_asset(guid)
        source.write_text(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
            encoding="ascii",
        )
        artifact.write_bytes(b"corrupt derived mesh")
        fallback = registry.begin_load_mesh_by_guid(guid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not registry.try_commit_asset_load(fallback):
            time.sleep(0.001)
        assert fallback.committed is True
        assert registry.get_mesh(guid).vertex_count == 3
        assert registry.get_asset_version(guid) == 2

        registry.invalidate_asset(guid)
        assert asset_database.reimport_asset(str(source))
        asset_database.flush_derived_index()
        artifact.unlink()
        asset_database.refresh()
        assert artifact.is_file()
        assert source.resolve() in {Path(path).resolve() for path in asset_database.last_refresh_imported_paths}

        stale = registry.begin_load_mesh_by_guid(guid)
        registry.invalidate_asset(guid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not stale.complete:
            time.sleep(0.001)
        assert stale.complete is True
        assert stale.produced_on_worker is True
        with pytest.raises(RuntimeError, match="stale"):
            registry.try_commit_asset_load(stale)
        assert registry.is_loaded(guid) is False
        assert registry.get_asset_version(guid) == 2
        runtime_record = next(record for record in engine.asset_runtime_records if record.guid == guid)
        assert runtime_record.runtime_version == 2
        assert runtime_record.cpu_resident is False
        assert runtime_record.cpu_bytes == 0
    finally:
        registry.invalidate_asset(guid)
        if asset_database.contains_path(str(source)):
            asset_database.delete_asset(str(source))
        assert artifact.exists() is False
        assert skin_artifact.exists() is False
        source.unlink(missing_ok=True)
        Path(f"{source}.meta").unlink(missing_ok=True)


def test_skinned_mesh_companion_artifact_is_atomic_worker_loaded_and_rebuilt(engine, scene):
    registry = AssetRegistry.instance()
    asset_database = registry.get_asset_database()
    fixture = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "assimp"
        / "test"
        / "models"
        / "FBX"
        / "animation_with_skeleton.fbx"
    )
    original_bytes = fixture.read_bytes()
    source = Path(asset_database.assets_root) / "skinned-artifact-probe.fbx"
    source.write_bytes(original_bytes)
    guid = asset_database.import_asset(str(source)).guid
    assert guid
    mesh_artifact = Path(asset_database.get_runtime_artifact_path(guid, ResourceType.Mesh))
    skin_artifact = (
        Path(asset_database.assets_root).parent
        / "Library"
        / "Artifacts"
        / "SkinnedMesh"
        / f"{guid}.inxskin"
    )
    assert mesh_artifact.read_bytes().startswith(b"INXMESH")
    assert skin_artifact.read_bytes().startswith(b"INXSKIN")
    metadata = asset_database.get_meta_by_guid(guid)
    assert metadata.get_int("bone_count") > 0
    assert metadata.get_int("animation_count") > 0

    def load_on_worker():
        ticket = registry.begin_load_mesh_by_guid(guid)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not registry.try_commit_asset_load(ticket):
            time.sleep(0.001)
        assert ticket.committed is True
        assert ticket.produced_on_worker is True
        loaded = registry.get_mesh(guid)
        assert loaded is not None
        assert loaded.has_skinned_data is True
        assert loaded.skinned_bone_count > 0
        assert loaded.skinned_animation_count > 0
        assert loaded.skinned_animation_names
        return loaded

    try:
        source.write_bytes(b"invalid source: artifact load must not parse this")
        mesh = load_on_worker()
        assert mesh.vertex_count > 0

        registry.invalidate_asset(guid)
        source.write_bytes(original_bytes)
        skin_artifact.write_bytes(b"corrupt skinned companion")
        fallback = load_on_worker()
        assert fallback.has_skinned_data is True

        registry.invalidate_asset(guid)
        assert asset_database.reimport_asset(str(source))
        assert skin_artifact.is_file()
        skin_artifact.unlink()
        asset_database.refresh()
        assert skin_artifact.is_file()
        assert source.resolve() in {Path(path).resolve() for path in asset_database.last_refresh_imported_paths}

        renderer = scene.create_game_object("SkinnedArtifactProbe").add_component("SkinnedMeshRenderer")
        renderer.set_source_model_guid(guid)
        assert renderer.source_model_guid == guid
        assert not hasattr(renderer, "source_model_path")
        assert renderer.animation_take_count > 0
        assert renderer.get_animation_take_names()
        document = renderer.serialize_document()
        assert document["meshAssetGuid"] == guid
        assert "sourceModelGuid" not in document
        assert "sourceModelPath" not in document
        assert "animationTakeNames" not in document
    finally:
        registry.invalidate_asset(guid)
        if asset_database.contains_path(str(source)):
            asset_database.delete_asset(str(source))
        assert mesh_artifact.exists() is False
        assert skin_artifact.exists() is False
        source.unlink(missing_ok=True)
        Path(f"{source}.meta").unlink(missing_ok=True)


def test_texture_cpu_artifact_prepares_on_worker_and_validates_cache(engine):
    registry = AssetRegistry.instance()
    asset_database = registry.get_asset_database()
    source = Path(asset_database.assets_root) / "async-texture-artifact.ppm"
    source_bytes = b"P6\n4 2\n255\n" + bytes(
        (
            255, 0, 0,
            0, 255, 0,
            0, 0, 255,
            255, 255, 255,
            255, 255, 0,
            0, 255, 255,
            255, 0, 255,
            32, 64, 128,
        )
    )
    source.write_bytes(source_bytes)
    guid = asset_database.import_asset(str(source)).guid
    assert guid
    artifact = Path(asset_database.get_runtime_artifact_path(guid, ResourceType.Texture))
    assert artifact.is_file()
    assert artifact.read_bytes().startswith(b"INXTEX")

    try:
        source.write_bytes(b"invalid texture source")
        ticket = registry.begin_load_texture_by_guid(guid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not registry.try_commit_asset_load(ticket):
            time.sleep(0.001)
        assert ticket.committed is True
        assert ticket.produced_on_worker is True
        texture = registry.get_texture_asset(guid)
        assert texture is not None
        assert texture.pixel_width == 4
        assert texture.pixel_height == 2
        assert texture.mip_count == 3
        assert texture.cpu_byte_size == 24
        assert texture.pixel_storage == "block_compressed"
        assert texture.pixel_format == "bc1_rgba_srgb"

        registry.invalidate_asset(guid)
        source.write_bytes(source_bytes)
        artifact.write_bytes(b"corrupt texture artifact")
        fallback = registry.begin_load_texture_by_guid(guid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not registry.try_commit_asset_load(fallback):
            time.sleep(0.001)
        assert fallback.committed is True
        assert registry.get_texture_asset(guid).cpu_byte_size == 24
    finally:
        registry.invalidate_asset(guid)
        if asset_database.contains_path(str(source)):
            asset_database.delete_asset(str(source))
        assert artifact.exists() is False
        source.unlink(missing_ok=True)
        Path(f"{source}.meta").unlink(missing_ok=True)


def test_asset_registry_cpu_residency_budget_respects_live_and_explicit_pins(engine):
    registry = AssetRegistry.instance()
    asset_database = registry.get_asset_database()
    sources = [Path(asset_database.assets_root) / f"residency-{index}.ppm" for index in range(2)]
    payload = b"P6\n2 1\n255\n" + bytes((255, 0, 0, 0, 255, 0))
    for index, source in enumerate(sources):
        source.write_bytes(payload[:-1] + bytes((index,)))

    guids = [asset_database.import_asset(str(source)).guid for source in sources]
    assert all(guids)
    original_budget = registry.cpu_budget_bytes
    baseline_cpu_bytes = registry.total_cpu_bytes
    first = registry.load_texture_by_guid(guids[0])
    second = registry.load_texture_by_guid(guids[1])
    assert first is not None and second is not None
    first_record = registry.get_asset_residency(guids[0])
    second_record = registry.get_asset_residency(guids[1])
    assert first_record.cpu_bytes > first.cpu_byte_size
    assert second_record.cpu_bytes > second.cpu_byte_size

    larger_payload = b"P6\n4 2\n255\n" + bytes(range(24))
    sources[0].write_bytes(larger_payload)
    assert asset_database.reimport_asset(str(sources[0]))
    assert registry.reload_asset(guids[0]) is True
    updated_first_record = registry.get_asset_residency(guids[0])
    assert first.cpu_byte_size == 24
    assert first.pixel_format == "bc1_rgba_srgb"
    assert updated_first_record.runtime_version == first_record.runtime_version + 1
    assert updated_first_record.cpu_bytes > first_record.cpu_bytes
    first_record = updated_first_record
    assert registry.total_cpu_bytes == baseline_cpu_bytes + first_record.cpu_bytes + second_record.cpu_bytes

    try:
        registry.pin_asset(guids[0])
        registry.pin_asset(guids[1])
        registry.cpu_budget_bytes = 1
        assert registry.trim_cpu_budget() == 0
        assert registry.is_loaded(guids[0]) is True
        assert registry.is_loaded(guids[1]) is True

        registry.unpin_asset(guids[0])
        del first
        gc.collect()
        assert registry.trim_cpu_budget() >= 1
        assert registry.is_loaded(guids[0]) is False
        assert registry.is_loaded(guids[1]) is True
        retained = registry.get_asset_residency(guids[1])
        assert retained.explicit_pin_count == 1
        assert retained.external_reference_count >= 1
        assert retained.evictable is False
    finally:
        if registry.is_loaded(guids[1]):
            registry.unpin_asset(guids[1])
        registry.cpu_budget_bytes = original_budget
        del second
        gc.collect()
        for guid, source in zip(guids, sources):
            registry.invalidate_asset(guid)
            if asset_database.contains_path(str(source)):
                asset_database.delete_asset(str(source))
            source.unlink(missing_ok=True)
            Path(f"{source}.meta").unlink(missing_ok=True)


class _RestoreFirst(InxComponent):
    value: int = 1


class _RestoreSecond(InxComponent):
    value: int = 2


class _RestoreReference(InxComponent):
    target = serialized_field(default=None, field_type=FieldType.GAME_OBJECT)


class _RestoreComponentReference(InxComponent):
    target = serialized_field(default=None, field_type=FieldType.COMPONENT)

# ═══════════════════════════════════════════════════════════════════════════
# Component add / remove / query
# ═══════════════════════════════════════════════════════════════════════════

class TestComponentLifecycle:
    def test_pending_python_component_reference_targets_are_preflighted(self, scene):
        from Infernux.engine.component_restore import (
            PythonComponentRestoreError,
            deserialize_game_object_document_transactionally,
        )

        game_object = scene.create_game_object("InvalidReferenceRestore")
        game_object.add_py_component(_RestoreReference())
        document = game_object.serialize_document()
        document["components"][0]["data"]["target"] = make_game_object_ref(999_999_999)

        with pytest.raises(PythonComponentRestoreError, match="does not exist"):
            deserialize_game_object_document_transactionally(game_object, document)
        assert len(game_object.get_py_components()) == 1
        assert scene.has_pending_py_components() is False

    def test_component_reference_can_target_same_pending_batch(self, scene):
        from Infernux.engine.component_restore import deserialize_game_object_document_transactionally

        game_object = scene.create_game_object("PendingReferenceRestore")
        target = game_object.add_py_component(_RestoreSecond())
        holder = game_object.add_py_component(_RestoreComponentReference())
        holder.target = ComponentRef(
            go_id=game_object.id,
            component_type="_RestoreSecond",
        )
        document = game_object.serialize_document()

        assert deserialize_game_object_document_transactionally(game_object, document) is True

        restored_components = game_object.get_py_components()
        restored_holder = next(
            component for component in restored_components
            if isinstance(component, _RestoreComponentReference)
        )
        restored_target = next(
            component for component in restored_components
            if isinstance(component, _RestoreSecond)
        )
        assert restored_holder.target is restored_target
        assert restored_target is not target

    def test_pending_python_component_restore_is_atomic(self, scene):
        from Infernux.engine.component_restore import (
            PythonComponentRestoreError,
            deserialize_game_object_document_transactionally,
        )

        game_object = scene.create_game_object("AtomicPythonRestore")
        game_object.add_py_component(_RestoreFirst())
        game_object.add_py_component(_RestoreSecond())
        document = game_object.serialize_document()
        document["components"][1]["data"]["value"] = "invalid"

        with pytest.raises(PythonComponentRestoreError, match="invalid fields"):
            deserialize_game_object_document_transactionally(game_object, document)

        assert len(game_object.get_py_components()) == 2
        assert scene.has_pending_py_components() is False

    def test_missing_python_component_type_restores_as_data_preserving_placeholder(self, scene):
        from Infernux.components.missing_script import MissingScript
        from Infernux.engine.component_restore import deserialize_game_object_document_transactionally

        game_object = scene.create_game_object("MissingPythonType")
        game_object.add_py_component(_RestoreFirst())
        document = game_object.serialize_document()
        descriptor = document["components"][0]
        parts = descriptor["type_id"].split(":")
        parts[-1] = "RemovedPythonComponent"
        descriptor["type_id"] = ":".join(parts)

        assert deserialize_game_object_document_transactionally(game_object, document)
        components = game_object.get_py_components()
        assert len(components) == 1
        assert isinstance(components[0], MissingScript)
        assert components[0]._is_broken is True
        assert components[0]._component_name == "RemovedPythonComponent"
        assert components[0]._serialize_fields_document()["value"] == 1
        assert scene.has_pending_py_components() is False

    def test_add_and_get_component(self, scene):
        go = scene.create_game_object("GO")
        rb = go.add_component("Rigidbody")
        assert rb is not None
        fetched = go.get_component("Rigidbody")
        assert fetched is not None

    def test_add_and_get_python_component_by_class(self, scene):
        class ProbeComponent(InxComponent):
            pass

        go = scene.create_game_object("GO")
        probe = go.add_component(ProbeComponent)

        assert isinstance(probe, ProbeComponent)
        assert go.get_component(ProbeComponent) is probe
        assert go.get_component("ProbeComponent") is probe
        assert go.get_components(ProbeComponent) == [probe]

    def test_add_and_get_builtin_component_by_class(self, scene):
        go = scene.create_game_object("CamGO")
        cam = go.add_component(CameraComponent)

        assert isinstance(cam, CameraComponent)
        assert go.get_component(CameraComponent) is cam
        assert go.get_components(CameraComponent) == [cam]
        assert go.remove_component(cam) is True

    def test_transform_always_present(self, scene):
        go = scene.create_game_object("GO")
        t = go.get_component("Transform")
        assert t is not None
        assert t.type_name == "Transform"

    def test_get_components_lists_all(self, scene):
        go = scene.create_game_object("GO")
        go.add_component("Rigidbody")

        go.add_component("BoxCollider")
        names = [c.type_name for c in go.get_components()]
        assert "Transform" in names
        assert "Rigidbody" in names
        assert "BoxCollider" in names

    def test_get_components_returns_python_instances_not_proxies(self, scene):
        class ProbeComponent(InxComponent):
            pass

        go = scene.create_game_object("GO")
        probe = go.add_component(ProbeComponent)

        components = go.get_components()

        assert probe in components
        assert all(type(component).__name__ != "PyComponentProxy" for component in components)

    def test_game_object_document_restore_recreates_python_components(self, scene):
        class DocumentProbeComponent(InxComponent):
            pass

        go = scene.create_game_object("DocumentRestoreGO")
        original = go.add_component(DocumentProbeComponent)
        document = go.serialize_document()

        from Infernux.engine.component_restore import deserialize_game_object_document_transactionally
        assert deserialize_game_object_document_transactionally(go, document) is True

        restored = go.get_component(DocumentProbeComponent)
        assert isinstance(restored, DocumentProbeComponent)
        assert restored is not original

    def test_script_loader_preserves_class_identity_for_imports(self, scene, tmp_path):
        from Infernux.components.script_loader import load_component_from_file
        from Infernux.engine.project_context import (
            get_project_root,
            set_project_root,
            temporary_script_import_paths,
        )

        project_root = tmp_path / "project"
        assets_root = project_root / "Assets"
        assets_root.mkdir(parents=True)
        script_path = assets_root / "a2.py"
        script_path.write_text(
            "from Infernux.components import *\n\n"
            "class NewComponent1(InxComponent):\n"
            "    pass\n",
            encoding="utf-8",
        )

        previous_root = get_project_root()
        saved_modules = {name: sys.modules.get(name) for name in ("a2", "Assets", "Assets.a2")}
        for name in saved_modules:
            sys.modules.pop(name, None)

        set_project_root(str(project_root))
        try:
            loaded_class = load_component_from_file(str(script_path))
            with temporary_script_import_paths(str(script_path)):
                direct_module = importlib.import_module("a2")
                with pytest.raises(ModuleNotFoundError):
                    importlib.import_module("Assets.a2")

            go = scene.create_game_object("GO")
            go.add_component(loaded_class)
            components = go.get_components()

            assert loaded_class is direct_module.NewComponent1
            assert any(isinstance(component, direct_module.NewComponent1) for component in components)
        finally:
            set_project_root(previous_root)
            for name in ("a2", "Assets.a2", "Assets"):
                sys.modules.pop(name, None)
            for name, module in saved_modules.items():
                if module is not None:
                    sys.modules[name] = module

    def test_remove_component(self, scene):
        go = scene.create_game_object("GO")
        rb = go.add_component("Rigidbody")
        go.remove_component(rb)
        assert go.get_component("Rigidbody") is None

    def test_remove_box_collider_with_mesh_collider_and_rigidbody(self, scene):
        go = scene.create_primitive(PrimitiveType.Cube, "ColliderHost")
        mesh = go.add_component("MeshCollider")
        box = go.get_component("BoxCollider")
        go.add_component("Rigidbody")
        assert mesh.convex is True

        assert go.remove_component(box) is True
        assert go.get_component("BoxCollider") is None
        assert go.get_component("MeshCollider") is mesh
        assert go.get_component("Rigidbody") is not None

    def test_rigidbody_does_not_invent_a_box_collider(self, scene):
        owner = scene.create_game_object("ShapeLessRigidbody")
        owner.add_component("Rigidbody")
        assert owner.get_component("BoxCollider") is None
        assert owner.get_component("Collider") is None

    def test_dynamic_rigidbody_forces_mesh_collider_convex_in_both_component_orders(self, scene):
        mesh_first = scene.create_primitive(PrimitiveType.Cube, "MeshFirst")
        first_mesh = mesh_first.add_component("MeshCollider")
        mesh_first.add_component("Rigidbody")
        assert first_mesh.convex is True

        rigidbody_first = scene.create_primitive(PrimitiveType.Cube, "RigidbodyFirst")
        rigidbody_first.add_component("Rigidbody")
        second_mesh = rigidbody_first.add_component("MeshCollider")
        assert second_mesh.convex is True

        with pytest.raises(ValueError, match="dynamic Rigidbody requires MeshCollider.convex"):
            second_mesh.convex = False
        assert second_mesh.convex is True

    def test_undo_adding_dynamic_rigidbody_restores_mesh_collider_convex(self, scene):
        from Infernux.engine.ui._inspector_undo import (
            _get_component_ids,
            _get_native_component_documents,
            _record_add_component_compound,
        )
        from Infernux.engine.undo import UndoManager

        previous_manager = UndoManager.instance()
        manager = UndoManager()
        try:
            owner = scene.create_primitive(PrimitiveType.Cube, "UndoDynamicMesh")
            mesh = owner.add_component("MeshCollider")
            assert mesh.convex is False
            before_documents = _get_native_component_documents(owner)
            before_ids = _get_component_ids(owner)

            rigidbody = owner.add_component("Rigidbody")
            _record_add_component_compound(
                owner,
                "Rigidbody",
                rigidbody,
                before_ids,
                before_documents=before_documents,
            )
            assert mesh.convex is True

            manager.undo()
            assert owner.get_component("Rigidbody") is None
            assert mesh.convex is False

            manager.redo()
            assert owner.get_component("Rigidbody") is not None
            assert mesh.convex is True
        finally:
            UndoManager._instance = previous_manager

    def test_dynamic_mesh_collider_survives_play_mode_document_rebuild(self, scene):
        from Infernux.engine.play_mode import PlayModeManager

        owner = scene.create_primitive(PrimitiveType.Cube, "PlayModeDynamicMesh")
        mesh = owner.add_component("MeshCollider")
        owner.add_component("Rigidbody")
        snapshot = scene.serialize_document()
        mesh_document = next(
            component
            for component in snapshot["objects"][0]["components"]
            if component["type_id"] == "native:infernux.MeshCollider"
        )
        assert mesh_document["data"]["convex"] is True

        previous_manager = PlayModeManager.instance()
        manager = PlayModeManager()
        manager.set_asset_database(AssetRegistry.instance().get_asset_database())
        try:
            assert manager._rebuild_active_scene(snapshot, for_play=True)
            runtime_owner = SceneManager.instance().get_active_scene().find("PlayModeDynamicMesh")
            assert runtime_owner.get_component("MeshCollider").convex is True

            assert manager._rebuild_active_scene(snapshot, for_play=False)
            restored_owner = SceneManager.instance().get_active_scene().find("PlayModeDynamicMesh")
            assert restored_owner.get_component("MeshCollider").convex is True
        finally:
            PlayModeManager._instance = previous_manager

    def test_mesh_collider_without_mesh_reports_cooking_error(self, scene):
        go = scene.create_game_object("MissingMesh")
        mesh = go.add_component("MeshCollider")
        Physics.sync_transforms()
        assert "requires a MeshRenderer" in mesh.shape_error

    def test_cannot_remove_transform(self, scene):
        go = scene.create_game_object("GO")
        t = go.get_component("Transform")
        result = go.remove_component(t)
        assert result is False
        assert go.get_component("Transform") is not None

    @pytest.mark.parametrize("comp_type", [
        "Rigidbody", "BoxCollider", "SphereCollider", "CapsuleCollider",
        "MeshCollider", "MeshRenderer", "Light", "Camera",
        "AudioSource", "AudioListener",
    ])
    def test_all_component_types_addable(self, scene, comp_type):
        go = scene.create_game_object(f"GO_{comp_type}")
        comp = go.add_component(comp_type)
        assert comp is not None
        assert comp.type_name == comp_type

    def test_python_component_receives_disable_when_game_object_deactivates(self, scene):
        events = []

        class ProbeComponent(InxComponent):
            def awake(self):
                events.append("awake")

            def on_enable(self):
                events.append("on_enable")

            def on_disable(self):
                events.append("on_disable")

        go = scene.create_game_object("LifecycleGO")
        go.add_component(ProbeComponent)

        go.active = False
        go.active = True

        assert events == ["awake", "on_enable", "on_disable", "on_enable"]

    def test_adding_component_to_inactive_game_object_defers_awake_until_activation(self, scene):
        events = []

        class ProbeComponent(InxComponent):
            def awake(self):
                events.append("awake")

            def on_enable(self):
                events.append("on_enable")

        go = scene.create_game_object("InactiveLifecycleGO")
        go.active = False
        go.add_component(ProbeComponent)

        assert events == []

        go.active = True

        assert events == ["awake", "on_enable"]

    def test_component_added_during_update_starts_before_same_frame_late_update(self, scene):
        sm = SceneManager.instance()
        events = []

        class SpawnedComponent(InxComponent):
            def awake(self):
                events.append("spawned_awake")

            def on_enable(self):
                events.append("spawned_on_enable")

            def start(self):
                events.append("spawned_start")

            def late_update(self, delta_time: float):
                events.append("spawned_late_update")

        class SpawnerComponent(InxComponent):
            def awake(self):
                self._spawned = False

            def update(self, delta_time: float):
                if self._spawned:
                    return
                self._spawned = True
                events.append("spawner_update")
                self.game_object.add_component(SpawnedComponent)

            def late_update(self, delta_time: float):
                events.append("spawner_late_update")

        go = scene.create_game_object("StartTimingGO")
        go.add_component(SpawnerComponent)

        sm.play()
        sm.pause()
        events.clear()

        sm.step(1.0 / 60.0)

        assert events == [
            "spawner_update",
            "spawned_awake",
            "spawned_on_enable",
            "spawned_start",
            "spawner_late_update",
            "spawned_late_update",
        ]

    def test_python_proxy_reports_native_update_dispatch(self, scene):
        sm = SceneManager.instance()

        class ProbeComponent(InxComponent):
            def update(self, delta_time: float):
                self.last_delta_time = delta_time

        component = scene.create_game_object("DispatchProbe").add_component(ProbeComponent)
        proxy = component._cpp_component

        sm.play()
        sm.pause()
        dispatch_before = proxy.update_dispatch_count
        forward_before = proxy.update_forward_count

        sm.step(1.0 / 60.0)

        assert proxy.overrides_update is True
        assert proxy.update_dispatch_count == dispatch_before + 1
        assert proxy.update_forward_count == forward_before + 1
        assert component.last_delta_time == pytest.approx(1.0 / 60.0)

    def test_disabling_component_does_not_stop_coroutines(self, scene):
        sm = SceneManager.instance()
        events = []

        class ProbeComponent(InxComponent):
            def awake(self):
                self.start_coroutine(self._runner())

            def _runner(self):
                events.append("coroutine_started")
                yield None
                events.append("coroutine_resumed")

            def update(self, delta_time: float):
                events.append("update")

        sm.play()
        sm.pause()

        go = scene.create_game_object("DisabledCoroutineGO")
        comp = go.add_component(ProbeComponent)
        comp.enabled = False
        events.clear()

        sm.step(1.0 / 60.0)

        assert events == ["coroutine_resumed"]

    def test_game_object_deactivation_stops_coroutines_even_when_component_is_disabled(self, scene):
        sm = SceneManager.instance()
        events = []

        class ProbeComponent(InxComponent):
            def awake(self):
                self.start_coroutine(self._runner())

            def _runner(self):
                events.append("coroutine_started")
                yield None
                events.append("coroutine_resumed")

        sm.play()
        sm.pause()

        go = scene.create_game_object("DeactivatedCoroutineGO")
        comp = go.add_component(ProbeComponent)
        comp.enabled = False
        events.clear()

        go.active = False
        go.active = True
        sm.step(1.0 / 60.0)

        assert events == []

    def test_awake_exception_disables_component(self, scene):
        events = []

        class ProbeComponent(InxComponent):
            def awake(self):
                events.append("awake")
                raise RuntimeError("boom")

            def on_enable(self):
                events.append("on_enable")

        go = scene.create_game_object("AwakeExceptionGO")
        comp = go.add_component(ProbeComponent)

        assert events == ["awake"]
        assert comp.enabled is False

    def test_python_component_destroy_skips_on_destroy_when_never_activated(self, scene):
        events = []

        class ProbeComponent(InxComponent):
            def on_destroy(self):
                events.append("on_destroy")

        go = scene.create_game_object("DormantDestroyGO")
        go.active = False
        go.add_component(ProbeComponent)

        scene.destroy_game_object(go)
        scene.process_pending_destroys()

        assert events == []

    def test_destroy_active_python_component_calls_disable_before_destroy(self, scene):
        events = []

        class ProbeComponent(InxComponent):
            def awake(self):
                events.append("awake")

            def on_enable(self):
                events.append("on_enable")

            def on_disable(self):
                events.append("on_disable")

            def on_destroy(self):
                events.append("on_destroy")

        go = scene.create_game_object("ActiveDestroyGO")
        go.add_component(ProbeComponent)
        events.clear()

        scene.destroy_game_object(go)
        scene.process_pending_destroys()

        assert events == ["on_disable", "on_destroy"]

    def test_renderstack_clears_active_instance_when_host_game_object_deactivates(self, scene):
        go = scene.create_game_object("RenderStackGO")
        stack = go.add_component(RenderStack)

        assert RenderStack.instance() is stack

        go.active = False

        assert RenderStack.instance() is None

    def test_renderstack_pipeline_ignores_inactive_game_objects(self, scene):
        go = scene.create_game_object("InactiveRenderStackGO")
        go.add_component(RenderStack)
        go.active = False

        RenderStack._active_instance = None


        class _Context:
            pass

        ctx = _Context()
        ctx.scene = scene

        pipeline = RenderStackPipeline()
        assert pipeline._find_render_stack(ctx) is None

    def test_renderstack_pipeline_restores_singleton_from_scene_cache(self, scene):
        owner = scene.create_game_object("CachedRenderStackGO")
        stack = owner.add_component(RenderStack)

        class _Context:
            pass

        ctx = _Context()
        ctx.scene = scene
        pipeline = RenderStackPipeline()

        RenderStack._active_instance = None
        assert pipeline._find_render_stack(ctx) is stack
        assert RenderStack.instance() is stack

        RenderStack._active_instance = None
        assert pipeline._find_render_stack(ctx) is stack
        assert RenderStack.instance() is stack

    def test_renderstack_active_instance_survives_play_mode_document_rebuild(self, scene):
        from Infernux.engine.play_mode import PlayModeManager

        owner = scene.create_game_object("PlayModeRenderStack")
        original = owner.add_component(RenderStack)
        snapshot = scene.serialize_document()

        previous_manager = PlayModeManager.instance()
        manager = PlayModeManager()
        manager.set_asset_database(AssetRegistry.instance().get_asset_database())
        try:
            assert RenderStack.instance() is original
            assert manager._rebuild_active_scene(snapshot, for_play=True)

            rebuilt_owner = SceneManager.instance().get_active_scene().find("PlayModeRenderStack")
            rebuilt = next(
                component
                for component in rebuilt_owner.get_py_components()
                if isinstance(component, RenderStack)
            )
            assert rebuilt is not original
            assert RenderStack.instance() is rebuilt
        finally:
            PlayModeManager._instance = previous_manager

    def test_renderstack_effect_slots_survive_component_serialization_hooks(self, scene):
        stack = scene.create_game_object("EffectBindingRenderStack").add_component(RenderStack)
        slots = (
            EffectSlot(slot_id="empty-slot", stage_id="final"),
            EffectSlot(
                slot_id="effect-slot",
                stage_id="final",
                effect=RenderEffectRef(
                    guid="missing-effect-guid",
                    path_hint="Assets/RenderEffects/Missing.effect",
                ),
            ),
        )
        stack.set_effect_stage_slots("final", slots)

        persisted = stack._serialize_fields_document()
        stack.effect_slots = []
        stack._deserialize_fields_document(persisted)

        restored = stack.get_effect_stage_slots("final")
        assert len(restored) == 2
        assert restored[0].slot_id == "empty-slot"
        assert restored[1].slot_id == "effect-slot"
        assert restored[1].effect_ref.guid == "missing-effect-guid"
        assert not hasattr(stack, "effect_stage_bindings_json")

    def test_renderstack_rejects_obsolete_binding_source(self, scene):
        stack = scene.create_game_object("InvalidEffectBindingRenderStack").add_component(RenderStack)
        with pytest.raises(ValueError, match="removed"):
            stack._deserialize_fields_document(
                {"effect_stage_bindings_json": '{"$schema":"broken"}'}
            )

# ═══════════════════════════════════════════════════════════════════════════
# Collider properties
# ═══════════════════════════════════════════════════════════════════════════

class TestColliders:
    def test_box_collider_size(self, scene):
        go = scene.create_game_object("BC")
        bc = go.add_component("BoxCollider")
        bc.size = Vector3(2, 3, 4)
        s = bc.size
        assert (s.x, s.y, s.z) == pytest.approx((2, 3, 4))

    def test_sphere_collider_radius(self, scene):
        go = scene.create_game_object("SC")
        sc = go.add_component("SphereCollider")
        sc.radius = 2.5
        assert sc.radius == pytest.approx(2.5)

    def test_capsule_collider_properties(self, scene):
        go = scene.create_game_object("CC")
        cc = go.add_component("CapsuleCollider")
        cc.height = 3.0
        cc.radius = 1.0
        assert cc.radius == pytest.approx(1.0)
        assert cc.height == pytest.approx(3.0)

    def test_collider_is_trigger(self, scene):
        go = scene.create_game_object("T")
        bc = go.add_component("BoxCollider")
        bc.is_trigger = True
        assert bc.is_trigger is True
        bc.is_trigger = False
        assert bc.is_trigger is False

    def test_collider_material_combine_round_trip(self, scene):
        material = InxPhysicMaterial()
        material.friction_combine = 2
        material.bounce_combine = 3

        assert material.friction_combine == 2
        assert material.bounce_combine == 3

        document = material.serialize_document()
        assert document["friction_combine"] == 2
        assert document["bounce_combine"] == 3

    def test_physic_material_inspector_edit_is_undoable_and_republishes(self):
        from types import SimpleNamespace
        from Infernux.core.physic_material import PhysicMaterial
        from Infernux.engine.ui.asset_details_renderer import _apply_physic_material_edit
        from Infernux.engine.undo import UndoManager

        class _ExecutionLayer:
            def __init__(self):
                self.published = []

            def schedule_rw_save(self, resource):
                self.published.append(resource.serialize_document())

        previous_manager = UndoManager.instance()
        manager = UndoManager()
        material = PhysicMaterial()
        execution_layer = _ExecutionLayer()
        state = SimpleNamespace(settings=material, exec_layer=execution_layer)
        try:
            assert _apply_physic_material_edit(state, "friction", 0.8)
            assert material.friction == pytest.approx(0.8)

            manager.undo()
            assert material.friction == pytest.approx(0.4)

            manager.redo()
            assert material.friction == pytest.approx(0.8)
            assert [entry["friction"] for entry in execution_layer.published] == pytest.approx([0.8, 0.4, 0.8])
        finally:
            UndoManager._instance = previous_manager

    def test_collider_rejects_invalid_material_combine(self, scene):
        material = InxPhysicMaterial()
        with pytest.raises(ValueError):
            material.friction_combine = 4
        with pytest.raises(ValueError):
            material.bounce_combine = -1

    def test_builtin_collider_exposes_typed_material_combine(self, scene):
        from Infernux.core.physic_material import PhysicMaterial

        collider = scene.create_game_object("TypedMaterial").add_component(BoxColliderComponent)
        material = PhysicMaterial()
        material.friction_combine = PhysicsMaterialCombine.Multiply
        material.bounce_combine = PhysicsMaterialCombine.Maximum
        collider.physic_material = material

        resolved = collider.physic_material.resolve()
        assert resolved is not None
        assert resolved.friction_combine == PhysicsMaterialCombine.Multiply
        assert resolved.bounce_combine == PhysicsMaterialCombine.Maximum

    @pytest.mark.parametrize(
        "component_type,attribute,value",
        [
            ("BoxCollider", "size", Vector3(0, 1, 1)),
            ("SphereCollider", "radius", 0.0),
            ("CapsuleCollider", "height", 0.5),
            ("CapsuleCollider", "direction", 3),
        ],
    )
    def test_collider_setters_reject_invalid_values(self, scene, component_type, attribute, value):
        collider = scene.create_game_object("StrictColliderSetter").add_component(component_type)
        with pytest.raises(ValueError):
            setattr(collider, attribute, value)

    @pytest.mark.parametrize(
        "component_type,field,value",
        [
            ("BoxCollider", "size", [1, 0, 1]),
            ("SphereCollider", "radius", -1.0),
            ("CapsuleCollider", "direction", 9),
            ("CapsuleCollider", "height", 0.5),
            ("MeshCollider", "convex", "yes"),
            ("BoxCollider", "physic_material_guid", 7),
        ],
    )
    def test_collider_documents_reject_invalid_values_transactionally(
        self, scene, component_type, field, value
    ):
        collider = scene.create_game_object("StrictColliderDocument").add_component(component_type)
        original = collider.serialize_document()
        invalid = dict(original)
        invalid[field] = value

        assert collider.deserialize_document(invalid) is False
        assert collider.serialize_document() == original

    def test_collider_document_rejects_removed_ordinary_field(self, scene):
        collider = scene.create_game_object("UnknownColliderField").add_component("BoxCollider")
        original = collider.serialize_document()
        invalid = dict(original)
        invalid["legacy_material"] = 1

        assert collider.deserialize_document(invalid) is False
        assert collider.serialize_document() == original

    @pytest.mark.parametrize(
        "field,value",
        [
            ("unknown", 2),
            ("friction", 1.1),
            ("bounciness", float("nan")),
            ("friction_combine", 4),
            ("bounce_combine", -1),
        ],
    )
    def test_physic_material_document_is_strict_and_transactional(self, field, value):
        material = InxPhysicMaterial()
        original = material.serialize_document()
        invalid = dict(original)
        invalid[field] = value

        with pytest.raises(ValueError):
            material.deserialize_document(invalid)
        assert material.serialize_document() == original

    def test_collider_persists_only_physic_material_guid(self, scene):
        registry = AssetRegistry.instance()
        asset_database = registry.get_asset_database()
        asset_path = Path(asset_database.assets_root) / "SharedSurface.physicMaterial"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_text(json.dumps({
            "friction": 0.65,
            "bounciness": 0.25,
            "friction_combine": 2,
            "bounce_combine": 3,
        }), encoding="utf-8")
        guid = asset_database.import_asset(str(asset_path)).guid
        assert guid

        material = registry.load_physic_material_by_guid(guid)
        assert material is not None
        assert registry.get_asset_version(guid) == 1
        assert registry.get_asset_runtime_type_name(guid)
        with pytest.raises(ValueError, match="Runtime asset type mismatch"):
            registry.get_mesh(guid)
        with pytest.raises(ValueError, match="resource type mismatch"):
            registry.load_mesh_by_guid(guid)
        assert registry.get_asset_version(guid) == 1
        collider = scene.create_game_object("SharedSurfaceCollider").add_component("BoxCollider")
        collider.physic_material = material
        document = collider.serialize_document()

        assert document["physic_material_guid"] == guid
        assert "friction" not in document
        assert "bounciness" not in document

        restored = scene.create_game_object("RestoredSurfaceCollider").add_component("BoxCollider")
        restored_document = dict(document)
        restored_document["component_id"] = restored.component_id
        assert restored.deserialize_document(restored_document) is True
        assert restored.physic_material_guid == guid
        assert restored.physic_material.resolve().native is material

        asset_path.write_text(json.dumps({
            "friction": 0.1,
            "bounciness": 0.9,
            "friction_combine": 1,
            "bounce_combine": 2,
        }), encoding="utf-8")
        assert AssetManager.reimport_asset(str(asset_path), database=asset_database)
        assert registry.get_asset_version(guid) == 2
        assert restored.physic_material.resolve().native is material
        assert material.friction == pytest.approx(0.1)
        assert material.bounciness == pytest.approx(0.9)

        assert AssetManager.delete_asset(str(asset_path), database=asset_database)
        asset_path.unlink(missing_ok=True)
        assert collider.physic_material.resolve() is None
        assert collider.physic_material.guid == ""
        assert collider.physic_material_guid == ""
        assert restored.physic_material.resolve() is None
        assert restored.physic_material.guid == ""
        assert restored.physic_material_guid == ""

# ═══════════════════════════════════════════════════════════════════════════
# Camera
# ═══════════════════════════════════════════════════════════════════════════

class TestCamera:
    def test_camera_defaults(self, scene):
        go = scene.create_game_object("Cam")
        cam = go.add_component("Camera")
        assert cam.field_of_view == pytest.approx(60.0)
        assert cam.near_clip > 0
        assert cam.far_clip > cam.near_clip

    def test_camera_fov_round_trip(self, scene):
        go = scene.create_game_object("Cam")
        cam = go.add_component("Camera")
        cam.field_of_view = 90.0
        assert cam.field_of_view == pytest.approx(90.0)

    def test_camera_depth(self, scene):
        go = scene.create_game_object("Cam")
        cam = go.add_component("Camera")
        cam.depth = 5
        assert cam.depth == pytest.approx(5)

# ═══════════════════════════════════════════════════════════════════════════
# Light
# ═══════════════════════════════════════════════════════════════════════════

class TestLight:
    def test_light_defaults(self, scene):
        go = scene.create_game_object("L")
        light = go.add_component("Light")
        assert light.light_type == LightType.Directional
        assert light.intensity == pytest.approx(1.0)
        assert light.shadow_bias == pytest.approx(1.0)
        assert light.shadow_normal_bias == pytest.approx(1.0)
        assert light.affect_geometry is True
        assert light.affect_particles is True

    def test_light_type_point(self, scene):
        go = scene.create_game_object("PL")
        light = go.add_component("Light")
        light.light_type = LightType.Point
        assert light.light_type == LightType.Point

    def test_light_intensity_round_trip(self, scene):
        go = scene.create_game_object("L")
        light = go.add_component("Light")
        light.intensity = 2.5
        assert light.intensity == pytest.approx(2.5)

    def test_light_color(self, scene):
        go = scene.create_game_object("L")
        light = go.add_component("Light")
        light.color = Vector3(1, 0, 0)
        c = light.color
        assert c[0] == pytest.approx(1.0)
        assert c[1] == pytest.approx(0.0)
        assert c[2] == pytest.approx(0.0)
        assert c[3] == pytest.approx(1.0)

    def test_light_shadows(self, scene):
        go = scene.create_game_object("L")
        light = go.add_component("Light")
        light.shadows = LightShadows.Hard
        assert light.shadows == LightShadows.Hard

    def test_light_influence_domains_round_trip(self, scene):
        go = scene.create_game_object("DomainLight")
        light = go.add_component("Light")
        light.affect_geometry = False
        light.affect_particles = True

        assert light.affect_geometry is False
        assert light.affect_particles is True
        data = json.loads(light.serialize())
        assert data["influenceDomains"] == 2

# ═══════════════════════════════════════════════════════════════════════════
# MeshRenderer
# ═══════════════════════════════════════════════════════════════════════════

class TestMeshRenderer:
    def test_primitive_mesh_has_data(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "Cube")
        mr = cube.get_component("MeshRenderer")
        assert mr is not None
        positions = mr.get_positions()
        normals = mr.get_normals()
        indices = mr.get_indices()
        assert len(positions) > 0
        assert len(normals) > 0
        assert len(indices) > 0

    def test_sphere_has_more_verts_than_cube(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "C")
        sphere = scene.create_primitive(PrimitiveType.Sphere, "S")
        cube_verts = len(cube.get_component("MeshRenderer").get_positions())
        sphere_verts = len(sphere.get_component("MeshRenderer").get_positions())
        assert sphere_verts > cube_verts

    def test_shadow_properties(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "C")
        mr = cube.get_component("MeshRenderer")
        mr.casts_shadows = False
        assert mr.casts_shadows is False
        mr.casts_shadows = True
        assert mr.casts_shadows is True

# ═══════════════════════════════════════════════════════════════════════════
# Texture (real GPU-side creation)
# ═══════════════════════════════════════════════════════════════════════════

class TestTextureCreation:
    def test_solid_color(self, engine):
        tex = TextureLoader.create_solid_color(32, 32, 255, 0, 0, 255)
        assert tex.width == 32
        assert tex.height == 32

    def test_different_sizes(self, engine):
        for size in [1, 16, 64, 256]:
            tex = TextureLoader.create_solid_color(size, size, 0, 0, 0, 255)
            assert tex.width == size
            assert tex.height == size

# ═══════════════════════════════════════════════════════════════════════════
# Material
# ═══════════════════════════════════════════════════════════════════════════

class TestMaterial:
    def test_python_material_wrapper_saves_through_current_native_api(self, engine, tmp_path):
        material = Material.create_lit("WrapperSave")
        path = tmp_path / "wrapper-save.mat"

        assert material.save(str(path)) is True
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["name"] == "WrapperSave"

    def test_texture_assignment_is_guid_only(self, engine):
        material = InxMaterial.create_default_unlit()

        material.set_texture("texSampler", "white")
        assert material.get_texture("texSampler") == "white"

        with pytest.raises(ValueError, match="texture GUID does not exist"):
            material.set_texture("texSampler", "Assets/Textures/legacy-path.png")
        with pytest.raises(ValueError, match="texture GUID does not exist"):
            material.set_texture("texSampler", "missing-texture-guid")
        assert material.get_texture("texSampler") == "white"

    def test_create_default_lit(self, engine):
        mat = InxMaterial.create_default_lit()
        assert mat is not None

    def test_material_assignable_to_renderer(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "MatCube")
        mr = cube.get_component("MeshRenderer")
        mat = InxMaterial.create_default_lit()
        mr.material = mat
        assert mr.get_material(0) is not None

    def test_material_document_is_strict_and_transactional(self, engine):
        mat = InxMaterial.create_default_lit()
        mat.name = "StableMaterial"
        mat.set_float("testValue", 0.25)
        document = json.loads(mat.serialize())

        assert "material_version" not in document
        assert document["shaders"]["vertex"]["shader_id"] == "Standard"
        assert document["shaders"]["fragment"]["shader_id"] == "Lit"

        shader_metadata = json.loads(json.dumps(document))
        shader_metadata["_shader_property_order"] = ["baseColor", "testValue"]
        shader_metadata["properties"]["baseColor"]["hdr"] = True
        shader_metadata["properties"]["testValue"]["range"] = [0.0, 1.0]
        assert mat.deserialize(json.dumps(shader_metadata)) is True
        metadata_round_trip = json.loads(mat.serialize())
        assert metadata_round_trip["_shader_property_order"] == ["baseColor", "testValue"]
        assert metadata_round_trip["properties"]["baseColor"]["hdr"] is True
        assert metadata_round_trip["properties"]["testValue"]["range"] == [0.0, 1.0]
        mat.set_color("baseColor", (2.0, 1.0, 0.5, 1.0))
        assert json.loads(mat.serialize())["properties"]["baseColor"]["hdr"] is True
        mat.set_float("testValue", 0.75)
        assert json.loads(mat.serialize())["properties"]["testValue"]["range"] == [0.0, 1.0]

        invalid_shader_order = json.loads(json.dumps(shader_metadata))
        invalid_shader_order["_shader_property_order"].append("missingProperty")
        assert mat.deserialize(json.dumps(invalid_shader_order)) is False
        assert json.loads(mat.serialize())["_shader_property_order"] == ["baseColor", "testValue"]

        invalid_range = json.loads(json.dumps(shader_metadata))
        invalid_range["properties"]["testValue"]["range"] = [2.0, 1.0]
        assert mat.deserialize(json.dumps(invalid_range)) is False
        assert json.loads(mat.serialize())["properties"]["testValue"]["range"] == [0.0, 1.0]

        extended_state = json.loads(json.dumps(document))
        extended_state["renderState"].update(
            {
                "lineWidth": 2.5,
                "depthBiasEnable": True,
                "depthBiasConstantFactor": 1.25,
                "depthBiasSlopeFactor": 0.75,
                "depthBiasClamp": 0.5,
                "topology": 1,
                "srcAlphaBlendFactor": 6,
                "dstAlphaBlendFactor": 7,
                "alphaBlendOp": 2,
            }
        )
        assert mat.deserialize(json.dumps(extended_state)) is True
        round_tripped_state = json.loads(mat.serialize())["renderState"]
        for field, expected in extended_state["renderState"].items():
            assert round_tripped_state[field] == expected

        removed_version = json.loads(json.dumps(document))
        removed_version["material_version"] = 4
        removed_version["name"] = "PartialMutation"
        assert mat.deserialize(json.dumps(removed_version)) is False
        assert mat.name == "StableMaterial"
        assert mat.get_float("testValue", 0.0) == pytest.approx(0.25)

        invalid_render_state = json.loads(json.dumps(document))
        invalid_render_state["renderState"]["lineWidth"] = 0.0
        invalid_render_state["name"] = "InvalidPipelineState"
        assert mat.deserialize(json.dumps(invalid_render_state)) is False
        assert mat.name == "StableMaterial"

        invalid_property = json.loads(json.dumps(document))
        invalid_property["properties"]["testValue"]["type"] = 99
        invalid_property["name"] = "AnotherPartialMutation"
        assert mat.deserialize(json.dumps(invalid_property)) is False
        assert mat.name == "StableMaterial"
        assert mat.get_float("testValue", 0.0) == pytest.approx(0.25)

        unknown_field = json.loads(json.dumps(document))
        unknown_field["legacyPath"] = "Assets/Materials/legacy.mat"
        assert mat.deserialize(json.dumps(unknown_field)) is False
        assert mat.name == "StableMaterial"

        unknown_property_field = json.loads(json.dumps(document))
        unknown_property_field["properties"]["testValue"]["legacy"] = True
        assert mat.deserialize(json.dumps(unknown_property_field)) is False
        assert mat.get_float("testValue", 0.0) == pytest.approx(0.25)

    def test_material_save_is_atomic(self, engine, tmp_path):
        mat = InxMaterial.create_default_unlit()
        path = tmp_path / "atomic.mat"

        assert mat.save_to(str(path)) is True
        assert "material_version" not in json.loads(path.read_text(encoding="utf-8"))
        assert list(tmp_path.glob("atomic.mat.tmp.*")) == []

    def test_renderer_embeds_typed_material_document(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "InlineMaterialCube")
        renderer = cube.get_component("MeshRenderer")
        material = InxMaterial.create_default_unlit()
        material.name = "InlineRuntimeMaterial"
        renderer.material = material

        document = json.loads(renderer.serialize())
        slot = document["materials"][0]
        assert isinstance(slot["material"], dict)
        assert "material_version" not in slot["material"]
        assert "material_json" not in slot

        serialized_scene = scene.serialize()
        scene_manager = SceneManager.instance()
        restored_scene = scene_manager.create_scene("material_round_trip")
        scene_manager.set_active_scene(restored_scene)
        assert restored_scene._commit_document(json.loads(serialized_scene)) is True
        restored = restored_scene.find("InlineMaterialCube").get_component("MeshRenderer")
        restored_slot = json.loads(restored.serialize())["materials"][0]
        assert restored_slot["material"]["name"] == "InlineRuntimeMaterial"

# ═══════════════════════════════════════════════════════════════════════════
# Component serialization
# ═══════════════════════════════════════════════════════════════════════════

class TestComponentSerialization:
    def test_embedded_material_source_path_is_rejected_without_mutation(self, scene):
        owner = scene.create_game_object("StrictEmbeddedMaterial")
        renderer = owner.add_component("MeshRenderer")
        renderer.material = InxMaterial.create_default_unlit()
        original = renderer.serialize_document()
        invalid = json.loads(json.dumps(original))
        invalid["materials"][0]["source_path"] = "Assets/Materials/legacy.mat"

        assert renderer.deserialize_document(invalid) is False
        assert renderer.serialize_document() == original

    @pytest.mark.parametrize("resource_kind", ["mesh", "material", "audio"])
    def test_missing_native_resource_fails_without_component_mutation(self, scene, resource_kind):
        owner = scene.create_game_object(f"Missing{resource_kind.title()}Resource")
        component_type = "AudioSource" if resource_kind == "audio" else "MeshRenderer"
        component = owner.add_component(component_type)
        original = component.serialize_document()
        invalid = json.loads(json.dumps(original))
        missing_guid = f"missing-{resource_kind}-resource-guid"
        if resource_kind == "mesh":
            invalid["meshAssetGuid"] = missing_guid
        elif resource_kind == "material":
            invalid["materials"] = [missing_guid]
        else:
            invalid["tracks"][0]["clip_guid"] = missing_guid

        assert component.deserialize_document(invalid) is False
        assert component.serialize_document() == original

    @pytest.mark.parametrize(
        "component_type",
        [
            "Transform",
            "Camera",
            "Light",
            "AudioListener",
            "AudioSource",
            "Rigidbody",
            "BoxCollider",
            "SphereCollider",
            "CapsuleCollider",
            "MeshCollider",
            "MeshRenderer",
            "SkinnedMeshRenderer",
            "SpriteRenderer",
        ],
    )
    def test_registered_component_rejects_removed_ordinary_field(self, scene, component_type):
        owner = scene.create_game_object(f"Strict{component_type}")
        if component_type == "Transform":
            component = owner.transform
        else:
            component = owner.add_component(component_type)
        original = component.serialize_document()
        invalid = dict(original)
        invalid["legacy"] = True

        assert component.deserialize_document(invalid) is False
        assert component.serialize_document() == original

    def test_component_documents_are_strict(self, scene):
        cube = scene.create_primitive(PrimitiveType.Cube, "CurrentFormatCube")
        renderer = cube.get_component("MeshRenderer")
        renderer_document = renderer.serialize_document()
        renderer_document["unknown"] = 5
        assert renderer.deserialize_document(renderer_document) is False

        rigidbody = cube.add_component("Rigidbody")
        rigidbody_document = rigidbody.serialize_document()
        assert "instance_guid" not in rigidbody_document

        obsolete_document = dict(rigidbody_document)
        obsolete_document["instance_guid"] = str(rigidbody.component_id)
        assert rigidbody.deserialize_document(obsolete_document) is False

        rigidbody_document["type"] = "Camera"
        assert rigidbody.deserialize_document(rigidbody_document) is False

    @pytest.mark.parametrize(
        "field,value",
        [
            ("mass", 0.0),
            ("drag", -1.0),
            ("constraints", 1),
            ("collision_detection_mode", 99),
            ("interpolation", 2),
            ("max_angular_velocity", -1.0),
        ],
    )
    def test_rigidbody_document_rejects_invalid_values_transactionally(self, scene, field, value):
        game_object = scene.create_game_object("StrictRigidbody")
        rigidbody = game_object.add_component("Rigidbody")
        rigidbody.mass = 3.5
        original = rigidbody.serialize_document()
        invalid = dict(original)
        invalid[field] = value

        assert rigidbody.deserialize_document(invalid) is False
        assert rigidbody.serialize_document() == original

    def test_rigidbody_document_requires_complete_current_schema(self, scene):
        rigidbody = scene.create_game_object("IncompleteRigidbody").add_component("Rigidbody")
        original = rigidbody.serialize_document()
        incomplete = dict(original)
        incomplete.pop("angular_drag")

        assert rigidbody.deserialize_document(incomplete) is False
        assert rigidbody.serialize_document() == original

    def test_rigidbody_serializes(self, scene):
        go = scene.create_game_object("RB")
        rb = go.add_component("Rigidbody")
        rb.mass = 3.14
        json_str = rb.serialize()
        assert "mass" in json_str.lower() or "3.14" in json_str

    def test_round_trip_via_scene(self, scene):
        go = scene.create_game_object("Persist")
        go.transform.position = Vector3(1, 2, 3)
        rb = go.add_component("Rigidbody")
        rb.mass = 7.77
        go.add_component("SphereCollider").radius = 2.0

        json_str = scene.serialize()

        sm = SceneManager.instance()
        scene2 = sm.create_scene("reload")
        sm.set_active_scene(scene2)
        from Infernux.engine.component_restore import deserialize_scene_document_transactionally
        assert deserialize_scene_document_transactionally(scene2, json.loads(json_str)) is True

        found = scene2.find("Persist")
        assert found is not None
        assert found.transform.position.x == pytest.approx(1)
        rb2 = found.get_component("Rigidbody")
        assert rb2.mass == pytest.approx(7.77)
        sc2 = found.get_component("SphereCollider")
        assert sc2.radius == pytest.approx(2.0)
