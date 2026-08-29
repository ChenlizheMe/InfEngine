from __future__ import annotations

import importlib.util
import importlib.machinery
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from Infernux.engine.build_cancellation import BuildCancelled
from Infernux.engine import game_builder as game_builder_module
from Infernux.engine.game_builder import BuildOutputDirectoryError, GameBuilder
from Infernux.engine.runtime_artifact_catalog import (
    RuntimeArtifactError,
    build_catalog,
    logical_type_for_path,
    payload_kind_for,
    runtime_artifact_reason_for,
    source_fingerprint,
    unix_ns_to_filetime_ticks,
    validate_artifact,
)
from Infernux.engine import nuitka_builder as nuitka_builder_module
from Infernux.engine import player_package_audit as player_package_audit_module
from Infernux.engine.nuitka_builder import NuitkaBuilder
from Infernux.engine.player_package_native import (
    extract_pack,
    read_entry,
    read_manifest,
    set_test_backend,
    write_pack,
)
from Infernux.engine.player_service_graph import forbidden_player_service_modules
from Infernux.particle.asset import ParticleGraphAsset
from Infernux.plugins.registry import PluginRegistry


@pytest.mark.parametrize(
    "text",
    (
        '"$schema": "https://json-schema.org/draft/2020-12/schema"',
        '"$id": "https://infernux-engine.com/schemas/runtime.json"',
        'documentation = "custom+https://example.invalid/runtime"',
    ),
)
def test_player_audit_does_not_treat_uri_schemes_as_windows_paths(text):
    assert not player_package_audit_module._contains_absolute_author_path(text)


@pytest.mark.parametrize(
    "text",
    (
        'source = "C:/Users/Author/Project/Assets/Main.scene"',
        r'source = "D:\\Workspace\\Project\\Assets\\Main.scene"',
        'source = "/home/author/project/Assets/Main.scene"',
        'source = "/Users/author/project/Assets/Main.scene"',
    ),
)
def test_player_audit_still_rejects_absolute_author_paths(text):
    assert player_package_audit_module._contains_absolute_author_path(text)


def test_player_audit_allows_equal_compiled_assets_with_distinct_runtime_paths():
    data_root = "Game_Data"
    distinct_assets = [
        "Game_Data/Content.inxpkg::Library/Artifacts/SkinnedMesh/first.inxskin",
        "Game_Data/Content.inxpkg::Library/Artifacts/SkinnedMesh/second.inxskin",
    ]

    assert player_package_audit_module._is_logically_distinct_asset_payload(
        distinct_assets,
        data_root,
    )
    assert not player_package_audit_module._is_logically_distinct_asset_payload(
        [
            *distinct_assets,
            "Game_Data/Runtime.inxrt::Infernux/lib/InfernuxRendererRuntime.dll",
        ],
        data_root,
    )


class _FakeNativeInxPack:
    """Contract-only backend; production packing remains native C++."""

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
        archive_key = cls._key(destination)
        cls.entries = {
            key: value for key, value in cls.entries.items() if key[0] != archive_key
        }
        records = []
        raw_total = 0
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
            cls.entries[(archive_key, logical)] = payload
            raw_total += len(payload)
        manifest = {
            "format": "infernux-native-inxpack",
            "revision": 65536,
            "codec": "store",
            "compression_profile": profile,
            "file_count": len(records),
            "raw_bytes": raw_total,
            "stored_bytes": raw_total,
            "payload_bytes": raw_total,
            "archive_bytes": 256 + len(records) * 128 + raw_total,
            "files": records,
        }
        encoded = json.dumps(manifest, sort_keys=True).encode("utf-8")
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(b"FAKE-NATIVE-INXPKG\0" + encoded)
        manifest["archive_bytes"] = destination_path.stat().st_size
        manifest["archive_sha256"] = hashlib.sha256(destination_path.read_bytes()).hexdigest()
        cls.manifests[archive_key] = manifest
        return manifest

    @staticmethod
    def _validate_logical_path(logical: str) -> str:
        normalized = str(logical).replace("\\", "/")
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or ":" in parts[0]
        ):
            raise RuntimeError(f"unsafe native package path: {logical}")
        return normalized

    @classmethod
    def _read_manifest(cls, path):
        archive_key = cls._key(path)
        try:
            return cls.manifests[archive_key]
        except KeyError:
            try:
                archive_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError as exc:
                raise RuntimeError(
                    f"fake native package is unavailable: {path}"
                ) from exc
            source_key = next(
                (
                    key
                    for key, manifest in cls.manifests.items()
                    if manifest.get("archive_sha256") == archive_hash
                ),
                None,
            )
            if source_key is None:
                raise RuntimeError(f"fake native package is unavailable: {path}")
            cls.manifests[archive_key] = cls.manifests[source_key]
            for (entry_archive, logical), payload in list(cls.entries.items()):
                if entry_archive == source_key:
                    cls.entries[(archive_key, logical)] = payload
            return cls.manifests[archive_key]

    @classmethod
    def _read_entry(cls, path, entry_path):
        cls._read_manifest(path)
        return cls.entries[(cls._key(path), str(entry_path).replace("\\", "/"))]

    @classmethod
    def _extract(cls, path, destination, allowed_roots=None):
        manifest = cls._read_manifest(path)
        roots = set(allowed_roots or [])
        for item in manifest["files"]:
            logical = cls._validate_logical_path(str(item["path"]))
            if roots and logical.split("/", 1)[0] not in roots:
                raise RuntimeError(f"unexpected native package root: {logical}")
            target = Path(destination) / Path(logical)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(cls._read_entry(path, logical))
        return manifest

    _inxpack_write = _write
    _inxpack_read_manifest = _read_manifest
    _inxpack_read_entry = _read_entry
    _inxpack_extract = _extract


@pytest.fixture(autouse=True)
def _native_package_backend():
    _FakeNativeInxPack.manifests.clear()
    _FakeNativeInxPack.entries.clear()
    set_test_backend(_FakeNativeInxPack)
    yield
    set_test_backend(None)


def _make_project(tmp_path):
    project_root = tmp_path / "project"
    settings_dir = project_root / "ProjectSettings"
    settings_dir.mkdir(parents=True)
    scene_path = project_root / "Assets" / "Main.scene"
    scene_path.parent.mkdir(parents=True)
    scene_path.write_text(
        json.dumps({"objects": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (settings_dir / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_asset_index(
        project_root,
        [_asset_index_entry(project_root, scene_path, "scene-guid", "", "Scene")],
    )
    return project_root


def _make_builder(tmp_path, output_dir):
    project_root = _make_project(tmp_path)
    return GameBuilder(str(project_root), str(output_dir), game_name="TestGame")


def _player_executable_name(game_name: str = "TestGame") -> str:
    return f"{game_name}.exe" if sys.platform == "win32" else game_name


def _write_player_executable(root: Path, payload: bytes = b"Infernux Player") -> Path:
    executable = root / _player_executable_name()
    executable.write_bytes(payload)
    if sys.platform != "win32":
        executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def _bind_staged_script_to_asset_index(
    builder: GameBuilder,
    output_dir: Path,
    staged_script: Path,
    *,
    guid: str,
) -> None:
    runtime_relative = staged_script.relative_to(output_dir / "Data")
    project_source = Path(builder.project_path) / runtime_relative
    project_source.parent.mkdir(parents=True, exist_ok=True)
    project_source.write_bytes(staged_script.read_bytes())
    entries = dict(getattr(builder, "_cooked_asset_entries", {}))
    entries[guid] = {
        "guid": guid,
        "normalized_path": project_source.resolve().as_posix(),
    }
    builder._cooked_asset_entries = entries


def _prepare_runtime_catalog_inputs(
    builder: GameBuilder,
    final_dir: Path,
    *,
    include_runtime: bool = True,
    include_content: bool = True,
    include_executable: bool = True,
) -> Path:
    data_root = final_dir / f"{builder.project_name}_Data"
    data_root.mkdir(parents=True, exist_ok=True)
    source_root = final_dir.parent / f"{final_dir.name}-catalog-sources"
    source_root.mkdir(parents=True, exist_ok=True)
    if include_runtime:
        runtime_source = source_root / "runtime.bin"
        runtime_source.write_bytes(b"runtime")
        write_pack(
            (("Infernux/resources/runtime.bin", runtime_source),),
            data_root / builder._RUNTIME_ARCHIVE_FILENAME,
        )
    if include_content:
        content_source = source_root / "content.bin"
        content_source.write_bytes(b"content")
        write_pack(
            (("RuntimeAssets/content.bin", content_source),),
            data_root / builder._CONTENT_ARCHIVE_FILENAME,
        )
    if include_executable:
        executable = final_dir / _player_executable_name(builder.project_name)
        executable.write_bytes(b"Infernux Player")
        if sys.platform != "win32":
            executable.chmod(executable.stat().st_mode | 0o111)
    return data_root


def _install_runtime_identity_bindings(
    builder: GameBuilder,
    records: dict[str, tuple[str, str]],
) -> None:
    builder._runtime_asset_identity_bindings = {
        runtime_path: {
            "source_guid": guid,
            "source_path": runtime_path,
            "source_fingerprint": {
                "size": 1,
                "modified_ns": 1,
                "content_hash": "a" * 16,
            },
            "dependencies": [],
            "runtime_artifact_reason": reason,
        }
        for runtime_path, (guid, reason) in records.items()
    }


def _write_asset_script(project_root, relative_path: str, source: str) -> None:
    script_path = project_root / "Assets" / relative_path
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(source, encoding="utf-8")


def test_runtime_catalog_rejects_serialized_and_direct_payloads():
    common = {
        "package": "Content.inxpkg",
        "runtime_path": "Assets/Main.scene",
        "bytes": 2,
        "sha256": hashlib.sha256(b"{}").hexdigest(),
        "payload": b"{}",
    }

    with pytest.raises(RuntimeArtifactError, match="direct or serialized runtime payload"):
        build_catalog(
            [common],
            player_host={"executable": "Game.exe", "sha256": "a" * 64},
            package_records=[],
        )

    with pytest.raises(RuntimeArtifactError, match="direct or serialized runtime payload"):
        build_catalog(
            [
                common
                | {
                    "asset_binding": {
                        "source_guid": "scene-guid",
                        "dependencies": [],
                        "runtime_artifact_reason": (
                            "runtime_loader_requires_serialized_document"
                        ),
                    }
                }
            ],
            player_host={"executable": "Game.exe", "sha256": "a" * 64},
            package_records=[],
        )


RUNTIME_DOCUMENT_AND_AUDIO_SUFFIXES = (
    ".scene",
    ".prefab",
    ".mat",
    ".effect",
    ".effectgroup",
    ".timeline",
    ".timelinefsm",
    ".animclip",
    ".animclip2d",
    ".animclip3d",
    ".animfsm",
    ".animtimeline",
    ".graph",
    ".particlegraph",
    ".json",
    ".yaml",
    ".yml",
    ".bin",
    ".wav",
    ".ogg",
    ".mp3",
    ".flac",
    ".aiff",
    ".aif",
)


def test_player_audit_runtime_document_suffixes_are_complete():
    expected = {
        ".scene",
        ".prefab",
        ".mat",
        ".effect",
        ".effectgroup",
        ".timeline",
        ".timelinefsm",
        ".animclip",
        ".animclip2d",
        ".animclip3d",
        ".animfsm",
        ".animtimeline",
        ".graph",
    }
    assert expected <= player_package_audit_module.RUNTIME_DOCUMENT_SUFFIXES


@pytest.mark.parametrize("suffix", RUNTIME_DOCUMENT_AND_AUDIO_SUFFIXES)
def test_all_runtime_document_and_audio_sources_are_library_only(suffix):
    source_path = f"Assets/Runtime/Asset{suffix}"
    source_type = logical_type_for_path(source_path)
    assert payload_kind_for(source_type) in {
        "serialized_runtime_document",
        "direct_runtime_asset",
    }
    assert runtime_artifact_reason_for(source_type) is not None

    with pytest.raises(
        RuntimeArtifactError,
        match="direct or serialized runtime payload",
    ):
        build_catalog(
            [
                {
                    "package": "Content.inxpkg",
                    "runtime_path": source_path,
                    "bytes": 2,
                    "sha256": hashlib.sha256(b"{}").hexdigest(),
                    "payload": b"{}",
                    "asset_binding": {
                        "source_guid": f"guid-{suffix[1:]}",
                        "dependencies": [],
                        "runtime_artifact_reason": (
                            "runtime_loader_requires_serialized_document"
                        ),
                    },
                }
            ],
            player_host={"executable": "Game.exe", "sha256": "a" * 64},
            package_records=[],
        )


@pytest.mark.parametrize("suffix", RUNTIME_DOCUMENT_AND_AUDIO_SUFFIXES)
def test_all_runtime_document_and_audio_library_paths_are_compiled_artifacts(suffix):
    directory = "Audio" if suffix in {".wav", ".ogg", ".mp3", ".flac", ".aiff", ".aif"} else "Document"
    artifact_type = logical_type_for_path(
        f"Library/Artifacts/{directory}/asset-guid{suffix}"
    )
    assert payload_kind_for(artifact_type) == "compiled_artifact"


def _reference_particle_graph(project_root: Path, stable_id: str) -> Path:
    graph_path = project_root / "Assets" / "VFX" / f"{stable_id}.particlegraph"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = ParticleGraphAsset(stable_id=stable_id, name=stable_id)
    graph_path.write_text(graph.canonical_json(), encoding="utf-8")
    guid = hashlib.md5(stable_id.encode("utf-8")).hexdigest()
    scene_path = project_root / "Assets" / "Main.scene"
    scene_path.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "components": [
                            {
                                "data": {
                                    "graph": {
                                        "$type": "asset_ref",
                                        "asset_type": "ParticleGraph",
                                        "guid": guid,
                                        "path_hint": f"Assets/VFX/{stable_id}.particlegraph",
                                    }
                                }
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project_root,
        [
            _asset_index_entry(project_root, scene_path, "scene-guid", "", "Scene"),
            _asset_index_entry(project_root, graph_path, guid, "", "ParticleGraph"),
        ],
    )
    runtime_index = project_root / "Library" / "Artifacts" / "Particle" / "RuntimeIndex.json"
    runtime_index.parent.mkdir(parents=True, exist_ok=True)
    runtime_index.write_text(
        json.dumps(
            {
                "$schema": "infernux.particle_runtime_index",
                "entries": [
                    {
                        "guid": guid,
                        "path_hint": f"Assets/VFX/{stable_id}.particlegraph",
                        "stable_id": stable_id,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return graph_path


def _asset_index_entry(
    project_root: Path,
    source: Path,
    guid: str,
    artifact_path: str,
    resource_type: str,
    content_hash: str | None = None,
) -> dict:
    stat = source.stat()
    normalized_path = str(source.resolve()).replace("\\", "/")
    if sys.platform == "win32":
        normalized_path = normalized_path.casefold()
    return {
        "normalized_path": normalized_path,
        "guid": guid,
        "resource_type": 3,
        "source": {
            "size": stat.st_size,
            "modified_ns": unix_ns_to_filetime_ticks(stat.st_mtime_ns),
        },
        "meta": {"size": 0, "modified_ns": 0},
        "content_hash": content_hash or hashlib.sha256(source.read_bytes()).hexdigest()[:16],
        "dependencies": [],
        "read_only": False,
        "import_succeeded": True,
        "import_error": "",
        "artifact_path": artifact_path,
        "metadata": {
            "metadata": {
                "guid": {"value": guid},
                "resource_type": {"value": resource_type},
            }
        },
    }


def _write_asset_index(project_root: Path, entries: list[dict]) -> None:
    index = {
        "project_root": str(project_root.resolve()).replace("\\", "/").casefold(),
        "entries": entries,
    }
    index_path = project_root / "Library" / "AssetIndex.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index), encoding="utf-8")


def _fnv1a64(payload: bytes) -> str:
    value = 14695981039346656037
    for byte in payload:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def test_source_fingerprint_accepts_cross_platform_timestamp_when_content_matches(tmp_path):
    project = tmp_path / "Project"
    source = project / "Assets" / "portable.bin"
    source.parent.mkdir(parents=True)
    payload = b"portable asset payload\n"
    source.write_bytes(payload)
    entry = _asset_index_entry(
        project,
        source,
        "portable-guid",
        "",
        "Binary",
        content_hash=_fnv1a64(payload),
    )
    entry["source"]["modified_ns"] = -987654321

    fingerprint = source_fingerprint(project, entry)

    assert fingerprint["size"] == len(payload)
    assert fingerprint["content_hash"] == _fnv1a64(payload)


def test_source_fingerprint_rejects_changed_content_despite_equal_size(tmp_path):
    project = tmp_path / "Project"
    source = project / "Assets" / "stale.bin"
    source.parent.mkdir(parents=True)
    original = b"before"
    source.write_bytes(original)
    entry = _asset_index_entry(
        project,
        source,
        "stale-guid",
        "",
        "Binary",
        content_hash=_fnv1a64(original),
    )
    source.write_bytes(b"after!")
    entry["source"]["modified_ns"] = -987654321

    with pytest.raises(RuntimeArtifactError, match="actual_content_hash"):
        source_fingerprint(project, entry)


def test_source_fingerprint_rejects_size_change_without_hashing(tmp_path):
    project = tmp_path / "Project"
    source = project / "Assets" / "resized.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"small")
    entry = _asset_index_entry(
        project,
        source,
        "resized-guid",
        "",
        "Binary",
        content_hash=_fnv1a64(b"small"),
    )
    source.write_bytes(b"larger")

    with pytest.raises(RuntimeArtifactError, match="fingerprint is stale"):
        source_fingerprint(project, entry)


def test_player_stages_enabled_package_runtime_by_guid_and_excludes_editor(tmp_path):
    project = _make_project(tmp_path)
    runtime = project / "Packages/vendor/gameplay/Runtime/lifecycle.py"
    editor = project / "Packages/vendor/gameplay/Editor/panel.py"
    content = project / "Assets/Plugins/vendor/gameplay/Scenes/Demo.scene"
    control = project / "Packages/vendor/gameplay/InxPackage.json"
    files = (
        (runtime, "runtime-guid", "Runtime/lifecycle.py", "runtime", b"VALUE = 1\n"),
        (editor, "editor-guid", "Editor/panel.py", "editor", b"PANEL = True\n"),
        (content, "content-guid", "Scenes/Demo.scene", "content", b"{}\n"),
    )
    records = []
    for path, guid, logical, role, payload in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        Path(str(path) + ".meta").write_text(
            json.dumps({"metadata": {"guid": {"type": "string", "value": guid}}}),
            encoding="utf-8",
        )
        records.append(
            {
                "logical_path": logical,
                "path_hint": path.relative_to(project).as_posix(),
                "guid": guid,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "role": role,
                "owned": True,
            }
        )
    control.parent.mkdir(parents=True, exist_ok=True)
    control_payload = b"{}\n"
    control.write_bytes(control_payload)
    Path(str(control) + ".meta").write_text(
        json.dumps({"metadata": {"guid": {"type": "string", "value": "control-guid"}}}),
        encoding="utf-8",
    )
    registry = PluginRegistry(str(project))
    registry.record_install(
        {"reference": "vendor/gameplay", "name": "Gameplay", "version": "1.0"},
        files=records,
        control={
            "logical_path": "InxPackage.json",
            "path_hint": control.relative_to(project).as_posix(),
            "guid": "control-guid",
            "sha256": hashlib.sha256(control_payload).hexdigest(),
            "role": "control",
            "owned": True,
        },
    )
    scene = project / "Assets/Main.scene"
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            *[
                _asset_index_entry(project, path, guid, "", "Script" if role != "content" else "Scene")
                for path, guid, _logical, role, _payload in files
            ],
        ],
    )
    output = tmp_path / "build"
    data = output / "Data"
    data.mkdir(parents=True)
    builder = GameBuilder(str(project), str(output), game_name="PackageGame")

    builder._stage_player_plugins(str(data))

    staged_runtime = data / "Packages/vendor/gameplay/Runtime/lifecycle.py"
    assert staged_runtime.read_bytes() == b"VALUE = 1\n"
    assert not (data / "Packages/vendor/gameplay/Editor/panel.py").exists()
    shipped = json.loads(
        (data / "ProjectSettings/InxPlugins.json").read_text(encoding="utf-8")
    )
    assert [item["logical_path"] for item in shipped["installed"][0]["files"]] == [
        "Runtime/lifecycle.py",
        "Scenes/Demo.scene",
    ]

    builder._compile_player_plugin_scripts(str(output))
    assert not staged_runtime.exists()
    assert staged_runtime.with_suffix(".pyc").is_file()


def _write_texture_asset_index(project_root: Path, source: Path, guid: str, artifact_path: str):
    relative_source = source.resolve().relative_to(project_root.resolve()).as_posix()
    scene_path = project_root / "Assets" / "Main.scene"
    scene_path.write_text(
        json.dumps(
            {
                "texture": {
                    "$type": "asset_ref",
                    "asset_type": "Texture",
                    "guid": guid,
                    "path_hint": relative_source,
                }
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project_root,
        [
            _asset_index_entry(project_root, scene_path, "scene-guid", "", "Scene"),
            _asset_index_entry(project_root, source, guid, artifact_path, "Texture", "a" * 16),
        ],
    )


def _particle_source_hash(source: Path) -> str:
    graph = ParticleGraphAsset.from_json(source.read_text(encoding="utf-8"))
    return hashlib.sha256(graph.canonical_json().encode("utf-8")).hexdigest()


def _write_particle_artifact(path: Path, source: Path, *, emitters=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.particle_artifact",
                "source_hash": _particle_source_hash(source),
                "kernel_ir": {"emitters": list(emitters or [])},
            }
        ),
        encoding="utf-8",
    )


def _write_texture_artifact(path: Path, payload: bytes, content_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"INXTEXTURE"
        + b"\x04\x03\x02\x01"
        + len(content_hash).to_bytes(4, "little")
        + content_hash.encode("ascii")
        + payload
    )


def _write_animation_texture_asset(
    texture_path: Path,
    *,
    guid: str,
    sprite_frame_ids=(),
    sprite: bool = True,
) -> None:
    from Infernux.core.asset_types import SpriteFrame, TextureImportSettings, TextureType

    texture_path.parent.mkdir(parents=True, exist_ok=True)
    texture_path.write_bytes(b"test texture")
    settings = TextureImportSettings(
        texture_type=TextureType.SPRITE if sprite else TextureType.DEFAULT,
        sprite_frames=[
            SpriteFrame(stable_id=stable_id, name=f"frame_{index}", w=16, h=16)
            for index, stable_id in enumerate(sprite_frame_ids)
        ],
    )

    def tagged(value):
        if type(value) is bool:
            tag = "bool"
        elif type(value) is int:
            tag = "int"
        elif type(value) is list:
            tag = "json_array"
        else:
            tag = "string"
        return {"type": tag, "value": value}

    metadata = {"guid": tagged(guid)}
    metadata.update({key: tagged(value) for key, value in settings.to_dict().items()})
    texture_path.with_name(texture_path.name + ".meta").write_text(
        json.dumps({"metadata": metadata}, indent=2),
        encoding="utf-8",
    )


def _write_animation_clip(
    clip_path: Path,
    *,
    texture_guid: str,
    texture_path: str,
    sprite_frame_id: str,
) -> None:
    from Infernux.core.animation_clip import AnimationClip, AnimationFrame

    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip = AnimationClip(
        name=clip_path.stem,
        authoring_texture_guid=texture_guid,
        authoring_texture_path=texture_path,
        frames=[AnimationFrame(sprite_frame_id=sprite_frame_id)],
    )
    clip_path.write_text(json.dumps(clip.to_dict(), indent=2), encoding="utf-8")


class TestGameBuilderAnimationClipPreflight:
    TEXTURE_GUID = "b" * 32
    FRAME_ID = "3" * 32

    def test_validate_runs_animation_preflight_and_resolves_texture_by_guid(self, tmp_path):
        project = _make_project(tmp_path)
        texture = project / "Assets" / "Sprites" / "sheet.png"
        _write_animation_texture_asset(
            texture,
            guid=self.TEXTURE_GUID,
            sprite_frame_ids=(self.FRAME_ID,),
        )
        _write_animation_clip(
            project / "Assets" / "Animations" / "walk.animclip2d",
            texture_guid=self.TEXTURE_GUID,
            texture_path="Assets/Sprites/stale-path.png",
            sprite_frame_id=self.FRAME_ID,
        )
        scene = project / "Assets" / "Main.scene"
        _write_asset_index(
            project,
            [
                _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
                _asset_index_entry(
                    project,
                    texture,
                    self.TEXTURE_GUID,
                    "",
                    "Texture",
                ),
            ],
        )
        builder = GameBuilder(
            str(project), str(tmp_path / "build_output"), game_name="TestGame"
        )

        builder._validate()

    def test_preflight_rejects_missing_sprite_frame_with_clip_context(self, tmp_path):
        project = _make_project(tmp_path)
        texture = project / "Assets" / "Sprites" / "sheet.png"
        _write_animation_texture_asset(
            texture,
            guid=self.TEXTURE_GUID,
            sprite_frame_ids=(self.FRAME_ID,),
        )
        missing_id = "4" * 32
        clip_path = project / "Assets" / "Animations" / "broken.animclip2d"
        _write_animation_clip(
            clip_path,
            texture_guid=self.TEXTURE_GUID,
            texture_path="Assets/Sprites/sheet.png",
            sprite_frame_id=missing_id,
        )
        builder = GameBuilder(
            str(project), str(tmp_path / "build_output"), game_name="TestGame"
        )

        with pytest.raises(ValueError, match="AnimationClip build validation failed") as exc:
            builder._validate_animation_clip_assets()

        assert str(clip_path) in str(exc.value)
        assert missing_id in str(exc.value)

    def test_preflight_rejects_texture_not_imported_as_sprite(self, tmp_path):
        project = _make_project(tmp_path)
        texture = project / "Assets" / "Textures" / "albedo.png"
        _write_animation_texture_asset(
            texture,
            guid=self.TEXTURE_GUID,
            sprite=False,
        )
        clip_path = project / "Assets" / "Animations" / "broken.animclip2d"
        _write_animation_clip(
            clip_path,
            texture_guid=self.TEXTURE_GUID,
            texture_path="Assets/Textures/albedo.png",
            sprite_frame_id=self.FRAME_ID,
        )
        builder = GameBuilder(
            str(project), str(tmp_path / "build_output"), game_name="TestGame"
        )

        with pytest.raises(ValueError, match="not imported as Sprite"):
            builder._validate_animation_clip_assets()


def test_validate_accepts_project_relative_build_scene_paths(tmp_path):
    project_root = tmp_path / "project"
    scene_path = project_root / "Assets" / "Acceptance" / "Burst Queue.scene"
    scene_path.parent.mkdir(parents=True)
    scene_path.write_text('{"objects": []}', encoding="utf-8")
    settings_dir = project_root / "ProjectSettings"
    settings_dir.mkdir()
    (settings_dir / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Acceptance/Burst Queue.scene"]}),
        encoding="utf-8",
    )
    _write_asset_index(
        project_root,
        [_asset_index_entry(project_root, scene_path, "scene-guid", "", "Scene")],
    )
    builder = GameBuilder(
        str(project_root), str(tmp_path / "build_output"), game_name="TestGame"
    )

    builder._validate()


def test_build_scene_outside_assets_is_rejected_without_legacy_fallback(tmp_path):
    project_root = _make_project(tmp_path)
    legacy_scene = project_root / "Legacy.scene"
    legacy_scene.write_text('{"objects": []}', encoding="utf-8")
    builder = GameBuilder(
        str(project_root), str(tmp_path / "build_output"), game_name="TestGame"
    )

    with pytest.raises(ValueError, match="inside the project Assets folder"):
        builder._resolve_build_scene_path("Legacy.scene")


def test_rewrite_build_settings_keeps_project_relative_scene_identity(tmp_path):
    project_root = _make_project(tmp_path)
    settings_path = project_root / "ProjectSettings" / "BuildSettings.json"
    settings_path.write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}), encoding="utf-8"
    )
    final_settings = tmp_path / "dist" / "Data" / "ProjectSettings"
    final_settings.mkdir(parents=True)
    shutil.copy2(settings_path, final_settings / "BuildSettings.json")
    builder = GameBuilder(
        str(project_root), str(tmp_path / "build_output"), game_name="TestGame"
    )

    builder._relativize_scenes(str(tmp_path / "dist"))

    rewritten = json.loads(
        (final_settings / "BuildSettings.json").read_text(encoding="utf-8")
    )
    assert rewritten["scenes"] == ["Assets/Main.scene"]


def test_rewrite_build_settings_strips_authoring_only_fields(tmp_path):
    project_root = _make_project(tmp_path)
    settings_path = project_root / "ProjectSettings" / "BuildSettings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.update(
        {
            "output_dir": str(tmp_path / "private-player-output"),
            "icon_path": str(project_root / "Assets" / "icon.png"),
            "debug_mode": False,
            "lto": True,
            "enable_jit": False,
            "additional_cook_roots": ["Assets/RuntimeOnly"],
        }
    )
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    final_settings = tmp_path / "dist" / "Data" / "ProjectSettings"
    final_settings.mkdir(parents=True)
    shutil.copy2(settings_path, final_settings / "BuildSettings.json")
    builder = GameBuilder(
        str(project_root), str(tmp_path / "build_output"), game_name="TestGame"
    )

    builder._relativize_scenes(str(tmp_path / "dist"))

    rewritten = json.loads(
        (final_settings / "BuildSettings.json").read_text(encoding="utf-8")
    )
    assert rewritten == {"scenes": ["Assets/Main.scene"]}


def test_build_cancellation_is_not_reported_as_a_build_failure(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    error_messages: list[str] = []
    build_log_dir = tmp_path / "build-log"
    monkeypatch.setattr(
        game_builder_module.tempfile,
        "mkdtemp",
        lambda **_kwargs: (build_log_dir.mkdir() or str(build_log_dir)),
    )
    monkeypatch.setattr(builder, "_build_inner", lambda *_args, **_kwargs: (_ for _ in ()).throw(BuildCancelled()))
    monkeypatch.setattr(game_builder_module.Debug, "log_error", error_messages.append)

    with pytest.raises(BuildCancelled):
        builder.build()

    build_log = (build_log_dir / "build.log").read_text(encoding="utf-8")
    assert "Build cancelled by user." in build_log
    assert "BUILD FAILED" not in build_log
    assert error_messages == []
    assert not (tmp_path / "project" / "Logs").exists()


def test_successful_build_log_is_not_created_in_project_or_kept_in_output(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    build_log_dir = tmp_path / "build-log"
    monkeypatch.setattr(
        game_builder_module.tempfile,
        "mkdtemp",
        lambda **_kwargs: (build_log_dir.mkdir() or str(build_log_dir)),
    )
    monkeypatch.setattr(builder, "_build_inner", lambda *_args, **_kwargs: str(tmp_path / "dist"))

    assert builder.build() == str(tmp_path / "dist")
    assert not (tmp_path / "project" / "Logs").exists()
    assert not build_log_dir.exists()


def test_nuitka_cancellation_does_not_wait_for_the_next_stdout_line(tmp_path, monkeypatch):
    monkeypatch.setattr(nuitka_builder_module, "_ensure_windows_msvc_environment", lambda env: env)
    builder = object.__new__(NuitkaBuilder)
    builder._staging_dir = str(tmp_path)
    cancelled = threading.Event()
    cancelled.set()

    started = time.perf_counter()
    with pytest.raises(BuildCancelled):
        builder._run_nuitka(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
            on_progress=None,
            cancel_event=cancelled,
        )

    assert time.perf_counter() - started < 2.5


@pytest.mark.parametrize(
    ("player_module", "expected_name"),
    ((False, "boot.py"), (True, "_InfernuxPlayer.py")),
)
def test_nuitka_staging_entry_matches_build_mode(tmp_path, player_module, expected_name):
    entry = tmp_path / "source.py"
    entry.write_text("VALUE = 1\n", encoding="utf-8")
    builder = object.__new__(NuitkaBuilder)
    builder.entry_script = str(entry)
    builder._staging_dir = str(tmp_path / f"stage-{expected_name}")
    builder.player_module = player_module

    builder._prepare_staging()

    assert Path(builder._staged_entry).name == expected_name
    assert Path(builder._staged_entry).read_text(encoding="utf-8") == "VALUE = 1\n"


def test_player_module_command_uses_matching_staged_module_name(tmp_path, monkeypatch):
    monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")
    monkeypatch.setattr(nuitka_builder_module, "_has_msvc_toolchain", lambda: True)
    entry = tmp_path / "source.py"
    entry.write_text("VALUE = 1\n", encoding="utf-8")
    builder = object.__new__(NuitkaBuilder)
    builder.entry_script = str(entry)
    builder._staging_dir = str(tmp_path / "module-stage")
    builder.player_module = True
    builder._builder_python = "python"
    builder.console_mode = "disable"
    builder.output_filename = "_InfernuxPlayer.pyd"
    builder.lto = False
    builder.extra_include_packages = []
    builder.extra_include_data = []
    builder.raw_copy_packages = []
    builder.product_name = "Infernux Player"
    builder.file_version = "0.2.9.0"
    builder.icon_path = None
    builder._prepare_staging()

    command = builder._build_command()

    assert "--module" in command
    assert "--standalone" not in command
    assert "--follow-imports" not in command
    assert "--nofollow-imports" in command
    assert not any(argument.startswith("--follow-import-to=") for argument in command)
    assert not any(argument.startswith("--output-filename=") for argument in command)
    assert not any(argument.startswith("--include-") for argument in command)
    assert Path(command[-1]).name == "_InfernuxPlayer.py"


def test_player_module_output_discovery_accepts_python_abi_suffix(tmp_path):
    suffix = nuitka_builder_module.importlib.machinery.EXTENSION_SUFFIXES[0]
    module = tmp_path / f"_InfernuxPlayer{suffix}"
    module.write_bytes(b"module")

    assert nuitka_builder_module._find_player_module_output(str(tmp_path)) == str(module)


def test_player_module_stages_explicit_python_bootstrap_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    sources = {}
    for filename in ("python313.dll", "_ctypes.pyd", "libffi-8.dll", "zlib.dll"):
        source = runtime / filename
        source.write_bytes(filename.encode("ascii"))
        sources[filename] = source
    encodings = runtime / "encodings"
    encodings.mkdir()
    (encodings / "__init__.py").write_text("from . import aliases\n", encoding="utf-8")
    (encodings / "aliases.py").write_text("aliases = {}\n", encoding="utf-8")
    (encodings / "utf_8.py").write_text("name = 'utf-8'\n", encoding="utf-8")
    builder = object.__new__(NuitkaBuilder)
    builder._python_bootstrap_runtime_sources = lambda: (sources, encodings)
    dist = tmp_path / "dist"
    dist.mkdir()

    builder._inject_python_bootstrap_runtime(str(dist))

    for filename in sources:
        assert (dist / filename).read_bytes() == sources[filename].read_bytes()
    bootstrap_manifest = json.loads(
        (dist / game_builder_module.BOOTSTRAP_NATIVE_MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert bootstrap_manifest == {
        "$schema": game_builder_module.BOOTSTRAP_NATIVE_MANIFEST_SCHEMA,
        "files": sorted(sources),
    }
    assert (dist / "stdlib" / "encodings" / "__init__.pyc").is_file()
    assert not list((dist / "stdlib" / "encodings").glob("*.py"))


def test_python_bootstrap_runtime_accepts_standalone_libffi_name(tmp_path, monkeypatch):
    python_root = tmp_path / "python313"
    dll_root = python_root / "DLLs"
    encodings = python_root / "Lib" / "encodings"
    dll_root.mkdir(parents=True)
    encodings.mkdir(parents=True)
    python_executable = python_root / "python.exe"
    python_executable.write_bytes(b"python")
    (python_root / "python313.dll").write_bytes(b"python ABI")
    ctypes_module = dll_root / "_ctypes.pyd"
    ctypes_module.write_bytes(b"ctypes ABI")
    libffi = dll_root / "libffi-8.dll"
    libffi.write_bytes(b"libffi ABI")

    def find_spec(name):
        if name == "_ctypes":
            return SimpleNamespace(origin=str(ctypes_module))
        if name == "encodings":
            return SimpleNamespace(submodule_search_locations=[str(encodings)])
        return None

    monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")
    monkeypatch.setattr(nuitka_builder_module.sys, "stdlib_module_names", frozenset())
    monkeypatch.setattr(nuitka_builder_module.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(nuitka_builder_module, "resolved_path", lambda value: str(value))
    monkeypatch.setattr(
        nuitka_builder_module,
        "path_key",
        lambda value: os.path.normcase(os.path.abspath(str(value))),
    )
    builder = object.__new__(NuitkaBuilder)
    builder._builder_python = str(python_executable)

    sources, resolved_encodings = builder._python_bootstrap_runtime_sources()

    assert sources["libffi-8.dll"] == libffi
    assert "ffi.dll" not in sources
    assert resolved_encodings == encodings


def test_python_bootstrap_runtime_follows_venv_base_prefix(tmp_path, monkeypatch):
    venv_root = tmp_path / "project" / ".venv"
    scripts_root = venv_root / "Scripts"
    dll_root = venv_root / "DLLs"
    base_root = tmp_path / "managed-python313"
    encodings = venv_root / "Lib" / "encodings"
    scripts_root.mkdir(parents=True)
    dll_root.mkdir(parents=True)
    base_root.mkdir()
    encodings.mkdir(parents=True)
    python_executable = scripts_root / "python.exe"
    python_executable.write_bytes(b"venv launcher")
    python_dll = base_root / "python313.dll"
    python_dll.write_bytes(b"base Python ABI")
    ctypes_module = dll_root / "_ctypes.pyd"
    ctypes_module.write_bytes(b"ctypes ABI")
    libffi = dll_root / "libffi-8.dll"
    libffi.write_bytes(b"libffi ABI")

    def find_spec(name):
        if name == "_ctypes":
            return SimpleNamespace(origin=str(ctypes_module))
        if name == "encodings":
            return SimpleNamespace(submodule_search_locations=[str(encodings)])
        return None

    monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")
    monkeypatch.setattr(nuitka_builder_module.sys, "prefix", str(venv_root))
    monkeypatch.setattr(nuitka_builder_module.sys, "exec_prefix", str(venv_root))
    monkeypatch.setattr(nuitka_builder_module.sys, "base_prefix", str(base_root))
    monkeypatch.setattr(nuitka_builder_module.sys, "base_exec_prefix", str(base_root))
    monkeypatch.setattr(nuitka_builder_module.sys, "executable", str(python_executable))
    monkeypatch.setattr(nuitka_builder_module.sys, "stdlib_module_names", frozenset())
    monkeypatch.setattr(nuitka_builder_module.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(nuitka_builder_module, "resolved_path", lambda value: str(value))
    monkeypatch.setattr(
        nuitka_builder_module,
        "path_key",
        lambda value: os.path.normcase(os.path.abspath(str(value))),
    )
    builder = object.__new__(NuitkaBuilder)
    builder._builder_python = str(python_executable)

    sources, resolved_encodings = builder._python_bootstrap_runtime_sources()

    assert sources["python313.dll"] == python_dll
    assert sources["libffi-8.dll"] == libffi
    assert resolved_encodings == encodings


def test_player_module_stages_source_less_engine_runtime(tmp_path, monkeypatch):
    import Infernux

    source = tmp_path / "source" / "Infernux"
    (source / "engine").mkdir(parents=True)
    (source / "lib").mkdir()
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "engine" / "__init__.py").write_text("ENGINE = 1\n", encoding="utf-8")
    (source / "engine" / "runtime.py").write_text(
        "from __future__ import annotations\nVALUE = 2\n",
        encoding="utf-8",
    )
    (source / "engine" / "path_utils.py").write_text(
        "def resolved_path(value): return str(value)\n",
        encoding="utf-8",
    )
    (source / "engine" / "runtime_artifact_catalog.py").write_text(
        "from .path_utils import resolved_path\nCATALOG_VERSION = 1\n",
        encoding="utf-8",
    )
    (source / "engine" / "build_settings.py").write_text(
        "def load_build_settings(): return {'scenes': []}\n",
        encoding="utf-8",
    )
    (source / "engine" / "data.json").write_text("{}\n", encoding="utf-8")
    (source / "engine" / "game_builder.py").write_text("BUILD = 1\n", encoding="utf-8")
    (source / "engine" / "nuitka_builder.py").write_text("COMPILE = 1\n", encoding="utf-8")
    (source / "engine" / "interaction").mkdir()
    (source / "engine" / "interaction" / "__init__.py").write_text(
        "EDITOR = 1\n", encoding="utf-8"
    )
    (source / "engine" / "undo").mkdir()
    (source / "engine" / "undo" / "__init__.py").write_text(
        "EDITOR = 1\n", encoding="utf-8"
    )
    (source / "engine" / "ui").mkdir()
    (source / "engine" / "ui" / "__init__.py").write_text("UI = 1\n", encoding="utf-8")
    (source / "engine" / "ui" / "theme.py").write_text("THEME = 1\n", encoding="utf-8")
    (source / "engine" / "ui" / "viewport_utils.py").write_text(
        "VIEWPORT = 1\n", encoding="utf-8"
    )
    (source / "engine" / "ui" / "engine_status.py").write_text(
        "STATUS = 1\n", encoding="utf-8"
    )
    (source / "engine" / "ui" / "runtime_canvas_snapshot.py").write_text(
        "SNAPSHOT = 1\n", encoding="utf-8"
    )
    (source / "engine" / "ui" / "editor_panel.py").write_text(
        "EDITOR = 1\n", encoding="utf-8"
    )
    (source / "lib" / "__init__.py").write_text("NATIVE = True\n", encoding="utf-8")
    (source / "lib" / "stale.dll").write_bytes(b"stale")

    monkeypatch.setattr(Infernux, "__file__", str(source / "__init__.py"))
    builder = object.__new__(NuitkaBuilder)
    dist = tmp_path / "dist"
    (dist / "Infernux" / "lib").mkdir(parents=True)
    (dist / "Infernux" / "lib" / "native.dll").write_bytes(b"native")

    builder._inject_engine_python_runtime(str(dist))

    runtime = dist / "Infernux"
    assert (runtime / "__init__.pyc").is_file()
    assert (runtime / "engine" / "runtime.pyc").is_file()
    assert (runtime / "engine" / "path_utils.pyc").is_file()
    assert (runtime / "engine" / "runtime_artifact_catalog.pyc").is_file()
    assert (runtime / "engine" / "build_settings.pyc").is_file()
    assert (runtime / "engine" / "data.json").is_file()
    assert (runtime / "lib" / "__init__.pyc").is_file()
    assert (runtime / "lib" / "native.dll").read_bytes() == b"native"
    assert not (runtime / "lib" / "stale.dll").exists()
    assert not (runtime / "engine" / "game_builder.pyc").exists()
    assert not (runtime / "engine" / "nuitka_builder.pyc").exists()
    assert not (runtime / "engine" / "interaction").exists()
    assert not (runtime / "engine" / "undo").exists()
    assert (runtime / "engine" / "ui" / "__init__.pyc").is_file()
    assert (runtime / "engine" / "ui" / "theme.pyc").is_file()
    assert (runtime / "engine" / "ui" / "viewport_utils.pyc").is_file()
    assert (runtime / "engine" / "ui" / "engine_status.pyc").is_file()
    assert (runtime / "engine" / "ui" / "runtime_canvas_snapshot.pyc").is_file()
    assert not (runtime / "engine" / "ui" / "editor_panel.pyc").exists()
    assert not list(runtime.rglob("*.py"))

    fake_stdlib = tmp_path / "stdlib-source"
    fake_stdlib.mkdir()
    shutil.copy2(Path(os.__file__).parent / "__future__.py", fake_stdlib / "__future__.py")
    shutil.copy2(Path(os.__file__).parent / "inspect.py", fake_stdlib / "inspect.py")
    (fake_stdlib / "README.txt").write_text("development documentation", encoding="utf-8")
    (fake_stdlib / "pydoc_data").mkdir()
    (fake_stdlib / "pydoc_data" / "_pydoc.css").write_text("body {}", encoding="utf-8")
    (fake_stdlib / "lib2to3").mkdir()
    (fake_stdlib / "lib2to3" / "Grammar.txt").write_text("grammar", encoding="utf-8")
    (fake_stdlib / "lib2to3" / "Grammar.pickle").write_bytes(b"pickle")
    builder._inject_python_runtime_stdlib(str(dist), source_root=fake_stdlib)
    assert (dist / "stdlib" / "__future__.pyc").is_file()
    assert (dist / "stdlib" / "inspect.pyc").is_file()
    staged_stdlib_files = [path for path in (dist / "stdlib").rglob("*") if path.is_file()]
    assert staged_stdlib_files
    assert all(path.suffix.casefold() == ".pyc" for path in staged_stdlib_files)
    assert not (dist / "stdlib" / "lib2to3" / "Grammar.txt").exists()

    probe = nuitka_builder_module._run_python(
        sys.executable,
        [
            "-I",
            "-S",
            "-c",
            (
                "import sys; "
                f"sys.path[:] = [{str(dist)!r}, {str(dist / 'stdlib')!r}]; "
                "import Infernux.engine.runtime as runtime; "
                "assert runtime.VALUE == 2"
            ),
        ],
    )
    assert probe.returncode == 0, probe.stderr


def test_player_always_raw_copies_numpy_when_jit_is_disabled(tmp_path, monkeypatch):
    captured: dict = {}

    class _FakeNuitkaBuilder:
        _JIT_NOFOLLOW_PACKAGES = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def build(self, **_kwargs):
            return str(tmp_path / "dist")

    monkeypatch.setattr(game_builder_module, "NuitkaBuilder", _FakeNuitkaBuilder)
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    builder.enable_jit = False

    result = builder._run_nuitka(
        str(tmp_path / "boot.py"),
        on_progress=None,
        user_packages=[],
    )

    assert result == str(tmp_path / "dist")
    assert captured["raw_copy_packages"] == ["numpy", "packaging"]
    assert captured["runtime_support_packages"] == ["numba", "llvmlite"]
    assert captured["runtime_pack_cache"] is True
    assert captured["output_filename"] == ("_InfernuxPlayer.pyd" if sys.platform == "win32" else "_InfernuxPlayer.so")
    assert captured["player_module"] is True
    assert captured["product_name"] == "Infernux Player"
    assert captured["icon_path"]
    assert Path(captured["icon_path"]).name == "icon.png"
    assert Path(captured["icon_path"]).is_file()


@pytest.mark.parametrize(
    ("debug_mode", "expected_profile"),
    ((False, "release"), (True, "development")),
)
def test_jit_build_installs_optional_parallel_runtime_module(
    tmp_path,
    monkeypatch,
    debug_mode,
    expected_profile,
):
    captured: dict = {}
    installed: dict = {}

    class _FakeNuitkaBuilder:
        _JIT_NOFOLLOW_PACKAGES = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def build(self, **_kwargs):
            return str(tmp_path / "dist")

        def install_runtime_module(self, dist_dir, **kwargs):
            installed.update({"dist_dir": dist_dir, **kwargs})
            return True

    monkeypatch.setattr(game_builder_module, "NuitkaBuilder", _FakeNuitkaBuilder)
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    builder.enable_jit = True
    builder.debug_mode = debug_mode

    result = builder._run_nuitka(
        str(tmp_path / "boot.py"),
        on_progress=None,
        user_packages=[],
    )

    assert result == str(tmp_path / "dist")
    assert captured["raw_copy_packages"] == ["numpy", "packaging"]
    assert installed == {
        "dist_dir": str(tmp_path / "dist"),
        "module_name": "parallel",
        "packages": ["numba", "llvmlite"],
        "archive_only": True,
        "profile": expected_profile,
    }


def test_debug_player_uses_generic_reusable_runtime_pack(tmp_path, monkeypatch):
    captured: dict = {}

    class _FakeNuitkaBuilder:
        _JIT_NOFOLLOW_PACKAGES = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def build(self, **_kwargs):
            return str(tmp_path / "dist")

    monkeypatch.setattr(game_builder_module, "NuitkaBuilder", _FakeNuitkaBuilder)
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    builder.debug_mode = True

    builder._run_nuitka(str(tmp_path / "boot.py"), on_progress=None, user_packages=[])

    assert captured["runtime_pack_cache"] is True
    assert captured["output_filename"] == ("_InfernuxPlayer.pyd" if sys.platform == "win32" else "_InfernuxPlayer.so")
    assert captured["player_module"] is True
    assert captured["product_name"] == "Infernux Player"
    assert captured["icon_path"]
    assert Path(captured["icon_path"]).name == "icon.png"
    assert Path(captured["icon_path"]).is_file()


def test_pack_core_runtime_moves_unclassified_native_files(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    final_dir.mkdir()
    data_root = final_dir / "TestGame_Data"
    data_root.mkdir()
    (final_dir / "late-runtime.dll").write_bytes(b"move into runtime")
    (final_dir / "Infernux" / "resources").mkdir(parents=True)
    (final_dir / "Infernux" / "resources" / "runtime.txt").write_text(
        "runtime", encoding="utf-8"
    )
    (final_dir / "Infernux" / "engine" / "locales").mkdir(parents=True)
    packaging_root = final_dir / "packaging"
    packaging_root.mkdir()
    (packaging_root / "__init__.pyc").write_bytes(b"packaging runtime")
    builder._pack_core_runtime_archive(str(final_dir))

    manifest = read_manifest(data_root / builder._RUNTIME_ARCHIVE_FILENAME)
    paths = {entry["path"] for entry in manifest["files"]}
    assert "stdlib/late-runtime.dll" in paths
    assert "packaging/__init__.pyc" in paths
    assert "stdlib/__future__.pyc" not in paths
    assert not (final_dir / "late-runtime.dll").exists()
    assert not packaging_root.exists()
    assert not (final_dir / "Infernux").exists()


def test_pack_core_runtime_moves_versioned_linux_sonames_off_root(
    tmp_path, monkeypatch
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    runtime_root = final_dir / "Infernux" / "resources"
    data_root.mkdir(parents=True)
    runtime_root.mkdir(parents=True)
    (runtime_root / "runtime.bin").write_bytes(b"runtime")
    (final_dir / "libz.so.1").write_bytes(b"versioned soname")
    monkeypatch.setattr(game_builder_module.sys, "platform", "linux")

    builder._pack_core_runtime_archive(str(final_dir))

    paths = {
        entry["path"]
        for entry in read_manifest(data_root / builder._RUNTIME_ARCHIVE_FILENAME)["files"]
    }
    assert "stdlib/libz.so.1" in paths
    assert not (final_dir / "libz.so.1").exists()


@pytest.mark.skipif(
    sys.platform != "win32", reason="validates the Windows Player DLL layout"
)
def test_pack_core_runtime_moves_full_native_closure_off_root(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    package_lib = final_dir / "Infernux" / "lib"
    package_lib.mkdir(parents=True)
    data_root.mkdir(parents=True)
    (package_lib / "_Infernux.pyd").write_bytes(b"full bridge")
    (package_lib / "InfernuxFoundation.dll").write_bytes(b"foundation")
    (package_lib / "InfernuxRendererRuntime.dll").write_bytes(b"runtime")
    for legacy_name in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_FILES:
        (package_lib / legacy_name).write_bytes(b"legacy static dependency")
        (final_dir / legacy_name).write_bytes(b"legacy root dependency")
    (final_dir / "_InfernuxBootstrap.pyd").write_bytes(b"bootstrap")
    (final_dir / "InfernuxFoundation.dll").write_bytes(b"foundation")
    (final_dir / "InfernuxRendererRuntime.dll").write_bytes(b"runtime")
    (final_dir / "python313.dll").write_bytes(b"python")
    (final_dir / "_ctypes.pyd").write_bytes(b"ctypes ABI")
    (final_dir / "libffi-8.dll").write_bytes(b"libffi ABI")
    (final_dir / "_socket.pyd").write_bytes(b"socket")

    builder._pack_core_runtime_archive(str(final_dir))

    paths = {
        entry["path"]
        for entry in read_manifest(data_root / builder._RUNTIME_ARCHIVE_FILENAME)["files"]
    }
    assert "Infernux/lib/_Infernux.pyd" in paths
    assert "Infernux/lib/InfernuxFoundation.dll" in paths
    assert "Infernux/lib/InfernuxRendererRuntime.dll" in paths
    assert "stdlib/_socket.pyd" in paths
    assert (final_dir / "_InfernuxBootstrap.pyd").is_file()
    assert (final_dir / "InfernuxFoundation.dll").is_file()
    assert (final_dir / "python313.dll").is_file()
    assert (final_dir / "_ctypes.pyd").is_file()
    assert (final_dir / "libffi-8.dll").is_file()
    assert "stdlib/_ctypes.pyd" not in paths
    assert "stdlib/libffi-8.dll" not in paths
    assert not {
        f"Infernux/lib/{name}" for name in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_FILES
    } & paths
    assert not {
        f"stdlib/{name}" for name in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_FILES
    } & paths
    assert not (final_dir / "InfernuxRendererRuntime.dll").exists()
    assert all(
        not (final_dir / name).exists()
        for name in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_FILES
    )
    assert not (final_dir / "Infernux").exists()


def test_bootstrap_archive_preserves_player_module_abi_filename(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    package_lib = final_dir / "Infernux" / "lib"
    data_root.mkdir(parents=True)
    package_lib.mkdir(parents=True)
    if sys.platform == "win32":
        python_bootstrap_files = (
            "python313.dll",
            "_ctypes.pyd",
            "libffi-8.dll",
            "_opcode.pyd",
        )
        bootstrap_module_name = "_InfernuxBootstrap.pyd"
        module_name = "_InfernuxPlayer.cp313-win_amd64.pyd"
        foundation_name = "InfernuxFoundation.dll"
    else:
        python_bootstrap_files = (
            "libpython3.13.so.1.0",
            "_ctypes.cpython-312-x86_64-linux-gnu.so",
            "libffi.so.8",
            "_opcode.cpython-313-x86_64-linux-gnu.so",
        )
        bootstrap_module_name = "_InfernuxBootstrap.cpython-312-x86_64-linux-gnu.so"
        module_name = "_InfernuxPlayer.cpython-312-x86_64-linux-gnu.so"
        foundation_name = "libInfernuxFoundation.so"
    for filename in (*python_bootstrap_files, bootstrap_module_name):
        (final_dir / filename).write_bytes(filename.encode("ascii"))
    (final_dir / game_builder_module.BOOTSTRAP_NATIVE_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "$schema": game_builder_module.BOOTSTRAP_NATIVE_MANIFEST_SCHEMA,
                "files": list(python_bootstrap_files),
            }
        ),
        encoding="utf-8",
    )
    (final_dir / module_name).write_bytes(b"player module")
    (package_lib / foundation_name).write_bytes(b"foundation")
    encodings = final_dir / "stdlib" / "encodings"
    encodings.mkdir(parents=True)
    for filename in ("__init__.pyc", "aliases.pyc", "utf_8.pyc"):
        (encodings / filename).write_bytes(b"bytecode")
    (final_dir / "stdlib" / "__future__.pyc").write_bytes(b"future bytecode")
    (final_dir / "stdlib" / "inspect.pyc").write_bytes(b"inspect bytecode")

    builder._pack_player_bootstrap_archive(str(final_dir))

    manifest = read_manifest(data_root / "Bootstrap.inxrt")
    paths = {entry["path"] for entry in manifest["files"]}
    assert module_name in paths
    assert "stdlib/encodings/__init__.pyc" in paths
    assert "stdlib/__future__.pyc" in paths
    assert "stdlib/inspect.pyc" in paths
    assert game_builder_module.BOOTSTRAP_NATIVE_MANIFEST_FILENAME in paths
    assert any(Path(path).name.startswith("_opcode") for path in paths)
    assert all(
        path.endswith(".pyc")
        for path in paths
        if path.startswith("stdlib/")
    )
    assert "stdlib/lib2to3/Grammar.txt" not in paths
    assert "_InfernuxPlayer.pyd" not in paths
    assert not (final_dir / module_name).exists()
    assert not (final_dir / "stdlib").exists()


def test_runtime_pack_cache_round_trip(tmp_path, monkeypatch):
    cache_root = tmp_path / "runtime-packs"
    monkeypatch.setattr(nuitka_builder_module, "_RUNTIME_PACK_DIR", str(cache_root))
    builder = object.__new__(NuitkaBuilder)
    builder._staging_dir = str(tmp_path / "staging")
    builder.console_mode = "disable"
    builder.lto = True
    os.makedirs(builder._staging_dir)
    dist = tmp_path / "original.dist"
    dist.mkdir(parents=True)
    (dist / "InfernuxPlayer.exe").write_bytes(b"runtime")
    (dist / "bindings.pyi.bak").write_bytes(b"editor backup")
    (dist / "InfernuxPlayer.pdb").write_bytes(b"debug symbols")

    builder._store_runtime_pack("a" * 64, str(dist))
    pack_root = cache_root / ("a" * 64)
    restored = builder._restore_runtime_pack("a" * 64)

    assert restored == os.path.join(builder._staging_dir, "boot.dist")
    assert (tmp_path / "staging" / "boot.dist" / "InfernuxPlayer.exe").read_bytes() == b"runtime"
    cache_manifest = json.loads(
        (cache_root / ("a" * 64) / "Player.inxmanifest").read_text(encoding="utf-8")
    )
    assert cache_manifest["archive"] == "Runtime.inxrt"
    assert cache_manifest["compression"] == "store"
    assert cache_manifest["lto"] is True
    assert cache_manifest["file_count"] == 1
    assert cache_manifest["archive_bytes"] > 0
    native_manifest = read_manifest(cache_root / ("a" * 64) / "Runtime.inxrt")
    assert native_manifest["compression_profile"] == "development"
    assert {entry["path"] for entry in native_manifest["files"]} == {
        "InfernuxPlayer.exe"
    }
    assert not (tmp_path / "staging" / "boot.dist" / "_infernux_runtime_pack.json").exists()


def test_packaged_runtime_pack_restores_without_local_cache(tmp_path, monkeypatch):
    cache_root = tmp_path / "runtime-packs"
    packaged_root = tmp_path / "wheel" / "_runtime_packs"
    monkeypatch.setattr(nuitka_builder_module, "_RUNTIME_PACK_DIR", str(cache_root))
    monkeypatch.setenv("INFERNUX_PREBUILT_RUNTIME_PACK_DIR", str(packaged_root))
    builder = object.__new__(NuitkaBuilder)
    builder._staging_dir = str(tmp_path / "staging")
    builder._engine_fingerprint_cache = "engine-content"
    builder.console_mode = "disable"
    builder.lto = True
    os.makedirs(builder._staging_dir)
    dist = tmp_path / "original.dist"
    dist.mkdir()
    (dist / "InfernuxPlayer.exe").write_bytes(b"prebuilt-runtime")
    fingerprint = "a" * 64
    compatibility_key = "b" * 64

    builder._store_runtime_pack(
        fingerprint,
        str(dist),
        compatibility_key=compatibility_key,
    )
    packaged_pack = packaged_root / compatibility_key
    packaged_pack.parent.mkdir(parents=True)
    shutil.copytree(cache_root / fingerprint, packaged_pack)
    shutil.rmtree(cache_root / fingerprint)

    restored = builder._restore_runtime_pack(
        fingerprint,
        compatibility_key=compatibility_key,
    )

    assert restored == os.path.join(builder._staging_dir, "boot.dist")
    assert (Path(restored) / "InfernuxPlayer.exe").read_bytes() == b"prebuilt-runtime"


def test_runtime_pack_rejects_unsafe_archive_paths(tmp_path, monkeypatch):
    cache_root = tmp_path / "runtime-packs"
    monkeypatch.setattr(nuitka_builder_module, "_RUNTIME_PACK_DIR", str(cache_root))
    builder = object.__new__(NuitkaBuilder)
    builder._staging_dir = str(tmp_path / "staging")
    builder._engine_fingerprint_cache = "engine-content"
    builder.console_mode = "disable"
    builder.lto = True
    os.makedirs(builder._staging_dir)
    dist = tmp_path / "original.dist"
    dist.mkdir()
    (dist / "InfernuxPlayer.exe").write_bytes(b"runtime")
    fingerprint = "a" * 64
    builder._store_runtime_pack(fingerprint, str(dist))

    pack_root = cache_root / fingerprint
    archive = pack_root / "Runtime.inxrt"
    archive_key = _FakeNativeInxPack._key(archive)
    manifest = read_manifest(archive)
    original_path = str(manifest["files"][0]["path"])
    payload = _FakeNativeInxPack.entries.pop((archive_key, original_path))
    manifest["files"][0]["path"] = "../escape.txt"
    _FakeNativeInxPack.entries[(archive_key, "../escape.txt")] = payload

    assert builder._restore_runtime_pack_root(str(pack_root)) is None
    assert not (tmp_path / "escape.txt").exists()


def test_raw_runtime_package_sources_are_replaced_with_adjacent_bytecode(tmp_path):
    package_root = tmp_path / "numba"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    nested = package_root / "nested.py"
    nested.write_text("def value():\n    return 2\n", encoding="utf-8")

    NuitkaBuilder._compile_raw_python_sources(package_root)

    assert not (package_root / "__init__.py").exists()
    assert not nested.exists()
    assert (package_root / "__init__.pyc").is_file()
    assert (package_root / "nested.pyc").is_file()


def test_packaged_parallel_runtime_module_round_trip(tmp_path, monkeypatch):
    module_root = tmp_path / "wheel" / "_runtime_modules"
    builder = object.__new__(NuitkaBuilder)
    builder.last_runtime_compatibility_key = "b" * 64
    builder._engine_fingerprint_cache = "engine-content"

    def fake_inject(dist_dir, packages=None):
        for package in packages or []:
            package_dir = Path(dist_dir) / package
            package_dir.mkdir(parents=True, exist_ok=True)
            (package_dir / "runtime.pyc").write_bytes(package.encode("utf-8"))
            (package_dir / "runtime.pyi").write_text(package, encoding="utf-8")
            (package_dir / "runtime.c").write_text(package, encoding="utf-8")
            (package_dir / "runtime.h").write_text(package, encoding="utf-8")

    monkeypatch.setattr(builder, "_inject_jit_packages", fake_inject)
    exported = builder.export_runtime_module(str(module_root))
    monkeypatch.setenv("INFERNUX_PREBUILT_RUNTIME_MODULE_DIR", str(module_root))

    dist = tmp_path / "dist"
    dist.mkdir()
    assert builder.install_runtime_module(str(dist)) is True
    assert (dist / "numba" / "runtime.pyc").read_bytes() == b"numba"
    assert (dist / "llvmlite" / "runtime.pyc").read_bytes() == b"llvmlite"
    manifest = json.loads(
        (Path(exported) / "Player.inxmanifest").read_text(encoding="utf-8")
    )
    assert manifest["archive"] == "Parallel.inxmod"
    assert manifest["compression"] == "store"
    assert manifest["compression_profile"] == "development"
    assert manifest["packages"] == ["llvmlite", "numba"]
    module_manifest = read_manifest(Path(exported) / "Parallel.inxmod")
    assert all(
        not entry["path"].endswith((".pyi", ".c", ".h"))
        for entry in module_manifest["files"]
    )

    # Re-exporting the same compatibility payload is idempotent. This is the
    # normal CMake retry/concurrent-build case on Windows and must not require
    # replacing an already published directory.
    assert builder.export_runtime_module(str(module_root)) == exported

    compressed_dist = tmp_path / "compressed-dist"
    compressed_dist.mkdir()
    assert builder.install_runtime_module(
        str(compressed_dist), archive_only=True
    ) is True
    staged = compressed_dist / "Parallel.inxmod"
    assert staged.read_bytes() == (Path(exported) / "Parallel.inxmod").read_bytes()
    assert not (compressed_dist / "numba").exists()
    assert not (compressed_dist / "llvmlite").exists()

    release_dist = tmp_path / "release-dist"
    release_dist.mkdir()
    assert builder.install_runtime_module(
        str(release_dist),
        archive_only=True,
        profile="release",
    ) is True
    assert read_manifest(release_dist / "Parallel.inxmod")["compression_profile"] == "release"


def test_runtime_engine_fingerprint_ignores_generated_meta(tmp_path, monkeypatch):
    import Infernux

    package_root = tmp_path / "Infernux"
    package_root.mkdir()
    package_init = package_root / "__init__.py"
    package_init.write_text("VALUE = 1\n", encoding="utf-8")
    metadata = package_root / "shader.frag.meta"
    metadata.write_text("first", encoding="utf-8")
    monkeypatch.setattr(Infernux, "__file__", str(package_init))
    monkeypatch.setattr(
        nuitka_builder_module,
        "_RUNTIME_PACK_DIR",
        str(tmp_path / "runtime-packs"),
    )

    builder = object.__new__(NuitkaBuilder)
    builder._engine_fingerprint_cache = ""
    first = builder._engine_content_fingerprint()
    metadata.write_text("changed", encoding="utf-8")
    builder._engine_fingerprint_cache = ""

    assert builder._engine_content_fingerprint() == first


def test_runtime_engine_fingerprint_ignores_editor_backups(tmp_path, monkeypatch):
    package_root = tmp_path / "Infernux"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    backup = package_root / "bindings.pyi.bak"
    backup.write_text("old", encoding="utf-8")
    fake_package = SimpleNamespace(__file__=str(package_root / "__init__.py"))
    monkeypatch.setitem(sys.modules, "Infernux", fake_package)
    monkeypatch.setattr(
        nuitka_builder_module,
        "_RUNTIME_PACK_DIR",
        str(tmp_path / "runtime-packs"),
    )

    builder = object.__new__(NuitkaBuilder)
    builder._engine_fingerprint_cache = ""
    first = builder._engine_content_fingerprint()
    backup.write_text("changed", encoding="utf-8")
    builder._engine_fingerprint_cache = ""

    assert builder._engine_content_fingerprint() == first


def test_player_compile_fingerprint_ignores_post_build_packaging_code(tmp_path, monkeypatch):
    package_root = tmp_path / "Infernux"
    (package_root / "engine").mkdir(parents=True)
    package_init = package_root / "__init__.py"
    package_init.write_text("", encoding="utf-8")
    runtime_source = package_root / "engine" / "player_bootstrap.py"
    runtime_source.write_text("RUNTIME = 1\n", encoding="utf-8")
    audit_source = package_root / "engine" / "player_package_audit.py"
    audit_source.write_text("AUDIT = 1\n", encoding="utf-8")
    builder_source = package_root / "engine" / "game_builder.py"
    builder_source.write_text("BUILDER = 1\n", encoding="utf-8")
    compile_builder_source = package_root / "engine" / "nuitka_builder.py"
    compile_builder_source.write_text("COMPILE_RULE = 1\n", encoding="utf-8")
    fake_package = SimpleNamespace(__file__=str(package_init))
    monkeypatch.setitem(sys.modules, "Infernux", fake_package)
    monkeypatch.setattr(
        nuitka_builder_module,
        "_RUNTIME_PACK_DIR",
        str(tmp_path / "runtime-packs"),
    )

    builder = object.__new__(NuitkaBuilder)
    builder._engine_fingerprint_cache = ""
    native_dir = package_root / "lib"
    native_dir.mkdir()
    builder._native_payload_dir = lambda: native_dir
    first = builder._player_compile_input_fingerprint()
    audit_source.write_text("AUDIT = 2\n", encoding="utf-8")
    builder_source.write_text("BUILDER = 2\n", encoding="utf-8")
    compile_builder_source.write_text("COMPILE_RULE = 2\n", encoding="utf-8")
    builder._engine_fingerprint_cache = ""

    assert builder._player_compile_input_fingerprint() != first

    compile_builder_source.write_text("COMPILE_RULE = 1\n", encoding="utf-8")
    builder._engine_fingerprint_cache = ""
    assert builder._player_compile_input_fingerprint() == first

    runtime_source.write_text("RUNTIME = 2\n", encoding="utf-8")
    builder._engine_fingerprint_cache = ""
    assert builder._player_compile_input_fingerprint() != first


def test_runtime_pack_fingerprint_tracks_generated_boot_and_keeps_environment_inputs(
    tmp_path,
):
    staged_entry = tmp_path / "boot.py"
    staged_entry.write_text("BOOT = 1\n", encoding="utf-8")
    builder = object.__new__(NuitkaBuilder)
    builder._staged_entry = str(staged_entry)
    builder._staging_dir = str(tmp_path / "staging")
    builder.extra_requirements_files = []
    builder._builder_environment_fingerprint = lambda: b"builder-env"
    builder._player_compile_input_fingerprint = lambda: "player-code"

    first = builder._runtime_pack_fingerprint(
        ["python", str(staged_entry), "--jobs=8"]
    )
    staged_entry.write_text("BOOT = 2\n", encoding="utf-8")

    assert builder._runtime_pack_fingerprint(
        ["python", str(staged_entry), "--jobs=8"]
    ) != first


def test_runtime_engine_fingerprint_tracks_loaded_native_payload(tmp_path, monkeypatch):
    import Infernux

    package_root = tmp_path / "Infernux"
    package_root.mkdir()
    package_init = package_root / "__init__.py"
    package_init.write_text("", encoding="utf-8")
    native_root = tmp_path / "native"
    native_root.mkdir()
    native_module = (
        "_Infernux.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_Infernux.so"
    )
    (native_root / native_module).write_bytes(b"module")
    bootstrap_module = (
        "_InfernuxBootstrap.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_InfernuxBootstrap.so"
    )
    (native_root / bootstrap_module).write_bytes(b"bootstrap")
    companion = native_root / (
        "InfernuxRendererRuntime.dll"
        if sys.platform == "win32"
        else "libInfernuxRendererRuntime.so"
    )
    companion.write_bytes(b"first")
    monkeypatch.setattr(Infernux, "__file__", str(package_init))
    monkeypatch.setenv("INFERNUX_NATIVE_MODULE_DIR", str(native_root))
    monkeypatch.setattr(
        nuitka_builder_module,
        "_RUNTIME_PACK_DIR",
        str(tmp_path / "runtime-packs"),
    )

    builder = object.__new__(NuitkaBuilder)
    builder._engine_fingerprint_cache = ""
    first = builder._engine_content_fingerprint()
    companion.write_bytes(b"second")
    builder._engine_fingerprint_cache = ""

    assert builder._engine_content_fingerprint() != first


def test_native_payload_injection_uses_one_override_and_overwrites_stale_files(
    tmp_path, monkeypatch
):
    native_root = tmp_path / "native"
    native_root.mkdir()
    native_module = (
        "_Infernux.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_Infernux.so"
    )
    companion = (
        "InfernuxRendererRuntime.dll"
        if sys.platform == "win32"
        else "libInfernuxRendererRuntime.so"
    )
    (native_root / native_module).write_bytes(b"current-module")
    bootstrap_module = (
        "_InfernuxBootstrap.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_InfernuxBootstrap.so"
    )
    (native_root / bootstrap_module).write_bytes(b"bootstrap-module")
    if sys.platform == "win32":
        # A stale short-name module can be left by Nuitka discovery. The
        # ABI-tagged build output must win and replace it atomically.
        (native_root / "_Infernux.pyd").write_bytes(b"stale-source-module")
    (native_root / companion).write_bytes(b"current-runtime")
    foundation = (
        native_root / "InfernuxFoundation.dll"
        if sys.platform == "win32"
        else native_root / "libInfernuxFoundation.so"
    )
    foundation.write_bytes(b"foundation")
    python_runtime = native_root / "python313.dll" if sys.platform == "win32" else None
    if python_runtime is not None:
        python_runtime.write_bytes(b"python-runtime")
        (native_root / "zlib.dll").write_bytes(b"runtime-zlib")
    monkeypatch.setenv("INFERNUX_NATIVE_MODULE_DIR", str(native_root))

    dist = tmp_path / "boot.dist"
    package_lib = dist / "Infernux" / "lib"
    package_lib.mkdir(parents=True)
    (package_lib / native_module).write_bytes(b"stale-module")
    canonical_module = "_Infernux.pyd" if sys.platform == "win32" else "_Infernux.so"
    (package_lib / canonical_module).write_bytes(b"stale-canonical-module")
    (package_lib / companion).write_bytes(b"stale-runtime")
    if sys.platform == "win32":
        (dist / companion).write_bytes(b"stale-root-runtime")
        for legacy_name in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_FILES:
            (dist / legacy_name).write_bytes(b"stale root legacy")
            (package_lib / legacy_name).write_bytes(b"stale package legacy")

    builder = object.__new__(NuitkaBuilder)
    builder._inject_native_libs(str(dist))

    assert (package_lib / native_module).read_bytes() == b"current-module"
    assert (package_lib / canonical_module).read_bytes() == b"current-module"
    assert (package_lib / companion).read_bytes() == b"current-runtime"
    if sys.platform == "win32":
        assert not (dist / companion).exists()
        assert (dist / "_InfernuxBootstrap.pyd").read_bytes() == b"bootstrap-module"
        assert (dist / "InfernuxFoundation.dll").read_bytes() == b"foundation"
        assert (dist / "python313.dll").read_bytes() == b"python-runtime"
        assert not (package_lib / "python313.dll").exists()
        assert (package_lib / "zlib.dll").read_bytes() == b"runtime-zlib"
        assert not (dist / "zlib.dll").exists()
        for legacy_name in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_FILES:
            assert not (dist / legacy_name).exists()
            assert not (package_lib / legacy_name).exists()


def test_player_native_payload_rejects_explicit_editor_runtime(tmp_path, monkeypatch):
    native_root = tmp_path / "native"
    native_root.mkdir()
    native_module = (
        "_Infernux.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_Infernux.so"
    )
    bootstrap_module = (
        "_InfernuxBootstrap.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_InfernuxBootstrap.so"
    )
    legacy_runtime = (
        "InfernuxRuntime.dll"
        if sys.platform == "win32"
        else "libInfernuxRuntime.so"
    )
    (native_root / native_module).write_bytes(b"editor-module")
    (native_root / bootstrap_module).write_bytes(b"bootstrap-module")
    (native_root / legacy_runtime).write_bytes(b"editor-runtime")
    monkeypatch.setenv("INFERNUX_NATIVE_MODULE_DIR", str(native_root))

    with pytest.raises(RuntimeError, match="static Release Player runtime"):
        NuitkaBuilder._native_payload_dir()


def test_player_native_payload_selects_static_source_build_sibling(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    package_root = repository / "python" / "Infernux"
    package_root.mkdir(parents=True)
    package_init = package_root / "__init__.py"
    package_init.write_text("", encoding="utf-8")
    build_root = repository / "out" / "build"
    editor_root = build_root / "windows-msvc-dev" / "python-sync"
    player_root = build_root / "windows-msvc-release" / "python-sync"
    editor_root.mkdir(parents=True)
    player_root.mkdir(parents=True)
    native_module = (
        "_Infernux.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_Infernux.so"
    )
    bootstrap_module = (
        "_InfernuxBootstrap.cp313-win_amd64.pyd"
        if sys.platform == "win32"
        else "_InfernuxBootstrap.so"
    )
    legacy_runtime = (
        "InfernuxRuntime.dll"
        if sys.platform == "win32"
        else "libInfernuxRuntime.so"
    )
    for root in (editor_root, player_root):
        (root / native_module).write_bytes(root.name.encode("utf-8"))
        (root / bootstrap_module).write_bytes(b"bootstrap-module")
    (editor_root / legacy_runtime).write_bytes(b"editor-runtime")

    import Infernux

    monkeypatch.delenv("INFERNUX_NATIVE_MODULE_DIR", raising=False)
    monkeypatch.setattr(Infernux, "__file__", str(package_init))
    monkeypatch.setattr(
        nuitka_builder_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(__file__=str(editor_root / native_module)),
    )

    assert NuitkaBuilder._native_payload_dir() == player_root


def test_runtime_compatibility_key_ignores_branding_and_managed_dependencies(
    tmp_path,
):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("numba==1\nfastmcp==2\n", encoding="utf-8")

    def make_builder(*, icon: str, custom: list[str] | None = None):
        builder = object.__new__(NuitkaBuilder)
        builder.extra_requirements_files = [str(requirements)]
        builder.extra_include_packages = list(custom or [])
        builder.extra_include_data = [str(tmp_path / "project-specific-data")]
        builder.raw_copy_packages = ["numpy"]
        builder.runtime_support_packages = ["numba", "llvmlite", "mcp", "fastmcp"]
        builder.icon_path = icon
        builder.console_mode = "disable"
        builder.lto = True
        builder.player_module = True
        builder._player_compile_input_fingerprint = lambda: "core-runtime"
        return builder

    first = make_builder(icon=str(tmp_path / "first.ico"))
    second = make_builder(icon=str(tmp_path / "second.ico"))
    assert first._runtime_pack_compatibility_key() == second._runtime_pack_compatibility_key()

    custom = make_builder(icon="", custom=["project_runtime_dependency"])
    custom_key = custom._runtime_pack_compatibility_key()
    assert custom_key != first._runtime_pack_compatibility_key()

    requirements.write_text(
        "numba==99\nfastmcp==99\nproject-runtime-dependency==3\n",
        encoding="utf-8",
    )
    assert custom._runtime_pack_compatibility_key() != custom_key


def test_player_compile_fingerprint_is_source_install_layout_equivalent(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source" / "Infernux"
    installed_root = tmp_path / "site-packages" / "Infernux"
    for package_root in (source_root, installed_root):
        (package_root / "engine").mkdir(parents=True)
        (package_root / "lib").mkdir()
        (package_root / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
        (package_root / "engine" / "player_runtime.py").write_text(
            "RUNTIME = 1\n", encoding="utf-8"
        )
    (installed_root / "engine" / "player_runtime.pyi").write_text(
        "RUNTIME: int\n", encoding="utf-8"
    )
    (installed_root / "engine" / "__pycache__").mkdir()
    (installed_root / "engine" / "__pycache__" / "player_runtime.pyc").write_bytes(
        b"layout-specific-bytecode"
    )
    generated = installed_root / "_runtime_packs" / ("a" * 64)
    generated.mkdir(parents=True)
    (generated / "Runtime.inxrt").write_bytes(b"generated cache")

    builder = object.__new__(NuitkaBuilder)
    builder._engine_fingerprint_cache = ""
    monkeypatch.setattr(builder, "_load_runtime_hash_state", lambda: {})
    monkeypatch.setattr(builder, "_store_runtime_hash_state", lambda _state: None)
    active_root = source_root
    monkeypatch.setattr(builder, "_native_payload_dir", lambda: active_root / "lib")

    fake_package = SimpleNamespace(__file__=str(source_root / "__init__.py"))
    monkeypatch.setitem(sys.modules, "Infernux", fake_package)
    source_fingerprint = builder._player_compile_input_fingerprint()

    active_root = installed_root
    fake_package.__file__ = str(installed_root / "__init__.py")
    builder._engine_fingerprint_cache = ""
    installed_fingerprint = builder._player_compile_input_fingerprint()

    assert installed_fingerprint == source_fingerprint


def test_cleanup_temp_removes_boot_directory_synchronously(tmp_path):
    boot_dir = tmp_path / "_build_temp"
    boot_dir.mkdir()
    boot_script = boot_dir / "boot.py"
    boot_script.write_text("print('temporary')", encoding="utf-8")

    GameBuilder._cleanup_temp(str(boot_script))

    assert not boot_dir.exists()


def test_player_boot_file_helpers_replace_shutil_without_losing_atomic_copy(tmp_path):
    source = tmp_path / "source.json"
    destination = tmp_path / "nested" / "destination.json"
    source.write_bytes(b'{"complete": true}')

    game_builder_module._copy_player_file_atomic(str(source), str(destination))

    assert destination.read_bytes() == source.read_bytes()
    assert not list(destination.parent.glob("destination.json.*.tmp"))

    tree = tmp_path / "cache-tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "payload.bin").write_bytes(b"payload")
    game_builder_module._remove_player_path(str(tree))
    assert not tree.exists()


def test_player_output_transaction_keeps_previous_product_until_commit(tmp_path):
    output = tmp_path / "已发布游戏"
    output.mkdir()
    previous = output / "TestGame.exe"
    previous.write_bytes(b"previous")
    builder = _make_builder(tmp_path, output)

    builder._begin_output_transaction()
    staging = Path(builder.output_dir)
    assert previous.read_bytes() == b"previous"
    (staging / "TestGame.exe").write_bytes(b"current")
    (staging / "TestGame_Data").mkdir()

    published = Path(builder._commit_output_transaction(str(staging)))

    assert published == output.resolve()
    assert (published / "TestGame.exe").read_bytes() == b"current"
    assert not Path(str(output) + ".infernux-build.lock").exists()


def test_player_output_transaction_abort_removes_only_private_staging(tmp_path):
    output = tmp_path / "Player"
    output.mkdir()
    (output / "TestGame.exe").write_bytes(b"previous")
    builder = _make_builder(tmp_path, output)

    builder._begin_output_transaction()
    staging = Path(builder.output_dir)
    (staging / "partial.bin").write_bytes(b"partial")
    builder._abort_output_transaction()

    assert (output / "TestGame.exe").read_bytes() == b"previous"
    assert not staging.exists()
    assert builder.output_dir == str(output.resolve())


def test_player_output_transaction_restores_previous_product_on_publish_failure(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "Player"
    output.mkdir()
    (output / "TestGame.exe").write_bytes(b"previous")
    builder = _make_builder(tmp_path, output)
    builder._begin_output_transaction()
    staging = Path(builder.output_dir)
    (staging / "TestGame.exe").write_bytes(b"current")
    native_replace = game_builder_module.os.replace

    def fail_staging_publish(source, destination):
        if Path(source) == staging and Path(destination) == output:
            raise PermissionError("read-only publication target")
        return native_replace(source, destination)

    monkeypatch.setattr(game_builder_module.os, "replace", fail_staging_publish)

    with pytest.raises(PermissionError, match="read-only"):
        builder._commit_output_transaction(str(staging))

    assert (output / "TestGame.exe").read_bytes() == b"previous"
    builder._abort_output_transaction()


def test_player_output_transaction_rejects_live_concurrent_owner(tmp_path, monkeypatch):
    output = tmp_path / "Player"
    lock = Path(str(output) + ".infernux-build.lock")
    lock.write_text(json.dumps({"pid": 123, "staging": ""}), encoding="utf-8")
    builder = _make_builder(tmp_path, output)
    monkeypatch.setattr(
        game_builder_module,
        "_player_builder_process_is_alive",
        lambda pid: pid == 123,
    )

    with pytest.raises(RuntimeError, match="Another Player build owns"):
        builder._begin_output_transaction()

    assert lock.is_file()


def test_player_output_transaction_recovers_stale_interrupted_staging(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "Player"
    stale = tmp_path / ".Player.infernux-staging-44-55"
    stale.mkdir()
    (stale / "partial.bin").write_bytes(b"partial")
    lock = Path(str(output) + ".infernux-build.lock")
    lock.write_text(
        json.dumps({"pid": 44, "staging": str(stale)}),
        encoding="utf-8",
    )
    builder = _make_builder(tmp_path, output)
    monkeypatch.setattr(
        game_builder_module,
        "_player_builder_process_is_alive",
        lambda _pid: False,
    )

    builder._begin_output_transaction()

    assert not stale.exists()
    builder._abort_output_transaction()


def test_player_output_transaction_restores_stale_previous_product(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "Player"
    stale = tmp_path / ".Player.infernux-staging-44-55"
    backup = tmp_path / ".Player.infernux-previous-44-55"
    stale.mkdir()
    (stale / "partial.bin").write_bytes(b"partial")
    backup.mkdir()
    (backup / "TestGame.exe").write_bytes(b"previous")
    lock = Path(str(output) + ".infernux-build.lock")
    lock.write_text(
        json.dumps(
            {
                "pid": 44,
                "staging": str(stale),
                "backup": str(backup),
            }
        ),
        encoding="utf-8",
    )
    builder = _make_builder(tmp_path, output)
    monkeypatch.setattr(
        game_builder_module,
        "_player_builder_process_is_alive",
        lambda _pid: False,
    )

    builder._begin_output_transaction()

    assert (output / "TestGame.exe").read_bytes() == b"previous"
    assert not stale.exists()
    assert not backup.exists()
    builder._abort_output_transaction()


def test_release_output_copies_player_host_and_keeps_module(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    host = tmp_path / "InfernuxPlayerHost.exe"
    host.write_bytes(b"host")
    monkeypatch.setattr(builder, "_player_host_path", lambda: str(host))
    builder.debug_mode = False
    dist = tmp_path / "staging" / "generic.dist"
    dist.mkdir(parents=True)
    module_name = "_InfernuxPlayer.pyd" if sys.platform == "win32" else "_InfernuxPlayer.so"
    (dist / module_name).write_bytes(b"player module")

    final_dir = Path(builder._organize_output(str(dist)))

    game_name = "TestGame.exe" if sys.platform == "win32" else "TestGame"
    assert (final_dir / game_name).read_bytes() == b"host"
    assert (final_dir / module_name).read_bytes() == b"player module"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PlayerHost path")
def test_player_host_resolves_from_player_runtime_resources(tmp_path, monkeypatch):
    package = tmp_path / "Infernux"
    engine = package / "engine"
    runtime = package / "resources" / "player_runtime"
    engine.mkdir(parents=True)
    runtime.mkdir(parents=True)
    host = runtime / "InfernuxPlayerHost.exe"
    host.write_bytes(b"host")
    monkeypatch.setattr(game_builder_module, "__file__", str(engine / "game_builder.py"))

    builder = _make_builder(tmp_path, tmp_path / "build_output")

    assert Path(builder._player_host_path()) == host


def test_current_player_layout_uses_one_renamed_executable(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    host = tmp_path / "InfernuxPlayerHost.exe"
    host.write_bytes(b"host")
    monkeypatch.setattr(builder, "_player_host_path", lambda: str(host))

    dist = tmp_path / "staging" / "generic.dist"
    dist.mkdir(parents=True)
    module_name = "_InfernuxPlayer.pyd" if sys.platform == "win32" else "_InfernuxPlayer.so"
    (dist / module_name).write_bytes(b"player module")
    (dist / "python313.dll").write_bytes(b"python")
    final_dir = Path(builder._organize_output(str(dist)))
    (final_dir / "Data").mkdir()
    (final_dir / "Data" / "BuildManifest.json").write_text("{}", encoding="utf-8")

    builder._organize_player_layout(str(final_dir))
    builder._write_output_marker(str(final_dir))

    data_root = final_dir / "TestGame_Data"
    executable_name = _player_executable_name()
    assert (final_dir / executable_name).read_bytes() == b"host"
    assert [path.name for path in final_dir.iterdir() if path.name == executable_name] == [
        executable_name
    ]
    assert (final_dir / module_name).read_bytes() == b"player module"
    assert (final_dir / "python313.dll").read_bytes() == b"python"
    assert (final_dir / GameBuilder.OUTPUT_MARKER_FILENAME).is_file()
    assert (data_root / "BuildManifest.json").is_file()
    assert not (data_root / "Runtime").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PE resource contract")
def test_windows_player_host_icon_is_replaced_with_project_icon(tmp_path):
    host = (
        Path(__file__).parents[1]
        / "Infernux"
        / "resources"
        / "player_runtime"
        / "InfernuxPlayerHost.exe"
    )
    if not host.is_file():
        pytest.skip("InfernuxPlayerHost.exe is not built")
    project_icon = (
        Path(__file__).parents[1]
        / "Infernux"
        / "resources"
        / "icons"
        / "icon.png"
    )
    executable = tmp_path / "BrandedGame.exe"
    shutil.copy2(host, executable)

    GameBuilder._apply_windows_executable_icon(str(executable), str(project_icon))

    kernel32 = ctypes.windll.kernel32
    kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel32.FindResourceW.restype = wintypes.HRSRC
    kernel32.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
    kernel32.SizeofResource.restype = wintypes.DWORD
    module = kernel32.LoadLibraryExW(str(executable), None, 0x00000002)
    assert module
    try:
        group = kernel32.FindResourceW(module, ctypes.c_void_p(101), ctypes.c_void_p(14))
        assert group
        assert kernel32.SizeofResource(module, group) >= 6 + 14 * 6
    finally:
        kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
        kernel32.FreeLibrary(module)


def test_build_branding_assets_are_manifested_and_packed(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    icon = tmp_path / "project-icon.png"
    splash = tmp_path / "opening.png"
    icon.write_bytes(b"icon")
    splash.write_bytes(b"splash")
    builder.icon_path = str(icon)
    builder.splash_items = [
        {
            "type": "image",
            "path": str(splash),
            "duration": 2.0,
            "fade_in": 0.25,
            "fade_out": 0.5,
        }
    ]
    final_dir = tmp_path / "dist"
    settings = final_dir / "Data" / "ProjectSettings"
    settings.mkdir(parents=True)
    (settings / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}), encoding="utf-8"
    )

    builder._process_build_icon(str(final_dir))
    builder._process_splash_items(str(final_dir))
    builder._generate_manifest(str(final_dir))
    _write_player_executable(final_dir, b"player")
    builder._organize_player_layout(str(final_dir))

    manifest = json.loads(
        (final_dir / "TestGame_Data" / "BuildManifest.json").read_text(encoding="utf-8")
    )
    assert manifest["icon_path"] == "Branding/icon.png"
    assert manifest["splash_items"][0]["path"] == "Splash/opening.png"
    builder._pack_content_archive(str(final_dir))
    header = read_manifest(final_dir / "TestGame_Data" / "Content.inxpkg")
    names = {entry["path"] for entry in header["files"]}
    assert "Branding/icon.png" in names
    assert "Splash/opening.png" in names


def test_requirements_install_is_skipped_when_content_is_unchanged(tmp_path, monkeypatch):
    state_root = tmp_path / "requirements-state"
    monkeypatch.setattr(nuitka_builder_module, "_REQUIREMENTS_STATE_DIR", str(state_root))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.32.0\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(nuitka_builder_module.subprocess, "check_call", lambda command: calls.append(command))

    nuitka_builder_module._install_requirements_files(sys.executable, [str(requirements)])
    nuitka_builder_module._install_requirements_files(sys.executable, [str(requirements)])

    assert len(calls) == 1


def test_player_cleanup_preserves_engine_icon_resources(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    icons = final_dir / "Infernux" / "resources" / "icons"
    icons.mkdir(parents=True)
    camera_icon = icons / "gizmo_camera.png"
    light_icon = icons / "gizmo_light.png"
    camera_icon.write_bytes(b"camera")
    light_icon.write_bytes(b"light")

    builder._cleanup_dist(str(final_dir))

    assert camera_icon.read_bytes() == b"camera"
    assert light_icon.read_bytes() == b"light"


def test_player_cleanup_preserves_project_meta_and_removes_engine_meta(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    project_meta = final_dir / "Data" / "Assets" / "Scripts" / "player.py.meta"
    engine_meta = final_dir / "Infernux" / "resources" / "shaders" / "lit.frag.meta"
    project_meta.parent.mkdir(parents=True)
    engine_meta.parent.mkdir(parents=True)
    project_meta.write_text("project", encoding="utf-8")
    engine_meta.write_text("engine", encoding="utf-8")

    builder._cleanup_dist(str(final_dir))

    assert project_meta.read_text(encoding="utf-8") == "project"
    assert not engine_meta.exists()


def test_player_cleanup_preserves_sourceless_runtime_dependency_bytecode(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    numpy_init = final_dir / "numpy" / "__init__.pyc"
    numpy_core = final_dir / "numpy" / "_core" / "__init__.pyc"
    cache_file = final_dir / "numpy" / "__pycache__" / "stale.pyc"
    numpy_core.parent.mkdir(parents=True)
    cache_file.parent.mkdir(parents=True)
    numpy_init.write_bytes(b"runtime package")
    numpy_core.write_bytes(b"runtime core")
    cache_file.write_bytes(b"cache")

    builder._cleanup_dist(str(final_dir))

    assert numpy_init.read_bytes() == b"runtime package"
    assert numpy_core.read_bytes() == b"runtime core"
    assert not cache_file.parent.exists()


def test_player_cleanup_keeps_bootstrap_root_and_package_full_module(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    package_module = final_dir / "Infernux" / "lib" / "_Infernux.pyd"
    root_module = final_dir / "_Infernux.pyd"
    bootstrap_module = final_dir / "_InfernuxBootstrap.pyd"
    package_module.parent.mkdir(parents=True)
    package_module.write_bytes(b"native module")
    root_module.write_bytes(b"native module")
    bootstrap_module.write_bytes(b"bootstrap module")

    builder._cleanup_dist(str(final_dir))

    assert not root_module.exists()
    assert package_module.read_bytes() == b"native module"
    assert bootstrap_module.read_bytes() == b"bootstrap module"


def test_player_cleanup_removes_redundant_library_resources(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    package_font = final_dir / "Infernux" / "resources" / "fonts" / "engine.otf"
    library_font = final_dir / "Data" / "Library" / "Resources" / "fonts" / "engine.otf"
    package_font.parent.mkdir(parents=True)
    library_font.parent.mkdir(parents=True)
    package_font.write_bytes(b"package-font")
    library_font.write_bytes(b"duplicate-font")

    builder._cleanup_dist(str(final_dir))

    assert package_font.read_bytes() == b"package-font"
    assert not library_font.parent.parent.exists()


def test_game_data_includes_render_effect_artifacts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    source_effect = project / "Assets" / "Rendering" / "Bloom.effect"
    source_effect.parent.mkdir(parents=True)
    source_effect.write_text(
        '{"$schema":"infernux.render_effect","name":"Bloom"}',
        encoding="utf-8",
    )
    scene = project / "Assets" / "Main.scene"
    scene.write_text(
        json.dumps(
            {
                "effect": {
                    "$type": "asset_ref",
                    "guid": "effect-guid",
                    "path_hint": "Assets/Rendering/Bloom.effect",
                }
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        project
        / "Library"
        / "Artifacts"
        / "RenderEffect"
        / "bloom.inxeffect"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "$schema": "infernux.render_effect_artifact",
                "source_hash": hashlib.sha256(source_effect.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(
                project,
                source_effect,
                "effect-guid",
                "Library/Artifacts/RenderEffect/bloom.inxeffect",
                "RenderEffect",
            ),
        ],
    )
    final_dir = tmp_path / "dist"

    builder._copy_game_data(str(final_dir))
    shipped = (
        final_dir
        / "Data"
        / "Library"
        / "Artifacts"
        / "RenderEffect"
        / artifact.name
    )

    assert shipped.read_text(encoding="utf-8") == artifact.read_text(encoding="utf-8")

    _write_player_executable(final_dir)
    builder._organize_player_layout(str(final_dir))
    builder._pack_content_archive(str(final_dir))
    header = read_manifest(
        final_dir / "TestGame_Data" / builder._CONTENT_ARCHIVE_FILENAME
    )
    assert "Library/Artifacts/RenderEffect/bloom.inxeffect" in {
        entry["path"] for entry in header["files"]
    }


def test_full_build_cook_rejects_an_empty_runtime_asset_selection(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    _write_asset_index(Path(builder.project_path), [])
    builder._full_build_validated = True
    data_dir = tmp_path / "dist" / "Data"
    data_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="selected no runtime assets"):
        builder._copy_cooked_assets(str(data_dir))


def test_player_cook_uses_asset_index_snapshot_after_live_index_invalidation(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    builder._asset_index_entries()
    (project / "Library" / "AssetIndex.json").unlink()
    data_dir = tmp_path / "dist" / "Data"

    builder._copy_cooked_assets(str(data_dir))

    assert (data_dir / "Assets" / "Main.scene").is_file()


def test_player_stages_project_shader_as_packed_runtime_glsl(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    shader = project / "Assets" / "Shaders" / "Surface.frag"
    shader.parent.mkdir(parents=True, exist_ok=True)
    shader.write_text('ShaderInfo { Name "Test/Surface" Type Fragment }\n', encoding="utf-8")
    entry = _asset_index_entry(project, shader, "shader-guid", "", "Shader")
    entry["metadata"]["metadata"]["type"] = {"value": "fragment"}
    builder._cooked_asset_entries = {"shader-guid": entry}
    builder._runtime_artifact_bindings = {}
    builder._runtime_artifact_source_paths = set()
    data_dir = tmp_path / "dist" / "Data"

    builder._stage_library_runtime_documents(str(data_dir))

    artifact = (
        data_dir
        / "Library"
        / "Artifacts"
        / "Blob"
        / "shader-guid.frag"
    )
    assert artifact.read_bytes() == shader.read_bytes()
    assert builder._runtime_artifact_bindings[
        "Library/Artifacts/Blob/shader-guid.frag"
    ]["source_path"] == "Assets/Shaders/Surface.frag"


def test_content_archive_keeps_only_catalog_staged_project_glsl(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    data = tmp_path / "dist" / "TestGame_Data"
    runtime_shader = data / "Library" / "Artifacts" / "Blob" / "shader-guid.frag"
    runtime_shader.parent.mkdir(parents=True)
    runtime_shader.write_text(
        'ShaderInfo { Name "Test/Surface" Type Fragment }\n',
        encoding="utf-8",
    )

    builder._pack_content_archive(str(tmp_path / "dist"))

    archive = data / builder._CONTENT_ARCHIVE_FILENAME
    names = {entry["path"] for entry in read_manifest(archive)["files"]}
    assert "Library/Artifacts/Blob/shader-guid.frag" in names


def test_content_archive_rejects_project_glsl_outside_runtime_catalog_area(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    shader = tmp_path / "dist" / "TestGame_Data" / "Assets" / "Shaders" / "Loose.frag"
    shader.parent.mkdir(parents=True)
    shader.write_text("#version 450\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="authoring/source files"):
        builder._pack_content_archive(str(tmp_path / "dist"))


def test_game_data_replaces_current_texture_source_with_library_artifact(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    source = project / "Assets" / "Art" / "Smoke.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"texture source")
    artifact_relative = "Library/Artifacts/Texture/texture-guid.inxtex"
    artifact = project / artifact_relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(
        b"INXTEXTURE" + b"\x04\x03\x02\x01" + (16).to_bytes(4, "little") + b"a" * 16 + b"payload"
    )
    _write_texture_asset_index(project, source, "texture-guid", artifact_relative)

    final_dir = tmp_path / "dist"
    builder._copy_game_data(str(final_dir))
    _write_player_executable(final_dir)
    builder._organize_player_layout(str(final_dir))
    builder._pack_content_archive(str(final_dir))

    archive = final_dir / "TestGame_Data" / builder._CONTENT_ARCHIVE_FILENAME
    names = {entry["path"] for entry in read_manifest(archive)["files"]}
    assert artifact_relative in names
    assert "Assets/Art/Smoke.png" not in names


def test_game_data_rejects_missing_current_library_artifact(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    source = project / "Assets" / "Art" / "Missing.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"texture source")
    _write_texture_asset_index(
        project,
        source,
        "missing-guid",
        "Library/Artifacts/Texture/missing-guid.inxtex",
    )

    with pytest.raises(RuntimeError, match="Library artifact selection failed"):
        builder._copy_game_data(str(tmp_path / "dist"))


def test_game_data_includes_particle_artifacts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    _reference_particle_graph(Path(builder.project_path), "smoke")
    artifact = (
        Path(builder.project_path)
        / "Library"
        / "Artifacts"
        / "Particle"
        / "smoke.inxparticle"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    graph_path = Path(builder.project_path) / "Assets" / "VFX" / "smoke.particlegraph"
    _write_particle_artifact(artifact, graph_path)
    final_dir = tmp_path / "dist"

    builder._copy_game_data(str(final_dir))
    shipped = (
        final_dir
        / "Data"
        / "Library"
        / "Artifacts"
        / "Particle"
        / artifact.name
    )
    assert shipped.read_text(encoding="utf-8") == artifact.read_text(encoding="utf-8")
    runtime_index = json.loads(
        (shipped.parent / builder._PARTICLE_RUNTIME_INDEX_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert runtime_index == {
        "$schema": "infernux.particle_runtime_index",
        "entries": [
            {
                "guid": hashlib.md5(b"smoke").hexdigest(),
                "path_hint": "Assets/VFX/smoke.particlegraph",
                "stable_id": "smoke",
            }
        ],
    }

    _write_player_executable(final_dir)
    builder._organize_player_layout(str(final_dir))
    builder._pack_content_archive(str(final_dir))
    header = read_manifest(
        final_dir / "TestGame_Data" / builder._CONTENT_ARCHIVE_FILENAME
    )
    names = {entry["path"] for entry in header["files"]}
    assert "Library/Artifacts/Particle/smoke.inxparticle" in names
    assert "Library/Artifacts/Particle/RuntimeIndex.json" in names


def test_game_data_excludes_unreachable_particle_artifacts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    _reference_particle_graph(project, "reachable")
    artifact_root = project / "Library" / "Artifacts" / "Particle"
    artifact_root.mkdir(parents=True, exist_ok=True)
    graph_path = project / "Assets" / "VFX" / "reachable.particlegraph"
    _write_particle_artifact(artifact_root / "reachable.inxparticle", graph_path)
    (artifact_root / "unreachable.inxparticle").write_text(
        '{"$schema":"infernux.particle_artifact"}',
        encoding="utf-8",
    )

    final_dir = tmp_path / "dist"
    builder._copy_game_data(str(final_dir))

    shipped = final_dir / "Data" / "Library" / "Artifacts" / "Particle"
    assert (shipped / "reachable.inxparticle").is_file()
    assert not (shipped / "unreachable.inxparticle").exists()


def test_game_data_compiles_missing_particle_artifact(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    _reference_particle_graph(project, "missing")
    (project / "Library" / "Artifacts" / "Particle").mkdir(parents=True, exist_ok=True)
    guid = hashlib.md5(b"missing").hexdigest()

    builder._copy_game_data(str(tmp_path / "dist"))

    shipped = (
        tmp_path
        / "dist"
        / "Data"
        / "Library"
        / "Artifacts"
        / "Particle"
        / f"{guid}.inxparticle"
    )
    assert shipped.is_file()
    payload = json.loads(shipped.read_text(encoding="utf-8"))
    assert payload["$schema"] == "infernux.particle_artifact"
    assert payload["source_hash"]


def test_game_data_fails_when_particle_source_cannot_compile(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    graph_path = _reference_particle_graph(project, "broken")
    graph_path.write_text("{not-json", encoding="utf-8")
    guid = hashlib.md5(b"broken").hexdigest()
    scene_path = project / "Assets" / "Main.scene"
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene_path, "scene-guid", "", "Scene"),
            _asset_index_entry(project, graph_path, guid, "", "ParticleGraph"),
        ],
    )
    (project / "Library" / "Artifacts" / "Particle").mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="Library particle artifact compile failed"):
        builder._copy_game_data(str(tmp_path / "dist"))


def test_game_data_keeps_particle_artifacts_that_share_graph_stable_id(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    first = _reference_particle_graph(project, "shared")
    second = project / "Assets" / "VFX" / "copy.particlegraph"
    second.write_text(
        ParticleGraphAsset(stable_id="shared", name="copy").canonical_json(),
        encoding="utf-8",
    )
    guid1 = hashlib.md5(b"shared").hexdigest()
    guid2 = hashlib.md5(b"copy").hexdigest()
    scene_path = project / "Assets" / "Main.scene"
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene_path, "scene-guid", "", "Scene"),
            _asset_index_entry(project, first, guid1, "", "ParticleGraph"),
            _asset_index_entry(project, second, guid2, "", "ParticleGraph"),
        ],
    )
    artifact_root = project / "Library" / "Artifacts" / "Particle"
    _write_particle_artifact(artifact_root / f"{guid1}.inxparticle", first)
    _write_particle_artifact(artifact_root / f"{guid2}.inxparticle", second)
    (artifact_root / "RuntimeIndex.json").write_text(
        json.dumps(
            {
                "$schema": "infernux.particle_runtime_index",
                "entries": [
                    {
                        "guid": guid1,
                        "path_hint": "Assets/VFX/shared.particlegraph",
                        "stable_id": "shared",
                    },
                    {
                        "guid": guid2,
                        "path_hint": "Assets/VFX/copy.particlegraph",
                        "stable_id": "shared",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    builder._copy_game_data(str(tmp_path / "dist"))
    shipped = tmp_path / "dist" / "Data" / "Library" / "Artifacts" / "Particle"
    assert (shipped / f"{guid1}.inxparticle").is_file()
    assert (shipped / f"{guid2}.inxparticle").is_file()


def test_validate_artifact_rejects_particle_owned_by_another_guid(tmp_path):
    project = _make_project(tmp_path)
    source = project / "Assets" / "VFX" / "owned.particlegraph"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        ParticleGraphAsset(stable_id="owned", name="owned").canonical_json(),
        encoding="utf-8",
    )
    guid = hashlib.md5(b"owned").hexdigest()
    artifact = project / "Library" / "Artifacts" / "Particle" / "owned.inxparticle"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "$schema": "infernux.particle_artifact",
                "source_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "source_hash": _particle_source_hash(source),
                "kernel_ir": {"emitters": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeArtifactError, match="belongs to"):
        validate_artifact(
            project,
            _asset_index_entry(project, source, guid, "", "ParticleGraph"),
            artifact,
        )


def test_particle_runtime_index_is_not_required_without_particle_references(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    (project / "Library" / "AssetIndex.json").unlink()
    data_dir = tmp_path / "dist" / "Data"

    builder._copy_reachable_particle_artifacts(str(data_dir))

    assert not (data_dir / "Library" / "Artifacts" / "Particle").exists()


def test_particle_runtime_index_remains_required_for_reachable_graph(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    _reference_particle_graph(project, "smoke")
    (project / "Library" / "AssetIndex.json").unlink()

    with pytest.raises(RuntimeError, match="current Library/AssetIndex.json"):
        builder._copy_reachable_particle_artifacts(str(tmp_path / "dist" / "Data"))


def test_particle_script_is_not_a_player_build_source(tmp_path):
    source = tmp_path / "Smoke.particle.py"
    source.write_text(
        "from Infernux.particle import ParticleScript\n"
        "class Smoke(ParticleScript):\n"
        "    stable_id = 'smoke-script'\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="ParticleScript is Preview/Future"):
        GameBuilder._particle_source_stable_id(str(source))


def test_game_data_collects_all_imported_particle_interface_artifacts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    _reference_particle_graph(project, "interfaces")
    particle_artifact = (
        project / "Library" / "Artifacts" / "Particle" / "interfaces.inxparticle"
    )
    particle_artifact.parent.mkdir(parents=True, exist_ok=True)

    def sample(opcode: str, stable_id: str) -> dict:
        return {"opcode": opcode, "immediates": [["interface", stable_id]]}

    emitters = [
        {
            "data_interfaces": [
                {
                    "kind": "vector_field",
                    "stable_id": "wind",
                    "texture": {"guid": "field-guid", "path_hint": ""},
                },
                {
                    "kind": "vector_field",
                    "stable_id": "unused",
                    "texture": {"guid": "unused-guid", "path_hint": ""},
                },
                {
                    "kind": "sdf_volume",
                    "stable_id": "collision",
                    "texture": {"guid": "sdf-guid", "path_hint": ""},
                },
            ],
            "init": {"instructions": []},
            "update": {
                "instructions": [
                    sample("sample_vector_field", "wind"),
                    sample("collide_sdf_position", "collision"),
                    sample("collide_sdf_velocity", "collision"),
                ]
            },
            "rendering": {"instructions": []},
        }
    ]
    graph_path = project / "Assets" / "VFX" / "interfaces.particlegraph"
    _write_particle_artifact(particle_artifact, graph_path, emitters=emitters)
    texture_artifact = (
        project / "Library" / "Artifacts" / "Texture" / "field-guid.inxtex"
    )
    unused_artifact = (
        project / "Library" / "Artifacts" / "Texture" / "unused-guid.inxtex"
    )
    sdf_artifact = (
        project / "Library" / "Artifacts" / "Texture" / "sdf-guid.inxtex"
    )
    texture_sources = {
        "field-guid": (project / "Assets" / "VFX" / "field.png", texture_artifact, "a" * 16),
        "sdf-guid": (project / "Assets" / "VFX" / "sdf.png", sdf_artifact, "b" * 16),
        "unused-guid": (project / "Assets" / "VFX" / "unused.png", unused_artifact, "c" * 16),
    }
    texture_entries = []
    for guid, (source, path, content_hash) in texture_sources.items():
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(guid.encode("ascii"))
        _write_texture_artifact(path, guid.encode("ascii"), content_hash)
        texture_entries.append(
            _asset_index_entry(project, source, guid, f"Library/Artifacts/Texture/{path.name}", "Texture", content_hash)
        )
    particle_guid = hashlib.md5(b"interfaces").hexdigest()
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, project / "Assets" / "Main.scene", "scene-guid", "", "Scene"),
            _asset_index_entry(project, graph_path, particle_guid, "", "ParticleGraph"),
            *texture_entries,
        ],
    )

    final_dir = tmp_path / "dist"
    builder._copy_game_data(str(final_dir))

    shipped = final_dir / "Data" / "Library" / "Artifacts"
    assert (shipped / "Texture" / texture_artifact.name).read_bytes() == texture_artifact.read_bytes()
    assert (shipped / "Texture" / sdf_artifact.name).read_bytes() == sdf_artifact.read_bytes()
    assert (shipped / "Texture" / unused_artifact.name).read_bytes() == unused_artifact.read_bytes()


def test_payload_manifest_rejects_missing_native_packages(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    _prepare_runtime_catalog_inputs(
        builder,
        final_dir,
        include_runtime=False,
        include_content=False,
    )

    with pytest.raises(RuntimeError, match="required native package is missing"):
        builder._write_payload_manifest(str(final_dir))


def test_content_archive_replaces_loose_project_files(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data = final_dir / "TestGame_Data"
    runtime_assets = data / "RuntimeAssets"
    settings = data / "ProjectSettings"
    runtime_assets.mkdir(parents=True)
    settings.mkdir(parents=True)
    (runtime_assets / "Main.inxscene").write_bytes(b"compiled scene")
    (runtime_assets / "Player.inxscript").write_bytes(b"compiled script")
    build_manifest = data / "BuildManifest.json"
    build_manifest.write_text('{"game_name": "TestGame"}', encoding="utf-8")
    (settings / "BuildSettings.json").write_text('{"scenes": ["RuntimeAssets/Main.inxscene"]}', encoding="utf-8")
    (settings / "mcp_capabilities.json").write_text('{"enabled": true}', encoding="utf-8")
    (settings / "agent_tools.json").write_text('{"tools": []}', encoding="utf-8")

    builder._pack_content_archive(str(final_dir))

    archive_path = data / "Content.inxpkg"
    assert archive_path.is_file()
    assert build_manifest.is_file()
    assert not runtime_assets.exists()
    assert not settings.exists()
    header = read_manifest(archive_path)
    assert {entry["path"] for entry in header["files"]} == {
        "ProjectSettings/BuildSettings.json",
        "RuntimeAssets/Main.inxscene",
        "RuntimeAssets/Player.inxscript",
    }


def test_player_cook_excludes_editor_project_settings_before_archive(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    settings = project / "ProjectSettings"
    editor_files = (
        ".infernux-engine-lock.json",
        "agent_tools.json",
        "mcp_capabilities.json",
        "requirements.txt",
        "EditorSettings.json",
        "GameView.ini",
    )
    for filename in editor_files:
        (settings / filename).write_text("editor-only", encoding="utf-8")
    (settings / "PhysicsSettings.json").write_text("{}", encoding="utf-8")
    (settings / "TagLayerSettings.json").write_text("{}", encoding="utf-8")
    (settings / "FutureEditorService.json").write_text(
        "{}",
        encoding="utf-8",
    )

    final_dir = tmp_path / "dist"
    builder._copy_game_data(str(final_dir))

    staged_settings = final_dir / "Data" / "ProjectSettings"
    assert (staged_settings / "BuildSettings.json").is_file()
    assert (staged_settings / "PhysicsSettings.json").is_file()
    assert (staged_settings / "TagLayerSettings.json").is_file()
    assert not (staged_settings / "FutureEditorService.json").exists()
    assert all(not (staged_settings / filename).exists() for filename in editor_files)


def test_content_archive_excludes_editor_settings_and_metadata(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data = final_dir / "TestGame_Data"
    settings = data / "ProjectSettings"
    assets = data / "Assets"
    settings.mkdir(parents=True)
    assets.mkdir(parents=True)
    (settings / "BuildSettings.json").write_text(
        '{"scenes": []}', encoding="utf-8"
    )
    editor_files = (
        ".infernux-engine-lock.json",
        "agent_tools.json",
        "mcp_capabilities.json",
        "requirements.txt",
        "EditorSettings.json",
        "GameView.ini",
    )
    for filename in editor_files:
        (settings / filename).write_text("editor-only", encoding="utf-8")
    (assets / "editor-only.meta").write_text("metadata", encoding="utf-8")

    builder._pack_content_archive(str(final_dir))

    archive = data / builder._CONTENT_ARCHIVE_FILENAME
    assert {entry["path"] for entry in read_manifest(archive)["files"]} == {
        "ProjectSettings/BuildSettings.json",
    }
    assert not (assets / "editor-only.meta").exists()
    assert all(not (settings / filename).exists() for filename in editor_files)


def test_content_archive_preserves_native_packages_and_player_controls(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    data = tmp_path / "dist" / "TestGame_Data"
    ordinary = data / "RuntimeAssets" / "Main.inxscene"
    runtime = data / builder._RUNTIME_ARCHIVE_FILENAME
    parallel = data / "Modules" / builder._PARALLEL_ARCHIVE_FILENAME
    extra_native = data / "Nested" / "Other.inxpkg"
    build_manifest = data / "BuildManifest.json"
    player_manifest = data / builder._PLAYER_MANIFEST_FILENAME
    ordinary.parent.mkdir(parents=True)
    parallel.parent.mkdir(parents=True)
    extra_native.parent.mkdir(parents=True)
    ordinary.write_bytes(b"compiled scene")
    runtime.write_bytes(b"runtime package")
    parallel.write_bytes(b"parallel module")
    extra_native.write_bytes(b"another native package")
    build_manifest.write_text('{"game_name":"TestGame"}', encoding="utf-8")
    player_manifest.write_text('{}', encoding="utf-8")

    builder._pack_content_archive(str(tmp_path / "dist"))

    header = read_manifest(data / builder._CONTENT_ARCHIVE_FILENAME)
    assert header["compression_profile"] == "release"
    names = {entry["path"] for entry in header["files"]}
    assert names == {"RuntimeAssets/Main.inxscene"}
    assert ordinary.exists() is False
    assert runtime.read_bytes() == b"runtime package"
    assert parallel.read_bytes() == b"parallel module"
    assert extra_native.read_bytes() == b"another native package"
    assert build_manifest.read_text(encoding="utf-8") == '{"game_name":"TestGame"}'
    assert player_manifest.read_text(encoding="utf-8") == "{}"


def test_content_archive_excludes_build_inputs_and_rewrites_project_paths(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    data = tmp_path / "dist" / "TestGame_Data"
    scene = data / "Assets" / "Main.scene"
    settings = data / "ProjectSettings"
    referenced = Path(builder.project_path) / "Assets" / "VFX" / "Smoke.particlegraph"
    scene.parent.mkdir(parents=True)
    settings.mkdir(parents=True)
    scene.write_text(
        json.dumps(
            {
                "graph": str(referenced),
                "external": "D:/External/Shared.asset",
            }
        ),
        encoding="utf-8",
    )
    (settings / "requirements.txt").write_text("numpy", encoding="utf-8")
    (settings / ".infernux-engine-lock.json").write_text(
        json.dumps({"project_root": builder.project_path}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="direct or serialized runtime payloads"):
        builder._pack_content_archive(str(tmp_path / "dist"))


def test_content_archive_excludes_particle_authoring_and_keeps_aot(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    data = tmp_path / "dist" / "TestGame_Data"
    graph = data / "Assets" / "VFX" / "Smoke.particlegraph"
    script = data / "Assets" / "VFX" / "Future.particle.py"
    artifact = data / "Library" / "Artifacts" / "Particle" / "smoke.inxparticle"
    runtime_index = artifact.parent / builder._PARTICLE_RUNTIME_INDEX_FILENAME
    graph.parent.mkdir(parents=True)
    artifact.parent.mkdir(parents=True)
    graph.write_text('{"$schema":"infernux.particle_graph"}', encoding="utf-8")
    graph.with_suffix(graph.suffix + ".meta").write_text("graph-meta", encoding="utf-8")
    script.write_text("# Preview ParticleScript", encoding="utf-8")
    script.with_suffix(".pyc").write_bytes(b"preview-bytecode")
    script.with_suffix(script.suffix + ".meta").write_text("script-meta", encoding="utf-8")
    cache = script.parent / "__pycache__"
    cache.mkdir()
    (cache / "Future.particle.cpython-312.pyc").write_bytes(b"preview-bytecode")
    (cache / "Future.particle.cpython-312.opt-2.pyc").write_bytes(b"optimized-preview")
    artifact.write_text('{"$schema":"infernux.particle_artifact"}', encoding="utf-8")
    runtime_index.write_text(
        '{"$schema":"infernux.particle_runtime_index","entries":[]}',
        encoding="utf-8",
    )

    builder._pack_content_archive(str(tmp_path / "dist"))

    header = read_manifest(data / builder._CONTENT_ARCHIVE_FILENAME)
    names = {entry["path"] for entry in header["files"]}
    assert "Library/Artifacts/Particle/smoke.inxparticle" in names
    assert "Library/Artifacts/Particle/RuntimeIndex.json" in names
    assert not any(name.casefold().endswith(".particlegraph") for name in names)
    assert not any(".particle." in name.casefold() for name in names)
    assert not graph.exists()
    assert not script.exists()


def test_content_archive_rejects_plaintext_project_scripts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    script = tmp_path / "dist" / "TestGame_Data" / "RuntimeAssets" / "Player.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('source')", encoding="utf-8")

    with pytest.raises(RuntimeError, match="authoring/source files"):
        builder._pack_content_archive(str(tmp_path / "dist"))


def test_content_archive_keeps_compiled_scripts_and_cooked_runtime_documents(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    data = tmp_path / "dist" / "TestGame_Data"
    assets = data / "Assets" / "Scripts"
    assets.mkdir(parents=True)
    (assets / "Player.pyc").write_bytes(b"compiled-player")
    (assets / "Player.py.meta").write_text("editor metadata", encoding="utf-8")
    document = data / "Library" / "Artifacts" / "Document" / "scene-guid.scene"
    document.parent.mkdir(parents=True)
    document.write_text(
        '{"name":"Main","objects":[]}', encoding="utf-8"
    )
    (data / "BuildManifest.json").write_text(
        '{"game_name":"TestGame"}', encoding="utf-8"
    )

    builder._pack_content_archive(str(tmp_path / "dist"))

    header = read_manifest(data / builder._CONTENT_ARCHIVE_FILENAME)
    names = {entry["path"] for entry in header["files"]}
    assert "Assets/Scripts/Player.pyc" in names
    assert "Library/Artifacts/Document/scene-guid.scene" in names
    assert not any(name.endswith(".meta") for name in names)
    assert not (assets / "Player.pyc").exists()
    assert not document.exists()


def test_core_runtime_archive_replaces_loose_numpy_and_resources(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    numpy_file = final_dir / "numpy" / "core.py"
    numpy_init = final_dir / "numpy" / "__init__.pyc"
    numpy_core_init = final_dir / "numpy" / "_core" / "__init__.pyc"
    numpy_dll = final_dir / "numpy.libs" / "openblas.dll"
    numpy_header = final_dir / "numpy" / "_core" / "include" / "numpy" / "arrayobject.h"
    numpy_example = final_dir / "numpy" / "random" / "_examples" / "extending.pyx"
    numpy_stub = final_dir / "numpy" / "typing" / "_array_like.pyi"
    numpy_tests_extension = (
        final_dir / "numpy" / "_core" / "_multiarray_tests.cp313-win_amd64.pyd"
    )
    numpy_api_changes = final_dir / "numpy" / "ma" / "API_CHANGES.txt"
    numpy_license = final_dir / "numpy" / "LICENSE.txt"
    font = final_dir / "Infernux" / "resources" / "fonts" / "engine.otf"
    gizmo_icon = final_dir / "Infernux" / "resources" / "icons" / "gizmo_camera.png"
    editor_icon = final_dir / "Infernux" / "resources" / "icons" / "file.png"
    numpy_file.parent.mkdir(parents=True)
    numpy_core_init.parent.mkdir(parents=True, exist_ok=True)
    numpy_dll.parent.mkdir(parents=True)
    numpy_header.parent.mkdir(parents=True)
    numpy_example.parent.mkdir(parents=True)
    numpy_stub.parent.mkdir(parents=True)
    numpy_tests_extension.parent.mkdir(parents=True, exist_ok=True)
    numpy_api_changes.parent.mkdir(parents=True, exist_ok=True)
    numpy_license.parent.mkdir(parents=True, exist_ok=True)
    font.parent.mkdir(parents=True)
    gizmo_icon.parent.mkdir(parents=True)
    numpy_file.write_text("VALUE = 1", encoding="utf-8")
    numpy_init.write_bytes(b"numpy package")
    numpy_core_init.write_bytes(b"numpy core package")
    numpy_dll.write_bytes(b"dll")
    numpy_header.write_text("header", encoding="utf-8")
    numpy_example.write_text("source", encoding="utf-8")
    numpy_stub.write_text("stub", encoding="utf-8")
    numpy_tests_extension.write_bytes(b"test extension")
    numpy_api_changes.write_text("history", encoding="utf-8")
    numpy_license.write_text("license", encoding="utf-8")
    font.write_bytes(b"font")
    gizmo_icon.write_bytes(b"gizmo")
    editor_icon.write_bytes(b"editor")
    stray_exe = (
        final_dir
        / "Infernux"
        / "resources"
        / "player_runtime"
        / "stray.exe"
    )
    stray_exe.parent.mkdir(parents=True)
    stray_exe.write_bytes(b"must not enter Runtime.inxrt")
    (final_dir / "TestGame_Data").mkdir(parents=True)

    builder._pack_core_runtime_archive(str(final_dir))

    archive = final_dir / "TestGame_Data" / "Runtime.inxrt"
    assert archive.is_file()
    assert not (final_dir / "numpy").exists()
    assert not (final_dir / "numpy.libs").exists()
    assert not (final_dir / "Infernux" / "resources").exists()
    header = read_manifest(archive)
    assert header["compression_profile"] == "release"
    assert {entry["path"] for entry in header["files"]} == {
        "Infernux/resources/fonts/engine.otf",
        "Infernux/resources/icons/gizmo_camera.png",
        "numpy.libs/openblas.dll",
        "numpy/__init__.pyc",
        "numpy/_core/__init__.pyc",
        "numpy/core.py",
        "numpy/LICENSE.txt",
    }


def test_core_runtime_archive_excludes_editor_icon_payloads(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    data_root.mkdir(parents=True)
    icons = final_dir / "Infernux" / "resources" / "icons"
    icons.mkdir(parents=True)
    for name in ("icon.png", "gizmo_camera.png", "file.png", "scene.png"):
        (icons / name).write_bytes(name.encode("ascii"))

    builder._pack_core_runtime_archive(str(final_dir))

    names = {entry["path"] for entry in read_manifest(data_root / "Runtime.inxrt")["files"]}
    assert "Infernux/resources/icons/icon.png" in names
    assert "Infernux/resources/icons/gizmo_camera.png" in names
    assert "Infernux/resources/icons/file.png" not in names
    assert "Infernux/resources/icons/scene.png" not in names


@pytest.mark.parametrize(
    ("debug_mode", "expected_profile"),
    ((False, "release"), (True, "development")),
)
def test_player_runtime_and_content_archives_share_build_profile(
    tmp_path,
    monkeypatch,
    debug_mode,
    expected_profile,
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    builder.debug_mode = debug_mode
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    runtime_source = final_dir / "Infernux" / "resources" / "runtime.bin"
    content_source = data_root / "RuntimeAssets" / "Main.inxscene"
    runtime_source.parent.mkdir(parents=True)
    content_source.parent.mkdir(parents=True)
    runtime_source.write_bytes(b"runtime")
    content_source.write_text("{}", encoding="utf-8")
    observed: dict[str, str] = {}
    native_write_pack = game_builder_module.write_pack

    def record_profile(files, destination, **kwargs):
        observed[Path(destination).name] = kwargs.get("profile", "development")
        return native_write_pack(files, destination, **kwargs)

    monkeypatch.setattr(game_builder_module, "write_pack", record_profile)

    builder._pack_core_runtime_archive(str(final_dir))
    builder._pack_content_archive(str(final_dir))

    assert observed == {
        "Runtime.inxrt": expected_profile,
        "Content.inxpkg": expected_profile,
    }


def test_pack_content_archive_yields_the_editor_thread(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data" / "RuntimeAssets"
    data_root.mkdir(parents=True)
    for index in range(12):
        (data_root / f"Item{index}.bin").write_bytes(b"payload")
    yields: list[int] = []
    reports: list[tuple[str, float]] = []
    monkeypatch.setattr(
        game_builder_module,
        "_yield_editor_thread",
        lambda: yields.append(1),
    )

    builder._pack_content_archive(
        str(final_dir),
        on_progress=lambda message, fraction: reports.append((message, fraction)),
    )

    assert len(yields) >= 12
    assert any("Packing project content" in message for message, _fraction in reports)
    assert any(message == "Compressing project content" for message, _fraction in reports)
    assert any(
        message.startswith("Finalizing packed project content")
        for message, _fraction in reports
    )
    assert (final_dir / "TestGame_Data" / "Content.inxpkg").is_file()


def test_pack_content_archive_finalizes_staged_trees_in_bulk(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data = final_dir / "TestGame_Data"
    assets = data / "Assets" / "Art"
    library = data / "Library" / "Artifacts"
    nested_logs = data / "Assets" / "Logs"
    assets.mkdir(parents=True)
    library.mkdir(parents=True)
    nested_logs.mkdir(parents=True)
    (data / "Logs").mkdir()
    (data / "Modules").mkdir()
    (assets / "a.inxtex").write_bytes(b"asset")
    (library / "b.inxtex").write_bytes(b"library")
    (library / "keep.inxpkg").write_bytes(b"nested-pack")
    (nested_logs / "keep.txt").write_text("nested-log", encoding="utf-8")
    (data / "Logs" / "build.log").write_text("ok", encoding="utf-8")
    (data / "Modules" / "Parallel.inxmod").write_bytes(b"mod")
    (data / "Modules" / "leftover.bin").write_bytes(b"drop")
    (data / "Runtime.inxrt").write_bytes(b"runtime")
    (data / "BuildManifest.json").write_text("{}", encoding="utf-8")
    (data / builder.OUTPUT_MARKER_FILENAME).write_text("marker", encoding="utf-8")

    removed: list[str] = []
    real_remove = os.remove

    def track_remove(path):
        removed.append(os.fspath(path))
        return real_remove(path)

    monkeypatch.setattr(os, "remove", track_remove)
    reports: list[str] = []

    builder._pack_content_archive(
        str(final_dir),
        on_progress=lambda message, _fraction: reports.append(message),
    )

    assert (data / "Content.inxpkg").is_file()
    assert not (data / "Assets" / "Art").exists()
    assert not (library / "b.bin").exists()
    assert (data / "Assets" / "Logs" / "keep.txt").read_text(encoding="utf-8") == "nested-log"
    assert (data / "Library" / "Artifacts" / "keep.inxpkg").read_bytes() == b"nested-pack"
    assert (data / "Logs" / "build.log").read_text(encoding="utf-8") == "ok"
    assert (data / "Modules" / "Parallel.inxmod").read_bytes() == b"mod"
    assert not (data / "Modules" / "leftover.bin").exists()
    assert (data / "Runtime.inxrt").read_bytes() == b"runtime"
    assert (data / "BuildManifest.json").read_text(encoding="utf-8") == "{}"
    assert (data / builder.OUTPUT_MARKER_FILENAME).read_text(encoding="utf-8") == "marker"
    assert any(message == "Finalizing packed project content (Assets)" for message in reports)
    assert any(message == "Finalizing packed project content (Library)" for message in reports)
    packed_unlinks = [
        path
        for path in removed
        if path.endswith(
            (os.path.join("Art", "a.inxtex"), os.path.join("Artifacts", "b.inxtex"))
        )
    ]
    assert packed_unlinks == []


def test_pack_content_archive_finalize_honors_cancel(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data = final_dir / "TestGame_Data"
    (data / "Assets").mkdir(parents=True)
    (data / "Library").mkdir(parents=True)
    (data / "Assets" / "a.inxtex").write_bytes(b"asset")
    (data / "Library" / "b.inxtex").write_bytes(b"library")
    cancel_event = threading.Event()

    def _progress(message, _fraction):
        if message == "Finalizing packed project content":
            cancel_event.set()

    with pytest.raises(BuildCancelled):
        builder._pack_content_archive(
            str(final_dir),
            on_progress=_progress,
            cancel_event=cancel_event,
        )

    assert (data / "Content.inxpkg").is_file()
    assert (data / "Assets" / "a.inxtex").is_file()
    assert (data / "Library" / "b.inxtex").is_file()


def _write_scene_material_audio_reachability_fixture(
    builder: GameBuilder,
    *,
    include_unreachable: bool,
) -> dict[str, Path]:
    project = Path(builder.project_path)
    assets = project / "Assets"
    scene = assets / "Main.scene"
    material = assets / "Materials" / "Bird.mat"
    audio = assets / "Audio" / "Wing.wav"
    unreachable = assets / "Unused.mat"
    material.parent.mkdir(parents=True, exist_ok=True)
    audio.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text(
        json.dumps(
            {
                "material": {
                    "$type": "asset_ref",
                    "guid": "material-guid",
                    "path_hint": "Assets/Materials/Bird.mat",
                }
            }
        ),
        encoding="utf-8",
    )
    material.write_text(
        json.dumps(
            {
                "audio": {
                    "$type": "asset_ref",
                    "guid": "audio-guid",
                    "path_hint": "Assets/Audio/Wing.wav",
                }
            }
        ),
        encoding="utf-8",
    )
    audio.write_bytes(b"reachable audio")
    entries = [
        _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
        _asset_index_entry(project, material, "material-guid", "", "Material"),
        _asset_index_entry(project, audio, "audio-guid", "", "Audio"),
    ]
    if include_unreachable:
        unreachable.write_text("{}", encoding="utf-8")
        entries.append(
            _asset_index_entry(
                project,
                unreachable,
                "unused-guid",
                "",
                "Material",
            )
        )
    _write_asset_index(project, entries)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    return {
        "scene": scene,
        "material": material,
        "audio": audio,
        "unreachable": unreachable,
    }


def test_payload_manifest_rejects_reachable_direct_scene_material_and_audio(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    sources = _write_scene_material_audio_reachability_fixture(
        builder,
        include_unreachable=False,
    )
    final_dir = tmp_path / "dist"
    data_root = _prepare_runtime_catalog_inputs(builder, final_dir)
    write_pack(
        (
            ("Assets/Main.scene", sources["scene"]),
            ("Assets/Materials/Bird.mat", sources["material"]),
            ("Assets/Audio/Wing.wav", sources["audio"]),
        ),
        data_root / builder._CONTENT_ARCHIVE_FILENAME,
    )
    _install_runtime_identity_bindings(
        builder,
        {
            "Assets/Main.scene": (
                "scene-guid",
                "runtime_loader_requires_serialized_document",
            ),
            "Assets/Materials/Bird.mat": (
                "material-guid",
                "runtime_loader_requires_serialized_document",
            ),
            "Assets/Audio/Wing.wav": (
                "audio-guid",
                "runtime_audio_backend_requires_encoded_stream",
            ),
        },
    )

    with pytest.raises(
        RuntimeError,
        match="direct or serialized runtime payloads",
    ):
        builder._write_payload_manifest(str(final_dir))


def test_all_imported_assets_enter_the_player_product_closure(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    material = project / "Assets" / "Materials" / "Bird.mat"
    audio = project / "Assets" / "Audio" / "Wing.wav"
    unused = project / "Assets" / "Unused.mat"
    material.parent.mkdir(parents=True, exist_ok=True)
    audio.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "components": [
                            {"data": {"materials": ["material-guid"]}}
                        ]
                    }
                ],
                "document_id": "not-an-indexed-resource",
            }
        ),
        encoding="utf-8",
    )
    material.write_text(
        json.dumps({"preview_audio_guid": "audio-guid"}),
        encoding="utf-8",
    )
    audio.write_bytes(b"reachable audio")
    unused.write_text("{}", encoding="utf-8")
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(
                project, material, "material-guid", "", "Material"
            ),
            _asset_index_entry(project, audio, "audio-guid", "", "Audio"),
            _asset_index_entry(project, unused, "unused-guid", "", "Material"),
        ],
    )
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    indexed, reachable = builder._project_asset_reachability_evidence()

    assert indexed == {
        "assets/main.scene",
        "assets/materials/bird.mat",
        "assets/audio/wing.wav",
        "assets/unused.mat",
    }
    assert reachable == {
        "assets/main.scene",
        "assets/materials/bird.mat",
        "assets/audio/wing.wav",
        "assets/unused.mat",
    }


def test_project_render_scripts_make_declared_shader_ids_reachable(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    script = project / "Assets" / "Scripts" / "CustomPipeline.py"
    declared_shader = project / "Assets" / "Shaders" / "Declared.frag"
    fullscreen_shader = project / "Assets" / "Shaders" / "Fullscreen.frag"
    unused_shader = project / "Assets" / "Shaders" / "Unused.frag"
    script.parent.mkdir(parents=True, exist_ok=True)
    declared_shader.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "components": [
                            {"type_id": "python:script-guid:type-guid:Scripts.CustomPipeline:Pipeline"}
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    script.write_text(
        "class Effect:\n"
        "    def get_shader_list(self):\n"
        "        return ['Project/Declared']\n"
        "    def setup(self, render_pass):\n"
        "        render_pass.fullscreen_quad('Project/Fullscreen')\n",
        encoding="utf-8",
    )
    declared_shader.write_text("void main() {}\n", encoding="utf-8")
    fullscreen_shader.write_text("void main() {}\n", encoding="utf-8")
    unused_shader.write_text("void main() {}\n", encoding="utf-8")

    scene_entry = _asset_index_entry(project, scene, "scene-guid", "", "Scene")
    script_entry = _asset_index_entry(project, script, "script-guid", "", "PythonScript")
    declared_entry = _asset_index_entry(
        project, declared_shader, "declared-shader-guid", "", "Shader"
    )
    declared_entry["metadata"]["metadata"]["shader_id"] = {
        "value": "Project/Declared"
    }
    fullscreen_entry = _asset_index_entry(
        project, fullscreen_shader, "fullscreen-shader-guid", "", "Shader"
    )
    fullscreen_entry["metadata"]["metadata"]["shader_id"] = {
        "value": "Project/Fullscreen"
    }
    unused_entry = _asset_index_entry(
        project, unused_shader, "unused-shader-guid", "", "Shader"
    )
    unused_entry["metadata"]["metadata"]["shader_id"] = {
        "value": "Project/Unused"
    }
    entries = [
        scene_entry,
        script_entry,
        declared_entry,
        fullscreen_entry,
        unused_entry,
    ]
    _write_asset_index(project, entries)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(entries)

    assert set(selected) == {
        "scene-guid",
        "script-guid",
        "declared-shader-guid",
        "fullscreen-shader-guid",
        "unused-shader-guid",
    }


def test_copy_cooked_assets_catalogs_builtin_shaders_without_duplicating_them(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    shader = project / "Library" / "Resources" / "shaders" / "standard.vert"
    shader.parent.mkdir(parents=True, exist_ok=True)
    shader.write_text("void main() {}\n", encoding="utf-8")
    scene.write_text(json.dumps({"shader": "builtin-shader-guid"}), encoding="utf-8")
    scene_entry = _asset_index_entry(
        project,
        scene,
        "scene-guid",
        "",
        "Scene",
    )
    shader_entry = _asset_index_entry(
        project,
        shader,
        "builtin-shader-guid",
        "",
        "Shader",
    )
    shader_entry["read_only"] = True
    _write_asset_index(project, [scene_entry, shader_entry])
    data_dir = tmp_path / "dist" / "Data"

    builder._copy_cooked_assets(str(data_dir))

    assert (data_dir / "Assets" / "Main.scene").is_file()
    assert not (data_dir / "Library" / "Resources" / "shaders" / "standard.vert").exists()
    assert "builtin-shader-guid" in builder._cooked_asset_entries

    builder._cooked_asset_entries = {"builtin-shader-guid": shader_entry}
    builder._runtime_artifact_bindings = {}
    builder._write_runtime_asset_records(str(tmp_path / "dist"))
    records = json.loads(
        (data_dir / "Library" / "RuntimeAssetRecords.json").read_text(encoding="utf-8")
    )
    shader_record = next(
        item for item in records["entries"] if item["guid"] == "builtin-shader-guid"
    )
    assert shader_record["runtime_artifacts"] == [
        {
            "package": "Runtime.inxrt",
            "runtime_path": "Infernux/resources/shaders/standard.vert",
            "runtime_artifact_id": game_builder_module.runtime_artifact_id(
                "Runtime.inxrt", "Infernux/resources/shaders/standard.vert"
            ),
        }
    ]


def test_platform_cook_packages_reachable_builtin_resources_in_content(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    shader = project / "Library" / "Resources" / "shaders" / "standard.vert"
    shader.parent.mkdir(parents=True, exist_ok=True)
    shader.write_text("void main() {}\n", encoding="utf-8")
    scene.write_text(json.dumps({"shader": "builtin-shader-guid"}), encoding="utf-8")
    scene_entry = _asset_index_entry(project, scene, "scene-guid", "", "Scene")
    shader_entry = _asset_index_entry(
        project,
        shader,
        "builtin-shader-guid",
        "",
        "Shader",
    )
    shader_entry["read_only"] = True
    _write_asset_index(project, [scene_entry, shader_entry])
    final_dir = tmp_path / "dist"
    data_dir = final_dir / "Data"

    builder._copy_cooked_assets(
        str(data_dir),
        package_builtin_resources=True,
    )

    packaged_shader = data_dir / "Infernux/resources/shaders/standard.vert"
    assert packaged_shader.read_text(encoding="utf-8") == "void main() {}\n"

    builder._cooked_asset_entries = {"builtin-shader-guid": shader_entry}
    builder._runtime_artifact_bindings = {}
    builder._write_runtime_asset_records(
        str(final_dir),
        package_builtin_resources=True,
    )
    records = json.loads(
        (data_dir / "Library" / "RuntimeAssetRecords.json").read_text(
            encoding="utf-8"
        )
    )
    shader_record = next(
        item for item in records["entries"] if item["guid"] == "builtin-shader-guid"
    )
    assert shader_record["runtime_artifacts"] == [
        {
            "package": "Content.inxpkg",
            "runtime_path": "Infernux/resources/shaders/standard.vert",
            "runtime_artifact_id": game_builder_module.runtime_artifact_id(
                "Content.inxpkg", "Infernux/resources/shaders/standard.vert"
            ),
        }
    ]

    data_root = final_dir / "TestGame_Data"
    data_root.parent.mkdir(parents=True, exist_ok=True)
    data_dir.rename(data_root)
    (data_root / "Assets/Main.scene").unlink()
    (data_root / "Assets").rmdir()
    builder._pack_content_archive(
        str(final_dir),
        package_builtin_resources=True,
    )
    manifest = read_manifest(data_root / "Content.inxpkg")
    assert "Infernux/resources/shaders/standard.vert" in {
        entry["path"] for entry in manifest["files"]
    }


def test_package_resource_shader_keeps_guid_identity_in_headless_build(
    tmp_path, monkeypatch
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    package_resources = tmp_path / "installed-engine" / "Infernux" / "resources"
    shader = package_resources / "shaders" / "particle_sprite.vert"
    shader.parent.mkdir(parents=True, exist_ok=True)
    shader.write_text("void main() {}\n", encoding="utf-8")
    scene.write_text(json.dumps({"shader": "particle-shader-guid"}), encoding="utf-8")
    scene_entry = _asset_index_entry(project, scene, "scene-guid", "", "Scene")
    shader_entry = _asset_index_entry(
        project,
        shader,
        "particle-shader-guid",
        "",
        "Shader",
    )
    shader_entry["read_only"] = True
    shader_entry["metadata"]["metadata"]["shader_id"] = {
        "type": "string",
        "value": "Particle Sprite",
    }
    monkeypatch.setattr(
        game_builder_module._resources,
        "get_package_resources_path",
        lambda: str(package_resources),
    )
    monkeypatch.setattr(
        game_builder_module._resources,
        "resources_path",
        str(package_resources),
    )
    entries = [scene_entry, shader_entry]
    _write_asset_index(project, entries)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(entries)

    assert set(selected) == {"scene-guid", "particle-shader-guid"}
    builder._copy_cooked_assets(str(tmp_path / "copy" / "Data"))
    assert (tmp_path / "copy" / "Data" / "Assets" / "Main.scene").is_file()
    assert not (
        tmp_path
        / "copy"
        / "Data"
        / "Library"
        / "Resources"
        / "shaders"
        / "particle_sprite.vert"
    ).exists()
    builder._cooked_asset_entries = {
        "particle-shader-guid": selected["particle-shader-guid"]
    }
    builder._runtime_artifact_bindings = {}
    final_dir = tmp_path / "dist"
    builder._write_runtime_asset_records(str(final_dir))
    records = json.loads(
        (final_dir / "Data" / "Library" / "RuntimeAssetRecords.json").read_text(
            encoding="utf-8"
        )
    )
    shader_record = next(
        item for item in records["entries"] if item["guid"] == "particle-shader-guid"
    )
    assert shader_record["runtime_path"] == (
        "Library/Resources/shaders/particle_sprite.vert"
    )
    assert shader_record["runtime_artifacts"][0]["runtime_path"] == (
        "Infernux/resources/shaders/particle_sprite.vert"
    )
    assert shader_record["metadata"]["metadata"]["shader_id"]["value"] == (
        "Particle Sprite"
    )


def test_source_checkout_shader_keeps_identity_when_wheel_builds_project(
    tmp_path, monkeypatch
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    checkout_resources = (
        tmp_path / "source-checkout" / "python" / "Infernux" / "resources"
    )
    installed_resources = (
        tmp_path / "project-venv" / "site-packages" / "Infernux" / "resources"
    )
    shader = checkout_resources / "shaders" / "particle_sprite.vert"
    shader.parent.mkdir(parents=True, exist_ok=True)
    shader.write_text(
        'ShaderInfo { Name "Particle Sprite" Capabilities [ParticleSprite] }\n',
        encoding="utf-8",
    )
    installed_resources.mkdir(parents=True, exist_ok=True)
    scene.write_text("{}\n", encoding="utf-8")
    scene_entry = _asset_index_entry(project, scene, "scene-guid", "", "Scene")
    shader_entry = _asset_index_entry(
        project,
        shader,
        "particle-shader-guid",
        "",
        "Shader",
    )
    shader_entry["read_only"] = True
    shader_entry["metadata"]["metadata"]["shader_id"] = {
        "type": "string",
        "value": "Particle Sprite",
    }
    monkeypatch.setattr(
        game_builder_module._resources,
        "get_package_resources_path",
        lambda: str(installed_resources),
    )
    monkeypatch.setattr(
        game_builder_module._resources,
        "resources_path",
        str(installed_resources),
    )
    entries = [scene_entry, shader_entry]
    _write_asset_index(project, entries)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(entries)

    assert set(selected) == {"scene-guid", "particle-shader-guid"}
    builder._copy_cooked_assets(str(tmp_path / "copy" / "Data"))
    assert "particle-shader-guid" in builder._cooked_asset_entries
    builder._cooked_asset_entries = {
        "particle-shader-guid": selected["particle-shader-guid"]
    }
    builder._runtime_artifact_bindings = {}
    final_dir = tmp_path / "dist"
    builder._write_runtime_asset_records(str(final_dir))
    records = json.loads(
        (final_dir / "Data" / "Library" / "RuntimeAssetRecords.json").read_text(
            encoding="utf-8"
        )
    )
    shader_record = next(
        item for item in records["entries"] if item["guid"] == "particle-shader-guid"
    )
    assert shader_record["runtime_path"] == (
        "Library/Resources/shaders/particle_sprite.vert"
    )
    assert shader_record["runtime_artifacts"][0]["runtime_path"] == (
        "Infernux/resources/shaders/particle_sprite.vert"
    )


def test_cooked_python_component_keeps_script_and_runtime_guid_identity(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    script = project / "Assets" / "Scripts" / "Mover.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("class Mover:\n    pass\n", encoding="utf-8")
    script_guid = "1234567890abcdef1234567890abcdef"
    scene.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "components": [
                            {
                                "type_id": (
                                    f"python:{script_guid}:type-guid:Scripts.Mover:Mover"
                                )
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(project, script, script_guid, "", "Script"),
        ],
    )
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    final_dir = tmp_path / "dist"
    data_dir = final_dir / "Data"

    builder._copy_cooked_assets(str(data_dir))
    assert (data_dir / "Assets" / "Scripts" / "Mover.py").is_file()

    builder._runtime_artifact_bindings = {}
    builder._runtime_artifact_source_paths = set()
    builder._stage_library_runtime_documents(str(data_dir))
    builder._compile_user_scripts(str(final_dir))
    builder._write_runtime_asset_records(str(final_dir))

    assert not (data_dir / "Assets" / "Scripts" / "Mover.py").exists()
    assert (data_dir / "Assets" / "Scripts" / "Mover.pyc").is_file()
    guid_map = json.loads((data_dir / "_script_guid_map.json").read_text(encoding="utf-8"))
    assert guid_map[script_guid].replace("\\", "/") == "Assets/Scripts/Mover.pyc"
    records = json.loads(
        (data_dir / "Library" / "RuntimeAssetRecords.json").read_text(encoding="utf-8")
    )
    script_record = next(item for item in records["entries"] if item["guid"] == script_guid)
    assert script_record["runtime_path"] == "Assets/Scripts/Mover.pyc"
    expected_artifact_id = game_builder_module.runtime_artifact_id(
        "Content.inxpkg", "Assets/Scripts/Mover.pyc"
    )
    assert records["records_version"] == 2
    assert script_record["primary_runtime_artifact_id"] == expected_artifact_id
    assert script_record["runtime_artifact_ids"] == [expected_artifact_id]
    assert builder._runtime_asset_identity_bindings["Assets/Scripts/Mover.pyc"][
        "source_guid"
    ] == script_guid
    assert str(project).replace("\\", "/") not in json.dumps(records, ensure_ascii=False)


def test_runtime_asset_records_preserve_compiled_sprite_metadata(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    texture = project / "Assets" / "Sprites" / "Sheet.png"
    texture.parent.mkdir(parents=True, exist_ok=True)
    texture.write_bytes(b"runtime texture")
    texture_guid = "fedcba0987654321fedcba0987654321"
    entry = _asset_index_entry(project, texture, texture_guid, "", "Texture")
    entry["metadata"]["metadata"].update(
        {
            "width": {"type": "int", "value": 128},
            "height": {"type": "int", "value": 64},
            "texture_type": {"type": "string", "value": "sprite"},
            "sprite_frames": {
                "type": "json_array",
                "value": [
                    {
                        "stable_id": "1" * 32,
                        "name": "Hero",
                        "x": 32,
                        "y": 16,
                        "w": 32,
                        "h": 16,
                        "pivot_x": 0.5,
                        "pivot_y": 0.5,
                    }
                ],
            },
        }
    )
    builder._cooked_asset_entries = {texture_guid: entry}
    builder._runtime_artifact_bindings = {
        f"Library/Artifacts/Textures/{texture_guid}.inxtex": {
            "source_guid": texture_guid,
        }
    }
    builder._runtime_artifact_source_paths = set()
    final_dir = tmp_path / "dist"

    builder._write_runtime_asset_records(str(final_dir))

    records = json.loads(
        (final_dir / "Data" / "Library" / "RuntimeAssetRecords.json").read_text(
            encoding="utf-8"
        )
    )
    metadata = records["entries"][0]["metadata"]["metadata"]
    assert metadata["texture_type"]["value"] == "sprite"
    assert metadata["sprite_frames"]["value"][0]["name"] == "Hero"
    assert not any(final_dir.rglob("*.meta"))


def test_runtime_asset_records_reject_missing_compiled_metadata(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    texture = project / "Assets" / "Broken.png"
    texture.parent.mkdir(parents=True, exist_ok=True)
    texture.write_bytes(b"runtime texture")
    entry = _asset_index_entry(project, texture, "broken-guid", "", "Texture")
    entry["metadata"] = {}
    builder._cooked_asset_entries = {"broken-guid": entry}
    builder._runtime_artifact_bindings = {
        "Library/Artifacts/Textures/broken-guid.inxtex": {
            "source_guid": "broken-guid",
        }
    }
    builder._runtime_artifact_source_paths = set()

    with pytest.raises(RuntimeError, match="Refusing to discard the \\.meta sidecar"):
        builder._write_runtime_asset_records(str(tmp_path / "dist"))


def test_cooked_python_component_includes_imported_project_helper(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    scripts = project / "Assets" / "Scripts" / "Voxel"
    component = scripts / "VoxelContinentGenerator.py"
    helper = scripts / "VoxelPipeline.py"
    unused = scripts / "Unused.py"
    scripts.mkdir(parents=True, exist_ok=True)
    component.write_text(
        "from Scripts.Voxel.VoxelPipeline import register_voxel_effects\n"
        "class VoxelContinentGenerator:\n"
        "    pass\n",
        encoding="utf-8",
    )
    helper.write_text("def register_voxel_effects():\n    return True\n", encoding="utf-8")
    unused.write_text("UNUSED = True\n", encoding="utf-8")
    component_guid = "component-script-guid"
    helper_guid = "helper-script-guid"
    scene.write_text(
        json.dumps(
            {
                "component": (
                    f"python:{component_guid}:type-guid:"
                    "Scripts.Voxel.VoxelContinentGenerator:VoxelContinentGenerator"
                )
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(project, component, component_guid, "", "Script"),
            _asset_index_entry(project, helper, helper_guid, "", "Script"),
            _asset_index_entry(project, unused, "unused-script-guid", "", "Script"),
        ],
    )
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(builder._asset_index_entries())

    assert set(selected) == {
        "scene-guid",
        component_guid,
        helper_guid,
        "unused-script-guid",
    }


def test_cooked_python_component_resolves_relative_helper_import(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    scripts = project / "Assets" / "Scripts" / "Gameplay"
    component = scripts / "Mover.py"
    helper = scripts / "movement.py"
    scripts.mkdir(parents=True, exist_ok=True)
    component.write_text("from . import movement\n", encoding="utf-8")
    helper.write_text("SPEED = 3.0\n", encoding="utf-8")
    component_guid = "relative-component-guid"
    helper_guid = "relative-helper-guid"
    scene.write_text(
        json.dumps(
            {
                "component": (
                    f"python:{component_guid}:type-guid:Scripts.Gameplay.Mover:Mover"
                )
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(project, component, component_guid, "", "Script"),
            _asset_index_entry(project, helper, helper_guid, "", "Script"),
        ],
    )
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(builder._asset_index_entries())

    assert set(selected) == {"scene-guid", component_guid, helper_guid}


def test_cooked_python_component_includes_literal_project_assets(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    script = project / "Assets" / "Scripts" / "Voxel.py"
    material = project / "Assets" / "Materials" / "Voxel.mat"
    cache = project / "Assets" / "Data" / "Voxel.npy"
    unused = project / "Assets" / "Data" / "Unused.npy"
    script.parent.mkdir(parents=True, exist_ok=True)
    material.parent.mkdir(parents=True, exist_ok=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        'MATERIAL = "Assets/Materials/Voxel.mat"\n'
        'CACHE = "Assets/Data/Voxel.npy"\n',
        encoding="utf-8",
    )
    material.write_text("{}", encoding="utf-8")
    cache.write_bytes(b"npy")
    unused.write_bytes(b"unused")
    script_guid = "voxel-script-guid"
    scene.write_text(
        json.dumps(
            {
                "component": (
                    f"python:{script_guid}:type-guid:Scripts.Voxel:Voxel"
                )
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(project, script, script_guid, "", "Script"),
            _asset_index_entry(project, material, "material-guid", "", "Material"),
            _asset_index_entry(project, cache, "cache-guid", "", "Binary"),
            _asset_index_entry(project, unused, "unused-guid", "", "Binary"),
        ],
    )
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(builder._asset_index_entries())

    assert set(selected) == {
        "scene-guid",
        script_guid,
        "material-guid",
        "cache-guid",
        "unused-guid",
    }


def test_complete_assets_cook_does_not_use_script_literals_as_content_roots(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    scene = project / "Assets" / "Main.scene"
    script = project / "Assets" / "Scripts" / "Broken.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('CACHE = "Assets/Data/Missing.npy"\n', encoding="utf-8")
    script_guid = "broken-script-guid"
    scene.write_text(
        json.dumps(
            {
                "component": (
                    f"python:{script_guid}:type-guid:Scripts.Broken:Broken"
                )
            }
        ),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
            _asset_index_entry(project, script, script_guid, "", "Script"),
        ],
    )
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )

    selected = builder._collect_library_asset_entries(builder._asset_index_entries())

    assert set(selected) == {"scene-guid", script_guid}


def test_player_type_registry_is_derived_from_script_ast_without_execution(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    script_guid = "1234567890abcdef1234567890abcdef"
    records = builder._runtime_component_type_records(
        "from Infernux.components import *\n"
        "class Mover(InxComponent):\n"
        "    def awake(self):\n"
        "        raise RuntimeError('must not execute during build')\n"
        "    def update(self, delta_time):\n"
        "        pass\n",
        script_guid=script_guid,
        runtime_path="Assets/Scripts/mover.pyc",
    )

    assert len(records) == 1
    assert records[0]["module"] == "Scripts.mover"
    assert records[0]["qualname"] == "Mover"
    assert records[0]["lifecycle"] == ["awake", "update"]
    assert records[0]["type_id"].startswith(f"python:{script_guid}:")


def test_payload_manifest_rejects_indexed_asset_outside_build_scene_closure(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    sources = _write_scene_material_audio_reachability_fixture(
        builder,
        include_unreachable=True,
    )
    final_dir = tmp_path / "dist"
    data_root = _prepare_runtime_catalog_inputs(builder, final_dir)
    write_pack(
        (
            ("Assets/Main.scene", sources["scene"]),
            ("Assets/Materials/Bird.mat", sources["material"]),
            ("Assets/Audio/Wing.wav", sources["audio"]),
            ("Assets/Unused.mat", sources["unreachable"]),
        ),
        data_root / builder._CONTENT_ARCHIVE_FILENAME,
    )
    _install_runtime_identity_bindings(
        builder,
        {
            "Assets/Main.scene": (
                "scene-guid",
                "runtime_loader_requires_serialized_document",
            ),
            "Assets/Materials/Bird.mat": (
                "material-guid",
                "runtime_loader_requires_serialized_document",
            ),
            "Assets/Audio/Wing.wav": (
                "audio-guid",
                "runtime_audio_backend_requires_encoded_stream",
            ),
            "Assets/Unused.mat": (
                "unused-guid",
                "runtime_loader_requires_serialized_document",
            ),
        },
    )

    with pytest.raises(RuntimeError, match="Assets/Unused.mat"):
        builder._write_payload_manifest(str(final_dir))


def test_copy_stage_uses_all_indexed_assets_before_content_pack(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    sources = _write_scene_material_audio_reachability_fixture(
        builder,
        include_unreachable=True,
    )
    sources["unreachable"].with_suffix(".mat.meta").write_text(
        "{}",
        encoding="utf-8",
    )
    unindexed = Path(builder.project_path) / "Assets" / "Dynamic" / "Runtime.bin"
    unindexed.parent.mkdir(parents=True)
    unindexed.write_bytes(b"unindexed payload")
    final_dir = tmp_path / "dist"

    builder._copy_game_data(str(final_dir))
    builder._write_runtime_asset_records(str(final_dir))

    staged = final_dir / "Data"
    assert (staged / "Assets" / "Main.scene").is_file()
    assert (staged / "Assets" / "Materials" / "Bird.mat").is_file()
    assert (staged / "Assets" / "Audio" / "Wing.wav").is_file()
    assert (staged / "Library" / "Artifacts" / "Document" / "scene-guid.scene").is_file()
    assert (staged / "Library" / "Artifacts" / "Document" / "material-guid.mat").is_file()
    assert (staged / "Library" / "Artifacts" / "Audio" / "audio-guid.wav").is_file()
    assert (staged / "Assets" / "Unused.mat").exists()
    assert not (staged / "Assets" / "Unused.mat.meta").exists()
    assert not (staged / "Assets" / "Dynamic" / "Runtime.bin").exists()

    _write_player_executable(final_dir)
    builder._organize_player_layout(str(final_dir))
    data_root = final_dir / "TestGame_Data"
    runtime_source = tmp_path / "runtime.bin"
    runtime_source.write_bytes(b"runtime")
    write_pack(
        (("Infernux/resources/runtime.bin", runtime_source),),
        data_root / builder._RUNTIME_ARCHIVE_FILENAME,
    )
    builder._pack_content_archive(str(final_dir))

    def reject_reopen(_package_path, entry_path):
        raise AssertionError(f"current-build catalog payload reopened from package: {entry_path}")

    monkeypatch.setattr(game_builder_module, "read_entry", reject_reopen)
    builder._write_payload_manifest(str(final_dir))

    content_names = {
        entry["path"]
        for entry in read_manifest(data_root / builder._CONTENT_ARCHIVE_FILENAME)["files"]
    }
    assert {
        "Library/Artifacts/Document/scene-guid.scene",
        "Library/Artifacts/Document/material-guid.mat",
        "Library/Artifacts/Audio/audio-guid.wav",
    } <= content_names
    assert "Assets/Main.scene" not in content_names
    assert "Assets/Materials/Bird.mat" not in content_names
    assert "Assets/Audio/Wing.wav" not in content_names
    assert "Assets/Unused.mat" not in content_names
    assert "Assets/Dynamic/Runtime.bin" not in content_names
    assert (data_root / "Library" / "RuntimeAssetCatalog.json").is_file()
    catalog = json.loads(
        (data_root / "Library" / "RuntimeAssetCatalog.json").read_text(
            encoding="utf-8"
        )
    )
    assert not {
        artifact["payload_kind"]
        for artifact in catalog["artifacts"]
    }.intersection({"serialized_runtime_document", "direct_runtime_asset"})


def test_cooked_document_and_audio_paths_are_compiled_artifacts():
    expected = {
        "Library/Artifacts/Document/scene-guid.scene": "scene_artifact",
        "Library/Artifacts/Document/clip-guid.animclip3d": "animation_clip_3d_artifact",
        "Library/Artifacts/Document/timeline-guid.animtimeline": "animation_timeline_artifact",
        "Library/Artifacts/Audio/audio-guid.wav": "audio_artifact",
        "Library/Artifacts/Blob/blob-guid.bin": "project_runtime_blob_artifact",
    }
    for runtime_path, logical_type in expected.items():
        assert logical_type_for_path(runtime_path) == logical_type
        assert payload_kind_for(logical_type) == "compiled_artifact"


def test_cooked_document_catalog_resolves_author_path_dependency_alias():
    scene_payload = json.dumps(
        {
            "material": {
                "$type": "asset_ref",
                "guid": "",
                "path_hint": "Assets/Materials/Bird.mat",
            }
        }
    ).encode("utf-8")
    material_payload = b"{}"
    entries = [
        {
            "package": "Content.inxpkg",
            "runtime_path": "Library/Artifacts/Document/scene-guid.scene",
            "bytes": len(scene_payload),
            "sha256": hashlib.sha256(scene_payload).hexdigest(),
            "payload": scene_payload,
            "asset_binding": {
                "source_guid": "scene-guid",
                "source_path": "Assets/Main.scene",
                "dependencies": [],
            },
        },
        {
            "package": "Content.inxpkg",
            "runtime_path": "Library/Artifacts/Document/material-guid.mat",
            "bytes": len(material_payload),
            "sha256": hashlib.sha256(material_payload).hexdigest(),
            "payload": material_payload,
            "asset_binding": {
                "source_guid": "material-guid",
                "source_path": "Assets/Materials/Bird.mat",
                "dependencies": [],
            },
        },
    ]

    catalog = build_catalog(
        entries,
        player_host={"executable": "Game.exe", "sha256": "a" * 64},
        package_records=[],
    )

    by_path = {artifact["runtime_path"]: artifact for artifact in catalog["artifacts"]}
    material_id = by_path[
        "Library/Artifacts/Document/material-guid.mat"
    ]["runtime_artifact_id"]
    scene = by_path["Library/Artifacts/Document/scene-guid.scene"]
    assert scene["dependencies"] == [material_id]
    assert scene["unresolved_dependencies"] == []


def test_player_document_rewrite_normalizes_asset_hints_and_particle_duplicates(
    tmp_path,
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    document_path = tmp_path / "RuntimeIndex.json"
    document_path.write_text(
        json.dumps(
            {
                "$schema": "infernux.particle_runtime_index",
                "entries": [
                    {
                        "guid": "1" * 32,
                        "stable_id": "2" * 32,
                        "path_hint": "Assets/VFX/Wind.particlegraph",
                    },
                    {
                        "guid": "1" * 32,
                        "stable_id": "2" * 32,
                        "path_hint": "C:/OldProject/Assets/VFX/Wind.particlegraph",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    builder._rewrite_player_document_paths(str(document_path), ".json")

    rewritten = json.loads(document_path.read_text(encoding="utf-8"))
    assert rewritten["entries"] == [
        {
            "guid": "1" * 32,
            "stable_id": "2" * 32,
            "path_hint": "Assets/VFX/Wind.particlegraph",
        }
    ]


def test_runtime_catalog_does_not_treat_type_or_stable_ids_as_assets():
    script_guid = "1" * 32
    type_guid = "2" * 32
    particle_guid = "3" * 32
    stable_id = "4" * 32
    script_payload = b"script"
    particle_payload = b"particle"
    type_registry = json.dumps(
        {
            "$schema": "infernux.runtime_type_registry",
            "types": [
                {
                    "script_guid": script_guid,
                    "type_guid": type_guid,
                }
            ],
        }
    ).encode("utf-8")
    particle_index = json.dumps(
        {
            "$schema": "infernux.particle_runtime_index",
            "entries": [
                {
                    "guid": particle_guid,
                    "path_hint": "Assets/VFX/Wind.particlegraph",
                    "stable_id": stable_id,
                }
            ],
        }
    ).encode("utf-8")
    entries = [
        {
            "package": "Content.inxpkg",
            "runtime_path": "Assets/Scripts/Game.pyc",
            "bytes": len(script_payload),
            "sha256": hashlib.sha256(script_payload).hexdigest(),
            "asset_binding": {
                "source_guid": script_guid,
                "source_path": "Assets/Scripts/Game.py",
                "dependencies": [],
            },
        },
        {
            "package": "Content.inxpkg",
            "runtime_path": f"Library/Artifacts/Particle/{particle_guid}.inxparticle",
            "bytes": len(particle_payload),
            "sha256": hashlib.sha256(particle_payload).hexdigest(),
            "asset_binding": {
                "source_guid": particle_guid,
                "source_path": "Assets/VFX/Wind.particlegraph",
                "dependencies": [],
            },
        },
        {
            "package": "Content.inxpkg",
            "runtime_path": "Library/RuntimeTypeRegistry.json",
            "bytes": len(type_registry),
            "sha256": hashlib.sha256(type_registry).hexdigest(),
            "payload": type_registry,
        },
        {
            "package": "Content.inxpkg",
            "runtime_path": "Library/Artifacts/Particle/RuntimeIndex.json",
            "bytes": len(particle_index),
            "sha256": hashlib.sha256(particle_index).hexdigest(),
            "payload": particle_index,
        },
    ]

    catalog = build_catalog(
        entries,
        player_host={"executable": "Game", "sha256": "a" * 64},
        package_records=[],
    )

    by_path = {artifact["runtime_path"]: artifact for artifact in catalog["artifacts"]}
    script = by_path["Assets/Scripts/Game.pyc"]
    particle = by_path[
        f"Library/Artifacts/Particle/{particle_guid}.inxparticle"
    ]
    registry = by_path["Library/RuntimeTypeRegistry.json"]
    index = by_path["Library/Artifacts/Particle/RuntimeIndex.json"]
    assert registry["dependencies"] == [script["runtime_artifact_id"]]
    assert registry["unresolved_dependencies"] == []
    assert index["dependencies"] == [particle["runtime_artifact_id"]]
    assert index["unresolved_dependencies"] == []


def test_animclip3d_catalog_depends_on_independent_animation_model():
    clip_guid = "1" * 32
    animation_model_guid = "2" * 32
    clip_payload = json.dumps(
        {
            "name": "Run",
            "source_model_guid": animation_model_guid,
            "source_model_path": "Assets/Animations/Run.fbx",
            "take_name": "Run",
            "bind_pose_bone_names": [],
            "duration_hint": 0.0,
            "events": [],
        }
    ).encode("utf-8")
    mesh_payload = b"mesh"
    entries = [
        {
            "package": "Content.inxpkg",
            "runtime_path": f"Library/Artifacts/Document/{clip_guid}.animclip3d",
            "bytes": len(clip_payload),
            "sha256": hashlib.sha256(clip_payload).hexdigest(),
            "payload": clip_payload,
            "asset_binding": {
                "source_guid": clip_guid,
                "source_path": "Assets/Animations/Run.animclip3d",
                "dependencies": [],
            },
        },
        {
            "package": "Content.inxpkg",
            "runtime_path": f"Library/Artifacts/Mesh/{animation_model_guid}.inxmesh",
            "bytes": len(mesh_payload),
            "sha256": hashlib.sha256(mesh_payload).hexdigest(),
            "asset_binding": {
                "source_guid": animation_model_guid,
                "source_path": "Assets/Animations/Run.fbx",
                "dependencies": [],
            },
        },
    ]

    catalog = build_catalog(
        entries,
        player_host={"executable": "Game.exe", "sha256": "a" * 64},
        package_records=[],
    )

    by_path = {artifact["runtime_path"]: artifact for artifact in catalog["artifacts"]}
    model = by_path[f"Library/Artifacts/Mesh/{animation_model_guid}.inxmesh"]
    clip = by_path[f"Library/Artifacts/Document/{clip_guid}.animclip3d"]
    assert clip["dependencies"] == [model["runtime_artifact_id"]]
    assert clip["unresolved_dependencies"] == []


def test_cooked_catalog_discovers_native_and_effect_group_asset_references():
    scene_guid = "11111111111111111111111111111111"
    material_guid = "22222222222222222222222222222222"
    group_guid = "33333333333333333333333333333333"
    effect_guid = "44444444444444444444444444444444"
    payloads = {
        f"Library/Artifacts/Document/{scene_guid}.scene": json.dumps(
            {"materials": [material_guid]}
        ).encode("utf-8"),
        f"Library/Artifacts/Document/{material_guid}.mat": b"{}",
        f"Library/Artifacts/Document/{group_guid}.effectgroup": json.dumps(
            {
                "entries": [
                    {
                        "asset": {
                            "guid": effect_guid,
                            "path_hint": "Assets/Effects/Ink.effect",
                        }
                    }
                ]
            }
        ).encode("utf-8"),
        f"Library/Artifacts/Document/{effect_guid}.effect": b"{}",
    }
    bindings = {
        scene_guid: "Assets/Main.scene",
        material_guid: "Assets/Materials/Main.mat",
        group_guid: "Assets/Effects/Ink.effectgroup",
        effect_guid: "Assets/Effects/Ink.effect",
    }
    entries = []
    for runtime_path, payload in payloads.items():
        guid = Path(runtime_path).stem
        entries.append(
            {
                "package": "Content.inxpkg",
                "runtime_path": runtime_path,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "payload": payload,
                "asset_binding": {
                    "source_guid": guid,
                    "source_path": bindings[guid],
                    "dependencies": [],
                },
            }
        )

    catalog = build_catalog(
        entries,
        player_host={"executable": "Game.exe", "sha256": "a" * 64},
        package_records=[],
    )

    by_path = {artifact["runtime_path"]: artifact for artifact in catalog["artifacts"]}
    scene = by_path[f"Library/Artifacts/Document/{scene_guid}.scene"]
    material = by_path[f"Library/Artifacts/Document/{material_guid}.mat"]
    group = by_path[f"Library/Artifacts/Document/{group_guid}.effectgroup"]
    effect = by_path[f"Library/Artifacts/Document/{effect_guid}.effect"]
    assert scene["dependencies"] == [material["runtime_artifact_id"]]
    assert group["dependencies"] == [effect["runtime_artifact_id"]]


def test_payload_manifest_rejects_source_replaced_by_current_library_artifact(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = _prepare_runtime_catalog_inputs(builder, final_dir)
    source = tmp_path / "albedo.png"
    source.write_bytes(b"duplicate source")
    write_pack(
        (("Assets/Textures/albedo.png", source),),
        data_root / builder._CONTENT_ARCHIVE_FILENAME,
    )
    builder._runtime_artifact_source_paths = {"assets/textures/albedo.png"}
    _install_runtime_identity_bindings(
        builder,
        {
            "Assets/Textures/albedo.png": (
                "texture-guid",
                "compiled_source_must_not_ship",
            )
        },
    )

    with pytest.raises(RuntimeError, match="direct or serialized runtime payloads"):
        builder._write_payload_manifest(str(final_dir))


def test_generated_player_log_is_lazy(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    source_path = Path(builder._generate_boot_script())
    source = source_path.read_text(encoding="utf-8")

    assert "os.makedirs(_LOGS_DIR, exist_ok=True)" in source
    assert source.count("os.makedirs(_LOGS_DIR, exist_ok=True)") == 1
    assert "open(_LOG, \"w\", encoding=\"utf-8\").close()" not in source


def test_payload_manifest_rejects_invalid_native_package(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = _prepare_runtime_catalog_inputs(
        builder,
        final_dir,
        include_runtime=False,
    )
    (data_root / builder._RUNTIME_ARCHIVE_FILENAME).write_bytes(b"not-inxpack")

    with pytest.raises(RuntimeError, match="cannot validate"):
        builder._write_payload_manifest(str(final_dir))


def test_payload_manifest_requires_content_package(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    _prepare_runtime_catalog_inputs(
        builder,
        final_dir,
        include_content=False,
    )

    with pytest.raises(RuntimeError, match=r"Content\.inxpkg"):
        builder._write_payload_manifest(str(final_dir))


def test_payload_manifest_requires_player_executable(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    _prepare_runtime_catalog_inputs(
        builder,
        final_dir,
        include_executable=False,
    )

    with pytest.raises(RuntimeError, match="Player executable is missing"):
        builder._write_payload_manifest(str(final_dir))


def test_payload_manifest_reports_current_native_packages(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = _prepare_runtime_catalog_inputs(builder, final_dir)

    builder._write_payload_manifest(str(final_dir))

    catalog = json.loads(
        (data_root / "Library" / "RuntimeAssetCatalog.json").read_text(
            encoding="utf-8"
        )
    )
    assert catalog["$schema"] == "infernux.runtime_asset_catalog"
    assert catalog["player_host"]["executable"] == _player_executable_name()
    assert {package["path"] for package in catalog["packages"]} == {
        "TestGame_Data/Runtime.inxrt",
        "TestGame_Data/Content.inxpkg",
    }
    assert all(package["archive_sha256"] for package in catalog["packages"])
    package_index = (
        data_root / builder._PLAYER_PACKAGE_INDEX_FILENAME
    ).read_text(encoding="ascii").splitlines()
    assert package_index[0] == "INFERNUX_PLAYER_PACKAGE_INDEX_V1"
    assert {line.split("\t", 1)[0] for line in package_index[1:]} == {
        "runtime",
        "content",
    }
    assert all(len(line.split("\t")) == 3 for line in package_index[1:])
    assert len(catalog["artifacts"]) == 2
    assert all(
        artifact["runtime_artifact_id"].startswith("ra_")
        and artifact["content_sha256"]
        and artifact["dependencies"] == []
        for artifact in catalog["artifacts"]
    )


def test_payload_manifest_supports_platform_native_player_host(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = _prepare_runtime_catalog_inputs(
        builder,
        final_dir,
        include_runtime=False,
        include_executable=False,
    )
    host = {
        "identity": "android-sdl-python-player-host",
        "entry_point": "com.infernux.bootstrap/.InfernuxActivity",
        "platform": "android",
        "architecture": "x86_64",
    }

    builder._write_payload_manifest(
        str(final_dir),
        platform_host=host,
        include_runtime_archive=False,
    )

    catalog = json.loads(
        (data_root / "Library/RuntimeAssetCatalog.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (data_root / "Player.inxmanifest").read_text(encoding="utf-8")
    )
    package_index = (data_root / "PackageIndex.inxmanifest").read_text(
        encoding="ascii"
    )
    assert catalog["player_host"] == host
    assert [record["path"] for record in catalog["packages"]] == [
        "TestGame_Data/Content.inxpkg"
    ]
    assert manifest["product"]["layout"] == "platform_native_packages"
    assert manifest["product"]["entry_points"] == [host["entry_point"]]
    assert "content\t" in package_index
    assert "runtime\t" not in package_index


def test_payload_manifest_rejects_direct_documents_after_dependency_reads(
    tmp_path,
    monkeypatch,
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    data_root.mkdir(parents=True)
    _write_player_executable(final_dir)

    sources = tmp_path / "catalog-payloads"
    sources.mkdir()
    bytecode = sources / "module.pyc"
    native = sources / "native.dll"
    shader = sources / "builtin.frag"
    artifact = sources / "texture.inxtex"
    bytecode.write_bytes(b"bytecode")
    native.write_bytes(b"native")
    shader.write_text("void main() {}", encoding="utf-8")
    artifact.write_bytes(b"artifact")
    metadata = sources / "RuntimeIndex.json"
    scene = sources / "Main.scene"
    material = sources / "Test.mat"
    audio = sources / "Sound.wav"
    reference = {
        "material": {
            "$type": "asset_ref",
            "path_hint": "Assets/Materials/Test.mat",
        }
    }
    metadata.write_text(json.dumps(reference), encoding="utf-8")
    scene.write_text(json.dumps(reference), encoding="utf-8")
    material.write_text("{}", encoding="utf-8")
    audio.write_bytes(b"audio")

    runtime_entries = [
        *[(f"stdlib/module_{index}.pyc", bytecode) for index in range(128)],
        *[(f"Infernux/lib/native_{index}.dll", native) for index in range(64)],
        ("Infernux/resources/shaders/standard.frag", shader),
        ("numpy/core/_multiarray_umath.pyd", native),
        ("Library/Artifacts/Textures/Test.inxtex", artifact),
        ("Library/Particle/RuntimeIndex.json", metadata),
    ]
    write_pack(runtime_entries, data_root / builder._RUNTIME_ARCHIVE_FILENAME)
    write_pack(
        (
            ("Assets/Main.scene", scene),
            ("Assets/Materials/Test.mat", material),
            ("Assets/Audio/Sound.wav", audio),
        ),
        data_root / builder._CONTENT_ARCHIVE_FILENAME,
    )

    reads: list[str] = []
    native_read_entry = game_builder_module.read_entry

    def counted_read_entry(package_path, entry_path):
        reads.append(str(entry_path).replace("\\", "/"))
        return native_read_entry(package_path, entry_path)

    monkeypatch.setattr(game_builder_module, "read_entry", counted_read_entry)
    _install_runtime_identity_bindings(
        builder,
        {
            "Assets/Main.scene": (
                "scene-guid",
                "runtime_loader_requires_serialized_document",
            ),
            "Assets/Materials/Test.mat": (
                "material-guid",
                "runtime_loader_requires_serialized_document",
            ),
            "Assets/Audio/Sound.wav": (
                "audio-guid",
                "runtime_audio_backend_requires_encoded_stream",
            ),
        },
    )

    with pytest.raises(
        RuntimeError,
        match="direct or serialized runtime payload",
    ):
        builder._write_payload_manifest(str(final_dir))

    assert set(reads) == {
        "Library/Particle/RuntimeIndex.json",
        "Assets/Main.scene",
        "Assets/Materials/Test.mat",
    }


def test_code_signing_isolates_windows_powershell_modules(tmp_path, monkeypatch):
    system_root = tmp_path / "Windows"
    powershell_root = system_root / "System32" / "WindowsPowerShell" / "v1.0"
    powershell_exe = powershell_root / "powershell.exe"
    powershell_exe.parent.mkdir(parents=True)
    powershell_exe.write_bytes(b"")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "Game.exe").write_bytes(b"exe")

    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setenv("PSModulePath", "C:/Program Files/PowerShell/7/Modules")
    observed: dict = {}
    internal_messages: list[str] = []
    warnings: list[str] = []

    def _run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "STATUS:UnknownError\n"
                "MESSAGE:A certificate chain terminated in an untrusted root\n"
                "SIGNER:AABBCC\n"
                "CERT:AABBCC\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(nuitka_builder_module.subprocess, "run", _run)
    monkeypatch.setattr(nuitka_builder_module.Debug, "log_internal", internal_messages.append)
    monkeypatch.setattr(nuitka_builder_module.Debug, "log_warning", warnings.append)
    builder = object.__new__(NuitkaBuilder)
    builder.output_filename = "Game.exe"

    builder._sign_executable(str(dist_dir))

    assert observed["command"][0] == str(powershell_exe)
    assert "-NoProfile" in observed["command"]
    assert "-NonInteractive" in observed["command"]
    assert observed["env"]["PSModulePath"] == str(powershell_root / "Modules")
    assert "PowerShell/7/Modules" not in observed["env"]["PSModulePath"]
    assert "Microsoft.PowerShell.Security.psd1" in observed["command"][-1]
    assert any("local root is not trusted" in message for message in internal_messages)
    assert warnings == []


class TestGameBuilderOutputSafety:
    def test_debug_player_boot_and_manifest_mark_validation_capability(self, tmp_path):
        output_dir = tmp_path / "build_output"
        builder = GameBuilder(
            str(_make_project(tmp_path)),
            str(output_dir),
            game_name="TestGame",
            debug_mode=True,
        )

        boot_path = builder._generate_boot_script()
        boot_bytes = Path(boot_path).read_bytes()
        assert b"\x00" not in boot_bytes
        assert b"\\x00" in boot_bytes
        boot_source = boot_bytes.decode("utf-8")
        compile(boot_source, boot_path, "exec")
        assert "import json" not in boot_source
        assert "import pathlib" not in boot_source
        assert "from pathlib" not in boot_source
        assert "import traceback" not in boot_source
        assert "_DEBUG_MODE = True" in boot_source
        assert 'os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = "1" if _DEBUG_MODE else "0"' in boot_source
        assert 'os.environ["PYTHONDONTWRITEBYTECODE"] = "1"' in boot_source
        assert 'os.environ["_INFERNUX_PLAYER_DATA_ROOT"] = _DATA_ROOT' in boot_source
        assert "sys.dont_write_bytecode = True" in boot_source
        assert 'os.environ["_INFERNUX_PACKAGED_RESOURCE_ROOT"]' in boot_source
        assert '_RUNTIME_ARCHIVE = os.path.join(_DATA_ROOT, "Runtime.inxrt")' in boot_source
        assert '"stdlib"' in boot_source
        assert '{"Infernux", "numpy", "numpy.libs", "packaging", "stdlib"}' in boot_source
        assert '_STDLIB_RUNTIME_DIR' in boot_source
        assert 'os.add_dll_directory(_dll_dir)' in boot_source
        assert '_CONTENT_ARCHIVE = os.path.join(_DATA_ROOT, "Content.inxpkg")' in boot_source
        assert '_PARALLEL_ARCHIVE = os.path.join(_DATA_ROOT, "Modules", "Parallel.inxmod")' in boot_source
        assert 'class _ParallelRuntimeFinder:' in boot_source
        assert '_mark_boot_phase("parallel_deferred")' in boot_source
        assert '_RUNTIME_MODULE_DIR = _extract_cached_archive(' in boot_source
        assert 'if os.path.isfile(_PARALLEL_ARCHIVE):\n    _RUNTIME_MODULE_DIR = _extract_cached_archive(' not in boot_source
        assert '_index_path = os.path.join(_DATA_ROOT, "PackageIndex.inxmanifest")' in boot_source
        assert "_PLAYER_PACKAGE_INDEX = _load_player_package_index()" in boot_source
        assert "for _package_kind, (_package_hash, _package_bytes) in _PLAYER_PACKAGE_INDEX.items():" in boot_source
        assert '_source_marker = os.path.join(_cache_root, ".source")' in boot_source
        assert "_archive_stat.st_mtime_ns" not in boot_source
        assert '_source_identity = _expected_hash + "\\n" + str(_archive_stat.st_size)' in boot_source
        assert "_extracted_manifest = dict(" in boot_source
        assert "identity does not match its build index" in boot_source
        assert '"_INFERNUX_PLAYER_DATA_ROOT"' in boot_source
        assert "_extract_cached_archive" in boot_source
        assert "_NATIVE_PACK._inxpack_extract" in boot_source
        assert "_INFERNUX_PLAYER_" in boot_source
        assert "_ARCHIVE_SHA256" in boot_source
        assert "_ARCHIVE_BYTES" in boot_source
        assert '_GAME_NAME = _EXE_STEM or "InfernuxPlayer"' in boot_source
        assert 'if os.environ.get("_INFERNUX_PLAYER_CONTROL_FILE"):' in boot_source
        pre_native_boot = boot_source.split(
            "_CORE_RUNTIME_DIR = _extract_cached_archive", 1
        )[0]
        assert "from Infernux" not in pre_native_boot
        assert "import Infernux" not in pre_native_boot
        assert "os.path.dirname(sys.executable)" in pre_native_boot
        assert "import _InfernuxBootstrap as _NATIVE_PACK" in boot_source
        assert "import shutil" not in boot_source
        assert "shutil." not in boot_source
        assert "import ctypes" not in boot_source
        assert "ctypes." not in boot_source
        assert '"_inxplayer_show_error"' in boot_source
        assert "_NATIVE_PACK._inxplayer_show_error(" in boot_source
        assert 'sys.modules["Infernux.lib._Infernux"]' not in boot_source
        assert "import _Infernux as _module" not in boot_source
        assert "from Infernux.lib import _Infernux" not in boot_source
        assert '_INFERNUX_LIB_DIR = os.path.join(_CORE_RUNTIME_DIR, "Infernux", "lib")' in boot_source
        assert 'os.environ["INFERNUX_NATIVE_MODULE_DIR"] = _INFERNUX_LIB_DIR' in boot_source
        assert "os.O_EXCL" in boot_source
        assert "Timed out waiting for Player cache publication" in boot_source
        expected_cache_helper = inspect.getsource(
            game_builder_module._publish_player_cache
        ).strip()
        assert "json." not in expected_cache_helper
        assert 'str(os.getpid()).encode("ascii")' in expected_cache_helper
        assert expected_cache_helper in boot_source.replace("\r\n", "\n")
        assert "_NATIVE_PACK._inxplayer_process_is_alive(pid)" in boot_source
        assert "os.kill(pid, 0)" not in boot_source
        assert "_remove_player_path(cache_root)" in boot_source
        assert "_copy_player_file_atomic(" in boot_source
        assert "os.replace(lock_path, stale_path)" in boot_source
        assert boot_source.index("import _InfernuxBootstrap as _NATIVE_PACK") < boot_source.index(
            "from Infernux.engine import run_player"
        )

        settings = output_dir / "Data" / "ProjectSettings"
        settings.mkdir(parents=True)
        (settings / "BuildSettings.json").write_text(json.dumps({"scenes": ["Assets/Main.scene"]}), encoding="utf-8")
        builder._generate_manifest(str(output_dir))
        manifest = json.loads((output_dir / "Data" / "BuildManifest.json").read_text(encoding="utf-8"))
        assert manifest["debug_build"] is True
        assert manifest["game_name"] == "TestGame"

    def test_validate_rejects_non_empty_unmarked_output_dir(self, tmp_path):
        output_dir = tmp_path / "build_output"
        output_dir.mkdir()
        keep_file = output_dir / "keep.txt"
        keep_file.write_text("keep", encoding="utf-8")
        builder = _make_builder(tmp_path, output_dir)

        with pytest.raises(BuildOutputDirectoryError) as exc_info:
            builder._validate()

        assert exc_info.value.reason == "not-empty-unmarked"
        assert exc_info.value.entries == ["keep.txt"]

        assert keep_file.read_text(encoding="utf-8") == "keep"

    def test_validate_rejects_unmarked_build_temp_output_dir(self, tmp_path):
        output_dir = tmp_path / "build_output"
        temp_dir = output_dir / "_build_temp"
        nested_dir = temp_dir / "nested"
        nested_dir.mkdir(parents=True)
        (nested_dir / "stale.bin").write_bytes(b"stale")
        builder = _make_builder(tmp_path, output_dir)

        with pytest.raises(BuildOutputDirectoryError, match="must be empty"):
            builder._validate()

    def test_clean_output_allows_marked_build_directory(self, tmp_path):
        output_dir = tmp_path / "build_output"
        output_dir.mkdir()
        old_file = output_dir / "old.bin"
        old_file.write_text("old", encoding="utf-8")
        nested_dir = output_dir / "Data"
        nested_dir.mkdir()
        (nested_dir / "stale.txt").write_text("stale", encoding="utf-8")

        builder = _make_builder(tmp_path, output_dir)
        builder._write_output_marker(str(output_dir))

        builder._validate()
        builder._clean_output()

        assert output_dir.is_dir()
        assert list(output_dir.iterdir()) == []

    def test_write_output_marker_creates_reusable_build_marker(self, tmp_path):
        output_dir = tmp_path / "build_output"
        output_dir.mkdir()
        builder = _make_builder(tmp_path, output_dir)

        builder._write_output_marker(str(output_dir))

        marker_path = Path(builder._output_marker_path(str(output_dir)))
        assert marker_path.is_file()
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        assert payload["tool"] == "Infernux"
        assert payload["kind"] == "build-output"
        assert payload["state"] == "complete"
        assert payload["project_name"] == "TestGame"
        assert len(payload["project_identity"]) == 64
        assert "project_path" not in payload

    def test_in_progress_marker_allows_safe_retry(self, tmp_path):
        output_dir = tmp_path / "build_output"
        output_dir.mkdir()
        builder = _make_builder(tmp_path, output_dir)
        builder._write_output_marker(str(output_dir), state="in_progress")
        (output_dir / "partial-player.exe").write_bytes(b"partial")

        builder._clean_output()

        assert list(output_dir.iterdir()) == []

    def test_foreign_output_marker_does_not_authorize_cleanup(self, tmp_path):
        output_dir = tmp_path / "build_output"
        output_dir.mkdir()
        (output_dir / GameBuilder.OUTPUT_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "tool": "Infernux",
                    "kind": "build-output",
                    "state": "complete",
                    "project_name": "OtherGame",
                    "project_identity": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "keep.txt").write_text("keep", encoding="utf-8")
        builder = _make_builder(tmp_path, output_dir)

        with pytest.raises(BuildOutputDirectoryError, match="must be empty"):
            builder._clean_output()


@pytest.fixture
def native_player_process_probe(monkeypatch):
    calls = []

    class _NativePack:
        @staticmethod
        def _inxplayer_process_is_alive(pid):
            calls.append(pid)
            return pid == os.getpid()

    monkeypatch.setattr(game_builder_module, "_NATIVE_PACK", _NativePack, raising=False)
    return calls


def test_player_cache_publication_is_safe_for_competing_processes(
    tmp_path, native_player_process_probe
):
    expected_hash = "a" * 64
    cache_root = tmp_path / "cache" / "runtime-a"
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def publish(index: int) -> None:
        temporary = tmp_path / f"runtime-{index}.tmp"
        temporary.mkdir()
        (temporary / ".ready").write_text(expected_hash, encoding="ascii")
        (temporary / "payload.bin").write_bytes(b"same payload")
        try:
            barrier.wait(timeout=5)
            results.append(
                game_builder_module._publish_player_cache(
                    str(temporary), str(cache_root), expected_hash
                )
            )
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=publish, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not errors
    assert results == [str(cache_root), str(cache_root)]
    assert (cache_root / ".ready").read_text(encoding="ascii") == expected_hash
    assert (cache_root / "payload.bin").read_bytes() == b"same payload"
    assert not (tmp_path / "cache" / "runtime-a.lock").exists()


def test_player_cache_reclaims_stale_lock(tmp_path, native_player_process_probe):
    expected_hash = "b" * 64
    cache_root = tmp_path / "cache" / "runtime-b"
    lock_path = Path(str(cache_root) + ".lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(str(2**31 - 1), encoding="ascii")
    stale_time = time.time() - 60
    os.utime(lock_path, (stale_time, stale_time))

    temporary = tmp_path / "runtime-b.tmp"
    temporary.mkdir()
    (temporary / ".ready").write_text(expected_hash, encoding="ascii")
    (temporary / "payload.bin").write_bytes(b"recovered payload")

    published = game_builder_module._publish_player_cache(
        str(temporary),
        str(cache_root),
        expected_hash,
        timeout_seconds=0.5,
        stale_lock_seconds=0.01,
    )

    assert published == str(cache_root)
    assert (cache_root / "payload.bin").read_bytes() == b"recovered payload"
    assert not lock_path.exists()


def test_player_cache_default_stale_window_precedes_timeout(
    tmp_path, native_player_process_probe
):
    helper_signature = inspect.signature(game_builder_module._publish_player_cache)
    default_timeout = helper_signature.parameters["timeout_seconds"].default
    default_stale_window = helper_signature.parameters["stale_lock_seconds"].default
    assert default_stale_window <= 1.0
    assert default_stale_window < default_timeout

    expected_hash = "d" * 64
    cache_root = tmp_path / "cache" / "runtime-d"
    lock_path = Path(str(cache_root) + ".lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(str(2**31 - 1), encoding="ascii")
    stale_time = time.time() - 2
    os.utime(lock_path, (stale_time, stale_time))

    temporary = tmp_path / "runtime-d.tmp"
    temporary.mkdir()
    (temporary / ".ready").write_text(expected_hash, encoding="ascii")

    # Keep the test bounded while leaving stale_lock_seconds at its default.
    published = game_builder_module._publish_player_cache(
        str(temporary),
        str(cache_root),
        expected_hash,
        timeout_seconds=1.0,
    )

    assert published == str(cache_root)
    assert not lock_path.exists()


def test_player_cache_does_not_reclaim_live_lock(tmp_path, native_player_process_probe):
    expected_hash = "c" * 64
    cache_root = tmp_path / "cache" / "runtime-c"
    lock_path = Path(str(cache_root) + ".lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(str(os.getpid()), encoding="ascii")
    stale_time = time.time() - 60
    os.utime(lock_path, (stale_time, stale_time))

    temporary = tmp_path / "runtime-c.tmp"
    temporary.mkdir()
    (temporary / ".ready").write_text(expected_hash, encoding="ascii")

    try:
        with pytest.raises(RuntimeError, match="Timed out waiting"):
            game_builder_module._publish_player_cache(
                str(temporary),
                str(cache_root),
                expected_hash,
                timeout_seconds=0.05,
                stale_lock_seconds=0.01,
            )
        assert lock_path.exists()
        assert os.getpid() in native_player_process_probe
    finally:
        lock_path.unlink(missing_ok=True)


class TestGameBuilderDependencyCollection:
    def test_collect_renderstack_provider_without_scene_component_reference(self, tmp_path):
        builder = _make_builder(tmp_path, tmp_path / "build_output")
        project = Path(builder.project_path)
        scene = project / "Assets" / "Main.scene"
        provider = project / "Assets" / "Rendering" / "StylizedPipeline.py"
        ordinary = project / "Assets" / "Scripts" / "Unused.py"
        provider.parent.mkdir(parents=True, exist_ok=True)
        ordinary.parent.mkdir(parents=True, exist_ok=True)
        scene.write_text("{}", encoding="utf-8")
        provider.write_text(
            "from Infernux.renderstack import DefaultForwardPipeline\n"
            "class StylizedPipeline(DefaultForwardPipeline):\n"
            "    name = 'Stylized'\n",
            encoding="utf-8",
        )
        ordinary.write_text("class Unused:\n    pass\n", encoding="utf-8")
        _write_asset_index(
            project,
            [
                _asset_index_entry(project, scene, "scene-guid", "", "Scene"),
                _asset_index_entry(project, provider, "provider-guid", "", "Script"),
                _asset_index_entry(project, ordinary, "ordinary-guid", "", "Script"),
            ],
        )
        (project / "ProjectSettings" / "BuildSettings.json").write_text(
            json.dumps({"scenes": ["Assets/Main.scene"]}), encoding="utf-8"
        )

        selected = builder._collect_library_asset_entries(builder._asset_index_entries())

        assert set(selected) == {"scene-guid", "provider-guid", "ordinary-guid"}

    def test_collect_user_dependencies_allows_mcp_named_user_requirements(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        (project_root / "requirements.txt").write_text(
            "mcp>=1.24,<2\nfastmcp\n",
            encoding="utf-8",
        )
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        def fake_find_spec(name):
            return object() if name in {"mcp", "fastmcp"} else None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        assert builder._collect_user_dependencies() == ["fastmcp", "mcp"]

    def test_collect_user_dependencies_allows_mcp_named_asset_imports(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "tooling.py", "import mcp\nimport fastmcp\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        def fake_find_spec(name):
            return object() if name in {"mcp", "fastmcp"} else None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        assert builder._collect_user_dependencies() == ["fastmcp", "mcp"]

    def test_project_requirement_files_keeps_user_packages_and_filters_disabled_jit(self, tmp_path):
        project_root = _make_project(tmp_path)
        req_path = project_root / "requirements.txt"
        req_path.write_text(
            "# keep comments\n"
            "mcp>=1.24,<2\n"
            "numba>=0.61\n"
            "llvmlite>=0.44\n"
            "requests>=2\n"
            "fastmcp\n",
            encoding="utf-8",
        )
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        filtered_files = builder._project_requirement_files()

        assert len(filtered_files) == 1
        filtered_text = open(filtered_files[0], "r", encoding="utf-8").read()
        assert "requests>=2" in filtered_text
        assert "mcp>=1.24,<2" in filtered_text.lower()
        assert "fastmcp" in filtered_text.lower()
        assert "numba" not in filtered_text.lower()
        assert "llvmlite" not in filtered_text.lower()
        assert "mcp>=1.24,<2" in req_path.read_text(encoding="utf-8")

    def test_filter_shipped_requirements_keeps_user_packages_and_removes_disabled_jit(self, tmp_path):
        data_dir = tmp_path / "build_output" / "Data"
        settings_dir = data_dir / "ProjectSettings"
        settings_dir.mkdir(parents=True)
        req_path = settings_dir / "requirements.txt"
        req_path.write_text(
            "numba>=0.61.0\n"
            "llvmlite>=0.44\n"
            "mcp>=1.24,<2\n"
            "fastmcp\n"
            "requests>=2\n",
            encoding="utf-8",
        )
        builder = _make_builder(tmp_path, tmp_path / "build_output")

        builder._filter_shipped_requirements(str(data_dir))

        filtered_text = req_path.read_text(encoding="utf-8")
        assert "requests>=2" in filtered_text
        assert "numba" not in filtered_text.lower()
        assert "llvmlite" not in filtered_text.lower()
        assert "mcp>=1.24,<2" in filtered_text.lower()
        assert "fastmcp" in filtered_text.lower()

    def test_nuitka_builder_does_not_reserve_plugin_dependency_names(self):
        assert not NuitkaBuilder._is_game_build_excluded_package("mcp")
        assert not NuitkaBuilder._is_game_build_excluded_package("fastmcp.server")
        assert not NuitkaBuilder._is_game_build_excluded_package("requests")


    def test_nuitka_player_excludes_editor_graph_but_keeps_runtime_viewport_utility(self):
        editor_modules = NuitkaBuilder._PLAYER_EDITOR_ONLY_MODULES

        assert "Infernux.engine.ui.editor_panel" in editor_modules
        assert "Infernux.engine.ui.asset_resource_preview" in editor_modules
        assert "Infernux.engine.ui.window_manager" in editor_modules
        assert "Infernux.engine.i18n" in editor_modules
        assert "Infernux.engine.play_mode" in editor_modules
        assert "Infernux.engine.scene_manager" in editor_modules
        assert "Infernux.engine.scene_document_transaction" in editor_modules
        assert "Infernux.engine.resources_manager" in editor_modules
        assert "Infernux.engine.import_coordinator" in editor_modules
        assert "Infernux.engine.script_compiler" in editor_modules
        expected_authoring_modules = {
            path[:-4].replace("/", ".")
            for path in forbidden_player_service_modules()
            if path.endswith(".pyc")
        }
        assert expected_authoring_modules.issubset(editor_modules)
        assert "Infernux.engine.ui.viewport_utils" not in editor_modules
        assert not NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/player_scene.py"
        )
        assert not NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/runtime_scene_transaction.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/scene_document_transaction.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/import_coordinator.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/script_compiler.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/_bootstrap_panels.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/_bootstrap_selection.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/_bootstrap_trace.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/_bootstrap_wiring.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/bootstrap_inspector/_wire.py"
        )
        assert not NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/build_settings.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/candidate_import.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/library_sync.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "engine/preferences_store.py"
        )
        assert NuitkaBuilder._is_player_runtime_excluded_source(
            "gizmos/collector.py"
        )
        assert "infernux_mcp" not in NuitkaBuilder._GAME_BUILD_NOFOLLOW_MODULES

    def test_collect_user_dependencies_adds_llvmlite_for_numba_import(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "stress.py", "import numba\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")
        builder.enable_jit = True

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name):
            if name in {"numba", "llvmlite", "numpy"}:
                return object()
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        deps = builder._collect_user_dependencies()

        assert deps == ["llvmlite", "numba", "numpy"]


class TestGameBuilderAutoParallelExport:
    def test_compile_user_scripts_embeds_auto_parallel_without_sidecar(self, tmp_path):
        output_dir = tmp_path / "build_output"
        assets_dir = output_dir / "Data" / "Assets"
        assets_dir.mkdir(parents=True)

        script_path = assets_dir / "stress.py"
        stale_sidecar = assets_dir / "stress.autop.pyc"
        stale_sidecar.write_bytes(b"obsolete")
        script_path.write_text(
            "from Infernux.jit import njit\n"
            "@njit(cache=True, auto_parallel=True)\n"
            "def burn(n):\n"
            "    acc = 0\n"
            "    for i in range(n):\n"
            "        acc += i\n"
            "    return acc\n",
            encoding="utf-8",
        )

        builder = _make_builder(tmp_path, output_dir)
        builder.enable_jit = True
        _bind_staged_script_to_asset_index(
            builder,
            output_dir,
            script_path,
            guid="auto-parallel-script-guid",
        )
        builder._compile_user_scripts(str(output_dir))

        assert not script_path.exists()
        bytecode_path = assets_dir / "stress.pyc"
        assert bytecode_path.is_file()
        assert not stale_sidecar.exists()

        loader = importlib.machinery.SourcelessFileLoader(
            "infernux_test_embedded_auto_parallel",
            str(bytecode_path),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        assert module.burn(10) == 45
        assert module.burn.parallel is not module.burn.serial
        manifest = module.__infernux_jit_manifest__["burn"]
        assert len(manifest["hir_fingerprint"]) == 64
        assert module.burn.compiler_fingerprint == manifest["hir_fingerprint"]

    def test_compile_user_scripts_skips_sidecar_for_non_auto_parallel_script(self, tmp_path):
        output_dir = tmp_path / "build_output"
        assets_dir = output_dir / "Data" / "Assets"
        assets_dir.mkdir(parents=True)

        script_path = assets_dir / "plain.py"
        script_path.write_text(
            "from Infernux.jit import njit\n"
            "@njit(cache=True)\n"
            "def burn(n):\n"
            "    acc = 0\n"
            "    for i in range(n):\n"
            "        acc += i\n"
            "    return acc\n",
            encoding="utf-8",
        )

        builder = _make_builder(tmp_path, output_dir)
        _bind_staged_script_to_asset_index(
            builder,
            output_dir,
            script_path,
            guid="serial-jit-script-guid",
        )
        builder._compile_user_scripts(str(output_dir))

        assert not script_path.exists()
        assert (assets_dir / "plain.pyc").is_file()
        assert not (assets_dir / "plain.autop.pyc").exists()

    def test_compile_user_scripts_rejects_required_unsafe_kernel(self, tmp_path):
        output_dir = tmp_path / "build_output"
        assets_dir = output_dir / "Data" / "Assets"
        assets_dir.mkdir(parents=True)
        script_path = assets_dir / "unsafe.py"
        script_path.write_text(
            "from Infernux.jit import njit\n"
            "@njit(auto_parallel=True, parallel_policy='required')\n"
            "def prefix(values):\n"
            "    for i in range(1, len(values)):\n"
            "        values[i] = values[i - 1] + 1\n",
            encoding="utf-8",
        )

        builder = _make_builder(tmp_path, output_dir)
        builder.enable_jit = True
        _bind_staged_script_to_asset_index(
            builder,
            output_dir,
            script_path,
            guid="unsafe-parallel-script-guid",
        )
        with pytest.raises(RuntimeError, match="auto_parallel compilation rejected"):
            builder._compile_user_scripts(str(output_dir))

    def test_compile_user_scripts_without_jit_keeps_auto_kernel_serial_only(self, tmp_path):
        output_dir = tmp_path / "build_output"
        assets_dir = output_dir / "Data" / "Assets"
        assets_dir.mkdir(parents=True)
        script_path = assets_dir / "auto.py"
        script_path.write_text(
            "from Infernux.jit import njit\n"
            "@njit(auto_parallel=True)\n"
            "def fill(values):\n"
            "    for i in range(len(values)):\n"
            "        values[i] = i\n",
            encoding="utf-8",
        )

        builder = _make_builder(tmp_path, output_dir)
        builder.enable_jit = False
        _bind_staged_script_to_asset_index(
            builder,
            output_dir,
            script_path,
            guid="serial-auto-script-guid",
        )
        builder._compile_user_scripts(str(output_dir))

        bytecode = (assets_dir / "auto.pyc").read_bytes()
        assert b"__infernux_jit_manifest__" not in bytecode
        assert b"__infernux_parallel_" not in bytecode

    def test_compile_user_scripts_without_jit_rejects_required_policy(self, tmp_path):
        output_dir = tmp_path / "build_output"
        assets_dir = output_dir / "Data" / "Assets"
        assets_dir.mkdir(parents=True)
        (assets_dir / "required.py").write_text(
            "from Infernux.jit import njit\n"
            "@njit(auto_parallel=True, parallel_policy='required')\n"
            "def fill(values):\n"
            "    for i in range(len(values)):\n"
            "        values[i] = i\n",
            encoding="utf-8",
        )

        builder = _make_builder(tmp_path, output_dir)
        builder.enable_jit = False
        _bind_staged_script_to_asset_index(
            builder,
            output_dir,
            assets_dir / "required.py",
            guid="required-parallel-script-guid",
        )
        with pytest.raises(RuntimeError, match="Auto Parallel build option"):
            builder._compile_user_scripts(str(output_dir))

    def test_collect_user_dependencies_detects_public_infernux_jit_api(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "jit_user.py", "from Infernux.jit import njit\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")
        builder.enable_jit = True

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name):
            if name in {"numba", "llvmlite", "numpy"}:
                return object()
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        deps = builder._collect_user_dependencies()

        assert deps == ["llvmlite", "numba", "numpy"]

    def test_collect_user_dependencies_keeps_public_jit_api_serial_when_disabled(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "jit_user.py", "from Infernux.jit import njit\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")
        builder.enable_jit = False

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name):
            if name in {"numba", "llvmlite", "numpy"}:
                return object()
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        assert builder._collect_user_dependencies() == []

    def test_collect_user_dependencies_rejects_direct_numba_when_disabled(self, tmp_path):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "jit_user.py", "import numba\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")
        builder.enable_jit = False

        with pytest.raises(RuntimeError, match="Auto Parallel is disabled"):
            builder._collect_user_dependencies()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SDK environment")
class TestNuitkaWindowsSdkEnvironment:
    def test_augment_windows_sdk_environment_adds_kits_tools_and_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")

        sdk_root = tmp_path / "Windows Kits" / "10"
        sdk_version = "10.0.22621.0"
        for relative_dir in (
            f"Include/{sdk_version}/ucrt",
            f"Include/{sdk_version}/shared",
            f"Include/{sdk_version}/um",
            f"Include/{sdk_version}/winrt",
            f"Lib/{sdk_version}/ucrt/x64",
            f"Lib/{sdk_version}/um/x64",
            f"bin/{sdk_version}/x64",
        ):
            (sdk_root / relative_dir).mkdir(parents=True, exist_ok=True)
        (sdk_root / "Include" / sdk_version / "um" / "Windows.h").write_text("", encoding="utf-8")

        for tool_name in ("rc.exe", "mt.exe"):
            tool_path = sdk_root / "bin" / sdk_version / "x64" / tool_name
            tool_path.write_text("", encoding="utf-8")
            tool_path.chmod(0o755)

        msvc_bin = tmp_path / "msvc" / "bin"
        msvc_bin.mkdir(parents=True)
        for tool_name in ("cl.exe", "link.exe"):
            tool_path = msvc_bin / tool_name
            tool_path.write_text("", encoding="utf-8")
            tool_path.chmod(0o755)
        msvc_include = tmp_path / "msvc" / "include"
        msvc_lib = tmp_path / "msvc" / "lib" / "x64"
        msvc_include.mkdir(parents=True)
        msvc_lib.mkdir(parents=True)
        (msvc_include / "excpt.h").write_text("", encoding="utf-8")
        (msvc_lib / "vcruntime.lib").write_text("", encoding="utf-8")

        monkeypatch.setattr(nuitka_builder_module, "_windows_sdk_roots_from_registry", lambda: [str(sdk_root)])
        env = {
            "PATH": str(msvc_bin),
            "INCLUDE": str(msvc_include),
            "LIB": str(msvc_lib),
        }

        augmented = nuitka_builder_module._augment_windows_sdk_environment(env)
        forced = nuitka_builder_module._force_msvc_tool_variables(augmented)

        assert nuitka_builder_module._msvc_env_ready(forced)
        assert str(sdk_root / "bin" / sdk_version / "x64") in forced["PATH"]
        assert str(sdk_root / "Include" / sdk_version / "um") in forced["INCLUDE"]
        assert str(sdk_root / "Lib" / sdk_version / "um" / "x64") in forced["LIB"]
        assert str(sdk_root / "Lib" / sdk_version / "um" / "x64") in forced["LIBPATH"]
        assert forced["WindowsSdkBinPath"].rstrip("\\/").endswith(str(sdk_root / "bin" / sdk_version / "x64"))
        assert forced["UniversalCRTSdkDir"].rstrip("\\/").endswith(str(sdk_root))
        assert forced["MSSDK_DIR"].rstrip("\\/").endswith(str(sdk_root))
        assert forced["WindowsSDKVersion"] == sdk_version
        assert forced["CC"].endswith("cl.exe")
        assert forced["CXX"].endswith("cl.exe")
        assert "LINK" not in forced
        assert forced["RC"].endswith("rc.exe")
        assert forced["MT"].endswith("mt.exe")

    def test_force_msvc_tool_variables_removes_link_environment_options(self, tmp_path):
        tool_dir = tmp_path / "Program Files" / "MSVC" / "bin"
        tool_dir.mkdir(parents=True)
        for tool_name in ("cl.exe", "link.exe", "rc.exe", "mt.exe"):
            tool_path = tool_dir / tool_name
            tool_path.write_text("", encoding="utf-8")
            tool_path.chmod(0o755)

        forced = nuitka_builder_module._force_msvc_tool_variables({
            "PATH": str(tool_dir),
            "LINK": str(tool_dir / "link.exe"),
            "_LINK_": str(tool_dir / "link.exe"),
        })

        assert forced["CC"].endswith("cl.exe")
        assert forced["CXX"].endswith("cl.exe")
        assert "LINK" not in forced
        assert "_LINK_" not in forced
        assert forced["RC"].endswith("rc.exe")
        assert forced["MT"].endswith("mt.exe")

    def test_sdk_only_environment_is_not_ready_without_msvc_headers_and_libs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")

        sdk_root = tmp_path / "Windows Kits" / "10"
        sdk_version = "10.0.26100.0"
        for relative_dir in (
            f"Include/{sdk_version}/ucrt",
            f"Include/{sdk_version}/shared",
            f"Include/{sdk_version}/um",
            f"Lib/{sdk_version}/ucrt/x64",
            f"Lib/{sdk_version}/um/x64",
            f"bin/{sdk_version}/x64",
        ):
            (sdk_root / relative_dir).mkdir(parents=True, exist_ok=True)
        (sdk_root / "Include" / sdk_version / "um" / "Windows.h").write_text("", encoding="utf-8")
        for tool_name in ("rc.exe", "mt.exe"):
            tool_path = sdk_root / "bin" / sdk_version / "x64" / tool_name
            tool_path.write_text("", encoding="utf-8")
            tool_path.chmod(0o755)

        msvc_bin = tmp_path / "msvc" / "bin"
        msvc_bin.mkdir(parents=True)
        for tool_name in ("cl.exe", "link.exe"):
            tool_path = msvc_bin / tool_name
            tool_path.write_text("", encoding="utf-8")
            tool_path.chmod(0o755)

        monkeypatch.setattr(nuitka_builder_module, "_windows_sdk_roots_from_registry", lambda: [str(sdk_root)])
        env = nuitka_builder_module._augment_windows_sdk_environment({"PATH": str(msvc_bin)})

        missing = nuitka_builder_module._msvc_env_missing_parts(env)
        assert "MSVC INCLUDE (excpt.h)" in missing
        assert "MSVC LIB (vcruntime.lib)" in missing
        assert not nuitka_builder_module._msvc_env_ready(env)

    def test_windows_sdk_roots_include_explicit_override(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")
        sdk_root = tmp_path / "custom-sdk"
        sdk_root.mkdir()
        monkeypatch.setenv("INFERNUX_WINDOWS_SDK_DIR", str(sdk_root))
        monkeypatch.setattr(nuitka_builder_module, "_windows_sdk_roots_from_registry", lambda: [])

        assert str(sdk_root) in nuitka_builder_module._windows_sdk_roots({})

    def test_msvc_environment_scripts_include_explicit_vs_root(self, tmp_path, monkeypatch):
        vs_root = tmp_path / "VS"
        script_path = vs_root / "Common7" / "Tools" / "VsDevCmd.bat"
        script_path.parent.mkdir(parents=True)
        script_path.write_text("", encoding="utf-8")
        monkeypatch.setenv("INFERNUX_VSINSTALLDIR", str(vs_root))
        monkeypatch.delenv("INFERNUX_VCVARSALL", raising=False)
        monkeypatch.setattr(nuitka_builder_module, "_visual_studio_roots_from_vswhere", lambda: [])
        monkeypatch.setattr(nuitka_builder_module, "_visual_studio_roots_from_registry", lambda: [])

        assert (str(script_path), ["-arch=x64", "-host_arch=x64"]) in nuitka_builder_module._find_msvc_environment_scripts()

    def test_windows_nuitka_command_does_not_force_msvc_latest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nuitka_builder_module.sys, "platform", "win32")
        monkeypatch.setattr(nuitka_builder_module, "_has_msvc_toolchain", lambda: True)

        builder = object.__new__(NuitkaBuilder)
        builder._builder_python = "python"
        builder.console_mode = "disable"
        builder._staging_dir = str(tmp_path / "stage")
        builder.output_filename = "Game.exe"
        builder.lto = False
        builder.extra_include_packages = []
        builder.extra_include_data = []
        builder.raw_copy_packages = []
        builder.product_name = "Game"
        builder.file_version = "1.0.0.0"
        builder.icon_path = None
        builder._staged_entry = str(tmp_path / "boot.py")

        cmd = builder._build_command()

        assert "--msvc=latest" not in cmd
        assert "--standalone" in cmd
        assert "--follow-imports" in cmd
        assert "--include-module=_InfernuxBootstrap" in cmd
        assert "--include-module=ctypes" in cmd
        assert "--include-module=bz2" not in cmd
        assert "--include-module=lzma" not in cmd
