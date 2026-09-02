"""Install and validate an Infernux Android Player on one ADB device."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
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
    automated_install_approval: bool
    reinstalled_after_signature_mismatch: bool
    resume_cycles: int
    back_action: bool
    touch_action: bool
    touch_attempts: int
    required_logs: tuple[str, ...]
    landscape_surface: bool
    surface_extents: tuple[tuple[int, int], ...]
    fatal_count: int
    abandoned_buffer_count: int
    surface_creation_count: int
    elapsed_seconds: float


class Adb:
    def __init__(self, executable: Path, serial: str | None = None) -> None:
        self.executable = executable
        self.serial = serial

    def command(self, *arguments: str) -> list[str]:
        command = [str(self.executable)]
        if self.serial:
            command.extend(("-s", self.serial))
        command.extend(arguments)
        return command

    def run(
        self,
        *arguments: str,
        timeout: float = 60.0,
        check: bool = True,
    ) -> str:
        command = self.command(*arguments)
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


_BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def oem_install_approval_target(hierarchy: str) -> tuple[int, int] | None:
    """Find a narrowly-scoped OEM USB-install confirmation button."""
    try:
        root = ET.fromstring(hierarchy)
    except ET.ParseError:
        return None

    for node in root.iter("node"):
        attributes = node.attrib
        if attributes.get("package") != "com.miui.securitycenter":
            continue
        if attributes.get("resource-id") != "android:id/button2":
            continue
        if attributes.get("clickable") != "true" or attributes.get("enabled") != "true":
            continue
        if attributes.get("text") not in {"继续安装", "Continue installation"}:
            continue
        match = _BOUNDS_PATTERN.fullmatch(attributes.get("bounds", ""))
        if not match:
            continue
        left, top, right, bottom = (int(value) for value in match.groups())
        if right > left and bottom > top:
            return ((left + right) // 2, (top + bottom) // 2)
    return None


def install_apk(adb: Adb, apk: Path, *, replace: bool) -> bool:
    """Install an APK and approve the known HyperOS USB prompt when present."""
    arguments = ["install"]
    if replace:
        arguments.append("-r")
    arguments.extend(("-t", str(apk)))
    process = subprocess.Popen(
        adb.command(*arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + 300.0
    next_approval_probe = time.monotonic() + 2.0
    approved = False
    remote_dump = "/sdcard/infernux-install-approval.xml"
    while process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate()
            raise RuntimeError("ADB install timed out after 300 seconds")
        now = time.monotonic()
        if not approved and now >= next_approval_probe:
            adb.run("shell", "rm", "-f", remote_dump, check=False)
            adb.run("shell", "uiautomator", "dump", remote_dump, check=False)
            hierarchy = adb.run("shell", "cat", remote_dump, check=False)
            if target := oem_install_approval_target(hierarchy):
                adb.run("shell", "input", "tap", str(target[0]), str(target[1]))
                approved = True
            else:
                next_approval_probe = time.monotonic() + 0.75
        time.sleep(0.25)

    stdout, stderr = process.communicate()
    adb.run("shell", "rm", "-f", remote_dump, check=False)
    output = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if process.returncode != 0:
        raise RuntimeError(
            f"ADB command failed ({process.returncode}): "
            f"{' '.join(adb.command(*arguments))}\n{output}"
        )
    return approved


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


def keyguard_is_showing(policy: str) -> bool:
    """Return the authoritative KeyguardServiceDelegate visibility state."""
    in_delegate = False
    for raw_line in policy.splitlines():
        line = raw_line.strip()
        if line == "KeyguardServiceDelegate":
            in_delegate = True
            continue
        if in_delegate and line.startswith("showing="):
            return line.partition("=")[2].strip().casefold() == "true"
    # An unknown policy format is not evidence that the device is unlocked.
    return True


def physical_display_size(output: str) -> tuple[int, int] | None:
    for raw_line in output.splitlines():
        match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", raw_line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def vulkan_surface_extents(log: str) -> tuple[tuple[int, int], ...]:
    """Read the actual swapchain extents emitted by the Vulkan backend."""
    return tuple(
        (int(match.group(1)), int(match.group(2)))
        for match in re.finditer(
            r"INFERNUX_VULKAN_SURFACE\b[^\r\n]*\bextent=(\d+)x(\d+)", log
        )
    )


def inject_touch_gesture(
    adb: Adb, width: int, height: int, *, step_delay: float = 0.25
) -> None:
    """Inject a real multi-frame touchscreen gesture through Android input."""
    points = (
        # Exercise the visible lower-left virtual stick. Android coordinates
        # use a top-left origin while the public Touch API uses Unity-style
        # bottom-left screen pixels.
        ("DOWN", max(1, width * 11 // 100), max(1, height * 82 // 100)),
        ("MOVE", max(1, width * 18 // 100), max(1, height * 70 // 100)),
        ("UP", max(1, width * 22 // 100), max(1, height * 66 // 100)),
    )
    for index, (phase, x, y) in enumerate(points):
        adb.run(
            "shell",
            "input",
            "touchscreen",
            "motionevent",
            phase,
            str(x),
            str(y),
        )
        if index + 1 < len(points):
            time.sleep(step_delay)


def unlock_device(adb: Adb, timeout: float = 10.0) -> None:
    """Wake and dismiss a non-secure keyguard before launching the Player."""
    adb.run("shell", "input", "keyevent", "KEYCODE_WAKEUP", check=False)
    adb.run("shell", "wm", "dismiss-keyguard", check=False)
    deadline = time.monotonic() + timeout
    gesture_sent = False
    last_policy = ""
    while time.monotonic() < deadline:
        last_policy = adb.run("shell", "dumpsys", "window", "policy", check=False)
        if not keyguard_is_showing(last_policy):
            return
        if not gesture_sent:
            size = physical_display_size(
                adb.run("shell", "wm", "size", check=False)
            )
            if size is not None:
                width, height = size
                adb.run(
                    "shell",
                    "input",
                    "swipe",
                    str(width // 2),
                    str(height * 4 // 5),
                    str(width // 2),
                    str(height // 5),
                    "300",
                    check=False,
                )
            adb.run("shell", "wm", "dismiss-keyguard", check=False)
            gesture_sent = True
        time.sleep(0.25)
    raise RuntimeError(
        "Android device is still locked; unlock it before running Player acceptance"
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


def _wait_for_required_logs(
    adb: Adb,
    package: str,
    expected_pid: str,
    required_logs: tuple[str, ...],
    timeout: float,
) -> str:
    """Wait until one Player process has published every required marker."""

    deadline = time.monotonic() + timeout
    last_log = ""
    missing = required_logs
    while time.monotonic() < deadline:
        current_pid = adb.run("shell", "pidof", package, check=False).strip()
        if current_pid != expected_pid:
            raise RuntimeError(
                "Android Player PID changed while waiting for runtime diagnostics: "
                f"expected {expected_pid}, got {current_pid or '<missing>'}"
            )
        last_log = adb.run("logcat", "-d", "-v", "brief", check=False)
        missing = tuple(marker for marker in required_logs if marker not in last_log)
        if not missing:
            return last_log
        time.sleep(0.5)
    raise RuntimeError(
        "Android Player did not publish required runtime diagnostics within "
        f"{timeout:.1f}s: {list(missing)!r}\n"
        + "\n".join(last_log.splitlines()[-120:])
    )


def _wait_for_foreground(adb: Adb, package: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        last_state = adb.run(
            "shell", "dumpsys", "activity", "activities", check=False
        )
        resumed_lines = (
            line
            for line in last_state.splitlines()
            if "ResumedActivity" in line or "topResumedActivity" in line
        )
        if any(package in line for line in resumed_lines):
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"Android Player did not become the resumed activity within {timeout:.1f}s"
    )


def _wait_for_input_focus(adb: Adb, package: str, timeout: float = 10.0) -> None:
    """Wait until Android will dispatch injected pointer events to the Player."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window_state = adb.run("shell", "dumpsys", "window", "displays", check=False)
        if any(
            "mCurrentFocus=" in line and package in line
            for line in window_state.splitlines()
        ):
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"Android Player did not gain input focus within {timeout:.1f}s"
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

    unlock_device(adb)
    reinstalled_after_signature_mismatch = False
    automated_install_approval = False
    if not arguments.no_install:
        try:
            automated_install_approval = install_apk(
                adb, arguments.apk, replace=True
            )
        except RuntimeError as error:
            if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" not in str(error):
                raise
            # Acceptance builds may come from different workstations or
            # regenerated debug keystores. The package is test data, so a
            # signature mismatch is the one installation failure for which a
            # clean reinstall is both deterministic and safe.
            adb.run("uninstall", arguments.package, check=False)
            automated_install_approval = install_apk(
                adb, arguments.apk, replace=False
            )
            reinstalled_after_signature_mismatch = True
    adb.run("logcat", "-c")
    adb.run("shell", "am", "force-stop", arguments.package)
    adb.run("shell", "am", "start", "-n", arguments.activity)
    _wait_for_foreground(adb, arguments.package)
    pid, log = _wait_for_player(
        adb,
        arguments.package,
        arguments.expect_log,
        arguments.startup_timeout,
    )
    if arguments.expect_ready_log and arguments.expect_ready_log not in log:
        ready_pid, log = _wait_for_player(
            adb,
            arguments.package,
            arguments.expect_ready_log,
            arguments.startup_timeout,
        )
        if ready_pid != pid:
            raise RuntimeError(
                f"Player PID changed before gameplay became ready: expected {pid}, "
                f"got {ready_pid}"
            )

    touch_action = False
    touch_attempts = 0
    if arguments.touch:
        extents = vulkan_surface_extents(log)
        if not extents:
            raise RuntimeError("Android Player emitted no Vulkan surface extent")
        _wait_for_foreground(adb, arguments.package)
        _wait_for_input_focus(adb, arguments.package)
        # The focused window can be published one frame before an Android
        # rotation transition's input consumer is removed. Give that consumer
        # one short frame-independent settling interval before injection.
        time.sleep(0.75)
        width, height = extents[-1]
        for touch_attempts in range(1, arguments.touch_attempts + 1):
            inject_touch_gesture(adb, width, height)
            time.sleep(1.0)
            log = adb.run("logcat", "-d", "-v", "brief", check=False)
            touch_action = (
                arguments.expect_touch_begin_log in log
                and arguments.expect_touch_end_log in log
                and arguments.expect_stick_begin_log in log
                and arguments.expect_stick_end_log in log
                and arguments.expect_touch_action_log in log
                and arguments.expect_unity_touch_api_log in log
            )
            if touch_action:
                break
            if touch_attempts < arguments.touch_attempts:
                _wait_for_foreground(adb, arguments.package)
                _wait_for_input_focus(adb, arguments.package)
                # OEM game/sidebar overlays can receive the first injected
                # ACTION_OUTSIDE while their launch animation is retiring.
                # Retry the same real Android input path after it settles.
                time.sleep(2.0)
        if not touch_action:
            raise RuntimeError(
                "Injected Android touchscreen gesture did not reach gameplay: "
                f"expected {arguments.expect_touch_begin_log!r} and "
                f"{arguments.expect_touch_end_log!r}, "
                f"{arguments.expect_stick_begin_log!r}, "
                f"{arguments.expect_stick_end_log!r}, and "
                f"{arguments.expect_touch_action_log!r}, and "
                f"{arguments.expect_unity_touch_api_log!r} after "
                f"{arguments.touch_attempts} attempts"
            )

    back_action = False
    if not arguments.no_back:
        _wait_for_foreground(adb, arguments.package)
        time.sleep(0.5)
        adb.run("shell", "input", "keyevent", "4")
        time.sleep(1.5)
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

    required_logs = tuple(dict.fromkeys(arguments.require_log))
    if required_logs:
        log = _wait_for_required_logs(
            adb,
            arguments.package,
            pid,
            required_logs,
            arguments.startup_timeout,
        )
    else:
        log = adb.run("logcat", "-d", "-v", "brief", check=False)
    fatal_count = sum(log.count(pattern) for pattern in _FATAL_PATTERNS)
    if fatal_count:
        fatal_lines = [
            line for line in log.splitlines() if any(p in line for p in _FATAL_PATTERNS)
        ]
        raise RuntimeError("Fatal Android log entries:\n" + "\n".join(fatal_lines))

    surface_extents = vulkan_surface_extents(log)
    surface_creation_count = len(surface_extents)
    max_surface_creations = (
        arguments.max_surface_creations
        if arguments.max_surface_creations is not None
        else arguments.resume_cycles + 2
    )
    if surface_creation_count > max_surface_creations:
        raise RuntimeError(
            "Android Player recreated its Vulkan swapchain too often: "
            f"{surface_creation_count} creations, limit {max_surface_creations}"
        )
    abandoned_buffer_count = log.count("BufferQueue has been abandoned")
    if abandoned_buffer_count > arguments.max_abandoned_buffers:
        raise RuntimeError(
            "Android Player kept rendering to an abandoned SurfaceView: "
            f"{abandoned_buffer_count} errors, limit {arguments.max_abandoned_buffers}"
        )
    landscape_surface = bool(
        surface_extents and surface_extents[-1][0] > surface_extents[-1][1]
    )
    if arguments.expect_landscape and not landscape_surface:
        raise RuntimeError(
            "Android Player final Vulkan surface is not landscape: "
            f"{surface_extents[-1] if surface_extents else '<missing>'}"
        )

    return SmokeResult(
        serial=device.serial,
        model=adb.run("shell", "getprop", "ro.product.model").strip(),
        android=adb.run("shell", "getprop", "ro.build.version.release").strip(),
        api=adb.run("shell", "getprop", "ro.build.version.sdk").strip(),
        abi=abi,
        apk=str(arguments.apk),
        pid=pid,
        automated_install_approval=automated_install_approval,
        reinstalled_after_signature_mismatch=reinstalled_after_signature_mismatch,
        resume_cycles=arguments.resume_cycles,
        back_action=back_action,
        touch_action=touch_action,
        touch_attempts=touch_attempts,
        required_logs=required_logs,
        landscape_surface=landscape_surface,
        surface_extents=surface_extents,
        fatal_count=fatal_count,
        abandoned_buffer_count=abandoned_buffer_count,
        surface_creation_count=surface_creation_count,
        elapsed_seconds=time.perf_counter() - started,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--adb", type=Path, default=Path("adb"))
    parser.add_argument("--report", type=Path, help="Write atomic JSON acceptance evidence")
    parser.add_argument("--serial")
    parser.add_argument("--package", default="com.infernux.bootstrap")
    parser.add_argument(
        "--activity",
        default="com.infernux.bootstrap/com.infernux.bootstrap.InfernuxActivity",
    )
    parser.add_argument("--expect-log", default="ENGINE_LOADED")
    parser.add_argument(
        "--require-log",
        action="append",
        default=[],
        help="Require this runtime log marker; may be repeated",
    )
    parser.add_argument(
        "--expect-ready-log",
        default="",
        help="Wait for this gameplay-ready marker before injecting Android Back",
    )
    parser.add_argument("--expect-back-log", default="BALANCE // CANCEL ACTION")
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--resume-cycles", type=int, default=3)
    parser.add_argument(
        "--max-surface-creations",
        type=int,
        default=None,
        help="Maximum swapchain generations; defaults to resume cycles plus two",
    )
    parser.add_argument(
        "--max-abandoned-buffers",
        type=int,
        default=8,
        help="Maximum tolerated abandoned SurfaceView dequeue errors",
    )
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--no-back", action="store_true")
    parser.add_argument(
        "--touch",
        action="store_true",
        help="Inject one touchscreen gesture and require gameplay touch markers",
    )
    parser.add_argument(
        "--touch-attempts",
        type=int,
        default=3,
        help="Maximum real touchscreen gesture attempts after gameplay becomes ready",
    )
    parser.add_argument(
        "--expect-touch-begin-log", default="BALANCE // TOUCH BEGIN"
    )
    parser.add_argument(
        "--expect-touch-end-log", default="BALANCE // TOUCH END"
    )
    parser.add_argument(
        "--expect-stick-begin-log", default="BALANCE // STICK BEGIN"
    )
    parser.add_argument(
        "--expect-stick-end-log", default="BALANCE // STICK END"
    )
    parser.add_argument(
        "--expect-touch-action-log", default="INFERNUX_ACCEPTANCE_INPUT_READY"
    )
    parser.add_argument(
        "--expect-unity-touch-api-log",
        default="INFERNUX_ACCEPTANCE_UNITY_TOUCH_API_READY",
    )
    parser.add_argument(
        "--expect-landscape",
        action="store_true",
        help="Require the final Vulkan swapchain extent to be landscape",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the Player running after acceptance for manual visual inspection",
    )
    return parser


def _stop_player_best_effort(arguments: argparse.Namespace, serial: str | None) -> None:
    """Stop the accepted Player without hiding the original acceptance result."""

    try:
        if serial is None:
            controller = Adb(arguments.adb)
            device = select_device(
                parse_devices(controller.run("devices", "-l")), arguments.serial
            )
            serial = device.serial
        Adb(arguments.adb, serial).run(
            "shell", "am", "force-stop", arguments.package, check=False
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return


def _write_report(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def main() -> int:
    arguments = _parser().parse_args()
    arguments.apk = arguments.apk.expanduser().resolve()
    arguments.adb = arguments.adb.expanduser()
    result = None
    run_started = False
    try:
        if not arguments.apk.is_file():
            raise FileNotFoundError(arguments.apk)
        if arguments.resume_cycles < 0:
            raise ValueError("--resume-cycles cannot be negative")
        if (
            arguments.max_surface_creations is not None
            and arguments.max_surface_creations < 1
        ):
            raise ValueError("--max-surface-creations must be positive")
        if arguments.max_abandoned_buffers < 0:
            raise ValueError("--max-abandoned-buffers cannot be negative")
        if arguments.touch_attempts < 1:
            raise ValueError("--touch-attempts must be positive")
        run_started = True
        result = run_smoke(arguments)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        payload = {
            "schema": "infernux.android_player_smoke",
            "status": "failed",
            "apk": str(arguments.apk),
            "serial": arguments.serial or "",
            "error": str(error),
        }
        _write_report(arguments.report, payload)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if run_started and not arguments.keep_running:
            _stop_player_best_effort(
                arguments,
                result.serial if result is not None else arguments.serial,
            )
    payload = {
        "schema": "infernux.android_player_smoke",
        "status": "passed",
        **asdict(result),
    }
    _write_report(arguments.report, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
