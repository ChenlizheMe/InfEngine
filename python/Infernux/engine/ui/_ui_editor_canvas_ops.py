"""UIEditorCanvasOps — extracted from UIEditorPanel."""
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
from Infernux.ui.ui_canvas_utils import collect_runtime_canvases_with_go
from .editor_panel import EditorPanel
from .panel_registry import editor_panel
from .editor_icons import EditorIcons
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiMouseCursor
from .ui_editor_shortcuts import UIEditorInput
from Infernux.debug import Debug
from .imgui_keys import (
    KEY_LEFT_ARROW, KEY_RIGHT_ARROW, KEY_UP_ARROW, KEY_DOWN_ARROW,
)


class UIEditorCanvasOps:
    """UIEditorCanvasOps method group for UIEditorPanel."""

    def _get_all_canvases(self):
        """Return every Canvas in the active and persistent runtime scenes."""
        from Infernux.lib import SceneManager
        scene_manager = SceneManager.instance()
        scene = scene_manager.get_active_scene()
        persistent_getter = getattr(
            scene_manager,
            "get_runtime_persistent_scene",
            None,
        )
        persistent_scene = persistent_getter() if callable(persistent_getter) else None
        if scene is None and persistent_scene is None:
            return []
        # A Canvas can move into the persistent Scene during the first Play
        # frames without changing the active Scene identity. Retry a cached
        # empty snapshot briefly so opening UI Editor during that transfer
        # cannot pin the workspace to "no canvas".
        return collect_runtime_canvases_with_go(
            scene,
            persistent_scene,
            allow_stale_empty=True,
        )

    def _get_active_canvas(self):
        """Return (go, UICanvas) for the first canvas, or (None, None)."""
        canvases = self._get_all_canvases()
        if not canvases:
            return None, None
        # SelectionService is the only selection authority. The UI Editor must
        # never infer semantic state from another panel's rendering cache.
        from Infernux.engine.interaction import SelectionService

        selected_id = SelectionService.instance().primary_scene_object_id()
        if selected_id:
            for go, canvas in canvases:
                if self._is_descendant_of(selected_id, go):
                    return go, canvas
        return canvases[0]

    def _get_focused_canvas(self, all_canvases=None):
        """Return (go, UICanvas) the user is currently focused on, or (None, None)."""
        if all_canvases is None:
            all_canvases = self._get_all_canvases()
        if not all_canvases:
            return None, None
        for go, canvas in all_canvases:
            if go.id == self._focused_canvas_id:
                return go, canvas
        # A query must never mutate editor state. The first canvas is only a
        # presentation fallback until a user action establishes focus.
        return all_canvases[0]

    def _find_canvas_for_object(self, go, all_canvases=None):
        """Return the owning canvas pair for *go*, or (None, None)."""
        if go is None:
            return None, None
        if all_canvases is None:
            all_canvases = self._get_all_canvases()
        for canvas_go, canvas in all_canvases:
            if self._is_descendant_of(go.id, canvas_go):
                return canvas_go, canvas
        return None, None

    def _focus_canvas_for_object(self, go, all_canvases=None):
        """Set the focused canvas to the one that owns *go*."""
        canvas_go, canvas = self._find_canvas_for_object(go, all_canvases)
        if canvas_go is not None:
            self._set_focused_canvas_id(
                canvas_go.id,
                record_history=False,
                description="Focus UI Canvas",
            )
        return canvas_go, canvas

    def _ensure_canvas_layout(self, all_canvases):
        """Ensure every canvas has a workspace position.  Auto-layout new ones."""
        changed = False
        for go, canvas in all_canvases:
            if go.id not in self._canvas_panel_positions:
                pos = self._auto_layout_position(canvas, all_canvases)
                self._canvas_panel_positions[go.id] = list(pos)
                changed = True
        # Prune stale IDs
        active_ids = {go.id for go, _ in all_canvases}
        for stale in [k for k in self._canvas_panel_positions if k not in active_ids]:
            del self._canvas_panel_positions[stale]
            changed = True
        if changed:
            self._save_view_settings()

    def _auto_layout_position(self, canvas, all_canvases):
        """Find a position to the right of all existing canvases."""
        SPACING = Theme.UI_EDITOR_CANVAS_SPACING
        if not self._canvas_panel_positions:
            return (0.0, 0.0)
        max_right = 0.0
        for go, cv in all_canvases:
            if go.id in self._canvas_panel_positions:
                wx = self._canvas_panel_positions[go.id][0]
                right = wx + float(cv.reference_width)
                if right > max_right:
                    max_right = right
        return (max_right + SPACING, 0.0)

    def _canvas_workspace_rect(self, go_id, canvas):
        """Return (x, y, w, h) of a canvas in workspace coords (includes header)."""
        pos = self._canvas_panel_positions.get(go_id, [0.0, 0.0])
        ref_w = float(canvas.reference_width)
        ref_h = float(canvas.reference_height)
        header_h = Theme.UI_EDITOR_CANVAS_HEADER_H / max(self._zoom, 0.01)
        return (pos[0], pos[1] - header_h, ref_w, ref_h + header_h)

    @staticmethod
    def _rects_overlap(ax, ay, aw, ah, bx, by, bw, bh):
        return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)

    def _clamp_canvas_no_overlap(self, drag_id, new_wx, new_wy, all_canvases):
        """Push back canvas position to prevent overlap with other canvases."""
        drag_canvas = None
        for go, cv in all_canvases:
            if go.id == drag_id:
                drag_canvas = cv
                break
        if drag_canvas is None:
            return new_wx, new_wy

        ref_w = float(drag_canvas.reference_width)
        ref_h = float(drag_canvas.reference_height)
        header_h = Theme.UI_EDITOR_CANVAS_HEADER_H / max(self._zoom, 0.01)
        drag_rect = (new_wx, new_wy - header_h, ref_w, ref_h + header_h)

        for go, cv in all_canvases:
            if go.id == drag_id:
                continue
            other = self._canvas_workspace_rect(go.id, cv)
            if self._rects_overlap(*drag_rect, *other):
                # Push to nearest non-overlapping position
                # Try four directions and pick shortest displacement
                candidates = []
                # Push right of other
                rx = other[0] + other[2]
                candidates.append((rx, new_wy, abs(rx - new_wx)))
                # Push left of other
                lx = other[0] - ref_w
                candidates.append((lx, new_wy, abs(lx - new_wx)))
                # Push below other
                by_ = other[1] + other[3] + header_h
                candidates.append((new_wx, by_, abs(by_ - new_wy)))
                # Push above other
                ty = other[1] - ref_h - header_h
                candidates.append((new_wx, ty, abs(ty - new_wy)))
                candidates.sort(key=lambda c: c[2])
                new_wx, new_wy = candidates[0][0], candidates[0][1]
                drag_rect = (new_wx, new_wy - header_h, ref_w, ref_h + header_h)
        return new_wx, new_wy

    def _get_focused_canvas_origin(self, area_min_x, area_min_y, all_canvases=None):
        """Get screen-space origin for the focused canvas."""
        if all_canvases is None:
            all_canvases = self._get_all_canvases()
        foc_go, foc_cv = self._get_focused_canvas(all_canvases)
        if foc_go is None:
            return area_min_x, area_min_y
        pos = self._canvas_panel_positions.get(foc_go.id, [0.0, 0.0])
        return (area_min_x + pos[0] * self._zoom, area_min_y + pos[1] * self._zoom)

