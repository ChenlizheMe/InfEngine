"""External lifecycle helper for constrained remote MCP project sessions."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import math
import os
import signal
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from Infernux.engine.path_utils import (
    is_path_within,
    path_fingerprint,
    relative_path,
    resolved_path,
    same_path,
)
from Infernux.mcp import capabilities
from Infernux.mcp import checkpoints as checkpoint_store


VALID_MODES = frozenset({"developer_assist", "global_validation"})
VALID_BUILD_PROFILES = frozenset({"debug_feedback", "release_exploration"})
HANDOFF_STATES = frozenset({"idle", "started", "completed", "failed"})


@dataclass
class SupervisorSession:
    project_root: str
    mode: str = "global_validation"
    build_profile: str = "debug_feedback"
    recording_enabled: bool = False
    managed_checkpoints_required: bool = False
    python_executable: str = sys.executable
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 9713
    session_id: str = field(default_factory=lambda: f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    started_at: float = field(default_factory=time.time)
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _attached_editor_pid: int = field(default=0, init=False, repr=False)
    _player_process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _attached_player_pid: int = field(default=0, init=False, repr=False)
    _editor_instance_id: str = field(default="", init=False, repr=False)
    _supervisor_lease: str = field(default="", init=False, repr=False)
    _project_lock_token: str = field(default="", init=False, repr=False)
    _player_control_token: str = field(default="", init=False, repr=False)
    _player_executable: str = field(default="", init=False, repr=False)
    _player_start_scene: str = field(default="", init=False, repr=False)
    _player_ready: bool = field(default=False, init=False, repr=False)
    _mcp_ready: bool = field(default=False, init=False, repr=False)
    _editor_log_handle: Any = field(default=None, init=False, repr=False)
    _player_log_handle: Any = field(default=None, init=False, repr=False)
    _last_handoff: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.project_root = resolved_path(self.project_root)
        self.mode = _require_choice("mode", self.mode, VALID_MODES)
        self.build_profile = _require_choice("build_profile", self.build_profile, VALID_BUILD_PROFILES)
        self.recording_enabled = bool(self.recording_enabled and self.build_profile == "debug_feedback")
        self.managed_checkpoints_required = bool(self.managed_checkpoints_required)
        self.python_executable = resolved_path(self.python_executable)
        self.mcp_host = _require_loopback_host(self.mcp_host)
        self.mcp_port = _require_port(self.mcp_port)

    @property
    def artifact_root(self) -> str:
        return os.path.join(self.project_root, ".infernux", "mcp_sessions", self.session_id)

    @property
    def state_path(self) -> str:
        return os.path.join(self.artifact_root, "supervisor-session.json")

    @property
    def operation_lock_path(self) -> str:
        return os.path.join(self.artifact_root, "supervisor-operation.lock")

    @property
    def agent_handoff_path(self) -> str:
        return os.path.join(self.artifact_root, "agent-handoff.json")

    @property
    def project_lock_path(self) -> str:
        return os.path.join(self.project_root, "ProjectSettings", ".infernux-engine-lock.json")

    @property
    def player_control_path(self) -> str:
        return os.path.join(self.artifact_root, "player-control.json")

    @property
    def player_response_path(self) -> str:
        return os.path.join(self.artifact_root, "player-response.json")

    @property
    def player_ready_path(self) -> str:
        return os.path.join(self.artifact_root, "player-ready.txt")

    @property
    def player_stdout_path(self) -> str:
        return os.path.join(self.artifact_root, "player.stdout.log")

    @property
    def player_runtime_log_path(self) -> str:
        if not self._player_executable:
            return ""
        return os.path.join(self._player_log_root(), "player.log")

    @property
    def player_debug_log_path(self) -> str:
        if not self._player_executable:
            return ""
        data_root = _player_data_root(self._player_executable)
        data_name = os.path.basename(data_root)
        game_name = data_name[:-5] if data_name.endswith("_Data") else data_name
        return os.path.join(self._player_log_root(), f"{game_name}_debug.log")

    @property
    def player_crash_log_path(self) -> str:
        if not self._player_executable:
            return ""
        return os.path.join(self._player_log_root(), "crash.log")

    def _player_log_root(self) -> str:
        data_root = _player_data_root(self._player_executable)
        data_name = os.path.basename(data_root)
        game_name = data_name[:-5] if data_name.endswith("_Data") else data_name
        state_home = (
            os.environ.get("LOCALAPPDATA", "").strip()
            or os.environ.get("XDG_STATE_HOME", "").strip()
            or os.path.join(os.path.expanduser("~"), ".local", "state")
        )
        return os.path.join(state_home, "Infernux", "Players", game_name, "Logs")

    @classmethod
    def resume(
        cls,
        project_root: str,
        session_id: str,
        *,
        verify_mcp: bool = True,
        timeout_seconds: float = 30.0,
    ) -> "SupervisorSession":
        """Reattach a new orchestrator process to a persisted Supervisor session.

        The original Python process that created a ``SupervisorSession`` is
        allowed to end while the Editor continues to run. A resumed session
        only trusts a saved PID after the loopback endpoint, mode/profile,
        editor instance ID, lease fingerprint, and project lock all agree.
        """
        root = resolved_path(project_root or "")
        requested_session_id = str(session_id or "").strip()
        if not requested_session_id:
            raise ValueError("A non-empty session_id is required to resume a Supervisor session.")
        state_path = os.path.join(root, ".infernux", "mcp_sessions", requested_session_id, "supervisor-session.json")
        state = _read_json_object(state_path)
        if not state:
            raise FileNotFoundError(f"Supervisor session state was not found: {state_path}")
        stored_root = resolved_path(str(state.get("project_root", "") or ""))
        if not stored_root or not same_path(stored_root, root):
            raise ValueError("Persisted Supervisor session belongs to a different project root.")
        stored_session_id = str(state.get("session_id", "") or "").strip()
        if stored_session_id != requested_session_id:
            raise ValueError("Persisted Supervisor session identifier does not match the requested session.")

        endpoint = urlparse(str(state.get("mcp_endpoint", "") or ""))
        if endpoint.scheme != "http" or endpoint.path != "/mcp":
            raise ValueError("Persisted Supervisor MCP endpoint is not a supported loopback HTTP endpoint.")
        host = endpoint.hostname or "127.0.0.1"
        port = int(endpoint.port or 9713)
        resumed = cls(
            root,
            mode=str(state.get("mode", "global_validation") or "global_validation"),
            build_profile=str(state.get("build_profile", "debug_feedback") or "debug_feedback"),
            recording_enabled=bool(state.get("recording_enabled", False)),
            managed_checkpoints_required=bool(state.get("managed_checkpoints_required", False)),
            mcp_host=host,
            mcp_port=port,
            session_id=requested_session_id,
            started_at=float(state.get("started_at", time.time()) or time.time()),
        )
        resumed._editor_instance_id = str(state.get("editor_instance_id", "") or "").strip()
        resumed._supervisor_lease = str(state.get("supervisor_lease", "") or "").strip()
        resumed._project_lock_token = str(state.get("project_lock_token", "") or "").strip()
        resumed._mcp_ready = bool(state.get("mcp_ready", False))
        resumed._player_control_token = str(state.get("player_control_token", "") or "").strip()
        resumed._player_executable = resolved_path(str(state.get("player_executable", "") or "")) if state.get("player_executable") else ""
        resumed._player_start_scene = str(state.get("player_start_scene", "") or "").strip()
        resumed._player_ready = bool(state.get("player_ready", False))
        resumed._last_handoff = _normalize_handoff_record(state.get("last_handoff"))
        persisted_pid = int(state.get("editor_pid", 0) or 0)
        if persisted_pid > 0 and _pid_is_running(persisted_pid):
            if not resumed._has_leased_editor_identity():
                raise RuntimeError(
                    "Cannot resume a running Supervisor session without its Editor instance ID, "
                    "Supervisor lease, and project-lock token."
                )
            resumed._attached_editor_pid = persisted_pid
            if verify_mcp:
                resumed._verify_attached_editor(timeout_seconds=timeout_seconds)
        else:
            resumed._mcp_ready = False
            if _mcp_health_is_alive(resumed.mcp_health_endpoint):
                if not resumed._try_attach_matching_unmanaged_editor(timeout_seconds=timeout_seconds):
                    raise RuntimeError(
                        "A live Infernux MCP endpoint exists for this session artifact, but its persisted Editor PID is stale. "
                        "Refusing to rewrite the project policy without a verified attachment."
                    )
        persisted_player_pid = int(state.get("player_pid", 0) or 0)
        if persisted_player_pid > 0 and _pid_is_running(persisted_player_pid):
            if len(resumed._player_control_token) < 16 or not resumed._player_executable:
                raise RuntimeError("Cannot resume a running Player without its executable and private control token.")
            resumed._attached_player_pid = persisted_player_pid
            resumed._player_ready = os.path.isfile(resumed.player_ready_path)
        return resumed

    @classmethod
    def attach(
        cls,
        project_root: str,
        session_id: str,
        *,
        verify_mcp: bool = True,
        timeout_seconds: float = 30.0,
    ) -> "SupervisorSession":
        """Alias for ``resume`` that makes cross-process attachment explicit."""
        return cls.resume(
            project_root,
            session_id,
            verify_mcp=verify_mcp,
            timeout_seconds=timeout_seconds,
        )

    def prepare_project(self) -> dict[str, Any]:
        """Create the minimal project layout and persist only session-safe config."""
        _validate_project_root(self.project_root)
        for directory in ("Assets", "ProjectSettings", "Library", "Logs"):
            os.makedirs(os.path.join(self.project_root, directory), exist_ok=True)
        os.makedirs(self.artifact_root, exist_ok=True)

        config = capabilities.load_capability_config(self.project_root)
        config["enabled"] = True
        config["profile"] = self.mode
        policy = config.setdefault("session", {})
        policy.update({
            "session_id": self.session_id,
            "build_profile": self.build_profile,
            "recording_enabled": self.recording_enabled,
            "managed_checkpoints_required": self.managed_checkpoints_required,
            "allowed_project_roots": [self.project_root],
        })
        capabilities.save_config(config, self.project_root)
        self._persist_state()
        return self.status()

    @property
    def mcp_endpoint(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"

    @property
    def mcp_health_endpoint(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/health"

    @property
    def editor_log_path(self) -> str:
        return os.path.join(self.artifact_root, "editor.stdout.log")

    @property
    def handoff_history_path(self) -> str:
        return os.path.join(self.artifact_root, "mode-handoffs.jsonl")

    @property
    def checkpoint_history_path(self) -> str:
        return os.path.join(self.artifact_root, "checkpoint-operations.jsonl")

    def launch_editor(self, *, wait_for_mcp: bool = False, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Start an Editor process for this prepared project."""
        if self._player_state()["running"]:
            raise RuntimeError("Stop the standalone Player normally before launching the Editor.")
        if self._editor_state()["running"]:
            status = self.status() | {"already_running": True}
            return self._wait_for_verified_mcp_ready(timeout_seconds=timeout_seconds) if wait_for_mcp else status
        if _mcp_health_is_alive(self.mcp_health_endpoint):
            raise RuntimeError(
                "A live Infernux MCP endpoint already occupies the persisted session endpoint; "
                "attach to it with matching identity or resolve it before launching another Editor."
            )
        self._new_editor_identity()
        self.prepare_project()
        self._close_editor_log()
        self._attached_editor_pid = 0
        self.mcp_port = _available_port(self.mcp_host, self.mcp_port)
        code = (
            "import sys; "
            "from Infernux.engine import release_engine; "
            "release_engine(project_path=sys.argv[1])"
        )
        env = os.environ.copy()
        env["INFERNUX_MCP_SUPERVISOR_SESSION_ID"] = self.session_id
        env["INFERNUX_MCP_BUILD_PROFILE"] = self.build_profile
        env["INFERNUX_MCP_HOST"] = self.mcp_host
        env["INFERNUX_MCP_PORT"] = str(self.mcp_port)
        env["INFERNUX_MCP_EDITOR_INSTANCE_ID"] = self._editor_instance_id
        env["INFERNUX_MCP_SUPERVISOR_LEASE"] = self._supervisor_lease
        env["_INFERNUX_PROJECT_LOCK_TOKEN"] = self._project_lock_token
        self._mcp_ready = False
        self._editor_log_handle = open(self.editor_log_path, "a", encoding="utf-8", newline="\n")
        self._process = subprocess.Popen(
            [self.python_executable, "-u", "-c", code, self.project_root],
            cwd=self.project_root,
            env=env,
            stdout=self._editor_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._persist_state()
        return self._wait_for_verified_mcp_ready(timeout_seconds=timeout_seconds) if wait_for_mcp else self.status()

    def launch_player(
        self,
        executable_path: str,
        *,
        start_scene: str = "",
        wait_for_ready: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Launch a Debug Player with a constrained, engine-owned validation channel."""
        if self.build_profile != "debug_feedback":
            raise RuntimeError("Supervisor-managed Player validation requires the debug_feedback profile.")
        if self._player_state()["running"]:
            status = self.status() | {"already_running": True}
            return self.wait_for_player_ready(timeout_seconds=timeout_seconds) if wait_for_ready else status

        executable, manifest = _validate_player_executable(executable_path, self.project_root)
        self._player_executable = executable
        self._player_start_scene = _resolve_player_start_scene(start_scene, self.project_root, manifest)
        self._player_control_token = secrets.token_urlsafe(32)
        self._player_ready = False
        self._attached_player_pid = 0
        for path in (self.player_control_path, self.player_response_path, self.player_ready_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        self._close_player_log()
        os.makedirs(self.artifact_root, exist_ok=True)
        self._player_log_handle = open(self.player_stdout_path, "a", encoding="utf-8", newline="\n")
        env = os.environ.copy()
        env["_INFERNUX_READY_FILE"] = self.player_ready_path
        env["_INFERNUX_PLAYER_CONTROL_FILE"] = self.player_control_path
        env["_INFERNUX_PLAYER_RESPONSE_FILE"] = self.player_response_path
        env["_INFERNUX_PLAYER_CONTROL_TOKEN"] = self._player_control_token
        env["_INFERNUX_PLAYER_ARTIFACT_ROOT"] = self.artifact_root
        data_root = _player_data_root(self._player_executable)
        env["_INFERNUX_PLAYER_RUNTIME_ROOT"] = os.path.dirname(self._player_executable)
        env["_INFERNUX_PLAYER_DATA_ROOT"] = data_root
        env["_INFERNUX_PLAYER_MODULE_ROOT"] = os.path.join(data_root, "RuntimeModules")
        if self._player_start_scene:
            env["_INFERNUX_PLAYER_START_SCENE"] = self._player_start_scene
        try:
            self._player_process = subprocess.Popen(
                [self._player_executable],
                cwd=os.path.dirname(self._player_executable),
                env=env,
                stdout=self._player_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception:
            self._close_player_log()
            self._player_process = None
            self._player_control_token = ""
            raise
        self._persist_state()
        return self.wait_for_player_ready(timeout_seconds=timeout_seconds) if wait_for_ready else self.status()

    def wait_for_player_ready(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
        while time.monotonic() < deadline:
            player = self._player_state()
            if not player["running"]:
                exit_code = player.get("exit_code")
                self._mark_player_stopped()
                return self.status() | {
                    "player_ready": False,
                    "player_exit_code": exit_code,
                    "ready_error": f"Player exited before readiness with code {exit_code}.",
                }
            try:
                with open(self.player_ready_path, "r", encoding="utf-8") as stream:
                    ready = stream.read(64).strip()
            except OSError:
                ready = ""
            if ready == "ENGINE_LOADED":
                self._player_ready = True
                self._persist_state()
                return self.status() | {"player_ready": True, "ready_error": ""}
            time.sleep(0.05)
        return self.status() | {"player_ready": False, "ready_error": "Timed out waiting for Player readiness."}

    def player_send_key(
        self,
        key: str | int,
        pressed: bool,
        *,
        repeat: bool = False,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        from Infernux.lib import InputManager

        if isinstance(key, bool):
            raise ValueError("key must be a key name or SDL scancode, not a boolean.")
        scancode = int(key) if isinstance(key, int) else int(InputManager.name_to_scancode(str(key)))
        if scancode <= 0:
            raise ValueError(f"Unknown key: {key!r}.")
        with self._operation_lock():
            return self._call_player_control(
                "key",
                {"scancode": scancode, "pressed": bool(pressed), "repeat": bool(repeat)},
                timeout_seconds=timeout_seconds,
            )

    def player_send_mouse_button(
        self,
        button: int,
        pressed: bool,
        x: float,
        y: float,
        *,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Send one human-equivalent pointer transition to the managed Player."""
        mouse_buttons = _normalize_player_hold_mouse_buttons([button])
        with self._operation_lock():
            return self._call_player_control(
                "mouse_button",
                {
                    "button": mouse_buttons[0],
                    "pressed": bool(pressed),
                    "x": _bounded_finite_float(x, "x", minimum=0.0, maximum=100_000.0),
                    "y": _bounded_finite_float(y, "y", minimum=0.0, maximum=100_000.0),
                },
                timeout_seconds=timeout_seconds,
            )

    def player_press_key(
        self,
        key: str | int,
        duration_seconds: float = 0.1,
        *,
        object_names: list[str] | None = None,
        component_probes: list[dict[str, Any]] | None = None,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Press and release a Player key with engine-controlled timing."""
        from Infernux.lib import InputManager

        if isinstance(key, bool):
            raise ValueError("key must be a key name or SDL scancode, not a boolean.")
        scancode = int(key) if isinstance(key, int) else int(InputManager.name_to_scancode(str(key)))
        if scancode <= 0:
            raise ValueError(f"Unknown key: {key!r}.")
        duration = _bounded_finite_float(duration_seconds, "duration_seconds", minimum=0.02, maximum=10.0)
        names = [str(name or "").strip() for name in (object_names or []) if str(name or "").strip()]
        if len(names) > 32:
            raise ValueError("object_names cannot contain more than 32 entries.")
        probes = _normalize_player_component_probes(component_probes, names)
        with self._operation_lock():
            return self._call_player_control(
                "press",
                {
                    "scancode": scancode,
                    "duration_seconds": duration,
                    "object_names": names,
                    "component_probes": probes,
                },
                timeout_seconds=max(float(timeout_seconds), duration + 2.0),
            )

    def player_capture_game(
        self,
        file_name: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Capture the managed Player's engine-owned Game render target."""
        requested = os.path.basename(str(file_name or "").strip())
        if not requested or os.path.splitext(requested)[1].lower() != ".png":
            raise ValueError("file_name must be a plain .png basename.")
        with self._operation_lock():
            return self._call_player_control(
                "capture",
                {
                    "file_name": requested,
                    "timeout_seconds": min(max(float(timeout_seconds) - 0.25, 0.5), 60.0),
                },
                timeout_seconds=timeout_seconds,
            )

    def player_motion_capture_arm(
        self,
        object_names: list[str],
        *,
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
        component_probes: list[dict[str, Any]] | None = None,
        stop_assertions: list[dict[str, Any]] | None = None,
        stop_mode: str = "all",
        pause_on_condition: bool = True,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Arm Player-owned startup sampling before a real cross-scene input."""
        names = [str(name or "").strip() for name in object_names if str(name or "").strip()]
        if not names:
            raise ValueError("object_names must contain at least one public object name.")
        if len(names) > 32:
            raise ValueError("object_names cannot contain more than 32 entries.")
        duration = _bounded_finite_float(seconds, "seconds", minimum=0.1, maximum=10.0)
        interval = _bounded_finite_float(sample_interval, "sample_interval", minimum=0.02, maximum=1.0)
        trigger_wait = _bounded_finite_float(trigger_timeout, "trigger_timeout", minimum=0.5, maximum=120.0)
        settle_wait = _bounded_finite_float(wait_seconds, "wait_seconds", minimum=0.0, maximum=30.0)
        probes = _normalize_player_component_probes(component_probes, names)
        hold_scancodes = _normalize_player_hold_scancodes(hold_key, hold_keys)
        mouse_buttons = _normalize_player_hold_mouse_buttons(hold_mouse_buttons)
        assertions = _normalize_player_stop_assertions(stop_assertions)
        normalized_stop_mode = str(stop_mode or "all").strip().lower()
        if normalized_stop_mode not in {"all", "any"}:
            raise ValueError("stop_mode must be 'all' or 'any'.")
        with self._operation_lock():
            return self._call_player_control(
                "motion_capture_arm",
                {
                    "object_names": names,
                    "component_probes": probes,
                    "seconds": duration,
                    "sample_interval": interval,
                    "trigger_scene_name": str(trigger_scene_name or "").strip(),
                    "trigger_timeout": trigger_wait,
                    "hold_scancodes": hold_scancodes,
                    "hold_mouse_buttons": mouse_buttons,
                    "mouse_x": _bounded_finite_float(
                        mouse_x, "mouse_x", minimum=-100_000.0, maximum=100_000.0
                    ),
                    "mouse_y": _bounded_finite_float(
                        mouse_y, "mouse_y", minimum=-100_000.0, maximum=100_000.0
                    ),
                    "frame_count": frame_count,
                    "hold_frame_count": hold_frame_count,
                    "wait_frame_count": wait_frame_count,
                    "wait_seconds": settle_wait,
                    "pause_on_complete": bool(pause_on_complete),
                    "stop_assertions": assertions,
                    "stop_mode": normalized_stop_mode,
                    "pause_on_condition": bool(pause_on_condition),
                },
                timeout_seconds=timeout_seconds,
            )

    def player_motion_capture_status(
        self,
        capture_id: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        with self._operation_lock():
            return self._call_player_control(
                "motion_capture_status",
                {"capture_id": str(capture_id or "").strip()},
                timeout_seconds=timeout_seconds,
            )

    def player_motion_capture_cancel(
        self,
        capture_id: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        with self._operation_lock():
            return self._call_player_control(
                "motion_capture_cancel",
                {"capture_id": str(capture_id or "").strip()},
                timeout_seconds=timeout_seconds,
            )

    def player_observe(
        self,
        object_names: list[str] | None = None,
        *,
        component_probes: list[dict[str, Any]] | None = None,
        include_scene_objects: bool = False,
        discovery_component_types: list[str] | None = None,
        max_discovered_objects: int = 32,
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        names = [str(name or "").strip() for name in (object_names or []) if str(name or "").strip()]
        if len(names) > 32:
            raise ValueError("object_names cannot contain more than 32 entries.")
        probes = _normalize_player_component_probes(component_probes, names)
        discovery_types = _normalize_player_discovery_component_types(discovery_component_types)
        discovered_limit = _normalize_player_discovered_object_count(max_discovered_objects)
        with self._operation_lock():
            return self._call_player_control(
                "observe",
                {
                    "object_names": names,
                    "component_probes": probes,
                    "include_scene_objects": bool(include_scene_objects),
                    "discovery_component_types": discovery_types,
                    "max_discovered_objects": discovered_limit,
                },
                timeout_seconds=timeout_seconds,
            )

    def player_read_logs(self, *, limit: int = 200) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 1000))
        return {
            "runtime_path": self.player_runtime_log_path,
            "runtime_lines": _tail_text_lines(self.player_runtime_log_path, bounded),
            "debug_path": self.player_debug_log_path,
            "debug_lines": _tail_text_lines(self.player_debug_log_path, bounded),
            "crash_path": self.player_crash_log_path,
            "crash_lines": _tail_text_lines(self.player_crash_log_path, bounded),
            "stdout_path": self.player_stdout_path,
            "stdout_lines": _tail_text_lines(self.player_stdout_path, bounded),
        }

    def stop_player(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Stop a managed Player through its main-thread engine shutdown path."""
        with self._operation_lock():
            player = self._player_state()
            if not player["running"]:
                self._mark_player_stopped()
                return self.status() | {"stopped": True, "already_stopped": True}
            if len(self._player_control_token) < 16:
                raise RuntimeError("Cannot request normal Player shutdown without its private control token.")
            response = self._call_player_control("shutdown", {}, timeout_seconds=timeout_seconds)
            if not bool(response.get("close_requested")):
                raise RuntimeError("Player rejected the normal Supervisor shutdown request.")
            if not _wait_for_pid_exit(int(player["pid"]), timeout_seconds):
                raise RuntimeError(
                    "Player did not complete normal engine shutdown before the timeout. "
                    "The Supervisor left it running and did not force-terminate the process."
                )
            self._mark_player_stopped()
            return self.status() | {"stopped": True, "already_stopped": False}

    def _call_player_control(
        self,
        action: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        timeout = _bounded_finite_float(timeout_seconds, "timeout_seconds", minimum=0.1, maximum=300.0)
        player = self._player_state()
        if not player["running"]:
            raise RuntimeError("Standalone Player is not running.")
        if not self._player_ready:
            raise RuntimeError("Standalone Player has not reported readiness.")
        command_id = f"player-{uuid.uuid4().hex}"
        for path in (self.player_response_path, self.player_control_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        _write_json(self.player_control_path, {
            "command_id": command_id,
            "token": self._player_control_token,
            "action": str(action),
            **arguments,
        })
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = _read_json_object(self.player_response_path)
            if str(response.get("command_id", "") or "") == command_id:
                if not bool(response.get("ok")):
                    raise RuntimeError(f"Player control command failed: {response.get('error', 'unknown error')}")
                data = response.get("data", {})
                if not isinstance(data, dict):
                    raise RuntimeError("Player control command returned an invalid response payload.")
                return data
            if not self._player_state()["running"]:
                raise RuntimeError("Player exited before acknowledging the control command.")
            time.sleep(0.02)
        raise TimeoutError(f"Timed out waiting for Player control action '{action}'.")

    def _wait_for_verified_mcp_ready(self, *, timeout_seconds: float) -> dict[str, Any]:
        readiness = self.wait_for_mcp_ready(timeout_seconds=timeout_seconds)
        if not readiness.get("mcp_ready"):
            return readiness
        try:
            self._verify_attached_editor(timeout_seconds=timeout_seconds)
        except Exception as exc:
            self._mcp_ready = False
            self._persist_state()
            return self.status() | {
                "mcp_ready": False,
                "ready_error": f"MCP identity verification failed: {exc}",
            }
        return self.status() | {"mcp_ready": True, "ready_error": ""}

    def wait_for_mcp_ready(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Wait for the child Editor's loopback MCP endpoint to accept probes."""
        deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
        last_error = ""
        while time.monotonic() < deadline:
            editor = self._editor_state()
            if not editor["running"]:
                return self.status() | {
                    "mcp_ready": False,
                    "ready_error": "Editor exited before MCP became ready.",
                }
            try:
                request = urllib.request.Request(self.mcp_health_endpoint, method="GET")
                with urllib.request.urlopen(request, timeout=1.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("name") == "Infernux Editor":
                    self._mcp_ready = True
                    self._persist_state()
                    return self.status() | {"mcp_ready": True, "ready_error": ""}
                last_error = "Unexpected MCP probe response."
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            time.sleep(0.1)
        return self.status() | {"mcp_ready": False, "ready_error": last_error or "Timed out waiting for MCP endpoint."}

    def create_loopback_client(self, *, timeout_seconds: float | None = None):
        """Return the proxy-safe FastMCP client for this Supervisor session."""
        from Infernux.mcp.client import create_loopback_client

        return create_loopback_client(self.mcp_endpoint, timeout_seconds=timeout_seconds)

    def stop_editor(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Close a verified Editor through its normal in-Editor shutdown lifecycle."""
        editor = self._editor_state()
        if not editor["running"]:
            self._mark_editor_stopped()
            return self.status() | {"stopped": True, "already_stopped": True}
        if not self._has_leased_editor_identity():
            raise RuntimeError("Cannot request a normal Editor shutdown without a verified Supervisor lease.")
        self._verify_attached_editor(timeout_seconds=timeout_seconds)
        result = self._call_mcp_tool(
            "mcp_supervisor_shutdown",
            {"lease_token": self._supervisor_lease},
            timeout_seconds=timeout_seconds,
        )
        if not bool(result.get("close_requested")):
            raise RuntimeError("Editor rejected the normal Supervisor shutdown request.")
        if not self._wait_for_clean_editor_shutdown(int(editor["pid"]), timeout_seconds=timeout_seconds):
            raise RuntimeError(
                "Editor did not complete its normal shutdown before the handoff timeout. "
                "The Supervisor left it running and did not force-terminate the process."
            )
        self._mark_editor_stopped()
        return self.status() | {"stopped": True, "already_stopped": False}

    def create_checkpoint(
        self,
        checkpoint_id: str,
        *,
        reason: str = "",
        restart_editor: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Create a hash-verified project checkpoint while the Editor is clean and stopped."""
        with self._operation_lock():
            safe_id = checkpoint_store.normalize_checkpoint_id(checkpoint_id)
            destination = checkpoint_store.checkpoint_directory(self.artifact_root, safe_id)
            if os.path.exists(destination):
                raise FileExistsError(f"Managed checkpoint already exists: {safe_id}")
            return self._create_checkpoint_locked(
                safe_id,
                reason=reason,
                restart_editor=restart_editor,
                timeout_seconds=timeout_seconds,
            )

    def _create_checkpoint_locked(
        self,
        checkpoint_id: str,
        *,
        reason: str,
        restart_editor: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        previous = self.status()
        event = {
            "kind": "infernux.mcp.checkpoint_create_event",
            "operation_id": f"checkpoint-create-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
            "checkpoint_id": checkpoint_id,
            "reason": str(reason or ""),
            "started_at": time.time(),
            "state": "started",
            "editor_was_running": bool(previous["editor_running"]),
        }
        _append_json_line(self.checkpoint_history_path, event)
        try:
            event["preflight"] = self._preflight_handoff(timeout_seconds=timeout_seconds)
            if previous["editor_running"]:
                self.stop_editor(timeout_seconds=timeout_seconds)
            self.managed_checkpoints_required = True
            self.prepare_project()
            manifest = checkpoint_store.create_checkpoint(
                self.project_root,
                self.artifact_root,
                checkpoint_id,
                session_id=self.session_id,
                metadata={
                    "reason": str(reason or ""),
                    "mode": self.mode,
                    "build_profile": self.build_profile,
                },
            )
            launch = None
            if previous["editor_running"] and restart_editor:
                launch = self.launch_editor(wait_for_mcp=True, timeout_seconds=timeout_seconds)
                if not launch.get("mcp_ready"):
                    raise RuntimeError("Editor restarted after checkpoint creation but MCP did not become ready.")
            event.update({
                "state": "completed",
                "completed_at": time.time(),
                "ledger_digest": manifest["ledger"]["digest"],
                "file_count": manifest["ledger"]["file_count"],
                "total_bytes": manifest["ledger"]["total_bytes"],
                "manifest_path": manifest["manifest_path"],
            })
            _append_json_line(self.checkpoint_history_path, event)
            return self.status() | {
                "checkpoint": _compact_checkpoint_manifest(manifest),
                "checkpoint_event": event,
                "launch": launch,
            }
        except Exception as exc:
            event.update({"state": "failed", "failed_at": time.time(), "error": str(exc)})
            _append_json_line(self.checkpoint_history_path, event)
            self._restart_after_failed_checkpoint_operation(previous, restart_editor, timeout_seconds)
            raise

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        *,
        reason: str = "",
        restart_editor: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Restore a managed project checkpoint with rollback and post-restore hash proof."""
        with self._operation_lock():
            safe_id = checkpoint_store.normalize_checkpoint_id(checkpoint_id)
            checkpoint_store.load_checkpoint(
                self.project_root,
                self.artifact_root,
                safe_id,
                session_id=self.session_id,
                verify_payload=True,
            )
            return self._restore_checkpoint_locked(
                safe_id,
                reason=reason,
                restart_editor=restart_editor,
                timeout_seconds=timeout_seconds,
            )

    def _restore_checkpoint_locked(
        self,
        checkpoint_id: str,
        *,
        reason: str,
        restart_editor: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        previous = self.status()
        event = {
            "kind": "infernux.mcp.checkpoint_restore_event",
            "operation_id": f"checkpoint-restore-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
            "checkpoint_id": checkpoint_id,
            "reason": str(reason or ""),
            "started_at": time.time(),
            "state": "started",
            "editor_was_running": bool(previous["editor_running"]),
        }
        _append_json_line(self.checkpoint_history_path, event)
        try:
            event["preflight"] = self._preflight_handoff(timeout_seconds=timeout_seconds)
            if previous["editor_running"]:
                self.stop_editor(timeout_seconds=timeout_seconds)
            proof = checkpoint_store.restore_checkpoint(
                self.project_root,
                self.artifact_root,
                checkpoint_id,
                session_id=self.session_id,
            )
            self.managed_checkpoints_required = True
            self.prepare_project()
            verified = checkpoint_store.checkpoint_status(
                self.project_root,
                self.artifact_root,
                checkpoint_id,
                session_id=self.session_id,
            )
            if not verified.get("current_match"):
                raise RuntimeError("Project changed after restore before the Editor restart could be verified.")
            launch = None
            if previous["editor_running"] and restart_editor:
                launch = self.launch_editor(wait_for_mcp=True, timeout_seconds=timeout_seconds)
                if not launch.get("mcp_ready"):
                    raise RuntimeError("Editor restarted after checkpoint restore but MCP did not become ready.")
            event.update({
                "state": "completed",
                "completed_at": time.time(),
                "proof_path": proof["proof_path"],
                "before_ledger_digest": proof["before_ledger_digest"],
                "after_ledger_digest": proof["after_ledger_digest"],
                "delta": checkpoint_store.compact_delta(proof["delta"]),
            })
            _append_json_line(self.checkpoint_history_path, event)
            return self.status() | {
                "checkpoint_restore": event,
                "checkpoint_status": verified,
                "launch": launch,
            }
        except Exception as exc:
            event.update({"state": "failed", "failed_at": time.time(), "error": str(exc)})
            _append_json_line(self.checkpoint_history_path, event)
            self._restart_after_failed_checkpoint_operation(previous, restart_editor, timeout_seconds)
            raise

    def checkpoint_status(self, checkpoint_id: str) -> dict[str, Any]:
        return checkpoint_store.checkpoint_status(
            self.project_root,
            self.artifact_root,
            checkpoint_id,
            session_id=self.session_id,
        )

    def list_checkpoints(self) -> list[dict[str, Any]]:
        root = os.path.join(self.artifact_root, "checkpoints")
        if not os.path.isdir(root):
            return []
        results = []
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path) or name.startswith("."):
                continue
            try:
                manifest = checkpoint_store.load_checkpoint(
                    self.project_root,
                    self.artifact_root,
                    name,
                    session_id=self.session_id,
                    verify_payload=False,
                )
            except (OSError, ValueError, checkpoint_store.CheckpointError):
                continue
            results.append(_compact_checkpoint_manifest(manifest))
        return results

    def _restart_after_failed_checkpoint_operation(
        self,
        previous_status: dict[str, Any],
        restart_editor: bool,
        timeout_seconds: float,
    ) -> None:
        if not previous_status.get("editor_running") or not restart_editor or self._editor_state()["running"]:
            return
        try:
            self.launch_editor(wait_for_mcp=True, timeout_seconds=timeout_seconds)
        except Exception:
            pass

    def force_stop_editor(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Emergency-only process termination for an abandoned Editor, never used by handoff."""
        editor = self._editor_state()
        if not editor["running"]:
            self._mark_editor_stopped()
            return self.status() | {"stopped": True, "already_stopped": True, "forced": False}
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(float(timeout_seconds), 0.1))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=max(float(timeout_seconds), 0.1))
        else:
            _terminate_pid(int(editor["pid"]))
            if not _wait_for_pid_exit(int(editor["pid"]), timeout_seconds):
                raise RuntimeError(f"Attached Editor process {editor['pid']} did not stop within the timeout.")
        self._mark_editor_stopped()
        return self.status() | {"stopped": True, "already_stopped": False, "forced": True}

    def handoff_mode(
        self,
        target_mode: str,
        *,
        checkpoint: str,
        reason: str = "",
        build_profile: str | None = None,
        recording_enabled: bool | None = None,
        restart_editor: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Compatibility spelling for the explicit :meth:`switch_mode` API.

        Legacy callers must still provide a checkpoint. New orchestration code
        should use ``switch_mode`` so the default un-managed session checkpoint
        is explicit in the returned audit record.
        """
        with self._operation_lock():
            return self._handoff_mode_locked(
                target_mode,
                checkpoint=checkpoint,
                reason=reason,
                build_profile=build_profile,
                recording_enabled=recording_enabled,
                restart_editor=restart_editor,
                timeout_seconds=timeout_seconds,
            )

    def switch_mode(
        self,
        target_mode: str,
        *,
        checkpoint: str = "",
        reason: str = "",
        build_profile: str | None = None,
        recording_enabled: bool | None = None,
        restart_editor: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Atomically hand one visible Editor session to another MCP mode.

        The two profiles remain disjoint. This method only changes the
        Supervisor-owned project policy after the old endpoint has shut down,
        then starts a fresh Editor identity and verifies its project, session,
        mode, profile, lease, and project lock before returning success.
        """
        requested_checkpoint = str(checkpoint or "").strip()
        if not requested_checkpoint:
            if self.managed_checkpoints_required:
                raise ValueError("Managed mode switches require an explicit checkpoint.")
            requested_checkpoint = "session-start"
        with self._operation_lock():
            return self._handoff_mode_locked(
                target_mode,
                checkpoint=requested_checkpoint,
                reason=reason,
                build_profile=build_profile,
                recording_enabled=recording_enabled,
                restart_editor=restart_editor,
                timeout_seconds=timeout_seconds,
            )

    def handoff_history(self) -> list[dict[str, Any]]:
        """Return the secret-free mode handoff audit trail for this session."""
        path = self.handoff_history_path
        if not os.path.isfile(path):
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    value = _read_json_line(line)
                    if value:
                        records.append(value)
        except OSError:
            return records
        return records

    def _handoff_mode_locked(
        self,
        target_mode: str,
        *,
        checkpoint: str,
        reason: str = "",
        build_profile: str | None = None,
        recording_enabled: bool | None = None,
        restart_editor: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Safely move a project between constrained MCP operating modes.

        A handoff is intentionally not a scene backup or restore mechanism.  It
        records the declared checkpoint and refuses to terminate a managed Editor
        unless ``project_info`` reports a clean scene in edit mode.
        """
        next_mode = _require_choice("target_mode", target_mode, VALID_MODES)
        checkpoint = str(checkpoint or "").strip()
        if not checkpoint:
            raise ValueError("A non-empty handoff checkpoint is required.")

        next_build_profile = _require_choice(
            "build_profile",
            self.build_profile if build_profile is None else build_profile,
            VALID_BUILD_PROFILES,
        )
        requested_recording = self.recording_enabled if recording_enabled is None else bool(recording_enabled)
        next_recording_enabled = bool(requested_recording and next_build_profile == "debug_feedback")
        previous_status = self.status()
        event: dict[str, Any] = {
            "handoff_id": f"handoff-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
            "started_at": time.time(),
            "state": "started",
            "phase": "preflight",
            "checkpoint": checkpoint,
            "reason": str(reason or ""),
            "from": {
                "mode": self.mode,
                "build_profile": self.build_profile,
                "recording_enabled": self.recording_enabled,
                "editor_running": previous_status["editor_running"],
            },
            "to": {
                "mode": next_mode,
                "build_profile": next_build_profile,
                "recording_enabled": next_recording_enabled,
                "restart_editor": bool(restart_editor),
            },
        }

        try:
            preflight = self._preflight_handoff(timeout_seconds=timeout_seconds)
            event["preflight"] = preflight
            if self.managed_checkpoints_required:
                checkpoint_status = checkpoint_store.checkpoint_status(
                    self.project_root,
                    self.artifact_root,
                    checkpoint,
                    session_id=self.session_id,
                )
                if not (
                    checkpoint_status.get("exists")
                    and checkpoint_status.get("payload_valid")
                    and checkpoint_status.get("current_match")
                ):
                    raise RuntimeError(
                        "Cannot hand off with a managed checkpoint that is missing, invalid, or stale. "
                        "Ask the external Supervisor to create a new clean checkpoint or restore the requested one."
                    )
                event["checkpoint_proof"] = {
                    "checkpoint_id": checkpoint,
                    "ledger_digest": checkpoint_status.get("ledger_digest", ""),
                    "file_count": checkpoint_status.get("file_count", 0),
                    "total_bytes": checkpoint_status.get("total_bytes", 0),
                }
            _append_json_line(self.handoff_history_path, event)

            was_running = bool(previous_status["editor_running"])
            same_policy = (
                self.mode == next_mode
                and self.build_profile == next_build_profile
                and self.recording_enabled == next_recording_enabled
            )
            if same_policy:
                event.update({
                    "state": "completed",
                    "phase": "noop",
                    "completed_at": time.time(),
                    "result": {
                        "editor_restarted": False,
                        "mcp_ready": bool(previous_status["mcp_ready"]),
                        "no_change": True,
                    },
                })
                self._last_handoff = dict(event)
                _append_json_line(self.handoff_history_path, event)
                self._persist_state()
                return self.status() | {"handoff": event, "launch": None}

            if was_running:
                event["phase"] = "stopping_old_editor"
                stopped = self.stop_editor(timeout_seconds=timeout_seconds)
                if not stopped.get("stopped") or stopped.get("editor_running"):
                    raise RuntimeError("Supervisor could not stop the Editor during mode handoff.")

            event["phase"] = "publishing_policy"
            self.mode = next_mode
            self.build_profile = next_build_profile
            self.recording_enabled = next_recording_enabled
            configured = self.prepare_project()
            event["configured_at"] = time.time()
            event["configured"] = {
                "mode": configured["mode"],
                "build_profile": configured["build_profile"],
                "recording_enabled": configured["recording_enabled"],
            }

            launch_status: dict[str, Any] | None = None
            if was_running and restart_editor:
                event["phase"] = "starting_new_editor"
                launch_status = self.launch_editor(wait_for_mcp=True, timeout_seconds=timeout_seconds)
                if not launch_status.get("mcp_ready"):
                    raise RuntimeError(
                        "Editor restarted for mode handoff but MCP did not become ready: "
                        f"{launch_status.get('ready_error', 'unknown error')}"
                    )

            event["state"] = "completed"
            event["phase"] = "verified"
            event["completed_at"] = time.time()
            event["result"] = {
                "editor_restarted": bool(was_running and restart_editor),
                "mcp_ready": bool(launch_status and launch_status.get("mcp_ready")),
            }
            self._last_handoff = dict(event)
            _append_json_line(self.handoff_history_path, event)
            self._persist_state()
            return self.status() | {"handoff": event, "launch": launch_status}
        except Exception as exc:
            event["state"] = "failed"
            event["phase"] = "failed"
            event["failed_at"] = time.time()
            event["error"] = str(exc)
            self._last_handoff = dict(event)
            _append_json_line(self.handoff_history_path, event)
            self._persist_state()
            raise

    def _preflight_handoff(self, *, timeout_seconds: float) -> dict[str, Any]:
        """Return the state that made an in-place handoff safe to perform."""
        if self._player_state()["running"]:
            raise RuntimeError("Cannot hand off MCP mode while a standalone validation Player is running.")
        if not self._editor_state()["running"]:
            if _mcp_health_is_alive(self.mcp_health_endpoint):
                raise RuntimeError(
                    "Cannot hand off because a live Infernux MCP endpoint exists without a verified Supervisor-owned Editor PID."
                )
            return {"required": False, "editor_running": False}

        self._verify_attached_editor(timeout_seconds=timeout_seconds)
        session_status = self._read_mcp_session_status(timeout_seconds=timeout_seconds)
        if bool(session_status.get("attempt_active")):
            raise RuntimeError("Cannot hand off while a global validation attempt is still active.")

        project_info = self._read_project_info(timeout_seconds=timeout_seconds)
        active_scene = project_info.get("active_scene") or {}
        if bool(active_scene.get("dirty")):
            raise RuntimeError("Cannot hand off while the active scene has unsaved changes.")
        if project_info.get("play_state") != "edit":
            raise RuntimeError("Cannot hand off while the Editor is not in edit mode.")
        return {
            "required": True,
            "editor_running": True,
            "active_scene": active_scene,
            "play_state": project_info["play_state"],
            "attempt_active": False,
            "editor_instance_id": self._editor_instance_id,
        }

    def _read_project_info(self, *, timeout_seconds: float) -> dict[str, Any]:
        return self._call_mcp_tool("project_info", {}, timeout_seconds=timeout_seconds)

    def _read_mcp_session_status(self, *, timeout_seconds: float) -> dict[str, Any]:
        return self._call_mcp_tool("mcp_session_status", {}, timeout_seconds=timeout_seconds)

    def _call_mcp_tool(self, tool_name: str, arguments: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        async def query_project_info() -> dict[str, Any]:
            async with self.create_loopback_client(timeout_seconds=timeout_seconds) as client:
                result = await client.call_tool(tool_name, arguments)
            payload = getattr(result, "data", None)
            if not isinstance(payload, dict):
                payload = getattr(result, "structured_content", None)
            if not isinstance(payload, dict):
                raise RuntimeError(f"{tool_name} returned an invalid MCP payload.")
            if payload.get("ok") is False:
                raise RuntimeError(f"{tool_name} failed: {payload.get('error', 'unknown error')}")
            info = payload.get("data", payload)
            if not isinstance(info, dict):
                raise RuntimeError(f"{tool_name} did not return an object.")
            return info

        return _run_async(query_project_info)

    def _verify_attached_editor(self, *, timeout_seconds: float) -> dict[str, Any]:
        """Verify that an endpoint, lease, and project lock all name this exact Editor instance."""
        if not self._has_leased_editor_identity():
            raise RuntimeError("Supervisor session has no Editor instance identity or shutdown lease to verify.")
        readiness = self.wait_for_mcp_ready(timeout_seconds=timeout_seconds)
        if not readiness.get("mcp_ready"):
            raise RuntimeError(
                "Cannot resume Supervisor session because its MCP endpoint is unavailable: "
                f"{readiness.get('ready_error', 'unknown error')}"
            )
        observed = self._read_mcp_session_status(timeout_seconds=timeout_seconds)
        observed_root = resolved_path(str(observed.get("project_root", "") or ""))
        observed_session_id = str(observed.get("session_id", "") or "")
        if not same_path(observed_root, self.project_root):
            raise RuntimeError("MCP endpoint project root does not match the persisted Supervisor session.")
        if observed_session_id != self.session_id:
            raise RuntimeError("MCP endpoint session_id does not match the persisted Supervisor session.")
        if observed.get("mode") != self.mode:
            raise RuntimeError("MCP endpoint mode does not match the persisted Supervisor session.")
        if observed.get("build_profile") != self.build_profile:
            raise RuntimeError("MCP endpoint build profile does not match the persisted Supervisor session.")
        if str(observed.get("editor_instance_id", "") or "") != self._editor_instance_id:
            raise RuntimeError("MCP endpoint Editor instance ID does not match the persisted Supervisor session.")
        if not bool(observed.get("supervisor_lease_configured")):
            raise RuntimeError("MCP endpoint was not configured with a Supervisor shutdown lease.")
        if str(observed.get("supervisor_lease_fingerprint", "") or "") != _secret_fingerprint(self._supervisor_lease):
            raise RuntimeError("MCP endpoint Supervisor lease fingerprint does not match the persisted session.")
        self._verify_project_lock_ownership()
        self._mcp_ready = True
        self._persist_state()
        return observed

    def _try_attach_matching_unmanaged_editor(self, *, timeout_seconds: float) -> bool:
        """Observe a manually launched Editor without taking ownership of it.

        Player validation can survive the original Supervisor host exiting while
        a user continues the exact same validation Editor manually.  This path
        intentionally accepts only a live endpoint and project lock which both
        agree with the persisted session's public identity.  It does not grant
        shutdown ownership because the original private lease is unavailable.
        """
        try:
            observed = self._read_mcp_session_status(timeout_seconds=timeout_seconds)
        except Exception:
            return False
        observed_root = resolved_path(str(observed.get("project_root", "") or ""))
        if not same_path(observed_root, self.project_root):
            return False
        if str(observed.get("session_id", "") or "") != self.session_id:
            return False
        if observed.get("mode") != self.mode or observed.get("build_profile") != self.build_profile:
            return False
        lock = _read_json_object(self.project_lock_path)
        lock_pid = int(lock.get("pid", 0) or 0) if lock else 0
        lock_project = resolved_path(str(lock.get("project_path", "") or "")) if lock else ""
        if lock_pid <= 0 or not _pid_is_running(lock_pid) or not same_path(lock_project, self.project_root):
            return False
        self._attached_editor_pid = lock_pid
        self._mcp_ready = True
        return True

    def _editor_state(self) -> dict[str, Any]:
        process = self._process
        if process is not None:
            exit_code = process.poll()
            return {
                "pid": int(process.pid),
                "running": exit_code is None,
                "exit_code": exit_code,
                "owner": "supervisor",
            }
        pid = int(self._attached_editor_pid or 0)
        if pid > 0 and _pid_is_running(pid):
            return {"pid": pid, "running": True, "exit_code": None, "owner": "reattached"}
        if pid > 0:
            self._attached_editor_pid = 0
        return {"pid": 0, "running": False, "exit_code": None, "owner": "none"}

    def _player_state(self) -> dict[str, Any]:
        process = self._player_process
        if process is not None:
            exit_code = process.poll()
            return {
                "pid": int(process.pid),
                "running": exit_code is None,
                "exit_code": exit_code,
                "owner": "supervisor",
            }
        pid = int(self._attached_player_pid or 0)
        if pid > 0 and _pid_is_running(pid):
            return {"pid": pid, "running": True, "exit_code": None, "owner": "reattached"}
        if pid > 0:
            self._attached_player_pid = 0
        return {"pid": 0, "running": False, "exit_code": None, "owner": "none"}

    def status(self) -> dict[str, Any]:
        editor = self._editor_state()
        player = self._player_state()
        if not editor["running"]:
            self._mcp_ready = False
        return {
            "session_id": self.session_id,
            "project_root": self.project_root,
            "mode": self.mode,
            "build_profile": self.build_profile,
            "recording_enabled": self.recording_enabled,
            "managed_checkpoints_required": self.managed_checkpoints_required,
            "checkpoint_count": len(self.list_checkpoints()),
            "mcp_endpoint": self.mcp_endpoint,
            "mcp_health_endpoint": self.mcp_health_endpoint,
            "mcp_ready": self._mcp_ready,
            "editor_log_path": self.editor_log_path,
            "artifact_root": self.artifact_root,
            "started_at": self.started_at,
            "editor_pid": int(editor["pid"]),
            "editor_running": bool(editor["running"]),
            "editor_exit_code": editor["exit_code"],
            "editor_process_owner": editor["owner"],
            "editor_instance_id": self._editor_instance_id,
            "supervisor_lease_fingerprint": _secret_fingerprint(self._supervisor_lease),
            "project_lock_fingerprint": _secret_fingerprint(self._project_lock_token),
            "player_pid": int(player["pid"]),
            "player_running": bool(player["running"]),
            "player_exit_code": player["exit_code"],
            "player_process_owner": player["owner"],
            "player_ready": bool(self._player_ready and player["running"]),
            "player_executable": self._player_executable,
            "player_start_scene": self._player_start_scene,
            "player_stdout_path": self.player_stdout_path,
            "player_runtime_log_path": self.player_runtime_log_path,
            "player_debug_log_path": self.player_debug_log_path,
            "player_crash_log_path": self.player_crash_log_path,
            "player_control_fingerprint": _secret_fingerprint(self._player_control_token),
            "last_handoff": dict(self._last_handoff),
            "agent_handoff": self.agent_handoff(),
        }

    def agent_handoff(self) -> dict[str, Any]:
        """Return a secret-free connection bundle suitable for a constrained subagent."""
        base_argv = [
            self.python_executable,
            "-m",
            "Infernux.mcp.client",
            "--endpoint",
            self.mcp_endpoint,
        ]
        return {
            "project_root": self.project_root,
            "working_directory": self.project_root,
            "session_id": self.session_id,
            "mode": self.mode,
            "build_profile": self.build_profile,
            "recording_enabled": self.recording_enabled,
            "managed_checkpoints_required": self.managed_checkpoints_required,
            "last_handoff": dict(self._last_handoff),
            "endpoint": self.mcp_endpoint,
            "health_endpoint": self.mcp_health_endpoint,
            "environment_activation": ["conda", "activate", "infernux"],
            "client_base_argv": base_argv,
            "probe_argv": [*base_argv, "call", "mcp_session_status", "--args", "{}"],
            "list_tools_argv": [*base_argv, "list-tools"],
            "checkpoint_list_argv": [*base_argv, "call", "mcp_checkpoint_list", "--args", "{}"],
            "instructions": [
                "Run probe_argv through the available shell before deciding that MCP tools are unavailable.",
                "Use the returned MCP tool schema and current mode policy; do not infer unavailable privileged tools.",
                "A missing directly injected connector is not an MCP outage when probe_argv succeeds.",
                "Use Supervisor.switch_mode to move between developer_assist and global_validation; never assume a mode change happened until the returned endpoint identity and mode are verified.",
                "For a managed attempt, call mcp_checkpoint_list, then mcp_checkpoint_status for the selected checkpoint before mcp_attempt_start; only the external Supervisor may create or restore checkpoints.",
            ],
        }

    def _new_editor_identity(self) -> None:
        self._editor_instance_id = uuid.uuid4().hex
        self._supervisor_lease = secrets.token_urlsafe(32)
        self._project_lock_token = secrets.token_urlsafe(32)

    def _has_leased_editor_identity(self) -> bool:
        return bool(self._editor_instance_id and self._supervisor_lease and self._project_lock_token)

    def _state_payload(self) -> dict[str, Any]:
        """Persist private continuity data without exposing it from the public status API."""
        return self.status() | {
            "editor_instance_id": self._editor_instance_id,
            "supervisor_lease": self._supervisor_lease,
            "project_lock_token": self._project_lock_token,
            "player_control_token": self._player_control_token,
        }

    def _persist_state(self) -> None:
        _write_json(self.state_path, self._state_payload())
        _write_json(self.agent_handoff_path, self.agent_handoff())

    def _mark_editor_stopped(self) -> None:
        self._mcp_ready = False
        self._process = None
        self._attached_editor_pid = 0
        self._editor_instance_id = ""
        self._supervisor_lease = ""
        self._project_lock_token = ""
        self._close_editor_log()
        self._persist_state()

    def _mark_player_stopped(self) -> None:
        self._player_process = None
        self._attached_player_pid = 0
        self._player_control_token = ""
        self._player_start_scene = ""
        self._player_ready = False
        self._close_player_log()
        for path in (self.player_control_path, self.player_ready_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        self._persist_state()

    def _verify_project_lock_ownership(self) -> None:
        editor = self._editor_state()
        lock = _read_json_object(self.project_lock_path)
        if not lock:
            raise RuntimeError("Editor project lock is missing during Supervisor identity verification.")
        if str(lock.get("token", "") or "") != self._project_lock_token:
            raise RuntimeError("Editor project lock token does not match the persisted Supervisor session.")
        if int(lock.get("pid", 0) or 0) != int(editor.get("pid", 0) or 0):
            raise RuntimeError("Editor project lock PID does not match the running Editor process.")
        lock_project = resolved_path(str(lock.get("project_path", "") or ""))
        if not same_path(lock_project, self.project_root):
            raise RuntimeError("Editor project lock project path does not match the persisted Supervisor session.")

    def _wait_for_clean_editor_shutdown(self, editor_pid: int, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
        while True:
            editor_stopped = not _pid_is_running(editor_pid)
            endpoint_stopped = not _mcp_health_is_alive(self.mcp_health_endpoint)
            project_lock_released = not _read_json_object(self.project_lock_path)
            if editor_stopped and endpoint_stopped and not project_lock_released:
                project_lock_released = self._remove_stale_owned_project_lock(editor_pid)
            if editor_stopped and endpoint_stopped and project_lock_released:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            time.sleep(min(0.05, remaining))

    def _remove_stale_owned_project_lock(self, editor_pid: int) -> bool:
        lock = _read_json_object(self.project_lock_path)
        if not lock:
            return True
        if int(lock.get("pid", 0) or 0) != int(editor_pid):
            return False
        if str(lock.get("token", "") or "") != self._project_lock_token:
            return False
        try:
            os.remove(self.project_lock_path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    @contextmanager
    def _operation_lock(self):
        """Prevent two external Supervisors from mutating a session artifact concurrently."""
        os.makedirs(self.artifact_root, exist_ok=True)
        token = secrets.token_urlsafe(18)
        payload = {"pid": os.getpid(), "token": token, "started_at": time.time()}
        while True:
            try:
                descriptor = os.open(self.operation_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                existing = _read_json_object(self.operation_lock_path)
                existing_pid = int(existing.get("pid", 0) or 0)
                if existing_pid > 0 and _pid_is_running(existing_pid):
                    raise RuntimeError("Another Supervisor operation is already active for this project session.")
                try:
                    os.remove(self.operation_lock_path)
                except FileNotFoundError:
                    pass
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(payload, f, ensure_ascii=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                yield
            finally:
                current = _read_json_object(self.operation_lock_path)
                if current.get("token") == token:
                    try:
                        os.remove(self.operation_lock_path)
                    except FileNotFoundError:
                        pass
            return

    def _close_editor_log(self) -> None:
        handle = self._editor_log_handle
        self._editor_log_handle = None
        if handle is not None:
            handle.close()

    def _close_player_log(self) -> None:
        handle = self._player_log_handle
        self._player_log_handle = None
        if handle is not None:
            handle.close()


def _validate_player_executable(executable_path: str, project_root: str) -> tuple[str, dict[str, Any]]:
    launcher = resolved_path(str(executable_path or ""))
    if not os.path.isfile(launcher):
        raise FileNotFoundError(f"Player executable was not found: {launcher}")

    output_root = os.path.dirname(launcher)
    game_name = os.path.splitext(os.path.basename(launcher))[0]
    data_root = resolved_path(os.path.join(output_root, f"{game_name}_Data"))
    manifest_path = os.path.join(data_root, "BuildManifest.json")
    manifest = _read_json_object(manifest_path)
    if not manifest:
        raise FileNotFoundError(f"Player BuildManifest was not found: {manifest_path}")

    player_manifest = _read_json_object(os.path.join(data_root, "Player.inxmanifest"))
    product = player_manifest.get("product") or {}
    if product.get("layout") == "infernux-single-entry-player":
        entry_points = [str(value or "") for value in product.get("entry_points", []) or []]
        if not bool(product.get("single_entry_point", False)) or entry_points != [os.path.basename(launcher)]:
            raise ValueError("Player single-entry manifest does not match the selected executable.")
        build_output = manifest.get("build_output") or {}
        if str(build_output.get("project_identity", "") or "") != path_fingerprint(project_root):
            raise ValueError("Player build output belongs to a different project.")
        if not bool(manifest.get("debug_build", False)):
            raise RuntimeError("Supervisor validation control is available only in a Debug Player build.")
        runtime_policy = (manifest.get("runtime_contract") or {}).get("runtime_policy") or {}
        if runtime_policy.get("player_control") != "token_authenticated":
            raise RuntimeError("Debug Player does not expose the authenticated validation control channel.")
        return launcher, manifest

    layout_path = os.path.join(data_root, "PlayerLayout.json")
    layout = _read_json_object(layout_path)
    if layout.get("layout") != "infernux-windows-player":
        raise ValueError("Player executable is not the launcher of a current Infernux Player layout.")
    if str(layout.get("launcher", "") or "") != os.path.basename(launcher):
        raise ValueError("Player layout launcher does not match the selected executable.")
    if str(layout.get("data_directory", "") or "") != os.path.basename(data_root):
        raise ValueError("Player layout data directory does not match the selected executable.")
    if str(layout.get("runtime_directory", "") or "") != "Runtime":
        raise ValueError("Player layout runtime directory is invalid.")

    runtime_executable = resolved_path(os.path.join(data_root, "Runtime", "InfernuxPlayer.exe"))
    if not is_path_within(runtime_executable, data_root, allow_root=False) or not os.path.isfile(runtime_executable):
        raise FileNotFoundError(f"Player runtime executable was not found: {runtime_executable}")

    marker = _read_json_object(os.path.join(data_root, ".infernux-build-output"))
    if marker.get("tool") != "Infernux" or marker.get("kind") != "build-output":
        raise ValueError("Player executable is not inside a verified Infernux build output directory.")
    if str(marker.get("project_identity", "") or "") != path_fingerprint(project_root):
        raise ValueError("Player build output belongs to a different project.")
    if not bool(manifest.get("debug_build", False)):
        raise RuntimeError("Supervisor validation control is available only in a Debug Player build.")
    return runtime_executable, manifest


def _player_data_root(runtime_executable: str) -> str:
    runtime = resolved_path(str(runtime_executable or ""))
    runtime_directory = os.path.dirname(runtime)
    single_entry_data = resolved_path(
        os.path.join(runtime_directory, f"{os.path.splitext(os.path.basename(runtime))[0]}_Data")
    )
    player_manifest = _read_json_object(os.path.join(single_entry_data, "Player.inxmanifest"))
    if (player_manifest.get("product") or {}).get("layout") == "infernux-single-entry-player":
        return single_entry_data
    if os.path.basename(runtime_directory) != "Runtime":
        raise ValueError("Player runtime executable is not inside the current organized Player layout.")
    data_root = resolved_path(os.path.dirname(runtime_directory))
    if not os.path.basename(data_root).endswith("_Data"):
        raise ValueError("Player runtime data directory is invalid.")
    return data_root


def _resolve_player_start_scene(start_scene: str, project_root: str, manifest: dict[str, Any]) -> str:
    """Return a BuildManifest-whitelisted relative scene path for Debug validation."""
    requested = str(start_scene or "").strip()
    if not requested:
        return ""

    root = resolved_path(project_root)
    candidate = resolved_path(requested if os.path.isabs(requested) else os.path.join(root, requested))
    if not is_path_within(candidate, root):
        raise ValueError("Player validation start_scene must stay inside the project root.")
    if os.path.splitext(candidate)[1].lower() != ".scene":
        raise ValueError("Player validation start_scene must name a .scene file.")

    for listed in manifest.get("scenes", []) or []:
        scene = str(listed or "").strip()
        if not scene:
            continue
        manifest_candidate = resolved_path(scene if os.path.isabs(scene) else os.path.join(root, scene))
        if same_path(manifest_candidate, candidate):
            return relative_path(manifest_candidate, root)
    raise ValueError("Player validation start_scene must be declared by the current Debug Player BuildManifest.")


def _compact_checkpoint_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    ledger = manifest.get("ledger") or {}
    return {
        "checkpoint_id": str(manifest.get("checkpoint_id", "") or ""),
        "created_at": float(manifest.get("created_at", 0.0) or 0.0),
        "manifest_path": str(manifest.get("manifest_path", "") or ""),
        "ledger_digest": str(ledger.get("digest", "") or ""),
        "file_count": int(ledger.get("file_count", 0) or 0),
        "total_bytes": int(ledger.get("total_bytes", 0) or 0),
        "metadata": dict(manifest.get("metadata") or {}),
    }


def _validate_project_root(path: str) -> None:
    root = resolved_path(path)
    if not os.path.isabs(root):
        raise ValueError("Project root must be an absolute path.")
    home = resolved_path(os.path.expanduser("~"))
    desktop = os.path.join(home, "Desktop")
    if same_path(root, desktop):
        raise ValueError("Project root must be a named folder under Desktop, not the entire Desktop.")


def _require_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    normalized = str(value or "")
    if normalized not in allowed:
        raise ValueError(f"Unsupported {name}: {normalized!r}")
    return normalized


def _require_loopback_host(value: str) -> str:
    host = str(value or "").strip()
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Supervisor MCP host must remain loopback-only.")
    return host


def _require_port(value: int) -> int:
    port = int(value)
    if port < 1024 or port > 65535:
        raise ValueError("Supervisor MCP port must be between 1024 and 65535.")
    return port


def _available_port(host: str, preferred: int) -> int:
    if _port_available(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


def _pid_is_running(pid: int) -> bool:
    """Return whether a persisted process identifier still belongs to a live process."""
    target = int(pid or 0)
    if target <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                target,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(target, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    """Terminate a reattached process after the caller has completed preflight."""
    target = int(pid or 0)
    if target <= 0:
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(target), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 and _pid_is_running(target):
            raise RuntimeError(f"Failed to terminate attached Editor process {target}.")
        return
    os.kill(target, signal.SIGTERM)


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _read_json_object(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_json_line(line: str) -> dict[str, Any]:
    try:
        value = json.loads(str(line or ""))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_handoff_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    state = str(value.get("state", "") or "")
    if state and state not in HANDOFF_STATES:
        return {}
    return dict(value)


def _normalize_player_component_probes(
    component_probes: list[dict[str, Any]] | None,
    object_names: list[str],
) -> list[dict[str, Any]]:
    raw_probes = component_probes or []
    if len(raw_probes) > 16:
        raise ValueError("component_probes cannot contain more than 16 entries.")
    allowed_names = set(object_names)
    probes = []
    for raw in raw_probes:
        if not isinstance(raw, dict):
            raise ValueError("component_probes entries must be objects.")
        object_name = str(raw.get("object_name", "") or "").strip()
        component_type = str(raw.get("component_type", "") or "").strip()
        fields = [str(field or "").strip() for field in raw.get("fields", [])]
        ordinal = int(raw.get("ordinal", 0) or 0)
        if object_name not in allowed_names:
            raise ValueError("component probe object_name must also be present in object_names.")
        if not component_type or ordinal < 0 or not fields or len(fields) > 16:
            raise ValueError("component probes require a public component type, ordinal, and 1-16 fields.")
        if any(not field or field.startswith("_") for field in fields):
            raise ValueError("component probe fields must be non-empty public field names.")
        probes.append({
            "object_name": object_name,
            "component_type": component_type,
            "fields": fields,
            "ordinal": ordinal,
        })
    return probes


def _bounded_finite_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return result


def _normalize_player_hold_scancodes(
    hold_key: str | int | None,
    hold_keys: list[str | int] | None,
) -> list[int]:
    if hold_key is not None and hold_keys:
        raise ValueError("Use hold_key or hold_keys, not both.")
    values = list(hold_keys or ([] if hold_key is None else [hold_key]))
    if len(values) > 8:
        raise ValueError("hold_keys may contain at most 8 keys.")
    from Infernux.lib import InputManager

    scancodes = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("hold_keys entries must be key names or SDL scancodes.")
        scancode = int(value) if isinstance(value, int) else int(InputManager.name_to_scancode(str(value)))
        if scancode <= 0:
            raise ValueError(f"Unknown hold key: {value!r}.")
        scancodes.append(scancode)
    if len(set(scancodes)) != len(scancodes):
        raise ValueError("hold_keys must not contain duplicate keys.")
    return scancodes


def _normalize_player_hold_mouse_buttons(value: list[int] | None) -> list[int]:
    buttons = list(value or [])
    if len(buttons) > 5:
        raise ValueError("hold_mouse_buttons may contain at most 5 buttons.")
    normalized: list[int] = []
    for button in buttons:
        if isinstance(button, bool) or int(button) not in range(5):
            raise ValueError("hold_mouse_buttons must use Unity button indices 0 through 4.")
        normalized.append(int(button))
    if len(set(normalized)) != len(normalized):
        raise ValueError("hold_mouse_buttons must not contain duplicate buttons.")
    return normalized


def _normalize_player_stop_assertions(assertions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    values = list(assertions or [])
    if len(values) > 16:
        raise ValueError("stop_assertions may contain at most 16 items.")
    normalized = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("stop_assertions entries must be objects.")
        normalized.append(dict(item))
    return normalized


def _normalize_player_discovery_component_types(component_types: list[str] | None) -> list[str]:
    values = [str(value or "").strip() for value in component_types or [] if str(value or "").strip()]
    if len(values) > 16:
        raise ValueError("discovery_component_types cannot contain more than 16 entries.")
    if any(value.startswith("_") for value in values):
        raise ValueError("discovery_component_types must contain public component type names.")
    return list(dict.fromkeys(values))


def _normalize_player_discovered_object_count(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("max_discovered_objects must be an integer.")
    count = int(value)
    if count < 1 or count > 64:
        raise ValueError("max_discovered_objects must be between 1 and 64.")
    return count


def _tail_text_lines(path: str, limit: int) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except OSError:
        return []
    return [line.rstrip("\r\n") for line in lines[-max(1, int(limit)):]]


def _secret_fingerprint(value: str) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _mcp_health_is_alive(endpoint: str) -> bool:
    try:
        request = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(request, timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("name") == "Infernux Editor"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _write_json(path: str, value: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise


def _append_json_line(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def _run_async(factory) -> Any:
    """Run a small async MCP query from either synchronous or async callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: list[Any] = []
    error: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            result.append(asyncio.run(factory()))
        except BaseException as exc:  # Propagate the original MCP failure to the caller.
            error.append(exc)

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]
