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
    data.pop("additional_cook_roots", None)
    return data


def load_build_settings_for_build(project_path: Optional[str] = None) -> dict:
    """Load and validate the authoritative settings document for a Player build.

    Editor/runtime discovery may use :func:`load_build_settings` while a project
    is being created.  A build is different: silently replacing a missing or
    malformed project document changes the produced Player.  Build callers
    therefore use this strict entry point and receive a precise failure.
    """

    path = build_settings_path(project_path)
    if not path:
        raise ValueError("Player build requires an explicit project root")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Player build settings are missing: {path}"
        )
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ValueError(
            f"Player build settings are unreadable: {path}: {error}"
        ) from error

    from Infernux.engine.interaction.project_settings import (
        normalize_build_settings,
    )

    try:
        return normalize_build_settings(data)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Player build settings are invalid: {path}: {error}"
        ) from error
