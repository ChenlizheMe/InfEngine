"""UICanvas — root container for screen-space UI elements.

A UICanvas is attached to a GameObject in the Hierarchy.
All UI elements (UIText, etc.) are children of the Canvas's GameObject.
The Canvas itself only stores configuration; rendering is handled by
the UI Editor panel & Game View overlay via ImGui draw primitives.

The canvas defines a *design* reference resolution (default 1920×1080).
At runtime the Game View scales from design resolution to actual viewport
size so that all positions, sizes and font sizes adapt proportionally.

Hierarchy:
    InxComponent → InxUIComponent → UICanvas
"""

import math

from Infernux.components import (
    disallow_multiple,
    add_component_menu,
    serialized_field,
    int_field,
)
from .inx_ui_component import InxUIComponent
from .enums import RenderMode, UIScaleMode, ScreenMatchMode
from .ui_render_revision import is_unchanged_ui_scalar, mark_runtime_ui_dirty


def _log2(x: float) -> float:
    return math.log2(max(x, 1e-6))


def _pow2(x: float) -> float:
    return 2.0 ** x


@disallow_multiple
@add_component_menu("UI/Canvas")
class UICanvas(InxUIComponent):
    """Screen-space UI canvas.

    reference_width / reference_height are the *design* reference resolution.
    They are user-editable and default to 1920×1080.  At runtime the Game
    View overlay scales all element positions, sizes and font sizes
    proportionally from this reference to the actual viewport.

    Attributes:
        render_mode: ScreenOverlay or CameraOverlay.
        sort_order: Rendering order (lower draws first).
        target_camera_id: Camera GameObject ID (CameraOverlay mode only).
    """

    render_mode: RenderMode = serialized_field(default=RenderMode.ScreenOverlay)
    sort_order: int = int_field(0, range=(-1000, 1000), tooltip="Render order (lower = earlier)")
    target_camera_id: int = int_field(0, tooltip="Camera ID for CameraOverlay mode")

    # Design reference resolution (serialized, user-editable)
    reference_width: int = int_field(1920, range=(1, 8192), tooltip="Design reference width", slider=False)
    reference_height: int = int_field(1080, range=(1, 8192), tooltip="Design reference height", slider=False)

    # ── Canvas Scaler (Unity-aligned) ──
    ui_scale_mode: UIScaleMode = serialized_field(
        default=UIScaleMode.ScaleWithScreenSize,
        tooltip="How the canvas scales UI elements",
    )
    screen_match_mode: ScreenMatchMode = serialized_field(
        default=ScreenMatchMode.MatchWidthOrHeight,
        tooltip="How to blend width/height matching when using ScaleWithScreenSize",
    )
    match_width_or_height: float = serialized_field(
        default=0.5, range=(0.0, 1.0),
        tooltip="0 = match width, 1 = match height, 0.5 = blend both",
        slider=True,
    )
    pixel_perfect: bool = serialized_field(
        default=False,
        tooltip="Round positions to nearest pixel for crisp rendering",
    )
    reference_pixels_per_unit: float = serialized_field(
        default=100.0, range=(1.0, 10000.0),
        tooltip="Pixels per unit for sprites in the canvas",
        slider=False,
    )

    def __setattr__(self, name, value):
        unchanged = is_unchanged_ui_scalar(self, name, value)
        super().__setattr__(name, value)
        if not name.startswith("_") and not unchanged:
            mark_runtime_ui_dirty()

    # ------------------------------------------------------------------
    # Scaling helpers (Unity CanvasScaler-aligned)
    # ------------------------------------------------------------------

    def compute_scale(self, screen_w: float, screen_h: float) -> tuple:
        """Compute (scale_x, scale_y, text_scale) for the given screen size.

        Mirrors Unity's CanvasScaler behaviour for the three supported
        ``UIScaleMode`` values.

        Returns:
            (scale_x, scale_y, text_scale) where text_scale is used for
            proportional font-size scaling.
        """
        ref_w = max(1.0, float(self.reference_width))
        ref_h = max(1.0, float(self.reference_height))
        if screen_w < 1 or screen_h < 1:
            return 1.0, 1.0, 1.0

        mode = self.ui_scale_mode

        if mode == UIScaleMode.ConstantPixelSize:
            return 1.0, 1.0, 1.0

        if mode == UIScaleMode.ConstantPhysicalSize:
            # Future: factor in DPI. For now, same as ConstantPixelSize.
            return 1.0, 1.0, 1.0

        # ScaleWithScreenSize
        log_w = _log2(screen_w / ref_w) if ref_w > 0 else 0.0
        log_h = _log2(screen_h / ref_h) if ref_h > 0 else 0.0

        match_mode = self.screen_match_mode

        if match_mode == ScreenMatchMode.MatchWidthOrHeight:
            match_val = max(0.0, min(1.0, float(self.match_width_or_height)))
            log_blend = log_w * (1.0 - match_val) + log_h * match_val
            scale = _pow2(log_blend)
        elif match_mode == ScreenMatchMode.Expand:
            scale = min(screen_w / ref_w, screen_h / ref_h)
        elif match_mode == ScreenMatchMode.Shrink:
            scale = max(screen_w / ref_w, screen_h / ref_h)
        else:
            scale = min(screen_w / ref_w, screen_h / ref_h)

        scale_x = scale
        scale_y = scale

        if self.pixel_perfect:
            scale_x = max(1.0, round(scale_x))
            scale_y = max(1.0, round(scale_y))

        return scale_x, scale_y, min(scale_x, scale_y)

    # ------------------------------------------------------------------
    # Cached element list (invalidated when hierarchy changes)
    # ------------------------------------------------------------------
    _cached_elements: list = None
    _cached_elements_version: int = -1

    def invalidate_element_cache(self):
        """Mark the cached element list as stale.

        Called automatically when structure_version changes.
        Also call manually after hierarchy changes (add/remove children).
        """
        self._cached_elements = None

    def _get_elements(self):
        """Return the cached element list, rebuilding if necessary.

        Uses scene.structure_version to avoid DFS every frame.
        """
        go = self.game_object
        if go is not None:
            scene = go.scene
            if scene is not None:
                ver = scene.structure_version
                if ver != self._cached_elements_version:
                    self._cached_elements = None
                    self._cached_elements_version = ver
        if self._cached_elements is None:
            self._cached_elements = list(self.iter_ui_elements())
        return self._cached_elements

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def iter_ui_elements(self):
        """Yield this canvas's screen-space UI components (depth-first).

        Nested child canvases define their own rendering and input islands, so
        their subtrees must not be folded into the parent canvas. This matches
        Unity-style nested canvas semantics where each canvas owns its own draw
        list and render mode.
        """
        go = self.game_object
        if go is None:
            return
        from .inx_ui_screen_component import InxUIScreenComponent

        for comp in go.get_py_components():
            if isinstance(comp, InxUIScreenComponent):
                yield comp
        yield from self._walk_children(go)

    def _walk_children(self, parent):
        from .inx_ui_screen_component import InxUIScreenComponent
        for child in parent.get_children():
            for comp in child.get_py_components():
                if isinstance(comp, InxUIScreenComponent):
                    yield comp
            yield from self._walk_children(child)

    def raycast(self, canvas_x: float, canvas_y: float, tolerance: float = 0.0):
        """Return the front-most element hit at (canvas_x, canvas_y), or None.

        Iterates children in reverse depth-first order (last drawn = top).
        Only elements with ``raycast_target = True`` participate.
        Uses AABB pre-rejection before the full rotated hit-test.
        """
        ref_w = float(self.reference_width)
        ref_h = float(self.reference_height)
        elements = self._get_elements()
        for elem in reversed(elements):
            elem_go = getattr(elem, "game_object", None)
            if elem_go is not None and not elem_go.active_in_hierarchy:
                continue
            if not getattr(elem, "raycast_target", True):
                continue
            if not getattr(elem, "enabled", True):
                continue
            # AABB pre-rejection: skip expensive contains_point if outside visual rect
            vx, vy, vw, vh = elem.get_visual_rect(ref_w, ref_h)
            if not (vx - tolerance <= canvas_x <= vx + vw + tolerance
                    and vy - tolerance <= canvas_y <= vy + vh + tolerance):
                continue
            if elem.contains_point(canvas_x, canvas_y, ref_w, ref_h, tolerance):
                return elem
        return None

    def raycast_all(self, canvas_x: float, canvas_y: float, tolerance: float = 0.0):
        """Return all elements hit at (canvas_x, canvas_y), front-to-back order."""
        ref_w = float(self.reference_width)
        ref_h = float(self.reference_height)
        elements = self._get_elements()
        hits = []
        for elem in reversed(elements):
            elem_go = getattr(elem, "game_object", None)
            if elem_go is not None and not elem_go.active_in_hierarchy:
                continue
            if not getattr(elem, "raycast_target", True):
                continue
            if not getattr(elem, "enabled", True):
                continue
            # AABB pre-rejection
            vx, vy, vw, vh = elem.get_visual_rect(ref_w, ref_h)
            if not (vx - tolerance <= canvas_x <= vx + vw + tolerance
                    and vy - tolerance <= canvas_y <= vy + vh + tolerance):
                continue
            if elem.contains_point(canvas_x, canvas_y, ref_w, ref_h, tolerance):
                hits.append(elem)
        return hits
