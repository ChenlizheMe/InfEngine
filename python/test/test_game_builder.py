from __future__ import annotations

import importlib.util
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
from Infernux.engine.runtime_artifact_catalog import unix_ns_to_filetime_ticks
from Infernux.engine import nuitka_builder as nuitka_builder_module
from Infernux.engine.nuitka_builder import NuitkaBuilder
from Infernux.engine.player_package_native import (
    extract_pack,
    read_entry,
    read_manifest,
    set_test_backend,
    write_pack,
)
from Infernux.particle.asset import ParticleGraphAsset


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
    scene_path = project_root / "main.scene"
    scene_path.write_text(
        json.dumps({"objects": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (settings_dir / "BuildSettings.json").write_text(
        json.dumps({"scenes": [str(scene_path)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_asset_index(project_root, [])
    return project_root


def _make_builder(tmp_path, output_dir):
    project_root = _make_project(tmp_path)
    return GameBuilder(str(project_root), str(output_dir), game_name="TestGame")


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
        (final_dir / f"{builder.project_name}.exe").write_bytes(
            b"Infernux Player"
        )
    return data_root


def _write_asset_script(project_root, relative_path: str, source: str) -> None:
    script_path = project_root / "Assets" / relative_path
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(source, encoding="utf-8")


def _reference_particle_graph(project_root: Path, stable_id: str) -> Path:
    graph_path = project_root / "Assets" / "VFX" / f"{stable_id}.particlegraph"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = ParticleGraphAsset(stable_id=stable_id, name=stable_id)
    graph_path.write_text(graph.canonical_json(), encoding="utf-8")
    guid = hashlib.md5(stable_id.encode("utf-8")).hexdigest()
    (project_root / "main.scene").write_text(
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
            _asset_index_entry(project_root, project_root / "main.scene", "scene-guid", "", "Scene"),
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
    return {
        "normalized_path": str(source.resolve()).replace("\\", "/").casefold(),
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


def _write_texture_asset_index(project_root: Path, source: Path, guid: str, artifact_path: str):
    relative_source = source.resolve().relative_to(project_root.resolve()).as_posix()
    scene_path = project_root / "main.scene"
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
    builder = GameBuilder(
        str(project_root), str(tmp_path / "build_output"), game_name="TestGame"
    )

    builder._validate()


def test_rewrite_build_settings_keeps_project_relative_scene_identity(tmp_path):
    project_root = _make_project(tmp_path)
    settings_path = project_root / "ProjectSettings" / "BuildSettings.json"
    settings_path.write_text(
        json.dumps({"scenes": ["main.scene"]}), encoding="utf-8"
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
    assert rewritten["scenes"] == ["main.scene"]


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
    for filename in ("python312.dll", "_ctypes.pyd", "ffi.dll", "zlib.dll"):
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
    assert (dist / "stdlib" / "encodings" / "__init__.pyc").is_file()
    assert not list((dist / "stdlib" / "encodings").glob("*.py"))


def test_player_module_stages_source_less_engine_runtime(tmp_path, monkeypatch):
    import Infernux

    source = tmp_path / "source" / "Infernux"
    (source / "engine").mkdir(parents=True)
    (source / "lib").mkdir()
    (source / "mcp").mkdir()
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
    (source / "engine" / "data.json").write_text("{}\n", encoding="utf-8")
    (source / "engine" / "game_builder.py").write_text("BUILD = 1\n", encoding="utf-8")
    (source / "lib" / "__init__.py").write_text("NATIVE = True\n", encoding="utf-8")
    (source / "lib" / "stale.dll").write_bytes(b"stale")
    (source / "mcp" / "server.py").write_text("SERVER = 1\n", encoding="utf-8")

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
    assert (runtime / "engine" / "data.json").is_file()
    assert (runtime / "lib" / "__init__.pyc").is_file()
    assert (runtime / "lib" / "native.dll").read_bytes() == b"native"
    assert not (runtime / "lib" / "stale.dll").exists()
    assert not (runtime / "engine" / "game_builder.pyc").exists()
    assert not (runtime / "mcp").exists()
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
    assert captured["raw_copy_packages"] == ["numpy"]
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
    assert captured["raw_copy_packages"] == ["numpy"]
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


def test_pack_core_runtime_moves_boot_deferred_stdlib_extensions(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    final_dir.mkdir()
    data_root = final_dir / "TestGame_Data"
    data_root.mkdir()
    deferred = sorted(builder._PLAYER_DEFERRED_STDLIB_FILES)
    for index, filename in enumerate(deferred):
        (final_dir / filename).write_bytes(f"deferred-{index}".encode("ascii"))
    (final_dir / "late-runtime.dll").write_bytes(b"move into runtime")
    (final_dir / "Infernux" / "resources").mkdir(parents=True)
    (final_dir / "Infernux" / "resources" / "runtime.txt").write_text(
        "runtime", encoding="utf-8"
    )
    (final_dir / "Infernux" / "engine" / "locales").mkdir(parents=True)
    builder._pack_core_runtime_archive(str(final_dir))

    manifest = read_manifest(data_root / builder._RUNTIME_ARCHIVE_FILENAME)
    paths = {entry["path"] for entry in manifest["files"]}
    assert {f"stdlib/{filename}" for filename in deferred} <= paths
    assert "stdlib/late-runtime.dll" in paths
    assert "stdlib/__future__.pyc" not in paths
    assert all(not (final_dir / filename).exists() for filename in deferred)
    assert not (final_dir / "late-runtime.dll").exists()
    assert not (final_dir / "Infernux").exists()


def test_pack_core_runtime_moves_full_native_closure_off_root(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    package_lib = final_dir / "Infernux" / "lib"
    package_lib.mkdir(parents=True)
    data_root.mkdir(parents=True)
    (package_lib / "_Infernux.pyd").write_bytes(b"full bridge")
    (package_lib / "InfernuxFoundation.dll").write_bytes(b"foundation")
    (package_lib / "InfernuxRuntime.dll").write_bytes(b"runtime")
    for legacy_name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS:
        (package_lib / legacy_name).write_bytes(b"legacy static dependency")
        (final_dir / legacy_name).write_bytes(b"legacy root dependency")
    (final_dir / "_InfernuxBootstrap.pyd").write_bytes(b"bootstrap")
    (final_dir / "InfernuxFoundation.dll").write_bytes(b"foundation")
    (final_dir / "InfernuxRuntime.dll").write_bytes(b"runtime")
    (final_dir / "python312.dll").write_bytes(b"python")
    (final_dir / "_ctypes.pyd").write_bytes(b"ctypes ABI")
    (final_dir / "ffi.dll").write_bytes(b"libffi ABI")
    (final_dir / "_socket.pyd").write_bytes(b"socket")

    builder._pack_core_runtime_archive(str(final_dir))

    paths = {
        entry["path"]
        for entry in read_manifest(data_root / builder._RUNTIME_ARCHIVE_FILENAME)["files"]
    }
    assert "Infernux/lib/_Infernux.pyd" in paths
    assert "Infernux/lib/InfernuxFoundation.dll" in paths
    assert "Infernux/lib/InfernuxRuntime.dll" in paths
    assert "stdlib/_socket.pyd" in paths
    assert (final_dir / "_InfernuxBootstrap.pyd").is_file()
    assert (final_dir / "InfernuxFoundation.dll").is_file()
    assert (final_dir / "python312.dll").is_file()
    assert (final_dir / "_ctypes.pyd").is_file()
    assert (final_dir / "ffi.dll").is_file()
    assert "stdlib/_ctypes.pyd" not in paths
    assert "stdlib/ffi.dll" not in paths
    assert not {
        f"Infernux/lib/{name}" for name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS
    } & paths
    assert not {
        f"stdlib/{name}" for name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS
    } & paths
    assert not (final_dir / "InfernuxRuntime.dll").exists()
    assert all(
        not (final_dir / name).exists()
        for name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS
    )
    assert not (final_dir / "Infernux").exists()


def test_bootstrap_archive_preserves_player_module_abi_filename(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    package_lib = final_dir / "Infernux" / "lib"
    data_root.mkdir(parents=True)
    package_lib.mkdir(parents=True)
    for filename in (
        "python312.dll",
        "_ctypes.pyd",
        "ffi.dll",
        "_InfernuxBootstrap.pyd",
    ):
        (final_dir / filename).write_bytes(filename.encode("ascii"))
    module_name = "_InfernuxPlayer.cp312-win_amd64.pyd"
    (final_dir / module_name).write_bytes(b"player module")
    (package_lib / "InfernuxFoundation.dll").write_bytes(b"foundation")
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
    assert all(not entry["path"].endswith(".pyi") for entry in module_manifest["files"])

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
        "_Infernux.cp312-win_amd64.pyd"
        if sys.platform == "win32"
        else "_Infernux.so"
    )
    (native_root / native_module).write_bytes(b"module")
    bootstrap_module = (
        "_InfernuxBootstrap.cp312-win_amd64.pyd"
        if sys.platform == "win32"
        else "_InfernuxBootstrap.so"
    )
    (native_root / bootstrap_module).write_bytes(b"bootstrap")
    companion = native_root / (
        "InfernuxRuntime.dll" if sys.platform == "win32" else "libInfernuxRuntime.so"
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
        "_Infernux.cp312-win_amd64.pyd"
        if sys.platform == "win32"
        else "_Infernux.so"
    )
    companion = (
        "InfernuxRuntime.dll" if sys.platform == "win32" else "libInfernuxRuntime.so"
    )
    (native_root / native_module).write_bytes(b"current-module")
    bootstrap_module = (
        "_InfernuxBootstrap.cp312-win_amd64.pyd"
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
    python_runtime = native_root / "python312.dll" if sys.platform == "win32" else None
    if python_runtime is not None:
        python_runtime.write_bytes(b"python-runtime")
        (native_root / "zlib.dll").write_bytes(b"runtime-zlib")
        for legacy_name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS:
            (native_root / legacy_name).write_bytes(b"legacy static dependency")
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
        for legacy_name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS:
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
        assert (dist / "python312.dll").read_bytes() == b"python-runtime"
        assert not (package_lib / "python312.dll").exists()
        assert (package_lib / "zlib.dll").read_bytes() == b"runtime-zlib"
        assert not (dist / "zlib.dll").exists()
        for legacy_name in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS:
            assert not (dist / legacy_name).exists()
            assert not (package_lib / legacy_name).exists()


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


def test_current_player_layout_uses_one_renamed_executable(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    host = tmp_path / "InfernuxPlayerHost.exe"
    host.write_bytes(b"host")
    monkeypatch.setattr(builder, "_player_host_path", lambda: str(host))

    dist = tmp_path / "staging" / "generic.dist"
    dist.mkdir(parents=True)
    module_name = "_InfernuxPlayer.pyd" if sys.platform == "win32" else "_InfernuxPlayer.so"
    (dist / module_name).write_bytes(b"player module")
    (dist / "python312.dll").write_bytes(b"python")
    final_dir = Path(builder._organize_output(str(dist)))
    (final_dir / "Data").mkdir()
    (final_dir / "Data" / "BuildManifest.json").write_text("{}", encoding="utf-8")

    builder._organize_player_layout(str(final_dir))
    builder._write_output_marker(str(final_dir))

    data_root = final_dir / "TestGame_Data"
    assert (final_dir / "TestGame.exe").read_bytes() == b"host"
    assert [path.name for path in final_dir.glob("*.exe")] == ["TestGame.exe"]
    assert (final_dir / module_name).read_bytes() == b"player module"
    assert (final_dir / "python312.dll").read_bytes() == b"python"
    assert (final_dir / GameBuilder.OUTPUT_MARKER_FILENAME).is_file()
    assert (data_root / "BuildManifest.json").is_file()
    assert not (data_root / "Runtime").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PE resource contract")
def test_windows_launcher_icon_is_replaced_with_project_icon(tmp_path):
    launcher = (
        Path(__file__).parents[1]
        / "Infernux"
        / "resources"
        / "player"
        / "InfernuxLauncher.exe"
    )
    project_icon = (
        Path(__file__).parents[1]
        / "Infernux"
        / "resources"
        / "icons"
        / "icon.png"
    )
    executable = tmp_path / "BrandedGame.exe"
    shutil.copy2(launcher, executable)

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
        json.dumps({"scenes": ["main.scene"]}), encoding="utf-8"
    )

    builder._process_build_icon(str(final_dir))
    builder._process_splash_items(str(final_dir))
    builder._generate_manifest(str(final_dir))
    (final_dir / "TestGame.exe").write_bytes(b"player")
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
    artifact = (
        Path(builder.project_path)
        / "Library"
        / "Artifacts"
        / "RenderEffect"
        / "bloom.inxeffect"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"$schema":"infernux.render_effect_artifact"}', encoding="utf-8")
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

    (final_dir / "TestGame.exe").write_bytes(b"Infernux Player")
    builder._organize_player_layout(str(final_dir))
    builder._pack_content_archive(str(final_dir))
    header = read_manifest(
        final_dir / "TestGame_Data" / builder._CONTENT_ARCHIVE_FILENAME
    )
    assert "Library/Artifacts/RenderEffect/bloom.inxeffect" in {
        entry["path"] for entry in header["files"]
    }


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
    (final_dir / "TestGame.exe").write_bytes(b"Infernux Player")
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

    (final_dir / "TestGame.exe").write_bytes(b"Infernux Player")
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


def test_game_data_requires_reachable_particle_artifact(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    project = Path(builder.project_path)
    _reference_particle_graph(project, "missing")
    (project / "Library" / "Artifacts" / "Particle").mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="Library artifact selection failed"):
        builder._copy_game_data(str(tmp_path / "dist"))


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


def test_game_data_collects_sampled_particle_interface_artifacts(tmp_path):
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
            _asset_index_entry(project, project / "main.scene", "scene-guid", "", "Scene"),
            _asset_index_entry(project, graph_path, particle_guid, "", "ParticleGraph"),
            *texture_entries,
        ],
    )

    final_dir = tmp_path / "dist"
    builder._copy_game_data(str(final_dir))

    shipped = final_dir / "Data" / "Library" / "Artifacts"
    assert (shipped / "Texture" / texture_artifact.name).read_bytes() == texture_artifact.read_bytes()
    assert (shipped / "Texture" / sdf_artifact.name).read_bytes() == sdf_artifact.read_bytes()
    assert not (shipped / "Texture" / unused_artifact.name).exists()


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

    final_dir = tmp_path / "dist"
    builder._copy_game_data(str(final_dir))

    staged_settings = final_dir / "Data" / "ProjectSettings"
    assert (staged_settings / "BuildSettings.json").is_file()
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

    builder._pack_content_archive(str(tmp_path / "dist"))

    archive = data / builder._CONTENT_ARCHIVE_FILENAME
    manifest = read_manifest(archive)
    names = {entry["path"] for entry in manifest["files"]}
    assert names == {"Assets/Main.scene"}
    payload = read_entry(archive, "Assets/Main.scene").decode("utf-8")
    serialized = json.loads(payload)
    assert serialized["graph"] == "Assets/VFX/Smoke.particlegraph"
    assert serialized["external"] == "D:/External/Shared.asset"
    assert builder.project_path.casefold() not in payload.casefold()


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


def test_content_archive_keeps_compiled_scripts_and_runtime_documents(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    data = tmp_path / "dist" / "TestGame_Data"
    assets = data / "Assets" / "Scripts"
    assets.mkdir(parents=True)
    (assets / "Player.pyc").write_bytes(b"compiled-player")
    (assets / "Player.py.meta").write_text("editor metadata", encoding="utf-8")
    (assets / "Main.scene").write_text(
        '{"name":"Main","objects":[]}', encoding="utf-8"
    )
    (data / "BuildManifest.json").write_text(
        '{"game_name":"TestGame"}', encoding="utf-8"
    )

    builder._pack_content_archive(str(tmp_path / "dist"))

    header = read_manifest(data / builder._CONTENT_ARCHIVE_FILENAME)
    names = {entry["path"] for entry in header["files"]}
    assert "Assets/Scripts/Player.pyc" in names
    assert "Assets/Scripts/Main.scene" in names
    assert not any(name.endswith(".meta") for name in names)
    assert not (assets / "Player.pyc").exists()
    assert not (assets / "Main.scene").exists()


def test_core_runtime_archive_replaces_loose_numpy_and_resources(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    numpy_file = final_dir / "numpy" / "core.py"
    numpy_dll = final_dir / "numpy.libs" / "openblas.dll"
    numpy_header = final_dir / "numpy" / "_core" / "include" / "numpy" / "arrayobject.h"
    numpy_example = final_dir / "numpy" / "random" / "_examples" / "extending.pyx"
    numpy_stub = final_dir / "numpy" / "typing" / "_array_like.pyi"
    numpy_tests_extension = (
        final_dir / "numpy" / "_core" / "_multiarray_tests.cp312-win_amd64.pyd"
    )
    numpy_api_changes = final_dir / "numpy" / "ma" / "API_CHANGES.txt"
    numpy_license = final_dir / "numpy" / "LICENSE.txt"
    font = final_dir / "Infernux" / "resources" / "fonts" / "engine.otf"
    gizmo_icon = final_dir / "Infernux" / "resources" / "icons" / "gizmo_camera.png"
    editor_icon = final_dir / "Infernux" / "resources" / "icons" / "file.png"
    numpy_file.parent.mkdir(parents=True)
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
    launcher = final_dir / "Infernux" / "resources" / "player" / "InfernuxLauncher.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"legacy editor launcher")
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
    content_source = data_root / "Assets" / "Main.scene"
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


def test_payload_manifest_accepts_reachable_scene_material_and_audio(tmp_path):
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

    builder._write_payload_manifest(str(final_dir))

    assert (data_root / "Library" / "RuntimeAssetCatalog.json").is_file()
    assert getattr(builder, "_unproven_residual_runtime_assets", []) == []


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

    with pytest.raises(RuntimeError, match="Assets/Unused.mat"):
        builder._write_payload_manifest(str(final_dir))


def test_copy_stage_prunes_indexed_unreachable_asset_before_content_pack(tmp_path):
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
    unindexed.write_bytes(b"future resource group payload")
    final_dir = tmp_path / "dist"

    builder._copy_game_data(str(final_dir))

    staged = final_dir / "Data"
    assert (staged / "Assets" / "Main.scene").is_file()
    assert (staged / "Assets" / "Materials" / "Bird.mat").is_file()
    assert (staged / "Assets" / "Audio" / "Wing.wav").is_file()
    assert not (staged / "Assets" / "Unused.mat").exists()
    assert not (staged / "Assets" / "Unused.mat.meta").exists()
    assert (staged / "Assets" / "Dynamic" / "Runtime.bin").is_file()

    (final_dir / "TestGame.exe").write_bytes(b"Infernux Player")
    builder._organize_player_layout(str(final_dir))
    data_root = final_dir / "TestGame_Data"
    runtime_source = tmp_path / "runtime.bin"
    runtime_source.write_bytes(b"runtime")
    write_pack(
        (("Infernux/resources/runtime.bin", runtime_source),),
        data_root / builder._RUNTIME_ARCHIVE_FILENAME,
    )
    builder._pack_content_archive(str(final_dir))
    builder._write_payload_manifest(str(final_dir))

    content_names = {
        entry["path"]
        for entry in read_manifest(data_root / builder._CONTENT_ARCHIVE_FILENAME)["files"]
    }
    assert {
        "Assets/Main.scene",
        "Assets/Materials/Bird.mat",
        "Assets/Audio/Wing.wav",
    } <= content_names
    assert "Assets/Unused.mat" not in content_names
    assert "Assets/Dynamic/Runtime.bin" in content_names
    assert (data_root / "Library" / "RuntimeAssetCatalog.json").is_file()


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

    with pytest.raises(RuntimeError, match="Assets/Textures/albedo.png"):
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
    assert catalog["player_host"]["executable"] == "TestGame.exe"
    assert {package["path"] for package in catalog["packages"]} == {
        "TestGame_Data/Runtime.inxrt",
        "TestGame_Data/Content.inxpkg",
    }
    assert all(package["archive_sha256"] for package in catalog["packages"])
    assert len(catalog["artifacts"]) == 2
    assert all(
        artifact["runtime_artifact_id"].startswith("ra_")
        and artifact["content_sha256"]
        and artifact["dependencies"] == []
        for artifact in catalog["artifacts"]
    )


def test_payload_manifest_reads_only_dependency_bearing_documents(
    tmp_path,
    monkeypatch,
):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data_root = final_dir / "TestGame_Data"
    data_root.mkdir(parents=True)
    (final_dir / "TestGame.exe").write_bytes(b"Infernux Player")

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

    residual_entries: list[dict[str, object]] = []
    monkeypatch.setattr(game_builder_module, "read_entry", counted_read_entry)
    builder._residual_direct_runtime_assets = lambda entries: (
        residual_entries.extend(entries) or []
    )

    builder._write_payload_manifest(str(final_dir))

    assert set(reads) == {
        "Library/Particle/RuntimeIndex.json",
        "Assets/Main.scene",
        "Assets/Materials/Test.mat",
    }
    assert len(residual_entries) == len(runtime_entries) + 3
    assert any(entry["runtime_path"] == "stdlib/module_127.pyc" for entry in residual_entries)
    assert any(entry["runtime_path"] == "Infernux/lib/native_63.dll" for entry in residual_entries)

    catalog = json.loads(
        (data_root / "Library" / "RuntimeAssetCatalog.json").read_text(
            encoding="utf-8"
        )
    )
    artifacts_by_path = {
        artifact_record["runtime_path"]: artifact_record
        for artifact_record in catalog["artifacts"]
    }
    material_id = artifacts_by_path["Assets/Materials/Test.mat"][
        "runtime_artifact_id"
    ]
    assert artifacts_by_path["Assets/Main.scene"]["dependencies"] == [material_id]
    assert artifacts_by_path["Library/Particle/RuntimeIndex.json"][
        "dependencies"
    ] == [material_id]


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
        assert '_STDLIB_RUNTIME_DIR' in boot_source
        assert 'os.add_dll_directory(_dll_dir)' in boot_source
        assert '_CONTENT_ARCHIVE = os.path.join(_DATA_ROOT, "Content.inxpkg")' in boot_source
        assert '_PARALLEL_ARCHIVE = os.path.join(_DATA_ROOT, "Modules", "Parallel.inxmod")' in boot_source
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
        assert "os.path.dirname(os.path.abspath(sys.argv[0]))" in pre_native_boot
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
    def test_collect_user_dependencies_excludes_mcp_packages_from_requirements(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        (project_root / "requirements.txt").write_text(
            "mcp>=1.24,<2\nfastmcp\n",
            encoding="utf-8",
        )
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        def fake_find_spec(name):
            assert name not in {"mcp", "fastmcp"}
            return None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        assert builder._collect_user_dependencies() == []

    def test_collect_user_dependencies_excludes_mcp_packages_from_asset_imports(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "tooling.py", "import mcp\nimport fastmcp\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        def fake_find_spec(name):
            assert name not in {"mcp", "fastmcp"}
            return None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        assert builder._collect_user_dependencies() == []

    def test_project_requirement_files_filters_mcp_for_game_build(self, tmp_path):
        project_root = _make_project(tmp_path)
        req_path = project_root / "requirements.txt"
        req_path.write_text(
            "# keep comments\n"
            "mcp>=1.24,<2\n"
            "requests>=2\n"
            "fastmcp\n",
            encoding="utf-8",
        )
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        filtered_files = builder._project_requirement_files()

        assert len(filtered_files) == 1
        filtered_text = open(filtered_files[0], "r", encoding="utf-8").read()
        assert "requests>=2" in filtered_text
        assert "mcp" not in filtered_text.lower()
        assert "fastmcp" not in filtered_text.lower()
        assert "mcp>=1.24,<2" in req_path.read_text(encoding="utf-8")

    def test_filter_shipped_requirements_removes_mcp_and_disabled_jit(self, tmp_path):
        data_dir = tmp_path / "build_output" / "Data"
        settings_dir = data_dir / "ProjectSettings"
        settings_dir.mkdir(parents=True)
        req_path = settings_dir / "requirements.txt"
        req_path.write_text(
            "numba>=0.61.0\n"
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
        assert "mcp" not in filtered_text.lower()
        assert "fastmcp" not in filtered_text.lower()

    def test_nuitka_builder_treats_mcp_as_game_build_excluded(self):
        assert NuitkaBuilder._is_game_build_excluded_package("mcp")
        assert NuitkaBuilder._is_game_build_excluded_package("fastmcp.server")
        assert not NuitkaBuilder._is_game_build_excluded_package("requests")


    def test_nuitka_player_excludes_editor_graph_but_keeps_runtime_viewport_utility(self):
        editor_modules = NuitkaBuilder._PLAYER_EDITOR_ONLY_MODULES

        assert "Infernux.engine.ui.editor_panel" in editor_modules
        assert "Infernux.engine.ui.asset_resource_preview" in editor_modules
        assert "Infernux.engine.ui.window_manager" in editor_modules
        assert "Infernux.engine.i18n" in editor_modules
        assert "Infernux.engine.ui.viewport_utils" not in editor_modules
        assert "Infernux.mcp" in NuitkaBuilder._GAME_BUILD_NOFOLLOW_MODULES

    def test_collect_user_dependencies_adds_llvmlite_for_numba_import(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "stress.py", "import numba\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name):
            if name in {"numba", "llvmlite", "numpy"}:
                return object()
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        deps = builder._collect_user_dependencies()

        assert deps == ["llvmlite", "numba", "numpy"]


class TestGameBuilderAutoParallelExport:
    def test_compile_user_scripts_emits_auto_parallel_sidecar_pyc(self, tmp_path):
        output_dir = tmp_path / "build_output"
        assets_dir = output_dir / "Data" / "Assets"
        assets_dir.mkdir(parents=True)

        script_path = assets_dir / "stress.py"
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
        builder._compile_user_scripts(str(output_dir))

        assert not script_path.exists()
        assert (assets_dir / "stress.pyc").is_file()
        assert (assets_dir / "stress.autop.pyc").is_file()

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
        builder._compile_user_scripts(str(output_dir))

        assert not script_path.exists()
        assert (assets_dir / "plain.pyc").is_file()
        assert not (assets_dir / "plain.autop.pyc").exists()

    def test_collect_user_dependencies_detects_public_infernux_jit_api(self, tmp_path, monkeypatch):
        project_root = _make_project(tmp_path)
        _write_asset_script(project_root, "jit_user.py", "from Infernux.jit import njit\n")
        builder = GameBuilder(str(project_root), str(tmp_path / "build_output"), game_name="TestGame")

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name):
            if name in {"numba", "llvmlite", "numpy"}:
                return object()
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        deps = builder._collect_user_dependencies()

        assert deps == ["llvmlite", "numba", "numpy"]


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
