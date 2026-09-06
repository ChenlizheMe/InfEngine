from __future__ import annotations

import copy

import pytest


def _manifest_document(flavor, features):
    from Infernux.engine.player_service_graph import (
        PLAYER_MANIFEST_SCHEMA,
        player_manifest_service_section,
        runtime_policy_for,
    )

    return {
        "$schema": PLAYER_MANIFEST_SCHEMA,
        "product": {"flavor": flavor.value},
        "features": features.to_manifest(),
        "runtime_policy": runtime_policy_for(flavor).to_manifest(),
        "services": player_manifest_service_section(flavor, features),
    }


def _asset_documents(scene_path="Assets/Scenes/Main.scene"):
    artifact_id = "content:scene-main"
    catalog = {
        "artifacts": [
            {
                "runtime_artifact_id": artifact_id,
                "runtime_path": scene_path,
                "package": "Game_Data/Content.inxpkg",
                "logical_type": "scene",
                "asset_guid": "scene-guid",
                "dependencies": [],
            }
        ]
    }
    records = {
        "entries": [
            {
                "guid": "scene-guid",
                "primary_runtime_artifact_id": artifact_id,
                "runtime_artifact_ids": [artifact_id],
                "dependencies": [],
            }
        ]
    }
    return catalog, records


@pytest.mark.parametrize("invalid", ["duplicate", "dependency", "path"])
def test_runtime_catalog_owner_rejects_invalid_asset_relationships(tmp_path, invalid):
    from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog

    catalog, records = _asset_documents()
    if invalid == "duplicate":
        catalog["artifacts"].append(dict(catalog["artifacts"][0]))
    elif invalid == "dependency":
        catalog["artifacts"][0]["dependencies"] = ["unknown-artifact"]
    else:
        catalog["artifacts"][0]["runtime_path"] = "../outside.scene"
    with pytest.raises(RuntimeError):
        PlayerRuntimeAssetCatalog.from_documents(str(tmp_path), catalog, records)


def test_runtime_service_graph_is_authoritative_for_all_products():
    from Infernux.engine.player_service_graph import (
        RuntimeFeatureSet,
        RuntimeFlavor,
        runtime_service_graph_for,
    )

    editor = runtime_service_graph_for(RuntimeFlavor.EDITOR_DEVELOPMENT)
    assert editor.contains("editor_resources")
    assert editor.contains("editor_script_compiler")
    assert editor.contains("editor_selection")
    assert editor.contains("editor_undo")
    assert not editor.contains("player_runtime_session")

    release = runtime_service_graph_for(RuntimeFlavor.PLAYER_RELEASE)
    assert release.contains("player_runtime_session")
    assert not release.contains("editor_resources")
    assert not release.contains("player_control_debug")
    assert not release.contains("jit_runtime_support")
    assert not release.contains("parallel_module")

    debug = runtime_service_graph_for(RuntimeFlavor.PLAYER_DEBUG)
    assert debug.contains("player_control_debug")

    accelerated = runtime_service_graph_for(
        RuntimeFlavor.PLAYER_RELEASE,
        RuntimeFeatureSet(
            jit=True,
            parallel=True,
            optional_subsystems=("splash",),
        ),
    )
    assert accelerated.contains("jit_runtime_support")
    assert accelerated.contains("parallel_module")
    assert accelerated.contains("splash_player")


def test_parallel_feature_requires_jit():
    from Infernux.engine.player_service_graph import RuntimeFeatureSet

    with pytest.raises(ValueError, match="requires the JIT"):
        RuntimeFeatureSet(parallel=True)
    with pytest.raises(ValueError, match="unknown runtime subsystems"):
        RuntimeFeatureSet(optional_subsystems=("editor_preview",))


def test_runtime_manifest_rejects_any_service_graph_drift():
    from Infernux.engine.player_service_graph import (
        RuntimeFeatureSet,
        RuntimeFlavor,
        RuntimeProductManifest,
    )

    document = _manifest_document(
        RuntimeFlavor.PLAYER_RELEASE,
        RuntimeFeatureSet(),
    )
    manifest = RuntimeProductManifest.from_document(document)
    assert manifest.flavor is RuntimeFlavor.PLAYER_RELEASE
    assert manifest.require_service("player_runtime_session").authoring is False

    drifted = copy.deepcopy(document)
    drifted["services"]["graph"][0]["module"] = "Infernux/engine/undo/_manager.pyc"
    with pytest.raises(RuntimeError, match="authoritative runtime product graph"):
        RuntimeProductManifest.from_document(drifted)

    release_with_control = copy.deepcopy(document)
    release_with_control["services"]["declared"].append("player_control_debug")
    with pytest.raises(RuntimeError, match="declared-service"):
        RuntimeProductManifest.from_document(release_with_control)


def test_runtime_manifest_rejects_scattered_policy_override():
    from Infernux.engine.player_service_graph import (
        RuntimeFeatureSet,
        RuntimeFlavor,
        RuntimeProductManifest,
    )

    document = _manifest_document(
        RuntimeFlavor.PLAYER_RELEASE,
        RuntimeFeatureSet(),
    )
    document["runtime_policy"]["profiling"] = "available"
    with pytest.raises(RuntimeError, match="runtime policy"):
        RuntimeProductManifest.from_document(document)


def test_runtime_asset_catalog_never_falls_back_to_source_discovery(tmp_path):
    from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog

    scene = tmp_path / "Assets" / "Scenes" / "Main.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("scene", encoding="utf-8")
    unlisted = scene.with_name("Unlisted.scene")
    unlisted.write_text("scene", encoding="utf-8")
    catalog_document, records = _asset_documents()
    catalog = PlayerRuntimeAssetCatalog.from_documents(
        str(tmp_path),
        catalog_document,
        records,
    )

    assert catalog.resolve_scene("Assets/Scenes/Main.scene") == str(scene)
    assert catalog.resolve_scene(str(scene)) == str(scene)
    assert catalog.resolve_scene(str(unlisted)) is None
    assert catalog.resolve_scene(tmp_path.parent / "Outside.scene") is None
    assert catalog.artifact_ids_for_guid("scene-guid") == ("content:scene-main",)


def test_runtime_asset_catalog_resolves_source_scene_alias_to_cooked_document(tmp_path):
    from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog

    cooked = (
        tmp_path
        / "Library"
        / "Artifacts"
        / "Document"
        / "scene-guid.scene"
    )
    cooked.parent.mkdir(parents=True)
    cooked.write_text("scene", encoding="utf-8")
    artifact_id = "content:cooked-scene"
    catalog = {
        "artifacts": [
            {
                "runtime_artifact_id": artifact_id,
                "runtime_path": "Library/Artifacts/Document/scene-guid.scene",
                "package": "Game_Data/Content.inxpkg",
                "logical_type": "scene_artifact",
                "asset_guid": "scene-guid",
                "dependencies": [],
            }
        ]
    }
    records = {
        "entries": [
            {
                "guid": "scene-guid",
                "runtime_path": "Assets/Scenes/Main.scene",
                "primary_runtime_artifact_id": artifact_id,
                "runtime_artifact_ids": [artifact_id],
                "dependencies": [],
            }
        ]
    }

    runtime_catalog = PlayerRuntimeAssetCatalog.from_documents(
        str(tmp_path), catalog, records
    )

    assert runtime_catalog.resolve_scene("Assets/Scenes/Main.scene") == str(cooked)
    assert runtime_catalog.resolve_scene(str(cooked)) == str(cooked)


def test_runtime_asset_catalog_resolves_any_source_alias_to_cooked_payload(tmp_path):
    from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog

    cooked = tmp_path / "Library" / "Artifacts" / "Blob" / "cache-guid.npy"
    cooked.parent.mkdir(parents=True)
    cooked.write_bytes(b"cache")
    artifact_id = "content:cooked-cache"
    catalog = {
        "artifacts": [
            {
                "runtime_artifact_id": artifact_id,
                "runtime_path": "Library/Artifacts/Blob/cache-guid.npy",
                "package": "Game_Data/Content.inxpkg",
                "logical_type": "project_runtime_blob_artifact",
                "asset_guid": "cache-guid",
                "dependencies": [],
            }
        ]
    }
    records = {
        "entries": [
            {
                "guid": "cache-guid",
                "runtime_path": "Assets/Data/Voxel.npy",
                "primary_runtime_artifact_id": artifact_id,
                "runtime_artifact_ids": [artifact_id],
                "dependencies": [],
            }
        ]
    }

    runtime_catalog = PlayerRuntimeAssetCatalog.from_documents(
        str(tmp_path), catalog, records
    )

    assert runtime_catalog.resolve_asset("Assets/Data/Voxel.npy") == str(cooked)
    assert runtime_catalog.resolve_asset(str(cooked)) == str(cooked)
    assert runtime_catalog.resolve_asset("Assets/Data/Missing.npy") is None


def test_player_scene_service_requires_catalog_membership(tmp_path):
    from Infernux.engine.player_scene import PlayerSceneService
    from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog

    scene = tmp_path / "Assets" / "Scenes" / "Main.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("scene", encoding="utf-8")
    unlisted = scene.with_name("Unlisted.scene")
    unlisted.write_text("scene", encoding="utf-8")
    catalog_document, records = _asset_documents()
    catalog = PlayerRuntimeAssetCatalog.from_documents(
        str(tmp_path),
        catalog_document,
        records,
    )
    service = PlayerSceneService()

    assert service.request_load(str(scene)) is False
    service.bind_runtime_catalog(catalog)
    assert service.request_load(str(unlisted)) is False
    assert service.request_load(str(scene)) is True
    service.cancel_pending_load()


def test_player_scene_service_starts_prepared_load_without_waiting_for_tick(monkeypatch):
    from Infernux.engine.player_scene import PlayerSceneService

    calls = []

    class Transaction:
        status = "reading"

        def start(self):
            calls.append("start")

        def cancel(self):
            calls.append("cancel")

    transaction = Transaction()
    service = PlayerSceneService()
    monkeypatch.setattr(
        service,
        "_validated_scene_path",
        lambda _path: "C:/Game/Content/Main.scene",
    )
    monkeypatch.setattr(service, "_new_transaction", lambda _path: transaction)

    assert service.request_prepared_load("Assets/Scenes/Main.scene") is True
    assert calls == ["start"]
    assert service.is_load_pending is True
    assert service._pending_scene_path is None
    assert service._transaction is transaction
    assert service._wait_for_ready is True

    service.cancel_pending_load()
    assert calls == ["start", "cancel"]


def test_player_scene_service_can_hold_and_activate_prepared_load(monkeypatch):
    from Infernux.engine.player_scene import PlayerSceneService

    calls = []

    class Transaction:
        status = "ready_to_commit"
        succeeded = True
        error = ""

        def start(self):
            calls.append("start")

        def poll(self):
            calls.append("commit")
            self.status = "completed"
            return True

    service = PlayerSceneService()
    monkeypatch.setattr(
        service,
        "_validated_scene_path",
        lambda _path: "C:/Game/Content/Main.scene",
    )
    monkeypatch.setattr(service, "_new_transaction", lambda _path: Transaction())
    monkeypatch.setattr(service, "_publish_completed_scene", lambda *_args, **_kwargs: None)

    assert service.request_prepared_load("Main", hold_for_activation=True) is True
    assert service.is_prepared is True
    service.process_pending_load()
    assert calls == ["start"]
    assert service.activate_prepared_load() is True
    service.process_pending_load()
    assert calls == ["start", "commit"]


def test_player_prepared_load_advances_one_phase_per_frame(monkeypatch):
    from Infernux.engine.player_scene import PlayerSceneService

    calls = []

    class Transaction:
        succeeded = True
        error = ""

        def poll(self):
            calls.append("poll")
            return len(calls) >= 3

    service = PlayerSceneService()
    service._transaction = Transaction()
    service._transaction_path = "C:/Game/Content/Main.scene"
    service._wait_for_ready = True
    monkeypatch.setattr(
        service,
        "_publish_completed_scene",
        lambda path, *, start_for_play: calls.append(
            ("publish", path, start_for_play)
        ),
    )

    service.process_pending_load()
    assert calls == ["poll"]
    assert service.is_load_pending is True

    service.process_pending_load()
    assert calls == ["poll", "poll"]
    assert service.is_load_pending is True

    service.process_pending_load()
    assert calls == [
        "poll",
        "poll",
        "poll",
        ("publish", "C:/Game/Content/Main.scene", True),
    ]
    assert service.is_load_pending is False


def test_player_type_registry_never_falls_back_when_not_installed(monkeypatch):
    from Infernux.engine.runtime_type_registry import (
        clear_runtime_type_registry,
        validate_runtime_component_identity,
    )

    clear_runtime_type_registry()
    monkeypatch.setenv("_INFERNUX_PLAYER_MODE", "1")
    with pytest.raises(RuntimeError, match="not installed"):
        validate_runtime_component_identity(
            script_guid="script-guid",
            type_guid="type-guid",
            module_name="game.components",
            qualified_name="Mover",
        )
    clear_runtime_type_registry()
