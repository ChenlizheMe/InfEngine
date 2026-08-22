"""Read-only semantic editor UI targeting for human-equivalent validation."""

from __future__ import annotations

import math
import time
from typing import Annotated, Any, Callable

from pydantic import Field

from Infernux.mcp import session
from Infernux.mcp.tools import input as input_tools
from Infernux.mcp.tools.common import fail, main_thread, ok, register_tool_metadata


_DIRECT_TEXT_TARGET_KINDS = {
    "text_input",
    "text_area",
    "int_input",
    "uint_input",
    "float_input",
    "component_search",
    "hierarchy_rename",
    "hierarchy_search",
    "inspector_name",
}
_NUMERIC_TEXT_TARGET_KINDS = {
    "drag_float",
    "drag_int",
    "float_slider",
    "int_slider",
    "vector_axis",
}
_TEXT_TARGET_KINDS = _DIRECT_TEXT_TARGET_KINDS | _NUMERIC_TEXT_TARGET_KINDS


def set_semantic_capture_enabled(enabled: bool) -> bool:
    """Toggle native capture without making an Editor-state mutation."""
    try:
        from Infernux.lib import set_gui_semantic_capture_enabled

        set_gui_semantic_capture_enabled(bool(enabled))
        return True
    except (AttributeError, ImportError):
        return False


def request_semantic_snapshot() -> int:
    """Ask the render thread to publish one fresh semantic frame."""
    try:
        from Infernux.lib import request_gui_semantic_snapshot

        return int(request_gui_semantic_snapshot() or 0)
    except (AttributeError, ImportError):
        return 0


def register_editor_ui_tools(mcp) -> None:
    """Register read-only targeting plus input-routed interaction helpers."""
    _register_metadata()

    @mcp.tool(name="editor_ui_snapshot")
    def editor_ui_snapshot(
        label: Annotated[
            str,
            Field(
                description=(
                    "Optional case-insensitive substring filter over target labels and IDs. "
                    "Leave empty to return targets from every label; this is not a snapshot title."
                )
            ),
        ] = "",
        kind: Annotated[
            str,
            Field(description="Optional exact target-kind filter. Leave empty to include every rendered kind."),
        ] = "",
        window: Annotated[
            str,
            Field(description="Optional case-insensitive window name/ID filter. Leave empty to include every window."),
        ] = "",
        semantic_id: Annotated[
            str,
            Field(description="Optional exact semantic ID filter. Leave empty to include every semantic target."),
        ] = "",
        visible_only: Annotated[
            bool,
            Field(description="When true, omit targets that the current ImGui frame marks as not visible."),
        ] = True,
        limit: Annotated[
            int,
            Field(ge=1, le=2000, description="Maximum number of matching targets returned."),
        ] = 500,
    ) -> dict:
        """Read controls rendered by the latest Editor frame, optionally filtering targets."""
        session.require_mode("global_validation")
        return _semantic_main_thread(
            "editor_ui_snapshot",
            lambda: _snapshot_payload(
                label=label,
                kind=kind,
                window=window,
                semantic_id=semantic_id,
                visible_only=visible_only,
                limit=limit,
            ),
            arguments={
                "label": label,
                "kind": kind,
                "window": window,
                "semantic_id": semantic_id,
                "visible_only": bool(visible_only),
                "limit": int(limit),
            },
        )

    @mcp.tool(name="editor_ui_capture_status")
    def editor_ui_capture_status() -> dict:
        """Read native semantic-capture sequencing without requesting another frame."""
        session.require_mode("global_validation")
        raw = _read_native_snapshot()
        return ok(
            {
                "capture_enabled": bool(raw.get("capture_enabled")),
                "published_frame": int(raw.get("frame", 0) or 0),
                "published_request_sequence": int(raw.get("request_sequence", 0) or 0),
                "capture_state": dict(raw.get("capture_state") or {}),
            }
        )

    @mcp.tool(name="editor_ui_wait_for_target")
    def editor_ui_wait_for_target(
        label: str = "",
        kind: str = "",
        window: str = "",
        semantic_id: str = "",
        visible_only: bool = True,
        timeout_seconds: float = 5.0,
        poll_interval: float = 0.10,
    ) -> dict:
        """Wait for a rendered target to appear after a human-equivalent action."""
        session.require_mode("global_validation")
        timeout = _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
        interval = _positive_finite("poll_interval", poll_interval, maximum=2.0)
        return _wait_for_snapshot_target(
            operation="editor_ui_wait_for_target",
            label=label,
            kind=kind,
            window=window,
            semantic_id=semantic_id,
            visible_only=visible_only,
            timeout_seconds=timeout,
            poll_interval=interval,
        )

    @mcp.tool(name="editor_ui_wait_for_window_focus")
    def editor_ui_wait_for_window_focus(
        window_id: str,
        timeout_seconds: float = 5.0,
        poll_interval: float = 0.05,
    ) -> dict:
        """Wait until an Editor window or one of its child regions owns keyboard focus."""
        session.require_mode("global_validation")
        target_window_id = _required_window_id("window_id", window_id)
        timeout = _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
        interval = _positive_finite("poll_interval", poll_interval, maximum=2.0)
        return _wait_for_window_focus(
            operation="editor_ui_wait_for_window_focus",
            window_id=target_window_id,
            timeout_seconds=timeout,
            poll_interval=interval,
        )

    @mcp.tool(name="editor_ui_open_menu")
    def editor_ui_open_menu(
        menu_semantic_id: str,
        item_semantic_id: str,
        timeout_seconds: float = 3.0,
        poll_interval: float = 0.05,
    ) -> dict:
        """Ensure a known menu item is rendered without toggling an already-open menu closed."""
        session.require_mode("global_validation")
        menu_id = _required_semantic_id("menu_semantic_id", menu_semantic_id)
        item_id = _required_semantic_id("item_semantic_id", item_semantic_id)
        timeout = _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
        interval = _positive_finite("poll_interval", poll_interval, maximum=2.0)

        existing = _read_snapshot_target(
            operation="editor_ui_open_menu.check_item",
            semantic_id=item_id,
            kind="menu_item",
        )
        if not existing.get("ok"):
            return existing
        existing_payload = dict(existing.get("data") or {})
        existing_targets = list(existing_payload.get("targets") or [])
        if existing_targets:
            return ok({
                "menu_semantic_id": menu_id,
                "item_semantic_id": item_id,
                "item": existing_targets[0],
                "snapshot_id": str(existing_payload.get("snapshot_id") or ""),
                "already_open": True,
                "action_path": "none",
            })

        menu_snapshot = _read_snapshot_target(
            operation="editor_ui_open_menu.find_menu",
            semantic_id=menu_id,
            kind="menu",
        )
        if not menu_snapshot.get("ok"):
            return menu_snapshot
        menu_payload = dict(menu_snapshot.get("data") or {})
        menu_targets = list(menu_payload.get("targets") or [])
        if not menu_targets:
            return fail(
                "error.ui_target_not_found",
                f"The menu '{menu_id}' is not rendered in the latest editor UI frame.",
                hint="Call editor_ui_snapshot to inspect the active editor layout and use an exact menu semantic ID.",
            ) | {"data": menu_payload}

        menu_target = dict(menu_targets[0])
        resolved = _resolve_target(str(menu_target.get("target_id") or ""), str(menu_payload.get("snapshot_id") or ""))
        if not resolved.get("found"):
            return fail(
                "error.stale_ui_target",
                str(resolved.get("reason") or "The requested menu is no longer rendered."),
                hint="Refresh editor_ui_snapshot and retry with the latest menu target.",
            ) | {"data": resolved}

        input_result = input_tools.perform_pointer_click(
            float(resolved["center_x"]),
            float(resolved["center_y"]),
            button=0,
            timeout_seconds=timeout,
            trace_name="editor_ui_open_menu",
            expected_target_id=str(resolved.get("target_id") or ""),
        )
        if not input_result.get("ok"):
            return input_result

        appeared = _wait_for_snapshot_target(
            operation="editor_ui_open_menu.wait_for_item",
            label="",
            kind="menu_item",
            window="",
            semantic_id=item_id,
            visible_only=True,
            timeout_seconds=timeout,
            poll_interval=interval,
        )
        if not appeared.get("ok"):
            return appeared | {
                "data": {
                    **dict(appeared.get("data") or {}),
                    "menu_semantic_id": menu_id,
                    "item_semantic_id": item_id,
                    "menu": resolved,
                    "input": input_result.get("data") or {},
                }
            }
        appeared_payload = dict(appeared.get("data") or {})
        return ok({
            "menu_semantic_id": menu_id,
            "item_semantic_id": item_id,
            "menu": resolved,
            "item": list(appeared_payload.get("targets") or [None])[0],
            "snapshot_id": str(appeared_payload.get("snapshot_id") or ""),
            "already_open": False,
            "input": input_result.get("data") or {},
            "action_path": "synthetic_sdl_pointer",
        })

    @mcp.tool(name="editor_ui_click")
    def editor_ui_click(
        target_id: str,
        snapshot_id: str,
        button: str | int = "left",
        modifier: str | int | None = None,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Click through SDL, optionally holding one modifier across rendered frames."""
        session.require_mode("global_validation")
        button_index = _pointer_button(button)
        resolved = _resolve_target(target_id, snapshot_id)
        if not resolved.get("found"):
            return _target_resolution_failure(
                resolved,
                fallback="The requested target is no longer rendered.",
                refresh_hint="Call editor_ui_snapshot again and use the latest target_id/snapshot_id pair.",
            )

        before = _read_interaction_observation()

        if modifier is None or str(modifier).strip() == "":
            input_result = input_tools.perform_pointer_click(
                float(resolved["center_x"]),
                float(resolved["center_y"]),
                button=button_index,
                timeout_seconds=timeout_seconds,
                trace_name="editor_ui_click",
                expected_target_id=str(resolved.get("target_id") or ""),
            )
            action_path = "synthetic_sdl_pointer"
        else:
            input_result = input_tools.perform_modifier_pointer_click(
                modifier,
                float(resolved["center_x"]),
                float(resolved["center_y"]),
                button=button_index,
                timeout_seconds=timeout_seconds,
                trace_name="editor_ui_click",
                expected_target_id=str(resolved.get("target_id") or ""),
            )
            action_path = "synthetic_sdl_modifier_pointer"
        if not input_result.get("ok"):
            return input_result
        post_action = _wait_for_post_action_observation(
            before,
            source_target_id=str(resolved.get("target_id") or ""),
            timeout_seconds=min(_positive_finite("timeout_seconds", timeout_seconds, maximum=30.0), 0.25),
        )
        return ok({
            "target": resolved,
            "button": button_index,
            "modifier": modifier,
            "input": input_result.get("data") or {},
            "post_action": post_action,
            "action_path": action_path,
        })

    @mcp.tool(name="editor_ui_double_click")
    def editor_ui_double_click(target_id: str, snapshot_id: str, timeout_seconds: float = 3.0) -> dict:
        """Double-click one target with a rendered-frame barrier between SDL clicks."""
        session.require_mode("global_validation")
        timeout = _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
        resolved = _resolve_target(target_id, snapshot_id)
        if not resolved.get("found"):
            return _target_resolution_failure(
                resolved,
                fallback="The requested target is no longer rendered.",
                refresh_hint="Call editor_ui_snapshot again and use the latest target_id/snapshot_id pair.",
            )

        first_click = input_tools.perform_pointer_click(
            float(resolved["center_x"]),
            float(resolved["center_y"]),
            button=0,
            timeout_seconds=timeout,
            trace_name="editor_ui_double_click.first",
            expected_target_id=str(resolved.get("target_id") or ""),
        )
        if not first_click.get("ok"):
            return first_click

        frame_barrier = _wait_for_rendered_target_after_click(
            target=resolved,
            previous_snapshot_id=str(resolved.get("snapshot_id") or ""),
            timeout_seconds=min(timeout, 0.25),
        )
        if not frame_barrier.get("ok"):
            return frame_barrier | {
                "data": {
                    "target": resolved,
                    "first_click": first_click.get("data") or {},
                    "previous_snapshot_id": str(resolved.get("snapshot_id") or ""),
                    "frame_barrier": frame_barrier.get("data") or {},
                }
            }

        barrier_data = dict(frame_barrier.get("data") or {})
        fresh_target = dict(barrier_data.get("target") or {})
        fresh_snapshot_id = str(barrier_data.get("snapshot_id") or "")
        second_target = _resolve_target(str(fresh_target.get("target_id") or ""), fresh_snapshot_id)
        if not second_target.get("found"):
            return fail(
                "error.stale_ui_target",
                str(second_target.get("reason") or "The target changed before the second click."),
                hint="Call editor_ui_snapshot and retry the double-click from a stable rendered target.",
            ) | {
                "data": {
                    "target": resolved,
                    "first_click": first_click.get("data") or {},
                    "frame_barrier": barrier_data,
                }
            }

        second_click = input_tools.perform_pointer_click(
            float(second_target["center_x"]),
            float(second_target["center_y"]),
            button=0,
            timeout_seconds=timeout,
            trace_name="editor_ui_double_click.second",
            expected_target_id=str(second_target.get("target_id") or ""),
        )
        if not second_click.get("ok"):
            return second_click
        return ok({
            "target": second_target,
            "first_target": resolved,
            "button": 0,
            "first_click": first_click.get("data") or {},
            "second_click": second_click.get("data") or {},
            "frame_barrier": {
                "previous_snapshot_id": str(resolved.get("snapshot_id") or ""),
                "snapshot_id": fresh_snapshot_id,
                "target": fresh_target,
            },
            "action_path": "synthetic_sdl_pointer_double_click",
        })

    @mcp.tool(name="editor_ui_drag")
    def editor_ui_drag(
        target_id: str,
        snapshot_id: str,
        destination_target_id: str = "",
        destination_snapshot_id: str = "",
        delta_x: float = 0.0,
        delta_y: float = 0.0,
        steps: int = 8,
        timeout_seconds: float = 5.0,
        button: str | int = "left",
    ) -> dict:
        """Drag a rendered target with a named human mouse button to another target or relative offset."""
        session.require_mode("global_validation")
        timeout = _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
        button_index = _pointer_button(button)
        source = _resolve_target(target_id, snapshot_id)
        if not source.get("found"):
            return fail(
                "error.stale_ui_target",
                str(source.get("reason") or "The drag source is no longer rendered."),
                hint="Refresh editor_ui_snapshot and retry with a current source target.",
            ) | {"data": {"source": source}}

        destination_id = str(destination_target_id or "").strip()
        destination = None
        if destination_id:
            destination = _resolve_target(
                destination_id,
                str(destination_snapshot_id or snapshot_id),
            )
            if not destination.get("found"):
                return fail(
                    "error.stale_ui_target",
                    str(destination.get("reason") or "The drag destination is no longer rendered."),
                    hint="Refresh editor_ui_snapshot and retry with current source and destination targets.",
                ) | {"data": {"source": source, "destination": destination}}
            end_x = float(destination["center_x"])
            end_y = float(destination["center_y"])
        else:
            if not math.isfinite(float(delta_x)) or not math.isfinite(float(delta_y)):
                raise ValueError("delta_x and delta_y must be finite.")
            if float(delta_x) == 0.0 and float(delta_y) == 0.0:
                raise ValueError("Provide destination_target_id or a non-zero relative drag offset.")
            end_x = float(source["center_x"]) + float(delta_x)
            end_y = float(source["center_y"]) + float(delta_y)

        before = _read_interaction_observation()
        input_result = input_tools.perform_pointer_drag(
            float(source["center_x"]),
            float(source["center_y"]),
            end_x,
            end_y,
            button=button_index,
            steps=int(steps),
            timeout_seconds=timeout,
            trace_name="editor_ui_drag",
            expected_target_id=str(source.get("target_id") or ""),
        )
        if not input_result.get("ok"):
            return input_result
        post_action = _wait_for_post_action_observation(
            before,
            source_target_id=str(source.get("target_id") or ""),
            timeout_seconds=min(timeout, 0.25),
        )
        return ok({
            "source": source,
            "destination": destination,
            "relative_offset": [float(delta_x), float(delta_y)] if destination is None else None,
            "button": button_index,
            "input": input_result.get("data") or {},
            "post_action": post_action,
            "action_path": "synthetic_sdl_semantic_pointer_drag",
        })

    @mcp.tool(name="editor_ui_set_checkbox")
    def editor_ui_set_checkbox(
        target_id: str,
        snapshot_id: str,
        checked: bool,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Set a rendered checkbox to the requested state without blind toggling."""
        session.require_mode("global_validation")
        timeout = _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
        resolved = _resolve_target(target_id, snapshot_id)
        if not resolved.get("found"):
            return fail(
                "error.stale_ui_target",
                str(resolved.get("reason") or "The requested target is no longer rendered."),
                hint="Call editor_ui_snapshot again and use the latest target_id/snapshot_id pair.",
            ) | {"data": resolved}
        if str(resolved.get("kind") or "") != "checkbox":
            return fail(
                "error.invalid_target",
                "editor_ui_set_checkbox only accepts rendered checkbox targets.",
                hint="Use editor_ui_click for controls that do not expose a boolean value.",
            ) | {"data": resolved}
        if not bool(resolved.get("value_available")):
            return fail(
                "error.checkbox_value_unavailable",
                "The rendered checkbox did not expose its current boolean value.",
                hint="Add boolean semantic value capture to the widget before using idempotent checkbox control.",
            ) | {"data": resolved}

        desired = bool(checked)
        before = bool(resolved.get("value"))
        if before == desired:
            return ok({
                "target": resolved,
                "before": before,
                "checked": desired,
                "changed": False,
                "action_path": "semantic_checkbox_noop",
            })

        input_result = input_tools.perform_pointer_click(
            float(resolved["center_x"]),
            float(resolved["center_y"]),
            button=0,
            timeout_seconds=timeout,
            trace_name="editor_ui_set_checkbox",
            expected_target_id=str(resolved.get("target_id") or ""),
        )
        if not input_result.get("ok"):
            return input_result
        observed = _wait_for_checkbox_state(resolved, desired, timeout_seconds=timeout)
        if not bool(observed.get("value_available")) or bool(observed.get("value")) != desired:
            return fail(
                "error.checkbox_state_timeout",
                "The checkbox click was delivered, but the requested state was not observed.",
                hint="Refresh editor_ui_snapshot and inspect console_read before retrying.",
            ) | {
                "data": {
                    "target": resolved,
                    "before": before,
                    "checked": desired,
                    "input": input_result.get("data") or {},
                    "observed": observed,
                }
            }
        return ok({
            "target": observed,
            "before": before,
            "checked": desired,
            "changed": True,
            "input": input_result.get("data") or {},
            "action_path": "synthetic_sdl_idempotent_checkbox",
        })

    @mcp.tool(name="editor_ui_focus")
    def editor_ui_focus(target_id: str, snapshot_id: str, timeout_seconds: float = 3.0) -> dict:
        """Focus a rendered text field through the same left-click input path as a human."""
        session.require_mode("global_validation")
        resolved = _resolve_target(target_id, snapshot_id)
        if not resolved.get("found"):
            return fail(
                "error.stale_ui_target",
                str(resolved.get("reason") or "The requested target is no longer rendered."),
                hint="Call editor_ui_snapshot again and use the latest target_id/snapshot_id pair.",
            ) | {"data": resolved}
        if str(resolved.get("kind") or "") not in _TEXT_TARGET_KINDS:
            return fail(
                "error.invalid_target",
                "editor_ui_focus only accepts rendered text-entry or numeric-entry targets.",
                hint="Use editor_ui_click for a menu, button, selectable, or other non-text control.",
            ) | {"data": resolved}

        input_result, action_path, held_modifier = _focus_target(
            resolved,
            timeout_seconds=timeout_seconds,
            trace_name="editor_ui_focus",
        )
        if not input_result.get("ok"):
            return input_result
        focus_confirmation = _wait_for_target_focus(str(resolved["target_id"]), timeout_seconds=timeout_seconds)
        modifier_release = _release_focus_modifier(
            held_modifier,
            timeout_seconds=timeout_seconds,
            trace_name="editor_ui_focus",
        )
        if modifier_release is not None and not modifier_release.get("ok"):
            return modifier_release
        if not focus_confirmation.get("text_input_ready"):
            return fail(
                "error.timeout",
                "The text field did not become focused after the pointer click.",
                hint="Refresh editor_ui_snapshot. The target may be obscured, disabled, or blocked by another editor modal.",
            ) | {"data": {"target": resolved, "input": input_result.get("data") or {}, "focus": focus_confirmation}}
        return ok({
            "target": resolved,
            "input": input_result.get("data") or {},
            "focus": focus_confirmation,
            "modifier_release": (modifier_release or {}).get("data") or {},
            "action_path": action_path,
        })

    @mcp.tool(name="editor_ui_replace_text")
    def editor_ui_replace_text(
        target_id: str,
        snapshot_id: str,
        text: str,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Focus a text or numeric field, select all, then type replacement text through SDL."""
        session.require_mode("global_validation")
        resolved = _resolve_target(target_id, snapshot_id)
        if not resolved.get("found"):
            return fail(
                "error.stale_ui_target",
                str(resolved.get("reason") or "The requested target is no longer rendered."),
                hint="Call editor_ui_snapshot again and use the latest target_id/snapshot_id pair.",
            ) | {"data": resolved}
        if str(resolved.get("kind") or "") not in _TEXT_TARGET_KINDS:
            return fail(
                "error.invalid_target",
                "editor_ui_replace_text only accepts rendered text-entry or numeric-entry targets.",
                hint="Use editor_ui_click for a menu, button, selectable, or other non-text control.",
            ) | {"data": resolved}

        focus_result, action_path, held_modifier = _focus_target(
            resolved,
            timeout_seconds=timeout_seconds,
            trace_name="editor_ui_replace_text.focus",
        )
        if not focus_result.get("ok"):
            return focus_result
        focus_confirmation = _wait_for_target_focus(str(resolved["target_id"]), timeout_seconds=timeout_seconds)
        modifier_release = _release_focus_modifier(
            held_modifier,
            timeout_seconds=timeout_seconds,
            trace_name="editor_ui_replace_text.focus",
        )
        if modifier_release is not None and not modifier_release.get("ok"):
            return modifier_release
        if not focus_confirmation.get("text_input_ready"):
            return fail(
                "error.timeout",
                "The text field did not become focused after the pointer click.",
                hint="Refresh editor_ui_snapshot. The target may be obscured, disabled, or blocked by another editor modal.",
            ) | {"data": {"target": resolved, "focus": focus_result.get("data") or {}, "focus_confirmation": focus_confirmation}}
        chord_result = input_tools.perform_key_chord(
            ["Left Ctrl", "A"],
            timeout_seconds=timeout_seconds,
            trace_name="editor_ui_replace_text.select_all",
        )
        if not chord_result.get("ok"):
            return chord_result
        replacement = str(text or "")
        if replacement:
            text_result = input_tools.perform_text_input(
                replacement,
                timeout_seconds=timeout_seconds,
                trace_name="editor_ui_replace_text.type",
            )
            if not text_result.get("ok"):
                return text_result
            action_path = f"{action_path}_and_keyboard"
        else:
            text_result = input_tools.perform_key_chord(
                ["Delete"],
                timeout_seconds=timeout_seconds,
                trace_name="editor_ui_replace_text.clear",
            )
            if not text_result.get("ok"):
                return text_result
            action_path = f"{action_path}_and_keyboard_clear"
        return ok({
            "target": resolved,
            "text_length": len(replacement),
            "focus": focus_result.get("data") or {},
            "focus_confirmation": focus_confirmation,
            "modifier_release": (modifier_release or {}).get("data") or {},
            "select_all": chord_result.get("data") or {},
            "input": text_result.get("data") or {},
            "action_path": action_path,
        })

    @mcp.tool(name="editor_ui_hover")
    def editor_ui_hover(target_id: str, snapshot_id: str, timeout_seconds: float = 3.0) -> dict:
        """Move the pointer over a rendered target without invoking its action."""
        session.require_mode("global_validation")
        resolved = _resolve_target(target_id, snapshot_id)
        if not resolved.get("found"):
            return fail(
                "error.stale_ui_target",
                str(resolved.get("reason") or "The requested target is no longer rendered."),
                hint="Call editor_ui_snapshot again and use the latest target_id/snapshot_id pair.",
            ) | {"data": resolved}

        input_result = input_tools.perform_pointer_move(
            float(resolved["center_x"]),
            float(resolved["center_y"]),
            timeout_seconds=timeout_seconds,
            trace_name="editor_ui_hover",
        )
        if not input_result.get("ok"):
            return input_result
        return ok({
            "target": resolved,
            "input": input_result.get("data") or {},
            "action_path": "synthetic_sdl_pointer_move",
        })


def _read_snapshot_target(*, operation: str, semantic_id: str, kind: str = "") -> dict:
    return _semantic_main_thread(
        operation,
        lambda: _snapshot_payload(
            label="",
            kind=kind,
            window="",
            semantic_id=semantic_id,
            visible_only=True,
            limit=500,
        ),
        arguments={"semantic_id": semantic_id, "kind": kind, "visible_only": True},
    )


def _wait_for_snapshot_target(
    *,
    operation: str,
    label: str,
    kind: str,
    window: str,
    semantic_id: str,
    visible_only: bool,
    timeout_seconds: float,
    poll_interval: float,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = _semantic_main_thread(
            operation,
            lambda: _snapshot_payload(
                label=label,
                kind=kind,
                window=window,
                semantic_id=semantic_id,
                visible_only=visible_only,
                limit=500,
            ),
            arguments={
                "label": label,
                "kind": kind,
                "window": window,
                "semantic_id": semantic_id,
                "visible_only": bool(visible_only),
            },
        )
        if not response.get("ok"):
            return response
        last_payload = dict(response.get("data") or {})
        if last_payload.get("targets"):
            return response
        time.sleep(poll_interval)
    return fail(
        "error.timeout",
        "Timed out waiting for a rendered editor UI target.",
        hint="Read editor_ui_snapshot and console_read. A native OS dialog is outside ImGui capture and must be reported as a blocker.",
    ) | {"data": last_payload}


def _wait_for_window_focus(
    *,
    operation: str,
    window_id: str,
    timeout_seconds: float,
    poll_interval: float,
) -> dict:
    """Wait for a deferred menu/dock focus request to reach a rendered frame."""
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = _semantic_main_thread(
            operation,
            _focused_window_payload,
            arguments={"window_id": window_id},
        )
        if not response.get("ok"):
            return response
        last_payload = dict(response.get("data") or {})
        if last_payload.get("ready") and _window_focus_matches(window_id, str(last_payload.get("focused_window_id") or "")):
            return ok({"window_id": window_id, **last_payload})
        time.sleep(poll_interval)
    return fail(
        "error.window_focus_timeout",
        f"Timed out waiting for Editor window '{window_id}' to receive focus.",
        hint="Wait for the menu or dock transition to render, then inspect editor_ui_snapshot and console_read.",
    ) | {"data": {"window_id": window_id, **last_payload}}


def _focused_window_payload() -> dict[str, Any]:
    raw = _read_native_snapshot()
    if not bool(raw.get("capture_enabled")):
        raise RuntimeError("Editor UI semantic capture is unavailable. Build the current native Editor and restart the session.")
    return {
        "ready": int(raw.get("frame", 0) or 0) > 0,
        "capture_enabled": True,
        "snapshot_id": str(raw.get("snapshot_id", raw.get("frame", 0))),
        "frame": int(raw.get("frame", 0) or 0),
        "focused_window": str(raw.get("focused_window") or ""),
        "focused_window_id": str(raw.get("focused_window_id") or ""),
    }


def _window_focus_matches(requested_window_id: str, focused_window_id: str) -> bool:
    """Treat ImGui child windows as focus owned by their stable root panel."""
    return focused_window_id == requested_window_id or focused_window_id.startswith(requested_window_id + "/")


def _wait_for_rendered_target_after_click(
    *,
    target: dict[str, Any],
    previous_snapshot_id: str,
    timeout_seconds: float,
) -> dict:
    """Wait until the first click has crossed at least one rendered UI frame."""
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = _semantic_main_thread(
            "editor_ui_double_click.frame_barrier",
            lambda: _snapshot_payload(
                label="",
                kind="",
                window="",
                semantic_id="",
                visible_only=True,
                limit=500,
            ),
            arguments={
                "target_id": str(target.get("target_id") or ""),
                "previous_snapshot_id": previous_snapshot_id,
            },
        )
        if not response.get("ok"):
            return response
        last_payload = dict(response.get("data") or {})
        current_snapshot_id = str(last_payload.get("snapshot_id") or "")
        refreshed_target = _find_equivalent_target(target, last_payload.get("targets") or [])
        if refreshed_target is not None and current_snapshot_id != previous_snapshot_id:
            last_payload["target"] = refreshed_target
            return ok(last_payload)
        time.sleep(min(0.01, max(deadline - time.monotonic(), 0.0)))
    return fail(
        "error.double_click_frame_barrier",
        "The first click was delivered, but the target did not reach a new rendered UI frame in time.",
        hint="Retry from a fresh editor_ui_snapshot instead of treating SDL delivery as completed UI interaction.",
    ) | {"data": last_payload}


def _find_equivalent_target(original: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the same rendered control after a frame boundary, preferring stable IDs."""
    target_id = str(original.get("target_id") or "")
    if target_id:
        for candidate in candidates:
            if str(candidate.get("target_id") or "") == target_id:
                return candidate

    semantic_id = str(original.get("semantic_id") or "")
    if semantic_id:
        for candidate in candidates:
            if str(candidate.get("semantic_id") or "") == semantic_id:
                return candidate

    identity = (
        str(original.get("label") or ""),
        str(original.get("kind") or ""),
        str(original.get("window_id") or original.get("window") or ""),
    )
    for candidate in candidates:
        candidate_identity = (
            str(candidate.get("label") or ""),
            str(candidate.get("kind") or ""),
            str(candidate.get("window_id") or candidate.get("window") or ""),
        )
        if candidate_identity == identity:
            return candidate
    return None


def _snapshot_payload(
    *,
    label: str,
    kind: str,
    window: str,
    semantic_id: str,
    visible_only: bool,
    limit: int,
) -> dict[str, Any]:
    raw = _read_native_snapshot()
    if not bool(raw.get("capture_enabled")):
        raise RuntimeError("Editor UI semantic capture is unavailable. Build the current native Editor and restart the session.")

    safe_limit = max(1, min(int(limit), 2000))
    all_targets = _coalesce_targets(_normalize_target(item) for item in raw.get("targets") or [])
    matching_targets = [
        item
        for item in all_targets
        if _matches_target(
            item,
            label=label,
            kind=kind,
            window=window,
            semantic_id=semantic_id,
            visible_only=visible_only,
        )
    ]
    targets = matching_targets
    filters = {
        "label_contains": str(label or "").strip(),
        "kind_equals": str(kind or "").strip(),
        "window_contains": str(window or "").strip(),
        "semantic_id_equals": str(semantic_id or "").strip(),
        "visible_only": bool(visible_only),
    }
    payload = {
        "ready": int(raw.get("frame", 0) or 0) > 0,
        "capture_enabled": True,
        "snapshot_id": str(raw.get("snapshot_id", raw.get("frame", 0))),
        "frame": int(raw.get("frame", 0) or 0),
        "mouse": list(raw.get("mouse") or [0.0, 0.0]),
        "wants_text_input": bool(raw.get("wants_text_input")),
        "drag_drop": dict(raw.get("drag_drop") or {}),
        "focused_window": str(raw.get("focused_window") or ""),
        "focused_window_id": str(raw.get("focused_window_id") or ""),
        "rendered_target_count": len(all_targets),
        "matching_target_count": len(targets),
        "filters": filters,
        "targets": targets[:safe_limit],
        "interaction": _interaction_core_payload(),
        "coverage_notice": (
            "Targets are registered only after ImGui renders them. Native OS dialogs are intentionally outside this "
            "capture; report them as blockers instead of using external UI automation."
        ),
    }
    if all_targets and not targets:
        payload["empty_match_hint"] = (
            "The Editor rendered semantic targets, but none matched the requested filters. "
            "Call editor_ui_snapshot with label='', kind='', window='', and semantic_id='' to inspect all targets; "
            "the label argument filters target labels and is not a snapshot title."
        )
    return payload


def _interaction_core_payload() -> dict[str, Any]:
    """Expose the authoritative interaction state without mutating the Editor."""
    try:
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            return {"available": False}

        focus = core.focus.snapshot
        selection = core.selection.snapshot
        primary = selection.primary
        event = core.shortcuts.last_event
        route = core.shortcuts.last_result
        command_result = route.command_result if route is not None else None
        close = core.close_coordinator
        close_document = close.active_document

        presentation: dict[str, Any] = {"available": False}
        try:
            from Infernux.engine.ui.window_manager import WindowManager

            window_manager = WindowManager.instance()
            if window_manager is not None:
                presentation = {
                    "available": True,
                    "windows": window_manager.presentation_snapshot(),
                }
        except Exception as exc:
            presentation = {"available": False, "error": str(exc)}

        history: dict[str, Any] = {"available": False}
        try:
            from Infernux.engine.undo import UndoManager

            manager = UndoManager.instance()
            if manager is not None and manager.action_journal is core.action_journal:
                journal = manager.action_journal

                def _locator_payload(locator) -> Optional[dict[str, str]]:
                    if locator is None:
                        return None
                    return {
                        "stable_id": str(locator.stable_id),
                        "kind": locator.key_hint.kind.value,
                        "resource_path": str(locator.resource_path or ""),
                        "title": str(locator.title or ""),
                    }

                def _context_payload(context) -> Optional[dict[str, Any]]:
                    if context is None:
                        return None
                    return {
                        "panel_id": context.focus.active_panel_id,
                        "document_id": context.focus.active_document_id,
                        "document": _locator_payload(context.document),
                        "scene": _locator_payload(context.scene),
                        "window": (
                            {
                                "window_id": context.window.window_id,
                                "type_id": context.window.type_id,
                            }
                            if context.window is not None
                            else None
                        ),
                    }

                def _entry_payload(entry) -> dict[str, Any]:
                    action = entry.action
                    return {
                        "description": str(getattr(action, "description", "")),
                        "command_type": type(action).__name__,
                        "marks_dirty": bool(getattr(action, "marks_dirty", True)),
                        "origin": entry.origin.value,
                        "revision": int(entry.revision),
                        "before_context": _context_payload(entry.before_context),
                        "after_context": _context_payload(entry.after_context),
                    }

                history = {
                    "available": True,
                    "cursor": journal.cursor,
                    "entry_count": len(journal.entries),
                    "can_undo": manager.can_undo,
                    "can_redo": manager.can_redo,
                    "undo_description": manager.undo_description,
                    "redo_description": manager.redo_description,
                    "replay_pending": manager.is_replay_pending,
                    "applied_tail": [
                        _entry_payload(entry)
                        for entry in journal.applied_entries()[-8:]
                    ],
                    "redo_tail": [
                        _entry_payload(entry)
                        for entry in journal.entries[journal.cursor : journal.cursor + 8]
                    ],
                }
        except Exception as exc:
            history = {"available": False, "error": str(exc)}

        def _target_payload(target) -> dict[str, str]:
            return {
                "domain": target.domain.value,
                "target_id": target.target_id,
                "document_id": target.document_id,
                "sub_kind": target.sub_kind,
            }

        return {
            "available": True,
            "focus_revision": core.focus.revision,
            "focus": {
                "active_panel_id": focus.active_panel_id,
                "active_view_id": focus.active_view_id,
                "active_document_id": focus.active_document_id,
                "child_context_id": focus.child_context_id,
                "capture_owner_id": focus.capture_owner_id,
            },
            "selection_revision": core.selection.revision,
            "selection": {
                "owner_id": selection.owner_id,
                "domain": selection.domain.value if selection.domain is not None else "",
                "primary": _target_payload(primary) if primary is not None else None,
                "targets": [_target_payload(target) for target in selection.targets],
            },
            "documents": [
                {
                    "document_id": document.document_id,
                    "stable_id": document.stable_id,
                    "kind": document.kind.value,
                    "title": document.title,
                    "resource_path": document.resource_path,
                    "revision": document.revision,
                    "saved_revision": document.saved_revision,
                    "dirty": document.is_dirty,
                    "state": document.state.value,
                    "view_ids": sorted(document.view_ids),
                    "dirty_view_ids": sorted(document.dirty_view_ids),
                }
                for document in core.documents.documents
            ],
            "close_transaction": {
                "active": close.is_active,
                "state": close.state.value,
                "intent": close.intent.kind.value if close.intent is not None else "",
                "active_document_id": (
                    close_document.document_id if close_document is not None else ""
                ),
                "active_document_title": (
                    close_document.title if close_document is not None else ""
                ),
                "issue": close.issue.value,
                "message": close.message,
            },
            "history": history,
            "presentation": presentation,
            "shortcut": {
                "route_revision": core.shortcuts.route_revision,
                "binding_revision": core.shortcuts.revision,
                "event": (
                    {
                        "chord": event.chord.display_name(),
                        "phase": event.phase.value,
                        "text_input_active": event.text_input_active,
                        "modal_active": event.modal_active,
                        "game_view_captured": event.game_view_captured,
                    }
                    if event is not None
                    else None
                ),
                "result": (
                    {
                        "status": route.status.value,
                        "command_id": route.command_id,
                        "binding_id": route.binding_id,
                        "conflicts": list(route.conflicts),
                        "command_status": (
                            command_result.status.value
                            if command_result is not None
                            else ""
                        ),
                        "message": (
                            command_result.message
                            if command_result is not None
                            else ""
                        ),
                    }
                    if route is not None
                    else None
                ),
            },
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _resolve_target(target_id: str, snapshot_id: str) -> dict[str, Any]:
    requested_target = str(target_id or "").strip()
    requested_snapshot = str(snapshot_id or "").strip()
    if not requested_target or not requested_snapshot:
        return {"found": False, "reason": "target_id and snapshot_id are both required."}

    def _read():
        raw = _read_native_snapshot()
        if not bool(raw.get("capture_enabled")):
            return {"found": False, "reason": "Editor UI semantic capture is unavailable."}
        current_snapshot = str(raw.get("snapshot_id", raw.get("frame", 0)))
        for raw_target in raw.get("targets") or []:
            target = _normalize_target(raw_target)
            if target["target_id"] != requested_target:
                continue
            if not target["visible"]:
                return {
                    "found": False,
                    "reason": "The requested target is no longer visible in the latest UI frame.",
                    "snapshot_id": current_snapshot,
                }
            if not target["enabled"]:
                return {
                    "found": False,
                    "reason": "The requested target is disabled in the latest UI frame.",
                    "snapshot_id": current_snapshot,
                }
            x, y, width, height = target["rect"]
            if width <= 0.0 or height <= 0.0:
                return {
                    "found": False,
                    "reason": "The requested target has no clickable rectangle in the latest UI frame.",
                    "snapshot_id": current_snapshot,
                }
            click_x, click_y = target["click_point"]
            return {
                "found": True,
                **target,
                # Internal tools consume the resolved reachability-checked point.
                "center_x": click_x,
                "center_y": click_y,
                "click_x": click_x,
                "click_y": click_y,
                "requested_snapshot_id": requested_snapshot,
                "snapshot_id": current_snapshot,
                "snapshot_refreshed": current_snapshot != requested_snapshot,
            }
        return {
            "found": False,
            "reason": "The requested target is no longer rendered in the latest UI frame.",
            "snapshot_id": current_snapshot,
        }

    response = _semantic_main_thread(
        "editor_ui_target_resolve",
        _read,
        arguments={"target_id": requested_target, "snapshot_id": requested_snapshot},
    )
    if not response.get("ok"):
        return {"found": False, "reason": str((response.get("error") or {}).get("message") or "Unable to read UI state.")}
    return dict(response.get("data") or {})


def _target_resolution_failure(
    resolved: dict[str, Any],
    *,
    fallback: str,
    refresh_hint: str,
) -> dict[str, Any]:
    code = str(resolved.get("error_code") or "error.stale_ui_target")
    hint = refresh_hint
    return fail(
        code,
        str(resolved.get("reason") or fallback),
        hint=hint,
    ) | {"data": resolved}


def _wait_for_target_focus(target_id: str, *, timeout_seconds: float) -> dict[str, Any]:
    """Wait until a clicked target can receive text in a rendered frame.

    ImGui's numeric inputs switch to an internal temporary text widget.  The
    outer InputScalar item remains active while ``IsItemFocused`` can be false,
    so global ``WantTextInput`` is the authoritative second half of that state.
    """
    deadline = time.monotonic() + _positive_finite("timeout_seconds", timeout_seconds, maximum=30.0)
    last: dict[str, Any] = {"found": False, "focused": False}
    while time.monotonic() < deadline:
        def _read() -> dict[str, Any]:
            raw = _read_native_snapshot()
            if not bool(raw.get("capture_enabled")):
                return {"found": False, "focused": False, "reason": "Editor UI semantic capture is unavailable."}
            snapshot_id = str(raw.get("snapshot_id", raw.get("frame", 0)))
            for raw_target in raw.get("targets") or []:
                target = _normalize_target(raw_target)
                if target["target_id"] == target_id:
                    wants_text_input = bool(raw.get("wants_text_input"))
                    text_input_ready = bool(target.get("focused")) or (
                        bool(target.get("active"))
                        and wants_text_input
                        and str(target.get("kind") or "") in _TEXT_TARGET_KINDS
                    )
                    return {
                        "found": True,
                        "snapshot_id": snapshot_id,
                        "wants_text_input": wants_text_input,
                        "text_input_ready": text_input_ready,
                        **target,
                    }
            return {
                "found": False,
                "focused": False,
                "snapshot_id": snapshot_id,
                "reason": "The requested text target is no longer rendered.",
            }

        response = _semantic_main_thread(
            "editor_ui_wait_for_focus",
            _read,
            arguments={"target_id": target_id},
        )
        if not response.get("ok"):
            return {
                "found": False,
                "focused": False,
                "reason": str((response.get("error") or {}).get("message") or "Unable to read UI focus state."),
            }
        last = dict(response.get("data") or {})
        if last.get("found") and last.get("text_input_ready"):
            return last
        time.sleep(0.02)
    return {
        **last,
        "focused": False,
        "text_input_ready": False,
        "reason": str(last.get("reason") or "Timed out waiting for the text target to become focused."),
    }


def _read_interaction_observation() -> dict[str, Any]:
    """Read a compact semantic state used to compare a click's visible effect."""
    response = _semantic_main_thread("editor_ui_click.observe_before", _interaction_observation_payload)
    if not response.get("ok"):
        return {}
    return dict(response.get("data") or {})


def _wait_for_post_action_observation(
    before: dict[str, Any],
    *,
    source_target_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Observe delayed popups and modals after SDL delivery without claiming domain completion."""
    started = time.monotonic()
    deadline = started + max(float(timeout_seconds), 0.02)
    previous_snapshot_id = str(before.get("snapshot_id") or "")
    last = dict(before)
    while time.monotonic() < deadline:
        response = _semantic_main_thread("editor_ui_click.observe_after", _interaction_observation_payload)
        if response.get("ok"):
            candidate = dict(response.get("data") or {})
            if str(candidate.get("snapshot_id") or "") != previous_snapshot_id:
                last = candidate
        remaining = deadline - time.monotonic()
        if remaining > 0.0:
            time.sleep(min(0.02, remaining))

    before_ids = set(str(value) for value in before.get("semantic_ids") or [] if value)
    after_ids = set(str(value) for value in last.get("semantic_ids") or [] if value)
    focused_changed = str(last.get("focused_window_id") or "") != str(before.get("focused_window_id") or "")
    ui_changed = focused_changed or before_ids != after_ids
    source_present = source_target_id in set(str(value) for value in last.get("target_ids") or [] if value)
    return {
        "snapshot_id": str(last.get("snapshot_id") or ""),
        "frame": int(last.get("frame", 0) or 0),
        "observed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "focused_window": str(last.get("focused_window") or ""),
        "focused_window_id": str(last.get("focused_window_id") or ""),
        "wants_text_input": bool(last.get("wants_text_input")),
        "rendered_target_count": int(last.get("rendered_target_count", 0) or 0),
        "ui_changed": ui_changed,
        "source_target_still_rendered": source_present,
        "new_semantic_ids": sorted(after_ids - before_ids)[:64],
        "removed_semantic_ids": sorted(before_ids - after_ids)[:64],
        "focused_window_targets": list(last.get("focused_window_targets") or [])[:64],
        "effect_completion": False,
        "observation_note": (
            "This is a bounded rendered-UI observation after input delivery, not proof that the domain action completed. "
            "Use editor_ui_wait_for_target, mcp_health, or a domain oracle for the expected result."
        ),
    }


def _interaction_observation_payload() -> dict[str, Any]:
    raw = _read_native_snapshot()
    if not bool(raw.get("capture_enabled")):
        raise RuntimeError("Editor UI semantic capture is unavailable. Build the current native Editor and restart the session.")
    targets = _coalesce_targets(_normalize_target(item) for item in raw.get("targets") or [])
    focused_window_id = str(raw.get("focused_window_id") or "")
    focused_targets = []
    for target in targets:
        target_window_id = str(target.get("window_id") or "")
        if not focused_window_id or not _window_focus_matches(target_window_id, focused_window_id):
            continue
        focused_targets.append({
            "target_id": str(target.get("target_id") or ""),
            "semantic_id": str(target.get("semantic_id") or ""),
            "label": str(target.get("label") or ""),
            "kind": str(target.get("kind") or ""),
        })
    return {
        "snapshot_id": str(raw.get("snapshot_id", raw.get("frame", 0))),
        "frame": int(raw.get("frame", 0) or 0),
        "focused_window": str(raw.get("focused_window") or ""),
        "focused_window_id": focused_window_id,
        "wants_text_input": bool(raw.get("wants_text_input")),
        "rendered_target_count": len(targets),
        "target_ids": [str(target.get("target_id") or "") for target in targets],
        "semantic_ids": [str(target.get("semantic_id") or "") for target in targets if target.get("semantic_id")],
        "focused_window_targets": focused_targets[:64],
    }


def _focus_target(
    resolved: dict[str, Any], *, timeout_seconds: float, trace_name: str
) -> tuple[dict, str, str | None]:
    """Activate direct text fields or Ctrl+Click numeric drags through SDL."""
    x = float(resolved["center_x"])
    y = float(resolved["center_y"])
    if str(resolved.get("kind") or "") in _NUMERIC_TEXT_TARGET_KINDS:
        return (
            input_tools.perform_modifier_pointer_click(
                "Left Ctrl",
                x,
                y,
                button=0,
                timeout_seconds=timeout_seconds,
                trace_name=trace_name,
                keep_modifier_pressed=True,
                expected_target_id=str(resolved.get("target_id") or ""),
            ),
            "synthetic_sdl_modifier_pointer",
            "Left Ctrl",
        )
    return (
        input_tools.perform_pointer_click(
            x,
            y,
            button=0,
            timeout_seconds=timeout_seconds,
            trace_name=trace_name,
            expected_target_id=str(resolved.get("target_id") or ""),
        ),
        "synthetic_sdl_pointer",
        None,
    )


def _release_focus_modifier(modifier: str | None, *, timeout_seconds: float, trace_name: str) -> dict | None:
    if modifier is None:
        return None
    return input_tools.perform_key_transition(
        modifier,
        False,
        timeout_seconds=timeout_seconds,
        trace_name=f"{trace_name}.modifier_release",
    )


def _normalize_target(value: Any) -> dict[str, Any]:
    raw = dict(value or {})
    rect_value = list(raw.get("rect") or [0.0, 0.0, 0.0, 0.0])
    rect = [float(rect_value[index]) if index < len(rect_value) else 0.0 for index in range(4)]
    fallback_click_point = [rect[0] + rect[2] * 0.5, rect[1] + rect[3] * 0.5]
    raw_click_point = raw.get("click_point")
    if bool(raw.get("has_click_point")) and isinstance(raw_click_point, (list, tuple)) and len(raw_click_point) >= 2:
        click_point = [float(raw_click_point[0]), float(raw_click_point[1])]
    else:
        click_point = fallback_click_point
    kind = str(raw.get("kind") or "")
    enabled = bool(raw.get("enabled", True))
    visible = bool(raw.get("visible", True))
    actions = []
    if visible and enabled and kind != "status":
        actions.append("click")
        if kind in _TEXT_TARGET_KINDS:
            actions.extend(["focus", "input_text"])
        if kind == "checkbox" and "value" in raw:
            actions.append("set_checked")
    normalized_value = None
    if "value" in raw:
        normalized_value = bool(raw.get("value")) if kind == "checkbox" else raw.get("value")
    return {
        "target_id": str(raw.get("id") or ""),
        "semantic_id": str(raw.get("semantic_id") or ""),
        "label": str(raw.get("label") or ""),
        "kind": kind,
        "role": kind,
        "owner": str(raw.get("window_id") or raw.get("window") or ""),
        "window": str(raw.get("window") or ""),
        "window_id": str(raw.get("window_id") or ""),
        "occluded_by_window": str(raw.get("occluded_by_window") or ""),
        "occluded_by_window_id": str(raw.get("occluded_by_window_id") or ""),
        "item_id": int(raw.get("item_id", 0) or 0),
        "rect": rect,
        "click_point": click_point,
        "visible": visible,
        "enabled": enabled,
        "active": bool(raw.get("active")),
        "focused": bool(raw.get("focused")),
        "value_available": "value" in raw,
        "value": normalized_value,
        "actions": actions,
    }


def _wait_for_checkbox_state(original: dict[str, Any], desired: bool, *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = _semantic_main_thread(
            "editor_ui_set_checkbox.observe",
            lambda: _snapshot_payload(label="", kind="", window="", semantic_id="", visible_only=True, limit=2000),
            arguments={"target_id": str(original.get("target_id") or ""), "checked": bool(desired)},
        )
        if not response.get("ok"):
            return last
        payload = dict(response.get("data") or {})
        candidate = _find_equivalent_target(original, payload.get("targets") or [])
        if candidate is not None:
            last = dict(candidate)
            if bool(last.get("value_available")) and bool(last.get("value")) == bool(desired):
                return last
        time.sleep(0.02)
    return last


def _matches_target(
    target: dict[str, Any],
    *,
    label: str,
    kind: str,
    window: str,
    semantic_id: str,
    visible_only: bool,
) -> bool:
    if visible_only and not target["visible"]:
        return False
    if semantic_id and str(target["semantic_id"]) != str(semantic_id).strip():
        return False
    if kind and str(kind).strip().lower() != str(target["kind"]).lower():
        return False
    if window:
        needle = str(window).strip().lower()
        if needle not in str(target["window"]).lower() and needle not in str(target["window_id"]).lower():
            return False
    if label:
        needle = str(label).strip().lower()
        haystack = " ".join([target["label"], target["semantic_id"], target["target_id"]]).lower()
        if needle not in haystack:
            return False
    return True


def _coalesce_targets(values) -> list[dict[str, Any]]:
    """Prefer panel-owned semantic IDs over generic wrapper records."""
    targets: list[dict[str, Any]] = []
    by_item: dict[tuple[Any, ...], int] = {}
    for target in values:
        item_id = int(target.get("item_id", 0) or 0)
        window_id = str(target.get("window_id") or "")
        if not window_id:
            targets.append(target)
            continue
        if str(target.get("kind") or "") == "status":
            key = (window_id, item_id, "status", str(target.get("semantic_id") or ""))
        elif item_id:
            key = (window_id, item_id)
        else:
            key = (window_id, item_id, *tuple(float(value) for value in target.get("rect") or ()))
        existing_index = by_item.get(key)
        if existing_index is None:
            by_item[key] = len(targets)
            targets.append(target)
            continue
        # Wrapper widgets record first; panels may then attach a more specific
        # domain alias to the same last item. On equal semantic quality the
        # later record is therefore authoritative.
        if _semantic_priority(target) >= _semantic_priority(targets[existing_index]):
            targets[existing_index] = target
    return targets


def _semantic_priority(target: dict[str, Any]) -> int:
    semantic_id = str(target.get("semantic_id") or "")
    if semantic_id and not semantic_id.startswith("##"):
        return 2
    if semantic_id:
        return 1
    return 0


def _request_fresh_semantic_snapshot(timeout_seconds: float = 0.5) -> bool:
    """Wait on the MCP worker until the render thread publishes one requested frame."""
    try:
        previous_frame = int(_read_native_snapshot().get("frame", 0) or 0)
    except (AttributeError, ImportError, RuntimeError):
        previous_frame = 0
    requested_sequence = int(request_semantic_snapshot() or 0)
    if requested_sequence <= 0:
        return False

    deadline = time.monotonic() + max(float(timeout_seconds), 0.01)
    while time.monotonic() < deadline:
        try:
            snapshot = _read_native_snapshot()
        except (AttributeError, ImportError, RuntimeError):
            return False
        frame = int(snapshot.get("frame", 0) or 0)
        published_sequence = snapshot.get("request_sequence")
        request_completed = (
            published_sequence is None
            or int(published_sequence or 0) >= requested_sequence
        )
        if (
            bool(snapshot.get("capture_enabled"))
            and frame > 0
            and frame != previous_frame
            and request_completed
        ):
            return True
        time.sleep(0.001)
    return False


def _semantic_main_thread(
    name: str,
    fn: Callable[[], Any],
    *,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # The HTTP tool runs on the MCP worker. It requests exactly one GUI frame
    # before enqueueing the immutable-snapshot read on the Editor main thread.
    _request_fresh_semantic_snapshot()
    return main_thread(name, fn, arguments=arguments)


def _read_native_snapshot() -> dict[str, Any]:
    from Infernux.lib import get_gui_semantic_snapshot

    value = get_gui_semantic_snapshot()
    return dict(value or {})


def _pointer_button(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("button must be a button name or integer index, not a boolean.")
    if isinstance(value, int):
        button = value
    else:
        names = {"left": 0, "right": 1, "middle": 2, "x1": 3, "x2": 4}
        key = str(value or "").strip().lower()
        if key not in names:
            raise ValueError("button must be left, right, middle, x1, x2, or an integer index.")
        button = names[key]
    if button not in range(5):
        raise ValueError("button must use Unity button indices 0 through 4.")
    return int(button)


def _positive_finite(name: str, value: float, *, maximum: float) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and positive.")
    return min(result, maximum)


def _required_semantic_id(name: str, value: str) -> str:
    semantic_id = str(value or "").strip()
    if not semantic_id:
        raise ValueError(f"{name} must be a non-empty exact semantic ID.")
    return semantic_id


def _required_window_id(name: str, value: str) -> str:
    window_id = str(value or "").strip()
    if not window_id:
        raise ValueError(f"{name} must be a non-empty stable Editor window ID.")
    return window_id


def _register_metadata() -> None:
    entries = {
        "editor_ui_snapshot": (
            "Read controls rendered by the latest Editor frame; filter arguments are optional target filters.",
            [],
            "low",
        ),
        "editor_ui_capture_status": (
            "Read semantic-capture request/completion state without scheduling a GUI frame.",
            [],
            "low",
        ),
        "editor_ui_wait_for_target": (
            "Wait for a rendered target after a human-equivalent editor action.",
            [],
            "low",
        ),
        "editor_ui_wait_for_window_focus": (
            "Wait for a requested Editor window or child region to own keyboard focus.",
            [],
            "low",
        ),
        "editor_ui_open_menu": (
            "Open a menu only when its requested item is not already rendered.",
            ["May queue one human-equivalent pointer click on the menu bar."],
            "low",
        ),
        "editor_ui_click": (
            "Resolve a rendered target, click it through synthetic SDL pointer events, and observe delayed rendered UI changes.",
            ["Queues a human-equivalent pointer move/down/up sequence.", "Observes rendered semantic frames for up to 250 ms."],
            "medium",
        ),
        "editor_ui_double_click": (
            "Resolve a rendered target and double-click it through synthetic SDL pointer events.",
            ["Queues two human-equivalent pointer move/down/up sequences at the same rendered target."],
            "medium",
        ),
        "editor_ui_drag": (
            "Drag one rendered semantic target to another target or by a relative offset through SDL pointer events.",
            ["Queues a human-equivalent pointer move/press/motion/release sequence with rendered-frame barriers."],
            "medium",
        ),
        "editor_ui_set_checkbox": (
            "Set a rendered checkbox to an explicit boolean state without blind toggling.",
            ["May queue one human-equivalent pointer click when the current value differs."],
            "medium",
        ),
        "editor_ui_focus": (
            "Focus a rendered text field through synthetic SDL pointer events.",
            ["Queues a human-equivalent pointer move/down/up sequence."],
            "medium",
        ),
        "editor_ui_replace_text": (
            "Focus a text field, replace its contents through Ctrl+A and SDL text input.",
            ["Queues a human-equivalent focus, keyboard chord, and text input sequence."],
            "medium",
        ),
        "editor_ui_hover": (
            "Move the pointer over a rendered target through synthetic SDL input.",
            ["Queues a human-equivalent pointer movement sequence."],
            "low",
        ),
    }
    for name, (summary, side_effects, risk_level) in entries.items():
        register_tool_metadata(
            name,
            summary=summary,
            category="global_validation/editor_ui",
            side_effects=side_effects,
            preconditions=["Requires global_validation mode and a running graphical Editor."],
            recovery=["Refresh editor_ui_snapshot after any action before choosing the next target."],
            risk_level=risk_level,
        )
