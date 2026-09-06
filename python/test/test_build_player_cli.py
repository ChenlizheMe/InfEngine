from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "build_player.py"


def _module():
    spec = importlib.util.spec_from_file_location("infernux_build_player_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_option_uses_json_values():
    module = _module()

    assert module._parse_option('android_artifact="apk"') == (
        "android_artifact",
        "apk",
    )
    assert module._parse_option("compress=true") == ("compress", True)
    assert module._parse_option("workers=2") == ("workers", 2)


@pytest.mark.parametrize("value", ["missing-separator", "=true", "value=nope"])
def test_build_option_rejects_invalid_syntax(value):
    with pytest.raises(argparse.ArgumentTypeError):
        _module()._parse_option(value)


def test_invalid_project_fails_closed_and_writes_atomic_evidence(tmp_path):
    module = _module()
    project = tmp_path / "not-a-project"
    output = tmp_path / "output"
    report = tmp_path / "evidence" / "build.json"

    status = module.main(
        [str(project), "web-wasm32", str(output), "--report", str(report)]
    )

    assert status == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["schema"] == "infernux.build_evidence"
    assert payload["status"] == "invalid-project"
    assert payload["target"] == "web-wasm32"
    assert payload["diagnostics"][0]["code"] == "build.project.invalid"
    assert not tuple(report.parent.glob(".*.tmp"))


def test_exporter_mapping_covers_plugin_targets():
    module = _module()

    assert set(module.EXPORTERS) == {
        "android-arm64",
        "android-x64-emulator",
        "web-wasm32",
        module.DESKTOP_TARGET,
    }
    assert all(path.is_dir() for path in module.PLUGIN_EDITORS.values())


def test_supported_targets_include_the_current_desktop_host():
    module = _module()

    assert module.DESKTOP_TARGET in {"windows-x64", "linux-x64"}
    assert module.DESKTOP_TARGET in module.SUPPORTED_TARGETS
    assert set(module.SUPPORTED_TARGETS) == set(module.EXPORTERS) | {
        module.DESKTOP_TARGET
    }


def test_desktop_target_loads_the_core_exporter():
    module = _module()
    python_root = str(ROOT / "python")
    if python_root not in module.sys.path:
        module.sys.path.insert(0, python_root)

    exporter = module._load_exporter(module.DESKTOP_TARGET)

    assert exporter.exporter_id == (
        "infernux/platform-windows"
        if module.DESKTOP_TARGET == "windows-x64"
        else "infernux/platform-linux"
    )
    assert [target.id for target in exporter.targets()] == [module.DESKTOP_TARGET]


def test_installed_acceptance_rejects_the_source_checkout():
    module = _module()
    with pytest.raises(RuntimeError, match="installed wheel"):
        module._prepare_engine(installed=True)


def test_installed_acceptance_is_an_explicit_cli_mode():
    arguments = _module()._parser().parse_args(["project", "web-wasm32", "output", "--installed"])
    assert arguments.installed is True


def test_installed_preload_has_project_paths_and_mirrored_resources(tmp_path, monkeypatch):
    from Infernux.application import Application
    from Infernux.engine import library_sync, project_context
    from Infernux.engine.build import exporter_registry
    from Infernux.plugins import PluginManager

    asset = tmp_path / "Packages/probe/runtime/message.txt"
    asset.parent.mkdir(parents=True)
    asset.write_text("package preload", encoding="utf-8")
    calls = []
    monkeypatch.setattr(project_context, "_project_root", None)
    monkeypatch.setattr(project_context, "_runtime_asset_resolver", None)
    monkeypatch.setattr(library_sync, "sync_resources", lambda root: calls.append(root))

    def startup(root, *, runtime):
        assert runtime is False
        assert calls == [root]
        assert Path(Application.asset_path("Packages/probe/runtime/message.txt")) == asset

    monkeypatch.setattr(PluginManager, "startup", startup)
    assert _module()._installed_exporter_registry(tmp_path) is exporter_registry


def test_raw_build_tool_output_is_not_retained_as_phase_progress():
    module = _module()

    assert module._is_verbose_progress(
        SimpleNamespace(detail={"source": "cmake"})
    )
    assert module._is_verbose_progress(
        SimpleNamespace(detail={"source": "gradle"})
    )
    assert not module._is_verbose_progress(SimpleNamespace(detail={}))
