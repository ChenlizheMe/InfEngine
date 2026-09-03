from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from infernux_mcp import supervisor as supervisor_module
from infernux_mcp.supervisor import SupervisorSession


class _RunningProcess:
    pid = 12345

    @staticmethod
    def poll():
        return None


def test_supervisor_launches_visible_editor_without_agent_window_policy_flags(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "VisiblePilot"), session_id="visible-session")
    launched: dict[str, object] = {}

    def _popen(argv, **kwargs):
        launched["argv"] = argv
        launched.update(kwargs)
        return _RunningProcess()

    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _popen)

    try:
        status = supervisor.launch_editor()
    finally:
        supervisor._close_editor_log()

    assert status["editor_running"] is True
    assert "INFERNUX_MCP_BACKGROUND" not in launched["env"]


def test_supervisor_detects_its_own_windows_process_when_available():
    if os.name != "nt":
        pytest.skip("Windows-specific process probing")
    assert supervisor_module._pid_is_running(os.getpid()) is True


def test_supervisor_prepares_desktop_style_project_and_persists_policy(tmp_path):
    project = tmp_path / "Desktop" / "CoreSystemsPilot"
    supervisor = SupervisorSession(
        str(project),
        mode="global_validation",
        build_profile="debug_feedback",
        recording_enabled=True,
    )

    status = supervisor.prepare_project()

    assert (project / "Assets").is_dir()
    assert (project / "ProjectSettings").is_dir()
    assert status["recording_enabled"] is True
    assert status["mcp_endpoint"] == "http://127.0.0.1:9713/mcp"
    assert status["mcp_health_endpoint"] == "http://127.0.0.1:9713/health"
    assert status["editor_log_path"].endswith("editor.stdout.log")
    handoff = status["agent_handoff"]
    assert handoff["working_directory"] == str(project.resolve())
    assert handoff["endpoint"] == "http://127.0.0.1:9713/mcp"
    assert handoff["probe_argv"][-4:] == ["call", "host_session_status", "--args", "{}"]
    assert handoff["checkpoint_list_argv"][-4:-2] == ["call", "operation_query_execute"]
    assert json.loads(handoff["checkpoint_list_argv"][-1]) == {
        "operation": "infernux.mcp.checkpoint.list",
        "arguments": {},
    }
    assert "infernux.mcp.checkpoint.list" in handoff["instructions"][-1]
    assert "lease" not in json.dumps(handoff).lower()
    persisted_handoff = json.loads((project / ".infernux" / "mcp_sessions" / supervisor.session_id / "agent-handoff.json").read_text(encoding="utf-8"))
    assert persisted_handoff == handoff
    with open(project / "ProjectSettings" / "mcp_capabilities.json", "r", encoding="utf-8") as f:
        config = json.load(f)
    assert config["profile"] == "global_validation"
    assert config["session"]["build_profile"] == "debug_feedback"


def test_supervisor_checkpoint_restores_project_ledger_but_preserves_derived_state(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "CheckpointPilot"
    assets = project / "Assets"
    settings = project / "ProjectSettings"
    library = project / "Library"
    assets.mkdir(parents=True)
    settings.mkdir()
    library.mkdir()
    scene = assets / "Race.scene"
    build_settings = settings / "BuildSettings.json"
    editor_settings = settings / "EditorSettings.json"
    cache = library / "cache.bin"
    scene.write_text("clean scene\n", encoding="utf-8")
    build_settings.write_text('{"scenes": ["Race.scene"]}\n', encoding="utf-8")
    editor_settings.write_text('{"lastOpenedScene": "Race.scene"}\n', encoding="utf-8")
    cache.write_bytes(b"derived-before")
    supervisor = SupervisorSession(str(project), session_id="checkpoint-session")
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    created = supervisor.create_checkpoint("clean-race-001", restart_editor=False)

    assert created["checkpoint"]["file_count"] >= 2
    assert created["managed_checkpoints_required"] is True
    assert supervisor.checkpoint_status("clean-race-001")["current_match"] is True

    scene.write_text("mutated scene\n", encoding="utf-8")
    (assets / "Temporary.prefab").write_text("temporary\n", encoding="utf-8")
    build_settings.unlink()
    editor_settings.write_text('{"lastOpenedScene": "Results.scene"}\n', encoding="utf-8")
    cache.write_bytes(b"derived-after")
    changed = supervisor.checkpoint_status("clean-race-001")

    assert changed["current_match"] is False
    assert changed["delta"]["added"] == ["Assets/Temporary.prefab"]
    assert changed["delta"]["modified"] == ["Assets/Race.scene"]
    assert changed["delta"]["deleted"] == ["ProjectSettings/BuildSettings.json"]

    restored = supervisor.restore_checkpoint("clean-race-001", restart_editor=False)

    assert restored["checkpoint_restore"]["state"] == "completed"
    assert restored["checkpoint_status"]["current_match"] is True
    assert scene.read_text(encoding="utf-8") == "clean scene\n"
    assert build_settings.is_file()
    assert editor_settings.read_text(encoding="utf-8") == '{"lastOpenedScene": "Results.scene"}\n'
    assert not (assets / "Temporary.prefab").exists()
    assert cache.read_bytes() == b"derived-after"


def test_checkpoint_restore_rolls_back_first_root_when_second_root_replace_fails(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "CheckpointRollbackPilot"
    assets = project / "Assets"
    settings = project / "ProjectSettings"
    assets.mkdir(parents=True)
    settings.mkdir()
    scene = assets / "Race.scene"
    scene.write_text("checkpoint\n", encoding="utf-8")
    (settings / "BuildSettings.json").write_text("checkpoint settings\n", encoding="utf-8")
    supervisor = SupervisorSession(str(project), session_id="checkpoint-rollback")
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)
    supervisor.create_checkpoint("rollback-001", restart_editor=False)
    scene.write_text("must survive failed restore\n", encoding="utf-8")

    original_replace = supervisor_module.checkpoint_store._replace_root

    def fail_second_staged_root(source, destination):
        normalized = os.path.normpath(str(source))
        if os.path.basename(normalized) == "ProjectSettings" and os.path.basename(os.path.dirname(normalized)) == "staged":
            raise OSError("injected ProjectSettings replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(supervisor_module.checkpoint_store, "_replace_root", fail_second_staged_root)

    with pytest.raises(OSError, match="injected"):
        supervisor.restore_checkpoint("rollback-001", restart_editor=False)

    assert scene.read_text(encoding="utf-8") == "must survive failed restore\n"
    assert (settings / "BuildSettings.json").read_text(encoding="utf-8") == "checkpoint settings\n"


def test_release_supervisor_forces_recording_off(tmp_path):
    supervisor = SupervisorSession(
        str(tmp_path / "ReleasePilot"),
        build_profile="release_exploration",
        recording_enabled=True,
    )

    status = supervisor.prepare_project()

    assert status["recording_enabled"] is False


def test_supervisor_rejects_non_loopback_mcp_host(tmp_path):
    try:
        SupervisorSession(str(tmp_path / "UnsafeHost"), mcp_host="0.0.0.0")
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("Supervisor accepted a network-exposed MCP host.")


def test_supervisor_handoff_persists_mode_transition_without_running_editor(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "HandoffPilot"
    supervisor = SupervisorSession(str(project), mode="developer_assist")
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    result = supervisor.switch_mode(
        "global_validation",
        checkpoint="scripts-reviewed",
        reason="Begin real editor validation.",
        restart_editor=False,
    )

    assert result["mode"] == "global_validation"
    assert result["handoff"]["state"] == "completed"
    assert result["handoff"]["preflight"] == {"required": False, "editor_running": False}
    with open(project / "ProjectSettings" / "mcp_capabilities.json", "r", encoding="utf-8") as f:
        config = json.load(f)
    assert config["profile"] == "global_validation"
    with open(supervisor.handoff_history_path, "r", encoding="utf-8") as f:
        history = [json.loads(line) for line in f if line.strip()]
    assert [entry["state"] for entry in history] == ["started", "completed"]


def test_supervisor_switch_mode_is_explicit_and_records_a_secret_free_audit(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "ExplicitSwitchPilot"
    supervisor = SupervisorSession(str(project), mode="developer_assist")
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    result = supervisor.switch_mode(
        "global_validation",
        reason="Run the human-equivalent validation pass.",
        restart_editor=False,
    )

    assert result["mode"] == "global_validation"
    assert result["handoff"]["checkpoint"] == "session-start"
    assert result["handoff"]["phase"] == "verified"
    assert result["last_handoff"]["handoff_id"] == result["handoff"]["handoff_id"]
    assert "lease" not in json.dumps(result["handoff"]).lower()
    assert supervisor.handoff_history()[-1]["state"] == "completed"


def test_supervisor_switch_mode_is_idempotent_when_policy_already_matches(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "IdempotentSwitchPilot"
    supervisor = SupervisorSession(str(project), mode="global_validation")
    supervisor.prepare_project()
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    result = supervisor.switch_mode("global_validation", reason="Confirm validation mode.")

    assert result["handoff"]["phase"] == "noop"
    assert result["handoff"]["result"]["no_change"] is True
    assert result["launch"] is None
    assert result["mode"] == "global_validation"


def test_supervisor_shutdown_quiescence_rejects_a_live_endpoint(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "LiveEndpointPilot"))
    ticks = iter((0.0, 0.0, 0.2))
    monkeypatch.setattr(supervisor_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(supervisor_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda _pid: False)
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: True)

    assert supervisor._wait_for_clean_editor_shutdown(7331, timeout_seconds=0.1) is False


def test_supervisor_handoff_requires_a_current_managed_checkpoint(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "ManagedHandoffPilot"
    supervisor = SupervisorSession(str(project), mode="developer_assist")
    supervisor.managed_checkpoints_required = True
    supervisor.prepare_project()
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    with pytest.raises(RuntimeError, match="managed checkpoint"):
        supervisor.switch_mode(
            "global_validation",
            checkpoint="missing-baseline",
            restart_editor=False,
        )

    with open(supervisor.handoff_history_path, "r", encoding="utf-8") as f:
        history = [json.loads(line) for line in f if line.strip()]
    assert history[-1]["state"] == "failed"


def test_supervisor_handoff_rejects_dirty_running_editor(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "DirtyPilot"))
    supervisor._process = _RunningProcess()
    monkeypatch.setattr(supervisor, "_verify_attached_editor", lambda **_: {})
    monkeypatch.setattr(supervisor, "_read_host_session_status", lambda **_: {"attempt_active": False})
    monkeypatch.setattr(
        supervisor,
        "_read_project_info",
        lambda **_: {"active_scene": {"dirty": True}, "play_state": "edit"},
    )

    with pytest.raises(RuntimeError, match="unsaved changes"):
        supervisor.switch_mode(
            "developer_assist",
            checkpoint="must-not-stop-dirty-editor",
            restart_editor=False,
        )

    assert supervisor._process.poll() is None
    with open(supervisor.handoff_history_path, "r", encoding="utf-8") as f:
        history = [json.loads(line) for line in f if line.strip()]
    assert history[-1]["state"] == "failed"


def test_supervisor_resume_reattaches_only_after_identity_verification(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "ResumablePilot"
    original = SupervisorSession(str(project), session_id="resume-session")
    original.prepare_project()
    state_path = project / ".infernux" / "mcp_sessions" / "resume-session" / "supervisor-session.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({
        "editor_pid": 4242,
        "editor_running": True,
        "mcp_ready": True,
        "editor_instance_id": "editor-resume-4242",
        "supervisor_lease": "resume-lease",
        "project_lock_token": "resume-lock-token",
    })
    state_path.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: int(pid) == 4242)
    verified: list[tuple[str, float]] = []

    def _verify(self, *, timeout_seconds):
        verified.append((self.session_id, timeout_seconds))
        self._mcp_ready = True

    monkeypatch.setattr(SupervisorSession, "_verify_attached_editor", _verify)

    resumed = SupervisorSession.resume(str(project), "resume-session", timeout_seconds=7.5)

    assert resumed.status()["editor_pid"] == 4242
    assert resumed.status()["editor_running"] is True
    assert resumed.status()["editor_process_owner"] == "reattached"
    assert verified == [("resume-session", 7.5)]


def test_supervisor_resume_ignores_stale_persisted_pid(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "StalePilot"
    original = SupervisorSession(str(project), session_id="stale-session")
    original.prepare_project()
    state_path = project / ".infernux" / "mcp_sessions" / "stale-session" / "supervisor-session.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"editor_pid": 9999, "editor_running": True, "mcp_ready": True})
    state_path.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda _pid: False)
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    resumed = SupervisorSession.resume(str(project), "stale-session")

    assert resumed.status()["editor_running"] is False
    assert resumed.status()["editor_pid"] == 0
    assert resumed.status()["editor_process_owner"] == "none"
    assert resumed.status()["mcp_ready"] is False


def test_supervisor_attaches_current_locked_headless_host_without_loopback_probe(tmp_path):
    project = tmp_path / "Desktop" / "CurrentHeadlessHost"
    original = SupervisorSession(
        str(project),
        mode="global_validation",
        session_id="current-headless-session",
        mcp_port=9841,
    )
    original.prepare_project()
    lock_path = project / "ProjectSettings" / ".infernux-engine-lock.json"
    lock_path.write_text(
        json.dumps({
            "pid": os.getpid(),
            "token": "headless-lock-token",
            "mode": "headless",
            "state": "running",
            "project_path": str(project.resolve()),
        }),
        encoding="utf-8",
    )

    attached = SupervisorSession.attach_current_host(
        str(project),
        "current-headless-session",
        mode="global_validation",
        build_profile="debug_feedback",
        editor_instance_id="headless-editor-instance",
        mcp_port=9841,
    )

    status = attached.status()
    assert status["editor_pid"] == os.getpid()
    assert status["editor_running"] is True
    assert status["mcp_ready"] is True
    state = json.loads(Path(attached.state_path).read_text(encoding="utf-8"))
    assert state["editor_instance_id"] == "headless-editor-instance"
    assert state["project_lock_token"] == "headless-lock-token"


def test_supervisor_resume_observes_matching_manual_editor_after_stale_pid(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "ManualValidationPilot"
    original = SupervisorSession(str(project), session_id="manual-validation-session")
    original.prepare_project()
    state_path = project / ".infernux" / "mcp_sessions" / "manual-validation-session" / "supervisor-session.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"editor_pid": 9999, "editor_running": True, "mcp_ready": True})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    lock_path = project / "ProjectSettings" / ".infernux-engine-lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 4242, "project_path": str(project)}), encoding="utf-8")

    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: True)
    monkeypatch.setattr(
        SupervisorSession,
        "_read_host_session_status",
        lambda self, **_: {
            "project_root": str(project),
            "session_id": "manual-validation-session",
            "mode": self.mode,
            "build_profile": self.build_profile,
        },
    )

    resumed = SupervisorSession.resume(str(project), "manual-validation-session", verify_mcp=False)

    assert resumed.status()["editor_running"] is True
    assert resumed.status()["editor_pid"] == 4242
    assert resumed.status()["mcp_ready"] is True


def test_reattached_supervisor_handoff_stops_clean_editor_before_reconfiguring(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "AttachedHandoffPilot"
    supervisor = SupervisorSession(str(project), mode="global_validation", session_id="attached-session")
    supervisor.prepare_project()
    supervisor._attached_editor_pid = 5151
    supervisor._editor_instance_id = "attached-editor"
    supervisor._supervisor_lease = "attached-lease"
    supervisor._project_lock_token = "attached-lock"
    alive = {"value": True}
    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: int(pid) == 5151 and alive["value"])
    monkeypatch.setattr(supervisor, "_verify_attached_editor", lambda **_: {})
    monkeypatch.setattr(supervisor, "_read_host_session_status", lambda **_: {"attempt_active": False})
    monkeypatch.setattr(
        supervisor,
        "_read_project_info",
        lambda **_: {"active_scene": {"dirty": False}, "play_state": "edit"},
    )
    stop_calls: list[float] = []

    def _normal_stop(*, timeout_seconds):
        stop_calls.append(timeout_seconds)
        alive["value"] = False
        supervisor._mark_editor_stopped()
        return {"stopped": True, "editor_running": False}

    monkeypatch.setattr(supervisor, "stop_editor", _normal_stop)

    result = supervisor.switch_mode(
        "developer_assist",
        checkpoint="clean-before-script-pass",
        restart_editor=False,
    )

    assert result["mode"] == "developer_assist"
    assert result["handoff"]["state"] == "completed"
    assert result["editor_running"] is False
    assert stop_calls == [30.0]
    with open(project / "ProjectSettings" / "mcp_capabilities.json", "r", encoding="utf-8") as f:
        config = json.load(f)
    assert config["profile"] == "developer_assist"


def test_supervisor_normal_stop_uses_lease_tool_without_force_termination(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "LeaseShutdown"), session_id="lease-shutdown")
    supervisor.prepare_project()
    supervisor._attached_editor_pid = 6116
    supervisor._editor_instance_id = "lease-editor"
    supervisor._supervisor_lease = "secret-lease"
    supervisor._project_lock_token = "lease-lock"
    alive = {"value": True}
    calls: list[tuple[str, str, dict[str, str]]] = []
    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: int(pid) == 6116 and alive["value"])
    monkeypatch.setattr(supervisor, "_verify_attached_editor", lambda **_: {})

    def _call(gateway_name, operation, arguments, *, timeout_seconds):
        calls.append((gateway_name, operation, arguments))
        alive["value"] = False
        return {"close_requested": True}

    monkeypatch.setattr(supervisor, "_call_mcp_operation", _call)
    monkeypatch.setattr(supervisor, "_wait_for_clean_editor_shutdown", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(supervisor_module, "_terminate_pid", lambda _pid: pytest.fail("normal handoff must not force-terminate"))

    result = supervisor.stop_editor(timeout_seconds=4.0)

    assert result["stopped"] is True
    assert "forced" not in result
    assert calls == [
        (
            "operation_command_execute",
            "infernux.mcp.supervisor.shutdown",
            {"lease_token": "secret-lease"},
        )
    ]
    assert result["editor_running"] is False


def test_supervisor_calls_schema_gateways_with_formal_operation_ids(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "SchemaGateway"))
    calls: list[tuple[str, dict[str, object]]] = []

    class _Client:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name == "host_session_status":
                return SimpleNamespace(
                    data={"ok": True, "data": {"session": {"attempt_active": False}}}
                )
            return SimpleNamespace(
                data={
                    "ok": True,
                    "data": {
                        "operation": "infernux.project.info",
                        "result": {
                            "active_scene": {"dirty": False},
                            "play_state": "edit",
                        },
                    },
                }
            )

    class _ClientContext:
        async def __aenter__(self):
            return _Client()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        supervisor,
        "create_loopback_client",
        lambda **_: _ClientContext(),
    )

    assert supervisor._read_host_session_status(timeout_seconds=1.0) == {
        "attempt_active": False
    }
    assert supervisor._read_project_info(timeout_seconds=1.0)["play_state"] == "edit"
    assert calls == [
        ("host_session_status", {}),
        (
            "operation_query_execute",
            {"operation": "infernux.project.info", "arguments": {}},
        ),
    ]


def test_supervisor_releases_its_stale_lock_after_the_editor_process_exits(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "StaleShutdownLock"), session_id="stale-shutdown-lock")
    supervisor.prepare_project()
    supervisor._project_lock_token = "owned-lock"
    os.makedirs(os.path.dirname(supervisor.project_lock_path), exist_ok=True)
    with open(supervisor.project_lock_path, "w", encoding="utf-8") as stream:
        json.dump({"pid": 7331, "token": "owned-lock", "project_path": supervisor.project_root}, stream)

    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda _pid: False)
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    assert supervisor._wait_for_clean_editor_shutdown(7331, timeout_seconds=0.1) is True
    assert not os.path.exists(supervisor.project_lock_path)


def test_supervisor_handoff_rejects_active_validation_attempt(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "ActiveAttempt"))
    supervisor._process = _RunningProcess()
    monkeypatch.setattr(supervisor, "_verify_attached_editor", lambda **_: {})
    monkeypatch.setattr(supervisor, "_read_host_session_status", lambda **_: {"attempt_active": True})

    with pytest.raises(RuntimeError, match="attempt"):
        supervisor.switch_mode(
            "developer_assist",
            checkpoint="attempt-must-stop-first",
            restart_editor=False,
        )


def test_supervisor_identity_verification_rejects_wrong_mode_or_instance(tmp_path, monkeypatch):
    supervisor = SupervisorSession(str(tmp_path / "IdentityCheck"), mode="developer_assist")
    supervisor._process = _RunningProcess()
    supervisor._editor_instance_id = "expected-editor"
    supervisor._supervisor_lease = "expected-lease"
    supervisor._project_lock_token = "expected-lock"
    monkeypatch.setattr(supervisor, "wait_for_mcp_ready", lambda **_: {"mcp_ready": True})
    monkeypatch.setattr(
        supervisor,
        "_read_host_session_status",
        lambda **_: {
            "project_root": supervisor.project_root,
            "session_id": supervisor.session_id,
            "mode": "global_validation",
            "build_profile": supervisor.build_profile,
            "editor_instance_id": "different-editor",
            "supervisor_lease_configured": True,
            "supervisor_lease_fingerprint": supervisor_module._secret_fingerprint("expected-lease"),
        },
    )

    with pytest.raises(RuntimeError, match="mode"):
        supervisor._verify_attached_editor(timeout_seconds=1.0)


def test_supervisor_public_status_excludes_private_lease_but_persists_recovery_state(tmp_path):
    supervisor = SupervisorSession(str(tmp_path / "PrivateLease"), session_id="private-lease")
    supervisor._new_editor_identity()
    supervisor.prepare_project()

    public_status = supervisor.status()
    persisted = json.loads((tmp_path / "PrivateLease" / ".infernux" / "mcp_sessions" / "private-lease" / "supervisor-session.json").read_text(encoding="utf-8"))

    assert supervisor._supervisor_lease not in json.dumps(public_status)
    assert persisted["supervisor_lease"] == supervisor._supervisor_lease


def _write_debug_player_output(tmp_path, project_root, *, debug_build=True, scenes=None):
    output = tmp_path / "PlayerBuild"
    data = output / "Pilot_Data"
    data.mkdir(parents=True)
    executable = output / "Pilot.exe"
    executable.write_bytes(b"direct-native-player")
    control = "token_authenticated" if debug_build else "disabled"
    (data / "BuildManifest.json").write_text(json.dumps({
        "game_name": "Pilot",
        "debug_build": debug_build,
        "scenes": scenes or [],
        "build_output": {
            "tool": "Infernux",
            "project_identity": supervisor_module.path_fingerprint(str(project_root)),
        },
        "runtime_contract": {"runtime_policy": {"player_control": control}},
    }), encoding="utf-8")
    (data / "Player.inxmanifest").write_text(json.dumps({
        "audit": {"passed": True},
        "product": {
            "layout": "direct_native_runtime",
            "single_entry_point": True,
            "entry_points": ["Pilot.exe"],
        },
    }), encoding="utf-8")
    return executable


def test_supervisor_launches_only_current_debug_player_output(tmp_path, monkeypatch):
    local_state = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    project = tmp_path / "Desktop" / "PlayerPilot"
    supervisor = SupervisorSession(str(project), session_id="player-launch")
    supervisor.prepare_project()
    executable = _write_debug_player_output(tmp_path, project)
    captured = {}

    class _PlayerProcess:
        pid = 8448

        @staticmethod
        def poll():
            return None

    def _popen(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        with open(kwargs["env"]["_INFERNUX_READY_FILE"], "w", encoding="utf-8") as stream:
            stream.write("ENGINE_LOADED\n")
        return _PlayerProcess()

    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _popen)

    status = supervisor.launch_player(str(executable), timeout_seconds=1.0)

    assert status["player_running"] is True
    assert status["player_ready"] is True
    assert status["player_pid"] == 8448
    expected_data = executable.parent / "Pilot_Data"
    assert captured["argv"] == [str(executable)]
    assert captured["env"]["_INFERNUX_PLAYER_CONTROL_TOKEN"] == supervisor._player_control_token
    assert captured["env"]["_INFERNUX_PLAYER_RUNTIME_ROOT"] == str(executable.parent)
    assert captured["env"]["_INFERNUX_PLAYER_DATA_ROOT"] == str(expected_data)
    assert captured["env"]["_INFERNUX_PLAYER_MODULE_ROOT"] == str(
        expected_data / "RuntimeModules"
    )
    assert "_INFERNUX_PLAYER_DEBUG_BUILD" not in captured["env"]
    assert supervisor.player_runtime_log_path == str(
        local_state / "Infernux" / "Players" / "Pilot" / "Logs" / "player.log"
    )
    supervisor._close_player_log()


def test_supervisor_launches_current_single_entry_debug_player_output(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "SingleEntryPilot"
    supervisor = SupervisorSession(str(project), session_id="single-entry-player-launch")
    supervisor.prepare_project()
    executable = _write_debug_player_output(tmp_path, project)
    captured = {}

    class _PlayerProcess:
        pid = 8449

        @staticmethod
        def poll():
            return None

    def _popen(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        with open(kwargs["env"]["_INFERNUX_READY_FILE"], "w", encoding="utf-8") as stream:
            stream.write("ENGINE_LOADED\n")
        return _PlayerProcess()

    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _popen)

    status = supervisor.launch_player(str(executable), timeout_seconds=1.0)

    expected_data = executable.parent / "Pilot_Data"
    assert status["player_running"] is True
    assert status["player_ready"] is True
    assert captured["argv"] == [str(executable)]
    assert captured["env"]["_INFERNUX_PLAYER_RUNTIME_ROOT"] == str(executable.parent)
    assert captured["env"]["_INFERNUX_PLAYER_DATA_ROOT"] == str(expected_data)
    assert captured["env"]["_INFERNUX_PLAYER_MODULE_ROOT"] == str(expected_data / "RuntimeModules")
    supervisor._close_player_log()


def test_supervisor_reports_playerhost_failure_without_waiting_for_timeout(
    tmp_path, monkeypatch
):
    project = tmp_path / "Desktop" / "FailedPlayerHost"
    supervisor = SupervisorSession(str(project), session_id="failed-player-host")
    supervisor.prepare_project()
    executable = _write_debug_player_output(tmp_path, project)

    class _PlayerProcess:
        pid = 8452

        @staticmethod
        def poll():
            return None

    def _popen(_argv, **kwargs):
        with open(kwargs["env"]["_INFERNUX_READY_FILE"], "w", encoding="utf-8") as stream:
            stream.write("ERROR:Bootstrap.inxrt is damaged\n")
        return _PlayerProcess()

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _popen)

    status = supervisor.launch_player(str(executable), timeout_seconds=30.0)

    assert status["player_ready"] is False
    assert status["ready_error"] == "Bootstrap.inxrt is damaged"
    supervisor._close_player_log()


def test_supervisor_player_logs_only_report_current_launch(tmp_path, monkeypatch):
    local_state = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    project = tmp_path / "Desktop" / "CurrentLogsPilot"
    supervisor = SupervisorSession(str(project), session_id="current-player-logs")
    supervisor.prepare_project()
    executable = _write_debug_player_output(tmp_path, project)
    logs_root = local_state / "Infernux" / "Players" / "Pilot" / "Logs"
    logs_root.mkdir(parents=True)
    runtime_log = logs_root / "player.log"
    debug_log = logs_root / "Pilot_debug.log"
    crash_log = logs_root / "crash.log"
    runtime_log.write_text("stale runtime\n", encoding="utf-8")
    debug_log.write_text("stale debug output\n", encoding="utf-8")
    crash_log.write_text("stale crash traceback\n", encoding="utf-8")
    Path(supervisor.player_stdout_path).parent.mkdir(parents=True, exist_ok=True)
    Path(supervisor.player_stdout_path).write_text("stale stdout\n", encoding="utf-8")

    class _PlayerProcess:
        pid = 8451

        @staticmethod
        def poll():
            return None

    def _popen(_argv, **kwargs):
        kwargs["stdout"].write("current native stdout\n")
        kwargs["stdout"].flush()
        with open(kwargs["env"]["_INFERNUX_READY_FILE"], "w", encoding="utf-8") as stream:
            stream.write("ENGINE_LOADED\n")
        return _PlayerProcess()

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _popen)
    supervisor.launch_player(str(executable), timeout_seconds=1.0)
    with runtime_log.open("a", encoding="utf-8") as stream:
        stream.write("current runtime\n")
    debug_log.write_text("current debug output\n", encoding="utf-8")

    logs = supervisor.player_read_logs()

    assert logs["runtime_lines"] == ["current runtime"]
    assert logs["debug_lines"] == ["current debug output"]
    assert logs["crash_lines"] == []
    assert logs["stdout_lines"] == ["current native stdout"]
    persisted = json.loads(Path(supervisor.state_path).read_text(encoding="utf-8"))
    assert set(persisted["player_log_baselines"]) == {"runtime", "debug", "crash"}

    crash_log.write_text("current crash traceback\n", encoding="utf-8")
    assert supervisor.player_read_logs()["crash_lines"] == ["current crash traceback"]
    supervisor._close_player_log()


def test_supervisor_player_scene_override_is_limited_to_manifest_scene(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "ScenePilot"
    (project / "Assets").mkdir(parents=True)
    race_scene = project / "Assets" / "RaceTrack.scene"
    race_scene.write_text("{}", encoding="utf-8")
    supervisor = SupervisorSession(str(project), session_id="player-start-scene")
    supervisor.prepare_project()
    executable = _write_debug_player_output(tmp_path, project, scenes=["Assets/RaceTrack.scene"])
    captured = {}

    class _PlayerProcess:
        pid = 8450

        @staticmethod
        def poll():
            return None

    def _popen(_argv, **kwargs):
        captured.update(kwargs)
        with open(kwargs["env"]["_INFERNUX_READY_FILE"], "w", encoding="utf-8") as stream:
            stream.write("ENGINE_LOADED\n")
        return _PlayerProcess()

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _popen)
    status = supervisor.launch_player(
        str(executable),
        start_scene="Assets/RaceTrack.scene",
        timeout_seconds=1.0,
    )

    assert status["player_start_scene"] == "Assets/RaceTrack.scene"
    assert captured["env"]["_INFERNUX_PLAYER_START_SCENE"] == "Assets/RaceTrack.scene"
    with pytest.raises(ValueError, match="BuildManifest"):
        supervisor_module._resolve_player_start_scene(
            "Assets/Other.scene", str(project), {"scenes": ["Assets/RaceTrack.scene"]}
        )
    supervisor._close_player_log()


def test_supervisor_rejects_release_player_control(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "ReleasePilot"
    supervisor = SupervisorSession(str(project), session_id="release-player")
    supervisor.prepare_project()
    executable = _write_debug_player_output(tmp_path, project, debug_build=False)
    monkeypatch.setattr(supervisor_module, "_mcp_health_is_alive", lambda _endpoint: False)

    with pytest.raises(RuntimeError, match="Debug Player"):
        supervisor.launch_player(str(executable), wait_for_ready=False)


def test_supervisor_stops_player_through_authenticated_control_without_force(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "StopPlayer"
    supervisor = SupervisorSession(str(project), session_id="stop-player")
    supervisor.prepare_project()
    supervisor._attached_player_pid = 9559
    supervisor._player_control_token = "private-player-control-token"
    launcher = _write_debug_player_output(tmp_path, project)
    supervisor._player_executable = str(launcher)
    supervisor._player_ready = True
    alive = {"value": True}
    original_write_json = supervisor_module._write_json

    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: int(pid) == 9559 and alive["value"])
    monkeypatch.setattr(supervisor_module, "_terminate_pid", lambda _pid: pytest.fail("normal Player stop must not terminate"))

    def _write_and_respond(path, value):
        original_write_json(path, value)
        if path != supervisor.player_control_path:
            return
        assert value["token"] == "private-player-control-token"
        assert value["action"] == "shutdown"
        original_write_json(supervisor.player_response_path, {
            "command_id": value["command_id"],
            "ok": True,
            "data": {"close_requested": True},
            "error": "",
        })
        alive["value"] = False

    monkeypatch.setattr(supervisor_module, "_write_json", _write_and_respond)

    result = supervisor.stop_player(timeout_seconds=1.0)

    assert result["stopped"] is True
    assert result["player_running"] is False
    assert "forced" not in result


def test_supervisor_reaps_the_player_process_it_owns(tmp_path, monkeypatch):
    project = tmp_path / "Desktop" / "OwnedPlayer"
    supervisor = SupervisorSession(str(project), session_id="owned-player")
    supervisor.prepare_project()
    waited = []

    class OwnedPlayerProcess:
        pid = 9560

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            waited.append(timeout)
            return 0

    supervisor._player_process = OwnedPlayerProcess()
    supervisor._player_control_token = "private-player-control-token"
    supervisor._player_ready = True
    monkeypatch.setattr(
        supervisor,
        "_call_player_control",
        lambda *_args, **_kwargs: {"close_requested": True},
    )
    monkeypatch.setattr(
        supervisor_module,
        "_wait_for_pid_exit",
        lambda *_args, **_kwargs: pytest.fail(
            "an owned Player must be reaped through its Popen handle"
        ),
    )

    result = supervisor.stop_player(timeout_seconds=1.5)

    assert waited == [1.5]
    assert result["stopped"] is True
    assert result["player_running"] is False
