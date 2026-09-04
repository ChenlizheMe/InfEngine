"""Validate Android system multi-touch against one installed Infernux Player."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from android_player_smoke import (
    Adb,
    _FATAL_PATTERNS,
    install_apk,
    parse_devices,
    select_device,
    unlock_device,
)


_PASSED_RESULT = "INSTRUMENTATION_RESULT: INFERNUX_MULTITOUCH_INJECTION=passed"
_PASSED_IME_RESULT = "INSTRUMENTATION_RESULT: INFERNUX_IME_INJECTION=passed"
_PASSED_ORIENTATION_RESULT = (
    "INSTRUMENTATION_RESULT: INFERNUX_ORIENTATION_INJECTION=passed"
)
_PASSED_BACK_RESULT = "INSTRUMENTATION_RESULT: INFERNUX_BACK_INJECTION=passed"
_PASSED_CODE = "INSTRUMENTATION_CODE: -1"
_EXTENT_PATTERN = re.compile(r"INSTRUMENTATION_RESULT: (height|width)=(\d+)")
_IME_INSET_PATTERN = re.compile(r"INSTRUMENTATION_RESULT: imeInset=(\d+)")
_COMMITTED_TEXT_PATTERN = re.compile(r"INSTRUMENTATION_RESULT: committedText=(.+)")
_ROTATION_PATTERN = re.compile(
    r"INSTRUMENTATION_RESULT: "
    r"(landscapeRotation|reverseLandscapeRotation)=(\d+)"
)
_SAFE_INSETS_PATTERN = re.compile(
    r"INSTRUMENTATION_RESULT: "
    r"(landscapeSafeInsets|reverseLandscapeSafeInsets)="
    r"(\d+),(\d+),(\d+),(\d+)"
)
_SCREEN_STATE_PATTERN = re.compile(
    r"INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE\s+"
    r"revision=(\d+)\s+size=(\d+)x(\d+)\s+"
    r"framebuffer=(\d+)x(\d+)\s+"
    r"safe=(\d+),(\d+),(\d+),(\d+)\s+"
    r"insets=(\d+),(\d+),(\d+),(\d+)\s+"
    r"pixel_ratio=([0-9.]+)"
)
_EXPECTED_TEXT = "输入测试中文🙂"
_DEFAULT_REQUIRED_LOGS = (
    "INFERNUX_PLATFORM_FIXTURE_SCREEN_STATE",
    "INFERNUX_PLATFORM_FIXTURE_PACKAGE_RESOURCE_READY "
    "value=Package resource reached UIText on every Player target.",
    "INFERNUX_PLATFORM_FIXTURE_PRELOAD_RESOURCE_READY "
    "value=Package resource reached UIText on every Player target.",
    "INFERNUX_PLATFORM_FIXTURE_MULTITOUCH_READY",
    "INFERNUX_PLATFORM_FIXTURE_UNITY_TOUCH_READY",
    "INFERNUX_PLATFORM_FIXTURE_TOUCH_CANCELED",
    "INFERNUX_PLATFORM_FIXTURE_UI_CLICK_READY",
    "INFERNUX_PLATFORM_FIXTURE_IME_VISIBLE",
    f"INFERNUX_PLATFORM_FIXTURE_TEXT_COMMITTED value={_EXPECTED_TEXT}",
    "INFERNUX_PLATFORM_FIXTURE_IME_HIDDEN",
    "INFERNUX_PLATFORM_FIXTURE_BACK_READY",
)


@dataclass(frozen=True, slots=True)
class MultiTouchResult:
    serial: str
    model: str
    android: str
    api: str
    abi: str
    instrumentation_apk: str
    target_package: str
    runner: str
    automated_install_approval: bool
    width: int
    height: int
    ime_inset: int
    committed_text: str
    landscape_rotation: int
    reverse_landscape_rotation: int
    landscape_safe_insets: tuple[int, int, int, int]
    reverse_landscape_safe_insets: tuple[int, int, int, int]
    screen_states: tuple[dict[str, object], ...]
    required_logs: tuple[str, ...]
    fatal_count: int
    elapsed_seconds: float


def instrumentation_extent(output: str) -> tuple[int, int]:
    values = {name: int(value) for name, value in _EXTENT_PATTERN.findall(output)}
    if set(values) != {"width", "height"}:
        raise RuntimeError("Android instrumentation did not report its target extent")
    if values["width"] <= 0 or values["height"] <= 0:
        raise RuntimeError("Android instrumentation reported an invalid target extent")
    return values["width"], values["height"]


def screen_state_samples(log: str) -> tuple[dict[str, object], ...]:
    samples: list[dict[str, object]] = []
    identities: set[tuple[object, ...]] = set()
    for match in _SCREEN_STATE_PATTERN.finditer(log):
        values = match.groups()
        sample = {
            "revision": int(values[0]),
            "size": (int(values[1]), int(values[2])),
            "framebuffer_size": (int(values[3]), int(values[4])),
            "safe_area": tuple(int(value) for value in values[5:9]),
            "safe_insets": tuple(int(value) for value in values[9:13]),
            "pixel_ratio": float(values[13]),
        }
        width, height = sample["size"]
        left, top, right, bottom = sample["safe_insets"]
        expected_safe_area = (
            left,
            top,
            width - left - right,
            height - top - bottom,
        )
        if sample["safe_area"] != expected_safe_area:
            raise RuntimeError(
                "Python Screen safe_area and safe_insets disagree: "
                + repr(sample)
            )
        identity = (
            sample["size"],
            sample["framebuffer_size"],
            sample["safe_area"],
            sample["safe_insets"],
            sample["pixel_ratio"],
        )
        if identity not in identities:
            identities.add(identity)
            samples.append(sample)
    return tuple(samples)


def validate_instrumentation_output(
    output: str,
) -> tuple[
    int,
    int,
    int,
    str,
    int,
    int,
    tuple[int, int, int, int],
    tuple[int, int, int, int],
]:
    if (
        _PASSED_RESULT not in output
        or _PASSED_IME_RESULT not in output
        or _PASSED_ORIENTATION_RESULT not in output
        or _PASSED_BACK_RESULT not in output
        or _PASSED_CODE not in output
    ):
        raise RuntimeError("Android system input instrumentation failed:\n" + output)
    width, height = instrumentation_extent(output)
    inset_match = _IME_INSET_PATTERN.search(output)
    text_match = _COMMITTED_TEXT_PATTERN.search(output)
    if inset_match is None or int(inset_match.group(1)) <= 0:
        raise RuntimeError("Android instrumentation did not report a visible IME inset")
    if text_match is None or text_match.group(1).strip() != _EXPECTED_TEXT:
        raise RuntimeError("Android instrumentation did not commit the expected Unicode text")
    rotations = {
        name: int(value) for name, value in _ROTATION_PATTERN.findall(output)
    }
    if rotations != {"landscapeRotation": 1, "reverseLandscapeRotation": 3}:
        raise RuntimeError(
            "Android instrumentation did not traverse both landscape rotations"
        )
    safe_insets = {
        name: tuple(int(value) for value in values)
        for name, *values in _SAFE_INSETS_PATTERN.findall(output)
    }
    expected_safe_keys = {
        "landscapeSafeInsets",
        "reverseLandscapeSafeInsets",
    }
    if set(safe_insets) != expected_safe_keys:
        raise RuntimeError(
            "Android instrumentation did not report both safe-area snapshots"
        )
    for values in safe_insets.values():
        left, top, right, bottom = values
        if min(values) < 0 or left + right >= width or top + bottom >= height:
            raise RuntimeError("Android instrumentation reported invalid safe insets")
    return (
        width,
        height,
        int(inset_match.group(1)),
        text_match.group(1).strip(),
        rotations["landscapeRotation"],
        rotations["reverseLandscapeRotation"],
        safe_insets["landscapeSafeInsets"],
        safe_insets["reverseLandscapeSafeInsets"],
    )


def _write_report(destination: Path | None, payload: dict[str, object]) -> None:
    if destination is None:
        return
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def run_smoke(arguments: argparse.Namespace) -> MultiTouchResult:
    started = time.perf_counter()
    controller = Adb(arguments.adb)
    device = select_device(
        parse_devices(controller.run("devices", "-l")), arguments.serial
    )
    adb = Adb(arguments.adb, device.serial)
    if not adb.run("shell", "pm", "path", arguments.target_package, check=False).strip():
        raise RuntimeError(
            f"Android target package {arguments.target_package!r} is not installed"
        )

    unlock_device(adb)
    adb.run("uninstall", arguments.instrumentation_package, check=False)
    installed = False
    try:
        automated_install_approval = install_apk(
            adb,
            arguments.instrumentation_apk,
            replace=False,
            approve_oem_prompt=not device.emulator,
        )
        installed = True
        adb.run("logcat", "-c")
        adb.run("shell", "am", "force-stop", arguments.target_package)
        output = adb.run(
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
            "-e",
            "waitMilliseconds",
            str(arguments.wait_milliseconds),
            arguments.runner,
            timeout=arguments.wait_milliseconds / 1000.0 + 90.0,
        )
        (
            width,
            height,
            ime_inset,
            committed_text,
            landscape_rotation,
            reverse_landscape_rotation,
            landscape_safe_insets,
            reverse_landscape_safe_insets,
        ) = validate_instrumentation_output(output)
        time.sleep(0.5)
        log = adb.run("logcat", "-d", "-v", "brief", check=False)
        screen_states = screen_state_samples(log)
        if not screen_states:
            raise RuntimeError("Android Player did not publish Python Screen state")
        published_insets = {
            tuple(sample["safe_insets"]) for sample in screen_states
        }
        expected_insets = {
            landscape_safe_insets,
            reverse_landscape_safe_insets,
        }
        if not expected_insets.issubset(published_insets):
            raise RuntimeError(
                "Python Screen did not receive both Android landscape safe areas: "
                f"expected={sorted(expected_insets)!r}, "
                f"published={sorted(published_insets)!r}"
            )
        required_logs = (
            tuple(arguments.require_log)
            if arguments.require_log
            else _DEFAULT_REQUIRED_LOGS
        )
        missing = tuple(marker for marker in required_logs if marker not in log)
        if missing:
            raise RuntimeError(
                "Android system multi-touch did not reach gameplay markers: "
                + ", ".join(repr(marker) for marker in missing)
            )
        fatal_lines = tuple(
            line
            for line in log.splitlines()
            if any(pattern in line for pattern in _FATAL_PATTERNS)
        )
        if fatal_lines:
            raise RuntimeError(
                "Fatal Android log entries during multi-touch:\n"
                + "\n".join(fatal_lines)
            )
        return MultiTouchResult(
            serial=device.serial,
            model=adb.run("shell", "getprop", "ro.product.model").strip(),
            android=adb.run("shell", "getprop", "ro.build.version.release").strip(),
            api=adb.run("shell", "getprop", "ro.build.version.sdk").strip(),
            abi=adb.run("shell", "getprop", "ro.product.cpu.abi").strip(),
            instrumentation_apk=str(arguments.instrumentation_apk),
            target_package=arguments.target_package,
            runner=arguments.runner,
            automated_install_approval=automated_install_approval,
            width=width,
            height=height,
            ime_inset=ime_inset,
            committed_text=committed_text,
            landscape_rotation=landscape_rotation,
            reverse_landscape_rotation=reverse_landscape_rotation,
            landscape_safe_insets=landscape_safe_insets,
            reverse_landscape_safe_insets=reverse_landscape_safe_insets,
            screen_states=screen_states,
            required_logs=required_logs,
            fatal_count=0,
            elapsed_seconds=time.perf_counter() - started,
        )
    finally:
        adb.run("shell", "am", "force-stop", arguments.target_package, check=False)
        adb.run("shell", "am", "force-stop", arguments.instrumentation_package, check=False)
        if installed:
            adb.run("uninstall", arguments.instrumentation_package, check=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instrumentation_apk", type=Path)
    parser.add_argument("--adb", type=Path, default=Path("adb"))
    parser.add_argument("--serial")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--target-package", default="com.infernux.bootstrap")
    parser.add_argument(
        "--instrumentation-package", default="com.infernux.acceptance.input"
    )
    parser.add_argument(
        "--runner",
        default=(
            "com.infernux.acceptance.input/"
            "com.infernux.acceptance.input.MultiTouchInstrumentation"
        ),
    )
    parser.add_argument("--wait-milliseconds", type=int, default=7000)
    parser.add_argument("--require-log", action="append", default=[])
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    arguments.instrumentation_apk = (
        arguments.instrumentation_apk.expanduser().resolve()
    )
    arguments.adb = arguments.adb.expanduser()
    try:
        if not arguments.instrumentation_apk.is_file():
            raise FileNotFoundError(arguments.instrumentation_apk)
        if arguments.wait_milliseconds <= 0:
            raise ValueError("--wait-milliseconds must be positive")
        result = run_smoke(arguments)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        payload = {
            "schema": "infernux.android_multitouch_smoke",
            "status": "failed",
            "instrumentation_apk": str(arguments.instrumentation_apk),
            "serial": arguments.serial or "",
            "error": str(error),
        }
        _write_report(arguments.report, payload)
        print(str(error), file=sys.stderr)
        return 1

    payload = {
        "schema": "infernux.android_multitouch_smoke",
        "status": "passed",
        **asdict(result),
    }
    _write_report(arguments.report, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
