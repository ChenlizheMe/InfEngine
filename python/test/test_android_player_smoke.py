from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "android_player_smoke.py"


def _module():
    spec = importlib.util.spec_from_file_location("android_player_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_device_parser_and_physical_preference():
    module = _module()
    devices = module.parse_devices(
        """List of devices attached
emulator-5554 device product:sdk model:Emulator transport_id:1
72D3FF11 device usb:1-2 product:mi model:REDMI_K80_Pro transport_id:2
"""
    )

    selected = module.select_device(devices, None)

    assert selected.serial == "72D3FF11"
    assert selected.attributes["model"] == "REDMI_K80_Pro"
    assert not selected.emulator


def test_unauthorized_requested_device_has_actionable_error():
    module = _module()
    devices = module.parse_devices("phone unauthorized usb:1-2\n")

    with pytest.raises(RuntimeError, match="unlock and authorize"):
        module.select_device(devices, "phone")


def test_multiple_emulators_require_explicit_serial():
    module = _module()
    devices = module.parse_devices(
        "emulator-5554 device\nemulator-5556 device\n"
    )

    with pytest.raises(RuntimeError, match="--serial"):
        module.select_device(devices, None)


def test_apk_abi_inventory(tmp_path: Path):
    module = _module()
    apk = tmp_path / "player.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("lib/arm64-v8a/libmain.so", b"arm")
        archive.writestr("lib/x86_64/libmain.so", b"x64")
        archive.writestr("assets/player.inxpack", b"content")

    assert module.apk_abis(apk) == frozenset({"arm64-v8a", "x86_64"})
