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
from Infernux.engine.interaction import ContinuousEditService, ViewCommandService
from Infernux.engine.project_context import get_project_root
from Infernux.engine.project_view_settings import (
    load_project_view_settings,
    write_project_view_settings_section,
)
from Infernux.ui.enums import TextResizeMode
from Infernux.ui.inx_ui_screen_component import clear_rect_cache
from Infernux.ui.ui_texture_cache import get_shared_cache as _get_tex_cache
from Infernux.ui.ui_render_dispatch import dispatch as _ui_dispatch
from Infernux.ui.ui_canvas_utils import scene_canvas_cache_key
from .editor_panel import EditorPanel
from .dpi import scaled_editor_metric
from .panel_registry import editor_panel
from .editor_icons import EditorIcons
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiMouseCursor
from .ui_editor_shortcuts import UIEditorInput
from Infernux.debug import Debug
from ._ui_editor_canvas_ops import UIEditorCanvasOps
from ._ui_editor_geometry import UIEditorGeometryMixin
from ._ui_editor_alignment import UIEditorAlignmentMixin
from ._ui_editor_resize import UIEditorResizeMixin
from ._ui_editor_creation import UIEditorCreationMixin


def _metric(ctx, value: float) -> float:
    return scaled_editor_metric(ctx, value)

@editor_panel("UI Editor", type_id="ui_editor", title_key="panel.ui_editor")
class UIEditorPanel(UIEditorCanvasOps, UIEditorGeometryMixin, UIEditorAlignmentMixin, UIEditorResizeMixin, UIEditorCreationMixin, EditorPanel):
    """Figma-style 2D UI editor panel."""

    WINDOW_TYPE_ID = "ui_editor"
    WINDOW_DISPLAY_NAME = "UI Editor"

    def __init__(self, title: str = "UI Editor"):
        super().__init__(title, window_id="ui_editor")

        # ── Canvas navigation ──
        self._zoom: float = 1.0
        self._pan_x: float = 0.0       # Pan offset in screen pixels
        self._pan_y: float = 0.0
        self._is_panning: bool = False

        # ── Selection projection ──
        # SelectionService owns the selection.  The panel only caches the
        # resolved screen component for the current service revision.
        self._selected_element_cache_revision: int = -1
        self._selected_element_cache_object_id: int = 0
        self._selected_element_cache_scene_key = None
        self._selected_element_cache = None
        self._dragging: bool = False
        self._drag_start_x: float = 0.0
        self._drag_start_y: float = 0.0
        self._drag_elem_start_x: float = 0.0
        self._drag_elem_start_y: float = 0.0

        # ── Resize handle state ──
        self._resizing: bool = False
        self._resize_handle_idx: int = -1     # Which handle is being dragged
        self._resize_start_mx: float = 0.0    # Mouse pos at resize start (screen)
        self._resize_start_my: float = 0.0
        self._resize_start_rect = (0.0, 0.0, 0.0, 0.0)  # (x, y, w, h) at start
        self._resize_start_rotation: float = 0.0
        self._resize_start_corners = [(0.0, 0.0)] * 4
        self._rotating: bool = False
        self._rotate_start_angle: float = 0.0
        self._rotate_start_rotation: float = 0.0
        self._rotate_center_sx: float = 0.0
        self._rotate_center_sy: float = 0.0

        # One core-owned session covers each cross-frame element manipulation.
        self._active_element_edit_key: str = ""

        # ── External references ──
        self._engine = None                  # Engine instance (for game texture)

        # ── Shared Hierarchy presentation ──
        self._settings_loaded: bool = False
        self._active_alignment_guides: list[tuple[str, float, float, float]] = []

        # ── Click-through cycling state (Unity/ScenePanel-style) ──
        self._pick_cycle_candidates: list = []
        self._pick_cycle_index: int = -1
        self._pick_cycle_last_canvas_pos: tuple[float, float] = (-1.0, -1.0)

        # ── Multi-canvas layout ──
        self._canvas_panel_positions: dict = {}   # {go_id: [wx, wy]} workspace coords
        self._focused_canvas_id: int = 0          # GO id of the focused canvas
        self._dragging_canvas: bool = False
        self._drag_canvas_id: int = 0
        self._drag_canvas_start_mx: float = 0.0
        self._drag_canvas_start_my: float = 0.0
        self._drag_canvas_start_wx: float = 0.0
        self._drag_canvas_start_wy: float = 0.0



    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_engine(self, engine):
        """Set engine instance (needed for Game background mode)."""
        self._engine = engine

    def current_child_context_id(self) -> str:
        return (
            f"canvas:{int(self._focused_canvas_id)}"
            if self._focused_canvas_id
            else ""
        )

    def restore_child_context(self, context_id: str) -> bool:
        value = str(context_id or "").strip()
        if not value:
            self._focused_canvas_id = 0
            return True
        prefix, separator, object_id = value.partition(":")
        if prefix != "canvas" or not separator:
            return False
        try:
            resolved = int(object_id)
        except ValueError:
            return False
        if resolved <= 0 or all(
            int(go.id) != resolved for go, _canvas in self._get_all_canvases()
        ):
            return False
        self._focused_canvas_id = resolved
        return True

    def _apply_focused_canvas_id(self, object_id: int) -> None:
        self._focused_canvas_id = max(0, int(object_id or 0))
        self.publish_child_context(
            self.current_child_context_id(),
            reason="ui_editor_canvas_focus",
            record_history=False,
        )

    def _set_focused_canvas_id(
        self,
        object_id: int,
        *,
        record_history: bool,
        description: str,
    ) -> bool:
        before = int(self._focused_canvas_id or 0)
        after = max(0, int(object_id or 0))
        if before == after:
            return True
        if not record_history:
            self._apply_focused_canvas_id(after)
            return True
        return ViewCommandService.require().set_value(
            before,
            after,
            self._apply_focused_canvas_id,
            description=description,
        )

    def can_nudge_selected(self) -> bool:
        return bool(
            self._selected_element_comp is not None
            and not self._dragging
            and not self._resizing
            and not self._rotating
        )

    def command_nudge_selected(self, dx: int, dy: int) -> bool:
        """Move the selected UI element through the global action journal."""
        elem = self._selected_element_comp
        dx = int(dx)
        dy = int(dy)
        if elem is None or (dx == 0 and dy == 0) or not self.can_nudge_selected():
            return False

        from Infernux.engine.interaction import ComponentCommandService

        changes = []
        if dx:
            old_x = float(elem.x)
            changes.append((elem, "x", old_x, old_x + dx, "Set x"))
        if dy:
            old_y = float(elem.y)
            changes.append((elem, "y", old_y, old_y + dy, "Set y"))
        return ComponentCommandService.require().execute_property_changes(
            changes,
            description="Nudge UI Element",
        )

    def project_global_selection(self, go):
        """Project the global scene selection into the UI Editor.

        If *go* has an InxUIScreenComponent, select it; if it's a Canvas,
        focus that canvas; otherwise clear element selection but keep canvas
        focus if the object is inside a canvas tree.
        """
        self._finish_element_manipulation(commit=True)
        if go is None:
            self._clear_interaction_state()
            return
        self._invalidate_selected_element_cache()
        self._focus_canvas_for_object(go)
        # Reset interaction state but keep focused canvas
        self._dragging = False
        self._resizing = False
        self._resize_handle_idx = -1
        self._rotating = False
        self._active_alignment_guides = []
        from Infernux.ui import UICanvas
        from Infernux.ui.inx_ui_screen_component import InxUIScreenComponent
        for comp in go.get_py_components():
            if isinstance(comp, UICanvas):
                self._set_focused_canvas_id(
                    go.id,
                    record_history=False,
                    description="Focus UI Canvas",
                )
                return
            if isinstance(comp, InxUIScreenComponent):
                return

    def _clear_interaction_state(self):
        """Reset transient manipulation state without owning selection."""
        self._finish_element_manipulation(commit=True)
        self._invalidate_selected_element_cache()
        self._dragging = False
        self._resizing = False
        self._resize_handle_idx = -1
        self._rotating = False
        self._active_alignment_guides = []

    def _invalidate_selected_element_cache(self) -> None:
        self._selected_element_cache_revision = -1
        self._selected_element_cache_object_id = 0
        self._selected_element_cache_scene_key = None
        self._selected_element_cache = None

    @staticmethod
    def _element_object_id(element) -> int:
        game_object = getattr(element, "game_object", None)
        return int(getattr(game_object, "id", 0) or 0)

    @property
    def _selected_element_comp(self):
        """Project the global GameObject selection onto one UI component."""
        from Infernux.engine.interaction import SelectionService

        selection = SelectionService.instance()
        object_id = selection.primary_scene_object_id()
        from Infernux.lib import SceneManager

        scene_manager = SceneManager.instance()
        scene = scene_manager.get_active_scene()
        persistent_getter = getattr(
            scene_manager,
            "get_runtime_persistent_scene",
            None,
        )
        persistent_scene = persistent_getter() if callable(persistent_getter) else None
        scenes = tuple(
            candidate
            for candidate in (scene, persistent_scene)
            if candidate is not None
        )
        scene_key = tuple(scene_canvas_cache_key(candidate) for candidate in scenes)
        cached_component = getattr(self, "_selected_element_cache", None)
        if (
            self._selected_element_cache_revision == selection.revision
            and self._selected_element_cache_object_id == object_id
            and getattr(self, "_selected_element_cache_scene_key", None) == scene_key
            and (
                cached_component is None
                or bool(getattr(cached_component, "is_valid", True))
            )
        ):
            return cached_component

        component = None
        if object_id > 0:
            from Infernux.ui.inx_ui_screen_component import InxUIScreenComponent

            game_object = None
            for candidate_scene in scenes:
                game_object = candidate_scene.find_by_id(object_id)
                if game_object is not None:
                    break
            if game_object is not None:
                component = next(
                    (
                        candidate
                        for candidate in game_object.get_py_components()
                        if isinstance(candidate, InxUIScreenComponent)
                    ),
                    None,
                )

        self._selected_element_cache_revision = selection.revision
        self._selected_element_cache_object_id = object_id
        self._selected_element_cache_scene_key = scene_key
        self._selected_element_cache = component
        return component

    def _settings_ini_path(self) -> Optional[str]:
        root = get_project_root()
        if not root:
            return None
        return os.path.join(root, "ProjectSettings", "GameView.ini")

    def _load_view_settings(self):
        if self._settings_loaded:
            return
        self._settings_loaded = True

        path = self._settings_ini_path()
        if not path:
            return
        if not os.path.isfile(path):
            self._save_view_settings()
            return

        try:
            cp = load_project_view_settings(path)
        except (OSError, configparser.Error) as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return

        if "UIEditor" not in cp:
            self._save_view_settings()
            return

        section = cp["UIEditor"]
        self._zoom = max(Theme.UI_EDITOR_MIN_ZOOM, min(Theme.UI_EDITOR_MAX_ZOOM, section.getfloat("zoom", fallback=1.0)))
        self._pan_x = section.getfloat("pan_x", fallback=0.0)
        self._pan_y = section.getfloat("pan_y", fallback=0.0)
        # bg_mode removed — always solid background

        # Canvas panel positions: stored as "canvas_pos_<id> = x,y"
        self._canvas_panel_positions.clear()
        for key, value in section.items():
            if key.startswith("canvas_pos_"):
                try:
                    go_id = int(key[len("canvas_pos_"):])
                    parts = value.split(",")
                    if len(parts) == 2:
                        self._canvas_panel_positions[go_id] = [float(parts[0]), float(parts[1])]
                except (ValueError, IndexError) as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    pass

    def _save_view_settings(self):
        path = self._settings_ini_path()
        if not path:
            return

        values = {
            "zoom": f"{self._zoom:.6f}",
            "pan_x": f"{self._pan_x:.3f}",
            "pan_y": f"{self._pan_y:.3f}",
        }
        # Persist canvas panel positions
        for go_id, pos in self._canvas_panel_positions.items():
            values[f"canvas_pos_{go_id}"] = f"{pos[0]:.1f},{pos[1]:.1f}"
        write_project_view_settings_section(path, "UIEditor", values)

    @property
    def _view_edit_owner(self) -> str:
        return f"{self.window_id}:view"

    def _view_edit_key(self, kind: str) -> str:
        return f"{self._view_edit_owner}:{str(kind or '').strip()}"

    def _capture_view_state(self):
        return (
            float(self._zoom),
            float(self._pan_x),
            float(self._pan_y),
            tuple(
                sorted(
                    (
                        int(go_id),
                        float(position[0]),
                        float(position[1]),
                    )
                    for go_id, position in self._canvas_panel_positions.items()
                )
            ),
        )

    def _apply_view_state(self, state, *, persist: bool = True) -> None:
        zoom, pan_x, pan_y, canvas_positions = state
        self._zoom = max(
            Theme.UI_EDITOR_MIN_ZOOM,
            min(Theme.UI_EDITOR_MAX_ZOOM, float(zoom)),
        )
        self._pan_x = float(pan_x)
        self._pan_y = float(pan_y)
        self._canvas_panel_positions = {
            int(go_id): [float(x), float(y)]
            for go_id, x, y in canvas_positions
        }
        if persist:
            self._save_view_settings()

    def _commit_view_state(self, before, description: str) -> bool:
        after = self._capture_view_state()
        if before == after:
            return False
        recorded = ViewCommandService.require().set_value(
            before,
            after,
            self._apply_view_state,
            description=description,
        )
        if not recorded:
            self._save_view_settings()
        return True

    def _commit_continuous_view_edit(self, session) -> bool:
        self._apply_view_state(session.current_value, persist=False)
        return self._commit_view_state(session.initial_value, session.description)

    def _cancel_continuous_view_edit(self, session) -> None:
        self._apply_view_state(session.initial_value, persist=False)

    def _begin_continuous_view_edit(self, kind: str, description: str) -> str:
        edits = ContinuousEditService.instance()
        key = self._view_edit_key(kind)
        if edits.get(key) is None:
            edits.commit_owner(self._view_edit_owner)
            edits.begin(
                key,
                owner_id=self._view_edit_owner,
                description=description,
                initial_value=self._capture_view_state(),
                on_commit=self._commit_continuous_view_edit,
                on_cancel=self._cancel_continuous_view_edit,
            )
        return key

    def _update_continuous_view_edit(self, key: str) -> None:
        ContinuousEditService.instance().update(key, self._capture_view_state())

    def _commit_pending_view_edits(self) -> None:
        ContinuousEditService.instance().commit_owner(self._view_edit_owner)

    # ------------------------------------------------------------------
    # Helpers — canvas / element discovery
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Multi-canvas layout helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Coordinate transforms
    # ------------------------------------------------------------------

    def _sync_text_layout(self, ctx: InxGUIContext, text_comp):
        text = getattr(text_comp, "text", "")
        font_size = max(
            1.0,
            float(getattr(text_comp, "font_size", Theme.UI_DEFAULT_FONT_SIZE)),
        )
        wrap_width = float(text_comp.get_editor_wrap_width()) if hasattr(text_comp, "get_editor_wrap_width") else (
            float(text_comp.get_wrap_width()) if hasattr(text_comp, "get_wrap_width") else 0.0
        )
        font_path = str(getattr(text_comp, "font_path", "") or "")
        line_height = float(getattr(text_comp, "line_height", 1.2))
        letter_spacing = float(getattr(text_comp, "letter_spacing", 0.0))
        pad_x, pad_y = getattr(text_comp, "get_auto_size_padding", lambda: (0.0, 0.0))()
        if wrap_width > 0.0:
            measured_w, measured_h = ctx.calc_text_size_wrapped(
                text, font_size, wrap_width, font_path, line_height, letter_spacing
            )
        else:
            measured_w, measured_h = ctx.calc_text_size(text, font_size, font_path, line_height, letter_spacing)

        _TOL = 0.5  # Skip writes when measured size is close enough (sub-pixel)

        _, canvas = self._get_focused_canvas()
        if canvas is None:
            if getattr(text_comp, "is_auto_width", lambda: False)():
                target_w = max(1.0, float(measured_w) + float(pad_x))
                if abs(float(text_comp.width) - target_w) > _TOL:
                    text_comp.width = target_w
            elif getattr(text_comp, "is_auto_height", lambda: False)():
                target_h = max(1.0, float(measured_h) + float(pad_y))
                if abs(float(text_comp.height) - target_h) > _TOL:
                    text_comp.height = target_h
            return

        # While the user is actively dragging or resizing, only adjust the
        # auto-sized dimension (width or height) without touching x/y.
        # set_size_preserve_corner rewrites x/y which fights the drag.
        _interacting = (self._dragging or self._resizing)

        if getattr(text_comp, "is_auto_width", lambda: False)():
            target_w = max(1.0, float(measured_w) + float(pad_x))
            if abs(float(text_comp.width) - target_w) > _TOL:
                if _interacting:
                    text_comp.width = target_w
                else:
                    text_comp.set_size_preserve_corner(
                        target_w,
                        float(text_comp.height),
                        float(canvas.reference_width),
                        float(canvas.reference_height),
                        "top_left",
                    )
        elif getattr(text_comp, "is_auto_height", lambda: False)():
            target_h = max(1.0, float(measured_h) + float(pad_y))
            if abs(float(text_comp.height) - target_h) > _TOL:
                if _interacting:
                    text_comp.height = target_h
                else:
                    text_comp.set_size_preserve_corner(
                        float(text_comp.width),
                        target_h,
                        float(canvas.reference_width),
                        float(canvas.reference_height),
                        "top_left",
                    )

    # ------------------------------------------------------------------
    # EditorPanel hooks
    # ------------------------------------------------------------------

    def _initial_size(self):
        return (Theme.UI_EDITOR_INIT_WINDOW_W, Theme.UI_EDITOR_INIT_WINDOW_H)

    def _window_flags(self) -> int:
        return Theme.WINDOW_FLAGS_VIEWPORT | Theme.WINDOW_FLAGS_NO_SCROLL

    def _on_visible_pre(self, ctx):
        self._load_view_settings()

    def _on_not_visible(self, ctx):
        self._commit_pending_view_edits()
        self._finish_element_manipulation(commit=True)

    def on_disable(self) -> None:
        self._commit_pending_view_edits()

    def on_render_content(self, ctx: InxGUIContext):
        all_canvases = self._get_all_canvases()
        if not all_canvases:
            self._render_no_canvas(ctx)
        else:
            self._ensure_canvas_layout(all_canvases)
            focused_go, focused_cv = self._get_focused_canvas(all_canvases)
            self._render_toolbar(ctx, focused_go, focused_cv)
            self._render_multi_canvas_area(ctx, all_canvases)

    # ── No Canvas placeholder ────────────────────────────────────────

    def _render_no_canvas(self, ctx: InxGUIContext):
        # Canvas is gone — clear any stale selection / interaction state
        self._clear_interaction_state()
        ctx.label("")
        ctx.label("  " + t("ui_editor.no_canvas"))
        ctx.label("")
        ctx.label("  " + t("ui_editor.create_canvas_hint").split("\n")[0])
        ctx.label("  " + t("ui_editor.create_canvas_hint").split("\n")[1])
        ctx.label("")
        ctx.button(t("ui_editor.create_canvas"), self._create_canvas,
                   width=_metric(ctx, Theme.UI_EDITOR_CREATE_BTN_W),
                   height=_metric(ctx, Theme.UI_EDITOR_CREATE_BTN_H))

    # ── Toolbar ──────────────────────────────────────────────────────

    def _render_toolbar(self, ctx: InxGUIContext, canvas_go, canvas):
        """Top toolbar with creation buttons, zoom control, and background toggle."""
        _GAP = _metric(ctx, Theme.UI_EDITOR_TOOLBAR_GAP)
        _SEC = _metric(ctx, Theme.UI_EDITOR_TOOLBAR_SECTION_GAP)

        ctx.label(f"Canvas: {canvas_go.name}")
        ctx.same_line(0, _SEC)

        native = self._engine.get_native_engine() if self._engine else None
        _ICO_SZ = _metric(ctx, Theme.EDITOR_ICON_SIZE)

        # ── Create Canvas button ──
        ctx.button(
            "Canvas",
            self._create_canvas,
            width=_metric(ctx, 72.0),
            height=_ICO_SZ + _metric(ctx, 8.0),
        )
        if ctx.is_item_hovered():
            ctx.set_tooltip(t("ui_editor.tooltip_canvas"))

        ctx.same_line(0, _GAP)
        tid_text = EditorIcons.get(native, Theme.ICON_IMG_UI_TEXT)
        if tid_text:
            if ctx.image_button("##add_text", tid_text, _ICO_SZ, _ICO_SZ):
                self._create_text_element(canvas_go)
        else:
            ctx.button(
                "T",
                lambda: self._create_text_element(canvas_go),
                width=_ICO_SZ + _metric(ctx, 8.0),
                height=_ICO_SZ + _metric(ctx, 8.0),
            )
        if ctx.is_item_hovered():
            ctx.set_tooltip(t("ui_editor.tooltip_text"))

        ctx.same_line(0, _GAP)
        tid_img = EditorIcons.get(native, Theme.ICON_IMG_UI_IMAGE)
        if tid_img:
            if ctx.image_button("##add_image", tid_img, _ICO_SZ, _ICO_SZ):
                self._create_image_element(canvas_go)
        else:
            ctx.button(
                "Img",
                lambda: self._create_image_element(canvas_go),
                width=_ICO_SZ + _metric(ctx, 8.0),
                height=_ICO_SZ + _metric(ctx, 8.0),
            )
        if ctx.is_item_hovered():
            ctx.set_tooltip(t("ui_editor.tooltip_image"))

        ctx.same_line(0, _GAP)
        tid_btn = EditorIcons.get(native, Theme.ICON_IMG_UI_BUTTON)
        if tid_btn:
            if ctx.image_button("##add_button", tid_btn, _ICO_SZ, _ICO_SZ):
                self._create_button_element(canvas_go)
        else:
            ctx.button(
                "Btn",
                lambda: self._create_button_element(canvas_go),
                width=_ICO_SZ + _metric(ctx, 8.0),
                height=_ICO_SZ + _metric(ctx, 8.0),
            )
        if ctx.is_item_hovered():
            ctx.set_tooltip(t("ui_editor.tooltip_button"))

        ctx.same_line(0, _SEC // 2)
        zoom_pct = int(self._zoom * 100)
        ctx.label(t("ui_editor.zoom").format(pct=zoom_pct))
        ctx.same_line(0, _SEC // 2)
        ctx.button(
            t("ui_editor.fit"),
            lambda: self._fit_zoom(ctx, canvas),
            width=_metric(ctx, 56.0),
        )

        ctx.separator()

    # ── Canvas area (multi-canvas) ───────────────────────────────────

    def _render_multi_canvas_area(self, ctx: InxGUIContext, all_canvases):
        """Main area: multi-canvas workspace with zoomable panels."""
        self._validate_element_selection(all_canvases)

        region_w = ctx.get_content_region_avail_width()
        region_h = ctx.get_content_region_avail_height()
        if region_w < 1 or region_h < 1:
            return

        ctx.invisible_button("##ui_canvas_area", region_w, region_h)
        area_hovered = ctx.is_item_hovered()
        area_min_x = ctx.get_item_rect_min_x()
        area_min_y = ctx.get_item_rect_min_y()
        area_max_x = area_min_x + region_w
        area_max_y = area_min_y + region_h

        inp = UIEditorInput(ctx, area_hovered)

        self._process_zoom_input(inp, area_min_x, area_min_y)
        self._process_canvas_drag_input(inp, all_canvases)
        self._process_workspace_pan(ctx, inp)

        focused_go, focused_canvas = self._get_focused_canvas(all_canvases)
        foc_origin_x, foc_origin_y = self._get_focused_canvas_origin(
            area_min_x, area_min_y, all_canvases)
        clear_rect_cache(_pc())

        if focused_canvas is not None:
            foc_ref_w = float(focused_canvas.reference_width)
            foc_ref_h = float(focused_canvas.reference_height)
        else:
            foc_ref_w = foc_ref_h = 1.0

        self._process_ongoing_interactions(inp, foc_ref_w, foc_ref_h, focused_canvas)

        area = (area_min_x, area_min_y, area_max_x, area_max_y)
        ctx.push_draw_list_clip_rect(*area, True)
        hovered_canvas_id, hovered_elem, hovered_all = self._draw_canvases_and_hittest(
            ctx, all_canvases, inp, area)
        self._draw_selection_overlay(
            ctx, focused_canvas, foc_origin_x, foc_origin_y,
            foc_ref_w, foc_ref_h, area)
        ctx.pop_draw_list_clip_rect()

        self._update_hover_cursor(ctx, area_hovered, inp.mouse_x, inp.mouse_y)
        self._handle_canvas_click(
            ctx, inp, all_canvases,
            hovered_canvas_id, hovered_elem, hovered_all,
            foc_origin_x, foc_origin_y,
            area_min_x, area_min_y,
        )

    # ------------------------------------------------------------------
    # Extracted helpers for _render_multi_canvas_area
    # ------------------------------------------------------------------

    def _validate_element_selection(self, all_canvases):
        """Clear selection if the selected element is no longer in any canvas."""
        if self._selected_element_comp is None:
            return
        try:
            sel_go = self._selected_element_comp.game_object
            if sel_go is None:
                self._clear_interaction_state()
            elif not any(self._is_descendant_of(sel_go.id, cgo) for cgo, _ in all_canvases):
                self._clear_interaction_state()
        except Exception:
            self._clear_interaction_state()

    def _process_zoom_input(self, inp, area_min_x, area_min_y):
        """Apply mouse-wheel zoom."""
        edits = ContinuousEditService.instance()
        key = self._view_edit_key("zoom")
        if abs(inp.wheel_delta) <= 0.01:
            edits.commit_if_idle(key, idle_seconds=0.12)
            return
        key = self._begin_continuous_view_edit("zoom", "Zoom UI Editor View")
        old_zoom = self._zoom
        self._zoom = max(Theme.UI_EDITOR_MIN_ZOOM,
                         min(Theme.UI_EDITOR_MAX_ZOOM,
                             self._zoom * (1.0 + inp.wheel_delta * Theme.UI_EDITOR_ZOOM_STEP)))
        factor = self._zoom / old_zoom
        self._pan_x = inp.mouse_x - area_min_x - factor * (inp.mouse_x - area_min_x - self._pan_x)
        self._pan_y = inp.mouse_y - area_min_y - factor * (inp.mouse_y - area_min_y - self._pan_y)
        self._update_continuous_view_edit(key)

    def _process_canvas_drag_input(self, inp, all_canvases):
        """Continue or finish dragging a canvas panel around the workspace."""
        if not self._dragging_canvas:
            return
        if inp.lmb_down:
            key = self._begin_continuous_view_edit(
                "canvas",
                "Move UI Editor Canvas",
            )
            dx = (inp.mouse_x - self._drag_canvas_start_mx) / self._zoom
            dy = (inp.mouse_y - self._drag_canvas_start_my) / self._zoom
            new_wx = self._drag_canvas_start_wx + dx
            new_wy = self._drag_canvas_start_wy + dy
            new_wx, new_wy = self._clamp_canvas_no_overlap(
                self._drag_canvas_id, new_wx, new_wy, all_canvases)
            self._canvas_panel_positions[self._drag_canvas_id] = [new_wx, new_wy]
            self._update_continuous_view_edit(key)
        else:
            self._dragging_canvas = False
            self._drag_canvas_id = 0
            ContinuousEditService.instance().commit(self._view_edit_key("canvas"))

    def _process_workspace_pan(self, ctx, inp):
        """Handle workspace panning via Space+LMB or MMB."""
        if inp.wants_pan and not self._dragging_canvas:
            if not self._is_panning:
                self._is_panning = True
                self._begin_continuous_view_edit("pan", "Pan UI Editor View")
            drag_btn = inp.pan_drag_button
            dx = ctx.get_mouse_drag_delta_x(drag_btn)
            dy = ctx.get_mouse_drag_delta_y(drag_btn)
            self._pan_x += dx
            self._pan_y += dy
            ctx.reset_mouse_drag_delta(drag_btn)
            self._update_continuous_view_edit(self._view_edit_key("pan"))
        else:
            if self._is_panning:
                ContinuousEditService.instance().commit(self._view_edit_key("pan"))
            self._is_panning = False

    def _process_ongoing_interactions(self, inp, foc_ref_w, foc_ref_h, focused_canvas):
        """Continue in-progress resize / rotate / drag each frame."""
        if self._resizing:
            if inp.lmb_down:
                if not self._apply_resize_suppressed(inp):
                    self._resizing = False
                    self._resize_handle_idx = -1
            else:
                self._finish_element_manipulation(commit=True)
                self._resizing = False
                self._resize_handle_idx = -1

        if self._rotating:
            if inp.lmb_down:
                if not self._apply_rotation_drag_suppressed(inp):
                    self._rotating = False
            else:
                self._finish_element_manipulation(commit=True)
                self._rotating = False

        if self._dragging:
            if inp.lmb_down:
                dx = (inp.mouse_x - self._drag_start_x) / self._zoom
                dy = (inp.mouse_y - self._drag_start_y) / self._zoom
                if inp.ctrl_down:
                    if abs(dx) >= abs(dy):
                        dy = 0.0
                    else:
                        dx = 0.0
                snap = self._drag_snap_step()
                new_vis_x = round((self._drag_elem_start_x + dx) / snap) * snap
                new_vis_y = round((self._drag_elem_start_y + dy) / snap) * snap
                if focused_canvas is not None:
                    new_vis_x, new_vis_y = self._apply_alignment_snapping(
                        focused_canvas, self._selected_element_comp,
                        new_vis_x, new_vis_y, foc_ref_w, foc_ref_h,
                    )
                if not self._apply_drag_suppressed(
                    new_vis_x, new_vis_y, foc_ref_w, foc_ref_h
                ):
                    self._dragging = False
            else:
                self._finish_element_manipulation(commit=True)
                self._dragging = False
                self._active_alignment_guides = []
        elif not self._resizing and not self._rotating:
            self._active_alignment_guides = []

    def _draw_canvases_and_hittest(self, ctx, all_canvases, inp, area):
        """Draw all canvas panels and hit-test for hovered elements.

        Returns ``(hovered_canvas_id, hovered_elem, hovered_all)``.
        """
        from Infernux.ui import UIText
        _tex_cache = _get_tex_cache()
        _get_tid = lambda tp: _tex_cache.get(self._engine, tp)

        area_min_x, area_min_y, area_max_x, area_max_y = area
        hovered_canvas_id = 0
        hovered_elem = None
        hovered_all: list = []
        HEADER_H = _metric(ctx, Theme.UI_EDITOR_CANVAS_HEADER_H)
        _PICK_TOL = _metric(ctx, 3.0) / self._zoom

        for canvas_go, canvas in all_canvases:
            go_id = canvas_go.id
            panel_pos = self._canvas_panel_positions.get(go_id, [0.0, 0.0])
            origin_x = area_min_x + panel_pos[0] * self._zoom
            origin_y = area_min_y + panel_pos[1] * self._zoom

            ref_w = float(canvas.reference_width)
            ref_h = float(canvas.reference_height)

            go_active = canvas_go.active_in_hierarchy
            canvas_enabled = getattr(canvas, 'enabled', True)
            is_active = go_active and canvas_enabled
            alpha_mult = 1.0 if is_active else Theme.UI_EDITOR_CANVAS_INACTIVE_ALPHA
            is_focused = (go_id == self._focused_canvas_id)

            canvas_bounds = self._compute_canvas_screen_bounds(
                origin_x, origin_y, ref_w, ref_h, HEADER_H, area)

            self._draw_canvas_chrome(ctx, canvas_go, ref_w, ref_h,
                                     is_active, is_focused, alpha_mult, canvas_bounds)

            hovered_canvas_id, hovered_elem, hovered_all = self._hittest_canvas_hover(
                inp, canvas, canvas_go, is_active, is_focused, origin_x, origin_y,
                ref_w, ref_h, _PICK_TOL, canvas_bounds,
                hovered_canvas_id, hovered_elem, hovered_all)

            if is_active:
                self._draw_canvas_elements(
                    ctx, canvas, origin_x, origin_y, ref_w, ref_h,
                    area, hovered_elem, _get_tid, UIText)

        # Fallback: pick elements on focused canvas outside canvas bounds
        if not hovered_elem and self._focused_canvas_id and inp.area_hovered and not self._dragging_canvas:
            hovered_canvas_id, hovered_elem, hovered_all = self._fallback_focused_pick(
                inp, all_canvases, area_min_x, area_min_y, _PICK_TOL,
                hovered_canvas_id, hovered_elem, hovered_all)

        return hovered_canvas_id, hovered_elem, hovered_all

    def _compute_canvas_screen_bounds(self, origin_x, origin_y, ref_w, ref_h, header_h, area):
        """Compute screen-space bounds for a canvas panel and its header."""
        area_min_x, area_min_y, area_max_x, area_max_y = area
        c_tl_x, c_tl_y = self._canvas_to_screen(0, 0, origin_x, origin_y)
        c_br_x, c_br_y = self._canvas_to_screen(ref_w, ref_h, origin_x, origin_y)
        c_tl_x = round(c_tl_x); c_tl_y = round(c_tl_y)
        c_br_x = round(c_br_x); c_br_y = round(c_br_y)
        h_tl_y = c_tl_y - header_h
        return (c_tl_x, c_tl_y, c_br_x, c_br_y, h_tl_y,
                max(c_tl_x, area_min_x), max(c_tl_y, area_min_y),
                min(c_br_x, area_max_x), min(c_br_y, area_max_y),
                max(c_tl_x, area_min_x), max(h_tl_y, area_min_y),
                min(c_br_x, area_max_x), min(c_tl_y, area_max_y))

    def _draw_canvas_chrome(self, ctx, canvas_go, ref_w, ref_h,
                            is_active, is_focused, alpha_mult, bounds):
        """Draw canvas header bar and background rectangle."""
        (c_tl_x, c_tl_y, c_br_x, c_br_y, h_tl_y,
         v_tl_x, v_tl_y, v_br_x, v_br_y,
         v_h_tl_x, v_h_tl_y, v_h_br_x, v_h_br_y) = bounds

        header_visible = (v_h_br_x > v_h_tl_x and v_h_br_y > v_h_tl_y)
        canvas_visible = (v_br_x > v_tl_x and v_br_y > v_tl_y)

        if header_visible:
            hdr_bg = Theme.UI_EDITOR_CANVAS_HEADER_BG_FOC if is_focused else Theme.UI_EDITOR_CANVAS_HEADER_BG
            ctx.draw_filled_rect(v_h_tl_x, v_h_tl_y, v_h_br_x, v_h_br_y,
                                 hdr_bg[0], hdr_bg[1], hdr_bg[2],
                                 hdr_bg[3] * alpha_mult, 0.0)
            label = f"{canvas_go.name}  {int(ref_w)}\u00d7{int(ref_h)}"
            if not is_active:
                label += "  (inactive)"
            tc = Theme.UI_EDITOR_CANVAS_HEADER_TEXT
            ctx.draw_text(
                          c_tl_x + _metric(ctx, 6.0),
                          h_tl_y + _metric(ctx, 3.0),
                          label,
                          tc[0], tc[1], tc[2], tc[3] * alpha_mult, 0.0)

        if canvas_visible:
            bg = Theme.UI_EDITOR_CANVAS_BG
            ctx.draw_filled_rect(v_tl_x, v_tl_y, v_br_x, v_br_y,
                                 bg[0], bg[1], bg[2], bg[3] * alpha_mult, 0.0)

    def _hittest_canvas_hover(self, inp, canvas, canvas_go, is_active, is_focused,
                              origin_x, origin_y, ref_w, ref_h, pick_tol, bounds,
                              hovered_canvas_id, hovered_elem, hovered_all):
        """Hit-test one canvas for hovered elements under the mouse."""
        if not inp.area_hovered or self._dragging_canvas:
            return hovered_canvas_id, hovered_elem, hovered_all

        (c_tl_x, c_tl_y, c_br_x, c_br_y, h_tl_y, *_rest) = bounds
        go_id = canvas_go.id

        if c_tl_x <= inp.mouse_x <= c_br_x and h_tl_y <= inp.mouse_y <= c_br_y:
            hovered_canvas_id = go_id
            if (is_active and c_tl_y <= inp.mouse_y <= c_br_y
                    and (is_focused or not self._focused_canvas_id)):
                cmx, cmy = self._screen_to_canvas(inp.mouse_x, inp.mouse_y, origin_x, origin_y)
                if 0.0 <= cmx <= ref_w and 0.0 <= cmy <= ref_h:
                    _all = canvas.raycast_all(cmx, cmy, pick_tol)
                    if _all:
                        hovered_all = _all
                        hovered_elem = _all[0]

        if is_focused and is_active and not hovered_elem:
            cmx, cmy = self._screen_to_canvas(inp.mouse_x, inp.mouse_y, origin_x, origin_y)
            _all = canvas.raycast_all(cmx, cmy, pick_tol)
            if _all:
                hovered_all = _all
                hovered_elem = _all[0]
                if not hovered_canvas_id:
                    hovered_canvas_id = go_id

        return hovered_canvas_id, hovered_elem, hovered_all

    def _draw_canvas_elements(self, ctx, canvas, origin_x, origin_y,
                              ref_w, ref_h, area, hovered_elem, _get_tid, UIText):
        """Draw all UI elements for one active canvas."""
        area_min_x, area_min_y, area_max_x, area_max_y = area
        elements = list(canvas.iter_ui_elements())

        selected_element = self._selected_element_comp
        selected_object_id = self._element_object_id(selected_element)
        hovered_object_id = self._element_object_id(hovered_elem)
        for elem in elements:
            if isinstance(elem, UIText):
                self._sync_text_layout(ctx, elem)
        clear_rect_cache(_pc())

        for elem in elements:
            elem_go = elem.game_object
            if elem_go is not None and not elem_go.active_in_hierarchy:
                continue
            if not getattr(elem, 'enabled', True):
                continue

            ex, ey, ew, eh = elem.get_visual_rect(ref_w, ref_h)
            base_x, base_y, base_w, base_h = elem.get_rect(ref_w, ref_h)
            s_x, s_y = self._canvas_to_screen(ex, ey, origin_x, origin_y)
            s_w = ew * self._zoom
            s_h = eh * self._zoom
            base_sx, base_sy = self._canvas_to_screen(base_x, base_y, origin_x, origin_y)
            base_sw = base_w * self._zoom
            base_sh = base_h * self._zoom

            s_x = round(s_x); s_y = round(s_y)
            s_w = round(s_w); s_h = round(s_h)

            element_object_id = self._element_object_id(elem)
            is_hovered = bool(element_object_id and element_object_id == hovered_object_id)
            is_selected = bool(element_object_id and element_object_id == selected_object_id)

            cx0 = max(s_x, area_min_x)
            cy0 = max(s_y, area_min_y)
            cx1 = min(s_x + s_w, area_max_x)
            cy1 = min(s_y + s_h, area_max_y)
            if cx1 <= cx0 or cy1 <= cy0:
                continue

            if not is_selected and is_hovered:
                ctx.draw_filled_rect(cx0, cy0, cx1, cy1,
                                     *Theme.UI_EDITOR_ELEMENT_HOVER, 0.0)
                ctx.draw_rect(cx0, cy0, cx1, cy1,
                              *Theme.UI_EDITOR_ELEMENT_SELECT[:3], 0.6, 1.0, 0.0)

            if not _ui_dispatch(
                elem, "editor",
                ctx=ctx,
                base_sx=base_sx, base_sy=base_sy,
                base_sw=base_sw, base_sh=base_sh,
                zoom=self._zoom,
                get_tex_id=_get_tid,
            ):
                tx = max(s_x + _metric(ctx, 2.0), area_min_x)
                ty = max(s_y + _metric(ctx, 2.0), area_min_y)
                if tx < area_max_x and ty < area_max_y:
                    ctx.draw_text(tx, ty,
                                  elem.type_name, *Theme.UI_EDITOR_FALLBACK_TEXT, 0.0)

    def _fallback_focused_pick(self, inp, all_canvases, area_min_x, area_min_y,
                               pick_tol, hovered_canvas_id, hovered_elem, hovered_all):
        """Pick elements on the focused canvas even outside its bounds."""
        for cgo, cv in all_canvases:
            if cgo.id != self._focused_canvas_id:
                continue
            if not cgo.active_in_hierarchy or not getattr(cv, 'enabled', True):
                break
            pp = self._canvas_panel_positions.get(cgo.id, [0.0, 0.0])
            foc_ox = area_min_x + pp[0] * self._zoom
            foc_oy = area_min_y + pp[1] * self._zoom
            cmx, cmy = self._screen_to_canvas(inp.mouse_x, inp.mouse_y, foc_ox, foc_oy)
            _all = cv.raycast_all(cmx, cmy, pick_tol)
            if _all:
                hovered_all = _all
                hovered_elem = _all[0]
                if not hovered_canvas_id:
                    hovered_canvas_id = cgo.id
            break
        return hovered_canvas_id, hovered_elem, hovered_all

    def _draw_selection_overlay(self, ctx, focused_canvas,
                                foc_origin_x, foc_origin_y,
                                foc_ref_w, foc_ref_h, area):
        """Draw selection box, handles, and rotation affordance."""
        area_min_x, area_min_y, area_max_x, area_max_y = area
        self._handle_positions = []
        self._draw_alignment_guides(ctx, foc_origin_x, foc_origin_y)
        if self._selected_element_comp is not None and focused_canvas is not None:
            sel = self._selected_element_comp
            self._selection_geometry = self._get_oriented_box_screen(
                sel, foc_ref_w, foc_ref_h, foc_origin_x, foc_origin_y)
            corners = self._selection_geometry['corners']
            _HS = _metric(ctx, Theme.UI_EDITOR_HANDLE_SIZE)
            self._handle_positions = [(cx - _HS, cy - _HS) for cx, cy in corners]

            for idx in range(4):
                ax, ay = corners[idx]
                bx, by = corners[(idx + 1) % 4]
                ctx.draw_line(ax, ay, bx, by,
                              *Theme.UI_EDITOR_ELEMENT_SELECT,
                              _metric(ctx, Theme.UI_EDITOR_SELECT_LINE_W))

            top_mid_x, top_mid_y = self._selection_geometry['top_mid']
            rotate_x, rotate_y = self._selection_geometry['rotate_handle']
            ctx.draw_line(top_mid_x, top_mid_y, rotate_x, rotate_y,
                          *Theme.UI_EDITOR_ELEMENT_SELECT,
                          _metric(ctx, Theme.UI_EDITOR_ROTATE_LINE_W))
            rotate_radius = _metric(ctx, Theme.UI_EDITOR_ROTATE_RADIUS)
            ctx.draw_filled_circle(rotate_x, rotate_y, rotate_radius,
                                   *Theme.UI_EDITOR_HANDLE_COLOR, 0)
            ctx.draw_circle(rotate_x, rotate_y, rotate_radius,
                            *Theme.UI_EDITOR_ELEMENT_SELECT, _metric(ctx, 1.0), 0)

            hs2 = _HS * 2
            for px, py in self._handle_positions:
                h_x1 = px + hs2
                h_y1 = py + hs2
                if (h_x1 > area_min_x and h_y1 > area_min_y
                        and px < area_max_x and py < area_max_y):
                    ctx.draw_filled_rect(px, py, h_x1, h_y1,
                                         *Theme.UI_EDITOR_HANDLE_COLOR, 0.0)
                    ctx.draw_rect(px, py, h_x1, h_y1,
                                  *Theme.UI_EDITOR_ELEMENT_SELECT,
                                  _metric(ctx, 1.0), 0.0)
        else:
            self._selection_geometry = None

    def _begin_element_interaction(self, inp, all_canvases, hovered_canvas_id,
                                    hovered_elem, foc_origin_x, foc_origin_y,
                                    area_min_x, area_min_y):
        """Start resize / rotate / drag on the selected element, or select a new one."""
        foc_origin_x, foc_origin_y = self._get_focused_canvas_origin(
            area_min_x, area_min_y, all_canvases)
        _, focused_canvas = self._get_focused_canvas(all_canvases)
        if focused_canvas is not None:
            foc_ref_w = float(focused_canvas.reference_width)
            foc_ref_h = float(focused_canvas.reference_height)
        else:
            foc_ref_w = foc_ref_h = 1.0

        clicked_kind, clicked_handle = self._hit_test_handle(
            inp.ctx, inp.mouse_x, inp.mouse_y
        )
        if clicked_kind in ("corner", "edge") and self._selected_element_comp is not None:
            sel = self._selected_element_comp
            if not self._begin_element_manipulation("resize", sel):
                return
            self._resizing = True
            self._resize_handle_idx = clicked_handle
            self._resize_start_mx = inp.mouse_x
            self._resize_start_my = inp.mouse_y
            if not self._mutate_element_manipulation(
                lambda: self._prepare_resize_element(sel)
            ):
                self._resizing = False
                self._resize_handle_idx = -1
                return
            self._resize_start_rect = sel.get_rect(foc_ref_w, foc_ref_h)
            self._resize_start_rotation = float(getattr(sel, 'rotation', 0.0))
            self._resize_start_corners = sel.get_rotated_corners(foc_ref_w, foc_ref_h)
        elif clicked_kind == "rotate" and self._selected_element_comp is not None:
            sel = self._selected_element_comp
            if not self._begin_element_manipulation("rotate", sel):
                return
            self._rotating = True
            self._dragging = False
            self._resizing = False
            self._resize_handle_idx = -1
            center_x, center_y = self._selection_geometry['center']
            self._rotate_center_sx = center_x
            self._rotate_center_sy = center_y
            self._rotate_start_angle = math.degrees(
                math.atan2(inp.mouse_y - center_y, inp.mouse_x - center_x))
            self._rotate_start_rotation = float(getattr(sel, 'rotation', 0.0))
        elif clicked_kind == "inside" and self._selected_element_comp is not None:
            sel = self._selected_element_comp
            if not self._begin_element_manipulation("drag", sel):
                return
            self._dragging = True
            self._drag_start_x = inp.mouse_x
            self._drag_start_y = inp.mouse_y
            drag_x, drag_y, _, _ = sel.get_visual_rect(foc_ref_w, foc_ref_h)
            self._drag_elem_start_x = drag_x
            self._drag_elem_start_y = drag_y
        elif hovered_elem is not None:
            self._select_element(hovered_elem)
            if not self._begin_element_manipulation("drag", hovered_elem):
                return
            self._dragging = True
            self._drag_start_x = inp.mouse_x
            self._drag_start_y = inp.mouse_y
            drag_x, drag_y, _, _ = hovered_elem.get_visual_rect(foc_ref_w, foc_ref_h)
            self._drag_elem_start_x = drag_x
            self._drag_elem_start_y = drag_y
        elif hovered_canvas_id:
            for cgo, _cv in all_canvases:
                if cgo.id == hovered_canvas_id:
                    self._select_canvas(cgo)
                    break
        else:
            self._select_element(None)

    def _handle_canvas_click(
        self, ctx, inp, all_canvases,
        hovered_canvas_id, hovered_elem, hovered_all,
        foc_origin_x, foc_origin_y,
        area_min_x, area_min_y,
    ):
        """Dispatch canvas-area click: element cycling, canvas drag, resize/rotate/move."""
        _cycle_trigger = (
            (inp.lmb_double_clicked or (inp.ctrl_down and inp.lmb_clicked))
            and not inp.space_down and hovered_all and len(hovered_all) > 1
        )
        if _cycle_trigger:
            _CYCLE_TOL = 3.0 / self._zoom
            cx, cy = self._screen_to_canvas(inp.mouse_x, inp.mouse_y,
                                            foc_origin_x, foc_origin_y)
            lx, ly = self._pick_cycle_last_canvas_pos
            same_spot = (abs(cx - lx) < _CYCLE_TOL and abs(cy - ly) < _CYCLE_TOL)
            candidate_ids = tuple(self._element_object_id(item) for item in hovered_all)
            previous_ids = tuple(
                self._element_object_id(item) for item in self._pick_cycle_candidates
            )
            if same_spot and previous_ids == candidate_ids:
                self._pick_cycle_index = (self._pick_cycle_index + 1) % len(hovered_all)
            else:
                self._pick_cycle_candidates = hovered_all
                selected_id = self._element_object_id(self._selected_element_comp)
                cur_idx = next(
                    (
                        index
                        for index, element in enumerate(hovered_all)
                        if selected_id
                        and self._element_object_id(element) == selected_id
                    ),
                    -1,
                )
                self._pick_cycle_index = (
                    (cur_idx + 1) % len(hovered_all) if cur_idx >= 0 else 0
                )
            self._pick_cycle_last_canvas_pos = (cx, cy)
            self._select_element(hovered_all[self._pick_cycle_index])

        elif inp.lmb_clicked and not inp.space_down:
            clicked_canvas_header = None
            if hovered_canvas_id and not hovered_elem:
                for cgo, cv in all_canvases:
                    if cgo.id != hovered_canvas_id:
                        continue
                    pp = self._canvas_panel_positions.get(cgo.id, [0.0, 0.0])
                    ox = area_min_x + pp[0] * self._zoom
                    oy = area_min_y + pp[1] * self._zoom
                    ct_y = round(oy + self._pan_y)
                    in_header = inp.mouse_y < ct_y
                    canvas_empty = not any(cv.iter_ui_elements())
                    if in_header or canvas_empty:
                        clicked_canvas_header = cgo
                        self._dragging_canvas = True
                        self._drag_canvas_id = cgo.id
                        self._drag_canvas_start_mx = inp.mouse_x
                        self._drag_canvas_start_my = inp.mouse_y
                        self._drag_canvas_start_wx = pp[0]
                        self._drag_canvas_start_wy = pp[1]
                    break

            if clicked_canvas_header is not None:
                self._select_canvas(clicked_canvas_header)
            elif not self._dragging_canvas:
                from Infernux.engine.interaction import EditorInteractionCore

                core = EditorInteractionCore.instance()
                if core is None:
                    raise RuntimeError("UI Editor interaction requires Interaction Core")
                with core.user_action("Select UI Editor Target"):
                    if hovered_canvas_id:
                        self._set_focused_canvas_id(
                            hovered_canvas_id,
                            record_history=True,
                            description="Focus UI Canvas",
                        )
                    self._begin_element_interaction(
                        inp, all_canvases, hovered_canvas_id, hovered_elem,
                        foc_origin_x, foc_origin_y, area_min_x, area_min_y)

    # ------------------------------------------------------------------
    # Snap helpers
    # ------------------------------------------------------------------

    def _drag_snap_step(self) -> float:
        """Return a 'nice' snap step in canvas pixels based on current zoom.

        Uses Theme.UI_EDITOR_SNAP_TABLE for configurable thresholds.
        """
        z = self._zoom
        for threshold, step in Theme.UI_EDITOR_SNAP_TABLE:
            if z >= threshold:
                return step
        return Theme.UI_EDITOR_SNAP_DEFAULT

    # ------------------------------------------------------------------
    # Resize handle helpers
    # ------------------------------------------------------------------

    def _hit_test_handle(self, ctx: InxGUIContext, mx: float, my: float):
        """Return (kind, index) for corner, edge, or rotate zones."""
        geom = getattr(self, '_selection_geometry', None)
        if geom is None:
            return None, -1
        hs = _metric(ctx, Theme.UI_EDITOR_HANDLE_SIZE + 2.0)

        for idx, (cx, cy) in enumerate(geom['corners']):
            if abs(mx - cx) <= hs and abs(my - cy) <= hs:
                return "corner", idx

        rotate_x, rotate_y = geom['rotate_handle']
        if math.hypot(mx - rotate_x, my - rotate_y) <= _metric(
            ctx, Theme.UI_EDITOR_ROTATE_HIT_R
        ):
            return "rotate", -1

        inside = self._point_in_quad(mx, my, geom['corners'])
        edge_tol = max(_metric(ctx, Theme.UI_EDITOR_EDGE_HIT_TOL), hs)
        edges = [(0, 1, 4), (1, 2, 7), (2, 3, 5), (3, 0, 6)]
        for a_idx, b_idx, handle_idx in edges:
            ax, ay = geom['corners'][a_idx]
            bx, by = geom['corners'][b_idx]
            if self._distance_point_to_segment(mx, my, ax, ay, bx, by) <= edge_tol:
                return "edge", handle_idx

        if inside:
            return "inside", -1

        return None, -1

    def _update_hover_cursor(self, ctx: InxGUIContext, area_hovered: bool, mouse_x: float, mouse_y: float):
        if not area_hovered or self._selected_element_comp is None:
            return
        kind, idx = self._hit_test_handle(ctx, mouse_x, mouse_y)
        if kind == "corner":
            ctx.set_mouse_cursor(ImGuiMouseCursor.ResizeNWSE if idx in (0, 3) else ImGuiMouseCursor.ResizeNESW)
        elif kind == "edge":
            ctx.set_mouse_cursor(ImGuiMouseCursor.ResizeNS if idx in (4, 5) else ImGuiMouseCursor.ResizeEW)
        elif kind == "rotate":
            ctx.set_mouse_cursor(ImGuiMouseCursor.Hand)

    # ------------------------------------------------------------------
    # Core-owned continuous element manipulation
    # ------------------------------------------------------------------

    @staticmethod
    def _component_commands():
        from Infernux.engine.interaction import ComponentCommandService

        return ComponentCommandService.require()

    @staticmethod
    def _element_manipulation_snapshot(kind: str, elem) -> dict:
        if kind == "drag":
            fields = ("x", "y")
        elif kind == "rotate":
            fields = ("rotation",)
        elif kind == "resize":
            fields = ("resize_mode", "x", "y", "width", "height")
        else:
            raise ValueError(f"unsupported UI element manipulation '{kind}'")
        return {
            field: getattr(elem, field)
            for field in fields
            if hasattr(elem, field)
        }

    @staticmethod
    def _element_manipulation_description(kind: str) -> str:
        return {
            "drag": "Move UI Element",
            "resize": "Resize UI Element",
            "rotate": "Rotate UI Element",
        }[kind]

    def _release_element_manipulation_ownership(self, key: str) -> None:
        if self._active_element_edit_key == key:
            self._active_element_edit_key = ""
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.capture_owner_id == "ui_editor.manipulation":
            focus.set_capture_owner("")

    def _restore_element_manipulation(self, commands, elem, state: dict) -> None:
        scope = commands.suppress_replay() if commands.can_record() else _nullcontext()
        with scope:
            for field, value in state.items():
                setattr(elem, field, value)

    def _begin_element_manipulation(self, kind: str, elem) -> bool:
        from Infernux.engine.interaction import EditorInteractionCore
        commands_service = self._component_commands()
        core = EditorInteractionCore.instance()
        if (
            elem is None
            or not commands_service.can_record()
            or core is None
            or bool(core.focus.snapshot.capture_owner_id)
        ):
            return False

        self._finish_element_manipulation(commit=True)
        initial = self._element_manipulation_snapshot(kind, elem)
        object_id = int(getattr(getattr(elem, "game_object", None), "id", 0) or 0)
        key = f"ui_editor:{kind}:{object_id or id(elem)}"
        description = self._element_manipulation_description(kind)
        before_context = core.capture_context()

        def on_commit(session):
            current = self._element_manipulation_snapshot(kind, elem)
            if not commands_service.can_record():
                return False
            changes = [
                (elem, field, old_value, current[field], f"Set {field}")
                for field, old_value in session.initial_value.items()
                if field in current and old_value != current[field]
            ]
            # Input capture is transient gesture state. It must never be stored
            # in the command's replayable after-context.
            self._release_element_manipulation_ownership(key)
            if changes:
                return commands_service.record_applied_property_changes(
                    changes,
                    description=description,
                    before_context=before_context,
                    after_context=core.capture_context(),
                )
            return True

        def on_cancel(session):
            self._restore_element_manipulation(
                commands_service, elem, session.initial_value
            )
            self._release_element_manipulation_ownership(key)

        core.continuous_edits.begin(
            key,
            owner_id=self.WINDOW_TYPE_ID,
            document_id=core.focus.snapshot.active_document_id,
            description=description,
            initial_value=initial,
            on_commit=on_commit,
            on_cancel=on_cancel,
        )
        self._active_element_edit_key = key
        core.focus.set_capture_owner("ui_editor.manipulation")
        return True

    def _mutate_element_manipulation(self, mutation) -> bool:
        from Infernux.engine.interaction import EditorInteractionCore

        key = self._active_element_edit_key
        core = EditorInteractionCore.instance()
        commands = self._component_commands()
        if (
            not key
            or core is None
            or core.continuous_edits.get(key) is None
            or not commands.can_record()
        ):
            self._finish_element_manipulation(commit=False)
            return False
        with commands.suppress_replay():
            mutation()
        elem = self._selected_element_comp
        session = core.continuous_edits.get(key)
        if elem is None or session is None:
            self._finish_element_manipulation(commit=False)
            return False
        return core.continuous_edits.update(
            key,
            self._element_manipulation_snapshot(
                str(key).split(":", 2)[1], elem
            ),
        )

    def _finish_element_manipulation(self, *, commit: bool) -> bool:
        from Infernux.engine.interaction import EditorInteractionCore

        key = self._active_element_edit_key
        if not key:
            return False
        core = EditorInteractionCore.instance()
        if core is None:
            self._release_element_manipulation_ownership(key)
            return False
        elem = self._selected_element_comp
        session = core.continuous_edits.get(key)
        if commit and elem is not None and session is not None:
            kind = str(key).split(":", 2)[1]
            core.continuous_edits.update(
                key, self._element_manipulation_snapshot(kind, elem)
            )
        handled = (
            core.continuous_edits.commit(key)
            if commit
            else core.continuous_edits.cancel(key)
        )
        self._release_element_manipulation_ownership(key)
        return handled

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Creation helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Zoom helpers
    # ------------------------------------------------------------------

    def _fit_zoom(self, ctx: InxGUIContext, canvas):
        """Fit all canvases into the available area."""
        avail_w = ctx.get_content_region_avail_width()
        avail_h = ctx.get_content_region_avail_height()
        if avail_w < 1 or avail_h < 1:
            return
        # Compute bounding box of all canvases in workspace space
        all_canvases = self._get_all_canvases()
        if not all_canvases:
            return
        self._ensure_canvas_layout(all_canvases)
        before = self._capture_view_state()
        min_x = float('inf')
        min_y = float('inf')
        max_x = float('-inf')
        max_y = float('-inf')
        for cgo, cv in all_canvases:
            pos = self._canvas_panel_positions.get(cgo.id, [0.0, 0.0])
            wx, wy = pos[0], pos[1]
            rw = float(cv.reference_width)
            rh = float(cv.reference_height)
            hdr_h = _metric(ctx, Theme.UI_EDITOR_CANVAS_HEADER_H)
            min_x = min(min_x, wx)
            min_y = min(min_y, wy - hdr_h)
            max_x = max(max_x, wx + rw)
            max_y = max(max_y, wy + rh)
        bbox_w = max_x - min_x
        bbox_h = max_y - min_y
        if bbox_w < 1 or bbox_h < 1:
            return
        margin = _metric(ctx, Theme.UI_EDITOR_FIT_MARGIN)
        zoom_w = (avail_w - margin) / bbox_w
        zoom_h = (avail_h - margin) / bbox_h
        self._zoom = max(Theme.UI_EDITOR_MIN_ZOOM,
                         min(Theme.UI_EDITOR_MAX_ZOOM, min(zoom_w, zoom_h)))
        # Center all canvases
        self._pan_x = (avail_w - bbox_w * self._zoom) / 2 - min_x * self._zoom
        self._pan_y = (avail_h - bbox_h * self._zoom) / 2 - min_y * self._zoom
        self._commit_view_state(before, "Fit UI Editor View")
