"""SceneViewPanel — 3D viewport with camera controls and gizmos."""

from __future__ import annotations

from typing import Callable

from Infernux.lib import InxGUIContext
from Infernux.engine.ui.editor_panel import EditorPanel


class SceneViewPanel(EditorPanel):
    """3D scene viewport with orbit camera, transform gizmos, and picking."""

    WINDOW_TYPE_ID: str
    WINDOW_DISPLAY_NAME: str

    KEY_W: int
    KEY_A: int
    KEY_S: int
    KEY_D: int
    KEY_Q: int
    KEY_E: int
    KEY_R: int
    KEY_LEFT_SHIFT: int
    KEY_RIGHT_SHIFT: int

    def __init__(self, title: str = "Scene", engine: object = None) -> None: ...

    def set_engine(self, engine: object) -> None: ...
    def set_play_mode_manager(self, manager: object) -> None: ...
    def set_on_object_picked(self, callback: Callable) -> None: ...
    def reset_camera(self) -> None:
        """Reset camera to the default position and orientation."""
        ...

    def focus_on(self, x: float, y: float, z: float, distance: float = 10.0) -> None:
        """Smoothly fly the camera to look at position ``(x, y, z)``.

        Args:
            x: World X coordinate.
            y: World Y coordinate.
            z: World Z coordinate.
            distance: Camera distance from the target.
        """
        ...

    def can_frame_object_by_id(self, object_id: int) -> bool: ...
    def frame_object_by_id(self, object_id: int) -> bool: ...
    def fly_to_object(self, game_object: object) -> bool:
        """Smoothly fly the camera to frame *game_object*."""
        ...

    def on_disable(self) -> None: ...
    def on_render_content(self, ctx: InxGUIContext) -> None: ...
