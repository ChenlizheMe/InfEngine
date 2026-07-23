"""SceneViewOverlaysMixin — extracted from SceneViewPanel."""
from __future__ import annotations

"""
Unity-style Scene View panel with 3D viewport and camera controls.
"""

import math
import os
from Infernux.lib import InxGUIContext, InputManager
from Infernux.engine.i18n import t
from Infernux.engine.texture_task_bridge import texture_stamp, query_or_schedule_texture
from .editor_panel import EditorPanel
from .closable_panel import ClosablePanel
from .panel_registry import editor_panel
from .theme import Theme, ImGuiCol, ImGuiStyleVar
from .viewport_utils import ViewportInfo, capture_viewport_info
from . import imgui_keys as _keys
import Infernux.resources as _resources

# Tool mode constants — imported from scene_view_panel
from .scene_view_panel import TOOL_NONE, TOOL_TRANSLATE, TOOL_ROTATE, TOOL_SCALE

# Gizmo handle IDs — must match C++ EditorTools constants
from Infernux.debug import Debug
from Infernux.lib._Infernux import (
    GIZMO_X_AXIS_ID,
    GIZMO_Y_AXIS_ID,
    GIZMO_Z_AXIS_ID,
    GIZMO_XY_PLANE_ID,
    GIZMO_XZ_PLANE_ID,
    GIZMO_YZ_PLANE_ID,
)


class SceneViewOverlaysMixin:
    """SceneViewOverlaysMixin method group for SceneViewPanel."""

    def _render_overlays_and_shortcuts(
        self,
        ctx,
        vp,
        cursor_start_x,
        cursor_start_y,
        scene_width,
        scene_height,
        delta_time,
    ):
        """Draw gizmo/pos overlays, prefab banner, and handle tool/camera shortcuts.

        Returns True if an overlay element is hovered.
        """
        ctx.set_cursor_pos_x(cursor_start_x + 8)
        ctx.set_cursor_pos_y(cursor_start_y + 8)
        overlay_hovered = self._draw_gizmo_overlay(ctx)

        # Prefab mode overlay banner
        from Infernux.engine.scene_manager import SceneFileManager
        scene_file_manager = SceneFileManager.instance()
        if scene_file_manager and scene_file_manager.is_prefab_mode:
            ctx.set_cursor_pos_x(cursor_start_x + scene_width / 2.0 - 60.0)
            ctx.set_cursor_pos_y(cursor_start_y + 8.0)

            # Use a prominent color for the exit button
            ctx.push_style_color(ImGuiCol.Button, *Theme.PREFAB_BTN_NORMAL)
            ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.PREFAB_BTN_HOVERED)
            ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.PREFAB_BTN_ACTIVE)

            exit_label = t("scene_view.exit_prefab_mode")
            exit_clicked = ctx.button(exit_label)
            record_item = getattr(ctx, "record_semantic_item", None)
            if callable(record_item):
                record_item(
                    "button",
                    exit_label,
                    True,
                    "scene_view.prefab.exit",
                )
            if exit_clicked:
                scene_file_manager.exit_prefab_mode_with_undo()
            if ctx.is_item_hovered() and ctx.is_mouse_button_down(0):
                overlay_hovered = True

            ctx.pop_style_color(3)

        self._draw_pos_overlay(ctx, vp)
        self._tick_particle_preview(delta_time)
        overlay_hovered = self._draw_particle_preview_overlay(
            ctx,
            cursor_start_x,
            cursor_start_y,
            scene_width,
            scene_height,
        ) or overlay_hovered

        # Unity-style tool switching shortcuts (Q/W/E/R)
        if not ctx.want_text_input() and not ctx.is_mouse_button_down(1):
            if ctx.is_key_pressed(self.KEY_Q):
                self._set_tool_mode(TOOL_NONE)
            elif ctx.is_key_pressed(self.KEY_W):
                self._set_tool_mode(TOOL_TRANSLATE)
            elif ctx.is_key_pressed(self.KEY_E):
                self._set_tool_mode(TOOL_ROTATE)
            elif ctx.is_key_pressed(self.KEY_R):
                self._set_tool_mode(TOOL_SCALE)

            ctrl = ctx.is_key_down(_keys.KEY_LEFT_CTRL) or ctx.is_key_down(_keys.KEY_RIGHT_CTRL)
            if ctrl and ctx.is_key_pressed(_keys.KEY_F):
                self._align_object_to_camera()

        return overlay_hovered

    @staticmethod
    def _particle_component_from_object(game_object):
        if game_object is None or not hasattr(game_object, "get_py_components"):
            return None
        from Infernux.components import ParticleSystem

        try:
            return next(
                (
                    component
                    for component in (game_object.get_py_components() or ())
                    if isinstance(component, ParticleSystem)
                ),
                None,
            )
        except (ReferenceError, RuntimeError):
            return None

    def _is_particle_preview_edit_mode(self) -> bool:
        manager = self._play_mode_manager
        if manager is None:
            from Infernux.engine.play_mode import PlayModeManager

            manager = PlayModeManager.instance()
        return manager is None or bool(manager.is_edit_mode)

    @staticmethod
    def _particle_preview_is_live(component, game_object) -> bool:
        if component is None or game_object is None:
            return False
        try:
            owner = component.game_object
            return owner is not None and int(owner.id) == int(game_object.id)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            return False

    def _discard_invalid_particle_preview(self) -> None:
        component = self._particle_preview_component
        if component is not None:
            try:
                component._remove_native_batch()
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        self._particle_preview_component = None
        self._particle_preview_object = None
        self._particle_preview_playing = False
        self._particle_preview_prepared = False

    def _restore_particle_preview_selection(self) -> None:
        if not self._engine:
            return
        try:
            from Infernux.lib import SceneManager

            object_id = int(self._engine.get_selected_object_id() or 0)
            scene = SceneManager.instance().get_active_scene()
            selected = scene.find_by_id(object_id) if scene and object_id else None
        except (AttributeError, ReferenceError, RuntimeError):
            selected = None
        self._on_particle_preview_selection(selected)

    def _on_particle_preview_selection(self, game_object) -> None:
        component = self._particle_component_from_object(game_object)
        if component is self._particle_preview_component and self._particle_preview_is_live(
            component, game_object
        ):
            return
        previous = self._particle_preview_component
        if previous is not None:
            try:
                previous.editor_preview_pause()
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        self._particle_preview_component = component
        self._particle_preview_object = game_object if component is not None else None
        self._particle_preview_playing = component is not None
        self._particle_preview_prepared = False
        if component is not None and self._is_particle_preview_edit_mode():
            try:
                self._particle_preview_prepared = bool(component.editor_preview_begin())
                self._particle_preview_playing = self._particle_preview_prepared
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
                Debug.log_suppressed("scene_view.particle_preview.select", exc)
                self._particle_preview_playing = False

    def _release_particle_preview_selection(self) -> None:
        component = self._particle_preview_component
        if component is not None:
            try:
                component.editor_preview_end()
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        self._particle_preview_component = None
        self._particle_preview_object = None
        self._particle_preview_playing = False
        self._particle_preview_prepared = False

    def _tick_particle_preview(self, delta_time: float) -> None:
        component = self._particle_preview_component
        if component is None:
            return
        if not self._particle_preview_is_live(
            component, self._particle_preview_object
        ):
            self._discard_invalid_particle_preview()
            return
        if not self._is_particle_preview_edit_mode():
            if self._particle_preview_prepared:
                try:
                    component.editor_preview_end()
                except (AttributeError, ReferenceError, RuntimeError):
                    pass
                self._particle_preview_prepared = False
            return
        try:
            if not self._particle_preview_prepared and self._particle_preview_playing:
                self._particle_preview_prepared = bool(component.editor_preview_begin())
                self._particle_preview_playing = self._particle_preview_prepared
            if self._particle_preview_prepared and self._particle_preview_playing:
                component.editor_preview_update(delta_time, self._particle_preview_speed)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
            if not self._particle_preview_is_live(
                component, self._particle_preview_object
            ):
                self._discard_invalid_particle_preview()
                return
            Debug.log_suppressed("scene_view.particle_preview.tick", exc)
            self._particle_preview_prepared = False
            self._particle_preview_playing = False

    def _draw_particle_preview_overlay(
        self,
        ctx: InxGUIContext,
        cursor_start_x: float,
        cursor_start_y: float,
        scene_width: float,
        scene_height: float,
    ) -> bool:
        component = self._particle_preview_component
        if component is None or not self._is_particle_preview_edit_mode():
            return False

        width = min(260.0, max(210.0, scene_width - 24.0))
        height = 76.0
        ctx.set_cursor_pos_x(cursor_start_x + scene_width - width - 12.0)
        ctx.set_cursor_pos_y(cursor_start_y + scene_height - height - 12.0)
        ctx.push_style_color(ImGuiCol.ChildBg, 0.06, 0.06, 0.065, 0.94)
        ctx.push_style_color(ImGuiCol.Border, 0.30, 0.30, 0.32, 0.95)
        ctx.push_style_var_float(ImGuiStyleVar.ChildRounding, 6.0)
        visible = ctx.begin_child("##particle_preview_controls", width, height, True)
        try:
            hovered = bool(ctx.is_window_hovered())
            if not visible:
                return hovered
            semantic_capture = bool(getattr(ctx, "semantic_capture_enabled", True))
            record_item = getattr(ctx, "record_semantic_item", None)
            speed = float(
                ctx.drag_float(
                    f"{t('particle_preview.speed')}##particle_preview_speed",
                    self._particle_preview_speed,
                    0.05,
                    0.05,
                    4.0,
                )
            )
            self._particle_preview_speed = min(4.0, max(0.05, speed))
            if semantic_capture and callable(record_item):
                record_item(
                    "particle_preview_speed",
                    t("particle_preview.speed"),
                    True,
                    "scene_view.particle_preview.speed",
                    float_value=self._particle_preview_speed,
                )
            if self._particle_preview_playing:
                pause_label = t("particle_preview.pause")
                pause_clicked = ctx.button(pause_label, width=72.0)
                if semantic_capture and callable(record_item):
                    record_item(
                        "button",
                        pause_label,
                        True,
                        "scene_view.particle_preview.pause",
                    )
                if pause_clicked:
                    try:
                        component.editor_preview_pause()
                    finally:
                        self._particle_preview_playing = False
            else:
                play_label = t("particle_preview.play")
                play_clicked = ctx.button(play_label, width=72.0)
                if semantic_capture and callable(record_item):
                    record_item(
                        "button",
                        play_label,
                        True,
                        "scene_view.particle_preview.play",
                    )
                if play_clicked:
                    try:
                        self._particle_preview_prepared = bool(
                            component.editor_preview_play()
                        )
                        self._particle_preview_playing = self._particle_preview_prepared
                    except (AttributeError, ReferenceError, RuntimeError):
                        self._particle_preview_prepared = False
            ctx.same_line(0, 8.0)
            stop_label = t("particle_preview.stop")
            stop_clicked = ctx.button(stop_label, width=72.0)
            if semantic_capture and callable(record_item):
                record_item(
                    "button",
                    stop_label,
                    True,
                    "scene_view.particle_preview.stop",
                )
            if stop_clicked:
                try:
                    component.editor_preview_stop()
                finally:
                    self._particle_preview_playing = False
                    self._particle_preview_prepared = False
            return hovered or bool(ctx.is_item_hovered())
        finally:
            ctx.end_child()
            ctx.pop_style_var()
            ctx.pop_style_color(2)

    def _draw_gizmo_overlay(self, ctx: InxGUIContext) -> bool:
        """Draw the top-left gizmo controls and return whether they are hovered."""
        hovered = self._draw_coord_space_dropdown(ctx)
        # Measure the combo height so tool buttons match exactly
        combo_h = ctx.get_item_rect_max_y() - ctx.get_item_rect_min_y()
        ctx.same_line(0, Theme.SCENE_GIZMO_TOOL_BTN_GAP)
        hovered = self._draw_tool_mode_buttons(ctx, combo_h) or hovered
        return hovered

    def _draw_coord_space_dropdown(self, ctx: InxGUIContext) -> bool:
        """Draw Global/Local coordinate-space dropdown in the top-left corner."""
        _SPACE_LABELS = [t("scene_view.global"), t("scene_view.local")]
        ctx.push_id_str("coord_space_dropdown")
        # Style the combo to look like a semi-transparent overlay control
        ctx.push_style_color(ImGuiCol.FrameBg, *Theme.SCENE_OVERLAY_COMBO_BG)
        ctx.push_style_color(ImGuiCol.FrameBgHovered, *Theme.SCENE_OVERLAY_COMBO_HOVER)
        ctx.push_style_color(ImGuiCol.FrameBgActive, *Theme.SCENE_OVERLAY_COMBO_ACTIVE)
        ctx.push_style_var_float(ImGuiStyleVar.FrameRounding, Theme.SCENE_OVERLAY_ROUNDING)
        ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, Theme.SCENE_OVERLAY_BORDER_SIZE)
        ctx.set_next_item_width(Theme.SCENE_COORD_DROPDOWN_W)
        new_val = ctx.combo("##coord_space", self._coord_space, _SPACE_LABELS)
        hovered = ctx.is_item_hovered()
        ctx.pop_style_var(2)
        ctx.pop_style_color(3)
        if new_val != self._coord_space:
            self._coord_space = new_val
            # Sync local mode to C++ so gizmo visuals align to object rotation
            if self._engine:
                self._engine.set_editor_tool_local_mode(self._coord_space == 1)
        ctx.pop_id()
        return hovered

    def _ensure_tool_icons(self):
        """Lazily upload tool icon textures to GPU."""
        if not self._engine:
            return
        native = self._engine.get_native_engine() if hasattr(self._engine, 'get_native_engine') else self._engine
        if native is None:
            return
        _ICON_MAP = {
            TOOL_NONE:      "tool_none.png",
            TOOL_TRANSLATE: "tool_move.png",
            TOOL_ROTATE:    "tool_rotate.png",
            TOOL_SCALE:     "tool_scale.png",
        }
        all_ready = True
        for mode, filename in _ICON_MAP.items():
            icon_path = os.path.join(_resources.file_type_icons_dir, filename)
            if not os.path.isfile(icon_path):
                all_ready = False
                continue
            stamp = texture_stamp(icon_path, "scene_tool_icon")
            if stamp == 0:
                all_ready = False
                continue
            tid, _, _ = query_or_schedule_texture(
                native,
                f"toolicon|{filename}",
                icon_path,
                int(stamp),
                nearest=False,
                srgb=False,
            )
            if tid != 0:
                self._tool_icon_ids[mode] = tid
            else:
                all_ready = False
        self._tool_icons_loaded = all_ready

    def _draw_tool_mode_buttons(self, ctx: InxGUIContext, combo_h: float = 20.0) -> bool:
        """Draw horizontally aligned gizmo-tool icon buttons matching the combo height."""
        self._ensure_tool_icons()
        items = [
            (TOOL_NONE,      t("scene_view.tool_select"), "##tool_none"),
            (TOOL_TRANSLATE, t("scene_view.tool_move"),   "##tool_move"),
            (TOOL_ROTATE,    t("scene_view.tool_rotate"), "##tool_rotate"),
            (TOOL_SCALE,     t("scene_view.tool_scale"),  "##tool_scale"),
        ]
        pad = Theme.SCENE_GIZMO_TOOL_BTN_PAD
        icon_size = max(combo_h - pad[1] * 2, 8.0)
        gap = Theme.SCENE_GIZMO_TOOL_BTN_GAP
        hovered = False
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *pad)
        ctx.push_style_var_float(ImGuiStyleVar.FrameRounding, Theme.SCENE_OVERLAY_ROUNDING)
        for i, (mode, label, btn_id) in enumerate(items):
            if i > 0:
                ctx.same_line(0, gap)
            active = (self._gizmo_tool_mode == mode)
            if active:
                ctx.push_style_color(ImGuiCol.Button, 235.0 / 255.0, 87.0 / 255.0, 87.0 / 255.0, 0.95)
                ctx.push_style_color(ImGuiCol.ButtonHovered, 1.0, 107.0 / 255.0, 107.0 / 255.0, 1.0)
                ctx.push_style_color(ImGuiCol.ButtonActive, 220.0 / 255.0, 67.0 / 255.0, 67.0 / 255.0, 1.0)
            else:
                ctx.push_style_color(ImGuiCol.Button, *Theme.SCENE_OVERLAY_COMBO_BG)
                ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.SCENE_OVERLAY_COMBO_HOVER)
                ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.SCENE_OVERLAY_COMBO_ACTIVE)
            tex_id = self._tool_icon_ids.get(mode, 0)
            clicked = False
            if tex_id != 0:
                clicked = ctx.image_button(btn_id, tex_id, icon_size, icon_size)
            else:
                clicked = ctx.button(label, lambda m=mode: self._set_tool_mode(m),
                                     width=combo_h, height=combo_h)
            if clicked:
                self._set_tool_mode(mode)
            hovered = ctx.is_item_hovered() or hovered
            ctx.pop_style_color(3)
        ctx.pop_style_var(2)
        return hovered

    def _draw_pos_overlay(self, ctx: InxGUIContext, vp: ViewportInfo):
        """Draw a Unity-style orientation gizmo in the top-right corner."""
        if not self._engine:
            return
        self._draw_orientation_gizmo(ctx, vp)

    def _draw_orientation_gizmo(self, ctx: InxGUIContext, vp: ViewportInfo):
        """Draw orientation gizmo with clickable axis endpoints."""
        cam = self._engine.editor_camera
        if not cam:
            return
        yaw, pitch = cam.rotation
        yaw_rad = math.radians(yaw)
        pitch_rad = math.radians(pitch)

        cos_y, sin_y = math.cos(yaw_rad), math.sin(yaw_rad)
        cos_p, sin_p = math.cos(pitch_rad), math.sin(pitch_rad)

        # Reconstruct the actual camera basis used by the Scene camera.
        # C++ SetEulerAngles(pitch, yaw, 0) yields forward.y = -sin(pitch).
        forward = (sin_y * cos_p, -sin_p, cos_y * cos_p)
        right = (cos_y, 0.0, -sin_y)
        up = (
            forward[1] * right[2] - forward[2] * right[1],
            forward[2] * right[0] - forward[0] * right[2],
            forward[0] * right[1] - forward[1] * right[0],
        )

        r = Theme.SCENE_ORIENT_RADIUS
        margin = Theme.SCENE_ORIENT_MARGIN
        # Use screen-absolute coordinates from the viewport info
        cx = vp.image_max_x - r - margin
        cy = vp.image_min_y + r + margin

        # Project world axis to 2D screen position
        axis_len = Theme.SCENE_ORIENT_AXIS_LEN
        axes = [
            ('X', (1, 0, 0)),
            ('Y', (0, 1, 0)),
            ('Z', (0, 0, 1)),
        ]

        # Collect endpoints and promote the front-facing side per axis so the
        # visible large labeled circles match Unity's scene gizmo behavior.
        endpoints = []
        axis_lines = []
        for label, (ax, ay, az) in axes:
            sx = ax * right[0] + ay * right[1] + az * right[2]
            sy = ax * up[0] + ay * up[1] + az * up[2]
            depth = ax * forward[0] + ay * forward[1] + az * forward[2]
            pos = ('+' + label, label, sx, sy, depth)
            neg = ('-' + label, label, -sx, -sy, -depth)
            front, back = (pos, neg) if depth <= -depth else (neg, pos)
            axis_lines.append(front)
            endpoints.append((front[0], front[1], front[2], front[3], front[4], True))
            endpoints.append((back[0], back[1], back[2], back[3], back[4], False))

        # Sort by depth (farther first; front-facing endpoints have smaller depth).
        endpoints.sort(key=lambda e: e[4], reverse=True)

        # Draw axis lines first (below circles), using the front-facing endpoint.
        for axis_key, label, sx, sy, depth in sorted(axis_lines, key=lambda e: e[4], reverse=True):
            clr = self._GIZMO_AXIS_COLORS[label]
            ex = cx + sx * axis_len
            ey = cy - sy * axis_len
            ctx.draw_line(cx, cy, ex, ey, *clr, 0.6, 2.0)

        # Draw endpoints
        mouse_x = ctx.get_mouse_pos_x()
        mouse_y = ctx.get_mouse_pos_y()
        clicked_axis = None

        for axis_key, label, sx, sy, depth, front_facing in endpoints:
            clr = self._GIZMO_AXIS_COLORS[label]
            ex = cx + sx * axis_len
            ey = cy - sy * axis_len
            er = Theme.SCENE_ORIENT_END_RADIUS if front_facing else Theme.SCENE_ORIENT_NEG_RADIUS
            a = 1.0 if front_facing else 0.5

            # Draw filled circle
            ctx.draw_filled_circle(ex, ey, er, clr[0], clr[1], clr[2], a, 16)

            # Unity-style: label the endpoint that currently faces the camera.
            if front_facing:
                ctx.draw_text(ex - 3, ey - 5, label, 1.0, 1.0, 1.0, 1.0)

            # Hit test
            dx = mouse_x - ex
            dy = mouse_y - ey
            if dx * dx + dy * dy <= er * er * 1.5:
                # Highlight on hover
                ctx.draw_circle(ex, ey, er + 1, 1.0, 1.0, 1.0, 0.7, 1.5, 16)
                if ctx.is_mouse_button_clicked(0):
                    clicked_axis = axis_key

        # Handle click — animate camera to axis view
        if clicked_axis is not None:
            target_yaw, target_pitch = self._GIZMO_AXIS_VIEWS[clicked_axis]
            self._start_fly_to_orientation(target_yaw, target_pitch)

