"""Coordinate-level MCP input tools for human-equivalent validation runs."""

from __future__ import annotations

import math
import time
from typing import Any

from Infernux.mcp import session
from Infernux.mcp.threading import MainThreadCommandQueue
from Infernux.mcp.tools.common import fail, main_thread, ok, register_tool_metadata


def register_input_tools(mcp) -> None:
    """Register input tools only for the global validation workflow."""
    _register_metadata()

    @mcp.tool(name="input_key")
    def input_key(
        key: str | int,
        pressed: bool,
        repeat: bool = False,
        wait_for_delivery: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Queue one physical-key transition and optionally wait for editor delivery."""
        scancode = _resolve_scancode(key)
        return _queue_input(
            "input_key",
            lambda native: native.queue_synthetic_key_input(scancode, bool(pressed), bool(repeat)),
            arguments={"key": key, "scancode": scancode, "pressed": bool(pressed), "repeat": bool(repeat)},
            wait_for_delivery=wait_for_delivery,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(name="input_key_chord")
    def input_key_chord(
        keys: list[str | int],
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Press a key chord through SDL, releasing keys in reverse order."""
        return perform_key_chord(keys, timeout_seconds=timeout_seconds)

    @mcp.tool(name="input_pointer_move")
    def input_pointer_move(
        x: float,
        y: float,
        delta_x: float = 0.0,
        delta_y: float = 0.0,
        wait_for_delivery: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Move the pointer in window coordinates through the editor event path."""
        _require_finite("x", x)
        _require_finite("y", y)
        _require_finite("delta_x", delta_x)
        _require_finite("delta_y", delta_y)
        return _queue_input(
            "input_pointer_move",
            lambda native: native.queue_synthetic_mouse_motion_input(float(x), float(y), float(delta_x), float(delta_y)),
            arguments={"x": float(x), "y": float(y), "delta_x": float(delta_x), "delta_y": float(delta_y)},
            wait_for_delivery=wait_for_delivery,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(name="input_mouse_button")
    def input_mouse_button(
        button: int,
        pressed: bool,
        x: float,
        y: float,
        wait_for_delivery: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Queue one mouse-button transition at a window coordinate."""
        button = _validate_mouse_button(button)
        _require_finite("x", x)
        _require_finite("y", y)
        return _queue_input(
            "input_mouse_button",
            lambda native: native.queue_synthetic_mouse_button_input(button, bool(pressed), float(x), float(y)),
            arguments={"button": button, "pressed": bool(pressed), "x": float(x), "y": float(y)},
            wait_for_delivery=wait_for_delivery,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(name="input_mouse_wheel")
    def input_mouse_wheel(
        horizontal: float = 0.0,
        vertical: float = 0.0,
        wait_for_delivery: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Queue one mouse-wheel event through the editor event path."""
        _require_finite("horizontal", horizontal)
        _require_finite("vertical", vertical)
        return _queue_input(
            "input_mouse_wheel",
            lambda native: native.queue_synthetic_mouse_wheel_input(float(horizontal), float(vertical)),
            arguments={"horizontal": float(horizontal), "vertical": float(vertical)},
            wait_for_delivery=wait_for_delivery,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(name="input_text")
    def input_text(
        text: str,
        wait_for_delivery: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Send UTF-8 text to the currently focused editor control."""
        return perform_text_input(
            text,
            wait_for_delivery=wait_for_delivery,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(name="input_window_close")
    def input_window_close(
        wait_for_delivery: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Request window close through the Editor's normal close-request path."""
        return perform_window_close_request(
            wait_for_delivery=wait_for_delivery,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(name="input_status")
    def input_status() -> dict:
        """Read delivery status for the synthetic input queue."""
        session.require_mode("global_validation")
        return main_thread("input_status", _native_status)

    @mcp.tool(name="input_wait")
    def input_wait(sequence: int, timeout_seconds: float = 3.0) -> dict:
        """Wait until a queued input sequence has reached ImGui and InputManager."""
        session.require_mode("global_validation")
        sequence = int(sequence)
        if sequence <= 0:
            raise ValueError("sequence must be a positive input sequence number.")
        delivered = _wait_for_delivery(sequence, timeout_seconds)
        if delivered is None:
            return fail(
                "error.timeout",
                f"Synthetic input sequence {sequence} was not consumed before the timeout.",
                hint="Check input_status and console_read. The editor may be blocked by a native modal dialog.",
            )
        return ok(delivered)


def perform_pointer_click(
    x: float,
    y: float,
    *,
    button: int = 0,
    timeout_seconds: float = 3.0,
    trace_name: str = "editor_ui_click",
    expected_target_id: str = "",
) -> dict:
    """Deliver a validated pointer move/down/up sequence through SDL input.

    ImGui buttons need to observe the press in one rendered frame before the
    release arrives.  Delivery to the SDL queue alone is not sufficient: a
    press/release pair consumed before one UI frame can leave standard
    ``ImGui::Button`` controls unclicked.
    """
    session.require_mode("global_validation")
    button = _validate_mouse_button(button)
    _require_finite("x", x)
    _require_finite("y", y)

    before_move_frame = _current_rendered_gui_frame(timeout_seconds)
    if before_move_frame is None:
        return fail(
            "error.semantic_capture_unavailable",
            "Cannot safely perform a semantic pointer click before editor UI capture has rendered a frame.",
            hint="Wait for editor_ui_snapshot to return a captured frame, then retry the click.",
        )

    move = _queue_input(
        f"{trace_name}.move",
        lambda native: native.queue_synthetic_mouse_motion_input(float(x), float(y), 0.0, 0.0),
        arguments={"x": float(x), "y": float(y), "button": button},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )
    if not move.get("ok"):
        return move

    rendered_move = _wait_for_rendered_gui_frame(
        before_move_frame,
        timeout_seconds=timeout_seconds,
        minimum_input_sequence=int((move.get("data") or {}).get("sequence", 0)),
    )
    if rendered_move is None:
        return fail(
            "error.click_move_frame_barrier",
            "Synthetic mouse movement did not reach its rendered ImGui frame.",
            hint="Check editor_ui_snapshot and console_read before retrying.",
        ) | {"data": {"x": float(x), "y": float(y), "button": button, "move": move.get("data") or {}}}

    press = _queue_input(
        f"{trace_name}.press",
        lambda native: native.queue_synthetic_mouse_button_input(button, True, float(x), float(y)),
        arguments={"x": float(x), "y": float(y), "button": button, "pressed": True},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )
    if not press.get("ok"):
        return press

    rendered_press = _wait_for_rendered_gui_frame(
        int(rendered_move["frame"]),
        timeout_seconds=timeout_seconds,
        expected_target_id=expected_target_id if button == 0 else "",
        minimum_input_sequence=int((press.get("data") or {}).get("sequence", 0)),
    )
    if rendered_press is None:
        # Do not leave the Editor's input state holding a synthetic button if
        # semantic capture is interrupted by a modal, shutdown, or stalled UI.
        cleanup_release = _queue_input(
            f"{trace_name}.release_after_frame_barrier_failure",
            lambda native: native.queue_synthetic_mouse_button_input(button, False, float(x), float(y)),
            arguments={"x": float(x), "y": float(y), "button": button, "pressed": False},
            wait_for_delivery=True,
            timeout_seconds=timeout_seconds,
        )
        return fail(
            "error.click_frame_barrier",
            "Synthetic mouse press did not reach a rendered ImGui frame before release.",
            hint="Check editor_ui_snapshot and console_read. The Editor may be blocked by a native modal dialog.",
        ) | {
            "data": {
                "x": float(x),
                "y": float(y),
                "button": button,
                "move": move.get("data") or {},
                "press": press.get("data") or {},
                "cleanup_release": cleanup_release.get("data") or {},
            }
        }

    if expected_target_id and button == 0 and not _press_target_accepted(rendered_press):
        cleanup_release = _queue_input(
            f"{trace_name}.release_after_target_miss",
            lambda native: native.queue_synthetic_mouse_button_input(button, False, float(x), float(y)),
            arguments={"x": float(x), "y": float(y), "button": button, "pressed": False},
            wait_for_delivery=True,
            timeout_seconds=timeout_seconds,
        )
        return fail(
            "error.ui_target_not_activated",
            "The synthetic press reached a rendered frame, but the requested interactive target was not activated.",
            hint="Refresh editor_ui_snapshot; the semantic click point may be covered by another item in the same panel.",
        ) | {
            "data": {
                "expected_target_id": expected_target_id,
                "press_frame": rendered_press,
                "cleanup_release": cleanup_release.get("data") or {},
            }
        }

    release = _queue_input(
        f"{trace_name}.release",
        lambda native: native.queue_synthetic_mouse_button_input(button, False, float(x), float(y)),
        arguments={"x": float(x), "y": float(y), "button": button, "pressed": False},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )
    if not release.get("ok"):
        return release

    rendered_release = _wait_for_rendered_gui_frame(
        int(rendered_press["frame"]),
        timeout_seconds=timeout_seconds,
        minimum_input_sequence=int((release.get("data") or {}).get("sequence", 0)),
    )
    if rendered_release is None:
        return fail(
            "error.click_release_frame_barrier",
            "Synthetic mouse release was delivered but did not reach a rendered ImGui frame.",
            hint="Check editor_ui_snapshot and the expected domain state before retrying.",
        ) | {
            "data": {
                "x": float(x),
                "y": float(y),
                "button": button,
                "move": move.get("data") or {},
                "press": press.get("data") or {},
                "release": release.get("data") or {},
            }
        }

    return ok({
        "x": float(x),
        "y": float(y),
        "button": button,
        "move_sequence": int((move.get("data") or {}).get("sequence", 0)),
        "press_sequence": int((press.get("data") or {}).get("sequence", 0)),
        "release_sequence": int((release.get("data") or {}).get("sequence", 0)),
        "move_render_frame": int(rendered_move["frame"]),
        "press_render_frame": int(rendered_press["frame"]),
        "release_render_frame": int(rendered_release["frame"]),
        "press_target_active": bool(rendered_press.get("target_active")) if expected_target_id else None,
        "press_frame": rendered_press if expected_target_id else None,
        "delivered": True,
    })


def perform_pointer_drag(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    *,
    button: int = 0,
    steps: int = 8,
    timeout_seconds: float = 5.0,
    trace_name: str = "editor_ui_drag",
    expected_target_id: str = "",
) -> dict:
    """Drag between two rendered points with an ImGui frame barrier at each phase."""
    session.require_mode("global_validation")
    button = _validate_mouse_button(button)
    for name, value in (
        ("start_x", start_x),
        ("start_y", start_y),
        ("end_x", end_x),
        ("end_y", end_y),
    ):
        _require_finite(name, value)
    step_count = int(steps)
    if step_count < 1 or step_count > 32:
        raise ValueError("steps must be between 1 and 32.")

    frame = _current_rendered_gui_frame(timeout_seconds)
    if frame is None:
        return fail(
            "error.semantic_capture_unavailable",
            "Cannot safely start a semantic drag before editor UI capture has rendered a frame.",
            hint="Wait for editor_ui_snapshot to return a captured frame, then retry the drag.",
        )

    def _release(x: float, y: float, suffix: str) -> dict:
        return _queue_input(
            f"{trace_name}.{suffix}",
            lambda native: native.queue_synthetic_mouse_button_input(button, False, float(x), float(y)),
            arguments={"x": float(x), "y": float(y), "button": button, "pressed": False},
            wait_for_delivery=True,
            timeout_seconds=timeout_seconds,
        )

    move = _queue_input(
        f"{trace_name}.move_to_start",
        lambda native: native.queue_synthetic_mouse_motion_input(float(start_x), float(start_y), 0.0, 0.0),
        arguments={"x": float(start_x), "y": float(start_y), "delta_x": 0.0, "delta_y": 0.0},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )
    if not move.get("ok"):
        return move

    moved_frame = _wait_for_rendered_gui_frame(
        frame,
        timeout_seconds=timeout_seconds,
        minimum_input_sequence=int((move.get("data") or {}).get("sequence", 0)),
    )
    if moved_frame is None:
        return fail(
            "error.drag_move_frame_barrier",
            "Synthetic drag start movement did not reach a rendered ImGui frame.",
            hint="Check editor_ui_snapshot and console_read before retrying.",
        )
    frame = int(moved_frame["frame"])

    press = _queue_input(
        f"{trace_name}.press",
        lambda native: native.queue_synthetic_mouse_button_input(button, True, float(start_x), float(start_y)),
        arguments={"x": float(start_x), "y": float(start_y), "button": button, "pressed": True},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )
    if not press.get("ok"):
        return press

    pressed_frame = _wait_for_rendered_gui_frame(
        frame,
        timeout_seconds=timeout_seconds,
        expected_target_id=expected_target_id,
        minimum_input_sequence=int((press.get("data") or {}).get("sequence", 0)),
    )
    if pressed_frame is None:
        cleanup = _release(start_x, start_y, "release_after_press_barrier_failure")
        return fail(
            "error.drag_press_frame_barrier",
            "Synthetic drag press did not reach a rendered ImGui frame.",
            hint="Check editor_ui_snapshot and console_read before retrying.",
        ) | {"data": {"cleanup_release": cleanup.get("data") or {}}}

    if expected_target_id and not _drag_press_target_accepted(pressed_frame, button):
        cleanup = _release(start_x, start_y, "release_after_target_miss")
        return fail(
            "error.ui_drag_target_not_reachable",
            "The drag press reached a rendered frame, but the requested semantic target was not reachable.",
            hint="Refresh editor_ui_snapshot and retry from the current target and snapshot IDs.",
        ) | {
            "data": {
                "expected_target_id": expected_target_id,
                "button": button,
                "press_frame": pressed_frame,
                "cleanup_release": cleanup.get("data") or {},
            }
        }

    current_frame = int(pressed_frame["frame"])
    previous_x = float(start_x)
    previous_y = float(start_y)
    motion_sequences: list[int] = []
    motion_frames: list[int] = []
    for index in range(1, step_count + 1):
        fraction = index / step_count
        x = float(start_x) + (float(end_x) - float(start_x)) * fraction
        y = float(start_y) + (float(end_y) - float(start_y)) * fraction
        motion = _queue_input(
            f"{trace_name}.motion.{index}",
            lambda native, px=x, py=y, dx=x - previous_x, dy=y - previous_y: native.queue_synthetic_mouse_motion_input(
                px, py, dx, dy
            ),
            arguments={"x": x, "y": y, "delta_x": x - previous_x, "delta_y": y - previous_y},
            wait_for_delivery=True,
            timeout_seconds=timeout_seconds,
        )
        if not motion.get("ok"):
            cleanup = _release(previous_x, previous_y, "release_after_motion_failure")
            return motion | {"data": {**dict(motion.get("data") or {}), "cleanup_release": cleanup.get("data") or {}}}
        rendered = _wait_for_rendered_gui_frame(
            current_frame,
            timeout_seconds=timeout_seconds,
            minimum_input_sequence=int((motion.get("data") or {}).get("sequence", 0)),
        )
        if rendered is None:
            cleanup = _release(x, y, "release_after_motion_barrier_failure")
            return fail(
                "error.drag_motion_frame_barrier",
                f"Synthetic drag motion {index} did not reach a rendered ImGui frame.",
                hint="Check editor_ui_snapshot and console_read before retrying.",
            ) | {"data": {"step": index, "cleanup_release": cleanup.get("data") or {}}}
        current_frame = int(rendered["frame"])
        motion_sequences.append(int((motion.get("data") or {}).get("sequence", 0)))
        motion_frames.append(current_frame)
        previous_x, previous_y = x, y

    release = _release(end_x, end_y, "release")
    if not release.get("ok"):
        return release
    released_frame = _wait_for_rendered_gui_frame(
        current_frame,
        timeout_seconds=timeout_seconds,
        minimum_input_sequence=int((release.get("data") or {}).get("sequence", 0)),
    )
    return ok({
        "start": [float(start_x), float(start_y)],
        "end": [float(end_x), float(end_y)],
        "button": button,
        "steps": step_count,
        "move_sequence": int((move.get("data") or {}).get("sequence", 0)),
        "press_sequence": int((press.get("data") or {}).get("sequence", 0)),
        "press_render_frame": int(pressed_frame["frame"]),
        "press_target_reachable": _drag_press_target_accepted(pressed_frame, button)
        if expected_target_id
        else None,
        "press_frame": pressed_frame if expected_target_id else None,
        "motion_sequences": motion_sequences,
        "motion_render_frames": motion_frames,
        "release_sequence": int((release.get("data") or {}).get("sequence", 0)),
        "release_render_frame": int((released_frame or {}).get("frame", 0)),
        "delivered": True,
    })


def perform_window_close_request(
    *,
    wait_for_delivery: bool = True,
    timeout_seconds: float = 3.0,
) -> dict:
    """Deliver an OS-equivalent close request without bypassing Editor policy."""
    session.require_mode("global_validation")
    return _queue_input(
        "input_window_close",
        lambda native: native.queue_synthetic_close_request(),
        arguments={},
        wait_for_delivery=wait_for_delivery,
        timeout_seconds=timeout_seconds,
    )


def perform_modifier_pointer_click(
    modifier: str | int,
    x: float,
    y: float,
    *,
    button: int = 0,
    timeout_seconds: float = 3.0,
    trace_name: str = "editor_ui_modifier_click",
    keep_modifier_pressed: bool = False,
    expected_target_id: str = "",
) -> dict:
    """Click while one physical modifier remains pressed through SDL."""
    button = _validate_mouse_button(button)
    _require_finite("x", x)
    _require_finite("y", y)

    press = perform_key_transition(
        modifier,
        True,
        timeout_seconds=timeout_seconds,
        trace_name=f"{trace_name}.modifier_press",
    )
    if not press.get("ok"):
        return press

    click = perform_pointer_click(
        float(x),
        float(y),
        button=button,
        timeout_seconds=timeout_seconds,
        trace_name=f"{trace_name}.pointer",
        expected_target_id=expected_target_id,
    )
    if not click.get("ok"):
        perform_key_transition(
            modifier,
            False,
            timeout_seconds=timeout_seconds,
            trace_name=f"{trace_name}.modifier_release_after_failure",
        )
        return click

    if keep_modifier_pressed:
        return ok({
            "modifier": modifier,
            "modifier_press_sequence": int((press.get("data") or {}).get("sequence", 0)),
            "pointer": click.get("data") or {},
            "modifier_held": True,
            "delivered": True,
        })

    release = perform_key_transition(
        modifier,
        False,
        timeout_seconds=timeout_seconds,
        trace_name=f"{trace_name}.modifier_release",
    )
    if not release.get("ok"):
        return release

    return ok({
        "modifier": modifier,
        "modifier_press_sequence": int((press.get("data") or {}).get("sequence", 0)),
        "pointer": click.get("data") or {},
        "modifier_release_sequence": int((release.get("data") or {}).get("sequence", 0)),
        "delivered": True,
    })


def perform_key_transition(
    key: str | int,
    pressed: bool,
    *,
    timeout_seconds: float = 3.0,
    trace_name: str = "input_key_transition",
) -> dict:
    """Queue one key state transition for composed human-equivalent input."""
    session.require_mode("global_validation")
    scancode = _resolve_scancode(key)
    return _queue_input(
        trace_name,
        lambda native: native.queue_synthetic_key_input(scancode, bool(pressed), False),
        arguments={"key": key, "scancode": scancode, "pressed": bool(pressed)},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )


def perform_mouse_button_transition(
    button: int,
    pressed: bool,
    *,
    x: float = -10_000.0,
    y: float = -10_000.0,
    timeout_seconds: float = 3.0,
    trace_name: str = "input_mouse_button_transition",
) -> dict:
    """Queue one gameplay mouse-button transition for composed input."""
    session.require_mode("global_validation")
    button = _validate_mouse_button(button)
    _require_finite("x", x)
    _require_finite("y", y)
    return _queue_input(
        trace_name,
        lambda native: native.queue_synthetic_mouse_button_input(
            button, bool(pressed), float(x), float(y)
        ),
        arguments={
            "button": button,
            "pressed": bool(pressed),
            "x": float(x),
            "y": float(y),
        },
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )


def perform_pointer_move(
    x: float,
    y: float,
    *,
    delta_x: float = 0.0,
    delta_y: float = 0.0,
    timeout_seconds: float = 3.0,
    trace_name: str = "editor_ui_hover",
) -> dict:
    """Move through SDL without clicking, for menus and hover-only controls."""
    session.require_mode("global_validation")
    _require_finite("x", x)
    _require_finite("y", y)
    _require_finite("delta_x", delta_x)
    _require_finite("delta_y", delta_y)
    return _queue_input(
        trace_name,
        lambda native: native.queue_synthetic_mouse_motion_input(
            float(x), float(y), float(delta_x), float(delta_y)
        ),
        arguments={"x": float(x), "y": float(y), "delta_x": float(delta_x), "delta_y": float(delta_y)},
        wait_for_delivery=True,
        timeout_seconds=timeout_seconds,
    )


def perform_key_chord(
    keys: list[str | int],
    *,
    timeout_seconds: float = 3.0,
    trace_name: str = "input_key_chord",
) -> dict:
    """Press keys in order and release them in reverse through SDL input."""
    session.require_mode("global_validation")
    values = list(keys or [])
    if not values:
        raise ValueError("keys must contain at least one key name or SDL scancode.")
    if len(values) > 8:
        raise ValueError("keys must contain at most 8 entries.")
    scancodes = [_resolve_scancode(value) for value in values]
    press_sequences: list[int] = []
    release_sequences: list[int] = []

    for index, scancode in enumerate(scancodes):
        result = _queue_input(
            f"{trace_name}.press.{index}",
            lambda native, code=scancode: native.queue_synthetic_key_input(code, True, False),
            arguments={"key": values[index], "scancode": scancode, "pressed": True},
            wait_for_delivery=True,
            timeout_seconds=timeout_seconds,
        )
        if not result.get("ok"):
            return result
        press_sequences.append(int((result.get("data") or {}).get("sequence", 0)))

    for index, scancode in reversed(list(enumerate(scancodes))):
        result = _queue_input(
            f"{trace_name}.release.{index}",
            lambda native, code=scancode: native.queue_synthetic_key_input(code, False, False),
            arguments={"key": values[index], "scancode": scancode, "pressed": False},
            wait_for_delivery=True,
            timeout_seconds=timeout_seconds,
        )
        if not result.get("ok"):
            return result
        release_sequences.append(int((result.get("data") or {}).get("sequence", 0)))

    return ok({
        "keys": values,
        "scancodes": scancodes,
        "press_sequences": press_sequences,
        "release_sequences": release_sequences,
        "delivered": True,
    })


def perform_text_input(
    text: str,
    *,
    wait_for_delivery: bool = True,
    timeout_seconds: float = 3.0,
    trace_name: str = "input_text",
) -> dict:
    """Send UTF-8 text through the focused Editor control's SDL path."""
    session.require_mode("global_validation")
    text = str(text or "")
    if not text:
        raise ValueError("text must not be empty.")
    if len(text.encode("utf-8")) > 4096:
        raise ValueError("text must be at most 4096 UTF-8 bytes.")
    return _queue_input(
        trace_name,
        lambda native: native.queue_synthetic_text_input(text),
        arguments={"text": text},
        wait_for_delivery=wait_for_delivery,
        timeout_seconds=timeout_seconds,
    )


def _queue_input(
    tool_name: str,
    enqueue,
    *,
    arguments: dict[str, Any],
    wait_for_delivery: bool,
    timeout_seconds: float,
) -> dict:
    session.require_mode("global_validation")

    def _submit() -> dict[str, int]:
        native = _native_engine()
        native.request_full_speed_frame()
        sequence = int(enqueue(native) or 0)
        if sequence <= 0:
            raise RuntimeError("The native synthetic input queue rejected the event.")
        return {"sequence": sequence, **_native_status_from(native)}

    result = main_thread(tool_name, _submit, arguments=arguments)
    if not result.get("ok") or not wait_for_delivery:
        return result

    sequence = int((result.get("data") or {}).get("sequence", 0))
    delivered = _wait_for_delivery(sequence, timeout_seconds)
    if delivered is None:
        return fail(
            "error.timeout",
            f"Synthetic input sequence {sequence} was queued but not consumed before the timeout.",
            hint="Check input_status and console_read. The editor may be blocked by a native modal dialog.",
        )
    result["data"].update(delivered)
    return result


def _wait_for_delivery(sequence: int, timeout_seconds: float) -> dict[str, int] | None:
    deadline = time.monotonic() + _timeout_seconds(timeout_seconds)
    last_status: dict[str, int] = {}
    while time.monotonic() < deadline:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000.0))
        try:
            last_status = MainThreadCommandQueue.instance().run_sync(
                "input_delivery_status",
                _native_status,
                timeout_ms=min(remaining_ms, 500),
            )
        except TimeoutError:
            time.sleep(0.01)
            continue
        if int(last_status.get("last_processed_sequence", 0)) >= sequence:
            return {
                "sequence": sequence,
                "delivered": True,
                **last_status,
            }
        time.sleep(0.01)
    return None


def _current_rendered_gui_frame(timeout_seconds: float) -> int | None:
    """Return the most recently published semantic UI frame, when available."""
    snapshot = _current_rendered_gui_status(timeout_seconds)
    if not snapshot or not bool(snapshot.get("capture_enabled")):
        return None
    frame = int(snapshot.get("frame", 0) or 0)
    return frame if frame > 0 else None


def _current_rendered_gui_status(timeout_seconds: float, *, expected_target_id: str = "") -> dict[str, Any] | None:
    """Read one immutable semantic frame and optional target activation state."""
    try:
        snapshot = MainThreadCommandQueue.instance().run_sync(
            "input_gui_frame_status",
            lambda: _native_gui_frame_status(expected_target_id),
            timeout_ms=min(max(1, int(_timeout_seconds(timeout_seconds) * 1000.0)), 500),
        )
    except TimeoutError:
        return None
    return dict(snapshot or {})


def _wait_for_rendered_gui_frame(
    previous_frame: int,
    *,
    timeout_seconds: float,
    expected_target_id: str = "",
    minimum_input_sequence: int = 0,
) -> dict[str, Any] | None:
    """Wait until a press has been visible to at least one completed UI frame."""
    deadline = time.monotonic() + _timeout_seconds(timeout_seconds)
    while time.monotonic() < deadline:
        status = _current_rendered_gui_status(
            min(deadline - time.monotonic(), 0.5),
            expected_target_id=expected_target_id,
        )
        current = int((status or {}).get("frame", 0) or 0)
        input_sequence = int((status or {}).get("input_sequence", 0) or 0)
        if current > previous_frame and input_sequence >= int(minimum_input_sequence):
            return status
        time.sleep(0.01)
    return None


def _native_status() -> dict[str, int]:
    return _native_status_from(_native_engine())


def _native_gui_frame_status(expected_target_id: str = "") -> dict[str, Any]:
    from Infernux.lib import get_gui_semantic_snapshot

    snapshot = dict(get_gui_semantic_snapshot() or {})
    targets = list(snapshot.get("targets", []) or [])

    def _target_id(item: dict[str, Any]) -> str:
        # Native snapshots expose ``id``; editor_ui's normalized representation
        # calls the same value ``target_id``.
        return str(item.get("target_id") or item.get("id") or "")

    exact_target = next(
        (item for item in targets if _target_id(item) == expected_target_id),
        None,
    )
    mouse = list(snapshot.get("mouse") or [])

    def _pointer_reachable(item: dict[str, Any]) -> bool:
        if not bool(item.get("visible")) or not bool(item.get("enabled")) or len(mouse) < 2:
            return False
        rect = list(item.get("rect") or [])
        if len(rect) < 4:
            return False
        mouse_x, mouse_y = float(mouse[0]), float(mouse[1])
        x, y, width, height = (float(value) for value in rect[:4])
        return x <= mouse_x <= x + width and y <= mouse_y <= y + height

    target = exact_target
    target_match = "target_id" if exact_target is not None else "none"
    semantic_matches: list[dict[str, Any]] = []
    eligible_semantic_matches: list[dict[str, Any]] = []
    if target is None and expected_target_id:
        # ImGui item/window IDs are frame-local implementation details. A
        # semantic ID is the stable identity intentionally exposed to MCP, so
        # accept it only when exactly one current target maps back to the
        # requested composite ID.
        semantic_matches = [
            item
            for item in targets
            if str(item.get("semantic_id") or "")
            and f':{str(item.get("semantic_id") or "")}:' in expected_target_id
        ]
        eligible_semantic_matches = [item for item in semantic_matches if _pointer_reachable(item)]
        if len(eligible_semantic_matches) == 1:
            target = eligible_semantic_matches[0]
            target_match = "semantic_id"

    def _target_summary(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "target_id": _target_id(item),
            "semantic_id": str(item.get("semantic_id") or ""),
            "kind": str(item.get("kind") or ""),
            "label": str(item.get("label") or ""),
            "rect": list(item.get("rect") or []),
            "active": bool(item.get("active")),
            "visible": bool(item.get("visible")),
            "enabled": bool(item.get("enabled")),
        }

    active_targets = [_target_summary(item) for item in targets if bool(item.get("active"))]
    pointer_targets: list[dict[str, Any]] = []
    if len(mouse) >= 2:
        mouse_x, mouse_y = float(mouse[0]), float(mouse[1])
        for item in targets:
            rect = list(item.get("rect") or [])
            if len(rect) < 4:
                continue
            x, y, width, height = (float(value) for value in rect[:4])
            if x <= mouse_x <= x + width and y <= mouse_y <= y + height:
                pointer_targets.append(_target_summary(item))
    target_under_pointer = any(
        str(item.get("target_id") or "") == _target_id(target or {})
        for item in pointer_targets
    )
    return {
        "capture_enabled": bool(snapshot.get("capture_enabled")),
        "frame": int(snapshot.get("frame", 0) or 0),
        "input_sequence": int(snapshot.get("input_sequence", 0) or 0),
        "mouse": mouse,
        "rendered_target_count": len(targets),
        "expected_target_id": expected_target_id,
        "target_match": target_match if expected_target_id else None,
        "target_id_matched": exact_target is not None if expected_target_id else None,
        "matched_target_id": _target_id(target or {}) if expected_target_id else "",
        "matched_semantic_id": str((target or {}).get("semantic_id") or "") if expected_target_id else "",
        "matched_target_kind": str((target or {}).get("kind") or "") if expected_target_id else "",
        "semantic_match_count": len(semantic_matches) if expected_target_id and exact_target is None else 0,
        "eligible_semantic_match_count": (
            len(eligible_semantic_matches) if expected_target_id and exact_target is None else 0
        ),
        "active_targets": active_targets[:16],
        "pointer_targets": pointer_targets[:16],
        "target_found": target is not None if expected_target_id else None,
        "target_active": bool((target or {}).get("active")) if expected_target_id else None,
        "target_visible": bool((target or {}).get("visible")) if expected_target_id else None,
        "target_enabled": bool((target or {}).get("enabled")) if expected_target_id else None,
        "target_under_pointer": target_under_pointer if expected_target_id else None,
    }


def _press_target_accepted(status: dict[str, Any]) -> bool:
    if bool(status.get("target_active")):
        return True
    reachable_without_active = {
        "menu",
        "menu_item",
        "node_graph_node",
        "node_graph_node_drag_handle",
        "node_graph_port",
        "node_graph_link",
        "game_ui_button",
    }
    return (
        str(status.get("matched_target_kind") or "") in reachable_without_active
        and bool(status.get("target_found"))
        and bool(status.get("target_visible"))
        and bool(status.get("target_enabled"))
        and bool(status.get("target_under_pointer"))
    )


def _drag_press_target_accepted(status: dict[str, Any], button: int) -> bool:
    if button == 0:
        return _press_target_accepted(status)
    return (
        bool(status.get("target_found"))
        and bool(status.get("target_visible"))
        and bool(status.get("target_enabled"))
        and bool(status.get("target_under_pointer"))
    )


def _native_status_from(native) -> dict[str, int]:
    return {
        "last_processed_sequence": int(native.last_processed_synthetic_input_sequence),
        "pending_event_count": int(native.pending_synthetic_input_count),
    }


def _native_engine():
    from Infernux.engine.bootstrap import EditorBootstrap

    bootstrap = EditorBootstrap.instance()
    engine = bootstrap.engine if bootstrap is not None else None
    native = engine.get_native_engine() if engine is not None else None
    if native is None:
        raise RuntimeError("Synthetic input requires a running graphical Editor session.")
    return native


_KEY_NAME_ALIASES = {
    "ctrl": 224,
    "control": 224,
    "shift": 225,
    "alt": 226,
    "option": 226,
    "cmd": 227,
    "command": 227,
    "super": 227,
    "win": 227,
    "windows": 227,
    "esc": 41,
}


def _resolve_scancode(key: str | int) -> int:
    if isinstance(key, bool):
        raise ValueError("key must be a key name or SDL scancode, not a boolean.")
    if isinstance(key, int):
        scancode = int(key)
    else:
        from Infernux.lib import InputManager

        key_name = str(key).strip()
        resolved_key = _KEY_NAME_ALIASES.get(key_name.casefold(), key_name)
        scancode = (
            int(resolved_key)
            if isinstance(resolved_key, int)
            else int(InputManager.name_to_scancode(resolved_key))
        )
    if scancode <= 0:
        raise ValueError(f"Unknown key: {key!r}.")
    return scancode


def _validate_mouse_button(button: int) -> int:
    if isinstance(button, bool) or int(button) not in range(5):
        raise ValueError("button must use Unity button indices 0 through 4.")
    return int(button)


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite.")


def _timeout_seconds(value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout_seconds must be finite and positive.")
    return min(timeout, 30.0)


def _register_metadata() -> None:
    entries = (
        ("input_key", "Send one keyboard press or release through the editor event queue."),
        ("input_key_chord", "Press a human-equivalent keyboard chord through the editor event queue."),
        ("input_pointer_move", "Move the editor pointer using window coordinates."),
        ("input_mouse_button", "Send one mouse press or release through the editor event queue."),
        ("input_mouse_wheel", "Send one mouse-wheel event through the editor event queue."),
        ("input_text", "Send UTF-8 text to the focused editor control."),
        ("input_window_close", "Request Editor window close through its normal intercepted close path."),
        ("input_status", "Read synthetic input queue and delivery progress."),
        ("input_wait", "Wait for an input event to reach the graphical event loop."),
    )
    for name, summary in entries:
        register_tool_metadata(
            name,
            summary=summary,
            category="global_validation/input",
            side_effects=[] if name in {"input_status", "input_wait"} else ["Queues a human-equivalent input event."],
            preconditions=["Requires global_validation mode and a running graphical Editor."],
            recovery=["Use input_status and console_read when delivery does not complete."],
            risk_level="medium" if name not in {"input_status", "input_wait"} else "low",
        )
