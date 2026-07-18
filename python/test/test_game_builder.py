from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest

from Infernux.engine.build_cancellation import BuildCancelled
from Infernux.engine.game_builder import BuildOutputDirectoryError, GameBuilder
from Infernux.engine import nuitka_builder as nuitka_builder_module
from Infernux.engine.nuitka_builder import NuitkaBuilder


def _make_project(tmp_path):
    project_root = tmp_path / "project"
    settings_dir = project_root / "ProjectSettings"
    settings_dir.mkdir(parents=True)
    scene_path = project_root / "main.scene"
    scene_path.write_text("scene", encoding="utf-8")
    (settings_dir / "BuildSettings.json").write_text(
        json.dumps({"scenes": [str(scene_path)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return project_root


def _make_builder(tmp_path, output_dir):
    project_root = _make_project(tmp_path)
    return GameBuilder(str(project_root), str(output_dir), game_name="TestGame")


def _write_asset_script(project_root, relative_path: str, source: str) -> None:
    script_path = project_root / "Assets" / relative_path
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(source, encoding="utf-8")


def test_build_cancellation_is_not_reported_as_a_build_failure(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    error_messages: list[str] = []
    monkeypatch.setattr(builder, "_build_inner", lambda *_args, **_kwargs: (_ for _ in ()).throw(BuildCancelled()))
    monkeypatch.setattr("Infernux.engine.game_builder.Debug.log_error", error_messages.append)

    with pytest.raises(BuildCancelled):
        builder.build()

    build_log = (tmp_path / "project" / "Logs" / "build.log").read_text(encoding="utf-8")
    assert "Build cancelled by user." in build_log
    assert "BUILD FAILED" not in build_log
    assert error_messages == []


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


def test_player_always_raw_copies_numpy_when_jit_is_disabled(tmp_path, monkeypatch):
    captured: dict = {}

    class _FakeNuitkaBuilder:
        _JIT_NOFOLLOW_PACKAGES = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def build(self, **_kwargs):
            return str(tmp_path / "dist")

    monkeypatch.setattr("Infernux.engine.game_builder.NuitkaBuilder", _FakeNuitkaBuilder)
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
    assert captured["output_filename"] == ("InfernuxPlayer.exe" if sys.platform == "win32" else "InfernuxPlayer")
    assert captured["product_name"] == "Infernux Player"
    assert captured["icon_path"]
    assert Path(captured["icon_path"]).name == "icon.png"
    assert Path(captured["icon_path"]).is_file()


def test_jit_build_installs_optional_parallel_runtime_module(tmp_path, monkeypatch):
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

    monkeypatch.setattr("Infernux.engine.game_builder.NuitkaBuilder", _FakeNuitkaBuilder)
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    builder.enable_jit = True

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
    }


def test_debug_player_uses_generic_reusable_runtime_pack(tmp_path, monkeypatch):
    captured: dict = {}

    class _FakeNuitkaBuilder:
        _JIT_NOFOLLOW_PACKAGES = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def build(self, **_kwargs):
            return str(tmp_path / "dist")

    monkeypatch.setattr("Infernux.engine.game_builder.NuitkaBuilder", _FakeNuitkaBuilder)
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    builder.debug_mode = True

    builder._run_nuitka(str(tmp_path / "boot.py"), on_progress=None, user_packages=[])

    assert captured["runtime_pack_cache"] is True
    assert captured["output_filename"] == ("InfernuxPlayer.exe" if sys.platform == "win32" else "InfernuxPlayer")
    assert captured["product_name"] == "Infernux Player"
    assert captured["icon_path"]
    assert Path(captured["icon_path"]).name == "icon.png"
    assert Path(captured["icon_path"]).is_file()


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
    restored = builder._restore_runtime_pack("a" * 64)

    assert restored == os.path.join(builder._staging_dir, "boot.dist")
    assert (tmp_path / "staging" / "boot.dist" / "InfernuxPlayer.exe").read_bytes() == b"runtime"
    marker = json.loads((tmp_path / "staging" / "boot.dist" / "_infernux_runtime_pack.json").read_text())
    assert marker["fingerprint"] == "a" * 64
    cache_manifest = json.loads(
        (cache_root / ("a" * 64) / "runtime-pack.json").read_text(encoding="utf-8")
    )
    assert cache_manifest["schema_version"] == 4
    assert cache_manifest["compression"] == "zip-deflate-9"
    assert cache_manifest["lto"] is True
    assert cache_manifest["file_count"] == 2
    assert cache_manifest["archive_bytes"] > 0
    with zipfile.ZipFile(cache_root / ("a" * 64) / "runtime-pack.zip") as archive:
        assert "bindings.pyi.bak" not in archive.namelist()
        assert "InfernuxPlayer.pdb" not in archive.namelist()


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
    archive_path = pack_root / "runtime-pack.zip"
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("../escape.txt", b"escape")
    manifest_path = pack_root / "runtime-pack.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archive_bytes"] = archive_path.stat().st_size
    manifest["archive_sha256"] = builder._hash_file(archive_path)
    manifest["file_count"] += 1
    manifest["uncompressed_bytes"] += len(b"escape")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert builder._restore_runtime_pack(fingerprint) is None
    assert not (tmp_path / "escape.txt").exists()


def test_packaged_parallel_runtime_module_round_trip(tmp_path, monkeypatch):
    module_root = tmp_path / "wheel" / "_runtime_modules"
    builder = object.__new__(NuitkaBuilder)
    builder.last_runtime_compatibility_key = "b" * 64
    builder._engine_fingerprint_cache = "engine-content"

    def fake_inject(dist_dir, packages=None):
        for package in packages or []:
            package_dir = Path(dist_dir) / package
            package_dir.mkdir(parents=True, exist_ok=True)
            (package_dir / "runtime.py").write_text(package, encoding="utf-8")
            (package_dir / "runtime.pyi").write_text(package, encoding="utf-8")

    monkeypatch.setattr(builder, "_inject_jit_packages", fake_inject)
    exported = builder.export_runtime_module(str(module_root))
    monkeypatch.setenv("INFERNUX_PREBUILT_RUNTIME_MODULE_DIR", str(module_root))

    dist = tmp_path / "dist"
    dist.mkdir()
    assert builder.install_runtime_module(str(dist)) is True
    assert (dist / "numba" / "runtime.py").read_text(encoding="utf-8") == "numba"
    assert (dist / "llvmlite" / "runtime.py").read_text(encoding="utf-8") == "llvmlite"
    manifest = json.loads(
        (Path(exported) / "parallel-module.json").read_text(encoding="utf-8")
    )
    assert manifest["compression"] == "zip-deflate-9"
    assert manifest["packages"] == ["llvmlite", "numba"]
    with zipfile.ZipFile(Path(exported) / "parallel-module.zip") as archive:
        assert all(not name.endswith(".pyi") for name in archive.namelist())

    compressed_dist = tmp_path / "compressed-dist"
    compressed_dist.mkdir()
    assert builder.install_runtime_module(
        str(compressed_dist), archive_only=True
    ) is True
    staged = compressed_dist / "RuntimeModules" / "parallel"
    assert (staged / "parallel-module.zip").read_bytes() == (
        Path(exported) / "parallel-module.zip"
    ).read_bytes()
    assert not (compressed_dist / "numba").exists()
    assert not (compressed_dist / "llvmlite").exists()


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


def test_cleanup_temp_removes_boot_directory_synchronously(tmp_path):
    boot_dir = tmp_path / "_build_temp"
    boot_dir.mkdir()
    boot_script = boot_dir / "boot.py"
    boot_script.write_text("print('temporary')", encoding="utf-8")

    GameBuilder._cleanup_temp(str(boot_script))

    assert not boot_dir.exists()


def test_release_output_renames_generic_prebuilt_player(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    monkeypatch.setattr(builder, "_player_launcher_path", lambda: "")
    builder.debug_mode = False
    dist = tmp_path / "staging" / "generic.dist"
    dist.mkdir(parents=True)
    runtime_name = "InfernuxPlayer.exe" if sys.platform == "win32" else "InfernuxPlayer"
    (dist / runtime_name).write_bytes(b"player")

    final_dir = Path(builder._organize_output(str(dist)))

    game_name = "TestGame.exe" if sys.platform == "win32" else "TestGame"
    assert (final_dir / game_name).read_bytes() == b"player"
    assert not (final_dir / runtime_name).exists()


def test_windows_launcher_layout_hides_runtime_payload(tmp_path, monkeypatch):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    launcher = tmp_path / "InfernuxLauncher.exe"
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(builder, "_player_launcher_path", lambda: str(launcher))

    dist = tmp_path / "staging" / "generic.dist"
    dist.mkdir(parents=True)
    (dist / "InfernuxPlayer.exe").write_bytes(b"runtime")
    (dist / "python312.dll").write_bytes(b"python")
    final_dir = Path(builder._organize_output(str(dist)))
    (final_dir / "Data").mkdir()
    (final_dir / "Data" / "BuildManifest.json").write_text("{}", encoding="utf-8")
    (final_dir / "RuntimeModules" / "core").mkdir(parents=True)
    (final_dir / "RuntimeModules" / "core" / "core-module.zip").write_bytes(b"core")

    builder._organize_player_layout(str(final_dir))
    builder._write_output_marker(str(final_dir))

    data_root = final_dir / "TestGame_Data"
    assert (final_dir / "TestGame.exe").read_bytes() == b"launcher"
    assert sorted(path.name for path in final_dir.iterdir()) == ["TestGame.exe", "TestGame_Data"]
    assert (data_root / GameBuilder.OUTPUT_MARKER_FILENAME).is_file()
    assert (data_root / "Runtime" / "InfernuxPlayer.exe").read_bytes() == b"runtime"
    assert (data_root / "Runtime" / "python312.dll").read_bytes() == b"python"
    assert (data_root / "RuntimeModules" / "core" / "core-module.zip").read_bytes() == b"core"
    layout = json.loads((data_root / "PlayerLayout.json").read_text(encoding="utf-8"))
    assert layout["layout"] == "infernux-windows-player-v3"


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

    builder._pack_content_archive(str(final_dir))
    with zipfile.ZipFile(final_dir / "Data" / builder._CONTENT_ARCHIVE_FILENAME) as archive:
        assert "Library/Artifacts/RenderEffect/bloom.inxeffect" in archive.namelist()


def test_payload_manifest_allows_project_asset_metadata(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    metadata = final_dir / "Data" / "Assets" / "Materials" / "wave.mat.meta"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("metadata", encoding="utf-8")

    builder._write_payload_manifest(str(final_dir))

    payload = json.loads(
        (final_dir / "Data" / "BuildPayload.json").read_text(encoding="utf-8")
    )
    assert payload["code_protection"]["project_metadata_count"] == 1


def test_content_archive_replaces_loose_project_files(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    data = final_dir / "Data"
    assets = data / "Assets"
    settings = data / "ProjectSettings"
    assets.mkdir(parents=True)
    settings.mkdir(parents=True)
    (assets / "Main.scene").write_text('{"objects": []}', encoding="utf-8")
    (assets / "Player.pyc").write_bytes(b"bytecode")
    (assets / "Player.py.meta").write_text("metadata", encoding="utf-8")
    build_manifest = data / "BuildManifest.json"
    build_manifest.write_text('{"game_name": "TestGame"}', encoding="utf-8")
    (settings / "BuildSettings.json").write_text('{"scenes": ["Assets/Main.scene"]}', encoding="utf-8")
    (settings / "mcp_capabilities.json").write_text('{"enabled": true}', encoding="utf-8")
    (settings / "agent_tools.json").write_text('{"tools": []}', encoding="utf-8")

    builder._pack_content_archive(str(final_dir))

    archive_path = data / "Content.inxpkg"
    manifest = json.loads((data / "Content.json").read_text(encoding="utf-8"))
    assert archive_path.is_file()
    assert build_manifest.is_file()
    assert not assets.exists()
    assert not settings.exists()
    assert manifest["compression"] == "zip-deflate-6"
    assert manifest["project_bytecode_count"] == 1
    assert manifest["project_metadata_count"] == 1
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "Assets/Main.scene",
            "Assets/Player.py.meta",
            "Assets/Player.pyc",
            "ProjectSettings/BuildSettings.json",
        }


def test_content_archive_rejects_plaintext_project_scripts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    script = tmp_path / "dist" / "Data" / "Assets" / "Player.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('source')", encoding="utf-8")

    with pytest.raises(RuntimeError, match="plaintext project scripts"):
        builder._pack_content_archive(str(tmp_path / "dist"))


def test_core_runtime_archive_replaces_loose_numpy_and_resources(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    numpy_file = final_dir / "numpy" / "core.py"
    numpy_dll = final_dir / "numpy.libs" / "openblas.dll"
    font = final_dir / "Infernux" / "resources" / "fonts" / "engine.otf"
    numpy_file.parent.mkdir(parents=True)
    numpy_dll.parent.mkdir(parents=True)
    font.parent.mkdir(parents=True)
    numpy_file.write_text("VALUE = 1", encoding="utf-8")
    numpy_dll.write_bytes(b"dll")
    font.write_bytes(b"font")

    builder._pack_core_runtime_archive(str(final_dir))

    module = final_dir / "RuntimeModules" / "core"
    manifest = json.loads((module / "core-module.json").read_text(encoding="utf-8"))
    assert manifest["compression"] == "zip-deflate-6"
    assert not (final_dir / "numpy").exists()
    assert not (final_dir / "numpy.libs").exists()
    assert not (final_dir / "Infernux" / "resources").exists()
    with zipfile.ZipFile(module / "core-module.zip") as archive:
        assert set(archive.namelist()) == {
            "Infernux/resources/fonts/engine.otf",
            "numpy.libs/openblas.dll",
            "numpy/core.py",
        }


def test_payload_manifest_rejects_plaintext_project_scripts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    assets = final_dir / "Data" / "Assets"
    assets.mkdir(parents=True)
    (assets / "Player.py").write_text("print('source')", encoding="utf-8")

    with pytest.raises(RuntimeError, match="plaintext project scripts"):
        builder._write_payload_manifest(str(final_dir))


def test_payload_manifest_rejects_plaintext_runtime_sources(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    runtime_source = final_dir / "_build_temp" / "boot.py"
    runtime_source.parent.mkdir(parents=True)
    runtime_source.write_text("print('source')", encoding="utf-8")

    with pytest.raises(RuntimeError, match="plaintext runtime sources"):
        builder._write_payload_manifest(str(final_dir))


def test_payload_manifest_rejects_build_time_artifacts(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    backup = final_dir / "Infernux" / "lib" / "bindings.pyi.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")

    with pytest.raises(RuntimeError, match="build-time artifacts"):
        builder._write_payload_manifest(str(final_dir))


def test_payload_manifest_reports_bytecode_and_native_payload(tmp_path):
    builder = _make_builder(tmp_path, tmp_path / "build_output")
    final_dir = tmp_path / "dist"
    assets = final_dir / "Data" / "Assets"
    assets.mkdir(parents=True)
    (assets / "Player.pyc").write_bytes(b"bytecode")
    (final_dir / "InfernuxRuntime.dll").write_bytes(b"native")

    builder._write_payload_manifest(str(final_dir))

    payload = json.loads(
        (final_dir / "Data" / "BuildPayload.json").read_text(encoding="utf-8")
    )
    assert payload["code_protection"]["plaintext_project_script_count"] == 0
    assert payload["code_protection"]["project_bytecode_count"] == 1
    assert payload["code_protection"]["strong_encryption"] is False
    assert payload["files"]["native_binary_count"] == 1


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
        boot_source = open(boot_path, "r", encoding="utf-8").read()
        compile(boot_source, boot_path, "exec")
        assert 'os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = "1" if _DEBUG_MODE else "0"' in boot_source
        assert 'os.environ["PYTHONDONTWRITEBYTECODE"] = "1"' in boot_source
        assert "sys.dont_write_bytecode = True" in boot_source
        assert 'os.environ["_INFERNUX_PACKAGED_RESOURCE_ROOT"]' in boot_source
        assert '_CORE_ROOT = os.path.join(_MODULE_ROOT, "core")' in boot_source
        assert '_PARALLEL_ROOT = os.path.join(_MODULE_ROOT, "parallel")' in boot_source
        assert '"_INFERNUX_PLAYER_DATA_ROOT"' in boot_source
        assert "_extract_cached_archive" in boot_source
        assert '_BUILD_MANIFEST.get("game_name", "InfernuxPlayer")' in boot_source
        assert 'if os.environ.get("_INFERNUX_PLAYER_CONTROL_FILE"):' in boot_source

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

    def test_validate_allows_only_build_temp_output_dir(self, tmp_path):
        output_dir = tmp_path / "build_output"
        temp_dir = output_dir / "_build_temp"
        nested_dir = temp_dir / "nested"
        nested_dir.mkdir(parents=True)
        (nested_dir / "stale.bin").write_bytes(b"stale")
        builder = _make_builder(tmp_path, output_dir)

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
        assert payload["project_name"] == "TestGame"
        assert "project_path" not in payload


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
