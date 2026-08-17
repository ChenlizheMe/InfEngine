"""Runtime-neutral access to ``ProjectSettings/BuildSettings.json``."""

from __future__ import annotations

import json
import os
from typing import Optional

from Infernux.engine.project_context import get_project_root


BUILD_SETTINGS_FILE = "BuildSettings.json"


def build_settings_path(project_path: Optional[str] = None) -> Optional[str]:
    root = project_path or get_project_root()
    if not root:
        return None
    return os.path.join(root, "ProjectSettings", BUILD_SETTINGS_FILE)


def load_build_settings(project_path: Optional[str] = None) -> dict:
    """Load build settings without importing any editor UI modules."""
    path = build_settings_path(project_path)
    if not path or not os.path.isfile(path):
        return {"scenes": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            data = json.load(stream)
    except (json.JSONDecodeError, OSError, ValueError):
        data = {"scenes": []}
    if not isinstance(data, dict):
        data = {"scenes": []}
    scenes = data.get("scenes")
    if not isinstance(scenes, list):
        data["scenes"] = []
    if "additional_cook_roots" not in data:
        data["additional_cook_roots"] = []
    return data
