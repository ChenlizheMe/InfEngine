"""Infernux Editor UI — panels, managers, and framework.

Re-exports all editor panel classes and the panel framework.
Skipped entirely in standalone player builds.
"""

from __future__ import annotations

from Infernux.lib import MenuBarPanel as MenuBarPanel
from Infernux.engine.ui.closable_panel import ClosablePanel as ClosablePanel
from Infernux.lib import HierarchyPanel as HierarchyPanel
from Infernux.lib import InspectorPanel as InspectorPanel
from Infernux.lib import ConsolePanel as ConsolePanel
from Infernux.engine.ui.scene_view_panel import SceneViewPanel as SceneViewPanel
from Infernux.engine.ui.game_view_panel import GameViewPanel as GameViewPanel
from Infernux.lib import ProjectPanel as ProjectPanel
from Infernux.engine.ui.window_manager import WindowManager as WindowManager, WindowInfo as WindowInfo
from Infernux.lib import ToolbarPanel as ToolbarPanel
from Infernux.engine.ui.tag_layer_settings import TagLayerSettingsPanel as TagLayerSettingsPanel, PhysicsLayerMatrixPanel as PhysicsLayerMatrixPanel
from Infernux.lib import StatusBarPanel as StatusBarPanel
from Infernux.engine.ui.engine_status import EngineStatus as EngineStatus
from Infernux.engine.ui.build_settings_panel import BuildSettingsPanel as BuildSettingsPanel
from Infernux.engine.ui.preferences_panel import PreferencesPanel as PreferencesPanel
from Infernux.engine.ui.history_panel import HistoryPanel as HistoryPanel
from Infernux.engine.ui.environment_settings_panel import EnvironmentSettingsPanel as EnvironmentSettingsPanel
from Infernux.engine.ui.viewport_utils import ViewportInfo as ViewportInfo, capture_viewport_info as capture_viewport_info
from Infernux.engine.ui.ui_editor_panel import UIEditorPanel as UIEditorPanel
from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel as ParticleGraphEditorPanel
from Infernux.engine.ui.editor_panel import EditorPanel as EditorPanel, FloatingEditorPanel as FloatingEditorPanel
from Infernux.engine.ui.editor_window import EditorWindow as EditorWindow, editor_window as editor_window
from Infernux.engine.ui.editor_services import EditorServices as EditorServices
from Infernux.engine.ui.panel_registry import PanelRegistry as PanelRegistry, editor_panel as editor_panel
from Infernux.engine.ui.modal_portal import ModalPortal as ModalPortal
from Infernux.engine.ui import panel_state as panel_state

__all__ = [
    "MenuBarPanel",
    "ToolbarPanel",
    "HierarchyPanel",
    "InspectorPanel",
    "ConsolePanel",
    "SceneViewPanel",
    "GameViewPanel",
    "ProjectPanel",
    "ClosablePanel",
    "WindowManager",
    "WindowInfo",
    "TagLayerSettingsPanel",
    "PhysicsLayerMatrixPanel",
    "StatusBarPanel",
    "EngineStatus",
    "BuildSettingsPanel",
    "PreferencesPanel",
    "HistoryPanel",
    "EnvironmentSettingsPanel",
    "ViewportInfo",
    "capture_viewport_info",
    "UIEditorPanel",
    "ParticleGraphEditorPanel",
    "EditorPanel",
    "FloatingEditorPanel",
    "EditorWindow",
    "EditorServices",
    "PanelRegistry",
    "editor_panel",
    "editor_window",
    "ModalPortal",
]
