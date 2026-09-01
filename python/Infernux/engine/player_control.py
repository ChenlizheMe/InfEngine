"""Debug-only control channel for Supervisor-managed standalone Players."""

from __future__ import annotations

import hmac
import json
import math
import os
import tempfile
import time
import uuid
from typing import Any

from Infernux.engine.path_utils import is_path_within, resolved_path


_MAX_COMMAND_BYTES = 64 * 1024
_MAX_OBJECT_NAMES = 32
_MAX_COMPONENT_PROBES = 16
_MAX_DISCOVERY_COMPONENT_TYPES = 16
_MAX_DISCOVERED_OBJECTS = 64
_MAX_HELD_INPUTS = 8
_MAX_CAPTURE_FRAMES = 120_000
_MAX_STOP_ASSERTIONS = 16
_MOTION_CAPTURE_TERMINAL_STATES = {"completed", "condition_met", "cancelled", "failed"}
_STOP_ASSERTION_KINDS = frozenset({"scene_name", "transform_axis", "component_field"})
_COMPARISON_ALIASES = {
    "equals": "equals",
    "equal": "equals",
    "==": "equals",
    "not_equals": "not_equals",
    "not equal": "not_equals",
    "!=": "not_equals",
    "greater_than": "greater_than",
    ">": "greater_than",
    "greater_or_equal": "greater_or_equal",
    ">=": "greater_or_equal",
    "less_than": "less_than",
    "<": "less_than",
    "less_or_equal": "less_or_equal",
    "<=": "less_or_equal",
}


class PlayerControlChannel:
    """Poll a token-authenticated file channel from the Player main thread."""

    def __init__(
        self,
        request_path: str = "",
        response_path: str = "",
        token: str = "",
        artifact_root: str = "",
    ) -> None:
        self.request_path = resolved_path(request_path) if request_path else ""
        self.response_path = resolved_path(response_path) if response_path else ""
        self.artifact_root = resolved_path(artifact_root) if artifact_root else ""
        self._token = str(token or "")
        self._last_command_id = ""
        self._pending_input: dict[str, Any] | None = None
        self._pending_render_capture: dict[str, Any] | None = None
        self._motion_capture: dict[str, Any] | None = None

    @classmethod
    def from_environment(cls) -> "PlayerControlChannel":
        if os.environ.get("_INFERNUX_PLAYER_DEBUG_BUILD") != "1":
            return cls()
        request_path = os.environ.get("_INFERNUX_PLAYER_CONTROL_FILE", "").strip()
        response_path = os.environ.get("_INFERNUX_PLAYER_RESPONSE_FILE", "").strip()
        token = os.environ.get("_INFERNUX_PLAYER_CONTROL_TOKEN", "").strip()
        artifact_root = os.environ.get("_INFERNUX_PLAYER_ARTIFACT_ROOT", "").strip()
        if not request_path or not response_path or len(token) < 16:
            return cls()
        return cls(request_path, response_path, token, artifact_root)

    @property
    def enabled(self) -> bool:
        return bool(self.request_path and self.response_path and self._token)

    def poll(self, engine) -> str | None:
        """Process at most one command and return an engine-owned action."""
        if not self.enabled:
            return None
        native = engine.get_native_engine() if engine is not None else None
        if native is None:
            return None

        self._poll_motion_capture(engine)

        if self._pending_render_capture is not None:
            pending = self._pending_render_capture
            snapshot = dict(native.query_capture(int(pending["capture_id"])))
            status = str(snapshot.get("status", "") or "")
            if status not in {"completed", "failed", "cancelled", "source_expired"} and time.monotonic() >= float(
                pending["deadline"]
            ):
                native.cancel_capture(int(pending["capture_id"]))
                status = "cancelled"
                snapshot.update({"status": status, "error": "Player render-target capture timed out"})
            if status in {"completed", "failed", "cancelled", "source_expired"}:
                snapshot["terminal"] = True
                snapshot["pixel_origin"] = "engine_render_target"
                snapshot["os_capture_fallback"] = False
                self._write_response(
                    str(pending["command_id"]),
                    status == "completed",
                    snapshot,
                    error=str(snapshot.get("error", "") or "capture failed"),
                )
                self._pending_render_capture = None
            return None

        if self._pending_input is not None:
            pending = self._pending_input
            sequence = int(pending["sequence"])
            if pending.get("kind") == "press":
                phase = str(pending.get("phase", "down"))
                if phase == "down":
                    if int(native.last_processed_synthetic_input_sequence) >= sequence:
                        started_at = time.monotonic()
                        pending["phase"] = "hold"
                        pending["hold_started_at"] = started_at
                        pending["release_at"] = started_at + float(pending["duration_seconds"])
                    return None
                if phase == "hold":
                    now = time.monotonic()
                    if now >= float(pending["release_at"]):
                        _sample_press_observation(engine, pending)
                        release_sequence = int(native.queue_synthetic_key_input(
                            int(pending["scancode"]),
                            False,
                            False,
                        ))
                        pending["phase"] = "up"
                        pending["sequence"] = release_sequence
                        pending["released_at"] = now
                    return None
            if int(native.last_processed_synthetic_input_sequence) >= sequence:
                command_id = str(pending["command_id"])
                from Infernux.input import Input
                from Infernux.lib import InputManager

                input_manager = InputManager.instance()
                if pending.get("kind") == "mouse_button":
                    button = int(pending["button"])
                    response = {
                        "sequence": sequence,
                        "delivered": True,
                        "button": button,
                        "pressed": bool(pending["pressed"]),
                        "x": float(input_manager.mouse_position_x),
                        "y": float(input_manager.mouse_position_y),
                        "game_focused": bool(Input.is_game_focused()),
                        "held": bool(input_manager.get_mouse_button(button)),
                        "down": bool(input_manager.get_mouse_button_down(button)),
                        "up": bool(input_manager.get_mouse_button_up(button)),
                        "pending_input_count": int(native.pending_synthetic_input_count),
                    }
                else:
                    scancode = int(pending["scancode"])
                    response = {
                        "sequence": sequence,
                        "delivered": True,
                        "scancode": scancode,
                        "game_focused": bool(Input.is_game_focused()),
                        "held": bool(input_manager.get_key(scancode)),
                        "down": bool(input_manager.get_key_down(scancode)),
                        "up": bool(input_manager.get_key_up(scancode)),
                        "pending_input_count": int(native.pending_synthetic_input_count),
                    }
                if pending.get("kind") == "press":
                    response.update({
                        "down_sequence": int(pending["down_sequence"]),
                        "requested_duration_seconds": float(pending["duration_seconds"]),
                        "actual_duration_seconds": max(
                            0.0,
                            float(pending["released_at"]) - float(pending["hold_started_at"]),
                        ),
                    })
                    if pending.get("object_names"):
                        _sample_press_observation(engine, pending)
                        response.update({
                            "initial_observation": pending["initial_observation"],
                            "final_observation": pending["final_observation"],
                            "last_same_scene_observation": pending["last_same_scene_observation"],
                        })
                self._write_response(command_id, True, response)
                self._pending_input = None
            return None

        command = self._read_command()
        if command is None:
            return None
        command_id = str(command.get("command_id", "") or "").strip()
        if not command_id or command_id == self._last_command_id:
            return None
        self._last_command_id = command_id
        self._remove_request()

        supplied_token = str(command.get("token", "") or "")
        if not hmac.compare_digest(supplied_token, self._token):
            self._write_response(command_id, False, error="control token mismatch")
            return None

        action = str(command.get("action", "") or "").strip().lower()
        try:
            if action == "shutdown":
                self._write_response(command_id, True, {"close_requested": True})
                _append_player_log("validation: normal shutdown requested")
                return "shutdown"
            if action == "key":
                scancode = int(command.get("scancode", 0) or 0)
                if scancode <= 0:
                    raise ValueError("scancode must be positive")
                sequence = int(native.queue_synthetic_key_input(
                    scancode,
                    bool(command.get("pressed", False)),
                    bool(command.get("repeat", False)),
                ))
                self._pending_input = {
                    "command_id": command_id,
                    "sequence": sequence,
                    "scancode": scancode,
                }
                return None
            if action == "mouse_button":
                button = int(command.get("button", -1))
                if button < 0 or button > 4:
                    raise ValueError("button must use a Unity mouse button index from 0 through 4")
                x = _bounded_finite_float(command.get("x", 0.0), "x", minimum=0.0, maximum=100_000.0)
                y = _bounded_finite_float(command.get("y", 0.0), "y", minimum=0.0, maximum=100_000.0)
                pressed = bool(command.get("pressed", False))
                sequence = int(native.queue_synthetic_mouse_button_input(button, pressed, x, y))
                self._pending_input = {
                    "kind": "mouse_button",
                    "command_id": command_id,
                    "sequence": sequence,
                    "button": button,
                    "pressed": pressed,
                }
                return None
            if action == "capture":
                if not self.artifact_root:
                    raise RuntimeError("Player capture artifact root is unavailable")
                file_name = os.path.basename(str(command.get("file_name", "") or "").strip())
                if not file_name or os.path.splitext(file_name)[1].lower() != ".png":
                    raise ValueError("file_name must be a plain .png basename")
                output_path = resolved_path(os.path.join(self.artifact_root, "review", file_name))
                if not is_path_within(output_path, self.artifact_root, allow_root=False):
                    raise ValueError("capture output must stay inside the session artifact root")
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                capture_id = int(native.request_capture("game", output_path))
                timeout_seconds = _bounded_finite_float(
                    command.get("timeout_seconds", 30.0),
                    "timeout_seconds",
                    minimum=0.5,
                    maximum=60.0,
                )
                self._pending_render_capture = {
                    "command_id": command_id,
                    "capture_id": capture_id,
                    "deadline": time.monotonic() + timeout_seconds,
                }
                return None
            if action == "press":
                scancode = int(command.get("scancode", 0) or 0)
                if scancode <= 0:
                    raise ValueError("scancode must be positive")
                duration_seconds = _bounded_finite_float(
                    command.get("duration_seconds", 0.0), "duration_seconds", minimum=0.02, maximum=10.0
                )
                object_names = _bounded_names(command.get("object_names", []))
                component_probes = _bounded_component_probes(command.get("component_probes", []), object_names)
                initial_observation = (
                    _observe_motion_state(engine, object_names, component_probes) if object_names else {}
                )
                sequence = int(native.queue_synthetic_key_input(scancode, True, False))
                self._pending_input = {
                    "kind": "press",
                    "phase": "down",
                    "command_id": command_id,
                    "sequence": sequence,
                    "down_sequence": sequence,
                    "scancode": scancode,
                    "duration_seconds": duration_seconds,
                    "object_names": object_names,
                    "component_probes": component_probes,
                    "initial_scene_name": initial_observation.get("scene_name", ""),
                    "initial_observation": initial_observation,
                    "final_observation": initial_observation,
                    "last_same_scene_observation": initial_observation,
                }
                return None
            if action == "motion_capture_arm":
                if self._motion_capture is not None and str(self._motion_capture.get("status")) not in (
                    _MOTION_CAPTURE_TERMINAL_STATES
                ):
                    raise RuntimeError("a Player motion capture is already armed or active")
                object_names = _bounded_names(command.get("object_names", []))
                if not object_names:
                    raise ValueError("object_names must contain at least one public object name")
                component_probes = _bounded_component_probes(command.get("component_probes", []), object_names)
                seconds = _bounded_finite_float(command.get("seconds", 2.0), "seconds", minimum=0.1, maximum=10.0)
                sample_interval = _bounded_finite_float(
                    command.get("sample_interval", 0.1), "sample_interval", minimum=0.02, maximum=1.0
                )
                trigger_timeout = _bounded_finite_float(
                    command.get("trigger_timeout", 60.0), "trigger_timeout", minimum=0.5, maximum=120.0
                )
                trigger_scene_name = str(command.get("trigger_scene_name", "") or "").strip()
                initial_scene_name = _active_scene_name()
                if trigger_scene_name and initial_scene_name.casefold() == trigger_scene_name.casefold():
                    raise ValueError("motion capture must be armed before the target scene becomes active")
                hold_scancodes = _bounded_scancodes(command.get("hold_scancodes", []))
                hold_mouse_buttons = _bounded_mouse_buttons(command.get("hold_mouse_buttons", []))
                mouse_x = _bounded_finite_float(
                    command.get("mouse_x", -10_000.0), "mouse_x", minimum=-100_000.0, maximum=100_000.0
                )
                mouse_y = _bounded_finite_float(
                    command.get("mouse_y", -10_000.0), "mouse_y", minimum=-100_000.0, maximum=100_000.0
                )
                frame_count = _bounded_positive_frame_count(command.get("frame_count"), "frame_count")
                hold_frame_count = _bounded_positive_frame_count(
                    command.get("hold_frame_count"), "hold_frame_count"
                )
                wait_frame_count = _bounded_wait_frame_count(command.get("wait_frame_count"))
                hold_frame_count, wait_frame_count, total_frame_count = _normalize_frame_plan(
                    frame_count,
                    hold_frame_count,
                    wait_frame_count,
                    bool(hold_scancodes or hold_mouse_buttons),
                )
                wait_seconds = _bounded_finite_float(
                    command.get("wait_seconds", 0.0), "wait_seconds", minimum=0.0, maximum=30.0
                )
                if wait_seconds and not hold_frame_count:
                    raise ValueError("wait_seconds requires hold_frame_count")
                stop_assertions = _bounded_stop_assertions(
                    command.get("stop_assertions", []), object_names, component_probes
                )
                stop_mode = _bounded_stop_mode(command.get("stop_mode", "all"))
                now = time.monotonic()
                self._motion_capture = {
                    "capture_id": f"player-motion-{uuid.uuid4().hex[:12]}",
                    "status": "armed",
                    "object_names": object_names,
                    "component_probes": component_probes,
                    "seconds": seconds,
                    "sample_interval": sample_interval,
                    "hold_scancodes": hold_scancodes,
                    "hold_mouse_buttons": hold_mouse_buttons,
                    "mouse_x": mouse_x,
                    "mouse_y": mouse_y,
                    "frame_count": total_frame_count,
                    "hold_frame_count": hold_frame_count,
                    "wait_frame_count": wait_frame_count,
                    "wait_seconds": wait_seconds,
                    "pause_on_complete": bool(command.get("pause_on_complete", False)),
                    "stop_assertions": stop_assertions,
                    "stop_mode": stop_mode,
                    "pause_on_condition": bool(command.get("pause_on_condition", True)),
                    "stop_condition": {},
                    "condition_met_at_frame": 0,
                    "condition_settle_until_frame": 0,
                    "condition_settle_until_time": 0.0,
                    "trigger_scene_name": trigger_scene_name,
                    "initial_scene_name": initial_scene_name,
                    "actual_scene_name": "",
                    "armed_at": now,
                    "trigger_deadline": now + trigger_timeout,
                    "started_at": 0.0,
                    "start_time_frame": 0,
                    "elapsed_frame_count": 0,
                    "frame_budget_completed_at": 0.0,
                    "frame_deadline": 0.0,
                    "next_sample_at": 0.0,
                    "trajectory": [],
                    "missing_object_names": list(object_names),
                    "input_presses": [],
                    "input_releases": [],
                    "input_released_after_hold_frame": 0,
                    "release_sequence": 0,
                    "paused_on_complete": False,
                    "error": "",
                }
                self._write_response(command_id, True, _public_motion_capture(self._motion_capture))
                return None
            if action == "motion_capture_status":
                capture = self._require_motion_capture(command.get("capture_id", ""))
                self._write_response(command_id, True, _public_motion_capture(capture))
                return None
            if action == "motion_capture_cancel":
                capture = self._require_motion_capture(command.get("capture_id", ""))
                cancelled = str(capture.get("status")) not in _MOTION_CAPTURE_TERMINAL_STATES
                if cancelled:
                    _release_motion_capture_input(native, capture)
                    capture["status"] = "cancelled"
                self._write_response(
                    command_id,
                    True,
                    {**_public_motion_capture(capture), "cancelled": cancelled},
                )
                return None
            if action == "observe":
                names = _bounded_names(command.get("object_names", []))
                component_probes = _bounded_component_probes(command.get("component_probes", []), names)
                discovery_component_types = _bounded_discovery_component_types(
                    command.get("discovery_component_types", [])
                )
                max_discovered_objects = _bounded_discovered_object_count(
                    command.get("max_discovered_objects", 32)
                )
                self._write_response(
                    command_id,
                    True,
                    _observe_player(
                        engine,
                        names,
                        component_probes,
                        include_scene_objects=bool(command.get("include_scene_objects", False)),
                        discovery_component_types=discovery_component_types,
                        max_discovered_objects=max_discovered_objects,
                    ),
                )
                return None
            raise ValueError(f"unsupported Player control action: {action or '<empty>'}")
        except Exception as exc:
            self._write_response(command_id, False, error=f"{type(exc).__name__}: {exc}")
            return None

    def _require_motion_capture(self, capture_id: Any) -> dict[str, Any]:
        identifier = str(capture_id or "").strip()
        capture = self._motion_capture
        if capture is None or str(capture.get("capture_id")) != identifier:
            raise LookupError(f"Player motion capture '{identifier}' was not found")
        return capture

    def _poll_motion_capture(self, engine) -> None:
        capture = self._motion_capture
        if capture is None or str(capture.get("status")) in _MOTION_CAPTURE_TERMINAL_STATES:
            return
        try:
            native = engine.get_native_engine()
            if native is None:
                raise RuntimeError("Player native engine is unavailable")
            now = time.monotonic()
            if str(capture.get("status")) == "armed":
                if now >= float(capture["trigger_deadline"]):
                    capture["status"] = "failed"
                    missing = ", ".join(capture.get("missing_object_names") or [])
                    capture["error"] = f"timed out waiting for target scene and objects: {missing}"
                    return
                scene_name = _active_scene_name()
                target_name = str(capture.get("trigger_scene_name") or "")
                initial_name = str(capture.get("initial_scene_name") or "")
                scene_matches = (
                    scene_name.casefold() == target_name.casefold()
                    if target_name
                    else bool(scene_name and scene_name.casefold() != initial_name.casefold())
                )
                if not scene_matches:
                    return
                first_sample = _observe_motion_state(
                    engine,
                    list(capture["object_names"]),
                    list(capture["component_probes"]),
                )
                missing = [
                    name for name in capture["object_names"] if name not in first_sample.get("objects", {})
                ]
                capture["missing_object_names"] = missing
                if missing:
                    return
                capture["status"] = "active"
                capture["actual_scene_name"] = scene_name
                capture["started_at"] = now
                capture["next_sample_at"] = now + float(capture["sample_interval"])
                capture["start_time_frame"] = _time_frame_count()
                capture["elapsed_frame_count"] = 0
                total_frames = int(capture.get("frame_count", 0) or 0)
                if total_frames:
                    capture["frame_deadline"] = now + max(
                        10.0,
                        float(capture["seconds"]) + float(capture.get("wait_seconds", 0.0)) + 2.0,
                        float(total_frames) / 5.0,
                    )
                _press_motion_capture_input(native, capture)
                capture["trajectory"].append({"time": 0.0, **first_sample})
                return

            started_at = float(capture["started_at"])
            elapsed = max(0.0, now - started_at)
            duration = float(capture["seconds"])
            frame_count = int(capture.get("frame_count", 0) or 0)
            if frame_count:
                elapsed_frames = max(0, _time_frame_count() - int(capture["start_time_frame"]))
                capture["elapsed_frame_count"] = elapsed_frames
                hold_frames = int(capture.get("hold_frame_count", 0) or 0)
                if hold_frames and elapsed_frames >= hold_frames:
                    _release_motion_capture_input(native, capture, hold_frame_count=hold_frames)
                if now >= float(capture.get("frame_deadline", 0.0) or 0.0):
                    raise TimeoutError("frame-bounded Player action did not complete before its safety timeout")

            if str(capture.get("status")) == "condition_settling":
                if int(native.last_processed_synthetic_input_sequence) < int(capture.get("release_sequence", 0)):
                    return
                if frame_count and int(capture.get("elapsed_frame_count", 0) or 0) < int(
                    capture.get("condition_settle_until_frame", 0) or 0
                ):
                    return
                if now < float(capture.get("condition_settle_until_time", 0.0) or 0.0):
                    return
                if bool(capture.get("pause_on_condition")):
                    capture["paused_on_complete"] = _pause_player_scene()
                capture["status"] = "condition_met"
                return

            complete_by_frame = frame_count and int(capture["elapsed_frame_count"]) >= frame_count
            should_sample = now >= float(capture["next_sample_at"]) or bool(complete_by_frame)
            if should_sample:
                sample = _observe_motion_state(
                    engine,
                    list(capture["object_names"]),
                    list(capture["component_probes"]),
                )
                capture["trajectory"].append({"time": min(elapsed, duration), **sample})
                capture["next_sample_at"] = now + float(capture["sample_interval"])
                if str(capture.get("status")) == "active" and capture.get("stop_assertions"):
                    condition = _evaluate_stop_assertions(
                        sample,
                        list(capture["stop_assertions"]),
                        str(capture.get("stop_mode") or "all"),
                    )
                    capture["stop_condition"] = condition
                    if condition["passed"]:
                        elapsed_frames = int(capture.get("elapsed_frame_count", 0) or 0)
                        _release_motion_capture_input(
                            native,
                            capture,
                            hold_frame_count=elapsed_frames,
                        )
                        capture["condition_met_at_frame"] = elapsed_frames
                        capture["condition_settle_until_frame"] = elapsed_frames + int(
                            capture.get("wait_frame_count", 0) or 0
                        )
                        capture["condition_settle_until_time"] = now + float(capture.get("wait_seconds", 0.0) or 0.0)
                        capture["status"] = "condition_settling"

            if frame_count:
                if not complete_by_frame:
                    return
                if int(native.last_processed_synthetic_input_sequence) < int(capture.get("release_sequence", 0)):
                    return
                completed_at = float(capture.get("frame_budget_completed_at", 0.0) or 0.0)
                if not completed_at:
                    capture["frame_budget_completed_at"] = now
                    completed_at = now
                if now - completed_at < float(capture.get("wait_seconds", 0.0)):
                    return
                if bool(capture.get("pause_on_complete")):
                    capture["paused_on_complete"] = _pause_player_scene()
                capture["status"] = "completed"
            elif elapsed >= duration:
                capture["status"] = "completed"
        except Exception as exc:
            native = engine.get_native_engine() if engine is not None else None
            if native is not None:
                _release_motion_capture_input(native, capture)
            capture["status"] = "failed"
            capture["error"] = f"{type(exc).__name__}: {exc}"

    def _read_command(self) -> dict[str, Any] | None:
        try:
            if not os.path.isfile(self.request_path):
                return None
            if os.path.getsize(self.request_path) > _MAX_COMMAND_BYTES:
                self._remove_request()
                return None
            with open(self.request_path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if isinstance(value, dict):
                return value
            self._remove_request()
            return None
        except json.JSONDecodeError:
            self._remove_request()
            return None
        except OSError:
            return None

    def _remove_request(self) -> None:
        try:
            os.remove(self.request_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _write_response(
        self,
        command_id: str,
        ok: bool,
        data: dict[str, Any] | None = None,
        *,
        error: str = "",
    ) -> None:
        payload = {
            "command_id": command_id,
            "ok": bool(ok),
            "data": data or {},
            "error": str(error or ""),
        }
        _write_json_atomic(self.response_path, payload)


def _bounded_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("object_names must be a list")
    names = [str(item or "").strip() for item in value if str(item or "").strip()]
    if len(names) > _MAX_OBJECT_NAMES:
        raise ValueError(f"object_names cannot contain more than {_MAX_OBJECT_NAMES} entries")
    return names


def _bounded_finite_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def _bounded_scancodes(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("hold_scancodes must be a list")
    if len(value) > _MAX_HELD_INPUTS:
        raise ValueError(f"hold_scancodes cannot contain more than {_MAX_HELD_INPUTS} entries")
    scancodes = []
    for raw in value:
        if isinstance(raw, bool):
            raise ValueError("hold_scancodes entries must be positive integers")
        scancode = int(raw)
        if scancode <= 0:
            raise ValueError("hold_scancodes entries must be positive integers")
        scancodes.append(scancode)
    if len(set(scancodes)) != len(scancodes):
        raise ValueError("hold_scancodes must not contain duplicates")
    return scancodes


def _bounded_positive_frame_count(value: Any, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    count = int(value)
    if count < 1 or count > _MAX_CAPTURE_FRAMES:
        raise ValueError(f"{field} must be between 1 and {_MAX_CAPTURE_FRAMES}")
    return count


def _bounded_wait_frame_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("wait_frame_count must be an integer")
    count = int(value)
    if count < 0 or count > _MAX_CAPTURE_FRAMES:
        raise ValueError(f"wait_frame_count must be between 0 and {_MAX_CAPTURE_FRAMES}")
    return count


def _normalize_frame_plan(
    frame_count: int,
    hold_frame_count: int,
    wait_frame_count: int,
    has_held_input: bool,
) -> tuple[int, int, int]:
    if wait_frame_count and not hold_frame_count:
        raise ValueError("wait_frame_count requires hold_frame_count")
    if hold_frame_count and not has_held_input:
        raise ValueError("hold_frame_count requires hold_key, hold_keys, or hold_mouse_buttons")
    if frame_count and wait_frame_count:
        raise ValueError("Use frame_count as the total budget, or use hold_frame_count with wait_frame_count")
    if frame_count:
        if hold_frame_count > frame_count:
            raise ValueError("hold_frame_count must not exceed frame_count")
        if has_held_input and not hold_frame_count:
            hold_frame_count = frame_count
        return hold_frame_count, frame_count - hold_frame_count, frame_count
    if hold_frame_count:
        total = hold_frame_count + wait_frame_count
        if total > _MAX_CAPTURE_FRAMES:
            raise ValueError(f"hold_frame_count plus wait_frame_count must not exceed {_MAX_CAPTURE_FRAMES}")
        return hold_frame_count, wait_frame_count, total
    if has_held_input:
        raise ValueError("held keyboard or mouse input requires frame_count or hold_frame_count")
    return 0, 0, 0


def _bounded_mouse_buttons(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("hold_mouse_buttons must be a list")
    if len(value) > 5:
        raise ValueError("hold_mouse_buttons cannot contain more than 5 entries")
    buttons: list[int] = []
    for raw in value:
        if isinstance(raw, bool) or int(raw) not in range(5):
            raise ValueError("hold_mouse_buttons must use Unity button indices 0 through 4")
        buttons.append(int(raw))
    if len(set(buttons)) != len(buttons):
        raise ValueError("hold_mouse_buttons must not contain duplicates")
    return buttons


def _bounded_stop_assertions(
    value: Any,
    object_names: list[str],
    component_probes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("stop_assertions must be a list")
    if len(value) > _MAX_STOP_ASSERTIONS:
        raise ValueError(f"stop_assertions cannot contain more than {_MAX_STOP_ASSERTIONS} items")
    probes = {
        (probe["object_name"], probe["component_type"], int(probe["ordinal"])): set(probe["fields"])
        for probe in component_probes
    }
    normalized = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("stop_assertions entries must be objects")
        item = dict(raw)
        kind = str(item.get("kind", "") or "").strip()
        if kind not in _STOP_ASSERTION_KINDS:
            supported = ", ".join(sorted(_STOP_ASSERTION_KINDS))
            raise ValueError(f"Player stop assertions support only: {supported}")
        _validate_comparison(item)
        if kind == "scene_name":
            normalized.append(item)
            continue
        object_name = str(item.get("object_name", "") or "").strip()
        if object_name not in object_names:
            raise ValueError("stop assertion object_name must also be present in object_names")
        item["object_name"] = object_name
        axis = str(item.get("axis", "") or "").lower()
        if kind == "transform_axis":
            if str(item.get("field", "position") or "position") != "position" or axis not in {"x", "y", "z"}:
                raise ValueError("transform_axis requires field 'position' and axis x, y, or z")
        else:
            component_type = str(item.get("component_type", "") or "").strip()
            field = str(item.get("field", "") or "").strip()
            ordinal = int(item.get("ordinal", 0) or 0)
            if not component_type or not field or field.startswith("_") or ordinal < 0:
                raise ValueError("component_field requires a public component_type, field, and ordinal")
            probe_fields = probes.get((object_name, component_type, ordinal))
            if probe_fields is None or field not in probe_fields:
                raise ValueError("component_field stop assertions require the same field in component_probes")
            item.update({"component_type": component_type, "field": field, "ordinal": ordinal})
        normalized.append(item)
    return normalized


def _bounded_stop_mode(value: Any) -> str:
    mode = str(value or "all").strip().lower()
    if mode not in {"all", "any"}:
        raise ValueError("stop_mode must be 'all' or 'any'")
    return mode


def _validate_comparison(item: dict[str, Any]) -> None:
    operator = str(item.get("operator", item.get("op", "equals")) or "equals").lower()
    if operator not in _COMPARISON_ALIASES:
        raise ValueError(f"unknown comparison operator: {operator!r}")
    if "value" not in item and "equals" not in item:
        raise ValueError("stop assertions require value or equals")
    tolerance = item.get("tolerance", 0.0)
    if isinstance(tolerance, bool) or float(tolerance) < 0.0:
        raise ValueError("stop assertion tolerance must be non-negative")


def _evaluate_stop_assertions(sample: dict[str, Any], assertions: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    results = [_evaluate_stop_assertion(sample, item) for item in assertions]
    passed = any(result["passed"] for result in results) if mode == "any" else all(result["passed"] for result in results)
    return {"passed": passed, "mode": mode, "sample_renderer_frame": sample.get("renderer_frame", 0), "results": results}


def _evaluate_stop_assertion(sample: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind", "") or "")
    actual: Any = None
    label = kind
    if kind == "scene_name":
        actual = sample.get("scene_name", "")
    else:
        object_name = str(item.get("object_name", "") or "")
        obj = dict(sample.get("objects", {}) or {}).get(object_name)
        if not isinstance(obj, dict):
            return _stop_assertion_result(item, False, f"object {object_name!r} was not found")
        if kind == "transform_axis":
            axis = {"x": 0, "y": 1, "z": 2}[str(item.get("axis", "") or "").lower()]
            position = obj.get("position", [])
            if not isinstance(position, list) or len(position) != 3:
                return _stop_assertion_result(item, False, f"object {object_name!r} has no position sample")
            actual = position[axis]
            label = f"transform.position.{item.get('axis')}"
        elif kind == "component_field":
            component_key = f"{item['component_type']}[{int(item.get('ordinal', 0) or 0)}]"
            fields = dict(obj.get("component_fields", {}) or {}).get(component_key)
            if not isinstance(fields, dict) or item["field"] not in fields:
                return _stop_assertion_result(item, False, f"component field {component_key}.{item['field']} was not sampled")
            actual = fields[item["field"]]
            label = f"component.{component_key}.{item['field']}"
    passed, operator, expected, detail = _compare_stop_value(actual, item)
    return _stop_assertion_result(item, passed, f"{label} is {actual!r}; {detail}", actual=actual, expected=expected, operator=operator)


def _compare_stop_value(actual: Any, item: dict[str, Any]) -> tuple[bool, str, Any, str]:
    raw_operator = str(item.get("operator", item.get("op", "equals")) or "equals").lower()
    operator = _COMPARISON_ALIASES[raw_operator]
    expected = item["value"] if "value" in item else item["equals"]
    numeric = _is_number(actual) and _is_number(expected)
    if operator in {"equals", "not_equals"}:
        equals = abs(float(actual) - float(expected)) <= float(item.get("tolerance", 0.0) or 0.0) if numeric else actual == expected
        return (equals if operator == "equals" else not equals), operator, expected, f"expected {operator} {expected!r}"
    if not numeric:
        return False, operator, expected, f"operator {operator!r} requires numeric actual and expected values"
    comparisons = {
        "greater_than": float(actual) > float(expected),
        "greater_or_equal": float(actual) >= float(expected),
        "less_than": float(actual) < float(expected),
        "less_or_equal": float(actual) <= float(expected),
    }
    return comparisons[operator], operator, expected, f"expected {operator} {expected!r}"


def _stop_assertion_result(
    assertion: dict[str, Any], passed: bool, message: str, *, actual: Any = None, expected: Any = None, operator: str = ""
) -> dict[str, Any]:
    result = {"assertion": assertion, "passed": bool(passed), "message": message}
    if actual is not None:
        result["actual"] = actual
    if expected is not None:
        result["expected"] = expected
    if operator:
        result["operator"] = operator
    return result


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _time_frame_count() -> int:
    from Infernux.timing import Time

    return int(Time.frame_count)


def _press_motion_capture_input(native, capture: dict[str, Any]) -> None:
    if capture.get("input_presses"):
        return
    presses = []
    for scancode in list(capture.get("hold_scancodes") or []):
        sequence = int(native.queue_synthetic_key_input(int(scancode), True, False))
        presses.append({
            "scancode": int(scancode),
            "pressed": True,
            "sequence": sequence,
            "queued_at_capture_frame": 0,
        })
    for button in list(capture.get("hold_mouse_buttons") or []):
        sequence = int(native.queue_synthetic_mouse_button_input(
            int(button),
            True,
            float(capture.get("mouse_x", -10_000.0)),
            float(capture.get("mouse_y", -10_000.0)),
        ))
        presses.append({
            "button": int(button),
            "pressed": True,
            "sequence": sequence,
            "queued_at_capture_frame": 0,
        })
    capture["input_presses"] = presses


def _release_motion_capture_input(native, capture: dict[str, Any], *, hold_frame_count: int = 0) -> None:
    if capture.get("input_releases"):
        return
    releases = []
    for button in reversed(list(capture.get("hold_mouse_buttons") or [])):
        sequence = int(native.queue_synthetic_mouse_button_input(
            int(button),
            False,
            float(capture.get("mouse_x", -10_000.0)),
            float(capture.get("mouse_y", -10_000.0)),
        ))
        releases.append({
            "button": int(button),
            "pressed": False,
            "sequence": sequence,
            "queued_at_hold_frame": int(hold_frame_count),
        })
    for scancode in reversed(list(capture.get("hold_scancodes") or [])):
        sequence = int(native.queue_synthetic_key_input(int(scancode), False, False))
        releases.append({
            "scancode": int(scancode),
            "pressed": False,
            "sequence": sequence,
            "queued_at_hold_frame": int(hold_frame_count),
        })
    if releases:
        capture["input_releases"] = releases
        capture["release_sequence"] = max(int(item["sequence"]) for item in releases)
        capture["input_released_after_hold_frame"] = int(hold_frame_count)


def _pause_player_scene() -> bool:
    from Infernux.lib import SceneManager

    manager = SceneManager.instance()
    if not manager.is_playing():
        return False
    if manager.is_paused():
        return True
    manager.pause()
    return bool(manager.is_paused())


def _sample_press_observation(engine, pending: dict[str, Any]) -> None:
    names = list(pending.get("object_names") or [])
    if not names:
        return
    observation = _observe_motion_state(engine, names, list(pending.get("component_probes") or []))
    pending["final_observation"] = observation
    if observation.get("scene_name") == pending.get("initial_scene_name"):
        pending["last_same_scene_observation"] = observation


def _bounded_component_probes(value: Any, object_names: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("component_probes must be a list")
    if len(value) > _MAX_COMPONENT_PROBES:
        raise ValueError(f"component_probes cannot contain more than {_MAX_COMPONENT_PROBES} entries")
    allowed_names = set(object_names)
    probes = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("component_probes entries must be objects")
        object_name = str(raw.get("object_name", "") or "").strip()
        component_type = str(raw.get("component_type", "") or "").strip()
        fields = [str(field or "").strip() for field in raw.get("fields", [])]
        ordinal = int(raw.get("ordinal", 0) or 0)
        if object_name not in allowed_names:
            raise ValueError("component probe object_name must also be present in object_names")
        if not component_type or ordinal < 0 or not fields or len(fields) > 16:
            raise ValueError("component probes require a public component type, ordinal, and 1-16 fields")
        if any(not field or field.startswith("_") for field in fields):
            raise ValueError("component probe fields must be non-empty public field names")
        probes.append({
            "object_name": object_name,
            "component_type": component_type,
            "fields": fields,
            "ordinal": ordinal,
        })
    return probes


def _bounded_discovery_component_types(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("discovery_component_types must be a list")
    component_types = [str(item or "").strip() for item in value if str(item or "").strip()]
    if len(component_types) > _MAX_DISCOVERY_COMPONENT_TYPES:
        raise ValueError(
            f"discovery_component_types cannot contain more than {_MAX_DISCOVERY_COMPONENT_TYPES} entries"
        )
    if any(component_type.startswith("_") for component_type in component_types):
        raise ValueError("discovery_component_types must contain public component type names")
    return list(dict.fromkeys(component_types))


def _bounded_discovered_object_count(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("max_discovered_objects must be an integer")
    count = int(value)
    if count < 1 or count > _MAX_DISCOVERED_OBJECTS:
        raise ValueError(f"max_discovered_objects must be between 1 and {_MAX_DISCOVERED_OBJECTS}")
    return count


def _active_scene_name() -> str:
    from Infernux.lib import SceneManager

    scene = SceneManager.instance().get_active_scene()
    return str(getattr(scene, "name", "") or "")


def _public_motion_capture(capture: dict[str, Any]) -> dict[str, Any]:
    status = str(capture.get("status") or "")
    trajectory = list(capture.get("trajectory") or [])
    return {
        "capture_id": str(capture.get("capture_id") or ""),
        "status": status,
        "terminal": status in _MOTION_CAPTURE_TERMINAL_STATES,
        "object_names": list(capture.get("object_names") or []),
        "seconds": float(capture.get("seconds") or 0.0),
        "sample_interval": float(capture.get("sample_interval") or 0.0),
        "hold_scancodes": list(capture.get("hold_scancodes") or []),
        "hold_mouse_buttons": list(capture.get("hold_mouse_buttons") or []),
        "mouse_x": float(capture.get("mouse_x", -10_000.0)),
        "mouse_y": float(capture.get("mouse_y", -10_000.0)),
        "frame_count": int(capture.get("frame_count") or 0),
        "hold_frame_count": int(capture.get("hold_frame_count") or 0),
        "wait_frame_count": int(capture.get("wait_frame_count") or 0),
        "wait_seconds": float(capture.get("wait_seconds") or 0.0),
        "pause_on_complete": bool(capture.get("pause_on_complete")),
        "stop_assertions": list(capture.get("stop_assertions") or []),
        "stop_mode": str(capture.get("stop_mode") or "all"),
        "pause_on_condition": bool(capture.get("pause_on_condition")),
        "stop_condition": dict(capture.get("stop_condition") or {}),
        "condition_met_at_frame": int(capture.get("condition_met_at_frame") or 0),
        "condition_settle_until_frame": int(capture.get("condition_settle_until_frame") or 0),
        "condition_settle_until_time": float(capture.get("condition_settle_until_time") or 0.0),
        "start_time_frame": int(capture.get("start_time_frame") or 0),
        "elapsed_frame_count": int(capture.get("elapsed_frame_count") or 0),
        "trigger_scene_name": str(capture.get("trigger_scene_name") or ""),
        "initial_scene_name": str(capture.get("initial_scene_name") or ""),
        "actual_scene_name": str(capture.get("actual_scene_name") or ""),
        "missing_object_names": list(capture.get("missing_object_names") or []),
        "sample_count": len(trajectory),
        "trajectory": trajectory,
        "input_presses": list(capture.get("input_presses") or []),
        "input_releases": list(capture.get("input_releases") or []),
        "input_released_after_hold_frame": int(capture.get("input_released_after_hold_frame") or 0),
        "paused_on_complete": bool(capture.get("paused_on_complete")),
        "error": str(capture.get("error") or ""),
    }


def _observe_player(
    engine,
    object_names: list[str],
    component_probes: list[dict[str, Any]] | None = None,
    *,
    include_scene_objects: bool = False,
    discovery_component_types: list[str] | None = None,
    max_discovered_objects: int = 32,
) -> dict[str, Any]:
    from Infernux.input import Input
    from Infernux.components import InxComponent
    from Infernux.lib import SceneManager
    from Infernux.scene import GameObjectQuery

    native = engine.get_native_engine()
    native_scene_manager = SceneManager.instance()
    scene = native_scene_manager.get_active_scene()
    play_manager = engine.get_play_mode_manager()
    scene_playing = bool(scene is not None and scene.is_playing())
    manager_playing = bool(native_scene_manager.is_playing())
    manager_paused = bool(native_scene_manager.is_paused())
    runtime_frame_count = int(native_scene_manager.runtime_frame_count)
    if manager_paused:
        state = "paused"
    elif play_manager is not None:
        state = getattr(getattr(play_manager, "state", None), "name", "unknown")
    elif manager_playing:
        state = "playing"
    else:
        state = "stopped"
    objects: dict[str, Any] = {}
    for name in object_names:
        obj = GameObjectQuery.find(name)
        if obj is None:
            continue
        transform = obj.transform
        components = []
        for component in obj.get_py_components() or []:
            component_type = type(component)
            update_method = getattr(component_type, "update", None)
            update_globals = getattr(update_method, "__globals__", {})
            components.append({
                "type_name": component_type.__name__,
                "enabled": bool(getattr(component, "enabled", False)),
                "awake_called": bool(getattr(component, "_awake_called", False)),
                "started": bool(getattr(component, "_has_started", False)),
                "update_overridden": bool(update_method is not InxComponent.update),
                "update_module": str(getattr(update_method, "__module__", "") or ""),
                "update_input_is_canonical": update_globals.get("Input") is Input,
                "load_requested": getattr(component, "_load_requested", None),
                "destination_scene": getattr(component, "destination_scene", None),
                "trigger_key": getattr(component, "trigger_key", None),
                "broken_script": bool(getattr(component, "_is_broken", False)),
                "broken_error": str(getattr(component, "_broken_error", "") or ""),
            })
        objects[name] = {
            "id": int(obj.id),
            "position": _vec3(transform.position),
            "euler_angles": _vec3(transform.euler_angles),
            "active": bool(getattr(obj, "active", True)),
            "python_components": components,
        }
        component_fields = _observe_component_fields(obj, name, component_probes or [])
        if component_fields:
            objects[name]["component_fields"] = component_fields

    from Infernux.application import _renderer_state_from_native

    renderer_state = _renderer_state_from_native(native)
    result = {
        "scene_name": str(getattr(scene, "name", "") or ""),
        "scene_playing": scene_playing,
        "scene_manager_playing": manager_playing,
        "scene_manager_paused": manager_paused,
        "runtime_frame_count": runtime_frame_count,
        "gameplay_ready": bool(
            scene_playing and manager_playing and runtime_frame_count > 0
        ),
        "play_state": str(state).lower(),
        "objects": objects,
        "renderer_frame": renderer_state["frame"],
        "gpu_residency": renderer_state["gpu_residency"],
        "msaa": renderer_state["msaa"],
        "submission_ready": renderer_state["submission_ready"],
        "last_processed_input_sequence": int(native.last_processed_synthetic_input_sequence),
        "pending_input_count": int(native.pending_synthetic_input_count),
        "game_focused": bool(Input.is_game_focused()),
    }
    discovery_types = set(discovery_component_types or [])
    if include_scene_objects or discovery_types:
        discovered = []
        match_count = 0
        for obj in list(scene.get_all_objects() or []) if scene is not None else []:
            component_types = _public_component_type_names(obj)
            if discovery_types and not discovery_types.intersection(component_types):
                continue
            match_count += 1
            if len(discovered) >= max_discovered_objects:
                continue
            discovered.append({
                "id": int(obj.id),
                "name": str(getattr(obj, "name", "") or ""),
                "active": bool(getattr(obj, "active", True)),
                "component_types": component_types,
            })
        result.update({
            "scene_objects": discovered,
            "scene_object_match_count": match_count,
            "scene_objects_truncated": match_count > len(discovered),
        })
    return result


def _public_component_type_names(obj) -> list[str]:
    names = []
    seen = set()
    for getter in ("get_components", "get_py_components"):
        try:
            components = getattr(obj, getter)() or []
        except Exception:
            continue
        for component in components:
            type_name = str(getattr(component, "type_name", type(component).__name__) or "").strip()
            if not type_name or type_name.startswith("_") or type_name in seen:
                continue
            seen.add(type_name)
            names.append(type_name)
    return names


def _observe_motion_state(
    engine,
    object_names: list[str],
    component_probes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture a small Player-owned motion sample without a second MCP round trip."""
    from Infernux import Rigidbody
    from Infernux.lib import SceneManager
    from Infernux.scene import GameObjectQuery

    native = engine.get_native_engine()
    scene = SceneManager.instance().get_active_scene()
    objects: dict[str, Any] = {}
    for name in object_names:
        obj = GameObjectQuery.find(name)
        if obj is None:
            continue
        state = {"position": _vec3(obj.transform.position)}
        rigidbody = obj.get_component(Rigidbody)
        if rigidbody is not None:
            state["velocity"] = _vec3(rigidbody.velocity)
        component_fields = _observe_component_fields(obj, name, component_probes or [])
        if component_fields:
            state["component_fields"] = component_fields
        objects[name] = state
    frame = dict(native.renderer_frame_snapshot)
    return {
        "scene_name": str(getattr(scene, "name", "") or ""),
        "renderer_frame": int(frame.get("frame", 0) or 0),
        "objects": objects,
    }


def _vec3(value) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def _observe_component_fields(
    obj,
    object_name: str,
    component_probes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    observed = {}
    for probe in component_probes:
        if probe["object_name"] != object_name:
            continue
        component_type = probe["component_type"]
        ordinal = probe["ordinal"]
        component = _find_public_component(obj, component_type, ordinal)
        fields = {}
        if component is not None:
            for field in probe["fields"]:
                if hasattr(component, field):
                    fields[field] = _json_public_value(getattr(component, field))
        observed[f"{component_type}[{ordinal}]"] = fields
    return observed


def _find_public_component(obj, component_type: str, ordinal: int):
    matches = []
    seen = set()
    for getter in ("get_components", "get_py_components"):
        try:
            candidates = getattr(obj, getter)() or []
        except Exception:
            continue
        for component in candidates:
            type_name = getattr(component, "type_name", type(component).__name__)
            if type_name != component_type and type(component).__name__ != component_type:
                continue
            public_component = _public_builtin_wrapper(obj, component, type_name)
            component_id = getattr(public_component, "component_id", None)
            key = (type_name, component_id) if component_id is not None else id(public_component)
            if key in seen:
                continue
            seen.add(key)
            matches.append(public_component)
    return matches[ordinal] if 0 <= ordinal < len(matches) else None


def _public_builtin_wrapper(obj, component, type_name: str):
    try:
        from Infernux.components.builtin_component import BuiltinComponent

        if isinstance(component, BuiltinComponent):
            return component
        wrapper_cls = BuiltinComponent._builtin_registry.get(type_name)
        if wrapper_cls is not None:
            return wrapper_cls._get_or_create_wrapper(component, obj)
    except Exception:
        pass
    return component


def _json_public_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_public_value(item) for item in value[:64]]
    if isinstance(value, dict):
        return {str(key): _json_public_value(item) for key, item in list(value.items())[:64]}
    if all(hasattr(value, axis) for axis in ("x", "y")):
        axes = ("x", "y", "z", "w")
        return [float(getattr(value, axis)) for axis in axes if hasattr(value, axis)]
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(resolved_path(path))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".player-control-", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise


def _append_player_log(message: str) -> None:
    path = os.environ.get("_INFERNUX_PLAYER_LOG", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(str(message) + "\n")
    except OSError:
        pass
