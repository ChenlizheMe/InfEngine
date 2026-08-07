"""tag_layer_settings — Tags & Layers editor and physics collision matrix."""

from __future__ import annotations

from Infernux.lib import InxGUIContext
from Infernux.engine.ui.editor_panel import EditorPanel, FloatingEditorPanel


class TagLayerSettingsPanel(EditorPanel):
    """Inspector-style panel for managing project-wide tags and layers.

    Usage::

        panel = TagLayerSettingsPanel()
        panel.set_project_path("C:/MyProject")
        panel.on_render_content(ctx)
    """

    WINDOW_TYPE_ID: str
    WINDOW_DISPLAY_NAME: str

    def __init__(self) -> None: ...
    def set_project_path(self, path: str) -> None: ...
    def on_enable(self) -> None: ...
    def on_render_content(self, ctx: InxGUIContext) -> None: ...


class PhysicsLayerMatrixPanel(FloatingEditorPanel):
    """Project physics settings and collision matrix utility surface."""

    def __init__(self) -> None: ...
    def set_project_path(self, path: str) -> None: ...

    def on_enable(self) -> None: ...
    def on_disable(self) -> None: ...
    def on_render_content(self, ctx: InxGUIContext) -> None: ...
