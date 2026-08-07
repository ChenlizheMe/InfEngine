"""build_settings_panel — Build Settings window with scene list and platform config."""

from __future__ import annotations

from typing import List, Optional

from Infernux.engine.ui.editor_panel import FloatingEditorPanel


BUILD_SETTINGS_FILE: str
"""Default filename for build settings (``BuildSettings.json``)."""

DRAG_DROP_SCENE: str
DRAG_DROP_REORDER: str


def load_build_settings() -> dict:
    """Load the project's build settings from disk.

    Returns:
        Parsed JSON dict, or empty dict if the file doesn't exist.
    """
    ...

class BuildSettingsPanel(FloatingEditorPanel):
    """Build Settings utility surface with scene list and platform config."""

    def __init__(self) -> None: ...
    def on_enable(self) -> None: ...

    def get_scene_list(self) -> List[str]:
        """Return ordered list of scene paths included in the build."""
        ...

    def on_render_content(self, ctx: object) -> None: ...
