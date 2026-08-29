"""Install and validate an Infernux Android Player on one ADB device."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path


_FATAL_PATTERNS = (
    "FATAL EXCEPTION",
    "Fatal signal",
    "Python exception",
    "Traceback (most recent call last)",
)


@dataclass(frozen=True, slots=True)
class Device:
    serial: str
    state: str
    attributes: dict[str, str]

    @property
    def emulator(self) -> bool:
        return self.serial.startswith("emulator-")


@dataclass(frozen=True, slots=True)
class SmokeResult:
    serial: str
    model: str
    android: str
    api: str
    abi: str
    apk: str
    pid: str
    resume_cycles: int
    back_action: bool
    fatal_count: int
    abandoned_buffer_count: int
    elapsed_seconds: float


class Adb:
    def __init__(self, executable: Path, serial: str | None = None) -> None:
        self.executable = executable
        self.serial = serial

    def run(
        self,
        *arguments: str,
        timeout: float = 60.0,
        check: bool = True,
    ) -> str:
        command = [str(self.executable)]
        if self.serial:
            command.extend(("-s", self.serial))
        command.extend(arguments)
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        if check and completed.returncode != 0:
            raise RuntimeError(
                f"ADB command failed ({completed.returncode}): "
                f"{' '.join(command)}\n{output}"
            )
        return output


def parse_devices(output: str) -> tuple[Device, ...]:
    devices: list[Device] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        attributes: dict[str, str] = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                attributes[key] = value
        devices.append(Device(parts[0], parts[1], attributes))
    return tuple(devices)


def select_device(devices: tuple[Device, ...], requested: str | None) -> Device:
    if requested:
        for device in devices:
            if device.serial == requested:
                if device.state != "device":
                    raise RuntimeError(
                        f"ADB device {requested} is {device.state}; unlock and authorize it"
                    )
                return device
        raise RuntimeError(f"ADB device is not connected: {requested}")

    available = tuple(device for device in devices if device.state == "device")
    physical = tuple(device for device in available if not device.emulator)
    if len(physical) == 1:
        return physical[0]
    if len(available) == 1:
        return available[0]
    if not available:
        raise RuntimeError("No authorized ADB device is connected")
    raise RuntimeError(
        "Multiple ADB devices are connected; pass --serial with one of: "
        + ", ".join(device.serial for device in available)
    )


def apk_abis(apk: Path) -> frozenset[str]:
    with zipfile.ZipFile(apk) as archive:
        return frozenset(
            match.group(1)
            for name in archive.namelist()
            if (match := re.match(r"lib/([^/]+)/[^/]+\.so$", name))
        )


def _wait_for_player(
    adb: Adb,
    package: str,
    expected_log: str,
    timeout: float,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    last_log = ""
    while time.monotonic() < deadline:
        pid = adb.run("shell", "pidof", package, check=False).strip()
        last_log = adb.run("logcat", "-d", "-v", "brief", check=False)
        if pid and expected_log in last_log:
            return pid, last_log
        time.sleep(0.5)
    raise RuntimeError(
        f"Android Player did not publish {expected_log!r} within {timeout:.1f}s\n"
        + "\n".join(last_log.splitlines()[-120:])
    )


def run_smoke(arguments: argparse.Namespace) -> SmokeResult:
    started = time.perf_counter()
    controller = Adb(arguments.adb)
    device = select_device(
        parse_devices(controller.run("devices", "-l")), arguments.serial
    )
    adb = Adb(arguments.adb, device.serial)
    abi = adb.run("shell", "getprop", "ro.product.cpu.abi").strip()
    packaged_abis = apk_abis(arguments.apk)
    if abi not in packaged_abis:
        raise RuntimeError(
            f"APK ABIs {sorted(packaged_abis)} do not include device ABI {abi}"
        )

    if not arguments.no_install:
        adb.run("install", "-r", str(arguments.apk), timeout=300.0)
    adb.run("logcat", "-c")
    adb.run("shell", "am", "force-stop", arguments.package)
    adb.run("shell", "am", "start", "-n", arguments.activity)
    pid, log = _wait_for_player(
        adb,
        arguments.package,
        arguments.expect_log,
        arguments.startup_timeout,
    )

    back_action = False
    if not arguments.no_back:
        adb.run("shell", "input", "keyevent", "4")
        time.sleep(1.0)
        after_back_pid = adb.run(
            "shell", "pidof", arguments.package, check=False
        ).strip()
        log = adb.run("logcat", "-d", "-v", "brief", check=False)
        if after_back_pid != pid:
            raise RuntimeError("Android Back terminated or restarted the Player")
        back_action = arguments.expect_back_log in log
        if not back_action:
            raise RuntimeError(
                "Android Back did not reach the expected gameplay action: "
                + repr(arguments.expect_back_log)
            )

    for _ in range(arguments.resume_cycles):
        adb.run("shell", "input", "keyevent", "3")
        time.sleep(0.75)
        adb.run("shell", "am", "start", "-n", arguments.activity)
        time.sleep(1.5)
        current_pid = adb.run(
            "shell", "pidof", arguments.package, check=False
        ).strip()
        if current_pid != pid:
            raise RuntimeError(
                f"Player PID changed across resume: expected {pid}, got {current_pid}"
            )

    log = adb.run("logcat", "-d", "-v", "brief", check=False)
    fatal_count = sum(log.count(pattern) for pattern in _FATAL_PATTERNS)
    if fatal_count:
        fatal_lines = [
            line for line in log.splitlines() if any(p in line for p in _FATAL_PATTERNS)
        ]
        raise RuntimeError("Fatal Android log entries:\n" + "\n".join(fatal_lines))

    return SmokeResult(
        serial=device.serial,
        model=adb.run("shell", "getprop", "ro.product.model").strip(),
        android=adb.run("shell", "getprop", "ro.build.version.release").strip(),
        api=adb.run("shell", "getprop", "ro.build.version.sdk").strip(),
        abi=abi,
        apk=str(arguments.apk),
        pid=pid,
        resume_cycles=arguments.resume_cycles,
        back_action=back_action,
        fatal_count=fatal_count,
        abandoned_buffer_count=log.count("BufferQueue has been abandoned"),
        elapsed_seconds=time.perf_counter() - started,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--adb", type=Path, default=Path("adb"))
    parser.add_argument("--serial")
    parser.add_argument("--package", default="com.infernux.bootstrap")
    parser.add_argument(
        "--activity",
        default="com.infernux.bootstrap/com.infernux.bootstrap.InfernuxActivity",
    )
    parser.add_argument("--expect-log", default="ENGINE_LOADED")
    parser.add_argument("--expect-back-log", default="BALANCE // CANCEL ACTION")
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--resume-cycles", type=int, default=3)
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--no-back", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    arguments.apk = arguments.apk.expanduser().resolve()
    arguments.adb = arguments.adb.expanduser()
    if not arguments.apk.is_file():
        raise FileNotFoundError(arguments.apk)
    if arguments.resume_cycles < 0:
        raise ValueError("--resume-cycles cannot be negative")
    result = run_smoke(arguments)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
