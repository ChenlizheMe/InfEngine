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
_PASSED_CODE = "INSTRUMENTATION_CODE: -1"
_EXTENT_PATTERN = re.compile(r"INSTRUMENTATION_RESULT: (height|width)=(\d+)")
_DEFAULT_REQUIRED_LOGS = (
    "INFERNUX_PLATFORM_FIXTURE_MULTITOUCH_READY",
    "INFERNUX_PLATFORM_FIXTURE_UNITY_TOUCH_READY",
    "INFERNUX_PLATFORM_FIXTURE_TOUCH_CANCELED",
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


def validate_instrumentation_output(output: str) -> tuple[int, int]:
    if _PASSED_RESULT not in output or _PASSED_CODE not in output:
        raise RuntimeError("Android system multi-touch instrumentation failed:\n" + output)
    return instrumentation_extent(output)


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
            adb, arguments.instrumentation_apk, replace=False
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
        width, height = validate_instrumentation_output(output)
        time.sleep(0.5)
        log = adb.run("logcat", "-d", "-v", "brief", check=False)
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
