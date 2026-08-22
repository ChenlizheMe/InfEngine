from __future__ import annotations

from typing import Any, Callable, Optional

from Infernux.lib import InxGUIRenderable, InxGUIContext, TextureLoader, TextureData
from Infernux.engine.engine import Engine, LogLevel
from Infernux.engine.headless import run_headless
from Infernux.engine.resources_manager import ResourcesManager
from Infernux.engine.play_mode import PlayModeManager, PlayModeState
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.ui import (
    MenuBarPanel,
    ToolbarPanel,
    HierarchyPanel,
    InspectorPanel,
    ConsolePanel,
    SceneViewPanel,
    GameViewPanel,
    ProjectPanel,
    WindowManager,
    TagLayerSettingsPanel,
    StatusBarPanel,
    BuildSettingsPanel,
    UIEditorPanel,
    EditorPanel,
    EditorServices,
    PanelRegistry,
    editor_panel,
)


def release_engine(project_path: str, engine_log_level: LogLevel = ...) -> None:
    """Launch Infernux with Unity-style editor layout.

    Args:
        project_path: Absolute path to the project directory.
        engine_log_level: Logging verbosity for the native engine.
    """
    ...

def run_player(project_path: str, engine_log_level: LogLevel = ...) -> None:
    """Launch Infernux in standalone player mode (no editor chrome).

    Opens the project's first scene from BuildSettings.json, applies the
    display mode from BuildManifest.json (fullscreen borderless or windowed
    with a custom resolution), plays the splash sequence if configured, then
    enters play mode and runs until the window is closed.

    Args:
        project_path: Absolute path to the project directory.
        engine_log_level: Logging verbosity for the native engine.
    """
    ...


__all__ = [
    "Engine",
    "LogLevel",
    "InxGUIRenderable",
    "InxGUIContext",
    "MenuBarPanel",
    "ToolbarPanel",
    "HierarchyPanel",
    "InspectorPanel",
    "ConsolePanel",
    "SceneViewPanel",
    "GameViewPanel",
    "UIEditorPanel",
    "ProjectPanel",
    "WindowManager",
    "TagLayerSettingsPanel",
    "StatusBarPanel",
    "PlayModeManager",
    "PlayModeState",
    "SceneFileManager",
    "TextureLoader",
    "TextureData",
    "release_engine",
    "run_player",
    "ResourcesManager",
    "BuildSettingsPanel",
    "EditorPanel",
    "EditorServices",
    "PanelRegistry",
    "editor_panel",
]
