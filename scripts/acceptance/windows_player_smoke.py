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


_SCRIPT_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _SCRIPT_ROOT.parents[1]
_PYTHON_ROOT = _REPOSITORY_ROOT / "python"
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

from Infernux.engine.platform_player_bootstrap import (  # noqa: E402
    read_player_build_manifest,
)

from linux_player_smoke import (  # noqa: E402
    _assert_component_probes,
    ControlClient,
    _axis_delta,
    _fatal_lines,
    _gameplay_control_ready,
    _new_log_text,
    _parse_component_probe,
    _player_component_probes,
    _position,
    _probe_object_names,
    _require_minimum_final_y,
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
    shutdown_elapsed_seconds: float
    capture_path: str
    capture_only: bool
    fixed_delta_seconds: float
    pause_after_frame: int
    captured_runtime_frame: int
    submission_ready: bool
    component_assertions: list[dict[str, Any]]
    component_fields: dict[str, Any]


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
    data_root = player.with_name(f"{player.stem}_Data")
    manifest = read_player_build_manifest(data_root)
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
    parser.add_argument("--maximum-shutdown-seconds", type=float, default=2.0)
    parser.add_argument("--object", default="PlayerBall")
    parser.add_argument("--press-scancode", type=int, default=26)
    parser.add_argument("--press-duration", type=float, default=1.0)
    parser.add_argument("--axis", choices=("x", "y", "z"), default="z")
    parser.add_argument("--minimum-axis-delta", type=float, default=0.1)
    parser.add_argument("--minimum-final-y", type=float)
    parser.add_argument("--click-at", nargs=2, type=float, metavar=("X", "Y"),
                        help="Click a UI button at client coordinates before capture and component assertions")
    parser.add_argument(
        "--capture-file",
        default="",
        help="Optional plain .png basename captured from the Player game render target",
    )
    parser.add_argument("--capture-timeout", type=float, default=30.0)
    parser.add_argument(
        "--capture-only",
        action="store_true",
        help="Capture deterministic render evidence without injecting gameplay input",
    )
    parser.add_argument(
        "--fixed-delta",
        type=float,
        default=0.0,
        help="PlayerDebug-only fixed simulation delta used for deterministic capture",
    )
    parser.add_argument(
        "--pause-after-frame",
        type=int,
        default=0,
        help="Pause after this exact active-scene gameplay frame",
    )
    parser.add_argument(
        "--component-probe",
        action="append",
        type=_parse_component_probe,
        default=[],
        help="Repeatable JSON component probe with public fields and assertions",
    )
    return parser


def _run(args: argparse.Namespace, artifact_root: Path) -> SmokeResult:
    if sys.platform != "win32":
        raise RuntimeError("windows_player_smoke.py must run on Windows")
    player = Path(args.player).expanduser().resolve()
    if not player.is_file():
        raise FileNotFoundError(f"Windows Player is missing: {player}")
    if args.startup_timeout <= 0.0 or args.press_duration <= 0.0:
        raise ValueError("timeouts and press duration must be positive")
    if args.maximum_shutdown_seconds <= 0.0:
        raise ValueError("--maximum-shutdown-seconds must be positive")
    capture_file = str(args.capture_file or "").strip()
    if capture_file and (
        Path(capture_file).name != capture_file
        or Path(capture_file).suffix.casefold() != ".png"
    ):
        raise ValueError("--capture-file must be a plain .png basename")
    if args.capture_timeout <= 0.0 or args.capture_timeout > 60.0:
        raise ValueError("--capture-timeout must be in (0, 60]")
    deterministic_capture = args.fixed_delta != 0.0 or args.pause_after_frame != 0
    if deterministic_capture:
        if not (0.0 < args.fixed_delta <= 0.25) or args.pause_after_frame <= 0:
            raise ValueError(
                "deterministic capture requires --fixed-delta in (0, 0.25] and "
                "a positive --pause-after-frame"
            )
        if not args.capture_only or not capture_file:
            raise ValueError(
                "deterministic capture requires --capture-only and --capture-file"
            )

    manifest = _load_manifest(player)
    game = str(manifest.get("game_name", "") or player.stem)
    request = artifact_root / "control-request.json"
    response = artifact_root / "control-response.json"
    outer_log = artifact_root / "player-outer.log"
    state_log = _state_log(game)
    state_start = state_log.stat().st_size if state_log.is_file() else 0
    token = secrets.token_hex(24)
    probes = list(args.component_probe)
    player_probes = _player_component_probes(probes)
    object_names = _probe_object_names(args.object, probes)
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
    if deterministic_capture:
        environment["_INFERNUX_PLAYER_FIXED_DELTA"] = format(args.fixed_delta, ".17g")
        environment["_INFERNUX_PLAYER_PAUSE_AFTER_FRAME"] = str(args.pause_after_frame)

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
        control = ControlClient(request, response, token, "Windows")
        deadline = time.monotonic() + args.startup_timeout
        observation: dict[str, Any] = {}
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            observation = control.call(
                "observe",
                {
                    "object_names": object_names,
                    "component_probes": player_probes,
                },
                timeout=remaining,
                process=process,
            )
            try:
                _position(observation, args.object)
                if _gameplay_control_ready(
                    observation, allow_paused=bool(args.capture_only)
                ):
                    break
            except RuntimeError:
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError(
                "Windows Player did not enter gameplay with object "
                f"'{args.object}' before startup timeout"
            )

        captured_runtime_frame = int(observation.get("runtime_frame_count", 0))
        if deterministic_capture:
            deadline = time.monotonic() + args.startup_timeout
            while time.monotonic() < deadline:
                observation = control.call(
                    "observe",
                    {
                        "object_names": object_names,
                        "component_probes": player_probes,
                    },
                    timeout=max(0.1, deadline - time.monotonic()),
                    process=process,
                )
                captured_runtime_frame = int(observation.get("runtime_frame_count", 0))
                if bool(observation.get("scene_manager_paused", False)):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(
                    "Windows Player did not reach the deterministic capture frame"
                )
            if captured_runtime_frame != args.pause_after_frame:
                raise RuntimeError(
                    "Windows Player paused at runtime frame "
                    f"{captured_runtime_frame}, expected {args.pause_after_frame}"
                )

        if args.click_at is not None:
            for pressed in (True, False):
                control.call(
                    "mouse_button",
                    {"button": 0, "pressed": pressed, "x": args.click_at[0], "y": args.click_at[1]},
                    timeout=args.startup_timeout,
                    process=process,
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

        if args.capture_only:
            initial = _position(observation, args.object)
            final = initial
            delta = 0.0
        else:
            press = control.call(
                "press",
                {
                    "scancode": args.press_scancode,
                    "duration_seconds": args.press_duration,
                    "object_names": object_names,
                    "component_probes": player_probes,
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
            _require_minimum_final_y(final, args.minimum_final_y)

        feature_deadline = time.monotonic() + args.startup_timeout
        assertion_results: list[dict[str, Any]] = []
        component_fields: dict[str, Any] = {}
        last_feature_error = "renderer submission is not ready"
        while time.monotonic() < feature_deadline:
            feature_observation = control.call(
                "observe",
                {
                    "object_names": object_names,
                    "component_probes": player_probes,
                },
                timeout=max(0.1, feature_deadline - time.monotonic()),
                process=process,
            )
            if not bool(feature_observation.get("submission_ready")):
                time.sleep(0.05)
                continue
            try:
                assertion_results, component_fields = _assert_component_probes(
                    feature_observation, probes
                )
                break
            except RuntimeError as error:
                last_feature_error = str(error)
                time.sleep(0.05)
        else:
            raise RuntimeError(
                "Windows Player feature readiness timed out: " + last_feature_error
            )

        shutdown_started = time.monotonic()
        control.call("shutdown", timeout=10.0, process=process)
        process.wait(timeout=10.0)
        shutdown_elapsed = time.monotonic() - shutdown_started
        if shutdown_elapsed > args.maximum_shutdown_seconds:
            raise RuntimeError(
                "Windows Player shutdown took "
                f"{shutdown_elapsed:.3f}s; maximum is "
                f"{args.maximum_shutdown_seconds:.3f}s"
            )
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
            shutdown_elapsed_seconds=shutdown_elapsed,
            capture_path=capture_path,
            capture_only=bool(args.capture_only),
            fixed_delta_seconds=float(args.fixed_delta),
            pause_after_frame=int(args.pause_after_frame),
            captured_runtime_frame=captured_runtime_frame,
            submission_ready=bool(feature_observation.get("submission_ready")),
            component_assertions=assertion_results,
            component_fields=component_fields,
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
