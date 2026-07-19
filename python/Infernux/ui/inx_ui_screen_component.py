"""InxUIScreenComponent — base for 2D screen-space UI elements.

Provides anchor-aware position, size, and appearance data for screen UI.

Hierarchy:
    InxComponent → InxUIComponent → InxUIScreenComponent
"""

import math

from Infernux.components import serialized_field
from .inx_ui_component import InxUIComponent
from .enums import ScreenAlignH, ScreenAlignV
from .ui_render_revision import mark_runtime_ui_dirty

# Rect cache — avoids repeated hierarchy walks and serialized-field reads.
# Runtime callers retain it until hierarchy or geometry changes; editor tools
# may still provide a unique token when they need frame-local invalidation.
_rect_cache: dict = {}
_rect_cache_frame = None


def clear_rect_cache(frame_id=0) -> None:
    """Clear cached rectangles when the caller's invalidation token changes."""
    global _rect_cache, _rect_cache_frame
    if frame_id != _rect_cache_frame:
        _rect_cache.clear()
        _rect_cache_frame = frame_id


def _invalidate_rect_cache() -> None:
    """Discard cached hierarchy rectangles after a geometry mutation."""
    _rect_cache.clear()


class InxUIScreenComponent(InxUIComponent):
    """2D screen-space UI element with a canvas-pixel rectangle.

    Attributes:
        x: Horizontal position in canvas pixels (from canvas left edge).
        y: Vertical position in canvas pixels (from canvas top edge).
        width: Width in canvas pixels (unrotated content size).
        height: Height in canvas pixels (unrotated content size).
        rotation: Visual rotation in degrees (any angle).

    The ``_hide_transform_`` class flag tells the Inspector to skip the
    Transform header for GameObjects owning this component.
    """

    _hide_transform_: bool = True
    _GEOMETRY_FIELDS = frozenset({
        "align_h", "align_v", "x", "y", "width", "height", "rotation",
    })

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("_"):
            return
        object.__setattr__(
            self,
            "_ui_render_revision",
            int(getattr(self, "_ui_render_revision", 0)) + 1,
        )
        mark_runtime_ui_dirty()
        if name in self._GEOMETRY_FIELDS:
            _invalidate_rect_cache()

    align_h: ScreenAlignH = serialized_field(default=ScreenAlignH.Left, tooltip="Horizontal anchor", group="Position")
    align_v: ScreenAlignV = serialized_field(default=ScreenAlignV.Top, tooltip="Vertical anchor", group="Position")
    x: float = serialized_field(default=0.0, tooltip="X offset from anchor in canvas pixels", group="Position")
    y: float = serialized_field(default=0.0, tooltip="Y offset from anchor in canvas pixels", group="Position")
    rotation: float = serialized_field(default=0.0, tooltip="Rotation in degrees", group="Position")
    mirror_x: bool = serialized_field(default=False, tooltip="Mirror horizontally", group="Position")
    mirror_y: bool = serialized_field(default=False, tooltip="Mirror vertically", group="Position")

    width: float = serialized_field(default=160.0, tooltip="Width in canvas pixels", group="Layout")
    height: float = serialized_field(default=40.0, tooltip="Height in canvas pixels", group="Layout")
    lock_aspect_ratio: bool = serialized_field(default=False, tooltip="Preserve width/height ratio while resizing", group="Layout")

    opacity: float = serialized_field(default=1.0, range=(0.0, 1.0), tooltip="Element opacity", group="Appearance", slider=True)
    corner_radius: float = serialized_field(default=0.0, range=(0.0, 1000.0), tooltip="Corner radius in canvas pixels", group="Appearance")

    # ── Interaction ──
    raycast_target: bool = serialized_field(default=True, tooltip="Receive pointer events", group="Interaction")

    def _anchor_origin(self, ref_width: float, ref_height: float):
        """Anchor offset within a reference rectangle (parent or canvas)."""
        if self.align_h == ScreenAlignH.Center:
            anchor_x = ref_width * 0.5
        elif self.align_h == ScreenAlignH.Right:
            anchor_x = ref_width
        else:
            anchor_x = 0.0

        if self.align_v == ScreenAlignV.Center:
            anchor_y = ref_height * 0.5
        elif self.align_v == ScreenAlignV.Bottom:
            anchor_y = ref_height
        else:
            anchor_y = 0.0
        return anchor_x, anchor_y

    def _get_parent_world_rect(self, canvas_width: float, canvas_height: float):
        """Return the world rect (x, y, w, h) of the nearest parent UI element.

        Walks up the GameObject hierarchy looking for a parent with an
        InxUIScreenComponent.  If none is found, returns the canvas rect.
        """
        go = self.game_object
        if go is None:
            return (0.0, 0.0, canvas_width, canvas_height)
        parent_go = go.get_parent()
        while parent_go is not None:
            for py_comp in parent_go.get_py_components():
                if isinstance(py_comp, InxUIScreenComponent):
                    return py_comp.get_rect(canvas_width, canvas_height)
            parent_go = parent_go.get_parent()
        return (0.0, 0.0, canvas_width, canvas_height)

    def get_rect(self, canvas_width=None, canvas_height=None):
        """Return (x, y, w, h) of the *unrotated* content rect in canvas-space.

        Position is parent-relative: ``x`` / ``y`` are offsets from the
        parent UI element's top-left (or from the canvas origin if this
        element has no parent UI component).  Anchor is computed within
        the parent's width/height.
        """
        if canvas_width is None or canvas_height is None:
            return (self.x, self.y, self.width, self.height)
        # Per-frame cache: keyed on (element id, canvas_width, canvas_height)
        cache_key = (id(self), canvas_width, canvas_height)
        cached = _rect_cache.get(cache_key)
        if cached is not None:
            return cached
        cw = float(canvas_width)
        ch = float(canvas_height)
        px, py, pw, ph = self._get_parent_world_rect(cw, ch)
        anchor_x, anchor_y = self._anchor_origin(pw, ph)
        result = (px + anchor_x + self.x, py + anchor_y + self.y, self.width, self.height)
        _rect_cache[cache_key] = result
        return result

    def _rot_sincos(self):
        """Return (sin, cos) for the current rotation, cached per value."""
        rot = float(self.rotation) % 360.0
        rad = math.radians(rot)
        return math.sin(rad), math.cos(rad)

    def get_visual_rect(self, canvas_width=None, canvas_height=None):
        """Return the axis-aligned bounding box of the rotated content rect.

        Rotation is applied around the center of the unrotated rect.
        Returns (vx, vy, vw, vh) in canvas-space.
        """
        rx, ry, rw, rh = self.get_rect(canvas_width, canvas_height)
        rot = float(self.rotation) % 360.0
        if abs(rot) < 0.001:
            return (rx, ry, rw, rh)
        sin_a, cos_a = self._rot_sincos()
        acos = abs(cos_a)
        asin = abs(sin_a)
        vw = rw * acos + rh * asin
        vh = rw * asin + rh * acos
        return (rx + rw * 0.5 - vw * 0.5, ry + rh * 0.5 - vh * 0.5, vw, vh)

    def calc_visual_size(self, width: float, height: float):
        """Return rotated AABB size for the given unrotated width/height."""
        rot = float(self.rotation) % 360.0
        width = float(width)
        height = float(height)
        if abs(rot) < 0.001:
            return (width, height)
        sin_a, cos_a = self._rot_sincos()
        acos = abs(cos_a)
        asin = abs(sin_a)
        return (
            width * acos + height * asin,
            width * asin + height * acos,
        )

    def _rotated_corner_offset(self, width: float, height: float, corner_index: int,
                                _sincos=None):
        """Return offset from rect origin to the specified rotated corner."""
        width = float(width)
        height = float(height)
        hw, hh = width * 0.5, height * 0.5
        # Local corner offsets: TL, TR, BR, BL
        if corner_index == 0:
            lx, ly = -hw, -hh
        elif corner_index == 1:
            lx, ly = hw, -hh
        elif corner_index == 2:
            lx, ly = hw, hh
        else:
            lx, ly = -hw, hh
        if _sincos is not None:
            sin_a, cos_a = _sincos
        else:
            sin_a, cos_a = self._rot_sincos()
        rx = lx * cos_a - ly * sin_a
        ry = lx * sin_a + ly * cos_a
        return (hw + rx, hh + ry)

    def get_rotated_corners(self, canvas_width=None, canvas_height=None):
        """Return rotated rect corners in TL, TR, BR, BL order."""
        rect_x, rect_y, rect_w, rect_h = self.get_rect(canvas_width, canvas_height)
        hw, hh = rect_w * 0.5, rect_h * 0.5
        sin_a, cos_a = self._rot_sincos()
        # Inline all 4 corners with shared sin/cos
        corners = []
        for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
            corners.append((rect_x + hw + lx * cos_a - ly * sin_a,
                            rect_y + hh + lx * sin_a + ly * cos_a))
        return corners

    def set_rect(self, rect_x: float, rect_y: float, rect_w: float, rect_h: float,
                 canvas_width: float, canvas_height: float):
        """Store a canvas-space rect back into parent-relative serialized fields."""
        cw, ch = float(canvas_width), float(canvas_height)
        px, py, pw, ph = self._get_parent_world_rect(cw, ch)
        anchor_x, anchor_y = self._anchor_origin(pw, ph)
        self.x = float(rect_x) - px - anchor_x
        self.y = float(rect_y) - py - anchor_y
        self.width = float(rect_w)
        self.height = float(rect_h)
        _invalidate_rect_cache()

    def set_visual_position(self, vis_x: float, vis_y: float,
                            canvas_width: float, canvas_height: float):
        """Move the element so the visual AABB top-left is at (vis_x, vis_y).

        Keeps width/height/rotation unchanged; only adjusts x/y.
        """
        rw, rh = float(self.width), float(self.height)
        rot = float(self.rotation) % 360.0
        if abs(rot) < 0.001:
            vw, vh = rw, rh
        else:
            sin_a, cos_a = self._rot_sincos()
            acos, asin = abs(cos_a), abs(sin_a)
            vw = rw * acos + rh * asin
            vh = rw * asin + rh * acos
        # Visual center → content rect top-left
        vis_cx = vis_x + vw * 0.5
        vis_cy = vis_y + vh * 0.5
        new_rx = vis_cx - rw * 0.5
        new_ry = vis_cy - rh * 0.5
        cw, ch = float(canvas_width), float(canvas_height)
        px, py, pw, ph = self._get_parent_world_rect(cw, ch)
        anchor_x, anchor_y = self._anchor_origin(pw, ph)
        self.x = new_rx - px - anchor_x
        self.y = new_ry - py - anchor_y
        _invalidate_rect_cache()

    def set_size_preserve_visual_position(self, width: float, height: float,
                                          canvas_width: float, canvas_height: float):
        """Set width/height while keeping current visual AABB top-left unchanged."""
        vis_x, vis_y, _, _ = self.get_visual_rect(canvas_width, canvas_height)
        new_width = float(width)
        new_height = float(height)
        new_vis_w, new_vis_h = self.calc_visual_size(new_width, new_height)
        vis_cx = vis_x + new_vis_w * 0.5
        vis_cy = vis_y + new_vis_h * 0.5
        new_rx = vis_cx - new_width * 0.5
        new_ry = vis_cy - new_height * 0.5
        cw, ch = float(canvas_width), float(canvas_height)
        px, py, pw, ph = self._get_parent_world_rect(cw, ch)
        anchor_x, anchor_y = self._anchor_origin(pw, ph)
        self.x = new_rx - px - anchor_x
        self.y = new_ry - py - anchor_y
        self.width = new_width
        self.height = new_height
        _invalidate_rect_cache()

    def set_size_preserve_center(self, width: float, height: float,
                                 canvas_width: float, canvas_height: float):
        """Set width/height while keeping the element's visual center fixed."""
        rect_x, rect_y, rect_w, rect_h = self.get_rect(canvas_width, canvas_height)
        center_x = rect_x + rect_w * 0.5
        center_y = rect_y + rect_h * 0.5
        new_width = float(width)
        new_height = float(height)
        new_rect_x = center_x - new_width * 0.5
        new_rect_y = center_y - new_height * 0.5
        cw, ch = float(canvas_width), float(canvas_height)
        px, py, pw, ph = self._get_parent_world_rect(cw, ch)
        anchor_x, anchor_y = self._anchor_origin(pw, ph)
        self.x = new_rect_x - px - anchor_x
        self.y = new_rect_y - py - anchor_y
        self.width = new_width
        self.height = new_height
        _invalidate_rect_cache()

    def set_size_preserve_corner(self, width: float, height: float,
                                 canvas_width: float, canvas_height: float,
                                 corner: str = "top_left"):
        """Set width/height while keeping a rotated corner fixed."""
        corner_map = {
            "top_left": 0,
            "top_right": 1,
            "bottom_right": 2,
            "bottom_left": 3,
        }
        corner_index = corner_map.get(corner, 0)
        fixed_corner_x, fixed_corner_y = self.get_rotated_corners(canvas_width, canvas_height)[corner_index]
        new_width = float(width)
        new_height = float(height)
        sc = self._rot_sincos()
        off_x, off_y = self._rotated_corner_offset(new_width, new_height, corner_index, _sincos=sc)
        new_rect_x = fixed_corner_x - off_x
        new_rect_y = fixed_corner_y - off_y
        cw, ch = float(canvas_width), float(canvas_height)
        px, py, pw, ph = self._get_parent_world_rect(cw, ch)
        anchor_x, anchor_y = self._anchor_origin(pw, ph)
        self.x = new_rect_x - px - anchor_x
        self.y = new_rect_y - py - anchor_y
        self.width = new_width
        self.height = new_height
        _invalidate_rect_cache()

    # ------------------------------------------------------------------
    # Pointer event hooks (override in subclasses)
    # ------------------------------------------------------------------

    def on_pointer_enter(self, event_data):
        """Called when the pointer enters this element's rect."""
        pass

    def on_pointer_exit(self, event_data):
        """Called when the pointer leaves this element's rect."""
        pass

    def on_pointer_down(self, event_data):
        """Called when a mouse button is pressed over this element."""
        pass

    def on_pointer_up(self, event_data):
        """Called when a mouse button is released over this element."""
        pass

    def on_pointer_click(self, event_data):
        """Called on a complete click (down + up on the same element)."""
        pass

    def on_begin_drag(self, event_data):
        """Called when a drag gesture starts on this element."""
        pass

    def on_drag(self, event_data):
        """Called each frame during a drag gesture."""
        pass

    def on_end_drag(self, event_data):
        """Called when a drag gesture ends."""
        pass

    def on_scroll(self, event_data):
        """Called when the scroll wheel is used over this element."""
        pass

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def contains_point(self, px: float, py: float,
                       canvas_width: float, canvas_height: float,
                       tolerance: float = 0.0) -> bool:
        """Test whether canvas-space point (px, py) lies inside this element.

        Uses the oriented (rotated) bounding box for accurate hit-testing.
        *tolerance* expands the hit area by that many canvas-space pixels on each side.
        """
        if not self.raycast_target:
            return False

        rx, ry, rw, rh = self.get_rect(canvas_width, canvas_height)
        if tolerance > 0.0:
            rx -= tolerance
            ry -= tolerance
            rw += tolerance * 2.0
            rh += tolerance * 2.0
        rot = float(self.rotation) % 360.0

        if abs(rot) < 0.001:
            # Fast axis-aligned path
            return (rx <= px <= rx + rw) and (ry <= py <= ry + rh)

        # Rotate point into element's local frame (negate angle)
        sin_a, cos_a = self._rot_sincos()
        dx = px - (rx + rw * 0.5)
        dy = py - (ry + rh * 0.5)
        lx = dx * cos_a + dy * sin_a + rw * 0.5
        ly = -dx * sin_a + dy * cos_a + rh * 0.5
        return (0.0 <= lx <= rw) and (0.0 <= ly <= rh)
