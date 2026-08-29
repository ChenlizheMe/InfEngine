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


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (
            """WindowManagerPolicy\n  KeyguardServiceDelegate\n    showing=true\n    occluded=false\n""",
            True,
        ),
        (
            """WindowManagerPolicy\n  KeyguardServiceDelegate\n    showing=false\n    occluded=false\n""",
            False,
        ),
        ("unrecognized policy output", True),
    ],
)
def test_keyguard_visibility_is_fail_closed(policy: str, expected: bool):
    module = _module()

    assert module.keyguard_is_showing(policy) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Physical size: 1440x3200", (1440, 3200)),
        ("Physical size: 1440x3200\nOverride size: 720x1600", (1440, 3200)),
        ("", None),
    ],
)
def test_physical_display_size(output: str, expected: tuple[int, int] | None):
    module = _module()

    assert module.physical_display_size(output) == expected


def test_smoke_parser_accepts_gameplay_ready_gate():
    module = _module()

    arguments = module._parser().parse_args(
        ["player.apk", "--expect-ready-log", "BALANCE 040 //"]
    )

    assert arguments.expect_ready_log == "BALANCE 040 //"
    assert arguments.serial is None
    assert arguments.max_surface_creations is None
    assert arguments.max_abandoned_buffers == 8


def test_hyperos_usb_install_approval_is_narrowly_detected():
    module = _module()
    hierarchy = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node package="com.miui.securitycenter" text="继续安装"
        resource-id="android:id/button2" clickable="true" enabled="true"
        bounds="[131,2825][698,3008]" />
</hierarchy>
"""

    assert module.oem_install_approval_target(hierarchy) == (414, 2916)


@pytest.mark.parametrize(
    "hierarchy",
    [
        "<hierarchy><node package='other' text='继续安装' resource-id='android:id/button2' clickable='true' enabled='true' bounds='[0,0][10,10]'/></hierarchy>",
        "<hierarchy><node package='com.miui.securitycenter' text='拒绝' resource-id='android:id/button2' clickable='true' enabled='true' bounds='[0,0][10,10]'/></hierarchy>",
        "<hierarchy><node package='com.miui.securitycenter' text='继续安装' resource-id='android:id/button1' clickable='true' enabled='true' bounds='[0,0][10,10]'/></hierarchy>",
        "not xml",
    ],
)
def test_oem_install_approval_rejects_unrelated_ui(hierarchy: str):
    module = _module()

    assert module.oem_install_approval_target(hierarchy) is None
