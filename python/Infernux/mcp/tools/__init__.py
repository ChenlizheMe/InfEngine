"""MCP tool registration for the embedded Infernux server."""

from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any

from Infernux.mcp import capabilities


def register_all_tools(mcp, project_path: str, config: dict[str, Any] | None = None) -> None:
    """Register all enabled MCP tool groups.

    The import style is intentionally lazy so disabled groups do not pull in
    optional editor subsystems during startup.
    """
    config = config or capabilities.current_config()
    if not bool(config.get("enabled", True)):
        return
    gated_mcp = _ToolGate(mcp, config)

    if _group(config, "docs"):
        from Infernux.mcp.tools.docs import register_docs_tools
        register_docs_tools(gated_mcp, project_path, config)
    if _group(config, "session") and _feature(config, "session_modes"):
        from Infernux.mcp.tools.session import register_session_tools
        register_session_tools(gated_mcp, project_path)
    if _group(config, "input") and _feature(config, "input_injection"):
        from Infernux.mcp.tools.input import register_input_tools
        register_input_tools(gated_mcp)
    if _group(config, "ui_semantics") and _feature(config, "semantic_ui_capture"):
        from Infernux.mcp.tools.editor_ui import register_editor_ui_tools
        register_editor_ui_tools(gated_mcp)
    if _group(config, "capture") and _feature(config, "engine_capture"):
        from Infernux.mcp.tools.capture import register_capture_tools
        register_capture_tools(gated_mcp, project_path)
    if _group(config, "player_validation") and _feature(config, "player_validation"):
        from Infernux.mcp.tools.player import register_player_tools
        register_player_tools(gated_mcp, project_path)
    if _group(config, "api"):
        from Infernux.mcp.tools.api import register_api_tools
        register_api_tools(gated_mcp)
    if _group(config, "project"):
        from Infernux.mcp.tools.project import register_project_tools
        register_project_tools(gated_mcp, project_path)
    if _group(config, "editor"):
        from Infernux.mcp.tools.editor import register_editor_tools
        register_editor_tools(gated_mcp)
    if _group(config, "scene"):
        from Infernux.mcp.tools.scene import register_scene_tools
        register_scene_tools(gated_mcp)
    if _group(config, "hierarchy"):
        from Infernux.mcp.tools.hierarchy import register_hierarchy_tools
        register_hierarchy_tools(gated_mcp)
    if _group(config, "asset"):
        from Infernux.mcp.tools.assets import register_asset_tools
        register_asset_tools(gated_mcp, project_path)
        from Infernux.mcp.tools.particle import register_particle_tools
        register_particle_tools(gated_mcp, project_path)
        from Infernux.mcp.tools.authoring import register_authoring_tools
        register_authoring_tools(gated_mcp, project_path)
    if _group(config, "material"):
        from Infernux.mcp.tools.material import register_material_tools
        register_material_tools(gated_mcp, project_path)
    if _group(config, "renderstack"):
        from Infernux.mcp.tools.renderstack import register_renderstack_tools
        register_renderstack_tools(gated_mcp, project_path)
    if _group(config, "console"):
        from Infernux.mcp.tools.console import register_console_tools
        register_console_tools(gated_mcp)
    if _group(config, "camera"):
        from Infernux.mcp.tools.camera import register_camera_tools
        register_camera_tools(gated_mcp)
    if _group(config, "runtime") and _feature(config, "runtime_observation"):
        from Infernux.mcp.tools.runtime import register_runtime_tools
        register_runtime_tools(gated_mcp)
        from Infernux.mcp.tools.particle import register_particle_runtime_tools
        register_particle_runtime_tools(gated_mcp)
    if _group(config, "ui"):
        from Infernux.mcp.tools.ui import register_ui_tools
        register_ui_tools(gated_mcp)
    if _group(config, "transactions") and _feature(config, "transactions"):
        from Infernux.mcp.tools.transactions import register_transaction_tools
        register_transaction_tools(gated_mcp, project_path)
    if _group(config, "research"):
        from Infernux.mcp.tools.research import register_research_tools
        register_research_tools(gated_mcp, project_path)
    if _group(config, "project_tool_management") and _feature(config, "project_defined_tools"):
        from Infernux.mcp.tools.project_tools import register_project_tool_management
        register_project_tool_management(gated_mcp, project_path)
    if _group(config, "project_defined_tools") and _feature(config, "project_defined_tools"):
        from Infernux.mcp.tools.project_tools import register_project_defined_tools
        register_project_defined_tools(gated_mcp, project_path)


def _feature(config: dict[str, Any], name: str) -> bool:
    return bool((config.get("features") or {}).get(name, True))


def _group(config: dict[str, Any], name: str) -> bool:
    return capabilities.tool_group_enabled(name)


class _ToolGate:
    def __init__(self, mcp, config: dict[str, Any]) -> None:
        self._mcp = mcp
        self._disabled = set(str(item) for item in config.get("disabled_tools", []))
        self._registered_names: set[str] = set()

    def registered_tool_names(self) -> frozenset[str]:
        """Return the tools actually attached to this MCP server instance."""
        return frozenset(self._registered_names)

    def tool(self, *args, **kwargs):
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        if name and (str(name) in self._disabled or not capabilities.tool_enabled(str(name))):
            def _skip(fn):
                return fn
            return _skip
        decorator = self._mcp.tool(*args, **kwargs)

        def _register(fn):
            tool_name = str(name or getattr(fn, "__name__", ""))
            try:
                from Infernux.mcp.tools.common import register_tool_signature
                register_tool_signature(tool_name, fn)
            except Exception:
                pass
            registered = decorator(_trace_public_tool_call(tool_name, fn))
            if tool_name:
                self._registered_names.add(tool_name)
            return registered

        return _register

    def __getattr__(self, name: str):
        return getattr(self._mcp, name)


def _trace_public_tool_call(tool_name: str, fn):
    """Wrap each registered MCP endpoint once, not every helper it invokes."""
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def _async_wrapper(*args, **kwargs):
            return await _invoke_traced_async(tool_name, fn, args, kwargs)

        return _async_wrapper

    @wraps(fn)
    def _sync_wrapper(*args, **kwargs):
        return _invoke_traced_sync(tool_name, fn, args, kwargs)

    return _sync_wrapper


def _invoke_traced_sync(tool_name: str, fn, args: tuple[Any, ...], kwargs: dict[str, Any]):
    from Infernux.mcp.project_tools import trace

    if trace.public_tool_trace_active():
        return fn(*args, **kwargs)
    return _invoke_with_trace(tool_name, fn, args, kwargs)


async def _invoke_traced_async(tool_name: str, fn, args: tuple[Any, ...], kwargs: dict[str, Any]):
    from Infernux.mcp.project_tools import trace

    if trace.public_tool_trace_active():
        return await fn(*args, **kwargs)
    return await _invoke_with_trace_async(tool_name, fn, args, kwargs)


def _invoke_with_trace(tool_name: str, fn, args: tuple[Any, ...], kwargs: dict[str, Any]):
    from Infernux.mcp.project_tools import trace

    arguments = _bound_arguments(fn, args, kwargs)
    trace_was_active = bool(trace.current_trace().get("active"))
    started = time.monotonic()
    token = trace.begin_public_tool_trace()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        trace.end_public_tool_trace(token)
        _record_public_tool_result(
            tool_name,
            trace_was_active=trace_was_active,
            started=started,
            arguments=arguments,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    trace.end_public_tool_trace(token)
    _record_public_tool_result(
        tool_name,
        trace_was_active=trace_was_active,
        started=started,
        arguments=arguments,
        result=result,
    )
    return result


async def _invoke_with_trace_async(tool_name: str, fn, args: tuple[Any, ...], kwargs: dict[str, Any]):
    from Infernux.mcp.project_tools import trace

    arguments = _bound_arguments(fn, args, kwargs)
    trace_was_active = bool(trace.current_trace().get("active"))
    started = time.monotonic()
    token = trace.begin_public_tool_trace()
    try:
        result = await fn(*args, **kwargs)
    except Exception as exc:
        trace.end_public_tool_trace(token)
        _record_public_tool_result(
            tool_name,
            trace_was_active=trace_was_active,
            started=started,
            arguments=arguments,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    trace.end_public_tool_trace(token)
    _record_public_tool_result(
        tool_name,
        trace_was_active=trace_was_active,
        started=started,
        arguments=arguments,
        result=result,
    )
    return result


def _record_public_tool_result(
    tool_name: str,
    *,
    trace_was_active: bool,
    started: float,
    arguments: dict[str, Any],
    result: Any,
    error: str = "",
) -> None:
    from Infernux.mcp.project_tools import trace

    elapsed_ms = (time.monotonic() - started) * 1000.0
    active_after = bool(trace.current_trace().get("active"))
    ok_flag = bool(result.get("ok", True)) if isinstance(result, dict) else not error
    if trace_was_active and active_after:
        trace.record_tool_call(
            tool_name,
            ok=ok_flag,
            elapsed_ms=elapsed_ms,
            arguments=arguments,
            result=result,
            error=error,
        )
    else:
        trace.record_tool_result(
            tool_name,
            ok=ok_flag,
            elapsed_ms=elapsed_ms,
            arguments=arguments,
            result=result,
            error=error,
        )


def _bound_arguments(fn, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        return _redact_sensitive_arguments(dict(bound.arguments))
    except (TypeError, ValueError):
        value = {"args": list(args)} if args else {}
        value.update(kwargs)
        return _redact_sensitive_arguments(value)


def _redact_sensitive_arguments(value: Any) -> Any:
    """Keep Supervisor and other bearer credentials out of traces and session logs."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if (
                normalized in {"lease", "lease_token", "password", "secret"}
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
            ):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = _redact_sensitive_arguments(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive_arguments(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive_arguments(item) for item in value]
    return value
