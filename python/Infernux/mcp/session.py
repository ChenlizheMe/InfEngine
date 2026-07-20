"""Constrained MCP session policy for remote developer and validation work."""

from __future__ import annotations

import ast
import copy
import hmac
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any

from Infernux.engine.path_utils import is_path_within, path_key, portable_path, relative_path, resolved_path
from Infernux.mcp import checkpoints as checkpoint_store


VALID_MODES = frozenset({"developer_assist", "global_validation"})
VALID_BUILD_PROFILES = frozenset({"debug_feedback", "release_exploration"})
VALID_BLOCKER_CATEGORIES = frozenset({
    "project_bug",
    "editor_ui_bug",
    "engine_bug",
    "mcp_bug",
    "public_api_gap",
    "nonportable_workaround",
    "policy_violation",
    "inconclusive",
})

_FORBIDDEN_IMPORTS = frozenset({"inspect", "importlib", "pkgutil", "subprocess", "zipfile"})
_CURRENT: "McpSession | None" = None


class McpPolicyError(RuntimeError):
    """Raised when a tool call crosses its active MCP session boundary."""


@dataclass
class McpSession:
    session_id: str
    project_root: str
    mode: str
    build_profile: str
    recording_enabled: bool
    artifact_root: str
    editor_instance_id: str = ""
    supervisor_lease: str = field(default="", repr=False)
    build_identity: dict[str, Any] = field(default_factory=dict)
    managed_checkpoints_required: bool = False
    allowed_project_roots: list[str] = field(default_factory=list)
    whl_readonly_source: list[str] = field(default_factory=list)
    workaround_allowlist: list[str] = field(default_factory=list)
    attempt_id: str = ""
    attempt_active: bool = False
    checkpoint: str = ""
    task: str = ""
    attempt_manifest_path: str = ""
    persistence_proof_path: str = ""
    attempt_baseline_ledger: dict[str, Any] = field(default_factory=dict, repr=False)
    last_trace_id: str = ""
    last_trace_path: str = ""
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("supervisor_lease", None)
        value.pop("attempt_baseline_ledger", None)
        value["recording_available"] = self.build_profile == "debug_feedback"
        value["recording_enabled"] = bool(self.recording_enabled and value["recording_available"])
        value["supervisor_lease_configured"] = bool(self.supervisor_lease)
        value["supervisor_lease_fingerprint"] = _secret_fingerprint(self.supervisor_lease)
        return value


def configure(project_path: str, config: dict[str, Any] | None = None) -> McpSession:
    """Create the active in-process session from the project MCP config."""
    global _CURRENT
    config = config or {}
    root = resolved_path(project_path or ".")
    policy = config.get("session") or {}
    policy = policy if isinstance(policy, dict) else {}
    mode = str(config.get("profile", "developer_assist") or "developer_assist")
    if mode not in VALID_MODES:
        mode = "developer_assist"
    build_profile = str(policy.get("build_profile", "debug_feedback") or "debug_feedback")
    if build_profile not in VALID_BUILD_PROFILES:
        build_profile = "debug_feedback"
    session_id = str(policy.get("session_id", "") or _new_session_id())
    roots = _normalized_roots(policy.get("allowed_project_roots"), root)
    if not _is_within_any_root(root, roots):
        roots.insert(0, root)
    artifact_root = os.path.join(root, ".infernux", "mcp_sessions", session_id)
    build_identity = _capture_build_identity(policy, build_profile)
    editor_instance_id = str(os.environ.get("INFERNUX_MCP_EDITOR_INSTANCE_ID", "") or "").strip() or uuid.uuid4().hex
    supervisor_lease = str(os.environ.get("INFERNUX_MCP_SUPERVISOR_LEASE", "") or "").strip()
    _CURRENT = McpSession(
        session_id=session_id,
        project_root=root,
        mode=mode,
        build_profile=build_profile,
        recording_enabled=bool(policy.get("recording_enabled", False)) and build_profile == "debug_feedback",
        artifact_root=artifact_root,
        editor_instance_id=editor_instance_id,
        supervisor_lease=supervisor_lease,
        build_identity=build_identity,
        managed_checkpoints_required=bool(policy.get("managed_checkpoints_required", False)),
        allowed_project_roots=roots,
        whl_readonly_source=_normalized_paths(policy.get("whl_readonly_source"), root),
        workaround_allowlist=[str(item) for item in (policy.get("workaround_allowlist") or []) if str(item)],
    )
    try:
        from Infernux.mcp.project_tools.trace import set_session_project_path

        set_session_project_path(root)
    except Exception:
        pass
    return _CURRENT


def current() -> McpSession:
    if _CURRENT is None:
        raise McpPolicyError("MCP session is not configured yet.")
    return _CURRENT


def status() -> dict[str, Any]:
    return current().to_dict()


def checkpoint_status(checkpoint: str) -> dict[str, Any]:
    active = current()
    return checkpoint_store.checkpoint_status(
        active.project_root,
        active.artifact_root,
        checkpoint,
        session_id=active.session_id,
    )


def list_checkpoints() -> list[dict[str, Any]]:
    active = current()
    return checkpoint_store.list_checkpoints(active.project_root, active.artifact_root, session_id=active.session_id)


def require_mode(*allowed: str) -> McpSession:
    session = current()
    if session.mode not in {str(item) for item in allowed}:
        expected = ", ".join(sorted({str(item) for item in allowed}))
        raise McpPolicyError(f"Tool requires MCP mode {expected}; active mode is {session.mode}.")
    return session


def require_release_exploration() -> McpSession:
    session = require_mode("developer_assist")
    if session.build_profile != "release_exploration":
        raise McpPolicyError("Wheel source access is only available in release_exploration sessions.")
    return session


def require_supervisor_lease(lease_token: str) -> McpSession:
    """Require the private lease injected by a trusted local Supervisor process."""
    active = current()
    provided = str(lease_token or "")
    if not active.supervisor_lease:
        raise McpPolicyError("This Editor was not launched by a Supervisor session with shutdown authority.")
    if not provided or not hmac.compare_digest(provided, active.supervisor_lease):
        raise McpPolicyError("Supervisor shutdown lease is missing or invalid.")
    return active


def validate_script(source: str, *, filename: str = "<script>") -> dict[str, Any]:
    """Perform a small, deterministic public-API policy check before execution."""
    violations: list[dict[str, Any]] = []
    try:
        tree = ast.parse(str(source or ""), filename=filename)
    except SyntaxError as exc:
        violations.append({
            "code": "syntax_error",
            "line": int(exc.lineno or 0),
            "message": str(exc.msg or "Invalid Python syntax."),
        })
        return _lint_result(violations)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import(alias.name, node.lineno, violations)
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            _check_import(module, node.lineno, violations)
            if module.startswith("Infernux"):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        _violate(violations, "private_symbol", node.lineno, f"Private Infernux symbol '{module}.{alias.name}' is not allowed.")
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in {"inspect.getsource", "inspect.getmembers", "pkgutil.iter_modules", "importlib.import_module"}:
                _violate(violations, "reflection", node.lineno, f"'{name}' is not allowed in project scripts.")
        elif isinstance(node, ast.Attribute):
            root = _dotted_name(node.value)
            if root.startswith("Infernux") and node.attr.startswith("_"):
                _violate(violations, "private_symbol", node.lineno, f"Private Infernux attribute '{root}.{node.attr}' is not allowed.")

    return _lint_result(violations)


def read_project_script(relative_path: str) -> dict[str, Any]:
    session = require_mode("developer_assist")
    target = _script_path(session, relative_path)
    if not os.path.isfile(target):
        raise FileNotFoundError(f"Project script was not found: {relative_path}")
    with open(target, "r", encoding="utf-8") as f:
        return {"path": _relative_to_project(session, target), "content": f.read()}


def write_project_script(relative_path: str, content: str) -> dict[str, Any]:
    session = require_mode("developer_assist")
    target = _script_path(session, relative_path)
    lint = validate_script(content, filename=target)
    if not lint["passed"]:
        raise McpPolicyError("public_api_lint rejected the project script.")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(content))
    return {"path": _relative_to_project(session, target), "lint": lint}


def read_release_wheel_source(wheel_path: str, member: str) -> dict[str, Any]:
    """Read an allowlisted text member from a wheel during release exploration."""
    session = require_release_exploration()
    target = resolved_path(wheel_path)
    if path_key(target) not in {path_key(path) for path in session.whl_readonly_source}:
        raise McpPolicyError("Wheel is not listed in session.whl_readonly_source.")
    if not target.lower().endswith(".whl") or not os.path.isfile(target):
        raise FileNotFoundError("Approved wheel file was not found.")
    member = portable_path(str(member or "")).lstrip("/")
    if not member.endswith((".py", ".pyi", ".txt", ".json")):
        raise McpPolicyError("Only text source members may be read from an approved wheel.")
    with zipfile.ZipFile(target) as archive:
        try:
            raw = archive.read(member)
        except KeyError as exc:
            raise FileNotFoundError(f"Wheel member was not found: {member}") from exc
    text = raw.decode("utf-8", errors="replace")
    if len(text) > 65536:
        text = text[:65536] + "\n# ...<truncated>"
    audit = {
        "time": time.time(),
        "wheel": target,
        "member": member,
        "session_id": session.session_id,
    }
    _append_jsonl(os.path.join(session.artifact_root, "wheel-audit.jsonl"), audit)
    return {"wheel": target, "member": member, "content": text, "audit": audit}


def write_blocker(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a reproducible blocker report for the Repair Agent."""
    session = require_mode("global_validation")
    data = copy.deepcopy(payload if isinstance(payload, dict) else {})
    category = str(data.get("category", "") or "")
    if category not in VALID_BLOCKER_CATEGORIES:
        allowed = ", ".join(sorted(VALID_BLOCKER_CATEGORIES))
        raise McpPolicyError(
            f"Unsupported blocker category: {category or '<empty>'}. Allowed categories: {allowed}."
        )
    for key in ("title", "expected", "actual"):
        if not str(data.get(key, "") or "").strip():
            raise McpPolicyError(f"Blocker report requires non-empty '{key}'.")
    workflow = data.get("normal_workflow")
    if not isinstance(workflow, list) or not workflow:
        raise McpPolicyError("Blocker report requires a non-empty normal_workflow list.")
    if not session.attempt_id or not session.checkpoint:
        raise McpPolicyError("Blocker reports require a started attempt with a checkpoint.")
    if session.attempt_active:
        raise McpPolicyError("Stop and save the current attempt trace before reporting a blocker.")
    if not session.last_trace_id:
        raise McpPolicyError("Stop and save the current attempt trace before reporting a blocker.")
    if not isinstance(data.get("logic_evidence"), dict) or not data["logic_evidence"]:
        raise McpPolicyError("Blocker report requires non-empty logic_evidence.")
    if not str(data.get("persistence_proof", "") or "").strip():
        raise McpPolicyError("Blocker report requires persistence_proof.")
    if session.build_profile == "debug_feedback" and category == "nonportable_workaround":
        raise McpPolicyError("nonportable_workaround is only valid in release_exploration.")
    data.update({
        "session_id": session.session_id,
        "project_root": session.project_root,
        "mode": session.mode,
        "build_profile": session.build_profile,
        "recording_enabled": bool(session.recording_enabled),
        "attempt_id": session.attempt_id,
        "checkpoint": session.checkpoint,
        "task": session.task,
        "attempt_manifest_path": session.attempt_manifest_path,
        "reproducer_trace": session.last_trace_path,
        "trace_id": session.last_trace_id,
        "reported_at": time.time(),
    })
    report_id = str(data.get("report_id", "") or f"blocker-{int(time.time())}-{uuid.uuid4().hex[:8]}")
    report_dir = os.path.join(session.artifact_root, "reports")
    path = os.path.join(report_dir, f"{report_id}.json")
    os.makedirs(report_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return {"report_id": report_id, "path": path, "report": data}


def blocker_report_contract() -> dict[str, Any]:
    """Describe the required trace and evidence for a Repair Agent blocker report."""
    return {
        "tool": "mcp_report_blocker",
        "allowed_categories": sorted(VALID_BLOCKER_CATEGORIES),
        "required_arguments": {
            "category": "One allowed category describing the owning layer.",
            "title": "Short, concrete failure title.",
            "expected": "What a normal editor or game workflow should do.",
            "actual": "What the same workflow did instead.",
            "normal_workflow": [
                "Ordered human-equivalent UI/input steps that reproduce the failure."
            ],
            "logic_evidence": {
                "example": "Observed values, semantic targets, console output, or trace facts."
            },
            "persistence_proof": "State whether the result repeats after returning to the checkpoint.",
        },
        "optional_arguments": {
            "severity": "low, medium, high, or critical; defaults to medium.",
            "notes": "Concise additional triage context.",
        },
        "required_sequence": [
            "Call mcp_attempt_start(task, checkpoint) before changing the editor or game.",
            "Perform only the normal human-equivalent UI/input workflow; after an effectful click, wait for its rendered result before judging it.",
            "Call mcp_attempt_stop() after the observation so the trace is saved.",
            "Call mcp_report_blocker with all required arguments only when a reproducible blocker remains.",
        ],
        "post_action_observation_rule": (
            "Synthetic input delivery proves the event reached the editor, not that the next ImGui frame has rendered "
            "its result. After a click expected to change the UI or object graph, call editor_ui_wait_for_target for "
            "the expected target; after opening or fronting an Editor window, call editor_ui_wait_for_window_focus "
            "before treating a snapshot as evidence."
        ),
        "debug_feedback_policy": "Report the blocker; do not create a workaround or mutate project internals.",
    }


def start_attempt(task: str, checkpoint: str) -> dict[str, Any]:
    """Start one replayable project attempt from an explicit checkpoint."""
    active = current()
    if active.attempt_active:
        raise McpPolicyError("A project attempt is already active. Stop it before starting another attempt.")
    checkpoint = str(checkpoint or "").strip()
    if not checkpoint:
        raise McpPolicyError("Project attempts require a non-empty checkpoint identifier.")
    checkpoint_proof: dict[str, Any] = {
        "managed": False,
        "checkpoint_id": checkpoint,
        "current_match": None,
    }
    baseline_ledger: dict[str, Any] = {}
    if active.managed_checkpoints_required:
        try:
            managed = checkpoint_store.load_checkpoint(
                active.project_root,
                active.artifact_root,
                checkpoint,
                session_id=active.session_id,
                verify_payload=True,
            )
            baseline_ledger = copy.deepcopy(managed["ledger"])
            current_ledger = checkpoint_store.capture_project_ledger(
                active.project_root,
                force_include_paths=checkpoint_store.recorded_ledger_paths(baseline_ledger),
            )
            delta = checkpoint_store.diff_ledgers(baseline_ledger, current_ledger)
        except (OSError, ValueError, checkpoint_store.CheckpointError) as exc:
            raise McpPolicyError(f"Managed checkpoint validation failed: {exc}") from exc
        compact = checkpoint_store.compact_delta(delta)
        if any(compact[f"{kind}_count"] for kind in ("added", "modified", "deleted")):
            raise McpPolicyError(
                "Current project does not match the managed checkpoint. "
                "Ask the external Supervisor to restore it or create a new clean checkpoint. "
                f"Delta: {json.dumps(compact, ensure_ascii=False)}"
            )
        checkpoint_proof = {
            "managed": True,
            "checkpoint_id": checkpoint,
            "current_match": True,
            "ledger_digest": baseline_ledger["digest"],
            "file_count": baseline_ledger["file_count"],
            "total_bytes": baseline_ledger["total_bytes"],
        }
    from Infernux.mcp.project_tools.trace import start_trace

    active.attempt_id = f"attempt-{uuid.uuid4().hex[:8]}"
    active.attempt_active = True
    active.checkpoint = checkpoint
    active.task = str(task or "")
    active.attempt_manifest_path = ""
    active.persistence_proof_path = ""
    active.attempt_baseline_ledger = baseline_ledger
    active.last_trace_id = ""
    active.last_trace_path = ""
    trace_context = {
        "attempt_id": active.attempt_id,
        "checkpoint": active.checkpoint,
        "session_id": active.session_id,
        "mode": active.mode,
        "build_profile": active.build_profile,
        "recording_enabled": active.recording_enabled,
        "build_identity": active.build_identity,
    }
    if checkpoint_proof["managed"]:
        trace_context["checkpoint_proof"] = checkpoint_proof
    trace_state = start_trace(active.project_root, task=active.task, context=trace_context)
    trace = trace_state.get("trace") or {}
    active.attempt_manifest_path = _write_attempt_manifest(active, trace)
    return {
        "attempt_id": active.attempt_id,
        "attempt_active": active.attempt_active,
        "checkpoint": active.checkpoint,
        "task": active.task,
        "trace_id": trace.get("trace_id", ""),
        "attempt_manifest_path": active.attempt_manifest_path,
        "checkpoint_proof": checkpoint_proof,
    }


def stop_attempt() -> dict[str, Any]:
    """Stop and save the active project trace before handoff or triage."""
    active = current()
    if not active.attempt_id:
        raise McpPolicyError("No project attempt is active.")
    if not active.attempt_active:
        if not active.last_trace_id:
            raise McpPolicyError("The project attempt is already stopped and has no saved trace.")
        response = {
            "attempt_id": active.attempt_id,
            "checkpoint": active.checkpoint,
            "trace_id": active.last_trace_id,
            "trace_path": active.last_trace_path,
            "attempt_manifest_path": active.attempt_manifest_path,
            "elapsed_seconds": 0.0,
            "already_stopped": True,
        }
        if active.persistence_proof_path:
            response["persistence_proof_path"] = active.persistence_proof_path
        return response
    from Infernux.mcp.project_tools.trace import stop_trace

    result = stop_trace(active.project_root, save=True)
    trace = result.get("trace") or {}
    trace_id = str(trace.get("trace_id", "") or "")
    if not trace_id:
        active.attempt_active = False
        raise McpPolicyError("Project attempt did not produce a trace.")
    active.attempt_active = False
    active.last_trace_id = trace_id
    active.last_trace_path = str(result.get("saved_path", "") or "")
    active.persistence_proof_path = _write_persistence_proof(active, trace)
    active.attempt_manifest_path = _write_attempt_manifest(active, trace)
    response = {
        "attempt_id": active.attempt_id,
        "checkpoint": active.checkpoint,
        "trace_id": active.last_trace_id,
        "trace_path": active.last_trace_path,
        "attempt_manifest_path": active.attempt_manifest_path,
        "elapsed_seconds": trace.get("elapsed_seconds", 0.0),
        "already_stopped": False,
    }
    if active.persistence_proof_path:
        response["persistence_proof_path"] = active.persistence_proof_path
    return response


def _write_attempt_manifest(active: McpSession, trace: dict[str, Any]) -> str:
    trace_id = str(trace.get("trace_id", "") or "")
    trace_path = active.last_trace_path or _expected_trace_path(trace_id)
    payload = {
        "kind": "infernux.mcp.attempt_manifest",
        "generated_at": time.time(),
        "session": {
            "session_id": active.session_id,
            "project_root": active.project_root,
            "mode": active.mode,
            "build_profile": active.build_profile,
            "recording_enabled": active.recording_enabled,
        },
        "attempt": {
            "attempt_id": active.attempt_id,
            "active": active.attempt_active,
            "checkpoint": active.checkpoint,
            "task": active.task,
            "trace_id": trace_id,
            "trace_path": trace_path,
            "persistence_proof_path": active.persistence_proof_path,
        },
        "build_identity": copy.deepcopy(active.build_identity),
    }
    attempt_dir = os.path.join(active.artifact_root, "attempts")
    os.makedirs(attempt_dir, exist_ok=True)
    path = os.path.join(attempt_dir, f"{active.attempt_id}-manifest.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return _relative_project_path(active.project_root, path)


def _write_persistence_proof(active: McpSession, trace: dict[str, Any]) -> str:
    if not active.managed_checkpoints_required or not active.attempt_baseline_ledger:
        return ""
    current_ledger = checkpoint_store.capture_project_ledger(
        active.project_root,
        force_include_paths=checkpoint_store.recorded_ledger_paths(active.attempt_baseline_ledger),
    )
    delta = checkpoint_store.diff_ledgers(active.attempt_baseline_ledger, current_ledger)
    trace_id = str(trace.get("trace_id", "") or "")
    payload = {
        "kind": "infernux.mcp.attempt_persistence_proof",
        "generated_at": time.time(),
        "session_id": active.session_id,
        "attempt_id": active.attempt_id,
        "checkpoint_id": active.checkpoint,
        "checkpoint_ledger_digest": active.attempt_baseline_ledger.get("digest", ""),
        "result_ledger_digest": current_ledger["digest"],
        "trace_id": trace_id,
        "trace_path": active.last_trace_path or _expected_trace_path(trace_id),
        "delta": delta,
        "changed": any(
            int(delta.get(f"{kind}_count", 0))
            for kind in ("added", "modified", "deleted")
        ),
    }
    attempt_dir = os.path.join(active.artifact_root, "attempts")
    os.makedirs(attempt_dir, exist_ok=True)
    path = os.path.join(attempt_dir, f"{active.attempt_id}-persistence.json")
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return _relative_project_path(active.project_root, path)


def _capture_build_identity(policy: dict[str, Any], build_profile: str) -> dict[str, Any]:
    source_root = _find_source_root()
    return {
        "captured_at": time.time(),
        "source_root": source_root,
        "package_version": _read_package_version(source_root),
        "git": _git_identity(source_root),
        "cmake": _cmake_identity(source_root, policy, build_profile),
        "python_package": _python_package_identity(source_root),
        "native_artifact": _native_artifact_identity(source_root),
    }


def _find_source_root() -> str:
    module_path = Path(resolved_path(__file__))
    for candidate in (module_path.parent, *module_path.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "CMakePresets.json").is_file():
            return str(candidate)
    return ""


def _read_package_version(source_root: str) -> str:
    pyproject = Path(source_root) / "pyproject.toml" if source_root else None
    if pyproject and pyproject.is_file():
        try:
            with open(pyproject, "rb") as f:
                project = tomllib.load(f).get("project") or {}
            return str(project.get("version", "") or "")
        except (OSError, tomllib.TOMLDecodeError):
            pass
    try:
        return str(importlib_metadata.version("Infernux") or "")
    except importlib_metadata.PackageNotFoundError:
        pass
    return ""


def _python_package_identity(
    source_root: str,
    package_root: Path | None = None,
) -> dict[str, Any]:
    root = package_root or Path(resolved_path(__file__)).parents[1]
    if not root.is_dir():
        return {"available": False}

    extensions = frozenset({".py", ".pyi", ".json"})
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in extensions
        and "__pycache__" not in path.parts
    )
    digest = hashlib.sha256()
    hashed_count = 0
    hashed_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(str(size).encode("ascii"))
                digest.update(b"\0")
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
                digest.update(b"\0")
        except OSError:
            return {"available": False, "error": f"failed to hash {relative}"}
        hashed_count += 1
        hashed_bytes += size

    return {
        "available": True,
        "path": _relative_project_path(source_root, str(root)),
        "file_count": hashed_count,
        "size_bytes": hashed_bytes,
        "sha256": digest.hexdigest(),
        "extensions": sorted(extensions),
    }


def _git_identity(source_root: str) -> dict[str, Any]:
    revision = _run_git(source_root, "rev-parse", "HEAD")
    if revision is None:
        return {"available": False}
    branch = _run_git(source_root, "rev-parse", "--abbrev-ref", "HEAD") or ""
    status = _run_git(source_root, "status", "--porcelain", "--untracked-files=no")
    status_lines = status.splitlines() if status is not None else []
    return {
        "available": True,
        "revision": revision,
        "branch": branch,
        "tracked_worktree_dirty": bool(status_lines),
        "tracked_change_count": len(status_lines),
    }


def _run_git(source_root: str, *args: str) -> str | None:
    if not source_root:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", source_root, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _cmake_identity(source_root: str, policy: dict[str, Any], build_profile: str) -> dict[str, Any]:
    default_preset = "debug" if build_profile == "debug_feedback" else "release"
    configured = str(policy.get("cmake_configure_preset", "") or "").strip()
    build = str(policy.get("cmake_build_preset", "") or "").strip()
    configure_source = "session_policy" if configured else "build_profile_inference"
    build_source = "session_policy" if build else "build_profile_inference"
    cache_path = os.path.join(source_root, "out", "build", "CMakeCache.txt") if source_root else ""
    build_type = ""
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("CMAKE_BUILD_TYPE:"):
                        build_type = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    build_preset = build or default_preset
    preset_configuration, presets_path = _build_preset_configuration(source_root, build_preset)
    return {
        "configure_preset": configured or default_preset,
        "configure_preset_source": configure_source,
        "build_preset": build_preset,
        "build_preset_source": build_source,
        "build_configuration": preset_configuration,
        "build_configuration_source": "CMakePresets.json" if preset_configuration else "",
        "presets_path": presets_path,
        "cache_path": _relative_project_path(source_root, cache_path) if cache_path else "",
        "cache_configured_build_type": build_type,
    }


def _build_preset_configuration(source_root: str, build_preset: str) -> tuple[str, str]:
    presets_path = os.path.join(source_root, "CMakePresets.json") if source_root else ""
    if not presets_path or not os.path.isfile(presets_path):
        return "", ""
    try:
        with open(presets_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "", _relative_project_path(source_root, presets_path)
    for preset in data.get("buildPresets") or []:
        if isinstance(preset, dict) and str(preset.get("name", "")) == build_preset:
            return str(preset.get("configuration", "") or ""), _relative_project_path(source_root, presets_path)
    return "", _relative_project_path(source_root, presets_path)


def _native_artifact_identity(source_root: str) -> dict[str, Any]:
    package_root = Path(resolved_path(__file__)).parents[1]
    native_dir = package_root / "lib"
    candidates = sorted(
        path for path in native_dir.glob("_Infernux.*") if path.suffix.lower() in {".pyd", ".so", ".dylib"}
    )
    if not candidates:
        return {"available": False}
    artifact = candidates[0]
    try:
        size = artifact.stat().st_size
    except OSError:
        return {"available": False}
    return {
        "available": True,
        "path": _relative_project_path(source_root, str(artifact)),
        "size_bytes": size,
        "sha256": _sha256_file(artifact),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _expected_trace_path(trace_id: str) -> str:
    return f".infernux/mcp_traces/{trace_id}.json" if trace_id else ""


def _relative_project_path(root: str, path: str) -> str:
    if not path:
        return ""
    try:
        return relative_path(path, root, allow_root=True) if root else path
    except ValueError:
        return path


def _new_session_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _secret_fingerprint(value: str) -> str:
    """Expose only a stable diagnostic fingerprint for a process-local secret."""
    secret = str(value or "")
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _normalized_paths(value: Any, project_root: str) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else []
    return [resolved_path(os.path.join(project_root, str(item)) if not os.path.isabs(str(item)) else str(item)) for item in items if str(item)]


def _normalized_roots(value: Any, project_root: str) -> list[str]:
    roots = _normalized_paths(value, project_root)
    return roots or [project_root]


def _is_within_any_root(path: str, roots: list[str]) -> bool:
    return any(is_path_within(path, root) for root in roots)


def _script_path(session: McpSession, relative_path: str) -> str:
    value = portable_path(str(relative_path or "")).lstrip("/")
    if value.startswith("Assets/"):
        value = value[len("Assets/"):]
    if not value or not value.endswith(".py"):
        raise McpPolicyError("Project scripts must use a relative .py path under Assets/.")
    target = resolved_path(os.path.join(session.project_root, "Assets", value))
    assets_root = os.path.join(session.project_root, "Assets")
    if not _is_within_any_root(target, [assets_root]):
        raise McpPolicyError("Project script path escapes Assets/.")
    return target


def _relative_to_project(session: McpSession, path: str) -> str:
    return relative_path(path, session.project_root)


def _append_jsonl(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def _check_import(module: str, line: int, violations: list[dict[str, Any]]) -> None:
    module = str(module or "")
    root = module.split(".", 1)[0]
    if root in _FORBIDDEN_IMPORTS:
        _violate(violations, "forbidden_import", line, f"Import '{module}' is not allowed in project scripts.")
    if module == "Infernux.mcp" or module.startswith("Infernux.mcp."):
        _violate(violations, "internal_module", line, f"MCP implementation import '{module}' is not allowed in project scripts.")
    if module == "Infernux.lib._Infernux" or module.startswith("Infernux.engine._"):
        _violate(violations, "internal_module", line, f"Private engine import '{module}' is not allowed in project scripts.")


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _violate(violations: list[dict[str, Any]], code: str, line: int, message: str) -> None:
    item = {"code": code, "line": int(line or 0), "message": message}
    if item not in violations:
        violations.append(item)


def _lint_result(violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"passed": not violations, "violations": violations}
