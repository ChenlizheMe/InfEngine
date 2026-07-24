"""Runtime observation and validation MCP tools."""

from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from math import dist
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from Infernux.mcp.threading import MainThreadCommandQueue
from Infernux.mcp.tools.common import (
    fail,
    find_game_object,
    ok,
    register_tool_metadata,
    serialize_component,
    serialize_value,
)


_PLAY_STATE_ALIASES = {
    "edit": "edit",
    "editing": "edit",
    "edit mode": "edit",
    "play": "playing",
    "playing": "playing",
    "play mode": "playing",
    "pause": "paused",
    "paused": "paused",
    "pause mode": "paused",
}

_TRANSFORM_VECTOR_FIELDS = {
    "position": "position",
    "euler_angles": "euler_angles",
    "local_position": "local_position",
    "local_euler_angles": "local_euler_angles",
    "local_scale": "local_scale",
}

_COMPARISON_ALIASES = {
    "eq": "equals",
    "equals": "equals",
    "ne": "not_equals",
    "not_equals": "not_equals",
    "gt": "greater_than",
    "greater_than": "greater_than",
    "gte": "greater_or_equal",
    "greater_or_equal": "greater_or_equal",
    "lt": "less_than",
    "less_than": "less_than",
    "lte": "less_or_equal",
    "less_or_equal": "less_or_equal",
}

_MOTION_CAPTURE_TERMINAL_STATES = frozenset({
    "completed",
    "condition_met",
    "failed",
    "cancelled",
    "trigger_timeout",
    "frame_timeout",
})
_MOTION_CAPTURE_LOCK = threading.Lock()
_MOTION_CAPTURES: dict[str, dict[str, Any]] = {}
_MAX_RETAINED_MOTION_CAPTURES = 64


RuntimeAssertionKind = Literal[
    "play_state",
    "audio_paused",
    "object_exists",
    "component_exists",
    "no_errors",
    "scene_name",
    "scene_path",
    "transform_axis",
    "input_axis",
    "component_field",
]
RuntimeComparisonOperator = Literal[
    "equals",
    "not_equals",
    "greater_than",
    "greater_or_equal",
    "less_than",
    "less_or_equal",
]
MotionCaptureStopMode = Literal["all", "any"]


class RuntimeAssertion(BaseModel):
    """Public, observation-only assertion accepted by ``runtime_assert``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"kind": "play_state", "equals": "playing"},
                {"kind": "audio_paused", "equals": False},
                {"kind": "scene_name", "equals": "Results"},
                {"kind": "object_exists", "object_name": "PlayerCar"},
                {
                    "kind": "component_exists",
                    "object_name": "PlayerCar",
                    "component_type": "Rigidbody",
                },
                {
                    "kind": "transform_axis",
                    "object_name": "PlayerCar",
                    "field": "position",
                    "axis": "z",
                    "operator": "greater_or_equal",
                    "value": 10.0,
                },
                {
                    "kind": "component_field",
                    "object_name": "FinishPortal",
                    "component_type": "RaceScenePortal",
                    "field": "destination_scene",
                    "equals": "Results",
                },
                {"kind": "input_axis", "axis": "Vertical", "equals": 1.0},
                {"kind": "no_errors"},
            ]
        },
    )

    kind: RuntimeAssertionKind = Field(description="Assertion behavior to evaluate from public runtime state.")
    object_id: int | None = Field(
        default=None,
        description="Runtime GameObject ID. Use object_name instead when IDs may change after scene loads.",
    )
    object_name: str | None = Field(
        default=None,
        description="Public GameObject name resolved in the active scene.",
    )
    component_type: str | None = Field(
        default=None,
        description="Public native or Python component type name.",
    )
    ordinal: int = Field(default=0, ge=0, description="Zero-based component occurrence on the object.")
    field: str | None = Field(
        default=None,
        description=(
            "Public component field, or transform vector field: position, euler_angles, "
            "local_position, local_euler_angles, local_scale."
        ),
    )
    axis: str | None = Field(
        default=None,
        description="x/y/z for transform_axis, or a public input axis name for input_axis.",
    )
    operator: RuntimeComparisonOperator | None = Field(
        default=None,
        description="Comparison operator. Defaults to equals when omitted.",
    )
    value: Any = Field(default=None, description="Expected value used with operator.")
    equals: Any = Field(default=None, description="Shorthand expected value for an equals comparison.")
    tolerance: float = Field(default=0.0, ge=0.0, description="Absolute tolerance for numeric equality.")


class RuntimeComponentProbe(BaseModel):
    """Public component fields sampled inside an action-owned motion window."""

    model_config = ConfigDict(extra="forbid")

    object_name: str = Field(min_length=1, description="GameObject name present in object_names.")
    component_type: str = Field(min_length=1, description="Public native or Python component type name.")
    fields: list[str] = Field(min_length=1, max_length=16, description="Public component fields to sample.")
    ordinal: int = Field(default=0, ge=0, description="Zero-based component occurrence on the object.")


def register_runtime_tools(mcp) -> None:
    _register_metadata()

    @mcp.tool(name="runtime_wait")
    def runtime_wait(
        play_state: str = "",
        scene_name: str = "",
        deferred_idle: bool = True,
        timeout_seconds: float = 10.0,
        poll_interval: float = 0.1,
    ) -> dict:
        """Wait until public Play Mode, scene, and deferred-task conditions are met."""
        deadline = time.time() + max(float(timeout_seconds), 0.01)
        desired_state = _normalize_play_state(play_state)
        desired_scene = str(scene_name or "").strip()
        last_state: dict[str, Any] = {}
        while time.time() < deadline:
            last_state = _run_on_main("runtime.wait.state", _editor_state)
            state_ok = not desired_state or last_state.get("play_state") == desired_state
            scene_ok = (
                not desired_scene
                or str(last_state.get("scene_name") or "").casefold() == desired_scene.casefold()
            )
            idle_ok = not deferred_idle or _is_runtime_idle(last_state)
            if state_ok and scene_ok and idle_ok:
                return ok({
                    "ready": True,
                    "state": last_state,
                    "requested_scene_name": desired_scene,
                    "elapsed_seconds": max(0.0, timeout_seconds - (deadline - time.time())),
                })
            time.sleep(max(float(poll_interval), 0.01))
        return fail(
            "error.timeout",
            "Timed out waiting for runtime condition.",
            hint="Use editor_get_state, console_read, or runtime_read_errors to diagnose why the condition did not become true.",
            explain={"tool": "runtime_wait", "summary": "Wait for Play Mode, scene, and deferred-task conditions."},
        ) | {"data": {"ready": False, "state": last_state, "requested_scene_name": desired_scene}}

    @mcp.tool(name="runtime_run_for")
    def runtime_run_for(seconds: float = 1.0, stop_on_error: bool = True, poll_interval: float = 0.25) -> dict:
        """Let Play Mode run for a duration while polling for errors."""
        duration = max(float(seconds), 0.0)
        deadline = time.time() + duration
        samples = []
        errors: list[dict[str, Any]] = []
        baseline_errors = _all_runtime_errors(
            _run_on_main("runtime.run_for.baseline_errors", _read_errors)
        )
        while time.time() < deadline:
            time.sleep(max(float(poll_interval), 0.01))
            state = _run_on_main("runtime.run_for.state", _editor_state)
            samples.append(state)
            current_errors = _all_runtime_errors(
                _run_on_main("runtime.run_for.errors", _read_errors)
            )
            errors = _new_runtime_errors(current_errors, baseline_errors)
            if stop_on_error and errors:
                break
        return ok({
            "elapsed_seconds": duration,
            "stopped_on_error": bool(stop_on_error and errors),
            "samples": samples[-10:],
            "errors": errors,
        })

    @mcp.tool(name="runtime_measure_motion")
    def runtime_measure_motion(
        object_names: list[str],
        seconds: float = 0.25,
        hold_key: str | int | None = None,
        hold_keys: list[str | int] | None = None,
        sample_interval: float = 0.1,
        component_probes: list[RuntimeComponentProbe] | None = None,
    ) -> dict:
        """Sample an already-running scene inside one bounded input action.

        Do not use this after a separate Play request when startup motion matters:
        transport/agent latency advances Play between calls. Arm
        ``runtime_motion_capture_arm`` before the real Toolbar Play action instead.
        """
        names = [str(name or "").strip() for name in object_names or [] if str(name or "").strip()]
        if not names or len(names) > 16:
            raise ValueError("object_names must contain between 1 and 16 non-empty names")
        probes = [_component_probe_mapping(item) for item in component_probes or []]
        if len(probes) > 16:
            raise ValueError("component_probes may contain at most 16 probes")
        for probe in probes:
            if probe["object_name"] not in names:
                raise ValueError("component probe object_name must also be present in object_names")
            if any(not field or field.startswith("_") for field in probe["fields"]):
                raise ValueError("component probe fields must be non-empty public field names")
        duration = max(0.0, min(float(seconds), 10.0))
        interval = max(0.02, min(float(sample_interval), 1.0))
        held_keys = _normalize_hold_keys(hold_key, hold_keys)
        if duration > 0.0:
            interval = max(interval, duration / 120.0)
        before_state = _run_on_main("runtime_measure_motion.before_state", _editor_state)
        before = _run_on_main("runtime_measure_motion.before", lambda: _named_transform_snapshots(names, probes))
        trajectory = [{"elapsed_seconds": 0.0, "objects": before}]
        presses: list[dict[str, Any]] = []
        releases: list[dict[str, Any]] = []
        pressed_keys: list[str | int] = []
        automation_input_armed = False
        try:
            if held_keys:
                _run_on_main(
                    "runtime_measure_motion.begin_game_input",
                    _begin_automation_game_input,
                )
                automation_input_armed = True
                from Infernux.mcp.tools.input import perform_key_transition

                for index, key in enumerate(held_keys):
                    press = perform_key_transition(
                        key,
                        True,
                        trace_name=f"runtime_measure_motion.press.{index}",
                    )
                    presses.append(dict(press.get("data") or {}))
                    if not press.get("ok"):
                        return press
                    pressed_keys.append(key)
            elapsed = 0.0
            while elapsed + 1.0e-9 < duration:
                step = min(interval, duration - elapsed)
                time.sleep(step)
                elapsed += step
                snapshot = _run_on_main(
                    "runtime_measure_motion.sample",
                    lambda: _named_transform_snapshots(names, probes),
                )
                trajectory.append({"elapsed_seconds": elapsed, "objects": snapshot})
        finally:
            try:
                if pressed_keys:
                    from Infernux.mcp.tools.input import perform_key_transition

                    for index, key in enumerate(reversed(pressed_keys)):
                        release = perform_key_transition(
                            key,
                            False,
                            trace_name=f"runtime_measure_motion.release.{index}",
                        )
                        releases.append(dict(release.get("data") or {}))
            finally:
                if automation_input_armed:
                    _run_on_main(
                        "runtime_measure_motion.end_game_input",
                        _end_automation_game_input,
                    )
        after_state = _run_on_main("runtime_measure_motion.after_state", _editor_state)
        if duration <= 0.0:
            after = _run_on_main("runtime_measure_motion.after", lambda: _named_transform_snapshots(names, probes))
            trajectory.append({"elapsed_seconds": 0.0, "objects": after})
        measurements = [_summarize_motion(name, trajectory) for name in names]
        return ok({
            "seconds": duration,
            "sample_interval": interval,
            "sample_count": len(trajectory),
            "hold_key": hold_key,
            "hold_keys": held_keys,
            "scene_before": before_state.get("scene_name", ""),
            "scene_after": after_state.get("scene_name", ""),
            "scene_changed": before_state.get("scene_path", "") != after_state.get("scene_path", ""),
            "trajectory": trajectory,
            "measurements": measurements,
            "component_measurements": _summarize_component_probes(probes, trajectory),
            "input_press": presses[0] if presses else {},
            "input_release": releases[0] if releases else {},
            "input_presses": presses,
            "input_releases": releases,
        })

    @mcp.tool(name="runtime_motion_capture_arm")
    def runtime_motion_capture_arm(
        object_names: list[str],
        seconds: float = 2.0,
        sample_interval: float = 0.1,
        trigger_play_state: Literal["playing", "paused"] = "playing",
        trigger_timeout: float = 60.0,
        hold_key: str | int | None = None,
        hold_keys: list[str | int] | None = None,
        hold_mouse_buttons: list[int] | None = None,
        mouse_x: float = -10_000.0,
        mouse_y: float = -10_000.0,
        frame_count: int | None = None,
        hold_frame_count: int | None = None,
        wait_frame_count: int | None = None,
        wait_seconds: float = 0.0,
        pause_on_complete: bool = False,
        component_probes: list[RuntimeComponentProbe] | None = None,
        stop_assertions: list[RuntimeAssertion] | None = None,
        stop_mode: MotionCaptureStopMode = "all",
        pause_on_condition: bool = True,
    ) -> dict:
        """Arm startup sampling before a human-equivalent Play/Pause action.

        This is the required tool for the first seconds after entering Play. An
        Optional SDL keys or mouse buttons, a game-frame budget, public component probes, and a
        declarative stop condition are owned by the same bounded capture window.
        ``hold_frame_count`` plus ``wait_frame_count`` describes a two-stage
        action: hold keys for N game frames, then release them and let the game
        run for M more frames and/or ``wait_seconds``. The condition is evaluated
        only at the configured sample interval, so a slow remote agent cannot
        miss its own pause point and no per-frame RPC is introduced.
        """
        names, duration, interval = _motion_capture_arguments(object_names, seconds, sample_interval)
        probes = [_component_probe_mapping(item) for item in component_probes or []]
        if len(probes) > 16:
            raise ValueError("component_probes may contain at most 16 probes")
        for probe in probes:
            if probe["object_name"] not in names:
                raise ValueError("component probe object_name must also be present in object_names")
            if any(not field or field.startswith("_") for field in probe["fields"]):
                raise ValueError("component probe fields must be non-empty public field names")
        trigger_state = _normalize_play_state(trigger_play_state)
        if trigger_state not in {"playing", "paused"}:
            raise ValueError("trigger_play_state must be 'playing' or 'paused'")
        timeout = max(0.5, min(float(trigger_timeout), 120.0))
        held_keys = _normalize_hold_keys(hold_key, hold_keys)
        held_mouse_buttons = _normalize_hold_mouse_buttons(hold_mouse_buttons)
        pointer_x = _finite_capture_coordinate("mouse_x", mouse_x)
        pointer_y = _finite_capture_coordinate("mouse_y", mouse_y)
        frames = _normalize_frame_count(frame_count)
        hold_frames, wait_frames, total_frames = _normalize_motion_capture_frame_plan(
            frames,
            hold_frame_count,
            wait_frame_count,
            bool(held_keys or held_mouse_buttons),
        )
        post_release_wait = max(0.0, min(float(wait_seconds), 30.0))
        if post_release_wait and not hold_frames:
            raise ValueError("wait_seconds requires hold_frame_count")
        assertions, assertion_mode = _normalize_motion_capture_stop_assertions(
            stop_assertions,
            stop_mode,
        )
        armed_state = _run_on_main("runtime_motion_capture.arm_state", _editor_state)
        if str(armed_state.get("play_state") or "") == trigger_state:
            return fail(
                "error.invalid_state",
                f"Motion capture must be armed before Play state becomes {trigger_state!r}.",
                hint="Return to Edit mode, arm the capture, then use the real Toolbar control to enter Play.",
            ) | {"data": {"state": armed_state, "trigger_play_state": trigger_state}}
        armed_objects = _run_on_main(
            "runtime_motion_capture.arm_objects",
            lambda: _named_transform_snapshots(names, probes),
        )
        missing = [name for name in names if name not in armed_objects]
        if missing:
            return fail(
                "error.not_found",
                f"Motion capture objects were not found: {', '.join(missing)}.",
                hint="Open the expected scene and resolve public object names before arming capture.",
            ) | {"data": {"missing_object_names": missing}}

        capture_id = f"motion-{uuid.uuid4().hex[:12]}"
        record = {
            "capture_id": capture_id,
            "status": "armed",
            "object_names": names,
            "seconds": duration,
            "sample_interval": interval,
            "trigger_play_state": trigger_state,
            "trigger_timeout": timeout,
            "hold_key": hold_key,
            "hold_keys": held_keys,
            "hold_mouse_buttons": held_mouse_buttons,
            "mouse_x": pointer_x,
            "mouse_y": pointer_y,
            "frame_count": total_frames,
            "hold_frame_count": hold_frames,
            "wait_frame_count": wait_frames,
            "wait_seconds": post_release_wait,
            "pause_on_complete": bool(pause_on_complete),
            "component_probes": probes,
            "stop_assertions": assertions,
            "stop_mode": assertion_mode,
            "pause_on_condition": bool(pause_on_condition),
            "stop_condition": {},
            "created_at": time.time(),
            "armed_state": armed_state,
            "armed_objects": armed_objects,
            "trigger_state": {},
            "trajectory": [],
            "measurements": [],
            "component_measurements": [],
            "input_press": {},
            "input_release": {},
            "input_presses": [],
            "input_releases": [],
            "input_released_after_hold_frame": 0,
            "input_release_error": "",
            "error": "",
            "cancel_event": threading.Event(),
            "_trigger_event": threading.Event(),
            "_frame_complete_event": threading.Event(),
            "_hold_complete_event": threading.Event(),
            "_terminal_event": threading.Event(),
            "_held_input_released": False,
        }
        with _MOTION_CAPTURE_LOCK:
            _prune_motion_captures_locked()
            if len(_MOTION_CAPTURES) >= _MAX_RETAINED_MOTION_CAPTURES:
                return fail(
                    "error.queue_full",
                    "The motion capture registry is full of active captures.",
                    hint="Cancel unfinished captures or wait for them to reach a terminal state.",
                )
            _MOTION_CAPTURES[capture_id] = record
        try:
            _register_motion_capture_trigger(capture_id)
        except Exception as exc:
            with _MOTION_CAPTURE_LOCK:
                _MOTION_CAPTURES.pop(capture_id, None)
            return fail(
                "error.unavailable",
                f"Motion capture could not subscribe to Play state: {exc}",
                hint="Keep the Editor in Edit mode and retry after PlayModeManager initialization completes.",
            )
        _start_motion_capture_worker(capture_id)
        return ok(_public_motion_capture(capture_id))

    @mcp.tool(name="runtime_motion_capture_status")
    def runtime_motion_capture_status(capture_id: str, wait_seconds: float = 0.0) -> dict:
        """Read or briefly wait for an armed motion capture without changing Play state."""
        identifier = str(capture_id or "").strip()
        wait = max(0.0, min(float(wait_seconds), 30.0))
        with _MOTION_CAPTURE_LOCK:
            record = _MOTION_CAPTURES.get(identifier)
            completion_event = record.get("_terminal_event") if record is not None else None
        if record is None:
            return fail(
                "error.not_found",
                f"Motion capture '{identifier}' was not found.",
                hint="Use the capture_id returned by runtime_motion_capture_arm.",
            )
        snapshot = _public_motion_capture(identifier)
        if snapshot.get("terminal") or wait <= 0.0:
            return ok(snapshot)
        if isinstance(completion_event, threading.Event):
            completion_event.wait(wait)
        else:
            time.sleep(wait)
        return ok(_public_motion_capture(identifier))

    @mcp.tool(name="runtime_motion_capture_cancel")
    def runtime_motion_capture_cancel(capture_id: str, pause: bool = False) -> dict:
        """Cancel an armed or active capture, optionally pausing Play first."""
        identifier = str(capture_id or "").strip()
        with _MOTION_CAPTURE_LOCK:
            record = _MOTION_CAPTURES.get(identifier)
            if record is None:
                return fail("error.not_found", f"Motion capture '{identifier}' was not found.")
            if str(record.get("status") or "") in _MOTION_CAPTURE_TERMINAL_STATES:
                return ok({**_public_motion_capture_locked(record), "cancelled": False})
        if pause:
            _run_on_main("runtime_motion_capture.cancel_pause", _pause_active_play_mode)
        with _MOTION_CAPTURE_LOCK:
            record = _MOTION_CAPTURES.get(identifier)
            if record is not None:
                record["cancel_event"].set()
        return ok({**_public_motion_capture(identifier), "cancel_requested": True})

    @mcp.tool(name="runtime_input_state")
    def runtime_input_state(
        keys: list[str | int] | None = None,
        axes: list[str] | None = None,
        mouse_buttons: list[int] | None = None,
    ) -> dict:
        """Read Game View focus plus selected keyboard, axis, and mouse input probes."""
        requested_keys = _bounded_probe_items(keys, ("w", "a", "s", "d"), "keys")
        requested_axes = _bounded_probe_items(axes, ("Horizontal", "Vertical"), "axes")
        requested_mouse_buttons = _bounded_mouse_buttons(mouse_buttons)
        return ok(
            _run_on_main(
                "runtime_input_state",
                lambda: _read_input_state(requested_keys, requested_axes, requested_mouse_buttons),
            )
        )

    @mcp.tool(name="runtime_renderer_state")
    def runtime_renderer_state() -> dict:
        """Read current renderer submission and GPU residency telemetry."""
        return ok(_run_on_main("runtime_renderer_state", _renderer_state))

    @mcp.tool(name="runtime_ui_performance")
    def runtime_ui_performance() -> dict:
        """Read an engine-recorded rolling UI profile without active frame polling."""
        return ok(_run_on_main("runtime_ui_performance", _ui_performance_state))

    @mcp.tool(name="runtime_physics_state")
    def runtime_physics_state() -> dict:
        """Read physics-world population and the latest fixed-step profile."""
        return ok(_run_on_main("runtime_physics_state", _physics_state))

    @mcp.tool(name="runtime_physics_raycast")
    def runtime_physics_raycast(
        origin: list[float],
        direction: list[float],
        max_distance: float = 1000.0,
        layer_mask: int = 0xFFFFFFFB,
        query_triggers: bool = True,
        limit: int = 64,
    ) -> dict:
        """Run a bounded read-only raycast through the public Physics API."""
        arguments = _physics_query_arguments(
            origin=origin,
            direction=direction,
            max_distance=max_distance,
            layer_mask=layer_mask,
            limit=limit,
        )
        return ok(
            _run_on_main(
                "runtime_physics_raycast",
                lambda: _physics_raycast(**arguments, query_triggers=bool(query_triggers)),
            )
        )

    @mcp.tool(name="runtime_physics_overlap_box")
    def runtime_physics_overlap_box(
        center: list[float],
        half_extents: list[float],
        layer_mask: int = 0xFFFFFFFB,
        query_triggers: bool = True,
        limit: int = 64,
    ) -> dict:
        """Query colliders in an axis-aligned box without mutating the scene."""
        query_center = _physics_vector(center, "center")
        query_half_extents = _physics_vector(half_extents, "half_extents", positive=True)
        query_layer_mask = _physics_layer_mask(layer_mask)
        query_limit = max(1, min(int(limit), 128))
        return ok(
            _run_on_main(
                "runtime_physics_overlap_box",
                lambda: _physics_overlap_box(
                    center=query_center,
                    half_extents=query_half_extents,
                    layer_mask=query_layer_mask,
                    query_triggers=bool(query_triggers),
                    limit=query_limit,
                ),
            )
        )

    @mcp.tool(name="runtime_get_object_state")
    def runtime_get_object_state(object_id: int) -> dict:
        """Read runtime GameObject transform and component summary."""

        def _read():
            obj = find_game_object(object_id)
            trans = obj.transform
            return {
                "id": int(obj.id),
                "name": str(obj.name),
                "active": bool(getattr(obj, "active", True)),
                **_object_prefab_linkage(obj),
                "transform": {
                    "position": _vec(trans.position),
                    "euler_angles": _vec(trans.euler_angles),
                    "local_position": _vec(trans.local_position),
                    "local_euler_angles": _vec(trans.local_euler_angles),
                    "local_scale": _vec(trans.local_scale),
                },
                "components": _components(obj),
                "parent_id": int(getattr(obj.get_parent(), "id", 0) or 0),
                "child_count": int(obj.get_child_count()),
            }

        try:
            return ok(_run_on_main("runtime_get_object_state", _read))
        except FileNotFoundError as exc:
            return fail(
                "error.not_found",
                str(exc),
                hint=(
                    "The active scene may have changed. Use mcp_health or runtime_assert "
                    "with a scene_name check before resolving IDs from the new scene."
                ),
            ) | {"data": {"object_id": int(object_id), "object_exists": False}}

    @mcp.tool(name="runtime_find_objects")
    def runtime_find_objects(name: str = "", tag: str = "", limit: int = 50) -> dict:
        """Resolve runtime GameObjects by public name/tag and return stable IDs."""
        bounded_limit = max(1, min(int(limit), 200))
        return ok(
            _run_on_main(
                "runtime_find_objects",
                lambda: {"objects": _find_runtime_objects(name=str(name or ""), tag=str(tag or ""), limit=bounded_limit)},
            )
        )

    @mcp.tool(name="runtime_get_component_state")
    def runtime_get_component_state(object_id: int, component_type: str, ordinal: int = 0) -> dict:
        """Read a component snapshot at runtime."""

        def _read():
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            data = serialize_component(comp)
            fields = {}
            try:
                from Infernux.components.serialized_field import get_serialized_fields
                for name in get_serialized_fields(type(comp)):
                    try:
                        fields[name] = serialize_value(getattr(comp, name))
                    except Exception:
                        pass
            except Exception:
                pass
            if data.get("broken_script"):
                serialize_fields = getattr(comp, "_serialize_fields_document", None)
                if callable(serialize_fields):
                    try:
                        preserved = serialize_fields()
                    except Exception:
                        preserved = None
                    if isinstance(preserved, dict):
                        fields.update({
                            str(name): serialize_value(value)
                            for name, value in preserved.items()
                            if not str(name).startswith("__")
                        })
            return {"object_id": int(obj.id), "component": data, "fields": fields}

        try:
            return ok(_run_on_main("runtime_get_component_state", _read))
        except FileNotFoundError as exc:
            return fail(
                "error.not_found",
                str(exc),
                hint="Use runtime_get_object_state or mcp_health before reading a component from the active scene.",
            ) | {"data": {"object_id": int(object_id), "component_exists": False}}

    @mcp.tool(name="runtime_read_errors")
    def runtime_read_errors(include_warnings: bool = False, limit: int = 100) -> dict:
        """Read console errors and script loader errors."""
        return ok(_run_on_main("runtime_read_errors", lambda: _read_errors(include_warnings=include_warnings, limit=limit)))

    @mcp.tool(name="runtime_assert")
    def runtime_assert(
        assertions: Annotated[
            list[RuntimeAssertion],
            Field(min_length=1, max_length=64, description="One or more public runtime assertions."),
        ],
    ) -> dict:
        """Evaluate runtime assertions for state, scene, transforms, input, and public component fields."""

        if not assertions:
            raise ValueError("assertions must contain between 1 and 64 items")
        normalized = [_assertion_mapping(item) for item in assertions]

        def _assert():
            return _evaluate_assertions(normalized)

        return ok(_run_on_main("runtime_assert", _assert))


def _run_on_main(name: str, fn):
    return MainThreadCommandQueue.instance().run_sync(name, fn, timeout_ms=30000)


def _renderer_state() -> dict[str, Any]:
    from Infernux.application import Application, _renderer_state_from_native

    engine = Application._current_engine()
    if engine is None:
        from Infernux.engine.bootstrap import EditorBootstrap

        bootstrap = EditorBootstrap.instance()
        engine = bootstrap.engine if bootstrap is not None else None
    native = engine.get_native_engine() if engine is not None else None
    state = _renderer_state_from_native(native)
    try:
        preview_tasks = [dict(item) for item in native.preview_task_snapshots]
    except (AttributeError, RuntimeError):
        preview_tasks = []
    try:
        asset_runtime_record_count = len(native.asset_runtime_records)
    except Exception:
        asset_runtime_record_count = 0
    return state | {
        "preview_tasks": preview_tasks,
        "asset_runtime_record_count": asset_runtime_record_count,
    }


def _ui_performance_state() -> dict[str, Any]:
    from Infernux.engine.bootstrap import EditorBootstrap

    bootstrap = EditorBootstrap.instance()
    engine = bootstrap.engine if bootstrap is not None else None
    native = engine.get_native_engine() if engine is not None else None
    if native is None:
        raise RuntimeError("UI performance telemetry requires a running graphical Editor session.")
    return dict(native.renderer_ui_performance_snapshot)


def _physics_state(physics_api=None, scene_manager=None) -> dict[str, Any]:
    if physics_api is None or scene_manager is None:
        from Infernux.lib import SceneManager
        from Infernux.physics import Physics

        physics_api = Physics if physics_api is None else physics_api
        scene_manager = SceneManager.instance() if scene_manager is None else scene_manager
    if scene_manager is None:
        raise RuntimeError("Physics telemetry requires an active SceneManager.")
    profile = dict(scene_manager.get_last_frame_profile())
    return {
        "body_count": int(physics_api.body_count),
        "fixed_time_step": float(scene_manager.get_fixed_time_step()),
        "frame_profile": profile,
        "fixed_step_ran": int(profile.get("fixed_steps", 0) or 0) > 0,
        "contact_events_observed": int(profile.get("contact_events", 0) or 0) > 0,
    }


def _physics_query_arguments(
    *,
    origin,
    direction,
    max_distance: float,
    layer_mask: int,
    limit: int,
) -> dict[str, Any]:
    distance = float(max_distance)
    if not math.isfinite(distance) or distance <= 0.0:
        raise ValueError("max_distance must be a finite value greater than zero")
    return {
        "origin": _physics_vector(origin, "origin"),
        "direction": _physics_vector(direction, "direction", non_zero=True),
        "max_distance": min(distance, 10000.0),
        "layer_mask": _physics_layer_mask(layer_mask),
        "limit": max(1, min(int(limit), 128)),
    }


def _physics_vector(value, name: str, *, positive: bool = False, non_zero: bool = False) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain exactly three numbers")
    vector = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in vector):
        raise ValueError(f"{name} must contain only finite numbers")
    if positive and any(component <= 0.0 for component in vector):
        raise ValueError(f"{name} components must all be greater than zero")
    if non_zero and sum(component * component for component in vector) <= 1e-12:
        raise ValueError(f"{name} must not be a zero vector")
    return vector


def _physics_layer_mask(value: int) -> int:
    mask = int(value)
    if mask < 0 or mask > 0xFFFFFFFF:
        raise ValueError("layer_mask must be an unsigned 32-bit integer")
    return mask


def _physics_raycast(*, origin, direction, max_distance, layer_mask, query_triggers, limit, physics_api=None):
    if physics_api is None:
        from Infernux.physics import Physics

        physics_api = Physics
    hits = list(physics_api.raycast_all(origin, direction, max_distance, layer_mask, query_triggers) or [])
    serialized = [_serialize_physics_hit(hit) for hit in hits[:limit]]
    return {
        "query": "raycast",
        "origin": list(origin),
        "direction": list(direction),
        "max_distance": float(max_distance),
        "layer_mask": int(layer_mask),
        "query_triggers": bool(query_triggers),
        "hit_count": len(hits),
        "truncated": len(hits) > limit,
        "hits": serialized,
    }


def _physics_overlap_box(*, center, half_extents, layer_mask, query_triggers, limit, physics_api=None):
    if physics_api is None:
        from Infernux.physics import Physics

        physics_api = Physics
    colliders = list(physics_api.overlap_box(center, half_extents, None, layer_mask, query_triggers) or [])
    serialized = [_serialize_physics_collider(collider) for collider in colliders[:limit]]
    return {
        "query": "overlap_box",
        "center": list(center),
        "half_extents": list(half_extents),
        "layer_mask": int(layer_mask),
        "query_triggers": bool(query_triggers),
        "hit_count": len(colliders),
        "truncated": len(colliders) > limit,
        "colliders": serialized,
    }


def _serialize_physics_hit(hit) -> dict[str, Any]:
    return {
        **_serialize_physics_collider(getattr(hit, "collider", None), game_object=getattr(hit, "game_object", None)),
        "distance": float(getattr(hit, "distance", 0.0)),
        "point": _vec(getattr(hit, "point")),
        "normal": _vec(getattr(hit, "normal")),
    }


def _serialize_physics_collider(collider, *, game_object=None) -> dict[str, Any]:
    obj = game_object if game_object is not None else getattr(collider, "game_object", None)
    return {
        "object_id": int(getattr(obj, "id", 0) or 0),
        "object_name": str(getattr(obj, "name", "") or ""),
        "collider_type": str(getattr(collider, "type_name", type(collider).__name__ if collider is not None else "")),
        "component_id": int(getattr(collider, "component_id", 0) or 0),
        "enabled": bool(getattr(collider, "enabled", True)),
        "is_trigger": bool(getattr(collider, "is_trigger", False)),
    }


def _motion_capture_arguments(
    object_names: list[str],
    seconds: float,
    sample_interval: float,
) -> tuple[list[str], float, float]:
    names = [str(name or "").strip() for name in object_names or [] if str(name or "").strip()]
    if not names or len(names) > 16:
        raise ValueError("object_names must contain between 1 and 16 non-empty names")
    duration = max(0.02, min(float(seconds), 30.0))
    interval = max(0.02, min(float(sample_interval), 1.0))
    interval = max(interval, duration / 300.0)
    return names, duration, interval


def _normalize_hold_keys(
    hold_key: str | int | None,
    hold_keys: list[str | int] | None,
) -> list[str | int]:
    if hold_key is not None and hold_keys:
        raise ValueError("Use hold_key or hold_keys, not both")
    values = list(hold_keys or ([] if hold_key is None else [hold_key]))
    if len(values) > 8:
        raise ValueError("hold_keys may contain at most 8 keys")
    normalized: list[str | int] = []
    identities: set[tuple[type, str]] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("hold_keys entries must be key names or SDL scancodes")
        if isinstance(value, str) and not value.strip():
            raise ValueError("hold_keys entries must not be empty")
        key = value.strip() if isinstance(value, str) else value
        identity = (type(key), str(key).lower())
        if identity in identities:
            raise ValueError("hold_keys must not contain duplicate keys")
        identities.add(identity)
        normalized.append(key)
    return normalized


def _normalize_frame_count(frame_count: int | None) -> int:
    if frame_count is None:
        return 0
    if isinstance(frame_count, bool):
        raise ValueError("frame_count must be an integer")
    frames = int(frame_count)
    if frames < 1 or frames > 120_000:
        raise ValueError("frame_count must be between 1 and 120000")
    return frames


def _normalize_motion_capture_frame_plan(
    frame_count: int,
    hold_frame_count: int | None,
    wait_frame_count: int | None,
    has_held_input: bool,
) -> tuple[int, int, int]:
    """Return hold, settle, and total frame budgets for one owned input action."""
    hold = _normalize_frame_count(hold_frame_count) if hold_frame_count is not None else 0
    if wait_frame_count is None:
        wait = 0
    else:
        if isinstance(wait_frame_count, bool):
            raise ValueError("wait_frame_count must be an integer")
        wait = int(wait_frame_count)
        if wait < 0 or wait > 120_000:
            raise ValueError("wait_frame_count must be between 0 and 120000")
    if wait and not hold:
        raise ValueError("wait_frame_count requires hold_frame_count")
    if hold and not has_held_input:
        raise ValueError("hold_frame_count requires hold_key, hold_keys, or hold_mouse_buttons")
    if frame_count and wait:
        raise ValueError("Use frame_count as the total budget, or use hold_frame_count with wait_frame_count")
    if frame_count:
        if hold > frame_count:
            raise ValueError("hold_frame_count must not exceed frame_count")
        if has_held_input and not hold:
            hold = frame_count
        return hold, frame_count - hold, frame_count
    if hold:
        total = hold + wait
        if total > 120_000:
            raise ValueError("hold_frame_count plus wait_frame_count must not exceed 120000")
        return hold, wait, total
    return 0, 0, 0


def _normalize_hold_mouse_buttons(value: list[int] | None) -> list[int]:
    buttons = list(value or [])
    if len(buttons) > 5:
        raise ValueError("hold_mouse_buttons may contain at most 5 buttons")
    normalized: list[int] = []
    for button in buttons:
        if isinstance(button, bool) or int(button) not in range(5):
            raise ValueError("hold_mouse_buttons must use Unity button indices 0 through 4")
        normalized.append(int(button))
    if len(set(normalized)) != len(normalized):
        raise ValueError("hold_mouse_buttons must not contain duplicate buttons")
    return normalized


def _finite_capture_coordinate(name: str, value: float) -> float:
    coordinate = float(value)
    if not math.isfinite(coordinate):
        raise ValueError(f"{name} must be finite")
    return coordinate


def _pause_active_play_mode() -> bool:
    from Infernux.engine.play_mode import PlayModeManager, PlayModeState

    manager = PlayModeManager.instance()
    if manager is None:
        raise RuntimeError("PlayModeManager is unavailable.")
    if manager.state == PlayModeState.PAUSED:
        return True
    if manager.state != PlayModeState.PLAYING:
        return False
    return bool(manager.pause())


def _begin_automation_game_input() -> None:
    from Infernux.input import Input

    Input._begin_automation_game_input()


def _end_automation_game_input() -> None:
    from Infernux.input import Input

    Input._end_automation_game_input()


def _arm_debug_frame_pause(
    frame_count: int,
    completion_event,
    pause_on_complete: bool,
    *,
    hold_frame_count: int = 0,
    hold_complete_event=None,
    hold_complete_callback=None,
) -> None:
    from Infernux.engine.play_mode import PlayModeManager, PlayModeState

    manager = PlayModeManager.instance()
    if manager is None or manager.state != PlayModeState.PLAYING:
        raise RuntimeError("Frame-bounded input requires active Play mode.")
    manager._arm_debug_frame_pause_gate(
        frame_count,
        completion_event,
        pause_on_complete=pause_on_complete,
        hold_frame_count=hold_frame_count,
        hold_complete_event=hold_complete_event,
        hold_complete_callback=hold_complete_callback,
    )


def _release_motion_capture_input_on_main(
    capture_id: str,
    keys: list[str | int],
    mouse_buttons: list[int],
    mouse_x: float,
    mouse_y: float,
    hold_frame_count: int,
) -> None:
    """Queue a release exactly on the configured Play-mode frame boundary."""
    from Infernux.mcp.tools import input as input_tools

    releases: list[dict[str, Any]] = []
    try:
        native = input_tools._native_engine()
        for button in reversed(mouse_buttons):
            sequence = int(native.queue_synthetic_mouse_button_input(button, False, mouse_x, mouse_y))
            releases.append({
                "button": button,
                "pressed": False,
                "sequence": sequence,
                "queued_at_hold_frame": int(hold_frame_count),
            })
        for key in reversed(keys):
            scancode = input_tools._resolve_scancode(key)
            sequence = int(native.queue_synthetic_key_input(scancode, False, False))
            releases.append({
                "key": key,
                "scancode": scancode,
                "pressed": False,
                "sequence": sequence,
                "queued_at_hold_frame": int(hold_frame_count),
            })
    except Exception as exc:
        with _MOTION_CAPTURE_LOCK:
            record = _MOTION_CAPTURES.get(capture_id)
            if record is not None:
                record["input_release_error"] = str(exc)
        return
    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(capture_id)
        if record is not None:
            record["_held_input_released"] = True
            record["input_releases"] = releases
            record["input_release"] = releases[0] if releases else {}
            record["input_released_after_hold_frame"] = int(hold_frame_count)


def _cancel_debug_frame_pause() -> None:
    from Infernux.engine.play_mode import PlayModeManager

    manager = PlayModeManager.instance()
    if manager is not None:
        manager._cancel_debug_frame_pause_gate()


def _start_motion_capture_worker(capture_id: str) -> None:
    worker = threading.Thread(
        target=_motion_capture_worker,
        args=(capture_id,),
        name=f"InfernuxMotionCapture-{capture_id[-6:]}",
        daemon=True,
    )
    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(capture_id)
        if record is not None:
            record["worker"] = worker
    worker.start()


def _register_motion_capture_trigger(capture_id: str) -> None:
    from Infernux.engine.play_mode import PlayModeManager

    manager = PlayModeManager.instance()
    if manager is None:
        raise RuntimeError("PlayModeManager is unavailable.")

    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(capture_id)
        if record is None:
            raise RuntimeError("Motion capture no longer exists.")
        trigger_state = str(record["trigger_play_state"])

    def on_state_change(event) -> None:
        observed = str(getattr(getattr(event, "new_state", None), "name", "")).lower()
        if observed != trigger_state:
            return
        with _MOTION_CAPTURE_LOCK:
            current = _MOTION_CAPTURES.get(capture_id)
            if current is None or str(current.get("status") or "") != "armed":
                return
            current["trigger_state"] = {
                "play_state": observed,
                "event_timestamp": float(getattr(event, "timestamp", time.time()) or time.time()),
            }
            current["_trigger_event"].set()

    manager.add_state_change_listener(on_state_change)
    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(capture_id)
        if record is None:
            manager.remove_state_change_listener(on_state_change)
            raise RuntimeError("Motion capture no longer exists.")
        record["_trigger_manager"] = manager
        record["_trigger_listener"] = on_state_change


def _unregister_motion_capture_trigger(capture_id: str) -> None:
    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(capture_id)
        if record is None:
            return
        manager = record.pop("_trigger_manager", None)
        listener = record.pop("_trigger_listener", None)
    if manager is not None and listener is not None:
        manager.remove_state_change_listener(listener)


def _motion_capture_worker(capture_id: str) -> None:
    pressed_keys: list[str | int] = []
    pressed_mouse_buttons: list[int] = []
    releases: list[dict[str, Any]] = []
    frame_gate_armed = False
    automation_input_armed = False
    try:
        with _MOTION_CAPTURE_LOCK:
            record = _MOTION_CAPTURES.get(capture_id)
            if record is None:
                return
            names = list(record["object_names"])
            duration = float(record["seconds"])
            interval = float(record["sample_interval"])
            trigger_state = str(record["trigger_play_state"])
            trigger_timeout = float(record["trigger_timeout"])
            held_keys = list(record.get("hold_keys") or [])
            held_mouse_buttons = list(record.get("hold_mouse_buttons") or [])
            mouse_x = float(record.get("mouse_x", -10_000.0))
            mouse_y = float(record.get("mouse_y", -10_000.0))
            frame_count = int(record.get("frame_count", 0) or 0)
            hold_frame_count = int(record.get("hold_frame_count", 0) or 0)
            wait_seconds = float(record.get("wait_seconds", 0.0) or 0.0)
            pause_on_complete = bool(record.get("pause_on_complete"))
            probes = list(record.get("component_probes") or [])
            stop_assertions = list(record.get("stop_assertions") or [])
            stop_mode = str(record.get("stop_mode") or "all")
            pause_on_condition = bool(record.get("pause_on_condition"))
            cancel_event = record["cancel_event"]
            trigger_event = record["_trigger_event"]
            frame_complete_event = record["_frame_complete_event"]
            hold_complete_event = record["_hold_complete_event"]

        trigger_deadline = time.monotonic() + trigger_timeout
        while time.monotonic() < trigger_deadline:
            if cancel_event.is_set():
                _finish_motion_capture(capture_id, status="cancelled")
                return
            remaining = max(trigger_deadline - time.monotonic(), 0.0)
            if trigger_event.wait(timeout=min(0.1, remaining)):
                break
        else:
            _finish_motion_capture(
                capture_id,
                status="trigger_timeout",
                error=f"Play state did not become {trigger_state!r} within {trigger_timeout:.3f} seconds.",
            )
            return

        with _MOTION_CAPTURE_LOCK:
            current = _MOTION_CAPTURES.get(capture_id)
            observed_state = dict((current or {}).get("trigger_state") or {})
        started = time.monotonic()
        with _MOTION_CAPTURE_LOCK:
            record = _MOTION_CAPTURES.get(capture_id)
            if record is None:
                return
            record["status"] = "capturing"
            record["triggered_at"] = time.time()
            record["trigger_state"] = observed_state

        presses: list[dict[str, Any]] = []
        if held_keys or held_mouse_buttons:
            _run_on_main(
                "runtime_motion_capture.begin_game_input",
                _begin_automation_game_input,
            )
            automation_input_armed = True
            from Infernux.mcp.tools.input import perform_key_transition
            from Infernux.mcp.tools.input import perform_mouse_button_transition

            for index, key in enumerate(held_keys):
                press = perform_key_transition(
                    key,
                    True,
                    trace_name=f"runtime_motion_capture.press.{index}",
                )
                presses.append(dict(press.get("data") or {}))
                if not press.get("ok"):
                    _finish_motion_capture(
                        capture_id,
                        status="failed",
                        error=str((press.get("error") or {}).get("message") or "Input press failed."),
                        input_presses=presses,
                    )
                    return
                pressed_keys.append(key)
            for index, button in enumerate(held_mouse_buttons):
                press = perform_mouse_button_transition(
                    button,
                    True,
                    x=mouse_x,
                    y=mouse_y,
                    trace_name=f"runtime_motion_capture.mouse_press.{index}",
                )
                presses.append(dict(press.get("data") or {}))
                if not press.get("ok"):
                    _finish_motion_capture(
                        capture_id,
                        status="failed",
                        error=str((press.get("error") or {}).get("message") or "Mouse input press failed."),
                        input_presses=presses,
                    )
                    return
                pressed_mouse_buttons.append(button)

        if frame_count:
            _run_on_main(
                "runtime_motion_capture.arm_frame_pause",
                lambda: _arm_debug_frame_pause(
                    frame_count,
                    frame_complete_event,
                    pause_on_complete and wait_seconds <= 0.0,
                    hold_frame_count=hold_frame_count,
                    hold_complete_event=hold_complete_event,
                    hold_complete_callback=(
                        lambda: _release_motion_capture_input_on_main(
                            capture_id,
                            held_keys,
                            held_mouse_buttons,
                            mouse_x,
                            mouse_y,
                            hold_frame_count,
                        )
                    ) if hold_frame_count else None,
                ),
            )
            frame_gate_armed = True

        trajectory: list[dict[str, Any]] = []
        elapsed = 0.0
        condition_met = False
        frame_budget_completed_at: float | None = None
        while True:
            if cancel_event.is_set():
                _finish_motion_capture(capture_id, status="cancelled", trajectory=trajectory)
                return
            if hold_frame_count and hold_complete_event.is_set() and (pressed_keys or pressed_mouse_buttons):
                with _MOTION_CAPTURE_LOCK:
                    current = _MOTION_CAPTURES.get(capture_id)
                    released = bool((current or {}).get("_held_input_released"))
                    release_error = str((current or {}).get("input_release_error") or "")
                    if released:
                        releases = list((current or {}).get("input_releases") or [])
                if release_error:
                    raise RuntimeError(f"Frame-bound input release failed: {release_error}")
                if released:
                    pressed_keys.clear()
                    pressed_mouse_buttons.clear()
            objects = _run_on_main(
                "runtime_motion_capture.sample",
                lambda: _named_transform_snapshots(names, probes),
            )
            missing = [name for name in names if name not in objects]
            if missing:
                _finish_motion_capture(
                    capture_id,
                    status="failed",
                    trajectory=trajectory,
                    error=f"Motion capture objects disappeared: {', '.join(missing)}.",
                )
                return
            trajectory.append({"elapsed_seconds": elapsed, "objects": objects})
            with _MOTION_CAPTURE_LOCK:
                current = _MOTION_CAPTURES.get(capture_id)
                if current is not None:
                    current["trajectory"] = list(trajectory)
                    current["elapsed_seconds"] = elapsed
            if stop_assertions:
                condition = _run_on_main(
                    "runtime_motion_capture.evaluate_stop_condition",
                    lambda: _evaluate_motion_capture_stop_assertions(stop_assertions, stop_mode),
                )
                with _MOTION_CAPTURE_LOCK:
                    current = _MOTION_CAPTURES.get(capture_id)
                    if current is not None:
                        current["stop_condition"] = condition
                if condition["passed"]:
                    condition_met = True
                    break
            if frame_count:
                if frame_complete_event.is_set():
                    if wait_seconds <= 0.0:
                        break
                    if frame_budget_completed_at is None:
                        frame_budget_completed_at = time.monotonic()
                    elif time.monotonic() - frame_budget_completed_at >= wait_seconds:
                        break
            elif elapsed + 1.0e-9 >= duration:
                break
            remaining = (duration + wait_seconds) - (time.monotonic() - started)
            if remaining <= 0.0:
                if frame_count:
                    _finish_motion_capture(
                        capture_id,
                        status="frame_timeout",
                        trajectory=trajectory,
                        after_state=_run_on_main("runtime_motion_capture.frame_timeout_state", _editor_state),
                        measurements=[_summarize_motion(name, trajectory) for name in names],
                        component_measurements=_summarize_component_probes(probes, trajectory),
                        input_presses=presses,
                        input_releases=releases,
                        error=(
                            f"Frame budget did not complete within the {duration:.3f}s safety timeout."
                        ),
                    )
                    return
                break
            wait = min(interval, remaining)
            if frame_count:
                frame_complete_event.wait(wait)
            else:
                time.sleep(wait)
            elapsed = min(time.monotonic() - started, duration)

        if condition_met and pause_on_condition:
            _run_on_main("runtime_motion_capture.pause_on_condition", _pause_active_play_mode)
        elif pause_on_complete and (not frame_count or wait_seconds > 0.0):
            _run_on_main("runtime_motion_capture.pause_on_complete", _pause_active_play_mode)

        if pressed_keys:
            from Infernux.mcp.tools.input import perform_key_transition

            for index, key in enumerate(reversed(list(pressed_keys))):
                release = perform_key_transition(
                    key,
                    False,
                    trace_name=f"runtime_motion_capture.release.{index}",
                )
                releases.append(dict(release.get("data") or {}))
                if not release.get("ok"):
                    raise RuntimeError(
                        str((release.get("error") or {}).get("message") or "Input release failed.")
                    )
                pressed_keys.remove(key)
        if pressed_mouse_buttons:
            from Infernux.mcp.tools.input import perform_mouse_button_transition

            for index, button in enumerate(reversed(list(pressed_mouse_buttons))):
                release = perform_mouse_button_transition(
                    button,
                    False,
                    x=mouse_x,
                    y=mouse_y,
                    trace_name=f"runtime_motion_capture.mouse_release.{index}",
                )
                releases.append(dict(release.get("data") or {}))
                if not release.get("ok"):
                    raise RuntimeError(
                        str((release.get("error") or {}).get("message") or "Mouse input release failed.")
                    )
                pressed_mouse_buttons.remove(button)
        after_state = _run_on_main("runtime_motion_capture.after_state", _editor_state)
        _finish_motion_capture(
            capture_id,
            status="condition_met" if condition_met else "completed",
            trajectory=trajectory,
            after_state=after_state,
            measurements=[_summarize_motion(name, trajectory) for name in names],
            component_measurements=_summarize_component_probes(probes, trajectory),
            input_presses=presses,
            input_releases=releases,
        )
    except Exception as exc:
        _finish_motion_capture(capture_id, status="failed", error=str(exc))
    finally:
        if pressed_keys:
            try:
                from Infernux.mcp.tools.input import perform_key_transition

                for index, key in enumerate(reversed(list(pressed_keys))):
                    release = perform_key_transition(
                        key,
                        False,
                        trace_name=f"runtime_motion_capture.release_after_failure.{index}",
                    )
                    releases.append(dict((release or {}).get("data") or {}))
                    if release.get("ok"):
                        pressed_keys.remove(key)
                _finish_motion_capture(
                    capture_id,
                    status=str((_public_motion_capture(capture_id) or {}).get("status") or "failed"),
                    input_releases=releases,
                )
            except Exception:
                pass
        try:
            _run_on_main(
                "runtime_motion_capture.unregister_trigger",
                lambda: _unregister_motion_capture_trigger(capture_id),
            )
        except Exception:
            pass
        if frame_gate_armed:
            try:
                _run_on_main("runtime_motion_capture.cancel_frame_pause", _cancel_debug_frame_pause)
            except Exception:
                pass
        if pressed_mouse_buttons:
            try:
                from Infernux.mcp.tools.input import perform_mouse_button_transition

                for index, button in enumerate(reversed(list(pressed_mouse_buttons))):
                    release = perform_mouse_button_transition(
                        button,
                        False,
                        x=mouse_x,
                        y=mouse_y,
                        trace_name=f"runtime_motion_capture.mouse_release_after_failure.{index}",
                    )
                    releases.append(dict((release or {}).get("data") or {}))
                    if release.get("ok"):
                        pressed_mouse_buttons.remove(button)
                _finish_motion_capture(
                    capture_id,
                    status=str((_public_motion_capture(capture_id) or {}).get("status") or "failed"),
                    input_releases=releases,
                )
            except Exception:
                pass
        if automation_input_armed:
            try:
                _run_on_main(
                    "runtime_motion_capture.end_game_input",
                    _end_automation_game_input,
                )
            except Exception:
                pass


def _finish_motion_capture(
    capture_id: str,
    *,
    status: str,
    trajectory: list[dict[str, Any]] | None = None,
    after_state: dict[str, Any] | None = None,
    measurements: list[dict[str, Any]] | None = None,
    component_measurements: list[dict[str, Any]] | None = None,
    input_press: dict[str, Any] | None = None,
    input_release: dict[str, Any] | None = None,
    input_presses: list[dict[str, Any]] | None = None,
    input_releases: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(capture_id)
        if record is None:
            return
        if trajectory is not None:
            record["trajectory"] = trajectory
        if after_state is not None:
            record["after_state"] = after_state
        if measurements is not None:
            record["measurements"] = measurements
        if component_measurements is not None:
            record["component_measurements"] = component_measurements
        if input_press is not None:
            record["input_press"] = input_press
        if input_release is not None:
            record["input_release"] = input_release
        if input_presses is not None:
            record["input_presses"] = input_presses
            record["input_press"] = input_presses[0] if input_presses else {}
        if input_releases is not None:
            record["input_releases"] = input_releases
            record["input_release"] = input_releases[0] if input_releases else {}
        record["status"] = status
        if error is not None:
            record["error"] = str(error or "")
        record["finished_at"] = time.time()
        completion_event = record.get("_terminal_event")
        if isinstance(completion_event, threading.Event):
            completion_event.set()


def _public_motion_capture(capture_id: str) -> dict[str, Any]:
    with _MOTION_CAPTURE_LOCK:
        record = _MOTION_CAPTURES.get(str(capture_id or ""))
        return _public_motion_capture_locked(record) if record is not None else {}


def _public_motion_capture_locked(record: dict[str, Any]) -> dict[str, Any]:
    value = {
        key: copy.deepcopy(item)
        for key, item in record.items()
        if key not in {"cancel_event", "worker"} and not key.startswith("_")
    }
    value["terminal"] = str(value.get("status") or "") in _MOTION_CAPTURE_TERMINAL_STATES
    value["sample_count"] = len(value.get("trajectory") or [])
    return value


def _prune_motion_captures_locked() -> None:
    if len(_MOTION_CAPTURES) < _MAX_RETAINED_MOTION_CAPTURES:
        return
    terminal = [
        (identifier, float(record.get("finished_at", record.get("created_at", 0.0)) or 0.0))
        for identifier, record in _MOTION_CAPTURES.items()
        if str(record.get("status") or "") in _MOTION_CAPTURE_TERMINAL_STATES
    ]
    for identifier, _timestamp in sorted(terminal, key=lambda item: item[1]):
        _MOTION_CAPTURES.pop(identifier, None)
        if len(_MOTION_CAPTURES) < _MAX_RETAINED_MOTION_CAPTURES:
            break


def _summarize_motion(name: str, trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [
        entry.get("objects", {}).get(name)
        for entry in trajectory
        if isinstance(entry.get("objects"), dict) and entry.get("objects", {}).get(name) is not None
    ]
    first = samples[0] if samples else None
    last = samples[-1] if samples else None
    positions = [list(sample["position"]) for sample in samples if sample.get("position") is not None]
    if not positions:
        return {
            "name": name,
            "before": first,
            "after": last,
            "delta": None,
            "sample_count": len(samples),
            "position_min": None,
            "position_max": None,
            "position_range": None,
            "max_excursion_from_start": None,
            "axis_path_length": None,
            "path_length": None,
        }

    start = positions[0]
    end = positions[-1]
    position_min = [min(position[axis] for position in positions) for axis in range(3)]
    position_max = [max(position[axis] for position in positions) for axis in range(3)]
    axis_path_length = [
        sum(abs(current[axis] - previous[axis]) for previous, current in zip(positions, positions[1:]))
        for axis in range(3)
    ]
    return {
        "name": name,
        "before": first,
        "after": last,
        "delta": [end[axis] - start[axis] for axis in range(3)],
        "sample_count": len(positions),
        "position_min": position_min,
        "position_max": position_max,
        "position_range": [position_max[axis] - position_min[axis] for axis in range(3)],
        "max_excursion_from_start": [
            max(abs(position[axis] - start[axis]) for position in positions) for axis in range(3)
        ],
        "axis_path_length": axis_path_length,
        "path_length": sum(dist(previous, current) for previous, current in zip(positions, positions[1:])),
    }


def _evaluate_assertions(assertions: list[dict[str, Any]] | None) -> dict[str, Any]:
    results = [_evaluate_assertion(item if isinstance(item, dict) else {}) for item in assertions or []]
    return {"passed": all(result["passed"] for result in results), "results": results}


def _normalize_motion_capture_stop_assertions(
    assertions: list[RuntimeAssertion] | None,
    mode: MotionCaptureStopMode,
) -> tuple[list[dict[str, Any]], MotionCaptureStopMode]:
    normalized = [_assertion_mapping(item) for item in assertions or []]
    if len(normalized) > 16:
        raise ValueError("stop_assertions may contain at most 16 items")
    selected_mode = str(mode or "all").lower()
    if selected_mode not in {"all", "any"}:
        raise ValueError("stop_mode must be 'all' or 'any'")
    return normalized, selected_mode  # type: ignore[return-value]


def _evaluate_motion_capture_stop_assertions(
    assertions: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    results = [_evaluate_assertion(item) for item in assertions]
    passed = any(result["passed"] for result in results) if mode == "any" else all(result["passed"] for result in results)
    return {"passed": passed, "mode": mode, "results": results}


def _assertion_mapping(item: RuntimeAssertion | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, RuntimeAssertion):
        return item.model_dump(exclude_unset=True, exclude_none=True)
    return dict(item)


def _evaluate_assertion(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind", "") or "")
    if kind == "play_state":
        actual = _editor_state().get("play_state")
        expected = _normalize_play_state(item.get("equals", ""))
        return _assertion_result(item, actual == expected, f"play_state is {actual!r}, expected {expected!r}", actual=actual)
    if kind == "audio_paused":
        actual = bool(_editor_state().get("audio_paused", False))
        return _comparison_assertion(item, actual, "audio_paused")
    if kind == "object_exists":
        obj = _resolve_assertion_object(item)
        passed = obj is not None
        return _assertion_result(item, passed, f"object_id {item.get('object_id')} exists={passed}")
    if kind == "component_exists":
        obj = _resolve_assertion_object(item)
        component_type = str(item.get("component_type", ""))
        comp = _find_component(obj, component_type, 0) if obj else None
        passed = comp is not None
        return _assertion_result(item, passed, f"component {component_type!r} exists={passed}")
    if kind == "no_errors":
        errors = _all_runtime_errors(_read_errors(include_warnings=False))
        return _assertion_result(item, not errors, f"{len(errors)} error(s)", actual=len(errors))
    if kind in {"scene_name", "scene_path"}:
        actual = _editor_state().get(kind, "")
        return _comparison_assertion(item, actual, kind)
    if kind == "transform_axis":
        obj = _resolve_assertion_object(item)
        field = str(item.get("field", "position") or "position")
        axis = str(item.get("axis", "") or "").lower()
        if obj is None:
            return _assertion_result(item, False, f"object_id {item.get('object_id')} was not found")
        if field not in _TRANSFORM_VECTOR_FIELDS:
            return _assertion_result(item, False, f"Unknown transform field: {field}")
        if axis not in {"x", "y", "z"}:
            return _assertion_result(item, False, f"Transform axis must be x, y, or z; got {axis!r}")
        value = getattr(obj.transform, _TRANSFORM_VECTOR_FIELDS[field])
        actual = float(getattr(value, axis))
        return _comparison_assertion(item, actual, f"transform.{field}.{axis}")
    if kind == "input_axis":
        axis = str(item.get("axis", "") or "")
        if not axis:
            return _assertion_result(item, False, "input_axis requires a non-empty axis")
        from Infernux.input import Input

        actual = float(Input.get_axis_raw(axis))
        return _comparison_assertion(item, actual, f"input_axis.{axis}")
    if kind == "component_field":
        obj = _resolve_assertion_object(item)
        component_type = str(item.get("component_type", "") or "")
        field = str(item.get("field", "") or "")
        if obj is None:
            return _assertion_result(item, False, f"object_id {item.get('object_id')} was not found")
        if not component_type or not field or field.startswith("_"):
            return _assertion_result(item, False, "component_field requires a component_type and public field")
        comp = _find_component(obj, component_type, int(item.get("ordinal", 0) or 0))
        if comp is None:
            return _assertion_result(item, False, f"component {component_type!r} was not found")
        if not hasattr(comp, field):
            return _assertion_result(item, False, f"component {component_type!r} has no public field {field!r}")
        actual = serialize_value(getattr(comp, field))
        return _comparison_assertion(item, actual, f"component.{component_type}.{field}")
    return _assertion_result(item, False, f"Unknown assertion kind: {kind}")


def _comparison_assertion(item: dict[str, Any], actual: Any, label: str) -> dict[str, Any]:
    passed, operator, expected, detail = _compare_value(actual, item)
    message = f"{label} is {actual!r}; {detail}"
    return _assertion_result(item, passed, message, actual=actual, expected=expected, operator=operator)


def _compare_value(actual: Any, item: dict[str, Any]) -> tuple[bool, str, Any, str]:
    raw_operator = str(item.get("operator", item.get("op", "equals")) or "equals").lower()
    operator = _COMPARISON_ALIASES.get(raw_operator, "")
    if "value" in item:
        expected = item["value"]
    elif "equals" in item:
        expected = item["equals"]
    else:
        return False, operator or raw_operator, None, "missing comparison value"
    if not operator:
        return False, raw_operator, expected, f"unknown comparison operator {raw_operator!r}"
    if operator in {"equals", "not_equals"}:
        if _is_number(actual) and _is_number(expected):
            try:
                tolerance = max(float(item.get("tolerance", 0.0) or 0.0), 0.0)
            except (TypeError, ValueError):
                return False, operator, expected, "tolerance must be numeric"
            equals = abs(float(actual) - float(expected)) <= tolerance
        else:
            equals = actual == expected
        passed = equals if operator == "equals" else not equals
        return passed, operator, expected, f"expected {operator} {expected!r}"
    if not (_is_number(actual) and _is_number(expected)):
        return False, operator, expected, f"operator {operator!r} requires numeric actual and expected values"
    actual_number = float(actual)
    expected_number = float(expected)
    comparisons = {
        "greater_than": actual_number > expected_number,
        "greater_or_equal": actual_number >= expected_number,
        "less_than": actual_number < expected_number,
        "less_or_equal": actual_number <= expected_number,
    }
    return comparisons[operator], operator, expected, f"expected {operator} {expected!r}"


def _assertion_result(
    assertion: dict[str, Any],
    passed: bool,
    message: str,
    *,
    actual: Any = None,
    expected: Any = None,
    operator: str = "",
) -> dict[str, Any]:
    result = {"assertion": assertion, "passed": bool(passed), "message": message}
    if actual is not None:
        result["actual"] = actual
    if expected is not None:
        result["expected"] = expected
    if operator:
        result["operator"] = operator
    return result


def _object_id(item: dict[str, Any]) -> int:
    try:
        return int(item.get("object_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_play_state(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())
    return _PLAY_STATE_ALIASES.get(normalized, normalized)


def _is_runtime_idle(state: dict[str, Any]) -> bool:
    """Return whether deferred work and scene transactions have both settled."""
    return not bool(state.get("deferred_task_busy")) and not bool(state.get("scene_loading"))


def _editor_state() -> dict[str, Any]:
    from Infernux.engine.deferred_task import DeferredTaskRunner
    from Infernux.engine.play_mode import PlayModeManager
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.ui.selection_manager import SelectionManager
    from Infernux.lib import AudioEngine, SceneManager

    pmm = PlayModeManager.instance()
    sfm = SceneFileManager.instance()
    sel = SelectionManager.instance()
    runner = DeferredTaskRunner.instance()
    scene = SceneManager.instance().get_active_scene()
    return {
        "play_state": getattr(getattr(pmm, "state", None), "name", "edit").lower() if pmm else "edit",
        "audio_paused": bool(AudioEngine.instance().is_paused),
        "deferred_task_busy": bool(getattr(runner, "is_busy", False)),
        "scene_loading": bool(getattr(sfm, "is_loading", False)) if sfm else False,
        "selected_ids": sel.get_ids() if sel else [],
        "scene_dirty": bool(sfm.is_dirty) if sfm else False,
        "is_prefab_mode": bool(getattr(sfm, "is_prefab_mode", False)) if sfm else False,
        "scene_name": str(getattr(scene, "name", "") or ""),
        "scene_path": str(getattr(sfm, "current_scene_path", "") or "") if sfm else "",
    }


def _read_errors(include_warnings: bool = False, limit: int = 100) -> dict[str, Any]:
    from Infernux.debug import DebugConsole
    from Infernux.components.script_loader import get_script_errors

    allowed = {"ERROR", "FATAL"}
    if include_warnings:
        allowed.add("WARN")
        allowed.add("WARNING")
    entries = []
    for entry in DebugConsole.instance().get_entries()[-max(int(limit), 1):]:
        level = getattr(entry.log_type, "name", str(entry.log_type)).upper()
        if level not in allowed:
            continue
        entries.append({
            "time": entry.get_formatted_time(),
            "level": level,
            "message": entry.message,
            "source_file": entry.source_file,
            "source_line": entry.source_line,
        })
    script_errors = [
        {"path": path, "traceback": tb}
        for path, tb in get_script_errors().items()
    ]
    return {"errors": entries, "script_errors": script_errors}


def _all_runtime_errors(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    errors = list(snapshot.get("errors") or [])
    for item in snapshot.get("script_errors") or []:
        errors.append({"level": "SCRIPT", **dict(item)})
    return errors


def _new_runtime_errors(
    current: list[dict[str, Any]], baseline: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    unmatched_baseline = list(baseline)
    added = []
    for item in current:
        try:
            unmatched_baseline.remove(item)
        except ValueError:
            added.append(item)
    return added


def _components(obj) -> list[dict[str, Any]]:
    items = []
    seen = set()
    for getter in ("get_components", "get_py_components"):
        try:
            for comp in getattr(obj, getter)() or []:
                data = serialize_component(comp)
                key = (data.get("type"), data.get("component_id"))
                if key in seen:
                    continue
                seen.add(key)
                items.append(data)
        except Exception:
            pass
    return items


def _find_component(obj, component_type: str, ordinal: int = 0):
    if obj is None:
        return None
    matches = []
    seen = set()
    for getter in ("get_components", "get_py_components"):
        try:
            for comp in getattr(obj, getter)() or []:
                type_name = getattr(comp, "type_name", type(comp).__name__)
                if type_name != component_type and type(comp).__name__ != component_type:
                    continue
                public_comp = _public_component_wrapper(obj, comp, type_name)
                component_id = getattr(public_comp, "component_id", None)
                key = (type_name, component_id) if component_id is not None else id(public_comp)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(public_comp)
        except Exception:
            pass
    return matches[ordinal] if 0 <= ordinal < len(matches) else None


def _public_component_wrapper(obj, comp, type_name: str):
    """Expose built-ins through their documented Python wrapper surface."""
    try:
        from Infernux.components.builtin_component import BuiltinComponent

        if isinstance(comp, BuiltinComponent):
            return comp
        wrapper_cls = BuiltinComponent._builtin_registry.get(type_name)
        if wrapper_cls is not None:
            return wrapper_cls._get_or_create_wrapper(comp, obj)
    except Exception:
        pass
    return comp


def _try_find_object(object_id: int):
    try:
        return find_game_object(object_id)
    except Exception:
        return None


def _resolve_assertion_object(item: dict[str, Any]):
    object_id = _object_id(item)
    if object_id:
        return _try_find_object(object_id)
    name = str(item.get("object_name", "") or "")
    if not name:
        return None
    try:
        from Infernux.scene import GameObjectQuery

        return GameObjectQuery.find(name)
    except Exception:
        return None


def _find_runtime_objects(*, name: str = "", tag: str = "", limit: int = 50) -> list[dict[str, Any]]:
    from Infernux.lib import SceneManager

    scene = SceneManager.instance().get_active_scene()
    if scene is None:
        return []
    roots = []
    for getter in ("get_root_objects", "get_root_game_objects"):
        if hasattr(scene, getter):
            roots = list(getattr(scene, getter)() or [])
            break
    matches: list[dict[str, Any]] = []
    pending = list(reversed(roots))
    while pending and len(matches) < limit:
        obj = pending.pop()
        child_count = int(obj.get_child_count())
        for index in reversed(range(child_count)):
            pending.append(obj.get_child(index))
        if name and str(obj.name) != name:
            continue
        if tag and str(getattr(obj, "tag", "")) != tag:
            continue
        matches.append({
            "id": int(obj.id),
            "name": str(obj.name),
            "tag": str(getattr(obj, "tag", "")),
            "layer": int(getattr(obj, "layer", 0)),
            "active": bool(getattr(obj, "active", True)),
            **_object_prefab_linkage(obj),
            "parent_id": int(getattr(obj.get_parent(), "id", 0) or 0),
            "components": _components(obj),
        })
    return matches


def _object_prefab_linkage(obj) -> dict[str, Any]:
    guid = str(getattr(obj, "prefab_guid", "") or "")
    is_root = bool(getattr(obj, "prefab_root", False))
    return {
        "prefab_guid": guid,
        "prefab_root": is_root,
        "prefab_linked": bool(guid),
    }


def _component_probe_mapping(item: RuntimeComponentProbe | dict[str, Any]) -> dict[str, Any]:
    raw = item.model_dump() if isinstance(item, RuntimeComponentProbe) else dict(item)
    return {
        "object_name": str(raw.get("object_name", "") or "").strip(),
        "component_type": str(raw.get("component_type", "") or "").strip(),
        "fields": [str(field or "").strip() for field in raw.get("fields", [])],
        "ordinal": max(0, int(raw.get("ordinal", 0) or 0)),
    }


def _named_transform_snapshots(
    names: list[str],
    component_probes: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    from Infernux.scene import GameObjectQuery

    snapshots = {}
    probes_by_name: dict[str, list[dict[str, Any]]] = {}
    for probe in component_probes or []:
        probes_by_name.setdefault(probe["object_name"], []).append(probe)
    for name in names:
        obj = GameObjectQuery.find(name)
        if obj is None:
            continue
        snapshot = {
            "id": int(obj.id),
            "position": _vec(obj.transform.position),
            "euler_angles": _vec(obj.transform.euler_angles),
        }
        component_fields = {}
        for probe in probes_by_name.get(name, []):
            component_type = probe["component_type"]
            ordinal = probe["ordinal"]
            comp = _find_component(obj, component_type, ordinal)
            values = {}
            if comp is not None:
                for field in probe["fields"]:
                    if hasattr(comp, field):
                        values[field] = serialize_value(getattr(comp, field))
            component_fields[f"{component_type}[{ordinal}]"] = values
        if component_fields:
            snapshot["component_fields"] = component_fields
        snapshots[name] = snapshot
    return snapshots


def _summarize_component_probes(
    probes: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries = []
    for probe in probes:
        object_name = probe["object_name"]
        component_type = probe["component_type"]
        ordinal = probe["ordinal"]
        component_key = f"{component_type}[{ordinal}]"
        for field in probe["fields"]:
            samples = []
            for frame in trajectory:
                obj = (frame.get("objects") or {}).get(object_name) or {}
                values = (obj.get("component_fields") or {}).get(component_key) or {}
                if field in values:
                    samples.append({"elapsed_seconds": frame.get("elapsed_seconds", 0.0), "value": values[field]})
            summary = {
                "object_name": object_name,
                "component_type": component_type,
                "ordinal": ordinal,
                "field": field,
                "sample_count": len(samples),
                "samples": samples,
            }
            if samples:
                values = [sample["value"] for sample in samples]
                summary["first"] = values[0]
                summary["last"] = values[-1]
                if all(_is_number(value) for value in values):
                    numeric = [float(value) for value in values]
                    summary["minimum"] = min(numeric)
                    summary["maximum"] = max(numeric)
                    summary["range"] = max(numeric) - min(numeric)
            summaries.append(summary)
    return summaries


def _vec(value) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def _bounded_probe_items(value, defaults: tuple, name: str) -> list:
    if value is None:
        items = list(defaults)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f"{name} must be a list or tuple.")
    if len(items) > 16:
        raise ValueError(f"{name} may contain at most 16 probes.")
    return items


def _read_input_state(keys: list, axes: list, mouse_buttons: list[int]) -> dict[str, Any]:
    from Infernux.input import Input

    state = _collect_input_state(Input, keys, axes, mouse_buttons)
    try:
        from Infernux.engine.bootstrap import EditorBootstrap

        bootstrap = EditorBootstrap.instance()
        game_view = bootstrap.game_view if bootstrap is not None else None
        processor = getattr(game_view, "_ui_event_processor", None)
        if processor is not None and hasattr(processor, "debug_state"):
            state["screen_ui_event"] = processor.debug_state()
    except Exception:
        pass
    return state


def _collect_input_state(input_api, keys: list, axes: list, mouse_buttons: list[int] | None = None) -> dict[str, Any]:
    button_states = {}
    for button in mouse_buttons or []:
        x, y, scroll_x, scroll_y, held, down, up = input_api.get_mouse_frame_state(int(button))
        button_states[str(button)] = {
            "position": [float(x), float(y)],
            "scroll": [float(scroll_x), float(scroll_y)],
            "held": bool(held),
            "down": bool(down),
            "up": bool(up),
        }
    return {
        "game_focused": bool(input_api.is_game_focused()),
        "cursor_locked": bool(input_api.is_cursor_locked()),
        "keys": {str(key): bool(input_api.get_key(key)) for key in keys},
        "axes": {str(axis): float(input_api.get_axis_raw(str(axis))) for axis in axes},
        "mouse_buttons": button_states,
    }


def _bounded_mouse_buttons(value) -> list[int]:
    items = _bounded_probe_items(value, (0,), "mouse_buttons")
    normalized = []
    for item in items:
        if isinstance(item, bool):
            raise ValueError("mouse_buttons must use integer button indices 0 through 4.")
        button = int(item)
        if button not in range(5):
            raise ValueError("mouse_buttons must use integer button indices 0 through 4.")
        normalized.append(button)
    return normalized


def _register_metadata() -> None:
    for name, summary in {
        "runtime_wait": "Wait for Play Mode, deferred-task, and scene-transition state.",
        "runtime_run_for": "Let runtime advance while polling errors.",
        "runtime_measure_motion": (
            "Sample an already-running scene inside one bounded input action; for Play startup, arm "
            "runtime_motion_capture_arm before the real Toolbar Play action."
        ),
        "runtime_motion_capture_arm": (
            "Arm the first seconds of Play/Pause sampling with optional SDL keys, a one- or two-stage frame budget "
            "(hold frames then released-key wait frames), public component probes, and sample-interval stop "
            "assertions that can pause Play before remote-agent latency misses it."
        ),
        "runtime_motion_capture_status": "Read or wait for an armed startup capture without changing Play state.",
        "runtime_motion_capture_cancel": (
            "Cancel an armed or active startup capture and release owned input; optionally pause Play first "
            "when an agent-side condition has become true."
        ),
        "runtime_input_state": "Read Game View focus and selected keyboard, axis, and mouse input probes.",
        "runtime_renderer_state": "Read renderer targets, camera availability, draw submissions, timings, and GPU residency.",
        "runtime_ui_performance": "Read the engine-recorded rolling UI profile without per-frame MCP polling.",
        "runtime_physics_state": "Read physics body population and latest fixed-step/contact profile.",
        "runtime_physics_raycast": "Run a bounded read-only raycast through the public Physics API.",
        "runtime_physics_overlap_box": "Read colliders overlapping a bounded axis-aligned world-space box.",
        "runtime_get_object_state": "Read object transform, component state, prefab_guid, and prefab_root linkage.",
        "runtime_find_objects": "Resolve runtime object IDs by public name/tag with prefab_guid and prefab_root linkage.",
        "runtime_get_component_state": "Read one component state at runtime.",
        "runtime_read_errors": "Read console and script loader errors.",
        "runtime_assert": "Evaluate runtime state, scene, transform, input, and component-field assertions.",
    }.items():
        register_tool_metadata(name, summary=summary, next_suggested_tools=["runtime_read_errors", "console_read"])
