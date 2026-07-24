"""Project-level MCP tools."""

from __future__ import annotations

import os
import time

from Infernux.engine.path_utils import path_key, relative_path, resolved_path, same_path
from Infernux.mcp.threading import MainThreadCommandQueue
from Infernux.mcp.tools.common import (
    get_asset_database,
    main_thread,
    ok,
    register_tool_metadata,
    resolve_asset_path,
    track_project_path_before_change,
)


def register_project_tools(mcp, project_path: str) -> None:
    @mcp.tool(name="project_info")
    def project_info() -> dict:
        """Return the currently opened project and scene state."""

        def _read():
            from Infernux.engine.play_mode import PlayModeManager
            from Infernux.engine.scene_manager import SceneFileManager
            from Infernux.engine.ui.selection_manager import SelectionManager
            from Infernux.lib import SceneManager

            sfm = SceneFileManager.instance()
            pmm = PlayModeManager.instance()
            sel = SelectionManager.instance()
            scene = SceneManager.instance().get_active_scene()
            return {
                "engine_version": "0.2.9",
                "project_root": project_path,
                "active_scene": {
                    "name": getattr(scene, "name", ""),
                    "path": getattr(sfm, "current_scene_path", "") if sfm else "",
                    "dirty": bool(getattr(sfm, "is_dirty", False)) if sfm else False,
                },
                "play_state": getattr(getattr(pmm, "state", None), "name", "edit").lower() if pmm else "edit",
                "selected_ids": sel.get_ids() if sel else [],
            }

        return main_thread("project_info", _read)

    @mcp.tool(name="project_asset_state")
    def project_asset_state(path: str = "", guid: str = "") -> dict:
        """Read one project asset's disk/meta and AssetDatabase identity state."""
        requested_path = _requested_asset_path(project_path, path)
        requested_guid = str(guid or "").strip()
        if not requested_path and not requested_guid:
            raise ValueError("Provide path or guid.")
        return main_thread(
            "project_asset_state",
            lambda: _read_asset_state(project_path, requested_path, requested_guid),
            arguments={"path": path, "guid": requested_guid},
        )

    @mcp.tool(name="project_wait_for_asset")
    def project_wait_for_asset(
        path: str = "",
        guid: str = "",
        exists: bool = True,
        timeout_seconds: float = 5.0,
        poll_interval: float = 0.05,
    ) -> dict:
        """Wait until disk/meta and AssetDatabase agree on an asset identity."""
        requested_path = _requested_asset_path(project_path, path)
        requested_guid = str(guid or "").strip()
        if not requested_path and not requested_guid:
            raise ValueError("Provide path or guid.")
        timeout = max(0.05, min(float(timeout_seconds), 30.0))
        interval = max(0.01, min(float(poll_interval), 1.0))
        deadline = time.monotonic() + timeout
        polls = 0
        state: dict = {}
        while True:
            polls += 1
            state = MainThreadCommandQueue.instance().run_sync(
                "project_wait_for_asset.poll",
                lambda: _read_asset_state(project_path, requested_path, requested_guid),
                timeout_ms=30000,
            )
            if _asset_expectation_met(state, bool(exists)):
                return ok({**state, "settled": True, "expected_exists": bool(exists), "polls": polls})
            if time.monotonic() >= deadline:
                return ok({**state, "settled": False, "expected_exists": bool(exists), "polls": polls})
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    @mcp.tool(name="project_build_scenes_get")
    def project_build_scenes_get() -> dict:
        """Read the project's ordered standalone build scene list."""
        return main_thread(
            "project_build_scenes_get",
            lambda: _read_build_scenes(project_path),
        )

    @mcp.tool(name="project_build_scenes_set")
    def project_build_scenes_set(scenes: list[str]) -> dict:
        """Atomically replace the ordered standalone build scene list."""
        return main_thread(
            "project_build_scenes_set",
            lambda: _set_build_scenes(project_path, scenes),
            arguments={"scenes": scenes},
        )

    register_tool_metadata(
        "project_asset_state",
        summary="Read project asset existence and AssetDatabase GUID/path identity without reading asset contents.",
        category="project/observation",
        side_effects=[],
        next_suggested_tools=["project_wait_for_asset", "runtime_read_errors"],
        risk_level="low",
    )
    register_tool_metadata(
        "project_wait_for_asset",
        summary="Wait for a Project rename/delete/import to settle across disk, meta, and AssetDatabase.",
        category="project/observation",
        preconditions=["The asset mutation was initiated through normal Editor UI."],
        side_effects=[],
        recovery=["If settled=false, report the exact disk/database mismatch instead of retrying the mutation blindly."],
        next_suggested_tools=["project_asset_state", "runtime_read_errors"],
        risk_level="low",
    )
    register_tool_metadata(
        "project_build_scenes_get",
        summary="Read the ordered Build Settings scene list and report missing entries.",
        category="project/observation",
        side_effects=[],
        recovery=["Use asset_search or project_asset_state if a configured scene is missing."],
        next_suggested_tools=["project_build_scenes_set", "project_asset_state"],
        risk_level="low",
    )
    register_tool_metadata(
        "project_build_scenes_set",
        summary="Replace the ordered Build Settings scene list with existing Assets/*.scene files.",
        category="project/mutation",
        preconditions=["Every scene has already been saved below the current project's Assets directory."],
        side_effects=["Updates ProjectSettings/BuildSettings.json through the canonical document store."],
        recovery=["Call project_build_scenes_get to inspect the committed order; use an MCP transaction to roll back the settings document when needed."],
        next_suggested_tools=["project_build_scenes_get", "runtime_read_errors"],
        risk_level="medium",
    )


def _requested_asset_path(project_path: str, path: str) -> str:
    value = str(path or "").strip()
    return resolve_asset_path(project_path, value) if value else ""


def _read_build_scenes(project_path: str) -> dict:
    from Infernux.engine.ui.build_settings_panel import load_build_settings

    settings = load_build_settings(project_path)
    raw_scenes = settings.get("scenes", [])
    if type(raw_scenes) is not list:
        raise ValueError("BuildSettings scenes must be an array")
    entries = []
    for index, raw_path in enumerate(raw_scenes):
        if type(raw_path) is not str or not raw_path.strip():
            raise ValueError(f"BuildSettings scenes[{index}] must be a non-empty string")
        try:
            absolute_path = resolve_asset_path(project_path, raw_path)
            relative = relative_path(absolute_path, project_path)
            valid_path = absolute_path.lower().endswith(".scene")
        except ValueError:
            absolute_path = resolved_path(raw_path)
            relative = str(raw_path)
            valid_path = False
        entries.append({
            "index": index,
            "path": relative,
            "exists": os.path.isfile(absolute_path),
            "valid_path": valid_path,
        })
    return {
        "scenes": [entry["path"] for entry in entries],
        "entries": entries,
        "startup_scene": entries[0]["path"] if entries else "",
    }


def _set_build_scenes(project_path: str, scenes: list[str]) -> dict:
    if type(scenes) is not list:
        raise ValueError("scenes must be an array of project scene paths")

    absolute_scenes: list[str] = []
    seen: set[str] = set()
    for index, raw_path in enumerate(scenes):
        if type(raw_path) is not str or not raw_path.strip():
            raise ValueError(f"scenes[{index}] must be a non-empty string")
        scene_path = resolve_asset_path(project_path, raw_path)
        if not scene_path.lower().endswith(".scene"):
            raise ValueError(f"scenes[{index}] must reference a .scene asset")
        if not os.path.isfile(scene_path):
            raise FileNotFoundError(f"Build scene does not exist: {relative_path(scene_path, project_path)}")
        key = path_key(scene_path)
        if key in seen:
            raise ValueError(f"Duplicate build scene: {relative_path(scene_path, project_path)}")
        seen.add(key)
        absolute_scenes.append(scene_path)

    from Infernux.engine.ui.build_settings_panel import load_build_settings, save_build_settings

    settings_path = os.path.join(project_path, "ProjectSettings", "BuildSettings.json")
    settings = load_build_settings(project_path)
    settings["scenes"] = [relative_path(path, project_path) for path in absolute_scenes]
    track_project_path_before_change(project_path, settings_path, "set_build_scenes")
    save_build_settings(settings, project_path)
    return _read_build_scenes(project_path)


def _read_asset_state(project_path: str, requested_path: str, requested_guid: str) -> dict:
    database = get_asset_database()
    if database is None:
        raise RuntimeError("AssetDatabase is not available.")

    database_path = str(database.get_path_from_guid(requested_guid) or "") if requested_guid else ""
    effective_path = requested_path or database_path
    database_guid = str(database.get_guid_from_path(effective_path) or "") if effective_path else ""
    mapping_consistent = bool(effective_path and database_guid)
    if requested_guid:
        mapping_consistent = mapping_consistent and database_guid == requested_guid
    if requested_path and requested_guid:
        mapping_consistent = mapping_consistent and _same_path(requested_path, database_path)

    database_relative_path = _relative_project_path(project_path, database_path)
    if requested_path and _same_path(requested_path, database_path):
        database_relative_path = _relative_project_path(project_path, requested_path)

    path_is_file = bool(effective_path and os.path.isfile(effective_path))
    path_is_directory = bool(effective_path and os.path.isdir(effective_path))
    return {
        "requested_path": _relative_project_path(project_path, requested_path),
        "requested_guid": requested_guid,
        "path_exists": path_is_file or path_is_directory,
        "path_kind": "file" if path_is_file else "directory" if path_is_directory else "missing",
        "meta_exists": bool(effective_path and os.path.isfile(effective_path + ".meta")),
        "database_contains_path": bool(effective_path and database.contains_path(effective_path)),
        "database_contains_guid": bool(requested_guid and database.contains_guid(requested_guid)),
        "database_guid": database_guid,
        "database_path": database_relative_path,
        "mapping_consistent": mapping_consistent,
        "refresh_pending": bool(database.refresh_pending),
        "query_generation": int(database.query_generation),
    }


def _asset_expectation_met(state: dict, expected_exists: bool) -> bool:
    if bool(state.get("refresh_pending")):
        return False
    if expected_exists:
        if state.get("path_kind") == "directory":
            return bool(state.get("path_exists") and not state.get("requested_guid"))
        return bool(
            state.get("path_exists")
            and state.get("meta_exists")
            and state.get("database_contains_path")
            and state.get("mapping_consistent")
        )
    return not bool(
        state.get("path_exists")
        or state.get("meta_exists")
        or state.get("database_contains_path")
        or state.get("database_contains_guid")
    )


def _relative_project_path(project_path: str, path: str) -> str:
    if not path:
        return ""
    try:
        return relative_path(path, project_path, allow_root=True)
    except ValueError:
        return ""


def _normalize(path: str) -> str:
    return path_key(path) if path else ""


def _same_path(first: str, second: str) -> bool:
    if not first or not second:
        return False
    return same_path(first, second)
