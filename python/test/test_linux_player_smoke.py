from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from Infernux.engine.player_package_native import read_manifest, write_pack


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "linux_player_smoke.py"
WINDOWS_SCRIPT = ROOT / "scripts" / "acceptance" / "windows_player_smoke.py"


def _module():
    spec = importlib.util.spec_from_file_location("linux_player_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", (SCRIPT, WINDOWS_SCRIPT))
def test_player_smoke_cli_bootstraps_repository_python_from_any_working_directory(
    script: Path, tmp_path: Path
):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_gameplay_control_ready_requires_started_runtime_and_unpaused_input():
    module = _module()

    assert not module._gameplay_control_ready({"gameplay_ready": False})
    assert module._gameplay_control_ready(
        {"gameplay_ready": True, "scene_manager_paused": False}
    )
    assert not module._gameplay_control_ready(
        {"gameplay_ready": True, "scene_manager_paused": True}
    )
    assert module._gameplay_control_ready(
        {"gameplay_ready": True, "scene_manager_paused": True},
        allow_paused=True,
    )


def test_linux_smoke_requires_debug_player_control(tmp_path: Path):
    module = _module()
    player = tmp_path / "Balance"
    player.write_bytes(b"")
    data = tmp_path / "Balance_Data"
    data.mkdir()
    build_manifest = tmp_path / "BuildManifest.json"
    build_manifest.write_text(
        json.dumps(
            {
                "game": "Balance",
                "runtime_contract": {
                    "runtime_policy": {"player_control": "disabled"}
                },
            }
        ),
        encoding="utf-8",
    )
    runtime_catalog = tmp_path / "RuntimeAssetCatalog.json"
    runtime_catalog.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_asset_catalog",
                "player_host": {},
                "packages": [],
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    catalog = data / "AssetCatalog.inxcat"
    write_pack(
        (
            ("RuntimeAssetCatalog.json", runtime_catalog),
            ("BuildManifest.json", build_manifest),
        ),
        catalog,
    )
    catalog_manifest = read_manifest(catalog)
    (data / "PackageIndex.inxmanifest").write_text(
        "INFERNUX_PLAYER_PACKAGE_INDEX\n"
        f"catalog\t{catalog_manifest['archive_sha256']}\t"
        f"{catalog_manifest['archive_bytes']}\n",
        encoding="ascii",
    )

    with pytest.raises(RuntimeError, match="development build"):
        module._load_debug_manifest(player)


def test_linux_smoke_position_and_axis_delta():
    module = _module()
    data = {"objects": {"PlayerBall": {"position": [1, 2.5, -3]}}}

    initial = module._position(data, "PlayerBall")

    assert initial == (1.0, 2.5, -3.0)
    assert module._axis_delta(initial, (1.0, 2.5, 4.0), "z") == 7.0


def test_player_smoke_rejects_a_body_that_falls_through_the_floor():
    module = _module()

    module._require_minimum_final_y((0.0, 0.5, 2.0), 0.0)
    with pytest.raises(RuntimeError, match="fell to y=-0.250000"):
        module._require_minimum_final_y((0.0, -0.25, 2.0), 0.0)


def test_linux_smoke_rejects_missing_public_position():
    module = _module()

    with pytest.raises(RuntimeError, match="no public position"):
        module._position({"objects": {}}, "PlayerBall")


def test_linux_smoke_report_is_atomic(tmp_path: Path):
    module = _module()
    report = tmp_path / "evidence" / "linux.json"

    module._write_json_atomic(
        report, {"schema": "infernux.linux_player_smoke", "status": "passed"}
    )

    assert json.loads(report.read_text(encoding="utf-8")) == {
        "schema": "infernux.linux_player_smoke",
        "status": "passed",
    }
    assert not list(report.parent.glob("*.tmp"))


def test_linux_smoke_parser_defaults_to_managed_cleanup():
    module = _module()

    arguments = module._parser().parse_args(["Balance"])

    assert arguments.xvfb == "auto"
    assert arguments.object == "PlayerBall"
    assert arguments.press_scancode == 26
    assert arguments.axis == "z"
    assert arguments.minimum_final_y is None
    assert not arguments.validation
    assert arguments.component_probe == []


def test_linux_smoke_component_probe_asserts_nested_public_state():
    module = _module()
    probe = module._parse_component_probe(
        json.dumps(
            {
                "object_name": "GoalParticles",
                "component_type": "ParticleSystem",
                "fields": ["is_playing", "time", "last_compile_error"],
                "assertions": [
                    {"field": "is_playing", "operator": "truthy"},
                    {"field": "time", "operator": "greater", "value": 0.0},
                    {
                        "field": "last_compile_error",
                        "operator": "equal",
                        "value": "",
                    },
                ],
            }
        )
    )
    observation = {
        "objects": {
            "GoalParticles": {
                "component_fields": {
                    "ParticleSystem[0]": {
                        "is_playing": True,
                        "time": 0.75,
                        "last_compile_error": "",
                    }
                }
            }
        }
    }

    results, fields = module._assert_component_probes(observation, [probe])

    assert len(results) == 3
    assert fields["GoalParticles/ParticleSystem[0]"]["time"] == 0.75


def test_linux_smoke_component_probe_rejects_failed_contract():
    module = _module()
    probe = module._parse_component_probe(
        json.dumps(
            {
                "object_name": "BallTrail",
                "component_type": "LineRenderer",
                "fields": ["position_count"],
                "assertions": [
                    {
                        "field": "position_count",
                        "operator": "greater_or_equal",
                        "value": 3,
                    }
                ],
            }
        )
    )
    observation = {
        "objects": {
            "BallTrail": {
                "component_fields": {
                    "LineRenderer[0]": {"position_count": 2}
                }
            }
        }
    }

    with pytest.raises(RuntimeError, match="component assertion failed"):
        module._assert_component_probes(observation, [probe])


def test_linux_smoke_fatal_scan_includes_vulkan_validation():
    module = _module()

    lines = module._fatal_lines(
        "normal line\nVulkan Validation Error: VUID-RuntimeSpirv-test\n"
    )

    assert lines == ["Vulkan Validation Error: VUID-RuntimeSpirv-test"]
