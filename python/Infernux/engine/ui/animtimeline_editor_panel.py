"""Timeline Editor — visual editor for ``.animtimeline`` transform timelines.

A minimal Unity-Timeline-style editor for a single transform track: a horizontal
timeline bar (like a video scrubber) shows keyframes as diamonds and a draggable
playhead.  Add a keyframe at the playhead, select a keyframe to edit its
transform and the transition curve used to reach it (from the previous keyframe).

Shortcuts: Space play/pause, Ctrl+S save, Ctrl+Shift+S save-as, Ctrl+N new.

Opened from Window menu → Timeline Editor, or by double-clicking a
``.animtimeline`` asset in the Project panel.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.path_utils import relative_path, resolved_path
from Infernux.engine.i18n import t
from Infernux.lib import InxGUIContext

from Infernux.core.animation_timeline import (
    AnimationTimeline,
    TimelineKeyframe,
    INTERP_MODES,
    APPLY_MODES,
)
from .editor_panel import EditorPanel
from .asset_save_dialog import AssetSaveAsDialog
from .panel_registry import editor_panel
from .imgui_keys import KEY_N, KEY_SPACE
from .theme import ImGuiCol, Theme

_MOD_CTRL = 1 << 12

# Combo label i18n keys (order matches INTERP_MODES / APPLY_MODES).
_INTERP_LABEL_KEYS = ("interp_constant", "interp_linear", "interp_ease_in", "interp_ease_out", "interp_ease_inout")
_APPLY_LABEL_KEYS = ("apply_additive", "apply_absolute")
_BAR_H = 88.0
_BAR_EDGE_PAD = 14.0  # inset track so t=0 / t=dur keyframes stay inside the bar
_PREVIEW_H = 196.0  # 3D preview height reserved at the bottom of the right (keyframe) panel
_KF_LABEL_W = 78.0  # fixed label column width so inline label+field rows align
_DRAG_THRESHOLD = 4.0  # px the mouse must move before a keyframe starts dragging (Blender-like)
_PREVIEW_RENDER_PX = 240  # fixed offscreen render size (stable → no per-frame framebuffer churn)


def _tl_colors():
    """Timeline palette pulled live from the editor Theme (so theme switches apply)."""

    def _mix(a, b, t):
        return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))

    bar = Theme.FRAME_BG
    return {
        "bar": bar,
        "ruler": Theme.HEADER,
        "lane": _mix(bar, Theme.HEADER, 0.6),
        "tick": Theme.TEXT_DIM,
        "text": Theme.TEXT_DIM,
        "key": Theme.TEXT_DIM,
        "key_hi": Theme.TEXT,
        "accent": Theme.APPLY_BUTTON,
        "preview_bg": Theme.WINDOW_BG,
    }


def _interp_labels():
    return [t(f"animtimeline_editor.{k}") for k in _INTERP_LABEL_KEYS]


def _apply_labels():
    return [t(f"animtimeline_editor.{k}") for k in _APPLY_LABEL_KEYS]


def _semantic_capture_enabled(ctx: InxGUIContext) -> bool:
    return bool(getattr(ctx, "semantic_capture_enabled", True))


@editor_panel(
    "Timeline Editor",
    type_id="animtimeline_editor",
    title_key="panel.animtimeline_editor",
    menu_path="Animation",
)
class AnimTimelineEditorPanel(EditorPanel):
    """Visual single-track transform timeline editor."""

    window_id = "animtimeline_editor"

    def __init__(self):
        super().__init__(title="Timeline Editor", window_id="animtimeline_editor")
        self._timeline: AnimationTimeline = AnimationTimeline(name="Timeline")
        self._file_path: str = ""
        self._dirty: bool = True
        self._playhead: float = 0.0
        self._playing: bool = False
        self._last_tick: float = 0.0
        self._play_wall_start: float = 0.0
        self._playhead_at_play_start: float = 0.0
        self._sel_key: Optional[TimelineKeyframe] = None
        self._drag_key: Optional[TimelineKeyframe] = None
        self._drag_armed: bool = False
        self._press_x: float = 0.0
        self._bar_was_active: bool = False
        self._idle_suppressed: bool = False
        self._idle_prev: bool = True
        # Orbit camera for the 3D preview viewport (yaw/pitch radians, distance = zoom).
        self._cam_yaw: float = -0.6
        self._cam_pitch: float = 0.5
        self._cam_dist: float = 6.0
        self._orbiting: bool = False
        self._preview_tex_cache: int = 0
        self._preview_request_signature = None
        self._save_as_dialog = AssetSaveAsDialog("animtimeline.save_as", "timeline")
        # Panel persistence: ``load_state`` may run from bootstrap or first render.
        self._panel_state_restored_once: bool = False
        self._panel_restore_data: Optional[dict] = None

    def on_disable(self) -> None:
        # Always restore the editor idle setting if we suppressed it.
        self._set_engine_active(False)

    # Unsaved marker in the window title (shared dirty/save/close handled by ClosablePanel).
    def _window_title_suffix(self) -> str:
        return " *" if self._dirty else ""

    # ── Lifecycle ──────────────────────────────────────────────────────
    def _initial_size(self):
        return (940, 560)

    def _open_timeline(self, path: str):
        tl = AnimationTimeline.load(path)
        if tl is None:
            Debug.log_warning(f"[TimelineEditor] Failed to load: {path}")
            return
        self._timeline = tl
        self._file_path = path
        self._playhead = 0.0
        self._playing = False
        self._sel_key = None
        self._drag_key = None
        self._set_dirty(False)

    def _new_timeline(self):
        self._timeline = AnimationTimeline(name="Timeline")
        self._file_path = ""
        self._playhead = 0.0
        self._playing = False
        self._sel_key = None
        self._drag_key = None
        self._set_dirty(True)

    def _set_dirty(self, value: bool):
        # ClosablePanel._sync_dirty_registry() reads self._dirty every frame and
        # registers the title + _do_save handler, so we only flip the flag here.
        self._dirty = bool(value)

    # ── State persistence ──────────────────────────────────────────────

    def _normalize_timeline_path(self, path: str) -> str:
        """Return a normalized absolute timeline path when possible."""
        p = (path or "").strip()
        if not p:
            return ""
        if os.path.isabs(p):
            return resolved_path(p)
        try:
            from Infernux.engine.project_context import get_project_root

            root = get_project_root()
        except Exception:
            root = None
        if root:
            return resolved_path(os.path.join(root, p))
        return resolved_path(p)

    def save_state(self) -> dict:
        """Persist open .animtimeline path (saved on disk only) and view settings."""
        data: dict = {}
        fp = self._normalize_timeline_path(self._file_path)
        rel_fallback = ""
        if fp and not os.path.isfile(fp):
            fp = ""
        if not fp and self._panel_restore_data:
            fp = self._normalize_timeline_path(self._panel_restore_data.get("file_path") or "")
            rel_fallback = (self._panel_restore_data.get("file_path_rel") or "").strip()
            if fp and not os.path.isfile(fp):
                fp = ""
        if not fp and not rel_fallback:
            try:
                from Infernux.engine.ui import panel_state as _ps

                prev = _ps.get(f"panel:{self.window_id}")
                if prev:
                    fp = self._normalize_timeline_path(prev.get("file_path") or "")
                    rel_fallback = (prev.get("file_path_rel") or "").strip()
                    if fp and not os.path.isfile(fp):
                        fp = ""
            except Exception:
                pass
        if fp:
            data["file_path"] = fp
            try:
                from Infernux.engine.project_context import get_project_root

                root = get_project_root()
                if root:
                    data["file_path_rel"] = relative_path(fp, root)
            except (ValueError, OSError):
                pass
        elif rel_fallback:
            data["file_path_rel"] = rel_fallback
        data["playhead"] = float(self._playhead)
        data["cam_yaw"] = float(self._cam_yaw)
        data["cam_pitch"] = float(self._cam_pitch)
        data["cam_dist"] = float(self._cam_dist)
        data["dirty"] = bool(self._dirty)
        if self._dirty:
            data["draft"] = self._timeline.to_dict()
        return data

    def _resolve_saved_timeline_path(self, data: dict) -> str:
        """Resolve persisted path using absolute path, then project-relative."""
        fp = self._normalize_timeline_path(data.get("file_path") or "")
        rel = (data.get("file_path_rel") or "").strip()
        if fp and os.path.isfile(fp):
            return fp
        if rel:
            try:
                from Infernux.engine.project_context import get_project_root

                root = get_project_root()
                if root:
                    cand = resolved_path(os.path.join(root, rel))
                    if os.path.isfile(cand):
                        return cand
            except (OSError, ValueError):
                pass
        return ""

    def load_state(self, data: dict) -> None:
        if not data:
            self._panel_restore_data = None
            self._panel_state_restored_once = True
            return
        self._panel_restore_data = dict(data)
        self._playhead = float(data.get("playhead", self._playhead))
        self._cam_yaw = float(data.get("cam_yaw", self._cam_yaw))
        self._cam_pitch = float(data.get("cam_pitch", self._cam_pitch))
        self._cam_dist = float(data.get("cam_dist", self._cam_dist))
        draft = data.get("draft")
        if bool(data.get("dirty")) and isinstance(draft, dict):
            self._timeline = AnimationTimeline.from_dict(draft)
            self._file_path = self._normalize_timeline_path(data.get("file_path") or "")
            self._timeline.file_path = self._file_path
            self._set_dirty(True)
            self._panel_state_restored_once = True
            return
        self._panel_state_restored_once = False

    def _apply_pending_panel_restore(self) -> None:
        """Open saved .animtimeline once project root can resolve relative paths."""
        if self._panel_state_restored_once:
            return
        data = self._panel_restore_data
        if not data:
            self._panel_state_restored_once = True
            return
        to_open = self._resolve_saved_timeline_path(data)
        if to_open:
            self._open_timeline(to_open)
            self._playhead = float(data.get("playhead", self._playhead))
            self._panel_state_restored_once = True
            return
        fp = (data.get("file_path") or "").strip()
        rel = (data.get("file_path_rel") or "").strip()
        if not fp and not rel:
            self._panel_state_restored_once = True
            return
        try:
            from Infernux.engine.project_context import get_project_root

            root = get_project_root()
        except Exception:
            root = None
        if root is None:
            return
        self._panel_state_restored_once = True

    # ── Save ───────────────────────────────────────────────────────────
    def _do_save(self):
        if self._file_path:
            self._save_to(self._file_path)
        else:
            self._show_save_as_dialog()

    def handle_save_command(self, save_as: bool = False) -> bool:
        if save_as:
            self._show_save_as_dialog()
        else:
            self._do_save()
        return True

    def _save_to(self, path: str) -> bool:
        self._timeline.name = os.path.splitext(os.path.basename(path))[0]
        if self._timeline.save(path):
            self._file_path = path
            Debug.log(f"[TimelineEditor] Saved: {path}")
            try:
                from Infernux.core.assets import AssetManager
                AssetManager.reimport_asset(path)
            except Exception:
                pass
            self._set_dirty(False)
            return True
        else:
            Debug.log_warning(f"[TimelineEditor] Failed to save: {path}")
            return False

    def _show_save_as_dialog(self):
        safe = (self._timeline.name or "Timeline").replace(" ", "_")
        if not self._save_as_dialog.request(
            title="Save Timeline",
            extension="animtimeline",
            default_name=safe,
            current_path=self._file_path,
        ):
            Debug.log_warning("[TimelineEditor] No project root set - cannot save timeline.")

    def _discard_unsaved_changes(self) -> bool:
        if self._file_path:
            self._open_timeline(self._file_path)
            return not self._dirty
        self._new_timeline()
        self._set_dirty(False)
        return True

    # ── Selection helpers ──────────────────────────────────────────────
    def _current_sel_key(self) -> Optional[TimelineKeyframe]:
        """Return the selected keyframe if it is still in the list (by identity)."""
        if self._sel_key is None:
            return None
        for k in self._timeline.keyframes:
            if k is self._sel_key:
                return k
        self._sel_key = None
        return None

    def _add_keyframe_at_playhead(self):
        sampled = self._timeline.sample(self._playhead)
        if sampled is not None:
            pos, rot, scl = sampled
        else:
            pos, rot, scl = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
        key = TimelineKeyframe(
            time=float(self._playhead), position=pos, rotation=rot, scale=scl,
        )
        self._timeline.keyframes.append(key)
        self._sel_key = key
        self._set_dirty(True)

    def _delete_selected_key(self):
        k = self._current_sel_key()
        if k is None:
            return
        self._timeline.keyframes = [x for x in self._timeline.keyframes if x is not k]
        self._sel_key = None
        self._drag_key = None
        self._set_dirty(True)

    # ── Playback ───────────────────────────────────────────────────────
    def _advance_playback(self):
        now = time.perf_counter()
        if self._playing:
            # Wall-clock playback: playhead advances at real time regardless of
            # frame rate or GPU preview stalls (dt accumulation was running slow).
            self._playhead = self._playhead_at_play_start + (now - self._play_wall_start)
            dur = max(1e-6, float(self._timeline.duration))
            if self._playhead >= dur:
                self._playhead = dur
                self._playing = False
        self._last_tick = now

    def _set_engine_active(self, active: bool):
        """Keep the editor loop at full speed while the timeline is interactively active."""
        try:
            from .asset_resource_preview import _resolve_native_engine
            native = _resolve_native_engine(self)
            if native is None:
                return
            if active and not self._idle_suppressed:
                self._idle_prev = bool(native.is_editor_idle_enabled())
                native.set_editor_idle_enabled(False)
                self._idle_suppressed = True
            elif not active and self._idle_suppressed:
                native.set_editor_idle_enabled(self._idle_prev)
                self._idle_suppressed = False
        except Exception:
            pass

    def _needs_full_speed_frames(self) -> bool:
        """Return whether timeline playback or interaction needs continuous frames."""
        return bool(self._playing or self._orbiting or self._bar_was_active)

    # ── Render ─────────────────────────────────────────────────────────
    def on_render_content(self, ctx: InxGUIContext):
        if not self._panel_state_restored_once:
            if self._panel_restore_data is None:
                from Infernux.engine.ui import panel_state as _ps

                data = _ps.get(f"panel:{self.window_id}")
                if data:
                    self.load_state(data)
                else:
                    self._panel_state_restored_once = True
            self._apply_pending_panel_restore()

        self._handle_shortcuts(ctx)
        self._advance_playback()

        self._render_toolbar(ctx)
        ctx.separator()

        avail_h = max(120.0, ctx.get_content_region_avail_height())
        avail_w = ctx.get_content_region_avail_width()
        right_w = min(360.0, max(260.0, avail_w * 0.34))
        left_w = max(240.0, avail_w - right_w - 8.0)

        # LEFT: transport + the full-height timeline scrubber (the main authoring area).
        if ctx.begin_child("##tl_left", left_w, avail_h, False):
            self._render_transport(ctx)
            ctx.dummy(0, 4)
            self._render_timeline_bar(ctx)
        ctx.end_child()
        ctx.same_line()
        # RIGHT: keyframe inspector (top) + the 3D preview viewport (bottom-right),
        # both sharing this panel.
        if ctx.begin_child("##tl_right", right_w, avail_h, True):
            inner_w = ctx.get_content_region_avail_width()
            inner_h = ctx.get_content_region_avail_height()
            insp_h = max(80.0, inner_h - _PREVIEW_H - 8.0)
            if ctx.begin_child("##tl_kf", inner_w, insp_h, False):
                self._render_keyframe_inspector(ctx)
            ctx.end_child()
            ctx.separator()
            self._render_preview_viewport(ctx, ctx.get_content_region_avail_width(),
                                          max(120.0, ctx.get_content_region_avail_height()))
        ctx.end_child()

        self._save_as_dialog.render(ctx, self._save_to)

        # Playback also needs continuous frames; wall-clock timing alone cannot
        # make an idle-throttled preview look smooth.
        self._set_engine_active(self._needs_full_speed_frames())

        # GPU work is scheduled above; pump after all timeline UI is drawn so scrubbing
        # feels instant while the preview catches up once per frame.
        try:
            from .asset_resource_preview import _resolve_native_engine
            native = _resolve_native_engine(self)
            if native is not None and hasattr(native, "pump_preview_tasks"):
                native.pump_preview_tasks()
        except Exception:
            pass

    def _is_focused(self, ctx: InxGUIContext) -> bool:
        # RootAndChildWindows = RootWindow(1<<1) | ChildWindows(1<<0) = 3
        try:
            return ctx.is_window_focused(3)
        except Exception:
            return True

    def _handle_shortcuts(self, ctx: InxGUIContext):
        # Only the focused editor window reacts to its shortcuts.
        if not self._is_focused(ctx):
            return
        ctrl = ctx.is_key_down(_MOD_CTRL)
        if ctrl and ctx.is_key_pressed(KEY_N):
            self._new_timeline()
        # Space toggles playback only when no widget (e.g. a text field) is focused.
        if ctx.is_key_pressed(KEY_SPACE) and not ctx.is_any_item_active():
            self._toggle_play()

    def _toggle_play(self):
        # Replaying from the end restarts (the preview plays once, no auto-loop).
        if not self._playing and self._playhead >= float(self._timeline.duration) - 1e-4:
            self._playhead = 0.0
        if not self._playing:
            self._play_wall_start = time.perf_counter()
            self._playhead_at_play_start = float(self._playhead)
        self._playing = not self._playing
        self._last_tick = time.perf_counter()

    def _render_toolbar(self, ctx: InxGUIContext):
        capture_semantics = _semantic_capture_enabled(ctx)
        new_label = t("animtimeline_editor.new")
        if ctx.button(new_label):
            self._new_timeline()
        if capture_semantics:
            ctx.record_semantic_item("button", new_label, True, "animtimeline.toolbar.new")
        ctx.same_line()
        save_label = t("animtimeline_editor.save")
        if ctx.button(save_label):
            self._do_save()
        if capture_semantics:
            ctx.record_semantic_item("button", save_label, True, "animtimeline.toolbar.save")
        ctx.same_line()
        save_as_label = t("animtimeline_editor.save_as")
        if ctx.button(save_as_label):
            self._show_save_as_dialog()
        if capture_semantics:
            ctx.record_semantic_item("button", save_as_label, True, "animtimeline.toolbar.save_as")
        ctx.same_line()
        ctx.label(f"{t('animtimeline_editor.name')}:")
        ctx.same_line()
        ctx.set_next_item_width(150.0)
        new_name = ctx.text_input("##tl_name", self._timeline.name, 128)
        if capture_semantics:
            ctx.record_semantic_item(
                "text_input", t("animtimeline_editor.name"), True,
                "animtimeline.toolbar.name", string_value=new_name,
            )
        if new_name != self._timeline.name:
            self._timeline.name = new_name
            self._set_dirty(True)
        ctx.same_line()
        ctx.label(f"{t('animtimeline_editor.duration')}:")
        ctx.same_line()
        ctx.set_next_item_width(70.0)
        new_dur = ctx.drag_float("##tl_dur", float(self._timeline.duration), 0.05, 0.05, 3600.0)
        if capture_semantics:
            ctx.record_semantic_item(
                "drag_float", t("animtimeline_editor.duration"), True,
                "animtimeline.toolbar.duration", numeric_value=new_dur,
            )
        if new_dur != self._timeline.duration:
            self._timeline.duration = max(0.05, float(new_dur))
            self._set_dirty(True)
        ctx.same_line()
        ctx.label(f"{t('animtimeline_editor.apply_mode')}:")
        ctx.same_line()
        ctx.set_next_item_width(160.0)
        apply_labels = _apply_labels()
        cur_mode = self._timeline.apply_mode if self._timeline.apply_mode in APPLY_MODES else APPLY_MODES[0]
        mode_idx = APPLY_MODES.index(cur_mode)
        new_mode = ctx.combo("##tl_apply_mode", mode_idx, apply_labels, len(apply_labels))
        if capture_semantics:
            ctx.record_semantic_item(
                "combo", t("animtimeline_editor.apply_mode"), True,
                "animtimeline.toolbar.apply_mode", string_value=APPLY_MODES[new_mode],
            )
        if new_mode != mode_idx:
            self._timeline.apply_mode = APPLY_MODES[new_mode]
            self._set_dirty(True)

    def _render_transport(self, ctx: InxGUIContext):
        capture_semantics = _semantic_capture_enabled(ctx)
        play_label = t("animtimeline_editor.pause") if self._playing else t("animtimeline_editor.play")
        if ctx.button(play_label):
            self._toggle_play()
        if capture_semantics:
            ctx.record_semantic_item(
                "button", play_label, True, "animtimeline.transport.play_pause"
            )
        ctx.same_line()
        stop_label = t("animtimeline_editor.stop")
        if ctx.button(stop_label):
            self._playing = False
            self._playhead = 0.0
        if capture_semantics:
            ctx.record_semantic_item("button", stop_label, True, "animtimeline.transport.stop")
        ctx.same_line()
        add_key_label = t("animtimeline_editor.add_key")
        if ctx.button(add_key_label):
            self._add_keyframe_at_playhead()
        if capture_semantics:
            ctx.record_semantic_item(
                "button", add_key_label, True, "animtimeline.transport.add_key"
            )
        ctx.same_line()
        delete_key_label = t("animtimeline_editor.delete_key")
        if ctx.button(delete_key_label):
            self._delete_selected_key()
        if capture_semantics:
            ctx.record_semantic_item(
                "button", delete_key_label, True, "animtimeline.transport.delete_key"
            )
        ctx.same_line()
        ctx.label(f"{self._playhead:.2f} / {self._timeline.duration:.2f}s")

    def _render_timeline_bar(self, ctx: InxGUIContext):
        dur = max(1e-6, float(self._timeline.duration))
        # Leave the same small horizontal breathing room used by the child
        # content so the ruler does not protrude past the toolbar above it.
        bar_w = max(80.0, ctx.get_content_region_avail_width() - 2.0)
        ctx.invisible_button("##tl_bar", bar_w, _BAR_H)
        if _semantic_capture_enabled(ctx):
            ctx.record_semantic_item(
                "button", "Timeline scrubber", True, "animtimeline.scrubber"
            )
        x0 = ctx.get_item_rect_min_x()
        y0 = ctx.get_item_rect_min_y()
        x1 = ctx.get_item_rect_max_x()
        y1 = ctx.get_item_rect_max_y()
        active = ctx.is_item_active()
        hovered = ctx.is_item_hovered()
        mx = ctx.get_mouse_pos_x()

        c = _tl_colors()
        ruler_h = 18.0
        ruler_y = y0 + ruler_h
        lane_y = (ruler_y + y1) * 0.5
        ks = 6.5  # keyframe half-size (square)

        # Track content is inset horizontally so edge keyframes remain grabbable.
        pad = _BAR_EDGE_PAD
        tx0 = x0 + pad
        tx1 = x1 - pad
        tw = max(1.0, tx1 - tx0)

        def time_to_x(tm: float) -> float:
            return tx0 + (max(0.0, min(dur, tm)) / dur) * tw

        def x_to_time(xx: float) -> float:
            return max(0.0, min(1.0, (xx - tx0) / tw)) * dur

        # Background, ruler strip, track lane (theme colors).
        ctx.draw_filled_rect(x0, y0, x1, y1, *c["bar"], 4.0)
        ctx.draw_filled_rect(x0, y0, x1, ruler_y, *c["ruler"], 4.0)
        ctx.draw_filled_rect(tx0, lane_y - 11, tx1, lane_y + 11, *c["lane"], 3.0)
        ctx.draw_text(tx0 + 2, lane_y - 9, t("animtimeline_editor.transform"), *c["text"])

        # Ruler ticks + time labels (aligned to inset track)
        for i in range(0, 11):
            frac = i / 10.0
            tick_x = tx0 + frac * tw
            major = (i % 5 == 0)
            ctx.draw_line(tick_x, ruler_y - (8.0 if major else 4.0), tick_x, ruler_y, *c["tick"], 1.0)
            if major:
                ctx.draw_text(tick_x + 2, y0 + 2, f"{frac * dur:.2f}", *c["text"])

        # Hover highlight: which keyframe would be grabbed
        hover_key = None
        if hovered and not active:
            best_dx = 9.0
            for k in self._timeline.keyframes:
                if abs(time_to_x(k.time) - mx) <= best_dx:
                    best_dx = abs(time_to_x(k.time) - mx)
                    hover_key = k

        # Keyframe markers: gray squares; selection draws an outer border (no rotate/scale).
        sel = self._current_sel_key()
        for k in self._timeline.keyframes:
            kx = time_to_x(k.time)
            col = c["key_hi"] if (k is hover_key or k is sel) else c["key"]
            ctx.draw_filled_rect(kx - ks, lane_y - ks, kx + ks, lane_y + ks, *col, 2.0)
            if k is sel:
                b = ks + 3.0
                ctx.draw_rect(kx - b, lane_y - b, kx + b, lane_y + b, *c["accent"], 1.6, 2.0)

        # Playhead (line + top handle, theme accent)
        px = time_to_x(self._playhead)
        ctx.draw_line(px, y0, px, y1, *c["accent"], 2.0)
        ctx.draw_filled_rect(px - 4.0, y0, px + 4.0, y0 + 7.0, *c["accent"], 1.0)

        # ── Interaction: grab a keyframe to drag (after a threshold); empty area scrubs ──
        press_started = active and not self._bar_was_active
        if press_started:
            self._press_x = mx
            self._drag_key = None
            self._drag_armed = False
            best_dx = max(9.0, ks + 2.0)
            for k in self._timeline.keyframes:
                if abs(time_to_x(k.time) - mx) <= best_dx:
                    best_dx = abs(time_to_x(k.time) - mx)
                    self._drag_key = k
            if self._drag_key is not None:
                self._sel_key = self._drag_key   # click selects immediately
                self._drag_armed = True          # but moving waits for the threshold
                self._playing = False
            else:
                self._playhead = x_to_time(mx)   # empty press scrubs right away
                self._playing = False
        elif active:
            if self._drag_key is not None:
                if self._drag_armed and abs(mx - self._press_x) > _DRAG_THRESHOLD:
                    self._drag_armed = False
                if not self._drag_armed:
                    self._drag_key.time = x_to_time(mx)
                    self._playhead = self._drag_key.time
                    self._set_dirty(True)
            else:
                self._playhead = x_to_time(mx)
                self._playing = False
        if not active:
            self._drag_key = None
            self._drag_armed = False
        self._bar_was_active = active

    # ── Interactive 3D preview viewport ─────────────────────────────────
    def _render_preview_viewport(self, ctx: InxGUIContext, w: float, h: float):
        """A scene-like 3D viewport: grid floor + cube in a virtual space.

        Rendered by the engine's GPU mesh-preview pipeline (same one used for FBX
        thumbnails) and shown as a texture. Right- or middle-drag orbits the camera;
        the mouse wheel zooms.
        """
        ctx.invisible_button("##tl_vp", w, h)
        x0 = ctx.get_item_rect_min_x()
        y0 = ctx.get_item_rect_min_y()
        x1 = ctx.get_item_rect_max_x()
        y1 = ctx.get_item_rect_max_y()
        hovered = ctx.is_item_hovered()

        # Zoom (wheel) and orbit (right/middle drag).
        if hovered:
            wheel = ctx.get_mouse_wheel_delta()
            if wheel:
                # Wide zoom range so the (now far-extending) grid can fill the view.
                self._cam_dist = max(2.0, min(40.0, self._cam_dist * (0.88 ** wheel)))
        drag_r = ctx.is_mouse_dragging(1)
        drag_m = ctx.is_mouse_dragging(2)
        if hovered and (drag_r or drag_m):
            self._orbiting = True
        if not (drag_r or drag_m):
            self._orbiting = False
        if self._orbiting:
            btn = 1 if drag_r else 2
            dx = ctx.get_mouse_drag_delta_x(btn)
            dy = ctx.get_mouse_drag_delta_y(btn)
            ctx.reset_mouse_drag_delta(btn)
            self._cam_yaw += dx * 0.012
            self._cam_pitch = max(-1.45, min(1.45, self._cam_pitch + dy * 0.012))

        c = _tl_colors()
        ctx.draw_filled_rect(x0, y0, x1, y1, *c["preview_bg"], 4.0)

        sampled = self._timeline.sample(self._playhead)
        if sampled is not None:
            pos, rot, scl = sampled
        else:
            pos, rot, scl = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]

        side = min(x1 - x0, y1 - y0)
        tex = self._cube_preview_texture(pos, rot, scl)
        if tex:
            cx = (x0 + x1) * 0.5
            cy = (y0 + y1) * 0.5
            ctx.draw_image_rect(tex, cx - side * 0.5, cy - side * 0.5, cx + side * 0.5, cy + side * 0.5,
                                0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, False, False, 3.0)
        ctx.draw_text(x0 + 8.0, y1 - 18.0, t("animtimeline_editor.preview"), *c["text"])

    def _cube_preview_texture(self, pos, rot, scl) -> int:
        """Schedule GPU cube preview; pump_preview_tasks() renders once per frame."""
        try:
            from .asset_resource_preview import _resolve_native_engine
            native = _resolve_native_engine(self)
            if native is None or not hasattr(native, "render_timeline_cube_preview"):
                return self._preview_tex_cache
            signature = tuple(round(float(value), 4) for value in (
                *pos, *rot, *scl, self._cam_yaw, self._cam_pitch, self._cam_dist
            ))
            # Native owns latest-wins backpressure: while one GPU submission is
            # active, newer states overwrite one pending slot and are rendered
            # as soon as it completes. Re-querying an identical state is cheap
            # and lets Python observe the texture once the native pump finishes.
            self._preview_request_signature = signature
            tex = int(native.render_timeline_cube_preview(
                float(pos[0]), float(pos[1]), float(pos[2]),
                float(rot[0]), float(rot[1]), float(rot[2]),
                float(scl[0]), float(scl[1]), float(scl[2]),
                float(self._cam_yaw), float(self._cam_pitch), float(self._cam_dist),
                _PREVIEW_RENDER_PX,
            ) or 0)
            if tex:
                self._preview_tex_cache = tex
            return tex or self._preview_tex_cache
        except Exception:
            return self._preview_tex_cache

    def _render_keyframe_inspector(self, ctx: InxGUIContext):
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("animtimeline_editor.keyframe"))
        ctx.pop_style_color(1)
        ctx.separator()

        k = self._current_sel_key()
        if k is None:
            ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DISABLED)
            ctx.label(t("animtimeline_editor.no_key_selected"))
            ctx.pop_style_color(1)
            return

        capture_semantics = _semantic_capture_enabled(ctx)

        # Time — inline label + field.
        ctx.label(t("animtimeline_editor.key_time"))
        ctx.same_line(_KF_LABEL_W)
        ctx.set_next_item_width(-1)
        nt = ctx.drag_float("##k_time", float(k.time), 0.01, 0.0, float(self._timeline.duration))
        if capture_semantics:
            ctx.record_semantic_item(
                "drag_float", t("animtimeline_editor.key_time"), True,
                "animtimeline.keyframe.time", numeric_value=nt,
            )
        if nt != k.time:
            k.time = max(0.0, min(float(self._timeline.duration), float(nt)))
            self._playhead = k.time
            self._set_dirty(True)

        # Transition — inline label + combo.
        ctx.label(t("animtimeline_editor.transition"))
        ctx.same_line(_KF_LABEL_W)
        ctx.set_next_item_width(-1)
        interp_labels = _interp_labels()
        idx = INTERP_MODES.index(k.interp) if k.interp in INTERP_MODES else 1
        nidx = ctx.combo("##k_interp", idx, interp_labels, len(interp_labels))
        if capture_semantics:
            ctx.record_semantic_item(
                "combo", t("animtimeline_editor.transition"), True,
                "animtimeline.keyframe.interpolation", string_value=INTERP_MODES[nidx],
            )
        if nidx != idx:
            k.interp = INTERP_MODES[nidx]
            self._set_dirty(True)

        ctx.dummy(0, 4)
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("animtimeline_editor.transform"))
        ctx.pop_style_color(1)
        ctx.separator()

        if self._vec3_row(
            ctx, "pos", t("animtimeline_editor.position"), k.position, 0.01,
            capture_semantics,
        ):
            self._set_dirty(True)
        if self._vec3_row(
            ctx, "rot", t("animtimeline_editor.rotation"), k.rotation, 0.25,
            capture_semantics,
        ):
            self._set_dirty(True)
        if self._vec3_row(
            ctx, "scl", t("animtimeline_editor.scale"), k.scale, 0.01,
            capture_semantics,
        ):
            self._set_dirty(True)

    @staticmethod
    def _vec3_row(
        ctx: InxGUIContext,
        vid: str,
        label: str,
        values,
        speed: float,
        capture_semantics: bool = True,
    ) -> bool:
        """Render a single-line ``label  [X][Y][Z]`` drag row. Mutates *values* in place."""
        ctx.label(label)
        ctx.same_line(_KF_LABEL_W)  # fixed column so all rows' fields align
        changed = False
        avail = ctx.get_content_region_avail_width()
        field_w = max(28.0, (avail - 12.0) / 3.0)
        for i, axis in enumerate(("X", "Y", "Z")):
            ctx.set_next_item_width(field_w)
            nv = ctx.drag_float(f"##{vid}_{axis}", float(values[i]), speed, -1.0e9, 1.0e9)
            if capture_semantics:
                ctx.record_semantic_item(
                    "drag_float", f"{label} {axis}", True,
                    f"animtimeline.keyframe.{vid}.{axis.lower()}", numeric_value=nv,
                )
            if nv != values[i]:
                values[i] = float(nv)
                changed = True
            if i < 2:
                ctx.same_line(0, 6)
        return changed
