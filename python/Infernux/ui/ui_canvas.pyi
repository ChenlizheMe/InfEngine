"""Type stubs for Infernux.ui.ui_canvas — root container for screen-space UI."""

from __future__ import annotations

from typing import Iterator, List, Optional, Tuple

from Infernux.ui.inx_ui_component import InxUIComponent
from Infernux.ui.inx_ui_screen_component import InxUIScreenComponent
from Infernux.ui.enums import RenderMode, ScreenMatchMode, UIScaleMode


class UICanvas(InxUIComponent):
    """Screen-space UI canvas — root container for all UI elements.

    Defines a *design* reference resolution (default 1920x1080).  At runtime
    the Game View scales from design resolution to actual viewport size so
    that all positions, sizes and font sizes adapt proportionally.

    Attributes:
        render_mode: ``ScreenOverlay`` or ``CameraOverlay``.
        sort_order: Rendering order (lower draws first).
        target_camera_id: Camera GameObject ID (CameraOverlay mode only).
        reference_width: Design reference width in pixels (default 1920).
        reference_height: Design reference height in pixels (default 1080).
        ui_scale_mode: Canvas scaler mode.
        screen_match_mode: Width/height match behavior for ScaleWithScreenSize.
        match_width_or_height: 0 = match width, 1 = match height.
        pixel_perfect: Round scale to integer pixels when possible.
        reference_pixels_per_unit: Sprite pixel density hint.
    """

    render_mode: RenderMode
    sort_order: int
    target_camera_id: int
    reference_width: int
    reference_height: int
    ui_scale_mode: UIScaleMode
    screen_match_mode: ScreenMatchMode
    match_width_or_height: float
    pixel_perfect: bool
    reference_pixels_per_unit: float

    def compute_scale(self, screen_w: float, screen_h: float) -> Tuple[float, float, float]:
        """Compute ``(scale_x, scale_y, text_scale)`` for a viewport size.

        Mirrors the supported Unity CanvasScaler modes. Agents should use this
        when converting design-space UI coordinates to Game View pixels.
        """
        ...

    def compute_logical_size(self, screen_w: float, screen_h: float) -> Tuple[float, float]:
        """Return the live logical Canvas extent for the viewport."""
        ...

    def invalidate_element_cache(self) -> None:
        """Mark the cached element list as stale.

        Called automatically when ``structure_version`` changes.
        Also call manually after hierarchy changes (add/remove children).
        """
        ...

    def iter_ui_elements(self) -> Iterator[InxUIScreenComponent]:
        """Yield all screen-space UI components on child GameObjects (depth-first)."""
        ...

    def raycast(self, canvas_x: float, canvas_y: float, tolerance: float = ...) -> Optional[InxUIScreenComponent]:
        """Return the front-most element hit at ``(canvas_x, canvas_y)``, or ``None``.

        Iterates children in reverse depth-first order (last drawn = top).
        Only elements with ``raycast_target = True`` participate.

        Args:
            canvas_x: X coordinate in canvas design pixels.
            canvas_y: Y coordinate in canvas design pixels.
        """
        ...

    def raycast_all(self, canvas_x: float, canvas_y: float, tolerance: float = ...) -> List[InxUIScreenComponent]:
        """Return all elements hit at the given point, front-to-back order.

        Args:
            canvas_x: X coordinate in canvas design pixels.
            canvas_y: Y coordinate in canvas design pixels.
        """
        ...
