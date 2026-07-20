"""Standalone Debug Player validation tools proxied by the Editor MCP."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from Infernux.mcp import session
from Infernux.engine.path_utils import resolved_path
from Infernux.mcp.supervisor import SupervisorSession
from Infernux.mcp.tools.common import fail, ok, register_tool_metadata
from Infernux.mcp.tools.runtime import MotionCaptureStopMode, RuntimeAssertion


class PlayerComponentProbe(BaseModel):
    """Public component fields observed inside the standalone Debug Player."""

    model_config = ConfigDict(extra="forbid")

    object_name: str = Field(min_length=1)
    component_type: str = Field(min_length=1)
    fields: list[str] = Field(min_length=1, max_length=16)
    ordinal: int = Field(default=0, ge=0)


def register_player_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="player_validation_launch")
    def player_validation_launch(
        executable_path: str = "",
        start_scene: str = "",
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Launch the Debug Player, optionally at a BuildManifest scene for validation."""
        try:
            supervisor = _supervisor()
            executable = resolved_path(executable_path) if executable_path else _configured_executable(project_path)
            status = supervisor.launch_player(
                executable,
                start_scene=start_scene,
                wait_for_ready=True,
                timeout_seconds=timeout_seconds,
            )
            if not bool(status.get("player_ready")):
                return fail(
                    "error.player_startup",
                    str(status.get("ready_error", "Player did not become ready.")),
                    hint="Inspect the attached Player logs, fix the startup blocker, rebuild, and retry.",
                ) | {"data": status, "logs": supervisor.player_read_logs(limit=200)}
            return ok(status)
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_status")
    def player_validation_status() -> dict:
        """Read managed Player process/readiness state without touching gameplay."""
        try:
            return ok(_supervisor().status())
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_observe")
    def player_validation_observe(
        object_names: list[str] | None = None,
        component_probes: list[PlayerComponentProbe] | None = None,
        include_scene_objects: bool = False,
        discovery_component_types: list[str] | None = None,
        max_discovered_objects: int = 32,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Read Player state and optionally discover bounded current-scene objects by public component type."""
        try:
            return ok(_supervisor().player_observe(
                object_names or [],
                component_probes=_probe_mappings(component_probes),
                include_scene_objects=bool(include_scene_objects),
                discovery_component_types=discovery_component_types or [],
                max_discovered_objects=max_discovered_objects,
                timeout_seconds=timeout_seconds,
            ))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_key")
    def player_validation_key(
        key: str | int,
        pressed: bool,
        repeat: bool = False,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Send one keyboard transition to Player through its SDL synthetic input queue."""
        try:
            return ok(_supervisor().player_send_key(
                key,
                bool(pressed),
                repeat=bool(repeat),
                timeout_seconds=timeout_seconds,
            ))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_press")
    def player_validation_press(
        key: str | int,
        duration_seconds: float = 0.1,
        object_names: list[str] | None = None,
        component_probes: list[PlayerComponentProbe] | None = None,
        timeout_seconds: float = 3.0,
    ) -> dict:
        """Press and release a Player key with engine-controlled hold timing."""
        try:
            return ok(_supervisor().player_press_key(
                key,
                duration_seconds=float(duration_seconds),
                object_names=object_names or [],
                component_probes=_probe_mappings(component_probes),
                timeout_seconds=timeout_seconds,
            ))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_motion_capture_arm")
    def player_validation_motion_capture_arm(
        object_names: list[str],
        seconds: float = 2.0,
        sample_interval: float = 0.1,
        trigger_scene_name: str = "",
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
        component_probes: list[PlayerComponentProbe] | None = None,
        stop_assertions: list[RuntimeAssertion] | None = None,
        stop_mode: MotionCaptureStopMode = "all",
        pause_on_condition: bool = True,
    ) -> dict:
        """Arm Player-owned sampling and an optional frame-bounded input plan.

        The Player presses held keys only after the target scene and public
        probe objects are ready, releases them on the requested game frame,
        and can pause after its bounded settle window. Agents should poll the
        returned capture rather than issuing per-frame input requests. Stop
        assertions are sampled only at ``sample_interval`` and may use scene
        name, sampled Transform position, or fields explicitly declared in
        ``component_probes``.
        """
        try:
            return ok(_supervisor().player_motion_capture_arm(
                object_names,
                seconds=float(seconds),
                sample_interval=float(sample_interval),
                trigger_scene_name=str(trigger_scene_name or ""),
                trigger_timeout=float(trigger_timeout),
                hold_key=hold_key,
                hold_keys=hold_keys,
                hold_mouse_buttons=hold_mouse_buttons,
                mouse_x=float(mouse_x),
                mouse_y=float(mouse_y),
                frame_count=frame_count,
                hold_frame_count=hold_frame_count,
                wait_frame_count=wait_frame_count,
                wait_seconds=float(wait_seconds),
                pause_on_complete=bool(pause_on_complete),
                component_probes=_probe_mappings(component_probes),
                stop_assertions=_assertion_mappings(stop_assertions),
                stop_mode=str(stop_mode),
                pause_on_condition=bool(pause_on_condition),
            ))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_motion_capture_status")
    def player_validation_motion_capture_status(
        capture_id: str,
        wait_seconds: float = 0.0,
    ) -> dict:
        """Read or briefly wait for a Player-owned startup capture."""
        try:
            wait = max(0.0, min(float(wait_seconds), 30.0))
            if wait:
                time.sleep(wait)
            return ok(_supervisor().player_motion_capture_status(capture_id))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_motion_capture_cancel")
    def player_validation_motion_capture_cancel(capture_id: str) -> dict:
        """Cancel an armed or active Player-owned startup capture."""
        try:
            return ok(_supervisor().player_motion_capture_cancel(capture_id))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_logs")
    def player_validation_logs(limit: int = 200) -> dict:
        """Read bounded Player runtime/stdout log tails for failures and warnings."""
        try:
            return ok(_supervisor().player_read_logs(limit=limit))
        except Exception as exc:
            return _player_failure(exc)

    @mcp.tool(name="player_validation_shutdown")
    def player_validation_shutdown(timeout_seconds: float = 15.0) -> dict:
        """Request normal Player shutdown on its main thread; never force-terminate it."""
        try:
            return ok(_supervisor().stop_player(timeout_seconds=timeout_seconds))
        except Exception as exc:
            return _player_failure(exc)


def _supervisor() -> SupervisorSession:
    active = session.require_mode("global_validation")
    if active.build_profile != "debug_feedback":
        raise RuntimeError("Player validation tools require the debug_feedback profile.")
    return SupervisorSession.resume(active.project_root, active.session_id, verify_mcp=False)


def _configured_executable(project_path: str) -> str:
    settings_path = os.path.join(project_path, "ProjectSettings", "BuildSettings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as stream:
            settings = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError(f"Build Settings could not be read: {settings_path}") from exc
    output_dir = resolved_path(str(settings.get("output_dir", "") or ""))
    game_name = str(settings.get("game_name", "") or "").strip()
    if not output_dir or not game_name:
        raise ValueError("Build Settings must define output_dir and game_name before Player validation.")
    suffix = ".exe" if os.name == "nt" else ""
    return os.path.join(output_dir, game_name + suffix)


def _probe_mappings(probes: list[PlayerComponentProbe] | None) -> list[dict[str, Any]]:
    return [probe.model_dump() if isinstance(probe, PlayerComponentProbe) else dict(probe) for probe in probes or []]


def _assertion_mappings(assertions: list[RuntimeAssertion] | None) -> list[dict[str, Any]]:
    return [
        assertion.model_dump(exclude_unset=True, exclude_none=True)
        if isinstance(assertion, RuntimeAssertion) else dict(assertion)
        for assertion in assertions or []
    ]


def _player_failure(exc: Exception) -> dict[str, Any]:
    return fail(
        "error.player_validation",
        f"{type(exc).__name__}: {exc}",
        hint=(
            "Use a current Debug Player build from this project. Check player_validation_status and "
            "player_validation_logs; report an engine blocker instead of force-killing the Player."
        ),
    )


def _register_metadata() -> None:
    entries = (
        (
            "player_validation_launch",
            "Launch the configured Debug Player under Supervisor control, optionally at a declared build scene.",
            ["Starts a Player process"],
        ),
        ("player_validation_status", "Read managed Player lifecycle state.", []),
        (
            "player_validation_observe",
            "Read Player scene, transforms, public component fields, bounded scene discovery, renderer, and input state.",
            [],
        ),
        ("player_validation_key", "Send a human-equivalent SDL key transition to Player.", ["Queues Player input"]),
        (
            "player_validation_press",
            "Press and release a Player key with engine-controlled timing and optional public component probes.",
            ["Queues Player input"],
        ),
        (
            "player_validation_motion_capture_arm",
            "Arm bounded Player-owned sampling with optional frame-bound Player input and pause.",
            ["Starts a bounded Player input/capture action"],
        ),
        (
            "player_validation_motion_capture_status",
            "Read or briefly wait for a Player-owned startup capture.",
            [],
        ),
        (
            "player_validation_motion_capture_cancel",
            "Cancel an armed or active Player-owned startup capture.",
            ["Cancels a bounded read-only Player capture"],
        ),
        ("player_validation_logs", "Read bounded Player log tails.", []),
        ("player_validation_shutdown", "Request normal engine-owned Player shutdown.", ["Stops the Player normally"]),
    )
    for name, summary, side_effects in entries:
        register_tool_metadata(
            name,
            summary=summary,
            category="global_validation/player",
            preconditions=["global_validation mode", "debug_feedback profile", "current Debug Player build"],
            side_effects=side_effects,
            recovery=["Use player_validation_status and player_validation_logs", "Report a blocker; never force-kill Player"],
            risk_level="medium" if side_effects else "low",
            feature="player_validation",
        )
