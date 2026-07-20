"""Human-review capture tools backed by engine render targets."""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Literal

from Infernux.engine.path_utils import relative_path, resolved_path
from Infernux.mcp import session
from Infernux.mcp.tools.common import fail, main_thread, register_tool_metadata


_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "source_expired"})


def register_capture_tools(mcp, project_path: str) -> None:
    del project_path
    _register_metadata()

    @mcp.tool(name="capture_request")
    def capture_request(source: Literal["scene", "game"] = "game", file_name: str = "") -> dict:
        """Request an engine render-target capture for later human review."""
        policy_error = _capture_policy_error()
        if policy_error is not None:
            return policy_error
        source_name = str(source).strip().lower()
        if source_name not in {"scene", "game"}:
            return fail("error.invalid_argument", "Capture source must be 'scene' or 'game'.")
        try:
            output_path = _artifact_path(source_name, file_name)
        except ValueError as exc:
            return fail("error.invalid_argument", str(exc), hint="Use a plain .png basename without directories.")

        def _request() -> dict:
            native = _native_editor_engine()
            capture_id = int(native.request_capture(source_name, output_path))
            return {
                "capture_id": capture_id,
                "source": source_name,
                "status": "pending_gpu",
                "pixel_origin": "engine_render_target",
                "os_capture_fallback": False,
                "artifact_uri": _artifact_uri(output_path),
                "pixel_access": False,
                "human_review_only": True,
            }

        return main_thread(
            "capture_request",
            _request,
            arguments={"source": source_name, "file_name": os.path.basename(output_path)},
        )

    @mcp.tool(name="capture_status")
    def capture_status(capture_id: int) -> dict:
        """Poll an engine capture without returning its pixels."""
        policy_error = _capture_policy_error()
        if policy_error is not None:
            return policy_error

        def _query() -> dict:
            return dict(_native_editor_engine().query_capture(int(capture_id)))

        response = main_thread("capture_status", _query, arguments={"capture_id": int(capture_id)})
        if not response.get("ok"):
            return response
        value = response.get("data") or {}
        value["pixel_origin"] = "engine_render_target"
        value["os_capture_fallback"] = False
        value["pixel_access"] = False
        value["human_review_only"] = True
        output_path = str(value.pop("output_path", "") or "")
        value["artifact_uri"] = _artifact_uri(output_path) if output_path else ""
        if value.get("status") == "completed" and output_path and os.path.isfile(output_path):
            value["byte_size"] = os.path.getsize(output_path)
            value["sha256"] = _sha256_file(output_path)
        value["terminal"] = str(value.get("status", "")) in _TERMINAL_STATES
        return response

    @mcp.tool(name="capture_cancel")
    def capture_cancel(capture_id: int) -> dict:
        """Cancel an unfinished engine capture request."""
        policy_error = _capture_policy_error()
        if policy_error is not None:
            return policy_error

        def _cancel() -> dict:
            return {"capture_id": int(capture_id), "cancelled": bool(_native_editor_engine().cancel_capture(int(capture_id)))}

        return main_thread("capture_cancel", _cancel, arguments={"capture_id": int(capture_id)})


def _capture_policy_error() -> dict | None:
    active = session.current()
    if active.build_profile != "debug_feedback":
        return fail(
            "error.capture_unavailable",
            "Engine capture is available only in a Debug feedback session.",
            hint="Restart the project through the Supervisor with build_profile=debug_feedback.",
        )
    if not active.recording_enabled:
        return fail(
            "error.recording_disabled",
            "Engine capture is disabled for this session.",
            hint="Use a Supervisor mode handoff to enable recording, then retry.",
        )
    return None


def _artifact_path(source: str, file_name: str) -> str:
    active = session.current()
    review_dir = os.path.join(active.artifact_root, "review")
    os.makedirs(review_dir, exist_ok=True)
    requested = os.path.basename(str(file_name or "").strip())
    if requested:
        stem, extension = os.path.splitext(requested)
        if extension.lower() != ".png":
            raise ValueError("Capture artifacts must use the .png extension.")
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-") or source
        requested = f"{safe_stem}.png"
    else:
        requested = f"{source}-{time.time_ns()}.png"
    return resolved_path(os.path.join(review_dir, requested))


def _artifact_uri(output_path: str) -> str:
    if not output_path:
        return ""
    active = session.current()
    relative = relative_path(output_path, active.artifact_root)
    return relative


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _native_editor_engine():
    from Infernux.engine.bootstrap import EditorBootstrap

    bootstrap = EditorBootstrap.instance()
    engine = bootstrap.engine if bootstrap is not None else None
    native = engine.get_native_engine() if engine is not None else None
    if native is None:
        raise RuntimeError("Engine capture requires a running graphical Editor session.")
    return native


def _register_metadata() -> None:
    invariants = [
        "Pixels come only from Infernux render targets through GPU readback.",
        "No Windows window handle, desktop coordinates, or screen capture API is accepted.",
        "Foreground focus, window occlusion, and user desktop activity cannot affect captured pixels.",
        "Unavailable engine sources fail explicitly; there is no operating-system capture fallback.",
        "Pixels are written to human-review artifacts and are never returned to the agent.",
        "Capture is opt-in and Debug-only.",
    ]
    register_tool_metadata(
        "capture_request",
        summary="Request an asynchronous engine render-target capture for human review; OS pixels are never read.",
        category="capture",
        level="renderer",
        parameters={
            "source": {"type": "string", "enum": ["scene", "game"], "default": "game"},
            "file_name": {"type": "string", "description": "Optional artifact basename ending in .png."},
        },
        preconditions=["Debug feedback profile", "recording_enabled=true", "source render target initialized"],
        side_effects=["Queues GPU readback", "Writes one PNG under the session review directory"],
        next_suggested_tools=["capture_status"],
        recovery=["Enable recording through the Supervisor", "Ensure the matching engine render target is initialized"],
        invariants=invariants,
        risk_level="low",
        feature="engine_capture",
    )
    register_tool_metadata(
        "capture_status",
        summary="Poll capture state and artifact metadata without returning pixels.",
        category="capture",
        level="renderer",
        next_suggested_tools=["capture_status", "mcp_report_blocker"],
        recovery=["Retry while status is pending_gpu or pending_encode"],
        invariants=invariants,
        risk_level="low",
        feature="engine_capture",
    )
    register_tool_metadata(
        "capture_cancel",
        summary="Cancel an unfinished engine capture.",
        category="capture",
        level="renderer",
        side_effects=["Cancels pending readback or discards an encoding result"],
        invariants=invariants,
        risk_level="low",
        feature="engine_capture",
    )
