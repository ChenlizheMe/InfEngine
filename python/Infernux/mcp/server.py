"""Embedded HTTP MCP server for Infernux Editor."""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.path_utils import resolved_path

HOST = "127.0.0.1"
PORT = 9713
PATH = "/mcp"
HEALTH_PATH = "/health"
SERVER_NAME = "Infernux Editor"

_server_thread: Optional[threading.Thread] = None
_server = None
_project_path = ""
_active_host = HOST
_active_port = PORT


def start_server(project_path: str, *, host: str = HOST, port: int = PORT) -> bool:
    """Start the embedded HTTP MCP server if it is not already running."""
    global _server_thread, _server, _project_path, _active_host, _active_port

    if _server_thread is not None and _server_thread.is_alive():
        return True

    try:
        FastMCP = _import_fastmcp()
    except Exception as exc:
        Debug.log_warning(
            "Infernux MCP disabled: install PyPI packages 'mcp' and 'fastmcp' to enable "
            f"the embedded HTTP server ({exc})."
        )
        return False

    _project_path = project_path
    _active_host = str(host)
    _active_port = int(port)
    from Infernux.mcp.capabilities import configure, feature_enabled, is_enabled
    capability_config = configure(project_path, write_default=True)
    if not is_enabled():
        Debug.log_internal("Infernux MCP server disabled by ProjectSettings/mcp_capabilities.json")
        return False
    from Infernux.mcp.session import configure as configure_session
    session_state = configure_session(project_path, capability_config)
    Debug.log_internal(
        "Infernux MCP session configured: "
        f"mode={session_state.mode}, build_profile={session_state.build_profile}, "
        f"recording={session_state.recording_enabled}"
    )
    if feature_enabled("session_call_log"):
        try:
            from Infernux.mcp.project_tools.trace import start_session_log
            info = start_session_log(project_path)
            Debug.log_internal(f"Infernux MCP session log initialized: {info.get('path')}")
        except Exception as exc:
            Debug.log_suppressed("Infernux.mcp.start_session_log", exc)
    _server = FastMCP(SERVER_NAME)

    # Keep health probing outside the streamable HTTP mount. A bare GET on
    # /mcp is transport negotiation, not a stable readiness endpoint.
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @_server.custom_route(HEALTH_PATH, methods=["GET"])  # type: ignore[attr-defined]
    async def _mcp_health_probe(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": SERVER_NAME,
                "message": "MCP endpoint is alive. Use streamable HTTP at /mcp for tool calls.",
                "transport": "streamable-http",
                "path": HEALTH_PATH,
                "url": endpoint_url(host=host, port=int(port)),
            }
        )

    from Infernux.mcp.tools import register_all_tools
    register_all_tools(_server, project_path, capability_config)
    if feature_enabled("discovery_files"):
        _write_discovery_files(project_path, host=host, port=int(port))

    def _run() -> None:
        last_error = None
        try:
            # Order matters: VS Code / Copilot negotiate MCP over streamable HTTP first
            # (see VS Code docs: HTTP Stream transport, then SSE fallback). FastMCP's
            # plain ``http`` transport can bind successfully but never completes the
            # JSON-RPC initialize handshake with those clients — logs show endless
            # "Waiting for server to respond to `initialize` request...".
            for transport in ("streamable-http", "http"):
                try:
                    _run_http_transport(_server, transport, host, int(port))
                    return
                except Exception as exc:
                    last_error = exc
                    if transport == "http":
                        raise
        except Exception as exc:
            Debug.log_error(f"Infernux MCP HTTP server stopped: {exc or last_error}")

    _server_thread = threading.Thread(target=_run, name="InfernuxMCPHTTP", daemon=True)
    _server_thread.start()
    Debug.log_internal(f"Infernux MCP HTTP server starting at {endpoint_url(host=host, port=int(port))}")
    return True


def _run_http_transport(server, transport: str, host: str, port: int) -> None:
    server.run(
        transport=transport,
        host=host,
        port=port,
        show_banner=False,
        # GUI launches use pythonw.exe, where stdout/stderr are None.
        # Uvicorn's default formatter calls isatty() before binding.
        uvicorn_config={"log_config": None},
    )


def stop_server() -> None:
    """Best-effort stop hook for editor shutdown."""
    global _server
    try:
        from Infernux.mcp.tools.editor_ui import set_semantic_capture_enabled
        set_semantic_capture_enabled(False)
    except Exception as exc:
        Debug.log_suppressed("Infernux.mcp.stop_server.semantic_capture", exc)
    server = _server
    _server = None
    for method_name in ("stop", "shutdown", "close"):
        method = getattr(server, method_name, None)
        if callable(method):
            try:
                method()
            except Exception as exc:
                Debug.log_suppressed(f"Infernux.mcp.stop_server.{method_name}", exc)
            break


def is_running() -> bool:
    return _server_thread is not None and _server_thread.is_alive()


def endpoint_url(*, host: str | None = None, port: int | None = None) -> str:
    resolved_host = _active_host if host is None else host
    resolved_port = _active_port if port is None else int(port)
    return f"http://{resolved_host}:{resolved_port}{PATH}"


def health_url(*, host: str | None = None, port: int | None = None) -> str:
    resolved_host = _active_host if host is None else host
    resolved_port = _active_port if port is None else int(port)
    return f"http://{resolved_host}:{resolved_port}{HEALTH_PATH}"


def connection_info(*, host: str | None = None, port: int | None = None) -> dict:
    resolved_host = _active_host if host is None else host
    resolved_port = _active_port if port is None else int(port)
    url = endpoint_url(host=resolved_host, port=resolved_port)
    return {
        "name": SERVER_NAME,
        "transport": "streamable-http",
        "host": resolved_host,
        "port": resolved_port,
        "path": PATH,
        "url": url,
        "health_url": health_url(host=resolved_host, port=resolved_port),
        "clients": _client_connection_configs(url),
    }


def _client_connection_configs(url: str) -> dict:
    return {
        "generic": {
            "file": "mcp.json",
            "format": "mcpServers",
            "config": {
                "mcpServers": {
                    "infernux-editor": {
                        "url": url,
                        "transport": "streamable-http",
                    }
                }
            },
        },
        "cursor": {
            "file": ".cursor/mcp.json",
            "format": "mcpServers",
            "config": {
                "mcpServers": {
                    "infernux-editor": {
                        "url": url,
                        "transport": "streamable-http",
                    }
                }
            },
        },
        "claude_code": {
            "file": ".mcp.json",
            "format": "mcpServers",
            "config": {
                "mcpServers": {
                    "infernux-editor": {
                        "type": "http",
                        "url": url,
                    }
                }
            },
        },
        "vscode_copilot": {
            "file": ".vscode/mcp.json",
            "format": "servers",
            "config": {
                "servers": {
                    "infernux-editor": {
                        "type": "http",
                        "url": url,
                    }
                }
            },
        },
        "trae": {
            "file": ".trae/mcp.json",
            "format": "mcpServers",
            "config": {
                "mcpServers": {
                    "infernux-editor": {
                        "type": "http",
                        "url": url,
                    }
                }
            },
        },
        "gemini": {
            "file": ".gemini/settings.json",
            "format": "mcpServers",
            "config": {
                "mcpServers": {
                    "infernux-editor": {
                        "httpUrl": url,
                        "timeout": 600000,
                        "trust": False,
                    }
                }
            },
        },
        "codex": {
            "file": ".codex/config.toml",
            "format": "toml:mcp_servers",
            "toml": (
                "[mcp_servers.\"infernux-editor\"]\n"
                f"url = \"{url}\"\n"
                "enabled = true\n"
                "startup_timeout_sec = 10\n"
                "tool_timeout_sec = 120\n"
            ),
        },
    }


def _write_discovery_files(project_path: str, *, host: str, port: int) -> None:
    """Write small project-local MCP discovery files for external agents.

    These files are intentionally data-only and safe to regenerate. They make
    the embedded editor MCP endpoint discoverable without hard-coding the port
    in an agent prompt.
    """
    root = resolved_path(project_path or "")
    if not root:
        return
    info = connection_info(host=host, port=port)
    try:
        os.makedirs(root, exist_ok=True)
        _write_generic_manifest(root, info)
        for client_name, client in info["clients"].items():
            if client_name == "generic":
                continue
            target = os.path.join(root, client["file"])
            if client.get("format") == "toml:mcp_servers":
                _upsert_toml_block(target, "infernux-editor", client["toml"])
            else:
                _merge_client_json_config(target, client["config"])
    except Exception as exc:
        Debug.log_suppressed("Infernux.mcp.write_discovery_files", exc)


def _write_generic_manifest(root: str, info: dict) -> None:
    generic = info["clients"]["generic"]["config"]
    _write_json_if_changed(os.path.join(root, "mcp.json"), {
        "infernux": {
            "name": info["name"],
            "transport": info["transport"],
            "host": info["host"],
            "port": info["port"],
            "path": info["path"],
            "url": info["url"],
        },
        "clients": {
            name: {"file": client["file"], "format": client["format"]}
            for name, client in info["clients"].items()
        },
        **generic,
    })


def _merge_client_json_config(path: str, config: dict) -> None:
    data = _read_json_object(path)
    for root_key, root_value in config.items():
        if isinstance(root_value, dict):
            bucket = data.setdefault(root_key, {})
            if isinstance(bucket, dict):
                bucket.update(root_value)
            else:
                data[root_key] = root_value
        else:
            data[root_key] = root_value
    _write_json_if_changed(path, data)


def _upsert_toml_block(path: str, server_name: str, block: str) -> None:
    start = f"# BEGIN INFERNUX MCP {server_name}"
    end = f"# END INFERNUX MCP {server_name}"
    marked = f"{start}\n{block.rstrip()}\n{end}\n"
    text = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    if start in text and end in text:
        before, rest = text.split(start, 1)
        _old, after = rest.split(end, 1)
        new_text = before.rstrip() + "\n\n" + marked + after.lstrip()
    else:
        duplicate_headers = (
            f'[mcp_servers."{server_name}"]',
            f"[mcp_servers.{server_name}]",
        )
        if any(header in text for header in duplicate_headers):
            return
        new_text = text.rstrip() + ("\n\n" if text.strip() else "") + marked
    _write_text_if_changed(path, new_text)


def _read_json_object(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_json_if_changed(path: str, value: dict) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    _write_text_if_changed(path, text)


def _write_text_if_changed(path: str, text: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _import_fastmcp():
    try:
        from fastmcp import FastMCP
        return FastMCP
    except Exception as first:
        try:
            from mcp.server.fastmcp import FastMCP
            return FastMCP
        except Exception as second:
            raise ImportError(
                "Need PyPI packages 'mcp' and 'fastmcp' (see ProjectSettings/requirements.txt). "
                f"Primary import failed: {first!r}; fallback failed: {second!r}"
            ) from second
