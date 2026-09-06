from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

import pytest

from Infernux.engine.precompiled_player import inspect_desktop_runtime, stage_desktop_runtime
from Infernux.engine.player_package_native import write_pack
from Infernux.version import ENGINE_VERSION


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "plugin/editor/player"
    root.mkdir(parents=True)
    manifest = {
        "engine_version": ENGINE_VERSION,
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "platform": sys.platform, "machine": platform.machine().casefold(),
        "archive": "Runtime.inxrt", "distribution": "platform-plugin",
    }
    (root / "Player.inxmanifest").write_text(json.dumps(manifest), encoding="utf-8")
    host = "InfernuxPlayerHost.exe" if sys.platform == "win32" else "InfernuxPlayerHost"
    (root / host).write_bytes(b"test host")
    source = tmp_path / "dependency.pyc"
    source.write_bytes(b"test payload")
    write_pack([("Infernux/dependency.pyc", str(source))], root / "Runtime.inxrt")
    write_pack([("numba/dependency.pyc", str(source))], root / "Parallel.inxmod")
    return root


def test_consumer_extracts_plugin_archives_without_compiler_or_fingerprint(tmp_path, payload, monkeypatch):
    from Infernux.engine.nuitka_builder import NuitkaBuilder
    for method in ("build", "_build_command", "_runtime_pack_fingerprint", "_runtime_pack_compatibility_key"):
        monkeypatch.setattr(NuitkaBuilder, method, lambda *args: pytest.fail("Consumer entered compiler/cache path"))
    staged = Path(stage_desktop_runtime(str(payload), str(tmp_path / "project/Cache/Build/Desktop"), parallel=True))
    assert (staged / "Infernux/dependency.pyc").read_bytes() == b"test payload"
    assert (staged / "Parallel.inxmod").read_bytes() == (payload / "Parallel.inxmod").read_bytes()


@pytest.mark.parametrize("field", ["engine_version", "python_abi", "platform", "machine"])
def test_incompatible_plugin_payload_is_rejected_before_staging(tmp_path, payload, field):
    path = payload / "Player.inxmanifest"
    manifest = json.loads(path.read_bytes())
    manifest[field] = "incompatible"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    staging = tmp_path / "staging"
    with pytest.raises(RuntimeError, match="does not match"):
        stage_desktop_runtime(str(payload), str(staging))
    assert not staging.exists()


def test_missing_runtime_never_uses_engine_wheel_or_compiles(tmp_path):
    with pytest.raises(RuntimeError, match="does not compile"):
        stage_desktop_runtime(str(tmp_path / "missing"), str(tmp_path / "staging"))
    assert not (tmp_path / "staging").exists()


def test_missing_parallel_module_is_not_rebuilt(tmp_path, payload):
    (payload / "Parallel.inxmod").unlink()
    with pytest.raises(FileNotFoundError, match="Parallel.inxmod"):
        stage_desktop_runtime(str(payload), str(tmp_path / "staging"), parallel=True)
    assert not (tmp_path / "staging").exists()


def test_failed_archive_extraction_removes_only_owned_staging(tmp_path, payload, monkeypatch):
    from Infernux.engine import precompiled_player
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "unrelated.txt").write_text("keep", encoding="utf-8")

    def fail(*args, **kwargs):
        raise RuntimeError("invalid archive")

    monkeypatch.setattr(precompiled_player, "extract_pack", fail)
    with pytest.raises(RuntimeError, match="invalid archive"):
        stage_desktop_runtime(str(payload), str(staging))
    assert [item.name for item in staging.iterdir()] == ["unrelated.txt"]


def test_release_engineering_exports_existing_formats_into_plugin_payload(tmp_path, payload):
    from Infernux.engine.prebuilt_runtime import export_platform_player
    target = tmp_path / "published"
    export_platform_player({"path": str(payload), "parallel_module_path": str(payload)}, str(target))
    assert inspect_desktop_runtime(str(target))["distribution"] == "platform-plugin"
    assert (target / "Runtime.inxrt").read_bytes() == (payload / "Runtime.inxrt").read_bytes()
    assert (target / "Parallel.inxmod").read_bytes() == (payload / "Parallel.inxmod").read_bytes()
