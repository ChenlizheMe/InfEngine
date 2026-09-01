"""Validate a Windows Player without showing or focusing its native window."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from linux_player_smoke import (
    ControlClient,
    _axis_delta,
    _fatal_lines,
    _new_log_text,
    _position,
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
    fatal_count: int
    elapsed_seconds: float
    capture_path: str


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


def _load_manifest(player: Path) -> dict[str, Any]:
    manifest_path = player.with_name(f"{player.stem}_Data") / "BuildManifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Windows Player manifest was not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = manifest.get("runtime_contract", {}).get("runtime_policy", {})
    if policy.get("player_control") != "token_authenticated":
        raise RuntimeError(
            "Windows Player acceptance requires a development build with the "
            "token-authenticated player control service"
        )
    return manifest


def _state_log(game: str) -> Path:
    state_home = Path(
        os.environ.get("LOCALAPPDATA", "").strip()
        or os.environ.get("XDG_STATE_HOME", "").strip()
        or Path.home() / ".local" / "state"
    )
    return state_home / "Infernux" / "Players" / game / "Logs" / "player.log"


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player", help="Path to a built Windows Player executable")
    parser.add_argument("--report", help="Write atomic JSON acceptance evidence")
    parser.add_argument("--artifact-root", help="Directory for logs and control files")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--object", default="PlayerBall")
    parser.add_argument("--press-scancode", type=int, default=26)
    parser.add_argument("--press-duration", type=float, default=1.0)
    parser.add_argument("--axis", choices=("x", "y", "z"), default="z")
    parser.add_argument("--minimum-axis-delta", type=float, default=0.1)
    parser.add_argument(
        "--capture-file",
        default="",
        help="Optional plain .png basename captured from the Player game render target",
    )
    parser.add_argument("--capture-timeout", type=float, default=30.0)
    return parser


def _run(args: argparse.Namespace, artifact_root: Path) -> SmokeResult:
    if sys.platform != "win32":
        raise RuntimeError("windows_player_smoke.py must run on Windows")
    player = Path(args.player).expanduser().resolve()
    if not player.is_file():
        raise FileNotFoundError(f"Windows Player is missing: {player}")
    if args.startup_timeout <= 0.0 or args.press_duration <= 0.0:
        raise ValueError("timeouts and press duration must be positive")
    capture_file = str(args.capture_file or "").strip()
    if capture_file and (
        Path(capture_file).name != capture_file
        or Path(capture_file).suffix.casefold() != ".png"
    ):
        raise ValueError("--capture-file must be a plain .png basename")
    if args.capture_timeout <= 0.0 or args.capture_timeout > 60.0:
        raise ValueError("--capture-timeout must be in (0, 60]")

    manifest = _load_manifest(player)
    game = str(manifest.get("game_name", "") or player.stem)
    request = artifact_root / "control-request.json"
    response = artifact_root / "control-response.json"
    outer_log = artifact_root / "player-outer.log"
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

    process: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        with outer_log.open("w", encoding="utf-8", newline="\n") as stream:
            process = subprocess.Popen(
                [str(player)],
                cwd=player.parent,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                ),
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
                process=process,
            )
            try:
                _position(observation, args.object)
                break
            except RuntimeError:
                time.sleep(0.1)
        else:
            raise TimeoutError(
                f"Windows Player did not publish object '{args.object}' before startup timeout"
            )

        capture_path = ""
        if capture_file:
            capture = control.call(
                "capture",
                {
                    "file_name": capture_file,
                    "timeout_seconds": args.capture_timeout,
                },
                timeout=args.capture_timeout + 5.0,
                process=process,
            )
            capture_path = str(capture.get("output_path", "") or "")
            if str(capture.get("status", "")) != "completed":
                raise RuntimeError(f"Player render-target capture failed: {capture!r}")
            if not capture_path or not Path(capture_path).is_file():
                raise RuntimeError(f"Player capture artifact is missing: {capture_path!r}")

        press = control.call(
            "press",
            {
                "scancode": args.press_scancode,
                "duration_seconds": args.press_duration,
                "object_names": [args.object],
            },
            timeout=args.startup_timeout + args.press_duration,
            process=process,
        )
        initial = _position(press.get("initial_observation", {}), args.object)
        final = _position(press.get("final_observation", {}), args.object)
        delta = _axis_delta(initial, final, args.axis)
        if abs(delta) < args.minimum_axis_delta:
            raise RuntimeError(
                f"Player input produced only {delta:.6f} movement on {args.axis}; "
                f"minimum is {args.minimum_axis_delta:.6f}"
            )

        control.call("shutdown", timeout=10.0, process=process)
        process.wait(timeout=10.0)
        state_text = _new_log_text(state_log, state_start)
        (artifact_root / "player-state.log").write_text(
            state_text, encoding="utf-8", newline="\n"
        )
        combined = outer_log.read_text(encoding="utf-8", errors="replace") + state_text
        fatals = _fatal_lines(combined)
        if fatals:
            raise RuntimeError("Windows Player emitted fatal or Vulkan diagnostics")
        return SmokeResult(
            player=str(player),
            game=game,
            scene=str(observation.get("scene_name", "")),
            object_name=args.object,
            axis=args.axis,
            initial_position=initial,
            final_position=final,
            axis_delta=delta,
            fatal_count=0,
            elapsed_seconds=time.monotonic() - started,
            capture_path=capture_path,
        )
    finally:
        _terminate(process)


def main() -> int:
    args = _parser().parse_args()
    report = Path(args.report).expanduser().resolve() if args.report else None
    if args.artifact_root:
        artifact_root = Path(args.artifact_root).expanduser().resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
    else:
        artifact_root = Path(tempfile.mkdtemp(prefix="infernux-windows-player-smoke-"))
    try:
        result = _run(args, artifact_root)
        payload = {
            "schema": "infernux.windows_player_smoke",
            "status": "passed",
            **asdict(result),
        }
        if report is not None:
            _write_json_atomic(report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except BaseException as exception:
        payload = {
            "schema": "infernux.windows_player_smoke",
            "status": "failed",
            "error": f"{type(exception).__name__}: {exception}",
        }
        if report is not None:
            _write_json_atomic(report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
