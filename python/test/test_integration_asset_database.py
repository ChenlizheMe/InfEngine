from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import time
import threading

import numpy as np
import pytest

from Infernux.lib import AssetDependencyGraph, AssetMutationErrorCode, AssetRegistry, InxMaterial, ResourceType
from Infernux.core.assets import AssetManager
from Infernux.engine.path_utils import same_path
from Infernux.particle import (
    AssetReference,
    ParticleArtifactRegistry,
    ParticleGraphAsset,
    PointCache,
    SdfVolume,
    VectorField,
)


def test_audio_import_rejects_noncurrent_metadata(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "legacy_audio.wav"
    source.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    meta_path = Path(f"{source}.meta")
    legacy_guid = "a" * 32
    meta_path.write_text(
        json.dumps({
            "metadata": {
                "guid": {"type": "string", "value": legacy_guid},
                "resource_type": {
                    "type": "enum infernux::ResourceType",
                    "value": "DefaultText",
                },
            },
        }),
        encoding="utf-8",
    )

    try:
        with pytest.raises(RuntimeError, match="current importer schema|resource_type"):
            asset_db.import_asset(str(source))
        assert not asset_db.contains_path(str(source))
    finally:
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))


def test_render_effect_import_tracks_group_dependencies(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    graph = AssetDependencyGraph.instance()
    bloom = tmp_path / "Bloom.effect"
    tone = tmp_path / "Tonemapping.effect"
    group = tmp_path / "Basic Post Processing.effectgroup"

    def effect_document(feature_type: str) -> dict:
        return {
            "$schema": "infernux.render_effect",
            "feature_type": feature_type,
            "parameters": {},
            "dependencies": [],
        }

    bloom.write_text(json.dumps(effect_document("infernux.post.bloom")), encoding="utf-8")
    tone.write_text(json.dumps(effect_document("infernux.post.tonemapping")), encoding="utf-8")

    imported_paths = []
    try:
        bloom_result = asset_db.import_asset(str(bloom))
        tone_result = asset_db.import_asset(str(tone))
        imported_paths.extend((bloom, tone))
        assert bloom_result and tone_result
        assert bloom_result.resource_type == ResourceType.RenderEffect
        assert tone_result.resource_type == ResourceType.RenderEffect

        group.write_text(
            json.dumps(
                {
                    "$schema": "infernux.render_effect_group",
                    "entries": [
                        {
                            "entry_id": "bloom",
                            "asset": {"guid": bloom_result.guid, "path_hint": str(bloom)},
                            "enabled": True,
                            "overrides": {"intensity": 0.8},
                        },
                        {
                            "entry_id": "tonemapping",
                            "asset": {"guid": "", "path_hint": str(tone)},
                            "enabled": True,
                            "overrides": {},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        group_result = asset_db.import_asset(str(group))
        imported_paths.append(group)

        assert group_result
        assert group_result.resource_type == ResourceType.RenderEffect
        assert set(graph.get_dependencies(group_result.guid)) == {
            bloom_result.guid,
            tone_result.guid,
        }
    finally:
        for path in reversed(imported_paths):
            if asset_db.contains_path(str(path)):
                asset_db.delete_asset(str(path))


def test_render_effect_import_rejects_mount_scope_in_asset(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "InvalidScope.effect"
    source.write_text(
        json.dumps(
            {
                "$schema": "infernux.render_effect",
                "feature_type": "infernux.route.pixelation",
                "parameters": {},
                "dependencies": [],
                "scope": "final",
            }
        ),
        encoding="utf-8",
    )

    result = asset_db.import_asset(str(source))

    assert not result
    assert not asset_db.contains_path(str(source))
    assert asset_db.get_guid_from_path(str(source)) == ""


def test_particle_graph_import_compiles_and_publishes_aot(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "Smoke.particlegraph"
    document = ParticleGraphAsset(stable_id="integration-smoke").to_dict()
    document["emitters"][0]["stages"]["rendering"]["nodes"][1]["properties"][
        "material"
    ] = AssetReference(guid="smoke-material-guid").to_dict()
    document["emitters"][0]["data_interfaces"] = [
        VectorField(
            stable_id="wind-field",
            texture=AssetReference(guid="wind-field-guid"),
        ).to_dict(),
        SdfVolume(
            stable_id="collision-field",
            texture=AssetReference(guid="collision-field-guid"),
        ).to_dict(),
        PointCache(
            stable_id="spawn-points",
            cache=AssetReference(guid="point-cache-guid"),
        ).to_dict(),
    ]
    source.write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    ParticleArtifactRegistry.clear()

    try:
        result = AssetManager.import_asset(str(source), database=asset_db)

        assert result
        assert result.resource_type == ResourceType.ParticleGraph
        assert asset_db.get_meta_by_path(str(source)).get_resource_type() == ResourceType.ParticleGraph
        artifact = ParticleArtifactRegistry.get(str(source), guid=result.guid)
        assert artifact is not None
        assert ParticleArtifactRegistry.get(str(source)) is artifact
        assert artifact.hir["stable_id"] == "integration-smoke"
        assert AssetDependencyGraph.instance().get_dependencies(result.guid) == {
            "collision-field-guid",
            "point-cache-guid",
            "smoke-material-guid",
            "wind-field-guid",
        }
    finally:
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))
        ParticleArtifactRegistry.clear()


def test_particle_script_import_uses_script_resource_and_particle_aot(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "Sparks.particle.py"
    source.write_text(
        """from Infernux.particle import (
    AssetReference, EmitterSettings, ParticleEmitter, ParticleScript
)

class SparksGraph(ParticleScript):
    stable_id = "integration-sparks"

    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(target="gpu")

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            particles.sprite(material=AssetReference())
""",
        encoding="utf-8",
    )
    ParticleArtifactRegistry.clear()

    try:
        result = AssetManager.import_asset(str(source), database=asset_db)

        assert result
        assert result.resource_type == ResourceType.Script
        artifact = ParticleArtifactRegistry.get(str(source), guid=result.guid)
        assert artifact is not None
        assert artifact.source_kind == "script"
        assert artifact.hir["schedule"] == ["sparks"]
    finally:
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))
        ParticleArtifactRegistry.clear()


def test_particle_graph_reimport_keeps_last_known_good_on_semantic_failure(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "StableSmoke.particlegraph"
    valid = ParticleGraphAsset(stable_id="stable-smoke")
    source.write_text(json.dumps(valid.to_dict()), encoding="utf-8")
    ParticleArtifactRegistry.clear()

    try:
        imported = AssetManager.import_asset(str(source), database=asset_db)
        assert imported
        published = ParticleArtifactRegistry.get(str(source), guid=imported.guid)
        assert published is not None

        invalid = valid.to_dict()
        invalid["emitters"][0]["stages"]["rendering"]["nodes"] = []
        invalid["emitters"][0]["stages"]["rendering"]["links"] = []
        source.write_text(json.dumps(invalid), encoding="utf-8")
        result = AssetManager.reimport_asset(str(source), database=asset_db)

        assert not result
        assert result.database_committed is True
        assert result.error_code == AssetMutationErrorCode.RUNTIME_APPLY_FAILED
        assert "keeping last-known-good" in result.error
        assert ParticleArtifactRegistry.get(str(source), guid=imported.guid) == published
    finally:
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))
        ParticleArtifactRegistry.clear()


def test_point_cache_import_bakes_typed_runtime_artifact(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "Morph.pointcache"
    source.write_text(
        json.dumps(
            {
                "$schema": "infernux.point_cache",
                "stable_id": "morph-cache",
                "name": "Morph Cache",
                "bake_basis": "right_handed_y_up",
                "point_count": 2,
                "channels": [
                    {
                        "name": "position",
                        "semantic": "position",
                        "type": "vec3",
                        "data": [[1.0, 2.0, 3.0], [-4.0, 5.5, 6.0]],
                    },
                    {
                        "name": "stable_id",
                        "semantic": "id",
                        "type": "u32",
                        "data": [7, 42],
                    },
                    {
                        "name": "temperature",
                        "semantic": "custom",
                        "type": "f32",
                        "data": [0.25, 0.75],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        result = AssetManager.import_asset(str(source), database=asset_db)

        artifact = Path(
            asset_db.get_runtime_artifact_path(result.guid, ResourceType.PointCache)
        )
        assert result, result.error
        assert result.resource_type == ResourceType.PointCache
        metadata = asset_db.get_meta_by_path(str(source))
        assert metadata.get_resource_type() == ResourceType.PointCache
        assert metadata.get_int("artifact_point_count") == 2
        assert metadata.get_int("artifact_channel_count") == 3
        assert metadata.get_string("artifact_bake_basis") == "right_handed_y_up"
        assert artifact.name == f"{result.guid}.inxpcache"
        published_artifact = artifact.read_bytes()
        assert published_artifact.startswith(b"INXPOINT")

        registry = AssetRegistry.instance()
        ticket = registry.begin_load_point_cache_by_guid(result.guid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not registry.try_commit_asset_load(ticket):
            time.sleep(0.001)
        assert ticket.committed is True
        assert ticket.produced_on_worker is True
        runtime = registry.get_point_cache(result.guid)
        assert runtime is not None
        assert runtime.guid == result.guid
        assert same_path(runtime.file_path, source)
        assert runtime.name == "Morph Cache"
        assert runtime.stable_id == "morph-cache"
        assert runtime.bake_basis == "right_handed_y_up"
        assert runtime.point_count == 2
        assert runtime.channel_names == ["position", "stable_id", "temperature"]
        assert runtime.has_channel("position")
        assert not runtime.has_channel("missing")
        assert runtime.cpu_byte_size > 0
        initial_generation = runtime.generation
        assert initial_generation > 0
        assert runtime.lookup_index(7) == 0
        assert runtime.lookup_index(42) == 1
        assert runtime.lookup_index(99) == 0xFFFFFFFF
        lookup_source = np.asarray([42, 99, 7], dtype=np.uint32)
        lookup_result = np.empty(3, dtype=np.uint32)
        runtime.lookup_indices(lookup_source, lookup_result)
        np.testing.assert_array_equal(lookup_result, [1, 0xFFFFFFFF, 0])

        original_positions = runtime.channel_array("position")
        stable_ids = runtime.channel_array("stable_id")
        temperatures = runtime.channel_array("temperature")
        assert original_positions.shape == (2, 3)
        assert original_positions.dtype == np.dtype(np.float32)
        assert stable_ids.shape == (2,)
        assert stable_ids.dtype == np.dtype(np.uint32)
        assert temperatures.shape == (2,)
        assert temperatures.dtype == np.dtype(np.float32)
        assert not original_positions.flags.writeable
        assert not stable_ids.flags.writeable
        np.testing.assert_array_equal(original_positions, [[1.0, 2.0, 3.0], [-4.0, 5.5, 6.0]])
        np.testing.assert_array_equal(stable_ids, [7, 42])
        np.testing.assert_array_equal(temperatures, [0.25, 0.75])
        with pytest.raises(KeyError, match="channel does not exist"):
            runtime.channel_array("missing")

        updated = json.loads(source.read_text(encoding="utf-8"))
        updated["channels"][0]["data"][0] = [9.0, 8.0, 7.0]
        source.write_text(json.dumps(updated), encoding="utf-8")
        reimported = AssetManager.reimport_asset(str(source), database=asset_db)
        assert reimported, reimported.error

        current_positions = runtime.channel_array("position")
        assert runtime.generation == initial_generation + 1
        np.testing.assert_array_equal(current_positions, [[9.0, 8.0, 7.0], [-4.0, 5.5, 6.0]])
        np.testing.assert_array_equal(original_positions, [[1.0, 2.0, 3.0], [-4.0, 5.5, 6.0]])
        published_artifact = artifact.read_bytes()

        invalid = json.loads(source.read_text(encoding="utf-8"))
        invalid["channels"][1]["data"] = [7, 7]
        source.write_text(json.dumps(invalid), encoding="utf-8")
        failed = AssetManager.reimport_asset(str(source), database=asset_db)

        assert not failed
        assert failed.guid == result.guid
        assert "stable point IDs must be unique" in failed.error
        assert artifact.read_bytes() == published_artifact
        assert runtime.generation == initial_generation + 1
        np.testing.assert_array_equal(runtime.channel_array("position"), current_positions)
    finally:
        if "result" in locals() and result.guid:
            AssetRegistry.instance().remove_asset(result.guid)
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))


def test_vector_field_import_exposes_immutable_volume_generations(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "Wind.inxvfield"

    def document(vectors):
        return {
            "$schema": "infernux.vector_field",
            "dimensions": [2, 1, 1],
            "storage_order": "x_fastest",
            "bake_basis": [
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ],
            "vectors": vectors,
        }

    source.write_text(json.dumps(document([[1, 2, 3], [-4, 5.5, 6]])), encoding="utf-8")
    registry = AssetRegistry.instance()

    try:
        result = AssetManager.import_asset(str(source), database=asset_db)
        assert result, result.error
        assert result.resource_type == ResourceType.Texture

        texture = registry.load_texture_by_guid(result.guid)
        assert texture is not None
        assert texture.dimension == "3d"
        assert texture.pixel_format == "rgba16_float"
        assert texture.pixel_depth == 1
        initial_generation = texture.generation
        assert initial_generation > 0
        assert tuple(texture.bake_basis) == tuple(document([])["bake_basis"])
        np.testing.assert_allclose(texture.value_min[:3], (-4.0, 2.0, 3.0))
        np.testing.assert_allclose(texture.value_max[:3], (1.0, 5.5, 6.0))

        original = texture.volume_array()
        assert original.shape == (1, 1, 2, 4)
        assert original.dtype == np.dtype(np.float16)
        assert not original.flags.writeable
        np.testing.assert_allclose(original[0, 0], [[1, 2, 3, 0], [-4, 5.5, 6, 0]])

        source.write_text(json.dumps(document([[7, 8, 9], [10, 11, 12]])), encoding="utf-8")
        reimported = AssetManager.reimport_asset(str(source), database=asset_db)
        assert reimported, reimported.error

        current = texture.volume_array()
        assert texture.generation == initial_generation + 1
        np.testing.assert_allclose(current[0, 0], [[7, 8, 9, 0], [10, 11, 12, 0]])
        np.testing.assert_allclose(original[0, 0], [[1, 2, 3, 0], [-4, 5.5, 6, 0]])
    finally:
        if "result" in locals() and result.guid:
            registry.remove_asset(result.guid)
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))


def test_signed_distance_field_import_exposes_immutable_volume_generations(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "Collider.inxsdf"

    def document(distances):
        return {
            "$schema": "infernux.sdf",
            "dimensions": [2, 1, 1],
            "storage_order": "x_fastest",
            "distance_unit": "field",
            "bake_basis": [
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ],
            "distances": distances,
        }

    source.write_text(json.dumps(document([-0.25, 0.75])), encoding="utf-8")
    registry = AssetRegistry.instance()

    try:
        result = AssetManager.import_asset(str(source), database=asset_db)
        assert result, result.error
        assert result.resource_type == ResourceType.Texture

        texture = registry.load_texture_by_guid(result.guid)
        assert texture is not None
        assert texture.dimension == "3d"
        assert texture.semantic == "signed_distance_field"
        assert texture.pixel_format == "rgba16_float"
        assert texture.pixel_depth == 1
        initial_generation = texture.generation
        assert initial_generation > 0
        assert tuple(texture.bake_basis) == tuple(document([])["bake_basis"])
        assert texture.value_min[0] == pytest.approx(-0.25)
        assert texture.value_max[0] == pytest.approx(0.75)

        original = texture.volume_array()
        assert original.shape == (1, 1, 2, 4)
        assert original.dtype == np.dtype(np.float16)
        assert not original.flags.writeable
        np.testing.assert_allclose(original[0, 0], [[-0.25, 0, 0, 0], [0.75, 0, 0, 0]])

        source.write_text(json.dumps(document([-0.5, 1.25])), encoding="utf-8")
        reimported = AssetManager.reimport_asset(str(source), database=asset_db)
        assert reimported, reimported.error

        current = texture.volume_array()
        assert texture.generation == initial_generation + 1
        np.testing.assert_allclose(current[0, 0], [[-0.5, 0, 0, 0], [1.25, 0, 0, 0]])
        np.testing.assert_allclose(original[0, 0], [[-0.25, 0, 0, 0], [0.75, 0, 0, 0]])
    finally:
        if "result" in locals() and result.guid:
            registry.remove_asset(result.guid)
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))


def test_asset_database_never_indexes_python_bytecode_or_cache_paths(engine):
    asset_db = engine.get_asset_database()
    fixture = Path(asset_db.assets_root) / "python-bytecode-ignore-fixture"
    cache = fixture / "__pycache__"
    similarly_named = fixture / "my__pycache__data"
    cache.mkdir(parents=True, exist_ok=True)
    similarly_named.mkdir(parents=True, exist_ok=True)
    cached_bytecode = cache / "Controller.cpython-312.pyc"
    top_level_bytecode = fixture / "Legacy.PYC"
    control = similarly_named / "control.txt"
    cached_bytecode.write_bytes(b"not real bytecode")
    top_level_bytecode.write_bytes(b"not real bytecode")
    control.write_text("import me", encoding="utf-8")

    try:
        asset_db.refresh()

        assert asset_db.contains_path(str(cached_bytecode)) is False
        assert asset_db.contains_path(str(top_level_bytecode)) is False
        assert asset_db.get_guid_from_path(str(cached_bytecode)) == ""
        assert asset_db.get_guid_from_path(str(top_level_bytecode)) == ""
        assert not Path(f"{cached_bytecode}.meta").exists()
        assert not Path(f"{top_level_bytecode}.meta").exists()
        assert cached_bytecode not in {Path(path) for path in asset_db.last_refresh_imported_paths}
        assert top_level_bytecode not in {Path(path) for path in asset_db.last_refresh_imported_paths}

        assert asset_db.contains_path(str(control)) is True
        assert asset_db.get_guid_from_path(str(control))

        explicit = asset_db.import_asset(str(top_level_bytecode))
        assert not explicit
        assert explicit.error_code == AssetMutationErrorCode.UNSUPPORTED_TYPE
        assert not Path(f"{top_level_bytecode}.meta").exists()
    finally:
        if asset_db.contains_path(str(control)):
            asset_db.delete_asset(str(control))
        shutil.rmtree(fixture, ignore_errors=True)
        asset_db.refresh()


def test_dependency_graph_separates_asset_and_runtime_domains():
    graph = AssetDependencyGraph.instance()
    asset_user = "test-asset-user"
    runtime_user = "test-runtime-user"
    dependency = "test-shared-dependency"
    generation = graph.asset_generation

    for legacy_name in ("add_dependency", "remove_dependency", "clear_dependencies_of", "set_dependencies"):
        assert not hasattr(graph, legacy_name)

    try:
        graph.add_asset_dependency(asset_user, dependency)
        assert graph.asset_generation == generation + 1
        graph.add_runtime_dependency(runtime_user, dependency)
        assert graph.has_dependency(asset_user, dependency)
        assert graph.has_dependency(runtime_user, dependency)
        assert {asset_user, runtime_user} <= set(graph.get_dependents(dependency))

        graph.clear_asset_dependencies_of(asset_user)
        assert not graph.has_dependency(asset_user, dependency)
        assert graph.has_dependency(runtime_user, dependency)
        assert set(graph.get_dependents(dependency)) == {runtime_user}

        with pytest.raises(ValueError, match="cannot depend on itself"):
            graph.add_runtime_dependency(runtime_user, runtime_user)
    finally:
        graph.clear_asset_dependencies_of(asset_user)
        graph.clear_runtime_dependencies_of(runtime_user)


def test_asset_database_canonical_crud_preserves_guid(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "canonical_asset.txt"
    source.write_text("first", encoding="utf-8")

    import_result = asset_db.import_asset(str(source))
    assert import_result
    guid = import_result.guid
    assert asset_db.get_guid_from_path(str(source)) == guid
    assert Path(asset_db.get_path_from_guid(guid)).resolve() == source.resolve()
    assert asset_db.get_meta_by_path(str(source)).get_guid() == guid
    catalog = asset_db.get_directory_catalog(str(tmp_path))
    catalog_entry = next(entry for entry in catalog if entry["guid"] == guid)
    assert Path(catalog_entry["path"]).resolve() == source.resolve()
    assert catalog_entry["name"] == source.name
    assert catalog_entry["size"] == len("first")
    assert asset_db.catalog_generation == asset_db.query_generation

    moved = tmp_path / "canonical_asset_moved.txt"
    source.replace(moved)
    assert asset_db.move_asset(str(source), str(moved))
    assert asset_db.get_guid_from_path(str(moved)) == guid
    assert not asset_db.contains_path(str(source))
    moved_catalog = asset_db.get_directory_catalog(str(tmp_path))
    assert next(entry for entry in moved_catalog if entry["guid"] == guid)["name"] == moved.name

    moved.unlink()
    assert asset_db.delete_asset(str(moved))
    assert not asset_db.contains_guid(guid)
    assert not Path(f"{moved}.meta").exists()
    assert all(entry["guid"] != guid for entry in asset_db.get_directory_catalog(str(tmp_path)))


def test_asset_database_does_not_expose_legacy_resource_crud(engine):
    asset_db = engine.get_asset_database()
    for name in (
        "register_resource",
        "modify_resource",
        "delete_resource",
        "move_resource",
        "get_all_resource_guids",
        "on_asset_created",
        "on_asset_modified",
        "on_asset_deleted",
        "on_asset_moved",
    ):
        assert not hasattr(asset_db, name)


def test_metadata_creation_uses_the_submitted_source_bytes(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    text = tmp_path / "metadata-source.txt"
    texture = tmp_path / "metadata-source.ppm"
    text_bytes = b"first\r\nsecond\r\n"
    texture_bytes = b"P6\n2 1\n255\n" + bytes((255, 0, 0, 0, 255, 0))
    text.write_bytes(text_bytes)
    texture.write_bytes(texture_bytes)

    try:
        text_guid = asset_db.import_asset(str(text)).guid
        texture_guid = asset_db.import_asset(str(texture)).guid
        assert text_guid and texture_guid

        text_meta = asset_db.get_meta_by_guid(text_guid)
        texture_meta = asset_db.get_meta_by_guid(texture_guid)
        text_document = text_meta.serialize_document()
        texture_document = texture_meta.serialize_document()
        assert text_document["metadata"]["file_size"] == {
            "type": "size_t",
            "value": len(text_bytes),
        }
        assert texture_document["metadata"]["file_size"] == {
            "type": "size_t",
            "value": len(texture_bytes),
        }
        assert texture_meta.get_int("width") == 2
        assert texture_meta.get_int("height") == 1
        assert texture_meta.get_int("channels") == 3
        assert texture_meta.get_string("source_container") == "PPM"
        assert texture_meta.get_string("texture_format") == "auto"
    finally:
        for path in (text, texture):
            if asset_db.contains_path(str(path)):
                asset_db.delete_asset(str(path))
            path.unlink(missing_ok=True)
            Path(f"{path}.meta").unlink(missing_ok=True)


def test_material_import_artifact_commits_metadata_and_dependencies_atomically(
    engine, tmp_path: Path
):
    asset_db = engine.get_asset_database()
    graph = AssetDependencyGraph.instance()
    vertex = tmp_path / "artifact.vert"
    fragment = tmp_path / "artifact.frag"
    material = tmp_path / "artifact.mat"
    vertex.write_text("void main() {}", encoding="utf-8")
    fragment.write_text("void main() {}", encoding="utf-8")
    vertex_guid = asset_db.import_asset(str(vertex)).guid
    fragment_guid = asset_db.import_asset(str(fragment)).guid
    assert vertex_guid and fragment_guid

    def write_material(shader_paths: list[Path]) -> None:
        document = json.loads(InxMaterial.create_default_lit().serialize())
        if shader_paths:
            document["shaders"]["vertex"] = {
                "guid": "", "shader_id": "artifact-vertex", "path_hint": str(shader_paths[0]),
            }
        if len(shader_paths) > 1:
            document["shaders"]["fragment"] = {
                "guid": "", "shader_id": "artifact-fragment", "path_hint": str(shader_paths[1]),
            }
        material.write_text(json.dumps(document), encoding="utf-8")

    try:
        material.write_text("{ invalid first import", encoding="utf-8")
        failed_import = asset_db.import_asset(str(material))
        assert not failed_import
        assert failed_import.error_code == AssetMutationErrorCode.IMPORT_FAILED
        assert failed_import.database_committed is False
        assert failed_import.error
        assert not asset_db.contains_path(str(material))

        write_material([vertex, fragment])
        material_guid = asset_db.import_asset(str(material)).guid
        assert material_guid
        assert set(graph.get_dependencies(material_guid)) == {
            vertex_guid,
            fragment_guid,
        }
        metadata_before = asset_db.get_meta_by_guid(material_guid).serialize_document()
        generation_before = asset_db.query_generation

        material.write_text("{ invalid material", encoding="utf-8")
        assert not asset_db.reimport_asset(str(material))
        assert asset_db.query_generation == generation_before
        assert set(graph.get_dependencies(material_guid)) == {
            vertex_guid,
            fragment_guid,
        }
        assert (
            asset_db.get_meta_by_guid(material_guid).serialize_document()
            == metadata_before
        )

        write_material([vertex])
        assert asset_db.reimport_asset(str(material))
        assert set(graph.get_dependencies(material_guid)) == {vertex_guid}
    finally:
        if asset_db.contains_path(str(material)):
            asset_db.delete_asset(str(material))
        asset_db.delete_asset(str(vertex))
        asset_db.delete_asset(str(fragment))


def test_asset_database_explicit_reimport_preserves_guid(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "reimport.txt"
    source.write_text("first", encoding="utf-8")
    guid = asset_db.import_asset(str(source)).guid

    source.write_text("second", encoding="utf-8")
    assert asset_db.reimport_asset(str(source))
    assert asset_db.get_guid_from_path(str(source)) == guid

    unregistered = tmp_path / "unregistered.txt"
    unregistered.write_text("content", encoding="utf-8")
    missing = asset_db.reimport_asset(str(unregistered))
    assert not missing
    assert missing.error_code == AssetMutationErrorCode.NOT_FOUND
    assert missing.error


def test_asset_database_rejects_worker_thread_mutation(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    source = tmp_path / "worker.txt"
    source.write_text("content", encoding="utf-8")
    errors = []

    def mutate():
        try:
            asset_db.import_asset(str(source))
        except RuntimeError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=mutate)
    worker.start()
    worker.join()

    assert len(errors) == 1
    assert "owner thread" in errors[0]
    assert not asset_db.contains_path(str(source))


def test_asset_database_publishes_concurrent_reader_snapshots(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    stable_path = tmp_path / "stable-reader.txt"
    stable_path.write_text("stable", encoding="utf-8")
    stable_guid = asset_db.import_asset(str(stable_path)).guid
    initial_generation = asset_db.query_generation

    start = threading.Event()
    stop = threading.Event()
    errors: list[str] = []

    def read_snapshots():
        start.wait()
        try:
            while not stop.is_set():
                assert asset_db.contains_guid(stable_guid)
                assert asset_db.contains_path(str(stable_path))
                assert asset_db.get_guid_from_path(str(stable_path)) == stable_guid
                assert asset_db.get_path_from_guid(stable_guid)
                meta = asset_db.get_meta_by_guid(stable_guid)
                assert meta is not None and meta.get_guid() == stable_guid
                assert stable_guid in asset_db.get_all_guids()
                assert any(
                    entry["guid"] == stable_guid
                    for entry in asset_db.get_directory_catalog(str(tmp_path))
                )
                assert asset_db.asset_count >= 1
        except BaseException as exc:
            errors.append(repr(exc))
            stop.set()

    readers = [threading.Thread(target=read_snapshots) for _ in range(4)]
    for reader in readers:
        reader.start()

    start.set()
    for index in range(32):
        transient = tmp_path / f"transient-{index}.txt"
        transient.write_text(str(index), encoding="utf-8")
        transient_guid = asset_db.import_asset(str(transient)).guid
        assert transient_guid
        assert asset_db.delete_asset(str(transient))
        transient.unlink()

    stop.set()
    for reader in readers:
        reader.join(timeout=5)
        assert not reader.is_alive()

    assert errors == []
    assert asset_db.query_generation >= initial_generation + 64
    assert asset_db.asset_count == len(asset_db.get_all_guids())

    retained_meta = asset_db.get_meta_by_guid(stable_guid)
    assert retained_meta is not None
    assert asset_db.delete_asset(str(stable_path))
    assert asset_db.get_meta_by_guid(stable_guid) is None
    assert retained_meta.get_guid() == stable_guid


@pytest.mark.skipif(os.name != "nt", reason="Windows short-path normalization regression")
def test_delete_removes_reverse_mapping_after_short_path_source_disappears(engine, tmp_path: Path):
    import ctypes

    asset_db = engine.get_asset_database()
    source = tmp_path / "short-path-delete-regression.txt"
    source.write_text("short path", encoding="utf-8")

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(source), buffer, len(buffer))
    if not length or buffer.value == str(source):
        pytest.skip("8.3 short paths are unavailable on this volume")

    result = asset_db.import_asset(buffer.value)
    assert result
    guid = result.guid
    assert guid
    assert asset_db.get_guid_from_path(str(source)) == guid

    source.unlink()
    assert asset_db.delete_asset(str(source))
    assert not asset_db.contains_guid(guid)
    assert not asset_db.contains_path(str(source))
    assert all(entry["guid"] != guid for entry in asset_db.get_directory_catalog(str(tmp_path)))


def test_refresh_builds_import_artifacts_only_on_workers(engine):
    asset_db = engine.get_asset_database()
    graph = AssetDependencyGraph.instance()
    fixture = Path(asset_db.assets_root) / "worker-import-artifact-fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    vertex = fixture / "worker.vert"
    fragment = fixture / "worker.frag"
    material = fixture / "worker.mat"
    model = fixture / "worker.obj"
    vertex.write_text("void main() {}", encoding="utf-8")
    fragment.write_text("void main() {}", encoding="utf-8")
    material_document = json.loads(InxMaterial.create_default_lit().serialize())
    material_document["shaders"] = {
        "vertex": {"guid": "", "shader_id": "worker-vertex", "path_hint": str(vertex)},
        "fragment": {"guid": "", "shader_id": "worker-fragment", "path_hint": str(fragment)},
    }
    material.write_text(json.dumps(material_document), encoding="utf-8")
    model.write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        encoding="ascii",
    )

    paths = (vertex, fragment, material, model)
    try:
        asset_db.refresh()
        assert asset_db.last_refresh_metadata_task_count >= len(paths)
        assert (
            asset_db.last_refresh_worker_metadata_count
            == asset_db.last_refresh_metadata_task_count
        )
        assert asset_db.last_refresh_index_build_on_worker is True
        assert asset_db.last_refresh_importer_task_count >= len(paths)
        assert (
            asset_db.last_refresh_worker_importer_count
            == asset_db.last_refresh_importer_task_count
        )
        assert set(paths).issubset(
            {Path(path) for path in asset_db.last_refresh_imported_paths}
        )

        vertex_guid = asset_db.get_guid_from_path(str(vertex))
        fragment_guid = asset_db.get_guid_from_path(str(fragment))
        material_guid = asset_db.get_guid_from_path(str(material))
        assert set(graph.get_dependencies(material_guid)) == {
            vertex_guid,
            fragment_guid,
        }
        model_meta = asset_db.get_meta_by_path(str(model))
        assert model_meta.get_int("mesh_count") == 1
        assert model_meta.get_int("vertex_count") == 3
        assert model_meta.get_int("index_count") == 3
        assert model_meta.get_int("material_slot_count") == 1
        assert model_meta.get_int("bone_count") == 0
        assert model_meta.get_int("animation_count") == 0
    finally:
        for path in paths:
            if asset_db.contains_path(str(path)):
                asset_db.delete_asset(str(path))
            path.unlink(missing_ok=True)
            Path(f"{path}.meta").unlink(missing_ok=True)
        fixture.rmdir()
        asset_db.refresh()


def test_asset_index_reuses_unchanged_assets_and_recovers_from_corruption(engine):
    asset_db = engine.get_asset_database()
    assert Path(asset_db.assets_root).name == "Assets"
    assert Path(asset_db.assets_root).parent.resolve() == Path(asset_db.project_root).resolve()
    fixture_root = Path(asset_db.assets_root) / "asset-index-fixture"
    fixture_root.mkdir(parents=True, exist_ok=True)
    paths = [fixture_root / f"asset-{index}.txt" for index in range(16)]
    for index, path in enumerate(paths):
        path.write_text(f"initial-{index}", encoding="utf-8")

    try:
        asset_db.refresh()
        assert asset_db.last_refresh_scan_on_worker is True
        assert asset_db.last_refresh_query_build_on_worker is True
        assert asset_db.last_refresh_query_build_ms >= 0.0
        assert asset_db.last_refresh_owner_merge_slice_count >= 3
        assert asset_db.last_refresh_owner_merge_max_slice_ms >= 0.0
        assert asset_db.last_refresh_scanned_count >= len(paths)
        assert asset_db.last_refresh_scan_ms >= 0.0
        assert asset_db.last_refresh_commit_ms >= 0.0
        original_guids = {path: asset_db.get_guid_from_path(str(path)) for path in paths}
        assert all(original_guids.values())
        assert asset_db.last_refresh_imported_count >= len(paths)

        index_path = Path(asset_db.asset_index_path)
        index_document = json.loads(index_path.read_text(encoding="utf-8"))
        assert set(index_document) == {"project_root", "entries"}

        query_generation = asset_db.query_generation
        catalog_generation = asset_db.catalog_generation
        asset_db.refresh()
        assert asset_db.last_refresh_reused_count >= len(paths)
        assert asset_db.last_refresh_imported_paths == []
        assert asset_db.query_generation == query_generation
        assert asset_db.catalog_generation == catalog_generation
        assert asset_db.last_refresh_restore_ms == 0.0
        assert asset_db.last_refresh_import_ms == 0.0
        assert asset_db.last_refresh_index_build_ms == 0.0
        assert asset_db.last_refresh_index_save_ms == 0.0
        assert asset_db.last_refresh_publish_ms == 0.0
        assert {path: asset_db.get_guid_from_path(str(path)) for path in paths} == original_guids

        changed_meta = Path(f"{paths[3]}.meta")
        changed_meta.write_text(
            changed_meta.read_text(encoding="utf-8") + "\n ",
            encoding="utf-8",
        )
        asset_db.refresh()
        assert asset_db.last_refresh_imported_count >= 1
        assert asset_db.last_refresh_reused_count >= len(paths) - 1
        assert asset_db.get_guid_from_path(str(paths[3])) == original_guids[paths[3]]

        paths[5].write_text("changed-content-with-a-different-size", encoding="utf-8")
        asset_db.refresh()
        assert asset_db.last_refresh_imported_count >= 1
        assert asset_db.last_refresh_reused_count >= len(paths) - 1
        assert asset_db.get_guid_from_path(str(paths[5])) == original_guids[paths[5]]

        index_path.write_text('{"legacy": true}', encoding="utf-8")
        asset_db.refresh()
        assert asset_db.last_refresh_imported_count >= len(paths)
        assert {path: asset_db.get_guid_from_path(str(path)) for path in paths} == original_guids
        rebuilt = json.loads(index_path.read_text(encoding="utf-8"))
        assert set(rebuilt) == {"project_root", "entries"}
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
            Path(f"{path}.meta").unlink(missing_ok=True)
        fixture_root.rmdir()
        asset_db.refresh()


def test_asset_database_async_refresh_commits_worker_artifact(engine):
    asset_db = engine.get_asset_database()
    asset_db.begin_refresh()
    assert asset_db.refresh_pending is True
    with pytest.raises(RuntimeError, match="already pending"):
        asset_db.begin_refresh()

    deadline = time.monotonic() + 10.0
    committed = False
    while time.monotonic() < deadline:
        if asset_db.try_commit_refresh():
            committed = True
            break
        time.sleep(0.001)

    assert committed is True
    assert asset_db.refresh_pending is False
    assert asset_db.last_refresh_scan_on_worker is True
    if asset_db.last_refresh_imported_count:
        assert asset_db.last_refresh_query_build_on_worker is True


def test_async_refresh_hides_prepared_state_until_worker_import_finalize(
    engine, tmp_path: Path
):
    asset_db = engine.get_asset_database()
    asset_db.refresh()
    fixture = Path(asset_db.assets_root) / "pending-import-visibility"
    fixture.mkdir(parents=True, exist_ok=True)
    model = fixture / "pending.obj"
    model.write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
        encoding="ascii",
    )
    blocked_mutation = tmp_path / "blocked-during-import.txt"
    blocked_mutation.write_text("blocked", encoding="utf-8")
    generation_before = asset_db.query_generation
    count_before = asset_db.asset_count

    try:
        asset_db.begin_refresh()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            assert asset_db.try_commit_refresh() is False
            if asset_db.last_refresh_importer_task_count > 0:
                break
            time.sleep(0.001)
        else:
            pytest.fail("refresh never entered its worker importer phase")

        assert asset_db.refresh_pending is True
        assert asset_db.query_generation == generation_before
        assert asset_db.asset_count == count_before
        assert asset_db.contains_path(str(model)) is False
        assert asset_db.get_meta_by_path(str(model)) is None
        assert Path(f"{model}.meta").exists() is False
        with pytest.raises(RuntimeError, match="refresh commit is pending"):
            asset_db.import_asset(str(blocked_mutation))

        while time.monotonic() < deadline:
            if asset_db.try_commit_refresh():
                break
            time.sleep(0.001)
        else:
            pytest.fail("worker importer phase did not finalize")

        assert asset_db.refresh_pending is False
        assert asset_db.query_generation > generation_before
        assert asset_db.asset_count == count_before + 1
        assert asset_db.contains_path(str(model)) is True
        assert asset_db.get_meta_by_path(str(model)).get_int("mesh_count") == 1
        assert Path(f"{model}.meta").is_file()
    finally:
        if asset_db.refresh_pending:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not asset_db.try_commit_refresh():
                time.sleep(0.001)
        if asset_db.contains_path(str(model)):
            asset_db.delete_asset(str(model))
        model.unlink(missing_ok=True)
        Path(f"{model}.meta").unlink(missing_ok=True)
        fixture.rmdir()
        asset_db.refresh()


def test_asset_database_rejects_stale_async_scan(engine, tmp_path: Path):
    asset_db = engine.get_asset_database()
    asset_db.begin_refresh()

    mutation = tmp_path / "mutation-during-scan.txt"
    mutation.write_text("newer owner state", encoding="utf-8")
    guid = asset_db.import_asset(str(mutation)).guid
    assert guid

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            completed = asset_db.try_commit_refresh()
        except RuntimeError as error:
            assert "stale" in str(error)
            break
        if completed:
            pytest.fail("stale scan artifact replaced a newer AssetDatabase generation")
        time.sleep(0.001)
    else:
        pytest.fail("asynchronous AssetDatabase scan did not finish")

    assert asset_db.refresh_pending is False
    assert asset_db.get_guid_from_path(str(mutation)) == guid


def test_refresh_regenerates_incompatible_metadata(engine):
    asset_db = engine.get_asset_database()
    fixture = Path(asset_db.assets_root) / "prepare-rollback-fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    source = fixture / "rollback.txt"
    source.write_text("stable", encoding="utf-8")

    try:
        asset_db.refresh()
        guid = asset_db.get_guid_from_path(str(source))
        meta_path = Path(f"{source}.meta")
        meta_path.write_text("{ broken metadata", encoding="utf-8")

        asset_db.refresh()
        assert asset_db.get_guid_from_path(str(source)) == guid
        regenerated = json.loads(meta_path.read_text(encoding="utf-8"))
        assert set(regenerated) == {"metadata"}
        assert regenerated["metadata"]["guid"]["value"] == guid

        regenerated["meta_version"] = 2
        meta_path.write_text(json.dumps(regenerated), encoding="utf-8")
        asset_db.refresh()
        assert asset_db.get_guid_from_path(str(source)) == guid
        regenerated = json.loads(meta_path.read_text(encoding="utf-8"))
        assert set(regenerated) == {"metadata"}
        assert regenerated["metadata"]["guid"]["value"] == guid
    finally:
        if asset_db.refresh_pending:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not asset_db.try_commit_refresh():
                time.sleep(0.001)
        if asset_db.contains_path(str(source)):
            asset_db.delete_asset(str(source))
        source.unlink(missing_ok=True)
        Path(f"{source}.meta").unlink(missing_ok=True)
        fixture.rmdir()
        asset_db.refresh()


def test_builtin_read_only_resources_do_not_create_metadata(engine):
    from Infernux.resources import resources_path

    resource_root = Path(resources_path)
    assert engine.get_asset_database().contains_path(str(resource_root / "shaders" / "standard.vert"))
    assert list(resource_root.rglob("*.meta")) == []
