"""Validate a Linux Player through its authenticated in-process control channel."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_FATAL_PATTERNS = (
    "Validation Error",
    "VUID-",
    "CRASH:",
    "Traceback (most recent call last)",
    "[ERROR]",
)


@dataclass(frozen=True, slots=True)
class SmokeResult:
    player: str
    game: str
    scene: str
    object_name: str
    axis: str
    initial_position: tuple[float, float, float]
    final_position: tuple[float, float, float]
    axis_delta: float
    validation: bool
    vk_driver_files: str
    fatal_count: int
    elapsed_seconds: float


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _player_data_directory(player: Path) -> Path:
    return player.with_name(f"{player.name}_Data")


def _load_debug_manifest(player: Path) -> dict[str, Any]:
    manifest_path = _player_data_directory(player) / "BuildManifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Linux Player manifest was not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_policy = manifest.get("runtime_contract", {}).get("runtime_policy", {})
    if runtime_policy.get("player_control") != "token_authenticated":
        raise RuntimeError(
            "Linux Player acceptance requires a development build with the "
            "token-authenticated player control service"
        )
    return manifest


def _position(data: dict[str, Any], object_name: str) -> tuple[float, float, float]:
    objects = data.get("objects", {})
    value = objects.get(object_name, {}).get("position")
    if not isinstance(value, list) or len(value) != 3:
        raise RuntimeError(f"Player object '{object_name}' has no public position")
    return tuple(float(component) for component in value)


def _axis_delta(
    initial: tuple[float, float, float],
    final: tuple[float, float, float],
    axis: str,
) -> float:
    return final[{"x": 0, "y": 1, "z": 2}[axis]] - initial[
        {"x": 0, "y": 1, "z": 2}[axis]
    ]


class ControlClient:
    def __init__(self, request: Path, response: Path, token: str) -> None:
        self.request = request
        self.response = response
        self._token = token

    def call(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
        process: subprocess.Popen[str],
    ) -> dict[str, Any]:
        command_id = f"linux-smoke-{uuid.uuid4().hex}"
        self.response.unlink(missing_ok=True)
        _write_json_atomic(
            self.request,
            {
                "command_id": command_id,
                "token": self._token,
                "action": action,
                **(payload or {}),
            },
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.response.is_file():
                try:
                    response = json.loads(self.response.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(0.05)
                    continue
                if response.get("command_id") != command_id:
                    time.sleep(0.05)
                    continue
                if not response.get("ok"):
                    raise RuntimeError(
                        f"Player control action '{action}' failed: "
                        f"{response.get('error', 'unknown error')}"
                    )
                return dict(response.get("data", {}))
            if process.poll() is not None:
                raise RuntimeError(
                    f"Linux Player exited before '{action}' completed "
                    f"(code {process.returncode})"
                )
            time.sleep(0.05)
        raise TimeoutError(f"Linux Player control action '{action}' timed out")


def _terminate(process: subprocess.Popen[str] | None, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=timeout)


def _state_log(game: str) -> Path:
    return (
        Path.home()
        / ".local"
        / "state"
        / "Infernux"
        / "Players"
        / game
        / "Logs"
        / "player.log"
    )


def _new_log_text(path: Path, start_size: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(min(start_size, path.stat().st_size))
        return stream.read().decode("utf-8", errors="replace")


def _fatal_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if any(pattern.casefold() in line.casefold() for pattern in _FATAL_PATTERNS)
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player", help="Path to a built Linux Player executable")
    parser.add_argument("--report", help="Write atomic JSON acceptance evidence")
    parser.add_argument("--artifact-root", help="Directory for logs and control files")
    parser.add_argument("--xvfb", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--display", default=":98")
    parser.add_argument("--vk-driver-files", default="")
    parser.add_argument("--validation", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--object", default="PlayerBall")
    parser.add_argument("--press-scancode", type=int, default=26)
    parser.add_argument("--press-duration", type=float, default=1.0)
    parser.add_argument("--axis", choices=("x", "y", "z"), default="z")
    parser.add_argument("--minimum-axis-delta", type=float, default=0.1)
    return parser


def _run(args: argparse.Namespace, artifact_root: Path) -> SmokeResult:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("linux_player_smoke.py must run on Linux")
    player = Path(args.player).expanduser().resolve()
    if not player.is_file() or not os.access(player, os.X_OK):
        raise FileNotFoundError(f"Linux Player is not executable: {player}")
    manifest = _load_debug_manifest(player)
    game = str(manifest.get("game", "") or player.name)
    if args.startup_timeout <= 0.0 or args.press_duration <= 0.0:
        raise ValueError("timeouts and press duration must be positive")

    request = artifact_root / "control-request.json"
    response = artifact_root / "control-response.json"
    outer_log = artifact_root / "player-outer.log"
    xvfb_log = artifact_root / "xvfb.log"
    state_log = _state_log(game)
    state_start = state_log.stat().st_size if state_log.is_file() else 0
    token = secrets.token_hex(24)
    environment = os.environ.copy()
    environment.update(
        {
            "_INFERNUX_PLAYER_DEBUG_BUILD": "1",
            "_INFERNUX_PLAYER_CONTROL_FILE": str(request),
            "_INFERNUX_PLAYER_RESPONSE_FILE": str(response),
            "_INFERNUX_PLAYER_CONTROL_TOKEN": token,
            "_INFERNUX_PLAYER_ARTIFACT_ROOT": str(artifact_root),
        }
    )
    if args.vk_driver_files:
        environment["VK_DRIVER_FILES"] = str(
            Path(args.vk_driver_files).expanduser().resolve()
        )
    if args.validation:
        environment["VK_INSTANCE_LAYERS"] = "VK_LAYER_KHRONOS_validation"

    use_xvfb = args.xvfb == "always" or (
        args.xvfb == "auto" and not environment.get("DISPLAY")
    )
    xvfb_process: subprocess.Popen[str] | None = None
    player_process: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        if use_xvfb:
            xvfb = shutil.which("Xvfb")
            if not xvfb:
                raise RuntimeError("Xvfb is required but was not found on PATH")
            with xvfb_log.open("w", encoding="utf-8", newline="\n") as xvfb_stream:
                xvfb_process = subprocess.Popen(
                    [
                        xvfb,
                        args.display,
                        "-screen",
                        "0",
                        "1280x720x24",
                        "-nolisten",
                        "tcp",
                    ],
                    stdout=xvfb_stream,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            environment["DISPLAY"] = args.display
            time.sleep(0.5)
            if xvfb_process.poll() is not None:
                raise RuntimeError(f"Xvfb exited with code {xvfb_process.returncode}")
        elif not environment.get("DISPLAY"):
            raise RuntimeError("DISPLAY is unset and --xvfb=never was requested")

        with outer_log.open("w", encoding="utf-8", newline="\n") as outer_stream:
            player_process = subprocess.Popen(
                [str(player)],
                cwd=player.parent,
                env=environment,
                stdout=outer_stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        control = ControlClient(request, response, token)
        deadline = time.monotonic() + args.startup_timeout
        observation: dict[str, Any] = {}
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            observation = control.call(
                "observe",
                {"object_names": [args.object]},
                timeout=remaining,
                process=player_process,
            )
            try:
                initial = _position(observation, args.object)
                break
            except RuntimeError:
                time.sleep(0.1)
        else:
            raise TimeoutError(
                f"Linux Player did not publish object '{args.object}' before startup timeout"
            )

        press = control.call(
            "press",
            {
                "scancode": args.press_scancode,
                "duration_seconds": args.press_duration,
                "object_names": [args.object],
            },
            timeout=args.startup_timeout + args.press_duration,
            process=player_process,
        )
        initial = _position(press.get("initial_observation", {}), args.object)
        final = _position(press.get("final_observation", {}), args.object)
        delta = _axis_delta(initial, final, args.axis)
        if abs(delta) < args.minimum_axis_delta:
            raise RuntimeError(
                f"Player input produced only {delta:.6f} movement on {args.axis}; "
                f"minimum is {args.minimum_axis_delta:.6f}"
            )

        control.call("shutdown", timeout=10.0, process=player_process)
        try:
            player_process.wait(timeout=10.0)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Linux Player ignored its normal shutdown request") from exc

        state_text = _new_log_text(state_log, state_start)
        (artifact_root / "player-state.log").write_text(
            state_text, encoding="utf-8", newline="\n"
        )
        combined = outer_log.read_text(encoding="utf-8", errors="replace") + state_text
        fatals = _fatal_lines(combined)
        if fatals:
            raise RuntimeError("Linux Player emitted fatal or Vulkan validation diagnostics")
        return SmokeResult(
            player=str(player),
            game=game,
            scene=str(observation.get("scene_name", "")),
            object_name=args.object,
            axis=args.axis,
            initial_position=initial,
            final_position=final,
            axis_delta=delta,
            validation=bool(args.validation),
            vk_driver_files=str(environment.get("VK_DRIVER_FILES", "")),
            fatal_count=0,
            elapsed_seconds=time.monotonic() - started,
        )
    finally:
        _terminate(player_process)
        _terminate(xvfb_process)


def main() -> int:
    args = _parser().parse_args()
    report = Path(args.report).expanduser().resolve() if args.report else None
    if args.artifact_root:
        artifact_root = Path(args.artifact_root).expanduser().resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
    else:
        artifact_root = Path(
            tempfile.mkdtemp(prefix="infernux-linux-player-smoke-")
        ).resolve()
    payload: dict[str, Any] = {
        "schema": 1,
        "status": "failed",
        "artifact_root": str(artifact_root),
    }
    try:
        result = _run(args, artifact_root)
        payload.update({"status": "passed", "result": asdict(result)})
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        if report:
            _write_json_atomic(report, payload)
        print(payload["error"], file=sys.stderr)
        return 1
    if report:
        _write_json_atomic(report, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
