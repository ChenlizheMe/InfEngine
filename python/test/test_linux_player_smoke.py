from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "linux_player_smoke.py"


def _module():
    spec = importlib.util.spec_from_file_location("linux_player_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_linux_smoke_requires_debug_player_control(tmp_path: Path):
    module = _module()
    player = tmp_path / "Balance"
    player.write_bytes(b"")
    data = tmp_path / "Balance_Data"
    data.mkdir()
    (data / "BuildManifest.json").write_text(
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

    with pytest.raises(RuntimeError, match="development build"):
        module._load_debug_manifest(player)


def test_linux_smoke_position_and_axis_delta():
    module = _module()
    data = {"objects": {"PlayerBall": {"position": [1, 2.5, -3]}}}

    initial = module._position(data, "PlayerBall")

    assert initial == (1.0, 2.5, -3.0)
    assert module._axis_delta(initial, (1.0, 2.5, 4.0), "z") == 7.0


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
    assert not arguments.validation


def test_linux_smoke_fatal_scan_includes_vulkan_validation():
    module = _module()

    lines = module._fatal_lines(
        "normal line\nVulkan Validation Error: VUID-RuntimeSpirv-test\n"
    )

    assert lines == ["Vulkan Validation Error: VUID-RuntimeSpirv-test"]
