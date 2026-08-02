"""Runtime registry for project-defined MCP tools."""

from __future__ import annotations

import inspect
import json
import os
import time
import traceback
from functools import wraps
from typing import Any, Callable

from Infernux.engine.path_utils import is_path_within, relative_path, resolved_path
from Infernux.mcp.project_tools.loadability import (
    ProjectToolDefinition,
    collect_tool_definitions,
    load_tool_module,
    validate_callable_schema,
    validate_file,
)
from Infernux.mcp.project_tools.trace import record_tool_call, record_tool_result
from Infernux.mcp.threading import MainThreadCommandQueue


_REGISTRY: "ProjectToolRegistry | None" = None


def get_project_tool_registry(project_path: str | None = None) -> "ProjectToolRegistry":
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ProjectToolRegistry(project_path or "")
    elif project_path:
        _REGISTRY.project_path = resolved_path(project_path)
    return _REGISTRY


class ProjectToolRegistry:
    def __init__(self, project_path: str) -> None:
        self.project_path = resolved_path(project_path) if project_path else ""
        self.tools: dict[str, ProjectToolDefinition] = {}
        self.disabled_tools: set[str] = set()
        self.file_reports: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self._registered_names: set[str] = set()
        self._mcp = None

    def configure(self, project_path: str) -> None:
        self.project_path = resolved_path(project_path)

    def discover(self) -> dict[str, Any]:
        from Infernux.mcp.capabilities import feature_enabled

        if not feature_enabled("project_defined_tools"):
            self.tools.clear()
            self.file_reports.clear()
            self._audit("discover", True, "Project-defined tools are disabled by MCP capability config.")
            return {"tool_count": 0, "loaded_files": [], "reports": [], "disabled": True}
        self.tools.clear()
        self.file_reports.clear()
        config = self._config()
        self.disabled_tools = set(str(item) for item in config.get("disabled_tools", []))
        loaded = []
        for file_path in self._iter_tool_files(config):
            rel_path = self._rel(file_path)
            report = self._load_file(file_path)
            self.file_reports[rel_path] = report
            if report.get("ok"):
                loaded.append(rel_path)
        self._audit("discover", True, f"Loaded {len(self.tools)} project tool(s).")
        return {
            "tool_count": len(self.tools),
            "loaded_files": loaded,
            "reports": list(self.file_reports.values()),
            "disabled_tools": sorted(self.disabled_tools),
        }

    def register_with_mcp(self, mcp) -> dict[str, Any]:
        from Infernux.mcp.tools.common import register_tool_metadata

        self._mcp = mcp
        registered = []
        skipped = []
        for name, definition in sorted(self.tools.items()):
            register_tool_metadata(
                name,
                summary=definition.summary,
                side_effects=["Runs project-defined Python code from Assets/AgentTools."],
                recovery=["Use project_tools_audit and project_tools_validate to diagnose project tool failures."],
                concepts={"Project MCP Tool": "A project-owned Python function exposed to agents as an MCP tool."},
            )
            if name in self._registered_names:
                skipped.append(name)
                continue
            try:
                mcp.tool(name=name)(self._make_mcp_callable(name))
                self._registered_names.add(name)
                registered.append(name)
            except Exception as exc:
                self._audit("register", False, f"Failed to register {name}: {exc}", tool=name)
        return {"registered": registered, "already_registered": skipped}

    def reload(self) -> dict[str, Any]:
        result = self.discover()
        if self._mcp is not None:
            result["registration"] = self.register_with_mcp(self._mcp)
        return result

    def validate(self, path: str = "") -> dict[str, Any]:
        if path:
            return validate_file(self.project_path, path)
        reports = []
        for file_path in self._iter_tool_files(self._config()):
            reports.append(validate_file(self.project_path, file_path))
        return {"ok": all(report.get("ok") for report in reports), "reports": reports}

    def list_tools(self) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": definition.name,
                    "summary": definition.summary,
                    "path": definition.rel_path,
                    "generated": definition.generated,
                    "tags": definition.tags,
                    "status": "disabled" if definition.name in self.disabled_tools else definition.status,
                    "validation": definition.validation,
                }
                for definition in sorted(self.tools.values(), key=lambda item: item.name)
            ],
            "files": list(self.file_reports.values()),
            "disabled_tools": sorted(self.disabled_tools),
        }

    def explain(self, name: str) -> dict[str, Any]:
        definition = self.tools.get(name)
        if definition is None:
            raise FileNotFoundError(f"Project tool not found: {name}")
        return {
            "name": definition.name,
            "summary": definition.summary,
            "path": definition.rel_path,
            "generated": definition.generated,
            "tags": definition.tags,
            "source_trace": definition.source_trace,
            "source_traces": definition.source_traces,
            "signature": str(inspect.signature(definition.callable)),
            "doc": inspect.getdoc(definition.callable) or "",
        }

    def audit(self, limit: int = 100) -> dict[str, Any]:
        return {"events": self.audit_events[-max(int(limit), 1):]}

    def execute(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from Infernux.mcp.tools.common import explain_for, fail, ok

        started = time.monotonic()
        definition = self.tools.get(name)
        if definition is None:
            result = fail("error.not_found", f"Project tool is not loaded: {name}")
            record_tool_result(name, ok=False, elapsed_ms=_elapsed_ms(started), arguments=_arguments(definition, args, kwargs), result=result, error="not loaded")
            return result
        if name in self.disabled_tools:
            result = fail("error.disabled", f"Project tool is disabled: {name}")
            record_tool_result(name, ok=False, elapsed_ms=_elapsed_ms(started), arguments=_arguments(definition, args, kwargs), result=result, error="disabled")
            return result

        explain = explain_for(name, summary=definition.summary)
        try:
            value = MainThreadCommandQueue.instance().run_sync(
                name,
                lambda: definition.callable(*args, **kwargs),
                timeout_ms=_project_tool_timeout_ms(),
            )
            result = value if _is_envelope(value) else ok(value, explain=explain)
            self._audit("call", True, f"Called {name}", tool=name)
            record_tool_call(name, ok=True, elapsed_ms=_elapsed_ms(started), arguments=_arguments(definition, args, kwargs))
            record_tool_result(name, ok=True, elapsed_ms=_elapsed_ms(started), arguments=_arguments(definition, args, kwargs), result=result)
            return result
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._audit("call", False, message, tool=name, traceback_text="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            result = fail("error.project_tool", message, hint="Use project_tools_audit and project_tools_validate to inspect this project-defined tool.", explain=explain)
            record_tool_result(name, ok=False, elapsed_ms=_elapsed_ms(started), arguments=_arguments(definition, args, kwargs), result=result, error=message)
            return result

    def _make_mcp_callable(self, name: str) -> Callable:
        definition = self.tools[name]

        @wraps(definition.callable)
        def _call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return self.execute(name, *args, **kwargs)

        try:
            _call.__signature__ = inspect.signature(definition.callable)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            pass
        _call.__doc__ = definition.summary or inspect.getdoc(definition.callable)
        return _call

    def _load_file(self, file_path: str) -> dict[str, Any]:
        rel_path = self._rel(file_path)
        load = load_tool_module(self.project_path, file_path)
        if not load["ok"]:
            self._audit("load", False, load.get("error", ""), path=rel_path, traceback_text=load.get("traceback", ""))
            return {"path": rel_path, "ok": False, "error": load.get("error", ""), "tools": []}
        definitions = collect_tool_definitions(load["module"], self.project_path, file_path)
        tools = []
        violations = []
        for definition in definitions:
            schema_issues = validate_callable_schema(definition.callable)
            if schema_issues:
                violations.append({"tool": definition.name, "issues": schema_issues})
            if not definition.name.startswith("project."):
                violations.append({"tool": definition.name, "issues": ["Project tool names should start with 'project.' for clarity."]})
            if definition.name in self.tools:
                violations.append({"tool": definition.name, "issues": [f"Duplicate project tool name: {definition.name}"]})
                continue
            self.tools[definition.name] = definition
            tools.append(definition.name)
        ok_flag = not violations
        self._audit("load", ok_flag, f"Loaded {len(tools)} tool(s) from {rel_path}", path=rel_path)
        return {"path": rel_path, "ok": ok_flag, "tools": tools, "violations": violations}

    def _iter_tool_files(self, config: dict[str, Any]) -> list[str]:
        roots = config.get("tool_roots") or ["Assets/AgentTools", "Assets/AgentTools/generated"]
        scan_generated = bool(config.get("scan_generated_tools", True))
        files = []
        for root in roots:
            root_path = self._resolve_project_path(str(root))
            if not os.path.isdir(root_path):
                continue
            is_generated_root = self._rel(root_path).replace("\\", "/").startswith("Assets/AgentTools/generated")
            if is_generated_root and not scan_generated:
                continue
            for base, dirs, names in os.walk(root_path):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in sorted(names):
                    if name.endswith(".py") and not name.startswith("_"):
                        files.append(os.path.join(base, name))
        return sorted(files)

    def _config(self) -> dict[str, Any]:
        default = {
            "enabled": True,
            "tool_roots": ["Assets/AgentTools", "Assets/AgentTools/generated"],
            "scan_generated_tools": True,
            "disabled_tools": [],
        }
        if not self.project_path:
            return default
        path = os.path.join(self.project_path, "ProjectSettings", "agent_tools.json")
        if not os.path.isfile(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                return merged
        except Exception as exc:
            self._audit("config", False, f"Failed to read ProjectSettings/agent_tools.json: {exc}")
        return default

    def _resolve_project_path(self, path: str) -> str:
        root = resolved_path(self.project_path)
        raw = resolved_path(path if os.path.isabs(path) else os.path.join(root, path))
        if not is_path_within(raw, root):
            raise ValueError("Project tool path must stay inside the project.")
        return raw

    def _rel(self, path: str) -> str:
        return relative_path(path, self.project_path, allow_root=True)

    def _audit(self, action: str, success: bool, message: str, **extra: Any) -> None:
        event = {
            "time": time.time(),
            "action": action,
            "ok": bool(success),
            "message": str(message),
        }
        event.update({key: value for key, value in extra.items() if value not in (None, "")})
        self.audit_events.append(event)
        if len(self.audit_events) > 500:
            self.audit_events = self.audit_events[-500:]


def _is_envelope(value: Any) -> bool:
    return isinstance(value, dict) and "ok" in value and ("data" in value or "error" in value)


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _project_tool_timeout_ms() -> int:
    try:
        from Infernux.mcp.capabilities import limit
        return int(limit("project_tool_timeout_ms", 60000) or 60000)
    except Exception:
        return 60000


def _arguments(definition: ProjectToolDefinition | None, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    if definition is None:
        return dict(kwargs)
    try:
        bound = inspect.signature(definition.callable).bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except Exception:
        return {"args": list(args), **kwargs}

