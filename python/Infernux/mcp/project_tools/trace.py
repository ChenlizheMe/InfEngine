"""Minimal trace recorder for MCP tool calls."""

from __future__ import annotations

from contextvars import ContextVar, Token
import json
import os
import time
import uuid
from typing import Any


_active_trace: dict[str, Any] | None = None
_last_trace: dict[str, Any] | None = None
_session_project_path = ""
_session_log_path = ""
_public_tool_trace_depth: ContextVar[int] = ContextVar("infernux_mcp_public_tool_trace_depth", default=0)


def begin_public_tool_trace() -> Token[int]:
    """Mark a top-level MCP tool invocation so nested helper calls stay silent."""
    return _public_tool_trace_depth.set(_public_tool_trace_depth.get() + 1)


def end_public_tool_trace(token: Token[int]) -> None:
    """Restore the nested-tool tracing state for the current MCP invocation."""
    _public_tool_trace_depth.reset(token)


def public_tool_trace_active() -> bool:
    """Return whether an outer MCP tool wrapper owns the current trace entry."""
    return _public_tool_trace_depth.get() > 0


def set_session_project_path(project_path: str) -> dict[str, Any]:
    """Bind trace output to a project without creating a log file yet."""
    global _session_project_path, _session_log_path
    _session_project_path = os.path.abspath(project_path or "") if project_path else ""
    _session_log_path = _session_log_file(_session_project_path)
    return session_log_info(_session_project_path)


def start_session_log(project_path: str) -> dict[str, Any]:
    """Clear and initialize the per-editor-session MCP call log."""
    set_session_project_path(project_path)
    if not _session_log_path:
        return session_log_info(project_path)
    os.makedirs(os.path.dirname(_session_log_path), exist_ok=True)
    with open(_session_log_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({
            "event": "session_start",
            "time": time.time(),
            "project_path": _session_project_path,
        }, ensure_ascii=False) + "\n")
    return session_log_info()


def session_log_info(project_path: str | None = None) -> dict[str, Any]:
    path = _session_log_path or _session_log_file(project_path or _session_project_path)
    exists = bool(path and os.path.isfile(path))
    return {
        "enabled": _session_log_enabled(),
        "path": _rel(project_path or _session_project_path, path) if path else "",
        "absolute_path": path,
        "exists": exists,
        "size": os.path.getsize(path) if exists else 0,
    }


def clear_session_log(project_path: str | None = None) -> dict[str, Any]:
    return start_session_log(project_path or _session_project_path)


def read_session_log(project_path: str | None = None, limit: int = 200) -> dict[str, Any]:
    path = _session_log_path or _session_log_file(project_path or _session_project_path)
    if not path or not os.path.isfile(path):
        return {"entries": [], **session_log_info(project_path)}
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"event": "raw", "text": line})
    return {"entries": entries[-max(int(limit), 1):], **session_log_info(project_path)}


def start_trace(
    project_path: str,
    task: str = "",
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a trace, optionally attaching immutable attempt/session context."""
    global _active_trace
    _active_trace = {
        "trace_id": f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "task": str(task or ""),
        "started_at": time.time(),
        "steps": [],
    }
    if context:
        _active_trace["context"] = _jsonable_summary(context)
    return current_trace()


def stop_trace(project_path: str, save: bool = True) -> dict[str, Any]:
    global _active_trace, _last_trace
    if _active_trace is None:
        return {"active": False, "trace": None, "saved_path": ""}
    trace = dict(_active_trace)
    trace["ended_at"] = time.time()
    trace["elapsed_seconds"] = max(0.0, trace["ended_at"] - float(trace.get("started_at", trace["ended_at"])))
    saved_path = ""
    if save:
        saved_path = _save_trace(project_path, trace)
    _last_trace = trace
    _active_trace = None
    return {"active": False, "trace": trace, "saved_path": saved_path}


def current_trace() -> dict[str, Any]:
    return {"active": _active_trace is not None, "trace": _active_trace}


def last_trace() -> dict[str, Any]:
    return {"trace": _last_trace}


def record_tool_call(
    name: str,
    *,
    ok: bool,
    elapsed_ms: float = 0.0,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    error: str = "",
) -> None:
    if public_tool_trace_active():
        return
    if _active_trace is None:
        _record_session_tool_call(name, ok=ok, elapsed_ms=elapsed_ms, arguments=arguments, result=result, error=error)
        return
    try:
        from Infernux.mcp.capabilities import feature_enabled
        if not feature_enabled("trace_recorder"):
            return
    except Exception:
        pass
    step = {
        "index": len(_active_trace["steps"]),
        "tool": str(name),
        "ok": bool(ok),
        "elapsed_ms": round(float(elapsed_ms), 3),
    }
    if arguments:
        step["arguments"] = _jsonable_summary(arguments)
    if result is not None:
        step["result"] = _jsonable_summary(
            result,
            max_string=_trace_result_max_string(),
            limit_name="trace_result_max_string",
        )
    if error:
        step["error"] = str(error)
    _active_trace["steps"].append(step)
    _record_session_tool_call(name, ok=ok, elapsed_ms=elapsed_ms, arguments=arguments, result=result, error=error)


def record_tool_result(
    name: str,
    *,
    ok: bool,
    elapsed_ms: float = 0.0,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    error: str = "",
) -> None:
    """Record a call with compact result data in the session log."""
    if public_tool_trace_active():
        return
    _record_session_tool_call(
        name,
        ok=ok,
        elapsed_ms=elapsed_ms,
        arguments=arguments,
        result=result,
        error=error,
    )


def list_traces(project_path: str, limit: int = 50) -> list[dict[str, Any]]:
    trace_dir = _trace_dir(project_path)
    if not os.path.isdir(trace_dir):
        return []
    entries = []
    for name in sorted(os.listdir(trace_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        path = os.path.join(trace_dir, name)
        entries.append({
            "file": os.path.relpath(path, project_path).replace("\\", "/"),
            "name": name,
            "size": os.path.getsize(path),
        })
        if len(entries) >= max(int(limit), 1):
            break
    return entries


def _save_trace(project_path: str, trace: dict[str, Any]) -> str:
    trace_dir = _trace_dir(project_path)
    os.makedirs(trace_dir, exist_ok=True)
    file_path = os.path.join(trace_dir, f"{trace['trace_id']}.json")
    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(trace, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return os.path.relpath(file_path, project_path).replace("\\", "/")


def _trace_dir(project_path: str) -> str:
    return os.path.join(os.path.abspath(project_path), ".infernux", "mcp_traces")


def _session_log_file(project_path: str) -> str:
    root = str(project_path or "").strip()
    if not root:
        return ""
    return os.path.join(os.path.abspath(root), "Logs", "mcp_session.jsonl")


def _record_session_tool_call(
    name: str,
    *,
    ok: bool,
    elapsed_ms: float = 0.0,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    error: str = "",
) -> None:
    if not _session_log_enabled():
        return
    path = _session_log_path or _session_log_file(_session_project_path)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        entry = {
            "event": "tool_call",
            "time": time.time(),
            "tool": str(name),
            "ok": bool(ok),
            "elapsed_ms": round(float(elapsed_ms), 3),
        }
        if arguments:
            entry["arguments"] = _jsonable_summary(arguments)
        if result is not None:
            entry["result"] = _jsonable_summary(
                result,
                max_string=_session_result_max_string(),
                limit_name="session_log_result_max_string",
            )
        if error:
            entry["error"] = str(error)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _session_log_enabled() -> bool:
    try:
        from Infernux.mcp.capabilities import feature_enabled
        return feature_enabled("session_call_log")
    except Exception:
        return True


def _session_result_max_string() -> int:
    try:
        from Infernux.mcp.capabilities import limit
        return int(limit("session_log_result_max_string", 480) or 480)
    except Exception:
        return 480


def _trace_result_max_string() -> int:
    try:
        from Infernux.mcp.capabilities import limit
        return int(limit("trace_result_max_string", 480) or 480)
    except Exception:
        return 480


def _rel(project_path: str, path: str) -> str:
    if not project_path or not path:
        return path
    try:
        return os.path.relpath(os.path.abspath(path), os.path.abspath(project_path)).replace("\\", "/")
    except Exception:
        return path


def _jsonable_summary(
    value: Any,
    *,
    max_string: int = 240,
    limit_name: str = "trace_argument_max_string",
) -> Any:
    if limit_name:
        try:
            from Infernux.mcp.capabilities import limit
            max_string = int(limit(limit_name, max_string) or max_string)
        except Exception:
            pass
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "...<truncated>"
    if isinstance(value, dict):
        return {
            str(k): _jsonable_summary(v, max_string=max_string, limit_name=limit_name)
            for k, v in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        summarized = [
            _jsonable_summary(v, max_string=max_string, limit_name=limit_name)
            for v in items[:40]
        ]
        if len(items) > 40:
            summarized.append(f"...<{len(items) - 40} more>")
        return summarized
    return str(value)

