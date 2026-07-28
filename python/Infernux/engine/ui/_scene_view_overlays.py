"""SceneViewOverlaysMixin — extracted from SceneViewPanel."""
from __future__ import annotations

"""
Unity-style Scene View panel with 3D viewport and camera controls.
"""

import math
from Infernux.lib import InxGUIContext, InputManager
from Infernux.engine.i18n import t
from .editor_panel import EditorPanel
from .closable_panel import ClosablePanel
from .panel_registry import editor_panel
from .theme import Theme, ImGuiCol, ImGuiMouseCursor, ImGuiStyleVar
from .viewport_utils import ViewportInfo, capture_viewport_info
from . import imgui_keys as _keys
from .editor_icons import EditorIcons

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
        self._particle_preview_resize_drag = False

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

    def _on_particle_preview_play_mode_changed(self, event) -> None:
        """Rebind edit preview after Play Mode recreates the scene objects."""
        from Infernux.engine.play_mode import PlayModeState

        if event.new_state is PlayModeState.EDIT:
            # Stop Mode restores a new authoring scene. Never retain component
            # wrappers from the runtime scene, even when object IDs match.
            self._release_particle_preview_selection()
            self._restore_particle_preview_selection()
            return
        self._release_particle_preview_selection()

    def _on_particle_preview_selection(self, game_object) -> None:
        component = self._particle_component_from_object(game_object)
        if component is self._particle_preview_component and self._particle_preview_is_live(
            component, game_object
        ):
            return
        previous = self._particle_preview_component
        if previous is not None:
            try:
                suspend = getattr(
                    previous,
                    "editor_preview_suspend",
                    previous.editor_preview_pause,
                )
                suspend()
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        self._particle_preview_component = component
        self._particle_preview_object = game_object if component is not None else None
        self._particle_preview_playing = component is not None
        self._particle_preview_prepared = False
        if component is not None and self._is_particle_preview_edit_mode():
            try:
                self._particle_preview_prepared = bool(component.editor_preview_begin())
                is_playing = getattr(component, "editor_preview_is_playing", None)
                self._particle_preview_playing = (
                    bool(is_playing())
                    if callable(is_playing)
                    else self._particle_preview_prepared
                )
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
        self._particle_preview_resize_drag = False

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
            # The component owns playback intent and GPU residency.  Tick it
            # even while the panel's cached state is stale so it can recover
            # after Play Mode replaces the native particle graph.
            self._particle_preview_prepared = bool(
                component.editor_preview_update(
                    delta_time,
                    self._particle_preview_speed,
                )
            )
            is_playing = getattr(component, "editor_preview_is_playing", None)
            self._particle_preview_playing = (
                bool(is_playing())
                if callable(is_playing)
                else self._particle_preview_prepared
            )
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

        try:
            emitter_states = component.editor_preview_emitter_states()
            is_ready = getattr(component, "editor_preview_is_ready", None)
            if callable(is_ready):
                self._particle_preview_prepared = bool(is_ready())
            is_playing = getattr(component, "editor_preview_is_playing", None)
            if callable(is_playing):
                self._particle_preview_playing = bool(is_playing())
        except (AttributeError, ReferenceError, RuntimeError):
            emitter_states = []

        width = max(220.0, min(380.0, scene_width - 24.0))
        min_height = min(126.0, max(64.0, scene_height - 24.0))
        max_height = max(min_height, scene_height - 24.0)
        height = min(max_height, max(min_height, self._particle_preview_height))
        if self._particle_preview_resize_drag:
            if ctx.is_mouse_button_down(0):
                delta = (
                    ctx.get_mouse_pos_y() - self._particle_preview_resize_start_y
                )
                height = min(
                    max_height,
                    max(
                        min_height,
                        self._particle_preview_resize_start_height - delta,
                    ),
                )
                self._particle_preview_height = height
                ctx.set_mouse_cursor(ImGuiMouseCursor.ResizeNS)
            else:
                self._particle_preview_resize_drag = False

        ctx.set_cursor_pos_x(cursor_start_x + scene_width - width - 12.0)
        ctx.set_cursor_pos_y(cursor_start_y + scene_height - height - 12.0)
        ctx.push_style_color(
            ImGuiCol.ChildBg,
            Theme.WINDOW_BG[0],
            Theme.WINDOW_BG[1],
            Theme.WINDOW_BG[2],
            0.97,
        )
        ctx.push_style_color(ImGuiCol.Border, *Theme.BORDER)
        ctx.push_style_var_float(ImGuiStyleVar.ChildRounding, 5.0)
        visible = ctx.begin_child("##particle_preview_controls", width, height, True)
        try:
            hovered = bool(ctx.is_window_hovered())
            if not visible:
                return hovered

            ctx.set_cursor_pos_x(0.0)
            ctx.set_cursor_pos_y(0.0)
            ctx.invisible_button("##particle_preview_resize", width, 10.0)
            if ctx.is_item_hovered() or ctx.is_item_active():
                hovered = True
                ctx.set_mouse_cursor(ImGuiMouseCursor.ResizeNS)
            if ctx.is_item_active() and not self._particle_preview_resize_drag:
                self._particle_preview_resize_drag = True
                self._particle_preview_resize_start_y = ctx.get_mouse_pos_y()
                self._particle_preview_resize_start_height = height
            window_x = ctx.get_window_pos_x()
            window_y = ctx.get_window_pos_y()
            grip_color = Theme.TEXT_DIM
            ctx.draw_line(
                window_x + width * 0.42,
                window_y + 5.0,
                window_x + width * 0.58,
                window_y + 5.0,
                *grip_color,
                1.5,
            )

            ctx.set_cursor_pos_x(10.0)
            ctx.set_cursor_pos_y(12.0)
            ctx.label(t("particle_preview.title"))
            ctx.separator()
            semantic_capture = bool(getattr(ctx, "semantic_capture_enabled", True))
            record_item = getattr(ctx, "record_semantic_item", None)
            ctx.align_text_to_frame_padding()
            ctx.label(t("particle_preview.speed"))
            ctx.same_line(96.0)
            ctx.set_next_item_width(-1)
            speed = float(
                ctx.float_slider(
                    "##particle_preview_speed",
                    self._particle_preview_speed,
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
                    numeric_value=self._particle_preview_speed,
                )
            if self._particle_preview_playing:
                pause_label = t("particle_preview.pause")
                ctx.push_style_color(ImGuiCol.Button, *Theme.PLAY_ACTIVE)
                pause_clicked = ctx.button(pause_label, width=72.0)
                ctx.pop_style_color(1)
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
                        is_playing = getattr(
                            component, "editor_preview_is_playing", None
                        )
                        self._particle_preview_playing = (
                            bool(is_playing())
                            if callable(is_playing)
                            else self._particle_preview_prepared
                        )
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
            for emitter in emitter_states:
                index = int(emitter["index"])
                ctx.separator()
                if not bool(emitter["enabled"]):
                    ctx.begin_disabled(True)
                was_visible = bool(emitter["visible"])
                preview_visible = bool(
                    ctx.checkbox(
                        f"{emitter['name']}##particle_preview_visible_{index}",
                        was_visible,
                    )
                )
                if semantic_capture and callable(record_item):
                    record_item(
                        "checkbox",
                        str(emitter["name"]),
                        bool(emitter["enabled"]),
                        f"scene_view.particle_preview.emitter.{index}.visible",
                        bool_value=preview_visible,
                    )
                if preview_visible != was_visible:
                    component.editor_preview_set_emitter_muted(
                        index, not preview_visible
                    )
                ctx.same_line(0, 8.0)
                solo = bool(
                    ctx.checkbox(
                        f"{t('particle_preview.solo')}##particle_preview_solo_{index}",
                        bool(emitter["solo"]),
                    )
                )
                if semantic_capture and callable(record_item):
                    record_item(
                        "checkbox",
                        t("particle_preview.solo"),
                        bool(emitter["enabled"]),
                        f"scene_view.particle_preview.emitter.{index}.solo",
                        bool_value=solo,
                    )
                if solo != bool(emitter["solo"]):
                    component.editor_preview_set_emitter_solo(index, solo)
                ctx.same_line(0, 8.0)
                restarted = ctx.button(
                    f"{t('particle_preview.restart')}##particle_preview_restart_{index}"
                )
                if semantic_capture and callable(record_item):
                    record_item(
                        "button",
                        t("particle_preview.restart"),
                        bool(emitter["enabled"]),
                        f"scene_view.particle_preview.emitter.{index}.restart",
                    )
                if restarted:
                    try:
                        restarted = bool(
                            component.editor_preview_restart_emitter(index)
                        )
                        self._particle_preview_prepared = restarted
                        self._particle_preview_playing = restarted
                    except (AttributeError, ReferenceError, RuntimeError):
                        self._particle_preview_prepared = False
                        self._particle_preview_playing = False
                if not bool(emitter["enabled"]):
                    ctx.end_disabled()
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
        """Lazily resolve pinned tool icon textures via EditorIcons."""
        if not self._engine:
            return
        native = self._engine.get_native_engine() if hasattr(self._engine, 'get_native_engine') else self._engine
        if native is None:
            return
        _ICON_MAP = {
            TOOL_NONE:      "tool_none",
            TOOL_TRANSLATE: "tool_move",
            TOOL_ROTATE:    "tool_rotate",
            TOOL_SCALE:     "tool_scale",
        }
        all_ready = True
        for mode, name in _ICON_MAP.items():
            # Re-resolve every frame until upload completes; never keep a
            # stale unpinned preview descriptor across frames.
            tid = EditorIcons.get(native, name)
            self._tool_icon_ids[mode] = tid
            if tid == 0:
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

