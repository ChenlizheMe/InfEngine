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
        [
            "player.apk",
            "--expect-ready-log",
            "BALANCE 040 //",
            "--touch",
            "--expect-landscape",
            "--require-log",
            "INFERNUX_ACCEPTANCE_LINE_READY",
            "--require-log",
            "INFERNUX_ACCEPTANCE_PARTICLE_READY",
        ]
    )

    assert arguments.expect_ready_log == "BALANCE 040 //"
    assert arguments.serial is None
    assert arguments.max_surface_creations is None
    assert arguments.max_abandoned_buffers == 8
    assert arguments.touch_attempts == 3
    assert arguments.require_log == [
        "INFERNUX_ACCEPTANCE_LINE_READY",
        "INFERNUX_ACCEPTANCE_PARTICLE_READY",
    ]
    assert arguments.touch
    assert arguments.expect_landscape
    assert not arguments.keep_running


def test_required_runtime_logs_wait_for_every_marker(monkeypatch):
    module = _module()
    logs = iter(("LINE_READY", "LINE_READY\nPARTICLE_READY\nANIMATION_READY"))

    class FakeAdb:
        def run(self, *arguments, **options):
            assert options == {"check": False}
            if arguments == ("shell", "pidof", "com.infernux.bootstrap"):
                return "321"
            assert arguments == ("logcat", "-d", "-v", "brief")
            return next(logs)

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    log = module._wait_for_required_logs(
        FakeAdb(),
        "com.infernux.bootstrap",
        "321",
        ("LINE_READY", "PARTICLE_READY", "ANIMATION_READY"),
        5.0,
    )

    assert "ANIMATION_READY" in log


def test_vulkan_surface_extents_follow_creation_order():
    module = _module()
    log = """
I/SDL: INFERNUX_VULKAN_SURFACE requested=1220x2712 current=1220x2712 extent=1220x2712 currentTransform=1 preTransform=1 supportedTransforms=3
I/SDL: unrelated
I/SDL: INFERNUX_VULKAN_SURFACE requested=2712x1220 current=2712x1220 extent=2712x1220 currentTransform=2 preTransform=1 supportedTransforms=3
"""

    assert module.vulkan_surface_extents(log) == ((1220, 2712), (2712, 1220))


def test_touch_gesture_is_injected_as_distinct_frame_phases(monkeypatch):
    module = _module()
    commands = []
    delays = []

    class FakeAdb:
        def run(self, *arguments):
            commands.append(arguments)

    monkeypatch.setattr(module.time, "sleep", delays.append)

    module.inject_touch_gesture(FakeAdb(), 3200, 1440)

    assert commands == [
        (
            "shell",
            "input",
            "touchscreen",
            "motionevent",
            "DOWN",
            "1400",
            "864",
        ),
        (
            "shell",
            "input",
            "touchscreen",
            "motionevent",
            "MOVE",
            "1600",
            "720",
        ),
        (
            "shell",
            "input",
            "touchscreen",
            "motionevent",
            "UP",
            "1800",
            "576",
        ),
    ]
    assert delays == [0.25, 0.25]


def test_input_focus_requires_the_player_current_focus():
    module = _module()
    states = iter(
        (
            "mCurrentFocus=Window{123 u0 com.miui.home/.Launcher}",
            "mCurrentFocus=Window{456 u0 com.infernux.bootstrap/.InfernuxActivity}",
        )
    )

    class FakeAdb:
        def run(self, *arguments, **options):
            assert arguments == ("shell", "dumpsys", "window", "displays")
            assert options == {"check": False}
            return next(states)

    module._wait_for_input_focus(FakeAdb(), "com.infernux.bootstrap")


def test_smoke_parser_allows_manual_session_to_remain_running():
    module = _module()

    arguments = module._parser().parse_args(["player.apk", "--keep-running"])

    assert arguments.keep_running


def test_atomic_report_records_structured_payload(tmp_path: Path):
    module = _module()
    report = tmp_path / "evidence" / "android.json"

    module._write_report(
        report, {"schema": "infernux.android_player_smoke", "status": "passed"}
    )

    assert report.read_text(encoding="utf-8") == (
        '{\n  "schema": "infernux.android_player_smoke",\n  "status": "passed"\n}\n'
    )


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
