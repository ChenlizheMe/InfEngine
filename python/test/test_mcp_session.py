from __future__ import annotations

import json
import zipfile

import pytest

from Infernux.mcp import checkpoints as checkpoint_store
from Infernux.mcp import session
from Infernux.mcp.project_tools import trace
from Infernux.mcp.tools import _trace_public_tool_call


def _config(mode: str = "developer_assist", **session_overrides):
    policy = {
        "build_profile": "debug_feedback",
        "recording_enabled": False,
        "allowed_project_roots": [],
        "whl_readonly_source": [],
        "workaround_allowlist": [],
    }
    policy.update(session_overrides)
    return {"profile": mode, "session": policy}


def test_session_status_enforces_debug_recording_policy(tmp_path):
    configured = session.configure(str(tmp_path), _config(recording_enabled=True))

    assert configured.mode == "developer_assist"
    assert session.status()["recording_enabled"] is True

    release = session.configure(
        str(tmp_path),
        _config(build_profile="release_exploration", recording_enabled=True),
    )
    assert release.recording_enabled is False
    assert session.status()["recording_available"] is False


def test_supervisor_lease_is_verified_but_never_exposed_in_status_or_trace(tmp_path, monkeypatch):
    lease = "private-supervisor-lease"
    monkeypatch.setenv("INFERNUX_MCP_EDITOR_INSTANCE_ID", "editor-instance-for-test")
    monkeypatch.setenv("INFERNUX_MCP_SUPERVISOR_LEASE", lease)
    configured = session.configure(str(tmp_path), _config("global_validation"))

    status = session.status()
    assert status["editor_instance_id"] == "editor-instance-for-test"
    assert status["supervisor_lease_configured"] is True
    assert status["supervisor_lease_fingerprint"]
    assert lease not in json.dumps(status)
    assert session.require_supervisor_lease(lease) is configured
    with pytest.raises(session.McpPolicyError, match="invalid"):
        session.require_supervisor_lease("wrong-lease")

    session.start_attempt("lease trace redaction", "before-normal-shutdown")

    wrapped = _trace_public_tool_call(
        "mcp_supervisor_shutdown",
        lambda lease_token: {"ok": True, "data": {"closed": True}},
    )
    wrapped(lease)
    stopped = session.stop_attempt()

    with open(tmp_path / stopped["trace_path"], "r", encoding="utf-8") as f:
        trace_payload = json.load(f)
    assert lease not in json.dumps(trace_payload)
    assert trace_payload["steps"][0]["arguments"] == {"lease_token": "<redacted>"}


def test_public_api_lint_rejects_internal_and_reflection_imports():
    accepted = session.validate_script("from Infernux.components import Component\nclass Drive(Component):\n    pass\n")
    assert accepted["passed"] is True

    rejected = session.validate_script(
        "import inspect\nfrom Infernux.lib import _Infernux\ninspect.getsource(_Infernux)\n"
    )
    assert rejected["passed"] is False
    assert {item["code"] for item in rejected["violations"]} >= {"forbidden_import", "private_symbol", "reflection"}


def test_developer_assist_prepares_only_lint_clean_assets_scripts(tmp_path):
    session.configure(str(tmp_path), _config())

    result = session.prepare_project_script_write(
        "Racing/drive.py",
        "from Infernux.components import Component\n",
    )
    assert result["path"] == "Assets/Racing/drive.py"
    assert result["lint"]["passed"] is True
    assert not (tmp_path / "Assets" / "Racing" / "drive.py").exists()

    with pytest.raises(session.McpPolicyError):
        session.prepare_project_script_write("../escape.py", "pass\n")


def test_developer_assist_attempt_persists_mode_and_checkpoint(tmp_path):
    configured = session.configure(str(tmp_path), _config("developer_assist"))

    attempt = session.start_attempt("author public HUD script", "before-hud-script")
    stopped = session.stop_attempt()

    with open(tmp_path / stopped["trace_path"], "r", encoding="utf-8") as f:
        trace = json.load(f)
    assert trace["context"]["mode"] == "developer_assist"
    assert trace["context"]["session_id"] == configured.session_id
    assert attempt["checkpoint"] == "before-hud-script"


def test_managed_attempt_requires_exact_checkpoint_and_writes_persistence_delta(tmp_path):
    assets = tmp_path / "Assets"
    settings = tmp_path / "ProjectSettings"
    assets.mkdir()
    settings.mkdir()
    scene = assets / "Race.scene"
    scene.write_text("clean\n", encoding="utf-8")
    configured = session.configure(
        str(tmp_path),
        _config(
            "global_validation",
            session_id="managed-attempt-session",
            managed_checkpoints_required=True,
        ),
    )
    checkpoint_store.create_checkpoint(
        str(tmp_path),
        configured.artifact_root,
        "clean-race-001",
        session_id=configured.session_id,
    )

    status = session.checkpoint_status("clean-race-001")
    attempt = session.start_attempt("managed persistence proof", "clean-race-001")
    (assets / "Created.prefab").write_text("created\n", encoding="utf-8")
    scene.write_text("modified\n", encoding="utf-8")
    stopped = session.stop_attempt()

    assert status["current_match"] is True
    assert attempt["checkpoint_proof"]["managed"] is True
    with open(tmp_path / stopped["persistence_proof_path"], "r", encoding="utf-8") as stream:
        proof = json.load(stream)
    assert proof["changed"] is True
    assert proof["delta"]["added"] == ["Assets/Created.prefab"]
    assert proof["delta"]["modified"] == ["Assets/Race.scene"]
    assert proof["delta"]["deleted"] == []

    with pytest.raises(session.McpPolicyError, match="does not match"):
        session.start_attempt("must restore first", "clean-race-001")


def test_checkpoint_list_returns_payload_verified_choices_without_project_scan(tmp_path):
    assets = tmp_path / "Assets"
    settings = tmp_path / "ProjectSettings"
    assets.mkdir()
    settings.mkdir()
    (assets / "Race.scene").write_text("clean\n", encoding="utf-8")
    (settings / "BuildSettings.json").write_text("{}\n", encoding="utf-8")
    configured = session.configure(
        str(tmp_path),
        _config("global_validation", session_id="checkpoint-list-session", managed_checkpoints_required=True),
    )
    checkpoint_store.create_checkpoint(
        str(tmp_path),
        configured.artifact_root,
        "race-clean-001",
        session_id=configured.session_id,
        metadata={"reason": "weak-agent entry"},
    )

    listed = session.list_checkpoints()

    assert len(listed) == 1
    assert listed[0]["checkpoint_id"] == "race-clean-001"
    assert listed[0]["payload_valid"] is True
    assert listed[0]["file_count"] == 2
    assert listed[0]["metadata"] == {"reason": "weak-agent entry"}


def test_global_validation_writes_logic_backed_blocker(tmp_path):
    session.configure(str(tmp_path), _config("global_validation"))
    attempt = session.start_attempt("physics contact validation", "clean-physics-001")
    stopped = session.stop_attempt()

    result = session.write_blocker({
        "category": "engine_bug",
        "title": "Physics contact was never emitted",
        "expected": "contact event count is positive",
        "actual": "contact event count is zero",
        "normal_workflow": ["create colliders", "enter play mode", "advance fixed steps"],
        "logic_evidence": {"fixed_steps": 120, "contact_event_count": 0},
        "persistence_proof": "passed",
    })

    report_path = result["path"]
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    assert report["mode"] == "global_validation"
    assert report["build_profile"] == "debug_feedback"
    assert report["category"] == "engine_bug"
    assert report["attempt_id"] == attempt["attempt_id"]
    assert report["trace_id"] == stopped["trace_id"]
    assert report["attempt_manifest_path"] == stopped["attempt_manifest_path"]


def test_unmanaged_attempt_uses_session_start_when_checkpoint_is_omitted(tmp_path):
    session.configure(str(tmp_path), _config("developer_assist"))

    attempt = session.start_attempt("author a particle graph", "")
    stopped = session.stop_attempt()

    assert attempt["checkpoint"] == "session-start"
    assert stopped["checkpoint"] == "session-start"


def test_managed_attempt_still_requires_a_supervisor_checkpoint(tmp_path):
    session.configure(
        str(tmp_path),
        _config("global_validation", managed_checkpoints_required=True),
    )

    with pytest.raises(session.McpPolicyError, match="mcp_checkpoint_list"):
        session.start_attempt("validate particles", "")


def test_global_validation_trace_persists_attempt_and_session_context(tmp_path):
    configured = session.configure(str(tmp_path), _config("global_validation", recording_enabled=True))

    attempt = session.start_attempt("save persistence validation", "before-scene-save")
    stopped = session.stop_attempt()

    trace_path = tmp_path / stopped["trace_path"]
    with open(trace_path, "r", encoding="utf-8") as f:
        trace = json.load(f)
    assert trace["task"] == "save persistence validation"
    context = dict(trace["context"])
    assert context.pop("build_identity") == configured.build_identity
    assert context == {
        "attempt_id": attempt["attempt_id"],
        "checkpoint": "before-scene-save",
        "session_id": configured.session_id,
        "mode": "global_validation",
        "build_profile": "debug_feedback",
        "recording_enabled": True,
    }


def test_global_validation_trace_persists_compact_tool_results(tmp_path, monkeypatch):
    session.configure(str(tmp_path), _config("global_validation"))
    monkeypatch.setattr(
        "Infernux.mcp.capabilities.limit",
        lambda name, default=None: 12 if name == "trace_result_max_string" else default,
    )

    session.start_attempt("runtime assertion evidence", "before-runtime-assertion")
    trace.record_tool_call(
        "runtime_assert",
        ok=True,
        arguments={"assertions": [{"kind": "scene_name", "equals": "Results"}]},
        result={"ok": True, "data": {"passed": True, "detail": "0123456789abcdef"}},
    )
    stopped = session.stop_attempt()

    with open(tmp_path / stopped["trace_path"], "r", encoding="utf-8") as f:
        saved = json.load(f)
    step = saved["steps"][0]
    assert step["arguments"]["assertions"][0]["kind"] == "scene_name"
    assert step["result"] == {
        "ok": True,
        "data": {"passed": True, "detail": "0123456789ab...<truncated>"},
    }


def test_global_validation_attempt_manifest_persists_build_identity(tmp_path, monkeypatch):
    identity = {
        "source_root": "E:/engine",
        "package_version": "0.2.1",
        "git": {"available": True, "branch": "029/030preview", "revision": "abc123"},
        "cmake": {"configure_preset": "debug", "build_preset": "debug"},
        "native_artifact": {"available": True, "sha256": "artifact-hash"},
    }
    monkeypatch.setattr(session, "_capture_build_identity", lambda policy, build_profile: identity)
    configured = session.configure(str(tmp_path), _config("global_validation"))

    attempt = session.start_attempt("manifest validation", "identity-captured")
    manifest_path = tmp_path / attempt["attempt_manifest_path"]
    with open(manifest_path, "r", encoding="utf-8") as f:
        started_manifest = json.load(f)
    assert started_manifest["session"]["session_id"] == configured.session_id
    assert started_manifest["attempt"]["active"] is True
    assert started_manifest["attempt"]["trace_id"] == attempt["trace_id"]
    assert started_manifest["build_identity"] == identity

    stopped = session.stop_attempt()
    with open(manifest_path, "r", encoding="utf-8") as f:
        stopped_manifest = json.load(f)
    assert stopped_manifest["attempt"]["active"] is False
    assert stopped_manifest["attempt"]["trace_path"] == stopped["trace_path"]


def test_cmake_identity_uses_build_preset_configuration(tmp_path):
    presets = {
        "buildPresets": [
            {"name": "debug", "configurePreset": "debug", "configuration": "RelWithDebInfo"},
        ],
    }
    (tmp_path / "CMakePresets.json").write_text(json.dumps(presets), encoding="utf-8")
    cache_dir = tmp_path / "out" / "build"
    cache_dir.mkdir(parents=True)
    (cache_dir / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:UNINITIALIZED=Release\n", encoding="utf-8")

    identity = session._cmake_identity(str(tmp_path), {}, "debug_feedback")

    assert identity["build_preset"] == "debug"
    assert identity["build_configuration"] == "RelWithDebInfo"
    assert identity["build_configuration_source"] == "CMakePresets.json"
    assert identity["cache_configured_build_type"] == "Release"


def test_package_version_falls_back_to_installed_distribution(monkeypatch):
    monkeypatch.setattr(session.importlib_metadata, "version", lambda _name: "0.2.1-installed")

    assert session._read_package_version("") == "0.2.1-installed"


def test_python_package_identity_hashes_actual_runtime_sources(tmp_path):
    package_root = tmp_path / "Infernux"
    package_root.mkdir()
    source = package_root / "runtime.py"
    schema = package_root / "schema.json"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    schema.write_text('{"version": 1}\n', encoding="utf-8")
    cache = package_root / "__pycache__"
    cache.mkdir()
    bytecode = cache / "runtime.pyc"
    bytecode.write_bytes(b"first")

    first = session._python_package_identity(str(tmp_path), package_root)
    bytecode.write_bytes(b"second")
    cache_only_change = session._python_package_identity(str(tmp_path), package_root)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    source_change = session._python_package_identity(str(tmp_path), package_root)

    assert first["available"] is True
    assert first["path"] == "Infernux"
    assert first["file_count"] == 2
    assert first["extensions"] == [".json", ".py", ".pyi"]
    assert len(first["sha256"]) == 64
    assert cache_only_change["sha256"] == first["sha256"]
    assert source_change["sha256"] != first["sha256"]


def test_global_validation_attempt_stop_is_idempotent_and_tracks_activity(tmp_path):
    session.configure(str(tmp_path), _config("global_validation"))

    attempt = session.start_attempt("menu state validation", "before-menu-open")
    assert attempt["attempt_active"] is True
    assert session.status()["attempt_active"] is True
    with pytest.raises(session.McpPolicyError, match="already active"):
        session.start_attempt("another validation", "another-checkpoint")

    stopped = session.stop_attempt()
    repeated = session.stop_attempt()

    assert stopped["already_stopped"] is False
    assert repeated == {
        "attempt_id": attempt["attempt_id"],
        "checkpoint": "before-menu-open",
        "trace_id": stopped["trace_id"],
        "trace_path": stopped["trace_path"],
        "attempt_manifest_path": stopped["attempt_manifest_path"],
        "elapsed_seconds": 0.0,
        "already_stopped": True,
    }
    assert session.status()["attempt_active"] is False


def test_blocker_report_contract_exposes_trace_first_workflow():
    contract = session.blocker_report_contract()

    assert "editor_ui_bug" in contract["allowed_categories"]
    assert set(contract["required_arguments"]) >= {
        "category",
        "title",
        "expected",
        "actual",
        "normal_workflow",
        "logic_evidence",
        "persistence_proof",
    }
    assert contract["required_sequence"][0].startswith("Call mcp_attempt_start")
    assert "mcp_attempt_stop" in contract["required_sequence"][2]
    assert "editor_ui_wait_for_target" in contract["post_action_observation_rule"]


def test_release_wheel_source_requires_allowlist_and_audits_read(tmp_path):
    wheel = tmp_path / "api-hints.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/hints.py", "PUBLIC_HINT = 'use public API first'\n")

    session.configure(
        str(tmp_path),
        _config(
            build_profile="release_exploration",
            whl_readonly_source=[str(wheel)],
        ),
    )

    result = session.read_release_wheel_source(str(wheel), "package/hints.py")
    assert "PUBLIC_HINT" in result["content"]
    assert (tmp_path / ".infernux" / "mcp_sessions").is_dir()
