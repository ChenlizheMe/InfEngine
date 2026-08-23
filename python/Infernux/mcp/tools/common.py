"""Shared helpers for Infernux MCP tools."""

from __future__ import annotations

import inspect
import os
import secrets
import time
from typing import Any, Callable

from Infernux.engine.path_utils import is_path_within, relative_path, resolved_path, same_path
from Infernux.mcp.threading import MainThreadCommandQueue

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SERVER_VERSION = "0.3.5"

_TOOL_METADATA: dict[str, dict[str, Any]] = {}
_KNOWLEDGE_TOKENS: dict[str, dict[str, Any]] = {}


class _MainThreadGuardRejection:
    """Carry a policy rejection out of the queued main-thread callback."""

    __slots__ = ("result",)

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

# Generic MCP file editing is deliberately narrower than the AssetDatabase.
# These files are authored as external source/text and may be safely restored
# from an MCP snapshot without competing with an editor document controller.
EXTERNAL_SOURCE_TEXT_EXTENSIONS = frozenset({
    ".bat",
    ".c",
    ".cc",
    ".cfg",
    ".cjs",
    ".cmake",
    ".cmd",
    ".comp",
    ".cpp",
    ".css",
    ".csv",
    ".cxx",
    ".frag",
    ".glsl",
    ".h",
    ".hh",
    ".hlsl",
    ".hpp",
    ".htm",
    ".html",
    ".hxx",
    ".ini",
    ".inl",
    ".js",
    ".json",
    ".jsx",
    ".lua",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".pyi",
    ".rc",
    ".scss",
    ".sh",
    ".shadingmodel",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".vert",
    ".webmanifest",
    ".xml",
    ".yaml",
    ".yml",
})

EXTERNAL_SOURCE_TEXT_FILENAMES = frozenset({
    ".clang-format",
    ".clang-tidy",
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "cmakelists.txt",
})

EDITOR_OWNED_ASSET_EXTENSIONS = frozenset({
    ".animclip2d",
    ".animclip3d",
    ".animfsm",
    ".animtimeline",
    ".effect",
    ".effectgroup",
    ".mat",
    ".meta",
    ".particlegraph",
    ".physicmaterial",
    ".prefab",
    ".scene",
    ".timelinefsm",
})

EXTERNAL_SOURCE_TRANSACTION_OPERATIONS = frozenset({
    "edit_text",
    "patch_text",
    "write_json",
    "write_text",
})

EDITOR_OWNED_PROJECT_DIRECTORIES = frozenset({
    ".infernux",
    "library",
    "projectsettings",
})


class KnowledgeTokenError(Exception):
    def __init__(self, scope: str, required_tool: str, provided: str = "") -> None:
        self.scope = str(scope or "")
        self.required_tool = str(required_tool or "")
        self.provided = str(provided or "")
        super().__init__(
            f"Invalid or missing knowledge token for '{self.scope}'. "
            f"Call {self.required_tool} first to learn the required knowledge and receive a temporary token."
        )


def issue_knowledge_token(scope: str, *, source_tool: str, ttl_seconds: int = 7200) -> dict[str, Any]:
    """Issue a temporary proof that the agent requested subsystem knowledge."""
    normalized_scope = str(scope or "").strip().lower()
    token = f"{normalized_scope}.{secrets.token_urlsafe(18)}"
    expires_at = time.time() + max(int(ttl_seconds or 7200), 60)
    _KNOWLEDGE_TOKENS[token] = {
        "scope": normalized_scope,
        "source_tool": str(source_tool or ""),
        "expires_at": expires_at,
    }
    return {
        "scope": normalized_scope,
        "token": token,
        "expires_at": expires_at,
        "ttl_seconds": max(int(ttl_seconds or 7200), 60),
        "usage": "Pass this value as knowledge_token to gated MCP tools for this subsystem.",
    }


def require_knowledge_token(scope: str, token: str, *, required_tool: str) -> None:
    """Raise KnowledgeTokenError unless *token* is a live token for *scope*."""
    normalized_scope = str(scope or "").strip().lower()
    token = str(token or "").strip()
    record = _KNOWLEDGE_TOKENS.get(token)
    if not record or record.get("scope") != normalized_scope or float(record.get("expires_at", 0.0)) < time.time():
        raise KnowledgeTokenError(normalized_scope, required_tool, token)


def register_tool_metadata(
    name: str,
    *,
    summary: str = "",
    category: str = "",
    tags: list[str] | None = None,
    level: str = "semantic",
    aliases: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
    preconditions: list[str] | None = None,
    postconditions: list[str] | None = None,
    side_effects: list[str] | None = None,
    next_suggested_tools: list[str] | None = None,
    recovery: list[str] | None = None,
    examples: list[dict[str, Any]] | None = None,
    concepts: dict[str, str] | None = None,
    invariants: list[str] | None = None,
    risk_level: str = "medium",
    feature: str = "",
) -> None:
    """Register human/agent-facing metadata for a tool."""
    _TOOL_METADATA[name] = {
        "name": name,
        "summary": summary,
        "category": str(category or _default_tool_category(name)),
        "tags": _normalized_string_list(tags),
        "level": str(level or "semantic"),
        "aliases": _normalized_string_list(aliases),
        "parameters": parameters or {},
        "returns": {},
        "preconditions": preconditions or [],
        "postconditions": postconditions or [],
        "side_effects": side_effects or [],
        "next_suggested_tools": next_suggested_tools or [],
        "recovery": recovery or [],
        "examples": examples or [],
        "concepts": concepts or {},
        "invariants": invariants or [],
        "risk_level": str(risk_level or "medium"),
        "feature": str(feature or ""),
    }


def get_tool_metadata(name: str) -> dict[str, Any]:
    meta = dict(_TOOL_METADATA.get(name, {}))
    if not meta:
        meta = {
            "name": name,
            "summary": "No detailed metadata registered yet.",
            "category": _default_tool_category(name),
            "tags": [],
            "level": "semantic",
            "aliases": [],
            "parameters": {},
            "returns": {},
            "preconditions": [],
            "postconditions": [],
            "side_effects": [],
            "next_suggested_tools": [],
            "recovery": ["Use mcp_capabilities or mcp_list_tools_verbose to inspect available tools."],
            "examples": [],
            "concepts": {},
            "invariants": [],
            "risk_level": "medium",
            "feature": "",
        }
    return meta


def register_tool_signature(name: str, fn: Callable) -> None:
    """Merge callable signature details into existing tool metadata."""
    tool_name = str(name or getattr(fn, "__name__", ""))
    if not tool_name:
        return
    meta = get_tool_metadata(tool_name)
    signature = _callable_signature_schema(fn)
    existing_params = meta.get("parameters") or {}
    if existing_params:
        merged_params = dict(existing_params)
        for key, value in signature.get("properties", {}).items():
            merged_params.setdefault(key, value)
    else:
        merged_params = signature.get("properties", {})
    meta["parameters"] = merged_params
    meta["required_parameters"] = signature.get("required", [])
    meta["signature"] = signature.get("signature", "")
    meta.setdefault("returns", signature.get("returns", {}))
    if not meta.get("returns"):
        meta["returns"] = signature.get("returns", {})
    doc = inspect.getdoc(fn) or ""
    meta["doc"] = doc or meta.get("doc", "")
    if doc and (not meta.get("summary") or str(meta.get("summary", "")).startswith("No detailed")):
        meta["summary"] = doc.splitlines()[0].strip()
    if not meta.get("examples"):
        meta["examples"] = [{
            "description": f"Minimal argument payload for {tool_name}. Replace placeholders before calling.",
            "arguments": _example_arguments(signature.get("properties", {}), signature.get("required", [])),
        }]
    _TOOL_METADATA[tool_name] = meta


def _callable_signature_schema(fn: Callable) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"properties": {}, "required": [], "signature": "(...)"}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name.startswith("_"):
            continue
        item: dict[str, Any] = {
            "kind": str(param.kind).replace("Parameter.", ""),
            "annotation": _annotation_name(param.annotation),
        }
        if param.default is inspect._empty:
            required.append(name)
        else:
            item["default"] = _jsonable_default(param.default)
        properties[name] = item
    return {
        "properties": properties,
        "required": required,
        "signature": str(sig),
        "returns": {
            "annotation": _annotation_name(sig.return_annotation),
            "envelope": "MCP tools return {'ok': true, 'data': ...} on success or {'ok': false, 'error': ...} on failure.",
        },
    }


def _annotation_name(annotation: Any) -> str:
    if annotation is inspect._empty:
        return "Any"
    return str(annotation).replace("typing.", "")


def _jsonable_default(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return repr(value)


def _example_arguments(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for name, schema in properties.items():
        if name not in required and "default" in schema:
            args[name] = schema["default"]
            continue
        args[name] = _placeholder_for_annotation(str(schema.get("annotation", "Any")), name)
    return args


def _placeholder_for_annotation(annotation: str, name: str) -> Any:
    lowered = annotation.lower()
    if "bool" in lowered:
        return False
    if "int" in lowered:
        return 0
    if "float" in lowered:
        return 0.0
    if "list" in lowered:
        return []
    if "dict" in lowered:
        return {}
    if "str" in lowered:
        return f"<{name}>"
    return None


def _default_tool_category(name: str) -> str:
    categories = {
        "mcp": "foundation/discovery",
        "engine": "foundation/concepts",
        "workflow": "foundation/workflows",
        "project": "project/info",
        "project_tools": "project/tools",
        "transaction": "project/transactions",
        "asset": "assets/files",
        "editor": "editor/state",
        "runtime": "runtime/observation",
        "console": "runtime/console",
        "scene": "scene/query",
        "gameobject": "scene/object",
        "hierarchy": "scene/create",
        "transform": "scene/transform",
        "component": "scene/component",
        "camera": "camera/framing",
        "lighting": "scene/lighting",
        "ui": "ui/screen",
        "material": "assets/materials",
        "renderstack": "renderstack/pipeline",
    }
    tool_name = str(name or "")
    for prefix in sorted(categories, key=len, reverse=True):
        if tool_name == prefix or tool_name.startswith(prefix + "_"):
            return categories[prefix]
    return "misc/other"


def _normalized_string_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [str(value) for value in values if str(value)]


def list_tool_metadata() -> list[dict[str, Any]]:
    return [get_tool_metadata(name) for name in sorted(_TOOL_METADATA)]


def explain_for(
    tool: str,
    *,
    summary: str = "",
    warnings: list[str] | None = None,
    side_effects: list[str] | None = None,
    next_suggested_tools: list[str] | None = None,
    recovery: list[str] | None = None,
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    meta = get_tool_metadata(tool)
    return {
        "tool": tool,
        "summary": summary or meta.get("summary", ""),
        "concepts": concepts or meta.get("concepts", {}),
        "side_effects": side_effects if side_effects is not None else meta.get("side_effects", []),
        "warnings": warnings or [],
        "next_suggested_tools": (
            next_suggested_tools
            if next_suggested_tools is not None
            else meta.get("next_suggested_tools", [])
        ),
        "recovery": recovery if recovery is not None else meta.get("recovery", []),
    }


def ok(data: Any = None, *, explain: dict[str, Any] | None = None, trace: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "data": data if data is not None else {}}
    if explain is not None:
        payload["explain"] = explain
    if trace is not None:
        payload["trace"] = trace
    return payload


def fail(
    code: str,
    message: str,
    *,
    hint: str = "",
    explain: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    payload: dict[str, Any] = {"ok": False, "error": error}
    if explain is not None:
        payload["explain"] = explain
    return payload


def main_thread(
    name: str,
    fn,
    *,
    timeout_ms: int = 30000,
    explain: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response_explain = explain or explain_for(name)
    timeout_ms = _configured_timeout_ms(timeout_ms)
    started = time.monotonic()
    try:
        def _run_automation_action():
            from Infernux.engine.interaction import ActionOrigin, action_origin_scope

            # State validation and mutation are one main-thread critical
            # operation. Reading SceneFileManager from the MCP server thread
            # can otherwise approve a command from stale Play/load state.
            guard_failure = _scene_guard_failure(name, response_explain)
            if guard_failure is not None:
                return _MainThreadGuardRejection(guard_failure)
            with action_origin_scope(ActionOrigin.AUTOMATION):
                return fn()

        value = MainThreadCommandQueue.instance().run_sync(
            name,
            _run_automation_action,
            timeout_ms=timeout_ms,
        )
        if isinstance(value, _MainThreadGuardRejection):
            result = value.result
            _record_trace(
                name,
                False,
                started,
                result.get("error", {}).get("message", ""),
                arguments=arguments,
                result=result,
            )
            return result
        result = ok(value, explain=response_explain)
        _record_trace(name, True, started, arguments=arguments, result=result)
        return result
    except TimeoutError as exc:
        result = fail(
            "error.timeout",
            str(exc),
            hint="The editor main thread did not process this command in time. Ensure the editor is not blocked by a modal operation.",
            explain=response_explain,
        )
        _record_trace(name, False, started, str(exc), arguments=arguments, result=result)
        return result
    except ValueError as exc:
        result = fail("error.invalid_argument", str(exc), hint="Check parameter schema with mcp_help or component.describe_type.", explain=response_explain)
        _record_trace(name, False, started, str(exc), arguments=arguments, result=result)
        return result
    except FileExistsError as exc:
        result = fail("error.exists", str(exc), hint="Treat existing folders as reusable. Use asset_ensure_folder, asset_list, or overwrite=true for files when appropriate.", explain=response_explain)
        _record_trace(name, False, started, str(exc), arguments=arguments, result=result)
        return result
    except FileNotFoundError as exc:
        result = fail("error.not_found", str(exc), hint="Use scene_inspect, gameobject_find, or asset_search to locate valid targets.", explain=response_explain)
        _record_trace(name, False, started, str(exc), arguments=arguments, result=result)
        return result
    except KnowledgeTokenError as exc:
        result = fail(
            "error.knowledge_token_required",
            str(exc),
            hint=f"Call {exc.required_tool} to obtain a fresh knowledge token, then retry with knowledge_token=<token>.",
            explain={
                **response_explain,
                "recovery": [
                    f"Call {exc.required_tool} and read the returned guide.",
                    "Copy data.knowledge_lock.token from the guide response.",
                    "Retry this tool with knowledge_token set to that token.",
                ],
                "next_suggested_tools": [exc.required_tool, "api_search", "mcp_catalog_search"],
            },
        )
        result["data"] = {
            "scope": exc.scope,
            "provided_token": exc.provided,
            "required_tool": exc.required_tool,
        }
        _record_trace(name, False, started, str(exc), arguments=arguments, result=result)
        return result
    except Exception as exc:
        result = fail("error.internal", str(exc), hint="Read console_read and retry with a smaller operation.", explain=response_explain)
        _record_trace(name, False, started, str(exc), arguments=arguments, result=result)
        return result


def _record_trace(
    name: str,
    ok_flag: bool,
    started: float,
    error: str = "",
    arguments: dict[str, Any] | None = None,
    result: Any = None,
) -> None:
    try:
        from Infernux.mcp.capabilities import feature_enabled
        from Infernux.mcp.project_tools.trace import record_tool_call, record_tool_result
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if feature_enabled("trace_recorder"):
            record_tool_call(name, ok=ok_flag, elapsed_ms=elapsed_ms, arguments=arguments, result=result, error=error)
        else:
            record_tool_result(name, ok=ok_flag, elapsed_ms=elapsed_ms, arguments=arguments, result=result, error=error)
    except Exception:
        pass


def _configured_timeout_ms(default: int) -> int:
    try:
        from Infernux.mcp.capabilities import limit
        return int(limit("main_thread_timeout_ms", default) or default)
    except Exception:
        return int(default)


def _scene_guard_failure(name: str, explain: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
    except Exception:
        return None
    if sfm is None:
        return None
    status = scene_status()
    if name in {"scene_open", "scene_new", "scene_discard", "scene_save"}:
        if status["play_state"] != "edit":
            return _state_guard_fail(
                "error.play_mode_active",
                f"{name} is not allowed while Play Mode is active. Stop Play Mode first.",
                status,
                explain,
                next_tools=["editor_stop", "runtime_wait", "scene_status"],
            )
        if status["loading"]:
            return _state_guard_fail(
                "error.scene_loading",
                f"{name} is not allowed while a scene load/new operation is pending.",
                status,
                explain,
                next_tools=["runtime_wait", "scene_status"],
            )
    if name in {"scene_open", "scene_new"} and status["dirty"]:
        return _state_guard_fail(
            "error.scene_dirty",
            "The active scene has unsaved changes. Save it before switching scenes.",
            status,
            explain,
            next_tools=["scene_save", "scene_status"],
        )
    if _is_edit_mode_mutation(name):
        if status["play_state"] != "edit":
            return _state_guard_fail(
                "error.play_mode_active",
                f"{name} mutates the editor scene and is not allowed while Play Mode is active.",
                status,
                explain,
                next_tools=["editor_stop", "runtime_wait", "scene_status"],
            )
        if status["loading"]:
            return _state_guard_fail(
                "error.scene_loading",
                f"{name} mutates the editor scene and is not allowed while a scene load/new operation is pending.",
                status,
                explain,
                next_tools=["runtime_wait", "scene_status"],
            )
    if _requires_saved_scene_file(name) and not status["path"]:
        return _state_guard_fail(
            "error.scene_unsaved",
            "The active scene has not been saved to a .scene file yet. Save it before mutating the scene through MCP.",
            status,
            explain,
            next_tools=["scene_save", "scene_status"],
        )
    return None


def _state_guard_fail(
    code: str,
    message: str,
    status: dict[str, Any],
    explain: dict[str, Any],
    *,
    next_tools: list[str],
) -> dict[str, Any]:
    result = fail(
        code,
        message,
        hint="Inspect data.status and call the suggested next tools before retrying.",
        explain={
            **explain,
            "recovery": [
                "Call scene_status to inspect the active scene path and dirty flag.",
                "If the scene is unsaved, call scene_save with data.suggested_save_path or another path under Assets/.",
                "If Play Mode is active, call editor_stop before editor-scene mutations.",
                "If scene loading is pending, wait before retrying.",
                "Retry the original MCP operation after the save succeeds.",
            ],
            "next_suggested_tools": next_tools,
        },
    )
    result["data"] = {"status": status, **status}
    return result


def _is_edit_mode_mutation(name: str) -> bool:
    return _requires_saved_scene_file(name) or name in {
        "scene_open",
        "scene_new",
        "scene_discard",
        "scene_save",
    }


def _requires_saved_scene_file(name: str) -> bool:
    exact = {
        "hierarchy_create_object",
        "gameobject_add_component",
        "gameobject_delete",
        "gameobject_batch_delete",
        "gameobject_batch_create",
        "gameobject_duplicate",
        "gameobject_set",
        "gameobject_set_parent",
        "gameobject_set_sibling_index",
        "gameobject_ensure_path",
        "gameobject_clone_from_json",
        "gameobject_create_from_model",
        "gameobject_set_tag_layer",
        "transform_set",
        "component_ensure",
        "component_set_field",
        "component_set_fields",
        "component_remove",
        "component_restore_snapshot",
        "camera_ensure_main",
        "camera_set_main",
        "camera_attach_to_target",
        "camera_setup_third_person",
        "camera_setup_2d_card_game",
        "camera_frame_targets",
        "camera_look_at",
        "lighting_ensure_default",
        "renderstack_find_or_create",
        "renderstack_set_pipeline",
        "renderstack_add_effect",
        "renderstack_remove_effect",
        "renderstack_set_effect_enabled",
        "scene_clear_generated",
    }
    if name in exact:
        return True
    return name.startswith(("ui_", "ui.")) and name not in {"ui_inspect", "ui_find_by_text"}


def scene_status() -> dict[str, Any]:
    path = ""
    dirty = False
    loading = False
    scene_name = ""
    suggested = ""
    play_state = "edit"
    try:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm is not None:
            path = str(getattr(sfm, "current_scene_path", "") or "")
            dirty = bool(getattr(sfm, "is_dirty", False))
            loading = bool(getattr(sfm, "is_loading", False))
            if not path:
                default_path = getattr(sfm, "_default_scene_save_path", lambda: None)()
                suggested = _project_rel(default_path) if default_path else ""
        try:
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            play_state = getattr(getattr(pmm, "state", None), "name", "edit").lower() if pmm else "edit"
        except Exception:
            play_state = "edit"
    except Exception:
        pass
    try:
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        scene_name = str(getattr(scene, "name", "") if scene else "")
    except Exception:
        pass
    return {
        "scene": scene_name,
        "path": _project_rel(path) if path else "",
        "absolute_path": path,
        "dirty": dirty,
        "loading": loading,
        "play_state": play_state,
        "saved_to_file": bool(path),
        "suggested_save_path": suggested,
        "requires_save_before_mcp_mutation": not bool(path),
    }


def ensure_not_active_scene_file(project_path: str, file_path: str, operation: str) -> None:
    """Prevent generic asset tools from editing scene files outside scene APIs."""
    target = resolved_path(file_path)
    if _path_is_or_contains_scene_file(target):
        raise ValueError(
            f"Refusing to {operation} .scene files through generic asset tools. "
            "Use scene_save for the active scene, scene_open to switch scenes, or close the scene before file-level scene maintenance."
        )
    try:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        active = resolved_path(str(getattr(sfm, "current_scene_path", "") or "")) if sfm else ""
    except Exception:
        active = ""
    if not active:
        return
    is_active_scene = same_path(target, active)
    contains_active_scene = os.path.isdir(target) and is_path_within(active, target)
    if is_active_scene or contains_active_scene:
        raise ValueError(
            f"Refusing to {operation} the active scene file through generic asset tools. "
            "Use scene_save for the active scene, or save/close the scene before replacing its file."
        )


def _path_is_or_contains_scene_file(path: str) -> bool:
    if path.lower().endswith(".scene"):
        return True
    if os.path.isdir(path):
        try:
            for base, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
                if any(name.lower().endswith(".scene") for name in files):
                    return True
        except Exception:
            return False
    return False


def _project_rel(path: str | None) -> str:
    if not path:
        return ""
    try:
        from Infernux.engine.project_context import get_project_root
        root = get_project_root()
        if root:
            return relative_path(path, root, allow_root=True)
    except Exception:
        pass
    return str(path)


def register_and_tool(mcp, name: str, **metadata: Any) -> Callable:
    """Register metadata and return mcp.tool for future tools."""
    register_tool_metadata(name, **metadata)
    return mcp.tool(name=name)


def get_asset_database():
    try:
        from Infernux.lib import AssetRegistry
        registry = AssetRegistry.instance()
        if registry:
            return registry.get_asset_database()
    except Exception:
        return None
    return None


def project_assets_dir(project_path: str) -> str:
    return os.path.join(resolved_path(project_path), "Assets")


def resolve_project_dir(project_path: str, directory: str | None) -> str:
    root = resolved_path(project_path)
    if not directory:
        return project_assets_dir(root)
    raw = resolved_path(directory if os.path.isabs(directory) else os.path.join(root, directory))
    if not is_path_within(raw, root):
        raise ValueError("Target directory must stay inside the project.")
    return raw


def resolve_project_path(project_path: str, path: str | None, *, default: str = "") -> str:
    """Resolve a project-relative or absolute path and keep it inside the project."""
    root = resolved_path(project_path)
    candidate = path or default
    if not candidate:
        candidate = root
    raw = resolved_path(candidate if os.path.isabs(candidate) else os.path.join(root, candidate))
    if not is_path_within(raw, root):
        raise ValueError("Path must stay inside the project.")
    return raw


def resolve_asset_path(project_path: str, path: str | None, *, default_name: str = "") -> str:
    """Resolve a path under the project Assets directory."""
    root = resolved_path(project_path)
    assets = project_assets_dir(root)
    candidate = path or default_name
    if not candidate:
        return assets
    raw = resolved_path(candidate if os.path.isabs(candidate) else os.path.join(root, candidate))
    if not is_path_within(raw, assets):
        raise ValueError("Asset path must stay inside Assets/.")
    return raw


def is_document_registry_managed_path(path: str) -> bool:
    """Return whether a live editor document owns *path* or an ancestor."""
    target = resolved_path(path)
    try:
        from Infernux.engine.interaction import DocumentRegistry
    except ImportError:
        return False
    registry = DocumentRegistry._instance
    if registry is None:
        return False
    for document in registry.documents:
        owner = str(getattr(document, "resource_path", "") or "").strip()
        if not owner:
            continue
        owner = resolved_path(owner)
        if same_path(target, owner) or is_path_within(target, owner):
            return True
    return False


def require_external_source_edit_path(
    project_path: str,
    path: str,
    operation: str = "edit",
) -> str:
    """Validate one generic MCP text target as externally authored source."""
    target = resolve_project_path(project_path, path)
    extension = os.path.splitext(target)[1].lower()
    filename = os.path.basename(target).lower()
    display = relative_path(target, project_path, allow_root=True)
    project_relative_parts = tuple(
        part.casefold()
        for part in display.replace("\\", "/").split("/")
        if part
    )

    if (
        project_relative_parts
        and project_relative_parts[0] in EDITOR_OWNED_PROJECT_DIRECTORIES
    ):
        raise ValueError(
            f"Refusing to {operation} Editor-owned project data '{display}' through generic MCP file editing. "
            "Use the corresponding Project Settings, asset, or MCP transaction service."
        )
    if extension in EDITOR_OWNED_ASSET_EXTENSIONS:
        raise ValueError(
            f"Refusing to {operation} Editor-owned asset '{display}' through generic MCP file editing. "
            "Use the asset's editor/document/controller tool so in-memory state, dirty revisions, and Undo remain consistent."
        )
    if (
        extension not in EXTERNAL_SOURCE_TEXT_EXTENSIONS
        and filename not in EXTERNAL_SOURCE_TEXT_FILENAMES
    ):
        raise ValueError(
            f"Refusing to {operation} '{display}' through generic MCP file editing. "
            "Only explicit external source and text formats are supported; use the owning asset tool for imported or editor-authored resources."
        )
    if is_document_registry_managed_path(target):
        raise ValueError(
            f"Refusing to {operation} '{display}' because it is managed by an open Editor document. "
            "Save or edit it through that document's controller."
        )
    return target


def require_existing_parent_directory(project_path: str, path: str) -> str:
    """Reject implicit directory creation by generic external file editors."""
    target = resolve_project_path(project_path, path)
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        display = relative_path(parent, project_path, allow_root=True)
        raise FileNotFoundError(
            f"Parent directory does not exist: {display}. Create it first with asset_ensure_folder."
        )
    return parent


def notify_asset_changed(path: str, action: str = "modified") -> None:
    """Commit one external file mutation through the canonical asset pipeline."""
    adb = get_asset_database()
    if adb is None:
        raise RuntimeError("AssetDatabase is not available")
    from Infernux.core.assets import AssetManager

    if action == "deleted":
        if not AssetManager.delete_asset(path, database=adb):
            raise RuntimeError(f"AssetDatabase failed to delete '{path}'")
        return
    if action not in {"created", "modified"}:
        raise ValueError(f"unknown asset change action: {action!r}")

    if adb.contains_path(path):
        if not AssetManager.reimport_asset(path, database=adb):
            raise RuntimeError(f"AssetDatabase failed to reimport '{path}'")
    else:
        AssetManager.import_asset(path, database=adb)


def track_project_path_before_change(project_path: str, path: str, operation: str = "modify") -> bool:
    """Snapshot only externally authored source files for MCP rollback."""
    if str(operation or "") not in EXTERNAL_SOURCE_TRANSACTION_OPERATIONS:
        return False
    try:
        target = require_external_source_edit_path(project_path, path, "snapshot")
    except (OSError, ValueError):
        return False
    try:
        from Infernux.mcp.capabilities import feature_enabled
        if not feature_enabled("transactions"):
            return False
        from Infernux.mcp.project_tools.transactions import record_path_before_change
        record_path_before_change(project_path, target, operation=operation)
        return True
    except Exception:
        return False


def write_external_source_text(
    project_path: str,
    path: str,
    text: str,
    *,
    overwrite: bool = True,
    operation: str = "write_text",
) -> dict[str, Any]:
    """Write one externally authored source file through the canonical path.

    Callers must already be executing on the Editor main thread. This is the
    sole MCP write primitive for scripts and other external source text.
    """
    file_path = require_external_source_edit_path(project_path, path, "write")
    existed = os.path.exists(file_path)
    if existed and not overwrite:
        raise FileExistsError(f"File already exists: {path}")
    require_existing_parent_directory(project_path, file_path)
    track_project_path_before_change(project_path, file_path, operation)
    from Infernux.core.document_store import write_document_text

    write_document_text(file_path, text or "")
    notify_asset_changed(file_path, "modified" if existed else "created")
    return {
        "path": relative_path(file_path, project_path),
        "bytes": os.path.getsize(file_path),
        "created": not existed,
    }


def serialize_vector(value) -> Any:
    if value is None:
        return None
    # Reference wrappers intentionally proxy arbitrary attribute reads to
    # their target and can therefore look structurally like a vector while
    # unresolved. Only accept a shape when every coordinate is numeric.
    for shape in (("x", "y", "z", "w"), ("x", "y", "z"), ("x", "y")):
        try:
            coordinates = [getattr(value, attr) for attr in shape]
        except (AttributeError, ReferenceError, RuntimeError):
            continue
        if all(isinstance(coordinate, (bool, int, float)) for coordinate in coordinates):
            return [float(coordinate) for coordinate in coordinates]
    return value


def coerce_vector3(value):
    from Infernux.lib import Vector3
    if isinstance(value, dict):
        return Vector3(float(value.get("x", 0.0)), float(value.get("y", 0.0)), float(value.get("z", 0.0)))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return Vector3(float(value[0]), float(value[1]), float(value[2]))
    raise ValueError("Expected Vector3 as [x, y, z] or {x, y, z}.")


def serialize_value(value: Any) -> Any:
    """Convert editor/Python/C++ values to JSON-friendly data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    vector = serialize_vector(value)
    if vector is not value:
        return vector
    if hasattr(value, "name") and hasattr(value, "value") and type(value).__name__ != "GameObject":
        try:
            return {"name": str(value.name), "value": int(value.value)}
        except Exception:
            return str(value)
    if hasattr(value, "id") and hasattr(value, "name"):
        try:
            return {"id": int(value.id), "name": str(value.name), "type": type(value).__name__}
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_value(v) for v in value]
    return str(value)


def serialize_component(comp) -> dict[str, Any]:
    type_name = str(getattr(comp, "type_name", type(comp).__name__))
    is_python_component = (
        hasattr(comp, "_script_guid")
        and not bool(getattr(comp, "_is_builtin_component_wrapper", False))
    )
    data: dict[str, Any] = {
        "type": type_name,
        "python": bool(is_python_component),
    }
    component_id = getattr(comp, "component_id", None)
    if component_id:
        data["component_id"] = int(component_id)
    script_guid = getattr(comp, "_script_guid", "") if is_python_component else ""
    if script_guid:
        data["script_guid"] = script_guid
    if bool(getattr(comp, "_is_broken", False)):
        data["broken_script"] = True
        data["broken_error"] = str(getattr(comp, "_broken_error", "") or "Script failed to load")
    return data


def find_game_object(object_id: int):
    from Infernux.lib import SceneManager
    scene_manager = SceneManager.instance()
    scene = scene_manager.get_active_scene()
    if not scene:
        raise RuntimeError("No active scene.")
    obj = scene_manager.find_runtime_object_by_id(int(object_id))
    if obj is None:
        raise FileNotFoundError(f"GameObject {object_id} was not found.")
    return obj
