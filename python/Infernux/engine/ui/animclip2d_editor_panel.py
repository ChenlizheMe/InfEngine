"""
2D Animation Clip Editor — visual editor for creating and editing .animclip2d files.

Drag a sprite-sheet texture onto the panel to load it, then click frames to
build animation sequences.  Supports multiple clips per texture, live
preview playback, and direct save to .animclip2d files.

Opened from Window menu → 2D Animation Clip Editor.
"""

from __future__ import annotations

import os
import copy
import json
import threading
import time
import zlib
from Infernux.engine.path_utils import path_key, resolved_path, same_path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.lib import InxGUIContext

from .asset_save_dialog import AssetSaveAsDialog
from .editor_panel import EditorPanel
from .igui import IGUI
from .panel_registry import editor_panel
from .theme import Theme, ImGuiCol, ImGuiStyleVar


# ═══════════════════════════════════════════════════════════════════════════
# Internal state
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _ClipState:
    """Editable state for one animation clip."""
    name: str = "NewClip"
    frame_indices: List[int] = field(default_factory=list)
    fps: float = 12.0
    loop: bool = True
    saved_path: str = ""          # .animclip2d file path (empty = unsaved)
    saved_texture_guid: str = ""
    saved_texture_path: str = ""


@dataclass
class _TextureState:
    """Cached sprite-sheet info for the currently loaded texture."""
    file_path: str = ""
    texture_id: int = 0
    tex_w: int = 0
    tex_h: int = 0
    frames: list = field(default_factory=list)   # List[SpriteFrame]
    guid: str = ""
    # Sampling state tracking — reschedule when these change
    filter_tag: str = ""
    srgb_tag: str = ""
    resource_key: str = ""
    stamp: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# Panel
# ═══════════════════════════════════════════════════════════════════════════

_PALETTE_THUMB_SIZE = 40.0   # frame slices in palette (bottom)
_SEQ_THUMB_SIZE = 36.0       # thumbnails in sequence strip
_THUMB_PAD = 3.0
_PREVIEW_MAX_SIZE = 320.0
_TABS_CARD_H = 44.0
_INFO_CARD_H = 44.0
_DETAILS_CARD_H = 124.0
_PREVIEW_CARD_H = 300.0
_SEQ_VISIBLE_ROWS = 2
_PALETTE_H = 180.0
_WIDE_LAYOUT_MIN_W = 920.0

_PLAYBACK_STOPPED = 0
_PLAYBACK_PLAYING = 1
_U64 = 0xFFFFFFFFFFFFFFFF


def _sprite_frame_imgui_uv(frame, tex_w: int, tex_h: int) -> Tuple[float, float, float, float]:
    """Map sprite-frame coordinates to the existing ImGui preview UV convention."""
    inv_w = float(max(tex_w, 1))
    inv_h = float(max(tex_h, 1))
    uv0_x = float(frame.x) / inv_w
    uv0_y = float(frame.y) / inv_h
    uv1_x = float(frame.x + frame.w) / inv_w
    uv1_y = float(frame.y + frame.h) / inv_h
    return uv0_x, uv0_y, uv1_x, uv1_y


@editor_panel(
    "2D Animation Clip Editor",
    type_id="animclip2d_editor",
    title_key="panel.animclip2d_editor",
    menu_path="Animation",
)
class AnimClip2DEditorPanel(EditorPanel):
    """Visual editor for building .animclip2d files from sprite sheets."""

    def __init__(self):
        super().__init__(title="2D Animation Clip Editor", window_id="animclip2d_editor")
        self._tex: Optional[_TextureState] = None
        self._clips: List[_ClipState] = [_ClipState()]
        self._active_clip_idx: int = 0
        # Preview playback
        self._playback: int = _PLAYBACK_STOPPED
        self._preview_frame_idx: int = 0  # index into active clip's frame_indices
        self._last_frame_time: float = 0.0
        self._dirty: bool = False
        self._last_saved_signature: str = ""
        self._last_saved_state: dict = {}
        self._save_as_dialog = AssetSaveAsDialog("animclip2d.save_as", "2D animation clip")
        self._pending_save_as_clip: Optional[_ClipState] = None
        self._mark_saved_snapshot()
        # A newly opened authoring window owns an untitled in-memory document.
        self._last_saved_signature = ""
        self._dirty = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _initial_size(self):
        return (1080, 760)

    def on_enable(self):
        from .event_bus import EditorEvent, EditorEventBus

        EditorEventBus.instance().subscribe(
            EditorEvent.ASSET_CHANGED,
            self._on_asset_changed,
        )

    def on_disable(self):
        from .event_bus import EditorEvent, EditorEventBus

        EditorEventBus.instance().unsubscribe(
            EditorEvent.ASSET_CHANGED,
            self._on_asset_changed,
        )
        self._cleanup_texture()
        try:
            from Infernux.engine.project_context import set_panel_dirty

            set_panel_dirty(self.window_id, False)
        except Exception:
            pass

    def _on_asset_changed(self, file_path: str, event_type: str = "modified") -> None:
        """Publish texture import changes into the open editor immediately."""
        tex = self._tex
        if tex is None or not same_path(file_path, tex.file_path):
            return
        if event_type == "deleted":
            self._cleanup_texture()
            self._tex = None
            return

        filter_tag, srgb_tag, _, _ = self._read_texture_sampling(tex.file_path)
        frames = self._read_sprite_frames(tex.file_path)
        source_w, source_h = self._read_source_dimensions(tex.file_path, frames)
        if not frames and source_w > 0 and source_h > 0:
            from Infernux.core.asset_types import SpriteFrame

            frames = [SpriteFrame(
                name="frame_0",
                x=0,
                y=0,
                w=source_w,
                h=source_h,
            )]

        tex.frames = frames
        if source_w > 0:
            tex.tex_w = source_w
        if source_h > 0:
            tex.tex_h = source_h
        tex.filter_tag = filter_tag
        tex.srgb_tag = srgb_tag
        tex.resource_key = (
            f"animclip_editor|{srgb_tag}_{filter_tag}|{resolved_path(tex.file_path)}"
        )
        tex.stamp = self._build_texture_stamp(
            tex.file_path,
            filter_tag,
            srgb_tag,
        )

    def _window_title_suffix(self) -> str:
        self._recompute_dirty()
        self._sync_project_dirty_flag()
        return " *" if self._dirty else ""

    def _sync_project_dirty_flag(self) -> None:
        try:
            from Infernux.engine.project_context import set_panel_dirty

            set_panel_dirty(self.window_id, bool(self._dirty))
        except Exception:
            pass

    def _current_edit_signature(self) -> str:
        tex = self._tex
        blob = {
            "texture_path": tex.file_path if tex else "",
            "texture_guid": tex.guid if tex else "",
            "active": self._active_clip_idx,
            "clips": [
                {
                    "name": c.name,
                    "frames": list(c.frame_indices),
                    "fps": float(c.fps),
                    "loop": bool(c.loop),
                    "saved_path": c.saved_path,
                    "saved_texture_guid": c.saved_texture_guid,
                    "saved_texture_path": c.saved_texture_path,
                }
                for c in self._clips
            ],
        }
        return json.dumps(blob, sort_keys=True, ensure_ascii=True)

    def _mark_saved_snapshot(self) -> None:
        self._last_saved_signature = self._current_edit_signature()
        self._dirty = False
        self._last_saved_state = copy.deepcopy(self.save_state())
        self._sync_project_dirty_flag()

    def _recompute_dirty(self) -> None:
        self._dirty = self._current_edit_signature() != self._last_saved_signature

    def save_state(self) -> dict:
        clips_data = []
        for c in self._clips:
            clips_data.append({
                "name": c.name, "frame_indices": c.frame_indices,
                "fps": c.fps, "loop": c.loop, "saved_path": c.saved_path,
                "saved_texture_guid": c.saved_texture_guid,
                "saved_texture_path": c.saved_texture_path,
            })
        d: dict = {
            "active_clip": self._active_clip_idx,
            "clips": clips_data,
            "dirty": bool(self._dirty),
        }
        if self._tex:
            d["texture_path"] = self._tex.file_path
        return d

    def load_state(self, data: dict):
        restore_dirty = bool(data.get("dirty", False))
        tex_path = data.get("texture_path", "")
        if tex_path and os.path.isfile(tex_path):
            self._load_texture(tex_path)
        clips_data = data.get("clips", [])
        if clips_data:
            self._clips = []
            for cd in clips_data:
                self._clips.append(_ClipState(
                    name=cd.get("name", "NewClip"),
                    frame_indices=list(cd.get("frame_indices", [])),
                    fps=float(cd.get("fps", 12.0)),
                    loop=bool(cd.get("loop", True)),
                    saved_path=cd.get("saved_path", ""),
                    saved_texture_guid=cd.get("saved_texture_guid", ""),
                    saved_texture_path=cd.get("saved_texture_path", tex_path if cd.get("saved_path", "") else ""),
                ))
        if not self._clips:
            self._clips = [_ClipState()]
        self._active_clip_idx = max(0, min(
            int(data.get("active_clip", 0)), len(self._clips) - 1))
        self._mark_saved_snapshot()
        if restore_dirty:
            self._last_saved_signature = ""
            self._dirty = True
            self._sync_project_dirty_flag()

    # ------------------------------------------------------------------
    # Render — layout: header -> tabs -> preview/details -> sequence -> palette
    # ------------------------------------------------------------------

    # ImGuiKey / ImGuiMod constants
    def on_render_content(self, ctx: InxGUIContext):
        self._recompute_dirty()
        self._sync_project_dirty_flag()
        try:
            avail_w = ctx.get_content_region_avail_width()
            self._render_texture_slot(ctx, avail_w)
            ctx.dummy(0, 8)

            if self._tex is None:
                self._render_empty_state(ctx)
                return

            # Guard against stale texture (e.g. import settings changed externally)
            if not self._validate_texture():
                self._render_empty_state(ctx)
                return

            if self._tex.texture_id == 0:
                self._render_empty_state(ctx)
                return

            try:
                clip = self._active_clip
                if clip is None:
                    return

                self._render_main_workspace(ctx, clip, ctx.get_content_region_avail_width())

            except Exception as exc:
                Debug.log_warning(f"[AnimClipEditor] Render error: {exc}")
        finally:
            self._render_save_as_dialog(ctx)

    def _render_save_as_dialog(self, ctx: InxGUIContext) -> None:
        self._save_as_dialog.render(
            ctx,
            self._save_pending_clip,
            cancel_callback=self._cancel_pending_save_as,
        )

    # ------------------------------------------------------------------
    # Header / empty state helpers
    # ------------------------------------------------------------------

    def _render_texture_slot(self, ctx: InxGUIContext, avail_w: float):
        tex = self._tex
        display = os.path.basename(tex.file_path) if tex else t("animclip_editor.drop_texture_hint")

        IGUI.object_field(
            ctx, "animclip_tex_slot", display, "Texture",
            clickable=False,
            accept="TEXTURE_FILE",
            on_drop=self._on_texture_drop,
            on_clear=self._on_texture_clear if tex else None,
            semantic_id="animclip2d.texture",
        )

        IGUI.drop_target(ctx, "ANIMCLIP_FILE", self._on_animclip_drop, outline=True)

    def _on_texture_clear(self):
        self._cleanup_texture()
        self._tex = None

    def _empty_state_hint(self) -> str:
        return t("animclip_editor.drop_texture_hint")

    def _empty_state_drop_types(self):
        return ["TEXTURE_FILE", "ANIMCLIP_FILE"]

    def _on_empty_state_drop(self, drop_type: str, payload):
        if drop_type == "TEXTURE_FILE":
            self._on_texture_drop(payload)
        elif drop_type == "ANIMCLIP_FILE":
            self._on_animclip_drop(payload)

    def _render_main_workspace(self, ctx: InxGUIContext, clip: _ClipState, avail_w: float):
        wide = avail_w >= _WIDE_LAYOUT_MIN_W

        # Name / save / basic info card — always on top, full width
        ctx.begin_child("##animclip_info_card", avail_w, _INFO_CARD_H, True)
        try:
            self._render_clip_info(ctx, clip, max(avail_w - 20.0, 120.0))
        finally:
            ctx.end_child()

        ctx.dummy(0, 8)

        if wide:
            seq_w = min(max(avail_w * 0.48, 340.0), 460.0)
            preview_w = max(avail_w - seq_w - 10.0, 260.0)

            ctx.begin_child("##animclip_sequence_card", seq_w, _PREVIEW_CARD_H, True)
            try:
                self._render_sequence_content(ctx, clip)
            finally:
                ctx.end_child()

            ctx.same_line(0, 10)

            ctx.begin_child("##animclip_preview_card", preview_w, _PREVIEW_CARD_H, True)
            try:
                self._render_preview(ctx, clip, preview_w)
            finally:
                ctx.end_child()
        else:
            ctx.begin_child("##animclip_preview_card", avail_w, _DETAILS_CARD_H, True)
            try:
                self._render_preview(ctx, clip, avail_w)
            finally:
                ctx.end_child()

            ctx.dummy(0, 8)

            ctx.begin_child("##animclip_sequence_card", avail_w, _PREVIEW_CARD_H, True)
            try:
                self._render_sequence_content(ctx, clip)
            finally:
                ctx.end_child()

        ctx.dummy(0, 8)
        self._render_frame_palette(ctx, avail_w)

    @staticmethod
    def _calc_grid_cols(width: float, thumb_size: float) -> int:
        cell_w = thumb_size + 16.0
        return max(1, int(max(width, thumb_size) / max(cell_w, 1.0)))

    # ------------------------------------------------------------------
    # Clip tabs
    # ------------------------------------------------------------------

    def _render_clip_tabs(self, ctx: InxGUIContext):
        ctx.begin_group()
        for i, clip in enumerate(self._clips):
            if i > 0:
                ctx.same_line(0, 2)
            is_active = (i == self._active_clip_idx)

            if is_active:
                ctx.push_style_color(ImGuiCol.Button, 0.25, 0.45, 0.7, 1.0)

            label = clip.name if clip.name else f"Clip {i}"
            clicked = ctx.button(f"{label}##clip_tab_{i}")
            ctx.record_semantic_item(
                "button", label, True, f"animclip2d.clip_tab.{i}",
            )
            if clicked:
                self._active_clip_idx = i
                self._stop_playback()

            if is_active:
                ctx.pop_style_color(1)

        ctx.same_line(0, 8)
        add_clicked = ctx.button(f"+##add_clip")
        ctx.record_semantic_item("button", "Add Clip", True, "animclip2d.clip.add")
        if add_clicked:
            self._clips.append(_ClipState(name=f"Clip_{len(self._clips)}"))
            self._active_clip_idx = len(self._clips) - 1
            self._stop_playback()

        # Delete clip (only if more than 1)
        if len(self._clips) > 1:
            ctx.same_line(0, 8)
            ctx.push_style_color(ImGuiCol.Button, 0.6, 0.15, 0.15, 0.8)
            delete_label = t("animclip_editor.delete_clip")
            delete_clicked = ctx.button(delete_label)
            ctx.record_semantic_item("button", delete_label, True, "animclip2d.clip.delete")
            if delete_clicked:
                idx = self._active_clip_idx
                self._clips.pop(idx)
                self._active_clip_idx = max(0, min(idx, len(self._clips) - 1))
                self._stop_playback()
            ctx.pop_style_color(1)

        ctx.end_group()

    # ------------------------------------------------------------------
    # Clip info — name, fps, save (compact top bar)
    # ------------------------------------------------------------------

    def _render_clip_info(self, ctx: InxGUIContext, clip: _ClipState, avail_w: float):
        # Row: Name + Save
        name_label = t("animclip_editor.clip_name")

        ctx.label(name_label)
        ctx.same_line(0, 4)
        ctx.set_next_item_width(min(140, avail_w * 0.2))
        new_name = ctx.text_input("##clip_name", clip.name, 256)
        ctx.record_semantic_item(
            "text_input", f"{name_label}: {new_name}", True, "animclip2d.clip.name",
        )
        if new_name != clip.name:
            clip.name = new_name
            # Notify FSM editor of clip name change
            if clip.saved_path:
                from .event_bus import EditorEventBus
                EditorEventBus.instance().emit("clip_name_changed", clip.saved_path, new_name)

        # Save button on same line as name
        ctx.same_line(0, 16)
        fc = len(clip.frame_indices)
        save_label = t("animclip_editor.save_clip")
        can_save = self._tex is not None and fc > 0
        if not can_save:
            ctx.begin_disabled(True)
        save_clicked = ctx.button(save_label + "##info_save")
        ctx.record_semantic_item(
            "button", save_label, can_save, "animclip2d.clip.save",
        )
        if save_clicked:
            self._save_clip(clip)
        ctx.same_line(0, 4)
        save_as_label = t("animclip_editor.save_as")
        save_as_clicked = ctx.button(save_as_label + "##info_save_as")
        ctx.record_semantic_item(
            "button", save_as_label, can_save, "animclip2d.clip.save_as",
        )
        if save_as_clicked:
            self._show_save_as_dialog(clip)
        if not can_save:
            ctx.end_disabled()
        if clip.saved_path:
            ctx.same_line(0, 8)
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(os.path.basename(clip.saved_path))
            ctx.pop_style_color(1)

    # ------------------------------------------------------------------
    # Preview — centered animated playback with transport controls
    # ------------------------------------------------------------------

    def _render_preview(self, ctx: InxGUIContext, clip: _ClipState, avail_w: float):
        tex = self._tex
        if tex is None:
            return

        ctx.label(t("animclip_editor.preview"))
        preview_frame = None
        preview_source_index = -1
        if clip.frame_indices:
            safe_idx = max(0, min(self._preview_frame_idx, len(clip.frame_indices) - 1))
            preview_source_index = clip.frame_indices[safe_idx]
            if 0 <= preview_source_index < len(tex.frames):
                preview_frame = tex.frames[preview_source_index]

        if preview_frame is not None:
            ctx.same_line(0, 8)
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(f"#{preview_source_index}  {preview_frame.w}x{preview_frame.h}")
            ctx.pop_style_color(1)

        ctx.separator()

        # Transport controls row
        fc = len(clip.frame_indices)
        if not fc:
            ctx.begin_disabled(True)

        is_playing = self._playback == _PLAYBACK_PLAYING
        if is_playing:
            play_label = t("animclip_editor.pause")
            play_clicked = ctx.button(play_label + "##transport")
            ctx.record_semantic_item(
                "button", play_label, bool(fc), "animclip2d.transport.play_pause",
            )
            if play_clicked:
                self._playback = _PLAYBACK_STOPPED
        else:
            play_label = t("animclip_editor.play")
            play_clicked = ctx.button(play_label + "##transport")
            ctx.record_semantic_item(
                "button", play_label, bool(fc), "animclip2d.transport.play_pause",
            )
            if play_clicked:
                self._playback = _PLAYBACK_PLAYING
                self._last_frame_time = time.perf_counter()
                if self._preview_frame_idx >= fc:
                    self._preview_frame_idx = 0

        ctx.same_line(0, 4)
        stop_label = t("animclip_editor.stop")
        stop_clicked = ctx.button(stop_label + "##transport")
        ctx.record_semantic_item(
            "button", stop_label, bool(fc), "animclip2d.transport.stop",
        )
        if stop_clicked:
            self._stop_playback()

        ctx.same_line(0, 8)
        step_back_clicked = ctx.button("|<##step_back")
        ctx.record_semantic_item(
            "button", "Previous Frame", bool(fc), "animclip2d.transport.previous",
        )
        if step_back_clicked:
            self._preview_frame_idx = max(0, self._preview_frame_idx - 1)
        ctx.same_line(0, 2)
        step_forward_clicked = ctx.button(">|##step_fwd")
        ctx.record_semantic_item(
            "button", "Next Frame", bool(fc), "animclip2d.transport.next",
        )
        if step_forward_clicked:
            self._preview_frame_idx = min(
                max(0, fc - 1), self._preview_frame_idx + 1)

        if fc:
            ctx.same_line(0, 12)
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(f"{self._preview_frame_idx + 1}/{fc}")
            ctx.pop_style_color(1)

        # FPS / duration — always editable
        fps_label = t("animclip_editor.clip_fps")
        ctx.same_line(0, 16)
        ctx.label(fps_label)
        ctx.same_line(0, 4)
        ctx.set_next_item_width(60)
        new_fps = ctx.drag_float("##clip_fps", clip.fps, 0.1, 0.1, 120.0)
        ctx.record_semantic_item(
            "drag_float", f"{fps_label}: {new_fps:g}", bool(fc), "animclip2d.clip.fps",
        )
        if new_fps != clip.fps:
            clip.fps = max(0.1, new_fps)

        dur = fc / max(clip.fps, 0.1)
        ctx.same_line(0, 8)
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label(f"{fc}f {dur:.2f}s")
        ctx.pop_style_color(1)

        ctx.same_line(0, 8)
        loop_label = t("animclip_editor.clip_loop")
        new_loop = ctx.checkbox(f"{loop_label}##clip_loop", clip.loop)
        ctx.record_semantic_item(
            "checkbox", loop_label, bool(fc), "animclip2d.clip.loop", bool(new_loop),
        )
        clip.loop = bool(new_loop)

        if not fc:
            ctx.end_disabled()

        # Preview area
        ctx.begin_child("##preview_area", 0, 0, False)
        try:
            child_w = ctx.get_content_region_avail_width()
            child_h = ctx.get_content_region_avail_height()

            if not clip.frame_indices:
                ctx.dummy(0, child_h * 0.4)
                hint = t("animclip_editor.sequence_empty_hint")
                tw = ctx.calc_text_width(hint)
                ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + (child_w - tw) * 0.5)
                ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
                ctx.label(hint)
                ctx.pop_style_color(1)
            else:
                is_playing = self._playback == _PLAYBACK_PLAYING

                # Advance frame if playing
                if is_playing and clip.fps > 0:
                    now = time.perf_counter()
                    elapsed = now - self._last_frame_time
                    interval = 1.0 / clip.fps
                    if elapsed >= interval:
                        steps = int(elapsed / interval)
                        self._preview_frame_idx += steps
                        self._last_frame_time = now
                        if self._preview_frame_idx >= fc:
                            self._preview_frame_idx = self._preview_frame_idx % fc

                self._preview_frame_idx = max(0, min(self._preview_frame_idx, fc - 1))
                fidx = clip.frame_indices[self._preview_frame_idx]

                if 0 <= fidx < len(tex.frames):
                    frame = tex.frames[fidx]
                    uv0_x, uv0_y, uv1_x, uv1_y = _sprite_frame_imgui_uv(frame, tex.tex_w, tex.tex_h)

                    # Fit preview into available space, centered
                    max_dim = max(8.0, min(_PREVIEW_MAX_SIZE, child_w - 24.0, child_h - 16.0))
                    aspect = frame.w / max(frame.h, 1)
                    if aspect >= 1.0:
                        pw = max_dim
                        ph = max_dim / aspect
                    else:
                        ph = max_dim
                        pw = max_dim * aspect

                    # Center horizontally and vertically
                    pad_x = (child_w - pw) * 0.5
                    pad_y = (child_h - ph) * 0.5
                    if pad_x > 0:
                        ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + pad_x)
                    if pad_y > 0:
                        ctx.set_cursor_pos_y(ctx.get_cursor_pos_y() + pad_y)

                    if tex.texture_id:
                        ctx.image(tex.texture_id, pw, ph, uv0_x, uv0_y, uv1_x, uv1_y)
        except Exception as exc:
            Debug.log_warning(f"[AnimClipEditor] Preview error: {exc}")
        finally:
            ctx.end_child()

    # ------------------------------------------------------------------
    # Sequence strip (ordered frame thumbnails)
    # ------------------------------------------------------------------

    def _render_sequence_content(self, ctx: InxGUIContext, clip: _ClipState):
        """Render frame sequence content (called inside a wrapping begin_child)."""
        tex = self._tex
        if tex is None or tex.texture_id == 0:
            return

        ctx.label(t("animclip_editor.sequence"))
        if clip.frame_indices:
            clear_label = t("animclip_editor.clear_sequence")
            clear_w = ctx.calc_text_width(clear_label) + 20.0
            ctx.same_line(max(ctx.get_window_width() - clear_w - 18.0, 180.0))
            clear_clicked = ctx.button(clear_label + "##seq_clear")
            ctx.record_semantic_item(
                "button", clear_label, True, "animclip2d.sequence.clear",
            )
            if clear_clicked:
                clip.frame_indices = []
                self._stop_playback()

        ctx.separator()
        ctx.begin_child("##seq_strip", 0, 0, False)
        try:
            if not clip.frame_indices:
                ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
                ctx.label(t("animclip_editor.sequence_empty_hint"))
                ctx.pop_style_color(1)
            else:
                thumb = _SEQ_THUMB_SIZE
                child_w = ctx.get_content_region_avail_width()
                cols = self._calc_grid_cols(child_w, thumb)
                to_remove = None

                if ctx.begin_table("##seq_grid", cols, 0, 0.0):
                    for seq_i, frame_idx in enumerate(clip.frame_indices):
                        ctx.table_next_column()

                        if 0 <= frame_idx < len(tex.frames):
                            frame = tex.frames[frame_idx]
                            uv0_x, uv0_y, uv1_x, uv1_y = _sprite_frame_imgui_uv(frame, tex.tex_w, tex.tex_h)

                            # Aspect-preserving image inside square button
                            aspect = frame.w / max(frame.h, 1)
                            if aspect >= 1.0:
                                iw, ih = thumb, thumb / aspect
                            else:
                                iw, ih = thumb * aspect, thumb

                            is_preview = (
                                self._playback == _PLAYBACK_PLAYING
                                and seq_i == self._preview_frame_idx
                            )
                            if is_preview:
                                ctx.push_style_color(ImGuiCol.Button, 0.2, 0.6, 0.2, 0.8)
                                ctx.push_style_color(ImGuiCol.ButtonHovered, 0.3, 0.7, 0.3, 0.8)

                            save_x = ctx.get_cursor_pos_x()
                            save_y = ctx.get_cursor_pos_y()
                            ctx.button(f"##seq_{seq_i}", width=thumb, height=thumb)
                            ctx.record_semantic_item(
                                "button", f"Sequence {seq_i}: Frame {frame_idx}", True,
                                f"animclip2d.sequence.frame.{seq_i}",
                            )
                            hovered = ctx.is_item_hovered()

                            if is_preview:
                                ctx.pop_style_color(2)

                            bx = ctx.get_item_rect_min_x()
                            by = ctx.get_item_rect_min_y()

                            # Overlay centered image
                            ctx.set_cursor_pos_x(save_x + (thumb - iw) * 0.5)
                            ctx.set_cursor_pos_y(save_y + (thumb - ih) * 0.5)
                            ctx.image(tex.texture_id, iw, ih, uv0_x, uv0_y, uv1_x, uv1_y)

                            ctx.draw_text(bx + 2, by + 1, str(frame_idx), 1.0, 1.0, 1.0, 0.9)

                            if hovered and ctx.is_mouse_button_clicked(1):
                                to_remove = seq_i
                        else:
                            ctx.button(f"?##seq_{seq_i}", width=thumb, height=thumb)
                            ctx.record_semantic_item(
                                "button", f"Sequence {seq_i}: Missing Frame {frame_idx}", True,
                                f"animclip2d.sequence.frame.{seq_i}",
                            )

                    ctx.end_table()

                if to_remove is not None:
                    clip.frame_indices.pop(to_remove)
        finally:
            ctx.end_child()

    # ------------------------------------------------------------------
    # Frame palette — grid of sprite slices at the bottom
    # ------------------------------------------------------------------

    def _render_frame_palette(self, ctx: InxGUIContext, avail_w: float):
        tex = self._tex
        if tex is None or tex.texture_id == 0:
            return

        palette_h = max(ctx.get_content_region_avail_height(), _PALETTE_H)
        ctx.begin_child("##animclip_palette_card", avail_w, palette_h, True)
        try:
            ctx.label(t("animclip_editor.frame_palette"))
            ctx.separator()

            ctx.begin_child("##frame_palette", 0, 0, False)
            try:
                active_clip = self._active_clip
                thumb = _PALETTE_THUMB_SIZE
                child_w = ctx.get_content_region_avail_width()
                cols = self._calc_grid_cols(child_w, thumb)

                if ctx.begin_table("##palette_grid", cols, 0, 0.0):
                    for i, frame in enumerate(tex.frames):
                        ctx.table_next_column()
                        uv0_x, uv0_y, uv1_x, uv1_y = _sprite_frame_imgui_uv(frame, tex.tex_w, tex.tex_h)

                        # Aspect-preserving image inside square button
                        aspect = frame.w / max(frame.h, 1)
                        if aspect >= 1.0:
                            iw, ih = thumb, thumb / aspect
                        else:
                            iw, ih = thumb * aspect, thumb

                        in_clip = (
                            active_clip is not None
                            and i in active_clip.frame_indices
                        )
                        if in_clip:
                            ctx.push_style_color(ImGuiCol.Button, 0.2, 0.5, 0.8, 0.6)
                            ctx.push_style_color(ImGuiCol.ButtonHovered, 0.3, 0.6, 0.9, 0.7)

                        save_x = ctx.get_cursor_pos_x()
                        save_y = ctx.get_cursor_pos_y()
                        clicked = ctx.button(f"##palette_{i}", width=thumb, height=thumb)
                        ctx.record_semantic_item(
                            "button", f"Frame {i}: {frame.name}", True,
                            f"animclip2d.palette.frame.{i}",
                        )
                        hovered = ctx.is_item_hovered()

                        if in_clip:
                            ctx.pop_style_color(2)

                        bx = ctx.get_item_rect_min_x()
                        by = ctx.get_item_rect_min_y()

                        # Overlay centered image
                        ctx.set_cursor_pos_x(save_x + (thumb - iw) * 0.5)
                        ctx.set_cursor_pos_y(save_y + (thumb - ih) * 0.5)
                        ctx.image(tex.texture_id, iw, ih, uv0_x, uv0_y, uv1_x, uv1_y)

                        if clicked and active_clip is not None:
                            active_clip.frame_indices.append(i)

                        if hovered:
                            ctx.set_tooltip(f"#{i}  {frame.name}  ({frame.w}x{frame.h})")

                        ctx.draw_text(bx + 2, by + 1, str(i), 1.0, 1.0, 1.0, 0.9)

                    ctx.end_table()
            finally:
                ctx.end_child()

        except Exception as exc:
            Debug.log_warning(f"[AnimClipEditor] Frame palette error: {exc}")
        finally:
            ctx.end_child()

    # ------------------------------------------------------------------
    # Texture loading
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_mtime_ns(path: str) -> int:
        try:
            return int(os.stat(path).st_mtime_ns)
        except OSError:
            return 0

    @classmethod
    def _build_texture_stamp(cls, file_path: str, filter_tag: str, srgb_tag: str) -> int:
        image_mtime = cls._safe_mtime_ns(file_path)
        meta_mtime = cls._safe_mtime_ns(f"{file_path}.meta")
        base = int((image_mtime ^ ((meta_mtime * 2654435761) & _U64)) & _U64)
        setting_hash = int(zlib.crc32(f"{filter_tag}|{srgb_tag}".encode("utf-8")) & 0xFFFFFFFF)
        return int((base ^ setting_hash) & _U64)

    @staticmethod
    def _get_native_engine():
        try:
            from .editor_services import EditorServices

            svc = EditorServices.instance()
            return svc.native_engine if svc else None
        except Exception:
            return None

    @staticmethod
    def _read_texture_sampling(file_path: str):
        filter_tag = "default"
        srgb_tag = "linear"
        use_nearest = False
        use_srgb = False
        try:
            from Infernux.core.asset_types import read_texture_import_settings, FilterMode

            settings = read_texture_import_settings(file_path)
            cur_filter = getattr(settings, 'filter_mode', None)
            use_srgb = bool(getattr(settings, 'srgb', False))
            filter_tag = cur_filter.name if cur_filter else "default"
            srgb_tag = "srgb" if use_srgb else "linear"
            use_nearest = (cur_filter == FilterMode.POINT)
        except Exception:
            pass
        return filter_tag, srgb_tag, use_nearest, use_srgb

    @staticmethod
    def _read_sprite_frames(file_path: str):
        frames = []
        try:
            from Infernux.core.asset_types import read_texture_import_settings

            settings = read_texture_import_settings(file_path)
            if settings and settings.sprite_frames:
                frames = list(settings.sprite_frames)
        except Exception:
            pass
        return frames

    @staticmethod
    def _read_source_dimensions(file_path: str, frames=None):
        source_w = 0
        source_h = 0
        try:
            from Infernux.core.asset_types import read_meta_file

            meta = read_meta_file(file_path) or {}
            source_w = int(meta.get('width', 0) or 0)
            source_h = int(meta.get('height', 0) or 0)
        except Exception:
            pass

        if (source_w <= 0 or source_h <= 0) and frames:
            source_w = max((int(f.x) + int(f.w) for f in frames), default=0)
            source_h = max((int(f.y) + int(f.h) for f in frames), default=0)

        return source_w, source_h

    def _on_texture_drop(self, payload):
        """Handle TEXTURE_FILE drop — payload is a file path."""
        if isinstance(payload, str) and payload:
            self._load_texture(payload)

    def _on_animclip_drop(self, payload):
        """Handle ANIMCLIP_FILE drop — open an existing .animclip2d."""
        if isinstance(payload, str) and payload:
            self._open_animclip(payload)

    def _load_texture(self, file_path: str):
        """Load a sprite-sheet texture and extract frame data from its .meta."""
        self._tex = None
        if not file_path or not os.path.isfile(file_path):
            return

        native = self._get_native_engine()
        if not native:
            Debug.log_warning("[AnimClipEditor] No native engine")
            return

        filter_tag, srgb_tag, use_nearest, use_srgb = self._read_texture_sampling(file_path)
        norm_path = resolved_path(file_path)
        resource_key = f"animclip_editor|{srgb_tag}_{filter_tag}|{norm_path}"
        stamp = self._build_texture_stamp(norm_path, filter_tag, srgb_tag)
        if stamp == 0:
            return

        native.pump_preview_tasks()
        texture_id, tex_w, tex_h = native.query_or_schedule_texture_preview(
            resource_key, norm_path, int(stamp), nearest=bool(use_nearest),
            srgb=bool(use_srgb), pump=False)
        texture_id = int(texture_id)
        tex_w = int(tex_w)
        tex_h = int(tex_h)

        # Read sprite frames from .meta
        frames = self._read_sprite_frames(norm_path)
        source_w, source_h = self._read_source_dimensions(norm_path, frames)
        if source_w <= 0:
            source_w = max(tex_w, 0)
        if source_h <= 0:
            source_h = max(tex_h, 0)

        if not frames and source_w > 0 and source_h > 0:
            from Infernux.core.asset_types import SpriteFrame

            frames = [SpriteFrame(name="frame_0", x=0, y=0, w=source_w, h=source_h)]

        # Resolve GUID
        guid = ""
        try:
            from Infernux.engine.bootstrap import EditorBootstrap
            adb = EditorBootstrap.instance().engine.get_asset_database()
            if adb:
                guid = adb.get_guid_from_path(file_path) or ""
        except Exception:
            pass

        self._tex = _TextureState(
            file_path=norm_path,
            texture_id=texture_id,
            tex_w=max(source_w, 0),
            tex_h=max(source_h, 0),
            frames=frames,
            guid=guid,
            filter_tag=filter_tag,
            srgb_tag=srgb_tag,
            resource_key=resource_key,
            stamp=int(stamp),
        )

    def _cleanup_texture(self):
        """Invalidate preview task entry for the currently loaded texture."""
        tex = self._tex
        native = self._get_native_engine()
        if tex and native and tex.resource_key:
            try:
                native.invalidate_texture_preview_task(tex.resource_key)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Texture validation — detect stale handles & sampling changes
    # ------------------------------------------------------------------

    def _validate_texture(self) -> bool:
        """Check if the loaded texture is still valid; re-upload if needed.

        Returns False if the texture is irrecoverable (file gone, etc.).
        """
        tex = self._tex
        if tex is None:
            return False

        if not tex.file_path or not os.path.isfile(tex.file_path):
            return False

        native = self._get_native_engine()
        if native is None:
            return False

        filter_tag, srgb_tag, use_nearest, use_srgb = self._read_texture_sampling(tex.file_path)
        norm_path = resolved_path(tex.file_path)
        resource_key = f"animclip_editor|{srgb_tag}_{filter_tag}|{norm_path}"
        stamp = self._build_texture_stamp(norm_path, filter_tag, srgb_tag)
        if stamp == 0:
            return False

        if tex.resource_key != resource_key or int(tex.stamp) != int(stamp):
            tex.resource_key = resource_key
            tex.stamp = int(stamp)
            tex.filter_tag = filter_tag
            tex.srgb_tag = srgb_tag
            tex.texture_id = 0

        native.pump_preview_tasks()
        texture_id, tex_w, tex_h = native.query_or_schedule_texture_preview(
            resource_key, norm_path, int(stamp), nearest=bool(use_nearest),
            srgb=bool(use_srgb), pump=False)
        texture_id = int(texture_id)
        tex_w = int(tex_w)
        tex_h = int(tex_h)

        # Never keep a stale handle: a zero result means the native texture is
        # not currently published (evicted or re-rendering), and the previous
        # descriptor may already be freed — binding it crashes the driver.
        tex.texture_id = texture_id

        if tex_w > 0 and tex_h > 0:
            tex.tex_w = tex_w
            tex.tex_h = tex_h

        frames = self._read_sprite_frames(norm_path)
        source_w, source_h = self._read_source_dimensions(norm_path, frames)
        if source_w <= 0:
            source_w = max(tex_w, tex.tex_w, 0)
        if source_h <= 0:
            source_h = max(tex_h, tex.tex_h, 0)

        if source_w > 0:
            tex.tex_w = source_w
        if source_h > 0:
            tex.tex_h = source_h

        if frames:
            tex.frames = frames
        elif not tex.frames and tex.tex_w > 0 and tex.tex_h > 0:
            from Infernux.core.asset_types import SpriteFrame

            tex.frames = [SpriteFrame(name="frame_0", x=0, y=0, w=tex.tex_w, h=tex.tex_h)]

        return True

    # ------------------------------------------------------------------
    # Open existing .animclip2d
    # ------------------------------------------------------------------

    def _open_animclip(self, animclip_path: str):
        """Load an existing .animclip2d file into the editor."""
        from Infernux.core.animation_clip import AnimationClip
        clip_data = AnimationClip.load(animclip_path)
        if clip_data is None:
            Debug.log_warning(f"[AnimClipEditor] Failed to load: {animclip_path}")
            return

        # Resolve texture from clip's GUID or path
        tex_resolved = False
        if clip_data.authoring_texture_guid:
            try:
                from Infernux.engine.bootstrap import EditorBootstrap
                adb = EditorBootstrap.instance().engine.get_asset_database()
                if adb:
                    tex_path = adb.get_path_from_guid(clip_data.authoring_texture_guid)
                    if tex_path and os.path.isfile(tex_path):
                        self._load_texture(tex_path)
                        tex_resolved = True
            except Exception:
                pass

        if not tex_resolved and clip_data.authoring_texture_path:
            tp = clip_data.authoring_texture_path
            if os.path.isfile(tp):
                self._load_texture(tp)
            else:
                # Try relative to project root
                try:
                    from Infernux.engine.project_context import get_project_root
                    pr = get_project_root()
                    if pr:
                        abs_tp = os.path.join(pr, tp)
                        if os.path.isfile(abs_tp):
                            self._load_texture(abs_tp)
                except Exception:
                    pass

        # Import clip state
        cs = _ClipState(
            name=clip_data.name,
            frame_indices=list(clip_data.frame_indices),
            fps=clip_data.fps,
            loop=clip_data.loop,
            saved_path=animclip_path,
            saved_texture_guid=clip_data.authoring_texture_guid or (self._tex.guid if self._tex else ""),
            saved_texture_path=self._tex.file_path if self._tex else clip_data.authoring_texture_path,
        )
        self._clips = [cs]
        self._active_clip_idx = 0
        self._stop_playback()
        self._mark_saved_snapshot()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_clip(self, clip: _ClipState):
        """Save the active clip as a .animclip2d file."""
        if clip.saved_path and self._can_direct_save_clip(clip):
            self._do_save_clip(clip, clip.saved_path)
            return
        self._show_save_as_dialog(clip)

    def handle_save_command(self, save_as: bool = False) -> bool:
        clip = self._active_clip
        if clip is None or self._tex is None or not clip.frame_indices:
            return True
        if save_as:
            self._show_save_as_dialog(clip)
        else:
            self._save_clip(clip)
        return True

    @staticmethod
    def _texture_identity(guid: str = "", path: str = "") -> str:
        guid = (guid or "").strip()
        if guid:
            return f"guid:{guid}"
        path = (path or "").strip()
        if path:
            return "path:" + path_key(path)
        return ""

    def _current_texture_identity(self) -> str:
        tex = self._tex
        if tex is None:
            return ""
        return self._texture_identity(tex.guid, tex.file_path)

    def _saved_texture_identity(self, clip: _ClipState) -> str:
        return self._texture_identity(clip.saved_texture_guid, clip.saved_texture_path)

    def _can_direct_save_clip(self, clip: _ClipState) -> bool:
        saved_identity = self._saved_texture_identity(clip)
        current_identity = self._current_texture_identity()
        return bool(saved_identity and current_identity and saved_identity == current_identity)

    def _do_save_clip(self, clip: _ClipState, save_path: str) -> bool:
        """Write the .animclip2d file to *save_path*."""
        from Infernux.core.animation_clip import AnimationClip
        tex = self._tex

        ac = AnimationClip(
            name=os.path.splitext(os.path.basename(save_path))[0],
            authoring_texture_guid=tex.guid if tex else "",
            authoring_texture_path=tex.file_path if tex else "",
            frame_indices=list(clip.frame_indices),
            fps=clip.fps,
            loop=clip.loop,
        )
        ac.file_path = save_path
        ok = ac.save()
        if ok:
            clip.saved_path = save_path
            clip.saved_texture_guid = tex.guid if tex else ""
            clip.saved_texture_path = tex.file_path if tex else ""
            Debug.log(f"[AnimClipEditor] Saved: {save_path}")
            try:
                from Infernux.core.assets import AssetManager
                AssetManager.reimport_asset(save_path)
            except Exception:
                pass
            self._mark_saved_snapshot()
            return True
        else:
            Debug.log_warning(f"[AnimClipEditor] Failed to save: {save_path}")
            return False

    def _show_save_as_dialog(self, clip: _ClipState) -> None:
        """Open the editor-owned Save As modal for one animation clip."""
        safe_name = clip.name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        if not self._save_as_dialog.request(
            title="Save 2D Animation Clip",
            extension="animclip2d",
            default_name=safe_name,
            current_path=clip.saved_path,
        ):
            Debug.log_warning("[AnimClipEditor] No project root set - cannot save animation clip.")
            return
        self._pending_save_as_clip = clip

    def _save_pending_clip(self, save_path: str) -> bool:
        clip = self._pending_save_as_clip
        if clip is None:
            Debug.log_warning("[AnimClipEditor] Save As completed without a pending animation clip.")
            return False
        saved = self._do_save_clip(clip, save_path)
        if saved:
            self._pending_save_as_clip = None
        return saved

    def _cancel_pending_save_as(self) -> None:
        self._pending_save_as_clip = None

    def _discard_unsaved_changes(self) -> bool:
        self._cancel_pending_save_as()
        self.load_state(copy.deepcopy(self._last_saved_state))
        self._recompute_dirty()
        self._sync_project_dirty_flag()
        return not self._dirty

    # ------------------------------------------------------------------
    # Playback helpers
    # ------------------------------------------------------------------

    def _stop_playback(self):
        self._playback = _PLAYBACK_STOPPED
        self._preview_frame_idx = 0
        self._last_frame_time = 0.0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _active_clip(self) -> Optional[_ClipState]:
        if 0 <= self._active_clip_idx < len(self._clips):
            return self._clips[self._active_clip_idx]
        return None
