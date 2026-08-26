"""Lazy exports for editor UI modules.

Importing a leaf module such as ``Infernux.engine.ui.theme`` must not construct
the entire editor panel graph. Besides startup cost, eager aggregation creates
cycles with the runtime ``Infernux.ui`` package.
"""

from __future__ import annotations

import importlib
import os

_EXPORTS = {
    "MenuBarPanel": ("Infernux.lib", "MenuBarPanel"),
    "ToolbarPanel": ("Infernux.lib", "ToolbarPanel"),
    "HierarchyPanel": ("Infernux.lib", "HierarchyPanel"),
    "InspectorPanel": ("Infernux.lib", "InspectorPanel"),
    "ConsolePanel": ("Infernux.lib", "ConsolePanel"),
    "ProjectPanel": ("Infernux.lib", "ProjectPanel"),
    "StatusBarPanel": ("Infernux.lib", "StatusBarPanel"),
    "ClosablePanel": (".closable_panel", "ClosablePanel"),
    "SceneViewPanel": (".scene_view_panel", "SceneViewPanel"),
    "GameViewPanel": (".game_view_panel", "GameViewPanel"),
    "WindowManager": (".window_manager", "WindowManager"),
    "WindowInfo": (".window_manager", "WindowInfo"),
    "TagLayerSettingsPanel": (".tag_layer_settings", "TagLayerSettingsPanel"),
    "PhysicsLayerMatrixPanel": (".tag_layer_settings", "PhysicsLayerMatrixPanel"),
    "EngineStatus": (".engine_status", "EngineStatus"),
    "BuildSettingsPanel": (".build_settings_panel", "BuildSettingsPanel"),
    "PreferencesPanel": (".preferences_panel", "PreferencesPanel"),
    "HistoryPanel": (".history_panel", "HistoryPanel"),
    "PluginPanel": (".plugin_panel", "PluginPanel"),
    "InxPackageImportPanel": (".plugin_panel", "InxPackageImportPanel"),
    "EnvironmentSettingsPanel": (".environment_settings_panel", "EnvironmentSettingsPanel"),
    "ViewportInfo": (".viewport_utils", "ViewportInfo"),
    "capture_viewport_info": (".viewport_utils", "capture_viewport_info"),
    "UIEditorPanel": (".ui_editor_panel", "UIEditorPanel"),
    "AnimClip2DEditorPanel": (".animclip2d_editor_panel", "AnimClip2DEditorPanel"),
    "AnimFSMEditorPanel": (".animfsm_editor_panel", "AnimFSMEditorPanel"),
    "AnimTimelineEditorPanel": (".animtimeline_editor_panel", "AnimTimelineEditorPanel"),
    "ParticleGraphEditorPanel": (".particle_graph_editor_panel", "ParticleGraphEditorPanel"),
    "EditorPanel": (".editor_panel", "EditorPanel"),
    "FloatingEditorPanel": (".editor_panel", "FloatingEditorPanel"),
    "EditorServices": (".editor_services", "EditorServices"),
    "PanelRegistry": (".panel_registry", "PanelRegistry"),
    "editor_panel": (".panel_registry", "editor_panel"),
    "EditorWindow": (".editor_window", "EditorWindow"),
    "editor_window": (".editor_window", "editor_window"),
    "ModalPortal": (".modal_portal", "ModalPortal"),
}

__all__ = [] if os.environ.get("_INFERNUX_PLAYER_MODE") else list(_EXPORTS)


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attribute)
    globals()[name] = value
    return value
