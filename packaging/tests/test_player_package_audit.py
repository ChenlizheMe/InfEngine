from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import struct
import types

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PYTHON = ROOT / "python"
sys.path.insert(0, str(SOURCE_PYTHON))

# Import the two pure-Python packaging modules without requiring the runtime
# initializer when this test runs in isolation. Never replace an Infernux
# package already imported by another test module during collection.
if "Infernux" not in sys.modules:
    _infernux_stub = types.ModuleType("Infernux")
    _infernux_stub.__path__ = [str(SOURCE_PYTHON / "Infernux")]
    sys.modules["Infernux"] = _infernux_stub
if "Infernux.engine" not in sys.modules:
    _engine_stub = types.ModuleType("Infernux.engine")
    _engine_stub.__path__ = [str(SOURCE_PYTHON / "Infernux" / "engine")]
    sys.modules["Infernux.engine"] = _engine_stub
    setattr(sys.modules["Infernux"], "engine", _engine_stub)

from Infernux.engine.player_package_audit import (
    BOOTSTRAP_NATIVE_ROOT_ALLOWLIST,
    RUNTIME_CONDITIONAL_NATIVE_FILES,
    RUNTIME_FORBIDDEN_LEGACY_NATIVE_FILES,
    RUNTIME_REQUIRED_NATIVE_FILES,
    audit_player_package,
)
import Infernux.engine.player_package_audit as player_package_audit
from Infernux.engine.player_package_native import read_entry, read_manifest, set_test_backend, write_pack
from Infernux.engine.player_service_graph import (
    RuntimeFeatureSet,
    RuntimeFlavor,
    player_runtime_contract_sections,
    runtime_service_graph_for,
)
from Infernux.engine.runtime_artifact_catalog import (
    RuntimeArtifactError,
    WINDOWS_FILETIME_EPOCH_OFFSET_TICKS,
    build_catalog,
    runtime_artifact_id,
    unix_ns_to_filetime_ticks,
    validate_artifact,
)


def _texture_artifact(source_hash: str) -> bytes:
    encoded = source_hash.encode("ascii")
    return b"INXTEXTURE" + b"\x04\x03\x02\x01" + len(encoded).to_bytes(4, "little") + encoded + b"payload"


def _asset_index_entry(project: Path, source: Path, guid: str, artifact: str) -> dict:
    stat = source.stat()
    return {
        "normalized_path": str(source.resolve()).replace("\\", "/"),
        "guid": guid,
        "source": {
            "size": stat.st_size,
            "modified_ns": unix_ns_to_filetime_ticks(stat.st_mtime_ns),
        },
        "content_hash": "a" * 16,
        "dependencies": [],
        "artifact_path": artifact,
        "metadata": {"metadata": {"resource_type": {"value": "Texture"}}},
    }


def test_runtime_catalog_ids_are_stable_and_output_is_deterministic():
    entries = [
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": "Library/Artifacts/Document/scene-b.scene",
            "bytes": 1,
            "sha256": "1" * 64,
            "payload": b"{\"name\":\"B\"}",
            "asset_binding": {
                "source_guid": "scene-b",
                "dependencies": [],
                "source_path": "Assets/B.scene",
            },
        },
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": "Library/Artifacts/Document/scene-a.scene",
            "bytes": 1,
            "sha256": "2" * 64,
            "payload": b"{\"name\":\"A\"}",
            "asset_binding": {
                "source_guid": "scene-a",
                "dependencies": [],
                "source_path": "Assets/A.scene",
            },
        },
    ]
    kwargs = {
        "player_host": {"executable": "Game.exe", "sha256": "3" * 64},
        "package_records": [],
    }
    first = build_catalog(entries, **kwargs)
    second = build_catalog(list(reversed(entries)), **kwargs)

    assert first == second
    assert len({item["runtime_artifact_id"] for item in first["artifacts"]}) == 2


def test_runtime_catalog_records_compiled_library_source_binding():
    binding = {
        "source_guid": "texture-guid",
        "source_path": "Assets/Smoke.png",
        "source_fingerprint": {"size": 12, "modified_ns": 34, "content_hash": "a" * 16},
        "artifact_source_hash": "a" * 16,
        "artifact_sha256": "b" * 64,
        "artifact_path": "Library/Artifacts/Texture/texture-guid.inxtex",
        "dependencies": [],
    }
    catalog = build_catalog(
        [
            {
                "package": "Game_Data/Content.inxpkg",
                "runtime_path": "Library/Artifacts/Texture/texture-guid.inxtex",
                "bytes": 7,
                "sha256": "b" * 64,
                "payload": _texture_artifact("a" * 16),
                "asset_binding": binding,
            }
        ],
        player_host={"executable": "Game.exe", "sha256": "c" * 64},
        package_records=[],
    )
    artifact = catalog["artifacts"][0]
    assert artifact["payload_kind"] == "compiled_artifact"
    assert artifact["source_asset"] == binding


def test_runtime_catalog_uses_mesh_as_deterministic_alias_for_mesh_and_skin():
    source_path = "Assets/Models/Character.glb"
    source_guid = "character-guid"
    mesh_path = "Library/Artifacts/Mesh/character-guid.inxmesh"
    skin_path = "Library/Artifacts/SkinnedMesh/character-guid.inxskin"
    scene_payload = json.dumps(
        {
            "mesh": {
                "$type": "asset_ref",
                "guid": "",
                "path_hint": source_path,
            }
        }
    ).encode("utf-8")
    entries = [
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": skin_path,
            "bytes": 7,
            "sha256": "1" * 64,
            "asset_binding": {
                "source_guid": source_guid,
                "source_path": source_path,
                "dependencies": [],
            },
        },
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": mesh_path,
            "bytes": 7,
            "sha256": "2" * 64,
            "asset_binding": {
                "source_guid": source_guid,
                "source_path": source_path,
                "dependencies": [],
            },
        },
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": "Library/Artifacts/Document/main.scene",
            "bytes": len(scene_payload),
            "sha256": "3" * 64,
            "payload": scene_payload,
            "asset_binding": {
                "source_guid": "scene-guid",
                "source_path": "Assets/Main.scene",
                "dependencies": [],
            },
        },
    ]
    kwargs = {
        "player_host": {"executable": "Game.exe", "sha256": "4" * 64},
        "package_records": [],
    }

    first = build_catalog(entries, **kwargs)
    second = build_catalog(list(reversed(entries)), **kwargs)

    assert first == second
    scene = next(
        artifact
        for artifact in first["artifacts"]
        if artifact["runtime_path"].endswith("main.scene")
    )
    assert scene["dependencies"] == [
        runtime_artifact_id("Game_Data/Content.inxpkg", mesh_path)
    ]
    assert scene["unresolved_dependencies"] == []


def test_runtime_catalog_rejects_source_alias_shared_by_different_guids():
    source_path = "Assets/Models/Ambiguous.glb"
    entries = [
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": "Library/Artifacts/Mesh/a.inxmesh",
            "bytes": 1,
            "sha256": "1" * 64,
            "asset_binding": {
                "source_guid": "guid-a",
                "source_path": source_path,
                "dependencies": [],
            },
        },
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": "Library/Artifacts/Mesh/b.inxmesh",
            "bytes": 1,
            "sha256": "2" * 64,
            "asset_binding": {
                "source_guid": "guid-b",
                "source_path": source_path,
                "dependencies": [],
            },
        },
    ]

    with pytest.raises(RuntimeArtifactError, match="runtime source alias is ambiguous"):
        build_catalog(
            entries,
            player_host={"executable": "Game.exe", "sha256": "3" * 64},
            package_records=[],
        )


def test_runtime_catalog_resolves_asset_guid_before_stale_path_hint():
    material_id = runtime_artifact_id(
        "Game_Data/Content.inxpkg",
        "Library/Artifacts/Document/material-guid.mat",
    )
    catalog = build_catalog(
        [
            {
                "package": "Game_Data/Content.inxpkg",
                "runtime_path": "Library/Artifacts/Document/scene-guid.scene",
                "bytes": 2,
                "sha256": "1" * 64,
                "payload": json.dumps(
                    {
                        "$type": "asset_ref",
                        "guid": "material-guid",
                        "path_hint": "Assets/Materials/Old.mat",
                    }
                ).encode("utf-8"),
                "asset_binding": {
                    "source_guid": "scene-guid",
                    "dependencies": [material_id],
                    "source_path": "Assets/Main.scene",
                },
            },
            {
                "package": "Game_Data/Content.inxpkg",
                "runtime_path": "Library/Artifacts/Document/material-guid.mat",
                "bytes": 2,
                "sha256": "2" * 64,
                "payload": b"{}",
                "asset_binding": {
                    "source_guid": "material-guid",
                    "dependencies": [],
                    "source_path": "Assets/Materials/Current.mat",
                },
            },
        ],
        player_host={"executable": "Game.exe", "sha256": "3" * 64},
        package_records=[],
    )

    scene = next(
        artifact
        for artifact in catalog["artifacts"]
        if artifact["logical_type"] == "scene_artifact"
    )
    assert scene["dependencies"] == [material_id]
    assert scene["unresolved_dependencies"] == []


def test_runtime_catalog_rejects_ambiguous_runtime_paths_across_packages():
    entries = [
        {
            "package": "Game_Data/Runtime.inxrt",
            "runtime_path": "Library/Shared.bin",
            "bytes": 1,
            "sha256": "1" * 64,
            "payload": b"a",
        },
        {
            "package": "Game_Data/Content.inxpkg",
            "runtime_path": "library/shared.bin",
            "bytes": 1,
            "sha256": "2" * 64,
            "payload": b"b",
        },
    ]

    with pytest.raises(RuntimeArtifactError, match="runtime artifact path is ambiguous"):
        build_catalog(
            entries,
            player_host={"executable": "Game.exe", "sha256": "3" * 64},
            package_records=[],
        )


def test_unix_ns_to_filetime_ticks_uses_windows_epoch_units():
    assert unix_ns_to_filetime_ticks(0) == WINDOWS_FILETIME_EPOCH_OFFSET_TICKS
    assert unix_ns_to_filetime_ticks(1_700_000_000_000_000_000) == 133444736000000000
    assert unix_ns_to_filetime_ticks(1_700_000_000_000_000_000) > 10**17


def test_library_artifact_validation_rejects_stale_source(tmp_path: Path):
    source = tmp_path / "Assets" / "Smoke.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    artifact_relative = "Library/Artifacts/Texture/texture-guid.inxtex"
    artifact = tmp_path / artifact_relative
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(_texture_artifact("a" * 16))
    entry = _asset_index_entry(tmp_path, source, "texture-guid", artifact_relative)
    validated = validate_artifact(tmp_path, entry, artifact)
    assert validated["source_guid"] == "texture-guid"
    assert validated["source_fingerprint"]["modified_ns"] == unix_ns_to_filetime_ticks(
        source.stat().st_mtime_ns
    )

    source.write_bytes(b"changed source")
    with pytest.raises(RuntimeArtifactError, match="fingerprint is stale"):
        validate_artifact(tmp_path, entry, artifact)


class _FakeNativeInxPack:
    """Small test double for the native binding; no production format logic."""

    manifests: dict[str, dict[str, object]] = {}
    entries: dict[tuple[str, str], bytes] = {}

    @classmethod
    def _key(cls, path) -> str:
        return str(Path(path).resolve())

    @classmethod
    def _write(
        cls,
        sources,
        destination,
        compression_level=None,
        profile="development",
    ):
        records = []
        raw_total = 0
        stored_total = 0
        for offset, (logical, source) in enumerate(sorted(sources)):
            logical = str(logical).replace("\\", "/")
            payload = Path(source).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            records.append(
                {
                    "path": logical,
                    "offset": offset * 64,
                    "stored_bytes": len(payload),
                    "raw_bytes": len(payload),
                    "codec": "store",
                    "sha256": digest,
                    "stored_sha256": digest,
                }
            )
            cls.entries[(cls._key(destination), logical)] = payload
            raw_total += len(payload)
            stored_total += len(payload)
        manifest = {
            "format": "infernux-native-inxpack",
            "revision": 65536,
            "codec": "zstd-or-store",
            "compression_profile": profile,
            "file_count": len(records),
            "raw_bytes": raw_total,
            "stored_bytes": stored_total,
            "payload_bytes": stored_total,
            "archive_bytes": 256 + len(records) * 128 + stored_total,
            "files": records,
        }
        encoded = json.dumps(manifest, sort_keys=True).encode("utf-8")
        Path(destination).write_bytes(b"FAKE-NATIVE-INXPKG\0" + encoded)
        manifest["archive_sha256"] = hashlib.sha256(Path(destination).read_bytes()).hexdigest()
        cls.manifests[cls._key(destination)] = manifest
        return manifest

    @classmethod
    def _read_manifest(cls, path):
        return cls.manifests[cls._key(path)]

    @classmethod
    def _read_entry(cls, path, entry_path):
        return cls.entries[(cls._key(path), str(entry_path).replace("\\", "/"))]

    @classmethod
    def _extract(cls, path, destination, allowed_roots=None):
        manifest = cls._read_manifest(path)
        roots = set(allowed_roots or [])
        for item in manifest["files"]:
            logical = item["path"]
            if roots and logical.split("/", 1)[0] not in roots:
                raise RuntimeError(f"unexpected root: {logical}")
            target = Path(destination) / Path(logical)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(cls._read_entry(path, logical))
        return manifest

    _inxpack_write = _write
    _inxpack_read_manifest = _read_manifest
    _inxpack_read_entry = _read_entry
    _inxpack_extract = _extract


@pytest.fixture(autouse=True)
def _native_backend():
    _FakeNativeInxPack.manifests.clear()
    _FakeNativeInxPack.entries.clear()
    set_test_backend(_FakeNativeInxPack)
    yield
    set_test_backend(None)


def _valid_player(tmp_path: Path) -> Path:
    root = tmp_path / "Balance"
    data = root / "Balance_Data"
    source = tmp_path / "source"
    data.mkdir(parents=True)
    source.mkdir()
    player_exe = bytearray(0x80 + 4 + 20 + 240)
    player_exe[0:2] = b"MZ"
    struct.pack_into("<I", player_exe, 0x3C, 0x80)
    player_exe[0x80:0x84] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH",
        player_exe,
        0x84,
        0x8664,
        1,
        0,
        0,
        0,
        240,
        0x0002,
    )
    struct.pack_into("<H", player_exe, 0x98, 0x020B)
    (root / "Balance.exe").write_bytes(player_exe)
    bootstrap_sources = []
    for name, payload in (
        ("python312.dll", b"python"),
        ("_ctypes.pyd", b"ctypes ABI"),
        ("ffi.dll", b"libffi ABI"),
        ("_InfernuxBootstrap.pyd", b"bootstrap"),
        ("_InfernuxPlayer.cp312-win_amd64.pyd", b"player module"),
        ("Infernux/lib/InfernuxFoundation.dll", b"foundation"),
        ("stdlib/encodings/__init__.pyc", b"encodings package"),
        ("stdlib/encodings/aliases.pyc", b"encoding aliases"),
        ("stdlib/encodings/utf_8.pyc", b"utf8 codec"),
    ):
        source_path = source / name.replace("/", "_")
        source_path.write_bytes(payload)
        bootstrap_sources.append((name, source_path))
    features = RuntimeFeatureSet()
    runtime_contract = player_runtime_contract_sections(
        RuntimeFlavor.PLAYER_RELEASE,
        features,
    )
    (data / "BuildManifest.json").write_text(
        json.dumps(
            {
                "debug_build": False,
                "runtime_contract": runtime_contract,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (data / "Library" / "RuntimeAssetCatalog.json").parent.mkdir(parents=True)
    (source / "runtime.bin").write_bytes(b"runtime payload")
    (source / "content.bin").write_bytes(b"content payload")
    runtime_sources = [("Infernux/resources/runtime.bin", source / "runtime.bin")]
    for index, relative in enumerate(sorted(RUNTIME_REQUIRED_NATIVE_FILES)):
        native_source = source / f"native-{index}.bin"
        native_source.write_bytes(
            b"foundation"
            if relative == "Infernux/lib/InfernuxFoundation.dll"
            else f"native-{relative}".encode("ascii")
        )
        runtime_sources.append((relative, native_source))
    for index, service in enumerate(
        runtime_service_graph_for(RuntimeFlavor.PLAYER_RELEASE, features).services
    ):
        if service.module.startswith("Modules/"):
            continue
        service_source = source / f"service-{index}.bin"
        service_source.write_bytes(f"service:{service.module}".encode("utf-8"))
        runtime_sources.append((service.module, service_source))
    type_registry = source / "runtime-type-registry.json"
    type_registry.write_text(
        json.dumps({"$schema": "infernux.runtime_type_registry", "types": []}),
        encoding="utf-8",
    )
    asset_records = source / "runtime-asset-records.json"
    asset_records.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_asset_records",
                "records_version": 2,
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    write_pack(bootstrap_sources, data / "Bootstrap.inxrt")
    write_pack(
        runtime_sources,
        data / "Runtime.inxrt",
    )
    write_pack(
        (
            ("RuntimeAssets/Balance.bin", source / "content.bin"),
            ("Library/RuntimeTypeRegistry.json", type_registry),
            ("Library/RuntimeAssetRecords.json", asset_records),
        ),
        data / "Content.inxpkg",
    )
    _write_catalog(root)
    return root


def _write_catalog(root: Path) -> None:
    data = root / "Balance_Data"
    package_entries = []
    package_records = []
    for package_name in ("Runtime.inxrt", "Content.inxpkg"):
        package_path = data / package_name
        package_manifest = read_manifest(package_path)
        package_relative = f"Balance_Data/{package_name}"
        package_records.append(
            {
                "path": package_relative,
                "archive_sha256": package_manifest["archive_sha256"],
                "archive_bytes": package_manifest["archive_bytes"],
                "file_count": package_manifest["file_count"],
                "raw_bytes": package_manifest["raw_bytes"],
                "stored_bytes": package_manifest["stored_bytes"],
                "codec": package_manifest["codec"],
            }
        )
        for entry in package_manifest["files"]:
            record = {
                "package": package_relative,
                "runtime_path": entry["path"],
                "bytes": entry["raw_bytes"],
                "sha256": entry["sha256"],
                "payload": read_entry(package_path, entry["path"]),
            }
            if str(entry["path"]).casefold().endswith(".wav"):
                record["asset_binding"] = {
                    "source_guid": "test-audio-guid",
                    "source_path": entry["path"],
                    "source_fingerprint": {
                        "size": entry["raw_bytes"],
                        "modified_ns": 1,
                    },
                    "dependencies": [],
                    "runtime_artifact_reason": (
                        "runtime_audio_backend_requires_encoded_stream"
                    ),
                }
            package_entries.append(record)
    catalog = build_catalog(
        package_entries,
        player_host={
            "executable": "Balance.exe",
            "sha256": hashlib.sha256((root / "Balance.exe").read_bytes()).hexdigest(),
            "identity": "nuitka-player-host",
        },
        package_records=package_records,
    )
    (data / "Library" / "RuntimeAssetCatalog.json").write_text(
        json.dumps(catalog, sort_keys=True), encoding="utf-8"
    )


def _append_package_entries(
    root: Path,
    package_name: str,
    additions: tuple[tuple[str, bytes], ...],
) -> None:
    data = root / "Balance_Data"
    package = data / package_name
    source_root = root.parent / f"{package_name}-rewrite"
    source_root.mkdir(exist_ok=True)
    entries = []
    for index, record in enumerate(read_manifest(package)["files"]):
        source = source_root / f"existing-{index}.bin"
        source.write_bytes(read_entry(package, record["path"]))
        entries.append((record["path"], source))
    for index, (runtime_path, payload) in enumerate(additions):
        extra = source_root / f"extra-{index}.bin"
        extra.write_bytes(payload)
        entries.append((runtime_path, extra))
    write_pack(entries, package)
    _write_catalog(root)


def _append_content_entry(root: Path, runtime_path: str, payload: bytes) -> None:
    _append_package_entries(root, "Content.inxpkg", ((runtime_path, payload),))


def test_audit_writes_current_player_manifest(tmp_path: Path):
    root = _valid_player(tmp_path)

    manifest = audit_player_package(root)

    assert manifest["$schema"] == "infernux.player_runtime_manifest"
    assert manifest["product"]["single_entry_point"] is True
    declared_services = manifest["services"]["declared"]
    assert declared_services == list(
        runtime_service_graph_for(RuntimeFlavor.PLAYER_RELEASE).service_ids
    )
    assert "scene_file_manager" not in declared_services
    assert "play_mode_manager" not in declared_services


def test_audit_rejects_runtime_contract_service_graph_drift(tmp_path: Path):
    root = _valid_player(tmp_path)
    build_manifest_path = root / "Balance_Data" / "BuildManifest.json"
    build_manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
    build_manifest["runtime_contract"]["services"]["graph"][0]["module"] = (
        "Infernux/engine/undo/_manager.pyc"
    )
    build_manifest_path.write_text(
        json.dumps(build_manifest, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="authoritative service graph"):
        audit_player_package(root)


def test_audit_rejects_unknown_project_settings_document(tmp_path: Path):
    root = _valid_player(tmp_path)
    _append_content_entry(
        root,
        "ProjectSettings/FutureEditorService.json",
        b"{}",
    )

    with pytest.raises(RuntimeError, match="unknown authoring document path"):
        audit_player_package(root)


def test_audit_rejects_unowned_parallel_module(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "parallel.bin"
    source.write_bytes(b"undeclared parallel module")
    module = root / "Balance_Data" / "Modules" / "Parallel.inxmod"
    module.parent.mkdir(parents=True)
    write_pack((("numba/runtime.bin", source),), module)

    with pytest.raises(RuntimeError, match="Parallel.inxmod presence disagrees"):
        audit_player_package(root)


def test_audit_manifest_replace_failure_preserves_previous_evidence(
    tmp_path: Path,
    monkeypatch,
):
    root = _valid_player(tmp_path)
    manifest_path = root / "Balance_Data" / "Player.inxmanifest"
    manifest_path.write_text("previous evidence\n", encoding="utf-8")
    native_replace = player_package_audit.os.replace

    def reject_manifest_replace(source, destination):
        if Path(destination) == manifest_path:
            raise PermissionError("read-only audit destination")
        return native_replace(source, destination)

    monkeypatch.setattr(player_package_audit.os, "replace", reject_manifest_replace)

    with pytest.raises(PermissionError, match="read-only"):
        audit_player_package(root, write_manifest=True)

    assert manifest_path.read_text(encoding="utf-8") == "previous evidence\n"
    assert not list(manifest_path.parent.glob(".Player.inxmanifest.*.tmp"))


def test_audit_supports_unicode_product_paths(tmp_path: Path):
    unicode_root = tmp_path / "发布" / "最终游戏"
    unicode_root.parent.mkdir()
    root = _valid_player(unicode_root)

    manifest = audit_player_package(root, write_manifest=True)

    assert manifest["audit"]["passed"] is True


def test_audit_does_not_read_binary_entries_individually(tmp_path: Path, monkeypatch):
    root = _valid_player(tmp_path)
    data = root / "Balance_Data"
    binary_sources = []
    for index in range(128):
        source = tmp_path / f"content-binary-{index}.bin"
        source.write_bytes(f"binary-{index}".encode("ascii"))
        binary_sources.append((f"RuntimeAssets/binary/{index:03d}.bin", source))
    _append_package_entries(
        root,
        "Content.inxpkg",
        tuple((runtime_path, source.read_bytes()) for runtime_path, source in binary_sources),
    )

    calls = []
    original_read_entry = player_package_audit.read_entry

    def tracked_read_entry(path, entry_path):
        calls.append((Path(path).name, str(entry_path)))
        return original_read_entry(path, entry_path)

    monkeypatch.setattr(player_package_audit, "read_entry", tracked_read_entry)

    manifest = audit_player_package(root, write_manifest=False)

    assert manifest["audit"]["passed"] is True
    assert calls
    assert all(not entry.startswith("RuntimeAssets/binary/") for _, entry in calls)


def test_audit_still_extracts_text_entries_for_absolute_path_checks(
    tmp_path: Path, monkeypatch
):
    root = _valid_player(tmp_path)
    data = root / "Balance_Data"
    source = tmp_path / "runtime-metadata.json"
    source.write_text('{"source": "C:/external/project/asset"}', encoding="utf-8")
    write_pack(
        (("RuntimeAssets/runtime-metadata.json", source),),
        data / "Content.inxpkg",
    )
    _write_catalog(root)

    calls = []
    original_extract_pack = player_package_audit.extract_pack

    def tracked_extract_pack(path, output_path):
        calls.append(Path(path).name)
        return original_extract_pack(path, output_path)

    monkeypatch.setattr(player_package_audit, "extract_pack", tracked_extract_pack)

    with pytest.raises(RuntimeError, match="absolute_author_paths"):
        audit_player_package(root, write_manifest=False)

    assert "Content.inxpkg" in calls


def test_audit_accepts_minimal_bootstrap_native_closure(tmp_path: Path):
    root = _valid_player(tmp_path)

    manifest = audit_player_package(root, write_manifest=False)

    assert manifest["bootstrap_surface"]["allowed"]
    assert set(BOOTSTRAP_NATIVE_ROOT_ALLOWLIST) == set()
    assert manifest["audit"]["bootstrap_payload_gaps"] == []
    assert manifest["runtime_native_surface"]["gaps"] == []
    assert "Infernux/lib/zlib.dll" not in manifest["runtime_native_surface"]["required"]
    assert not (
        RUNTIME_FORBIDDEN_LEGACY_NATIVE_FILES
        & set(manifest["runtime_native_surface"]["required"])
    )
    assert manifest["audit"]["duplicate_payload_groups"] == []
    catalog = json.loads(
        (
            root / "Balance_Data" / "Library" / "RuntimeAssetCatalog.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["path"] for item in catalog["packages"]} == {
        "Balance_Data/Runtime.inxrt",
        "Balance_Data/Content.inxpkg",
    }
    assert all(
        item["package"] != "Balance_Data/Bootstrap.inxrt"
        for item in catalog["artifacts"]
    )


@pytest.mark.parametrize(
    "removed_name",
    ("_bz2.pyd", "_lzma.pyd", "libbz2.dll", "liblzma.dll"),
)
def test_audit_rejects_removed_bootstrap_dependencies(tmp_path: Path, removed_name: str):
    root = _valid_player(tmp_path)
    (root / removed_name).write_bytes(b"removed bootstrap dependency")

    with pytest.raises(RuntimeError, match="bootstrap_surface_gaps"):
        audit_player_package(root, write_manifest=False)


@pytest.mark.parametrize("required_name", ("_ctypes.pyd", "ffi.dll"))
def test_audit_requires_ctypes_abi_startup_closure(tmp_path: Path, required_name: str):
    root = _valid_player(tmp_path)
    bootstrap = root / "Balance_Data" / "Bootstrap.inxrt"
    source_files = []
    for entry in read_manifest(bootstrap)["files"]:
        if entry["path"] == required_name:
            continue
        source = tmp_path / ("bootstrap-" + entry["path"].replace("/", "_"))
        source.write_bytes(read_entry(bootstrap, entry["path"]))
        source_files.append((entry["path"], source))
    write_pack(source_files, bootstrap)
    _write_catalog(root)

    with pytest.raises(RuntimeError, match="bootstrap archive file"):
        audit_player_package(root, write_manifest=False)


def test_audit_tracks_zlib_only_when_present_in_runtime_closure(tmp_path: Path):
    root = _valid_player(tmp_path)
    zlib_source = tmp_path / "zlib.dll"
    zlib_source.write_bytes(b"runtime zlib")
    _append_package_entries(
        root,
        "Runtime.inxrt",
        (("Infernux/lib/zlib.dll", zlib_source.read_bytes()),),
    )

    manifest = audit_player_package(root, write_manifest=False)

    assert RUNTIME_CONDITIONAL_NATIVE_FILES == frozenset({"Infernux/lib/zlib.dll"})
    assert "Infernux/lib/zlib.dll" in manifest["runtime_native_surface"]["required"]
    assert manifest["runtime_native_surface"]["gaps"] == []


def test_audit_rejects_legacy_dynamic_shader_compiler_libraries(tmp_path: Path):
    root = _valid_player(tmp_path)
    data = root / "Balance_Data"
    runtime_sources = []
    for index, relative in enumerate(sorted(RUNTIME_REQUIRED_NATIVE_FILES)):
        native_source = tmp_path / f"required-static-shader-case-{index}.bin"
        native_source.write_bytes(relative.encode("ascii"))
        runtime_sources.append((relative, native_source))
    for index, relative in enumerate(sorted(RUNTIME_FORBIDDEN_LEGACY_NATIVE_FILES)):
        legacy_source = tmp_path / f"legacy-shader-dll-{index}.bin"
        legacy_source.write_bytes(relative.encode("ascii"))
        runtime_sources.append((relative, legacy_source))
    write_pack(runtime_sources, data / "Runtime.inxrt")
    _write_catalog(root)

    with pytest.raises(RuntimeError, match="legacy shader compiler DLL"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_full_engine_bridge_at_player_root(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "_Infernux.pyd").write_bytes(b"legacy full bridge")

    with pytest.raises(RuntimeError, match="bootstrap_surface_gaps"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_data_root_that_does_not_match_executable(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance_Data").rename(root / "Other_Data")

    with pytest.raises(RuntimeError, match="data directory must be named"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_unknown_root_surface(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "unexpected-cache").mkdir()

    with pytest.raises(RuntimeError, match="bootstrap_surface_gaps"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_editor_i18n_in_loose_payload(tmp_path: Path):
    root = _valid_player(tmp_path)
    locale = root / "Infernux" / "engine" / "locales"
    locale.mkdir(parents=True)
    (locale / "zh.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="editor_i18n_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_meta_and_author_source(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance_Data" / "Assets").mkdir()
    (root / "Balance_Data" / "Assets" / "Player.py").write_text("pass", encoding="utf-8")
    (root / "Balance_Data" / "Assets" / "Player.py.meta").write_text("meta", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Player package audit failed"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_missing_runtime_catalog(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance_Data" / "Library" / "RuntimeAssetCatalog.json").unlink()

    with pytest.raises(RuntimeError, match="library_artifact_gap"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_tampered_runtime_catalog(tmp_path: Path):
    root = _valid_player(tmp_path)
    catalog_path = root / "Balance_Data" / "Library" / "RuntimeAssetCatalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["artifacts"][0]["content_sha256"] = "0" * 64
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(RuntimeError, match="library_artifact_gap"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_tampered_runtime_catalog_package_summary(tmp_path: Path):
    root = _valid_player(tmp_path)
    catalog_path = root / "Balance_Data" / "Library" / "RuntimeAssetCatalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["packages"][0]["archive_sha256"] = "0" * 64
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(RuntimeError, match="library_artifact_gap"):
        audit_player_package(root, write_manifest=False)


def test_catalog_rejects_direct_runtime_asset_before_audit(
    tmp_path: Path,
):
    root = _valid_player(tmp_path)
    source = tmp_path / "sound.wav"
    source.write_bytes(b"direct audio payload")
    balance = tmp_path / "balance.bin"
    balance.write_bytes(b"balance payload")
    write_pack(
        (
            ("RuntimeAssets/Balance.bin", balance),
            ("Assets/Audio/sound.wav", source),
        ),
        root / "Balance_Data" / "Content.inxpkg",
    )
    with pytest.raises(
        RuntimeArtifactError,
        match="direct or serialized runtime payload",
    ):
        _write_catalog(root)


def test_audit_rejects_duplicate_native_payload(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "_Infernux.pyd").write_bytes(b"same native")
    (root / "Balance_Data" / "engine-copy.dll").write_bytes(b"same native")

    with pytest.raises(RuntimeError, match="duplicate_native_payloads"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_legacy_zip(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance_Data" / "legacy.zip").write_bytes(b"PK\x03\x04legacy")

    with pytest.raises(RuntimeError, match="legacy_zip_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_legacy_inxpack(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance_Data" / "legacy.inxpack").write_bytes(b"old")

    with pytest.raises(RuntimeError, match="legacy_inxpack_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_legacy_data_directory(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Data").mkdir()
    (root / "Balance_Data").rename(root / "Data" / "nested")

    with pytest.raises(RuntimeError, match="legacy Data"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_unknown_file_in_player_data(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance_Data" / "unexpected.bin").write_bytes(b"unexpected")

    with pytest.raises(RuntimeError, match="data_surface_gaps"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_unverified_player_host(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "Balance.exe").write_bytes(b"placeholder executable")

    with pytest.raises(RuntimeError, match="player_host_gap"):
        audit_player_package(root, write_manifest=False)


def test_audit_accepts_structurally_valid_player_host(tmp_path: Path):
    root = _valid_player(tmp_path)

    manifest = audit_player_package(root, write_manifest=False)

    assert manifest["audit"]["passed"] is True


def test_audit_allows_only_controlled_runtime_builtin_shaders(tmp_path: Path):
    root = _valid_player(tmp_path)
    sources = {}
    for suffix in ("glsl", "vert", "frag", "shadingmodel"):
        source = tmp_path / f"builtin.{suffix}.bin"
        source.write_bytes(f"builtin {suffix} payload".encode("ascii"))
        sources[suffix] = source
    _append_package_entries(
        root,
        "Runtime.inxrt",
        (
            ("Infernux/resources/shaders/builtin.glsl", sources["glsl"].read_bytes()),
            ("Infernux/resources/shaders/builtin.vert", sources["vert"].read_bytes()),
            ("Infernux/resources/shaders/builtin.frag", sources["frag"].read_bytes()),
            (
                "Infernux/resources/shaders/builtin.shadingmodel",
                sources["shadingmodel"].read_bytes(),
            ),
        ),
    )

    manifest = audit_player_package(root, write_manifest=False)

    assert manifest["audit"]["passed"] is True


def test_audit_still_rejects_project_content_shader_source(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "project-shader.vert"
    source.write_bytes(b"project shader payload")
    write_pack(
        (("Assets/Shaders/project.vert", source),),
        root / "Balance_Data" / "Content.inxpkg",
    )

    with pytest.raises(RuntimeError, match="author_source_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_uncontrolled_runtime_shader_path(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "uncontrolled.vert"
    source.write_bytes(b"uncontrolled shader payload")
    write_pack(
        (("Infernux/resources/not-shaders/uncontrolled.vert", source),),
        root / "Balance_Data" / "Runtime.inxrt",
    )

    with pytest.raises(RuntimeError, match="author_source_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_multiple_player_entry_points(tmp_path: Path):
    root = _valid_player(tmp_path)
    (root / "LegacyPlayer.exe").write_bytes(b"legacy entry")

    with pytest.raises(RuntimeError, match="legacy_dual_entry_point"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_hidden_executable_in_native_toc(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "hidden.exe"
    source.write_bytes(b"hidden")
    write_pack(
        (("RuntimeAssets/hidden.exe", source),),
        root / "Balance_Data" / "Content.inxpkg",
    )

    with pytest.raises(RuntimeError, match="hidden_executables"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_source_and_meta_in_native_toc(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "authoring.bin"
    source.write_bytes(b"authoring")
    write_pack(
        (
            ("RuntimeAssets/source.py", source),
            ("RuntimeAssets/source.meta", source),
        ),
        root / "Balance_Data" / "Content.inxpkg",
    )

    with pytest.raises(RuntimeError, match="author_source_files"):
        audit_player_package(root, write_manifest=False)


def test_audit_allows_compiled_script_inside_content_package(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "compiled.pyc"
    source.write_bytes(b"compiled bytecode")
    _append_content_entry(root, "Assets/Scripts/Player.pyc", source.read_bytes())

    manifest = audit_player_package(root, write_manifest=False)

    assert manifest["audit"]["passed"] is True


def test_audit_rejects_duplicate_payloads_inside_native_container(tmp_path: Path):
    root = _valid_player(tmp_path)
    source = tmp_path / "duplicate.bin"
    source.write_bytes(b"same payload")
    write_pack(
        (("RuntimeAssets/a.bin", source), ("RuntimeAssets/b.bin", source)),
        root / "Balance_Data" / "Content.inxpkg",
    )

    with pytest.raises(RuntimeError, match="duplicate_payload_groups"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_corrupted_binary_package_via_manifest_validation(
    tmp_path: Path, monkeypatch
):
    # This test can be collected beside GameBuilder tests, which install a
    # different fake backend through their module-level fixture.  Pin the
    # backend for this package contract so the test exercises this module's
    # archive and entry registry deterministically.
    set_test_backend(_FakeNativeInxPack)
    root = _valid_player(tmp_path)
    archive = root / "Balance_Data" / "Content.inxpkg"
    archive_key = _FakeNativeInxPack._key(archive)
    _FakeNativeInxPack.entries[(archive_key, "RuntimeAssets/Balance.bin")] += b"corrupted"
    original_read_manifest = player_package_audit.read_manifest

    def reject_corrupted_manifest(path):
        if Path(path).resolve() == archive.resolve():
            raise RuntimeError("payload hash mismatch")
        return original_read_manifest(path)

    monkeypatch.setattr(player_package_audit, "read_manifest", reject_corrupted_manifest)

    with pytest.raises(RuntimeError, match="native InxPack validation failed"):
        audit_player_package(root, write_manifest=False)


def test_audit_rejects_unsafe_native_entry_path(tmp_path: Path):
    set_test_backend(_FakeNativeInxPack)
    root = _valid_player(tmp_path)
    _append_content_entry(root, "RuntimeAssets/unsafe-path-probe.bin", b"probe")
    archive = root / "Balance_Data" / "Content.inxpkg"
    archive_key = _FakeNativeInxPack._key(archive)
    manifest = _FakeNativeInxPack.manifests[archive_key]
    original_path = "RuntimeAssets/unsafe-path-probe.bin"
    record = next(item for item in manifest["files"] if item["path"] == original_path)
    payload = _FakeNativeInxPack.entries.pop((archive_key, original_path))
    record["path"] = "../escape.bin"
    _FakeNativeInxPack.entries[(archive_key, "../escape.bin")] = payload

    with pytest.raises(RuntimeError, match="unsafe_entry_paths"):
        audit_player_package(root, write_manifest=False)
