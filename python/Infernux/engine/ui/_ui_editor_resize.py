"""UIEditorResizeMixin — extracted from UIEditorPanel."""
from __future__ import annotations

"""UI Editor panel — Figma-style 2D canvas editor for screen-space UI layout.

Displays the selected UICanvas at its reference resolution and lets users
visually position UI elements via drag.  Max zoom is 100% (1:1 pixels).

Docked alongside Scene / Game views.
"""

import configparser
import math
import os
from contextlib import nullcontext as _nullcontext
from time import perf_counter as _pc

from typing import Optional
from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from Infernux.engine.project_context import get_project_root
from Infernux.ui.enums import TextResizeMode
from Infernux.ui.inx_ui_screen_component import clear_rect_cache
from Infernux.ui.ui_texture_cache import get_shared_cache as _get_tex_cache
from Infernux.ui.ui_render_dispatch import dispatch as _ui_dispatch
from Infernux.ui.ui_canvas_utils import collect_canvases_with_go
from .editor_panel import EditorPanel
from .panel_registry import editor_panel
from .editor_icons import EditorIcons
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiMouseCursor
from .ui_editor_shortcuts import UIEditorInput
from Infernux.debug import Debug
from .imgui_keys import (
    KEY_LEFT_ARROW, KEY_RIGHT_ARROW, KEY_UP_ARROW, KEY_DOWN_ARROW,
)


class UIEditorResizeMixin:
    """UIEditorResizeMixin method group for UIEditorPanel."""

    def _prepare_resize_element(self, elem):
        if elem is None:
            return
        if hasattr(elem, "resize_mode"):
            if getattr(elem, "resize_mode", None) != TextResizeMode.FixedSize:
                elem.resize_mode = TextResizeMode.FixedSize

    def _apply_rotation_drag(self, inp):
        elem = self._selected_element_comp
        if elem is None:
            return
        angle = math.degrees(math.atan2(inp.mouse_y - self._rotate_center_sy,
                                        inp.mouse_x - self._rotate_center_sx))
        elem.rotation = float(self._rotate_start_rotation + (angle - self._rotate_start_angle))

    def _apply_resize(self, inp):
        """Update element rect based on current resize handle drag.

        Rotation-aware: mouse deltas are projected onto the element's local
        axes and the opposite rotated corner is preserved.

        Handle index mapping (from hit test):
            Corners: 0=TL, 1=TR, 2=BR, 3=BL
            Edges:   4=top, 5=bottom, 6=left, 7=right
        """
        elem = self._selected_element_comp
        if elem is None:
            return

        _, canvas = self._get_focused_canvas()
        if canvas is None:
            return
        cw = float(canvas.reference_width)
        ch = float(canvas.reference_height)

        # Canvas-space mouse delta
        dx_canvas = (inp.mouse_x - self._resize_start_mx) / self._zoom
        dy_canvas = (inp.mouse_y - self._resize_start_my) / self._zoom

        # Project onto element's local axes using rotation at drag start
        rot = math.radians(self._resize_start_rotation)
        cos_r = math.cos(rot)
        sin_r = math.sin(rot)
        dlx = dx_canvas * cos_r + dy_canvas * sin_r   # delta along local X
        dly = -dx_canvas * sin_r + dy_canvas * cos_r  # delta along local Y

        snap = self._drag_snap_step()
        dlx = round(dlx / snap) * snap
        dly = round(dly / snap) * snap

        _, _, sw, sh = self._resize_start_rect
        idx = self._resize_handle_idx
        MIN_SIZE = Theme.UI_EDITOR_MIN_ELEM_SIZE

        # Handle → (width_delta_sign, height_delta_sign, fixed_corner_index)
        # Corner indices: TL=0, TR=1, BR=2, BL=3
        _HANDLE_INFO = {
            0: (-1, -1, 2),  # TL handle → fix BR
            1: (+1, -1, 3),  # TR handle → fix BL
            2: (+1, +1, 0),  # BR handle → fix TL
            3: (-1, +1, 1),  # BL handle → fix TR
            4: ( 0, -1, 3),  # top edge  → fix BL
            5: ( 0, +1, 0),  # bot edge  → fix TL
            6: (-1,  0, 1),  # left edge → fix TR
            7: (+1,  0, 0),  # right edge→ fix TL
        }
        w_sign, h_sign, fixed_idx = _HANDLE_INFO.get(idx, (0, 0, 0))

        new_w = sw + dlx * w_sign if w_sign != 0 else sw
        new_h = sh + dly * h_sign if h_sign != 0 else sh
        new_w = max(new_w, MIN_SIZE)
        new_h = max(new_h, MIN_SIZE)

        # Aspect ratio lock
        if bool(getattr(elem, 'lock_aspect_ratio', False)) and sw > 0.0 and sh > 0.0:
            aspect = sw / max(sh, 1e-6)
            if w_sign != 0:  # width changed → adjust height
                new_h = max(MIN_SIZE, new_w / max(aspect, 1e-6))
            else:            # height only  → adjust width
                new_w = max(MIN_SIZE, new_h * aspect)

        # Alignment snapping for resize (snap moving edges to guides)
        new_w, new_h = self._apply_resize_alignment_snapping(
            canvas, elem, new_w, new_h, w_sign, h_sign, fixed_idx, cw, ch,
        )

        # Preserve the fixed corner using stored initial positions
        fixed_cx, fixed_cy = self._resize_start_corners[fixed_idx]
        off_x, off_y = elem._rotated_corner_offset(new_w, new_h, fixed_idx)
        new_rx = fixed_cx - off_x
        new_ry = fixed_cy - off_y
        anchor_x, anchor_y = elem._anchor_origin(cw, ch)
        elem.x = new_rx - anchor_x
        elem.y = new_ry - anchor_y
        elem.width = new_w
        elem.height = new_h

    def _apply_drag_suppressed(self, vis_x, vis_y, ref_w, ref_h):
        """Apply drag inside the core-owned continuous edit session."""
        elem = self._selected_element_comp
        if elem is None:
            return False
        return self._mutate_element_manipulation(
            lambda: elem.set_visual_position(vis_x, vis_y, ref_w, ref_h)
        )

    def _apply_resize_suppressed(self, inp):
        """Apply resize inside the core-owned continuous edit session."""
        return self._mutate_element_manipulation(lambda: self._apply_resize(inp))

    def _apply_rotation_drag_suppressed(self, inp):
        """Apply rotation inside the core-owned continuous edit session."""
        return self._mutate_element_manipulation(
            lambda: self._apply_rotation_drag(inp)
        )

