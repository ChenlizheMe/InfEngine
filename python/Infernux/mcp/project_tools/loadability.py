"""Loadability checks for project-defined MCP tools."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import py_compile
import sys
import traceback
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable

from Infernux.engine.path_utils import is_path_within, path_key, relative_path, resolved_path
from Infernux.engine.project_context import temporary_script_import_paths
from Infernux.mcp.project_tools.decorators import AgentToolMetadata, InxAgentToolset


@dataclass
class ProjectToolDefinition:
    name: str
    summary: str
    callable: Callable
    path: str
    rel_path: str
    module_name: str
    generated: bool = False
    tags: list[str] = field(default_factory=list)
    source_trace: str = ""
    source_traces: list[str] = field(default_factory=list)
    validation: str = ""
    status: str = "loaded"


def module_name_for_path(path: str) -> str:
    normalized = path_key(path)
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]
    stem = os.path.splitext(os.path.basename(path))[0]
    safe_stem = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in stem)
    return f"infernux_project_tool_{safe_stem}_{digest}"


def validate_file(project_path: str, path: str) -> dict[str, Any]:
    file_path = _resolve_project_file(project_path, path)
    rel_path = relative_path(file_path, project_path)
    result: dict[str, Any] = {
        "path": rel_path,
        "ok": False,
        "violations": [],
        "tools": [],
    }
    if not os.path.isfile(file_path):
        result["violations"].append({"code": "not_found", "message": f"File not found: {rel_path}"})
        return result
    if not file_path.endswith(".py"):
        result["violations"].append({"code": "not_python", "message": "Project MCP tools must be Python files."})
        return result

    try:
        py_compile.compile(file_path, doraise=True)
    except py_compile.PyCompileError as exc:
        result["violations"].append({"code": "syntax_error", "message": str(exc)})
        return result

    load = load_tool_module(project_path, file_path)
    if not load["ok"]:
        result["violations"].append({
            "code": "import_error",
            "message": load.get("error", ""),
            "traceback": load.get("traceback", ""),
        })
        return result

    definitions = collect_tool_definitions(load["module"], project_path, file_path)
    for definition in definitions:
        schema_issues = validate_callable_schema(definition.callable)
        for issue in schema_issues:
            result["violations"].append({"code": "schema_error", "tool": definition.name, "message": issue})
        result["tools"].append(_definition_summary(definition))
    result["ok"] = not result["violations"]
    return result


def load_tool_module(project_path: str, file_path: str) -> dict[str, Any]:
    module_name = module_name_for_path(file_path)
    sys.modules.pop(module_name, None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Failed to create module spec for {file_path}")
        module = importlib.util.module_from_spec(spec)
        with temporary_script_import_paths(file_path):
            spec.loader.exec_module(module)
        sys.modules[module_name] = module
        return {"ok": True, "module": module, "module_name": module_name}
    except Exception as exc:
        return {
            "ok": False,
            "module_name": module_name,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }


def collect_tool_definitions(module: ModuleType, project_path: str, file_path: str) -> list[ProjectToolDefinition]:
    rel_path = relative_path(file_path, project_path)
    module_name = getattr(module, "__name__", module_name_for_path(file_path))
    definitions: list[ProjectToolDefinition] = []
    for _, obj in inspect.getmembers(module, inspect.isfunction):
        meta = getattr(obj, "__inx_agent_tool__", None)
        if isinstance(meta, AgentToolMetadata):
            definitions.append(_definition_from_meta(meta, obj, file_path, rel_path, module_name))

    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls is InxAgentToolset or not issubclass(cls, InxAgentToolset):
            continue
        if getattr(cls, "__module__", "") != module_name:
            continue
        try:
            instance = cls()
        except Exception:
            continue
        namespace = str(getattr(cls, "namespace", "project") or "project").strip(".")
        class_tags = [str(tag) for tag in getattr(cls, "tags", [])]
        for method_name, method in inspect.getmembers(instance, inspect.ismethod):
            meta = getattr(method.__func__, "__inx_agent_action__", None)
            if not isinstance(meta, AgentToolMetadata):
                continue
            action_name = meta.name or method_name
            full_name = action_name if "." in action_name else f"{namespace}.{action_name}"
            merged = AgentToolMetadata(
                name=full_name,
                summary=meta.summary,
                tags=class_tags + list(meta.tags),
                generated=meta.generated,
                source_trace=meta.source_trace,
                source_traces=list(meta.source_traces),
                validation=meta.validation,
                extra=dict(meta.extra),
            )
            definitions.append(_definition_from_meta(merged, method, file_path, rel_path, module_name))
    return definitions


def validate_callable_schema(fn: Callable) -> list[str]:
    issues: list[str] = []
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        return [f"Cannot inspect signature: {exc}"]
    for name, param in signature.parameters.items():
        if name == "self":
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            issues.append(f"Parameter '{name}' uses *args/**kwargs, which cannot form a stable MCP schema.")
        if param.annotation is inspect.Signature.empty:
            issues.append(f"Parameter '{name}' has no type annotation.")
    if signature.return_annotation is inspect.Signature.empty:
        issues.append("Return type has no annotation; use -> dict for best MCP schema quality.")
    return issues


def _definition_from_meta(
    meta: AgentToolMetadata,
    fn: Callable,
    file_path: str,
    rel_path: str,
    module_name: str,
) -> ProjectToolDefinition:
    return ProjectToolDefinition(
        name=meta.name,
        summary=meta.summary or (inspect.getdoc(fn) or ""),
        callable=fn,
        path=file_path,
        rel_path=rel_path,
        module_name=module_name,
        generated=meta.generated,
        tags=list(meta.tags),
        source_trace=meta.source_trace,
        source_traces=list(meta.source_traces),
        validation=meta.validation,
    )


def _definition_summary(definition: ProjectToolDefinition) -> dict[str, Any]:
    return {
        "name": definition.name,
        "summary": definition.summary,
        "path": definition.rel_path,
        "generated": definition.generated,
        "tags": definition.tags,
        "validation": definition.validation,
    }


def _resolve_project_file(project_path: str, path: str) -> str:
    root = resolved_path(project_path)
    raw = resolved_path(path if os.path.isabs(path) else os.path.join(root, path))
    if not is_path_within(raw, root):
        raise ValueError("Project tool path must stay inside the project.")
    return raw

