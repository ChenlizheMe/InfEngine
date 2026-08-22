"""Console MCP tools."""

from __future__ import annotations

from typing import Any

from Infernux.mcp.tools.common import main_thread


_LEVEL_ALIASES = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "LOG": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ASSERT": "ERROR",
    "EXCEPTION": "ERROR",
    "FATAL": "FATAL",
}


def _canonical_level(value: Any) -> str:
    """Normalize Python and native log names to the MCP vocabulary."""
    raw = getattr(value, "name", value)
    return _LEVEL_ALIASES.get(str(raw).upper(), str(raw).upper())


def _native_console():
    """Return the authoritative native Console panel when the Editor is live."""
    try:
        from Infernux.engine.bootstrap import EditorBootstrap

        bootstrap = EditorBootstrap.instance()
        return getattr(bootstrap, "console", None) if bootstrap is not None else None
    except (AttributeError, ImportError, RuntimeError):
        return None


def _serialize_python_entry(entry: Any) -> dict[str, Any]:
    return {
        "time": entry.get_formatted_time(),
        "level": _canonical_level(entry.log_type),
        "message": entry.message,
        "source_file": entry.source_file,
        "source_line": entry.source_line,
        "stack_trace": entry.stack_trace,
        "uid": None,
        "latest_uid": None,
        "count": 1,
    }


def _filter_levels(entries: list[dict[str, Any]], levels: list[str] | None) -> list[dict[str, Any]]:
    if not levels:
        return entries
    allowed = {_canonical_level(level) for level in levels}
    return [entry for entry in entries if _canonical_level(entry.get("level")) in allowed]


def _read_native_entries(panel: Any, limit: int, levels: list[str] | None) -> dict[str, Any] | None:
    """Read the native Console's bounded visible cache, if supported."""
    reader = getattr(panel, "_get_visible_log_snapshot", None)
    if not callable(reader):
        return None

    entries = [dict(item) for item in reader(limit)]
    entries = _filter_levels(entries, levels)
    view_options = {}
    for option in ("show_info", "show_warnings", "show_errors", "collapse", "follow"):
        try:
            view_options[option] = bool(panel.get_view_option(option))
        except (AttributeError, RuntimeError):
            pass

    status_bar = None
    try:
        message, level, info, warnings, errors, uid = panel._get_status_snapshot()
        status_bar = {
            "surface": "status_bar",
            "message": message,
            "level": _canonical_level(level),
            "counts": {"info": info, "warnings": warnings, "errors": errors},
            "mirrors_console_uid": uid or None,
        }
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass

    try:
        search = panel.get_search_query()
    except (AttributeError, RuntimeError):
        search = ""

    return {
        "entries": entries,
        "source": "native_console",
        "surface": "console",
        "filters": {"levels": list(levels or []), "panel": view_options, "search": search},
        "status_bar": status_bar,
    }


def register_console_tools(mcp) -> None:
    @mcp.tool(name="console_read")
    def console_read(limit: int = 100, levels: list[str] | None = None) -> dict:
        """Read the authoritative Console view without treating status-bar overlays as entries.

        In the Editor, native C++ logs and Python Debug logs share the native
        Console panel. The response labels that source explicitly. The
        ``status_bar`` field is a separate UI surface and is never included in
        ``entries``; this matters for renderer Profile text shown at the
        bottom of the Editor window.
        """

        def _read():
            from Infernux.debug import DebugConsole

            bounded_limit = max(int(limit), 1)
            native_panel = _native_console()
            native = _read_native_entries(native_panel, bounded_limit, levels) if native_panel else None
            if native is not None:
                return native

            entries: list[dict[str, Any]] = []
            for entry in DebugConsole.instance().get_entries()[-bounded_limit:]:
                entries.append(_serialize_python_entry(entry))
            entries = _filter_levels(entries, levels)
            return {
                "entries": entries,
                "source": "python_debug_fallback",
                "surface": "console",
                "filters": {"levels": list(levels or [])},
                "status_bar": None,
            }

        return main_thread("console_read", _read)
