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
    runtime_frame_count: int
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


def _require_minimum_final_y(
    final: tuple[float, float, float], minimum: float | None
) -> None:
    if minimum is not None and final[1] < minimum:
        raise RuntimeError(
            f"Player object fell to y={final[1]:.6f}; minimum is {minimum:.6f}"
        )


def _gameplay_control_ready(
    observation: dict[str, Any], *, allow_paused: bool = False
) -> bool:
    return bool(observation.get("gameplay_ready")) and (
        allow_paused or not bool(observation.get("scene_manager_paused"))
    )


_COMPONENT_ASSERTION_OPERATORS = {
    "equal",
    "not_equal",
    "greater",
    "greater_or_equal",
    "less",
    "less_or_equal",
    "truthy",
    "falsy",
    "non_empty",
}


def _parse_component_probe(encoded: str) -> dict[str, Any]:
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(
            f"component probe must be a JSON object: {error.msg}"
        ) from error
    if not isinstance(raw, dict):
        raise argparse.ArgumentTypeError("component probe must be a JSON object")
    object_name = str(raw.get("object_name", "") or "").strip()
    component_type = str(raw.get("component_type", "") or "").strip()
    ordinal = int(raw.get("ordinal", 0) or 0)
    fields = [str(field or "").strip() for field in raw.get("fields", [])]
    assertions = raw.get("assertions", [])
    if not object_name or not component_type or ordinal < 0 or not fields:
        raise argparse.ArgumentTypeError(
            "component probe requires object_name, component_type, fields, and a non-negative ordinal"
        )
    if len(fields) > 16 or any(not field or field.startswith("_") for field in fields):
        raise argparse.ArgumentTypeError(
            "component probe fields must contain 1-16 public field names"
        )
    if not isinstance(assertions, list) or not assertions:
        raise argparse.ArgumentTypeError(
            "component probe requires at least one assertion"
        )
    normalized_assertions = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            raise argparse.ArgumentTypeError("component assertions must be JSON objects")
        field = str(assertion.get("field", "") or "").strip()
        operator = str(assertion.get("operator", "") or "").strip()
        if not field or field.split(".", 1)[0] not in fields:
            raise argparse.ArgumentTypeError(
                "component assertion fields must start with a probed public field"
            )
        if operator not in _COMPONENT_ASSERTION_OPERATORS:
            raise argparse.ArgumentTypeError(
                f"unsupported component assertion operator: {operator!r}"
            )
        if operator not in {"truthy", "falsy", "non_empty"} and "value" not in assertion:
            raise argparse.ArgumentTypeError(
                f"component assertion operator {operator!r} requires value"
            )
        normalized_assertions.append(
            {
                "field": field,
                "operator": operator,
                **({"value": assertion["value"]} if "value" in assertion else {}),
            }
        )
    return {
        "object_name": object_name,
        "component_type": component_type,
        "ordinal": ordinal,
        "fields": list(dict.fromkeys(fields)),
        "assertions": normalized_assertions,
    }


def _player_component_probes(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "object_name": probe["object_name"],
            "component_type": probe["component_type"],
            "ordinal": probe["ordinal"],
            "fields": probe["fields"],
        }
        for probe in probes
    ]


def _probe_object_names(primary: str, probes: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys([primary, *(probe["object_name"] for probe in probes)])
    )


def _nested_public_value(fields: dict[str, Any], path: str) -> Any:
    value: Any = fields
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            raise RuntimeError(f"component observation is missing public field {path!r}")
        value = value[segment]
    return value


def _numeric(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"component field {field!r} is not numeric: {value!r}")
    return float(value)


def _assert_component_probes(
    observation: dict[str, Any], probes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results = []
    observed_fields: dict[str, Any] = {}
    objects = observation.get("objects", {})
    for probe in probes:
        object_name = probe["object_name"]
        component_key = f"{probe['component_type']}[{probe['ordinal']}]"
        fields = (
            objects.get(object_name, {})
            .get("component_fields", {})
            .get(component_key)
        )
        if not isinstance(fields, dict):
            raise RuntimeError(
                f"Player did not publish {object_name}/{component_key} component fields"
            )
        observed_fields[f"{object_name}/{component_key}"] = fields
        for assertion in probe["assertions"]:
            field = assertion["field"]
            operator = assertion["operator"]
            actual = _nested_public_value(fields, field)
            expected = assertion.get("value")
            if operator == "equal":
                passed = actual == expected
            elif operator == "not_equal":
                passed = actual != expected
            elif operator == "truthy":
                passed = bool(actual)
            elif operator == "falsy":
                passed = not bool(actual)
            elif operator == "non_empty":
                passed = actual is not None and hasattr(actual, "__len__") and len(actual) > 0
            else:
                actual_number = _numeric(actual, field)
                expected_number = _numeric(expected, field)
                passed = {
                    "greater": actual_number > expected_number,
                    "greater_or_equal": actual_number >= expected_number,
                    "less": actual_number < expected_number,
                    "less_or_equal": actual_number <= expected_number,
                }[operator]
            result = {
                "object_name": object_name,
                "component": component_key,
                "field": field,
                "operator": operator,
                "actual": actual,
                **({"expected": expected} if "value" in assertion else {}),
            }
            if not passed:
                raise RuntimeError(f"component assertion failed: {result!r}")
            results.append(result)
    return results, observed_fields


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
    parser.add_argument("--minimum-final-y", type=float)
    parser.add_argument(
        "--component-probe",
        action="append",
        type=_parse_component_probe,
        default=[],
        help="Repeatable JSON component probe with public fields and assertions",
    )
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
                {
                    "object_names": object_names,
                    "component_probes": player_probes,
                },
                timeout=remaining,
                process=player_process,
            )
            try:
                initial = _position(observation, args.object)
                if _gameplay_control_ready(observation):
                    break
            except RuntimeError:
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError(
                "Linux Player did not enter gameplay with object "
                f"'{args.object}' before startup timeout"
            )

        press = control.call(
            "press",
            {
                "scancode": args.press_scancode,
                "duration_seconds": args.press_duration,
                "object_names": object_names,
                "component_probes": player_probes,
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
                process=player_process,
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
                "Linux Player feature readiness timed out: " + last_feature_error
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
            runtime_frame_count=int(feature_observation.get("runtime_frame_count", 0)),
            submission_ready=bool(feature_observation.get("submission_ready")),
            component_assertions=assertion_results,
            component_fields=component_fields,
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
        "schema": "infernux.linux_player_smoke",
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
