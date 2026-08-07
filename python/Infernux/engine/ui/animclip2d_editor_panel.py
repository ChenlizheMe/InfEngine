"""
2D Animation Clip Editor — visual editor for creating and editing .animclip2d files.

Drag a sprite-sheet texture onto the panel to load it, then click frames to
build one animation clip document with live preview and direct save support.

Opened from Window menu → 2D Animation Clip Editor.
"""

from __future__ import annotations

import os
import copy
import json
import math
import threading
import time
import uuid
import zlib
from Infernux.engine.path_utils import path_key, resolved_path, same_path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    BoundPanelCommand,
    CommandSource,
    KeyChord,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    PanelShortcutSpec,
    PanelViewStateField,
    PanelViewStateSchema,
    SelectionDomain,
    SelectionService,
    SelectionSnapshot,
    SelectionTarget,
)
from Infernux.lib import InxGUIContext
from Infernux.core.animation_clip import AnimationFrame

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
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "NewClip"
    frames: List[AnimationFrame] = field(default_factory=list)
    fps: float = 12.0
    loop: bool = True


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
_TOOLBAR_CARD_H = 34.0
_INFO_CARD_H = 42.0
_DETAILS_CARD_H = 220.0
_PREVIEW_CARD_H = 310.0
_SEQ_VISIBLE_ROWS = 2
_PALETTE_H = 180.0
_WIDE_LAYOUT_MIN_W = 920.0

_FRAME_IMAGE_PAD = 5.0
_FRAME_LABEL_H = 18.0
_FRAME_CELL_EXTRA_W = 12.0
_CONTENT_PAD_X = 16.0
_CONTENT_PAD_Y = 12.0
_FRAME_SELECTED_FILL = (0.36, 0.20, 0.20, 0.74)
_FRAME_HOVER_FILL = (0.27, 0.22, 0.22, 0.72)
_FRAME_IDLE_FILL = (0.18, 0.18, 0.18, 0.78)
_FRAME_IDLE_BORDER = (0.32, 0.32, 0.32, 1.0)

_PLAYBACK_STOPPED = 0
_PLAYBACK_PLAYING = 1
_U64 = 0xFFFFFFFFFFFFFFFF


def _sprite_frame_by_id(frames, frame_id: str):
    return next(
        (frame for frame in frames if frame.stable_id == frame_id),
        None,
    )


def _clip_state_from_document(document: dict) -> _ClipState:
    expected = {"stable_id", "name", "frames", "fps", "loop"}
    if type(document) is not dict or set(document) != expected:
        raise ValueError("2D animation clip state must use the complete current field set")
    stable_id = document["stable_id"]
    if (
        type(stable_id) is not str
        or len(stable_id) != 32
        or any(ch not in "0123456789abcdef" for ch in stable_id)
    ):
        raise TypeError(
            "2D animation clip stable_id must be a 32-character lowercase UUID hex string"
        )
    if type(document["name"]) is not str:
        raise TypeError("2D animation clip name must be a string")
    if type(document["frames"]) is not list:
        raise TypeError("2D animation clip frames must be an array")
    fps = document["fps"]
    if (
        isinstance(fps, bool)
        or not isinstance(fps, (int, float))
        or not math.isfinite(fps)
        or fps <= 0.0
    ):
        raise ValueError("2D animation clip fps must be a positive finite number")
    if type(document["loop"]) is not bool:
        raise TypeError("2D animation clip loop must be a bool")
    frames = [AnimationFrame.from_dict(value) for value in document["frames"]]
    if len({frame.stable_id for frame in frames}) != len(frames):
        raise ValueError("2D animation clip frame stable_id values must be unique")
    return _ClipState(
        stable_id=stable_id,
        name=document["name"],
        frames=frames,
        fps=float(fps),
        loop=document["loop"],
    )


def _bind_animclip2d_panel(panel: object) -> PanelCommandAdapter:
    required = (
        "command_new_clip_document",
        "can_new_clip_document",
        "command_delete_selected_frame",
        "can_delete_selected_frame",
        "command_toggle_preview",
        "command_stop_preview",
        "command_previous_frame",
        "command_next_frame",
        "command_clear_sequence",
        "command_add_frame",
    )
    missing = tuple(name for name in required if not callable(getattr(panel, name, None)))
    if missing:
        raise TypeError(f"2D animation panel interaction contract is missing: {missing}")
    return PanelCommandAdapter(
        {
            "animclip2d.new": BoundPanelCommand(
                lambda _context: panel.command_new_clip_document(),
                lambda _context: panel.can_new_clip_document(),
            ),
            "edit.delete": BoundPanelCommand(
                lambda _context: panel.command_delete_selected_frame(),
                lambda _context: panel.can_delete_selected_frame(),
            ),
            "edit.deselect": BoundPanelCommand(
                lambda _context: SelectionService.instance().clear(
                    reason="animclip2d_deselect",
                    record_history=True,
                ),
                lambda context: bool(context.selection.targets),
            ),
            "animclip2d.play_pause": BoundPanelCommand(
                lambda _context: panel.command_toggle_preview(),
                lambda _context: panel.can_preview(),
            ),
            "animclip2d.stop": BoundPanelCommand(
                lambda _context: panel.command_stop_preview(),
                lambda _context: panel.can_preview(),
            ),
            "animclip2d.previous_frame": BoundPanelCommand(
                lambda _context: panel.command_previous_frame(),
                lambda _context: panel.can_preview(),
            ),
            "animclip2d.next_frame": BoundPanelCommand(
                lambda _context: panel.command_next_frame(),
                lambda _context: panel.can_preview(),
            ),
            "animclip2d.clear_sequence": BoundPanelCommand(
                lambda _context: panel.command_clear_sequence(),
                lambda _context: panel.can_preview(),
            ),
            "animclip2d.add_frame": BoundPanelCommand(
                lambda context: panel.command_add_frame(
                    str(context.payload.get("frame_id", ""))
                ),
                lambda context: panel.can_add_frame(
                    str(context.payload.get("frame_id", ""))
                ),
            ),
        }
    )


_ANIMCLIP2D_PANEL_INTERACTION = PanelInteractionDescriptor(
    document_backed=True,
    owned_selection_domains=frozenset(
        {SelectionDomain.TIMELINE_ELEMENT, SelectionDomain.ASSET_SUBRESOURCE}
    ),
    commands=(
        PanelCommandSpec("animclip2d.new"),
        PanelCommandSpec("edit.delete"),
        PanelCommandSpec("edit.deselect"),
        PanelCommandSpec("animclip2d.play_pause"),
        PanelCommandSpec("animclip2d.stop"),
        PanelCommandSpec("animclip2d.previous_frame"),
        PanelCommandSpec("animclip2d.next_frame"),
        PanelCommandSpec("animclip2d.clear_sequence"),
        PanelCommandSpec("animclip2d.add_frame"),
    ),
    shortcuts=(
        PanelShortcutSpec("animclip2d.play_pause", KeyChord.parse("Space")),
        PanelShortcutSpec("edit.delete", KeyChord.parse("Delete")),
        PanelShortcutSpec("edit.deselect", KeyChord.parse("Escape")),
    ),
    adapter_factory=_bind_animclip2d_panel,
)


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
    interaction=_ANIMCLIP2D_PANEL_INTERACTION,
)
class AnimClip2DEditorPanel(EditorPanel):
    """Visual editor for building .animclip2d files from sprite sheets."""

    window_id = "animclip2d_editor"
    VIEW_STATE_SCHEMA = PanelViewStateSchema(
        "animclip2d_editor.view",
        (
            PanelViewStateField(
                "preview_frame_index",
                "_preview_frame_idx",
                int,
            ),
        ),
    )

    def __init__(self):
        super().__init__(title="2D Animation Clip Editor", window_id="animclip2d_editor")
        from Infernux.engine.interaction import AuthoringDocumentController

        self._authoring_document_controller = AuthoringDocumentController(self)
        self._tex: Optional[_TextureState] = None
        self._clips: List[_ClipState] = [_ClipState()]
        self._active_clip_idx: int = 0
        # Preview playback
        self._playback: int = _PLAYBACK_STOPPED
        self._preview_frame_idx: int = 0  # index into the active clip's frames
        self._last_frame_time: float = 0.0
        self._save_as_dialog = AssetSaveAsDialog(
            "animclip2d.save_as",
            "2D animation clip",
            owner_id=self._window_id,
        )
        self._pending_save_as_clip: Optional[_ClipState] = None
        self._pending_save_ticket_id: str = ""
        # A newly opened authoring window owns an untitled in-memory document.
        self._replace_animclip_document(resource_path="", dirty=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _initial_size(self):
        return (1080, 760)

    def on_enable(self):
        from Infernux.engine.interaction import AssetMutationService

        service = AssetMutationService.instance()
        self._asset_mutation_service = service
        if service is not None:
            service.add_listener(self._on_asset_changed)
        selection = SelectionService.instance()
        self._selection_service = selection
        selection.add_listener(self._on_global_selection_changed)
        self._project_sequence_selection(selection.snapshot)

    def on_disable(self):
        service = getattr(self, "_asset_mutation_service", None)
        if service is not None:
            service.remove_listener(self._on_asset_changed)
        self._asset_mutation_service = None
        selection = getattr(self, "_selection_service", None)
        if selection is not None:
            selection.remove_listener(self._on_global_selection_changed)
        self._selection_service = None
        self._cleanup_texture()

    def _project_sequence_selection(self, snapshot: SelectionSnapshot) -> bool:
        primary = snapshot.primary
        clip = self._active_clip
        if (
            primary is None
            or clip is None
            or primary.domain is not SelectionDomain.TIMELINE_ELEMENT
            or primary.document_id != self.document_id
            or primary.sub_kind != "animclip2d_frame"
        ):
            return False
        index = next(
            (
                index
                for index, frame in enumerate(clip.frames)
                if frame.stable_id == primary.target_id
            ),
            -1,
        )
        if index < 0:
            return False
        self._preview_frame_idx = index
        self._playback = _PLAYBACK_STOPPED
        return True

    def _on_global_selection_changed(self, change) -> None:
        self._project_sequence_selection(change.after)

    def _select_sequence_index(self, index: int, *, reason: str) -> bool:
        clip = self._active_clip
        if clip is None or not clip.frames:
            return False
        target_index = max(0, min(int(index), len(clip.frames) - 1))
        target = self._sequence_target(clip.frames[target_index])
        return SelectionService.instance().select(
            target,
            owner_id=self.window_id,
            reason=reason,
            record_history=True,
        )

    def _on_asset_changed(self, change) -> None:
        """Publish texture import changes into the open editor immediately."""
        from Infernux.engine.interaction import AssetMutationKind, iter_asset_mutations

        tex = self._tex
        if tex is None:
            return
        mutation = next(
            (
                item
                for item in iter_asset_mutations(change)
                if same_path(item.source_path, tex.file_path)
                or same_path(item.path, tex.file_path)
            ),
            None,
        )
        if mutation is None:
            return
        if mutation.kind is AssetMutationKind.DELETED:
            self._cleanup_texture()
            self._tex = None
            return
        if mutation.kind is AssetMutationKind.MOVED:
            tex.file_path = mutation.destination_path

        filter_tag, srgb_tag, _, _ = self._read_texture_sampling(tex.file_path)
        frames = self._read_sprite_frames(tex.file_path)
        source_w, source_h = self._read_source_dimensions(tex.file_path, frames)
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

    def _capture_authoring_snapshot(self) -> dict:
        return {
            "texture_path": self._tex.file_path if self._tex else "",
            "clips": [
                {
                    "stable_id": clip.stable_id,
                    "name": clip.name,
                    "frames": [frame.to_dict() for frame in clip.frames],
                    "fps": float(clip.fps),
                    "loop": bool(clip.loop),
                }
                for clip in self._clips
            ],
        }

    def capture_authoring_snapshot(self) -> dict:
        """Return the document state consumed by Interaction Core mutations."""
        return self._capture_authoring_snapshot()

    def restore_authoring_snapshot(self, state: dict) -> None:
        if type(state) is not dict or set(state) != {"texture_path", "clips"}:
            raise ValueError("2D animation authoring state must use the complete current field set")
        if type(state["texture_path"]) is not str:
            raise TypeError("2D animation texture_path must be a string")
        if type(state["clips"]) is not list:
            raise TypeError("2D animation clips must be an array")
        items = state["clips"]
        if len(items) != 1:
            raise ValueError("2D animation authoring state must contain exactly one clip")
        clip = _clip_state_from_document(items[0])
        texture_path = state["texture_path"]
        current_path = self._tex.file_path if self._tex else ""
        if texture_path and not same_path(texture_path, current_path):
            self._load_texture(texture_path)
        elif not texture_path and self._tex is not None:
            self._cleanup_texture()
            self._tex = None
        self._clips = [clip]
        self._active_clip_idx = 0
        self._stop_playback()

    def capture_document_restore_state(self, document_id: str) -> dict:
        if document_id != self.document_id:
            raise ValueError("2D animation restore capture targeted another document")
        document = self._animclip_document()
        return {
            "authoring": self._capture_authoring_snapshot(),
            "file_path": document.resource_path if document is not None else "",
        }

    def restore_document_restore_state(self, state: dict) -> None:
        if not isinstance(state, dict) or set(state) != {"authoring", "file_path"}:
            raise ValueError("2D animation document restore state is invalid")

        def validate_authoring(authoring) -> None:
            if not isinstance(authoring, dict) or set(authoring) != {
                "texture_path",
                "clips",
            }:
                raise ValueError("2D animation authoring restore state is invalid")
            clips = authoring["clips"]
            if not isinstance(clips, list):
                raise TypeError("2D animation authoring clips must be an array")
            required_clip_fields = {
                "stable_id",
                "name",
                "frames",
                "fps",
                "loop",
            }
            if any(
                not isinstance(item, dict) or set(item) != required_clip_fields
                for item in clips
            ):
                raise ValueError("2D animation authoring clip field set is invalid")

        validate_authoring(state["authoring"])
        self.restore_authoring_snapshot(copy.deepcopy(state["authoring"]))

    def recover_incompatible_document_restore_state(
        self,
        state,
        error: Exception,
    ) -> bool:
        del error
        path = str(state.get("file_path", "")) if isinstance(state, dict) else ""
        if path and os.path.isfile(path):
            return self.open_document_resource_immediate(path) is True
        return self._new_clip_document_immediate()

    def _apply_authoring_mutation(
        self,
        description: str,
        mutation,
        *,
        merge_key: str = "",
        before_selection=None,
        after_selection=None,
    ) -> bool:
        from Infernux.engine.interaction import AuthoringMutationService

        if not self.document_id:
            return False
        self.publish_interaction_ownership(reason="animclip2d_authoring")
        changed = AuthoringMutationService.require().apply(
            self.document_id,
            description,
            mutation,
            view_id=self.window_id,
            merge_key=merge_key,
            before_selection=before_selection,
            after_selection=after_selection,
        )
        return changed

    def _sequence_target(self, frame: AnimationFrame) -> SelectionTarget:
        return SelectionTarget.timeline_element(
            self.document_id,
            frame.stable_id,
            sub_kind="animclip2d_frame",
        )

    def _selected_sequence_frame(self) -> Optional[AnimationFrame]:
        primary = SelectionService.instance().snapshot.primary
        if (
            primary is None
            or primary.domain is not SelectionDomain.TIMELINE_ELEMENT
            or primary.document_id != self.document_id
            or primary.sub_kind != "animclip2d_frame"
        ):
            return None
        clip = self._active_clip
        if clip is None:
            return None
        return next(
            (frame for frame in clip.frames if frame.stable_id == primary.target_id),
            None,
        )

    def can_delete_selected_frame(self) -> bool:
        return self._selected_sequence_frame() is not None

    def command_delete_selected_frame(self) -> bool:
        clip = self._active_clip
        selected = self._selected_sequence_frame()
        if clip is None or selected is None:
            return False
        before_selection = SelectionService.instance().snapshot
        index = clip.frames.index(selected)
        remaining = [frame for frame in clip.frames if frame is not selected]
        next_target = None
        if remaining:
            next_target = self._sequence_target(remaining[min(index, len(remaining) - 1)])
        after_selection = SelectionSnapshot.create(
            (next_target,) if next_target is not None else (),
            owner_id=self.window_id if next_target is not None else "",
            primary=next_target,
            anchor=next_target,
        )
        changed = self._apply_authoring_mutation(
            "Remove 2D animation frame",
            lambda: clip.frames.remove(selected),
            before_selection=before_selection,
            after_selection=after_selection,
        )
        if changed:
            self._preview_frame_idx = min(
                self._preview_frame_idx,
                max(0, len(clip.frames) - 1),
            )
            self._playback = _PLAYBACK_STOPPED
        return changed

    def _execute_animclip_command(self, command_id: str, *, payload=None) -> bool:
        return self.execute_owned_command(
            command_id,
            source=CommandSource.TOOLBAR,
            payload=payload,
        )

    def can_preview(self) -> bool:
        clip = self._active_clip
        return bool(clip is not None and clip.frames)

    def command_toggle_preview(self) -> bool:
        if not self.can_preview():
            return False
        if self._playback == _PLAYBACK_PLAYING:
            self._playback = _PLAYBACK_STOPPED
        else:
            self._playback = _PLAYBACK_PLAYING
            self._last_frame_time = time.perf_counter()
            if self._preview_frame_idx >= len(self._active_clip.frames):
                self._preview_frame_idx = 0
        return True

    def command_stop_preview(self) -> bool:
        if not self.can_preview():
            return False
        changed = bool(
            self._playback != _PLAYBACK_STOPPED
            or self._preview_frame_idx != 0
        )
        self._stop_playback()
        return changed

    def command_previous_frame(self) -> bool:
        if not self.can_preview():
            return False
        self._select_sequence_index(
            self._preview_frame_idx - 1,
            reason="animclip2d_preview_previous",
        )
        return True

    def command_next_frame(self) -> bool:
        if not self.can_preview():
            return False
        self._select_sequence_index(
            self._preview_frame_idx + 1,
            reason="animclip2d_preview_next",
        )
        return True

    def command_clear_sequence(self) -> bool:
        clip = self._active_clip
        if clip is None or not clip.frames:
            return False
        owns_selection = self._selected_sequence_frame() is not None
        changed = self._apply_authoring_mutation(
            "Clear 2D animation frames",
            clip.frames.clear,
            before_selection=(
                SelectionService.instance().snapshot if owns_selection else None
            ),
            after_selection=(SelectionSnapshot() if owns_selection else None),
        )
        if changed:
            self._stop_playback()
        return changed

    def can_add_frame(self, frame_id: str) -> bool:
        return bool(
            self._active_clip is not None
            and self._tex is not None
            and any(
                frame.stable_id == str(frame_id or "")
                for frame in self._tex.frames
            )
        )

    def command_add_frame(self, frame_id: str) -> bool:
        clip = self._active_clip
        if clip is None or not self.can_add_frame(frame_id):
            return False
        entry = AnimationFrame(sprite_frame_id=str(frame_id))
        before_selection = SelectionService.instance().snapshot
        target = self._sequence_target(entry)
        after_selection = SelectionSnapshot.create(
            (target,),
            owner_id=self.window_id,
            primary=target,
            anchor=target,
        )
        return self._apply_authoring_mutation(
            "Add 2D animation frame",
            lambda: clip.frames.append(entry),
            before_selection=before_selection,
            after_selection=after_selection,
        )

    def _animclip_document(self):
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().get(self.document_id)

    @staticmethod
    def _animclip_document_key(path: str):
        from Infernux.engine.interaction import DocumentKey, DocumentKind

        normalized = resolved_path(path)
        try:
            from Infernux.core.asset_types import read_meta_guid

            guid = read_meta_guid(normalized)
        except Exception:
            guid = ""
        if guid:
            return DocumentKey.asset(DocumentKind.ANIMATION_CLIP, guid)
        return DocumentKey.resource(DocumentKind.ANIMATION_CLIP, normalized)

    def _replace_animclip_document(self, *, resource_path: str, dirty: bool) -> None:
        from Infernux.engine.interaction import (
            DocumentCapability,
            DocumentKey,
            DocumentKind,
            DocumentRegistry,
        )

        path = resolved_path(resource_path) if resource_path else ""
        title = os.path.splitext(os.path.basename(path))[0] if path else "2D Animation Clip"
        key = (
            self._animclip_document_key(path)
            if path
            else DocumentKey.session(DocumentKind.ANIMATION_CLIP)
        )
        registry = DocumentRegistry.instance()
        document, created = registry.open_or_create(
            key,
            title,
            resource_path=path,
            revision=1 if dirty else 0,
            saved_revision=0,
            capabilities=(
                DocumentCapability.SAVE
                | DocumentCapability.SAVE_AS
                | DocumentCapability.DISCARD
            ),
            controller=self._authoring_document_controller,
        )
        del created
        self._bind_replaced_document(document.document_id, dirty=dirty)

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del document_id, source_path, destination_path, guid

    def save_state(self) -> dict:
        return super().save_state()

    def load_state(self, data: dict) -> None:
        super().load_state(data)

    # ------------------------------------------------------------------
    # Render — layout: header -> tabs -> preview/details -> sequence -> palette
    # ------------------------------------------------------------------

    # ImGuiKey / ImGuiMod constants
    def on_render_content(self, ctx: InxGUIContext):
        try:
            if self._tex is not None:
                self._validate_texture()
            ctx.push_style_var_vec2(
                ImGuiStyleVar.WindowPadding, _CONTENT_PAD_X, _CONTENT_PAD_Y,
            )
            try:
                visible = ctx.begin_child("##animclip_content", 0, 0, False)
                try:
                    if visible:
                        ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + _CONTENT_PAD_X)
                        ctx.set_cursor_pos_y(ctx.get_cursor_pos_y() + _CONTENT_PAD_Y)
                        clip = self._active_clip
                        if clip is not None:
                            self._render_main_workspace(
                                ctx,
                                clip,
                                max(
                                    120.0,
                                    ctx.get_content_region_avail_width()
                                    - _CONTENT_PAD_X,
                                ),
                            )
                finally:
                    # The outer content region is manually padded as well.
                    # Submit a final item so ImGui does not infer its boundary
                    # solely from SetCursorPos calls in an empty/short layout.
                    ctx.dummy(0.0, 0.0)
                    ctx.end_child()
                IGUI.drop_target(
                    ctx, "ANIMCLIP_FILE", self._on_animclip_drop, outline=True,
                )
            finally:
                ctx.pop_style_var(1)
        except Exception as exc:
            Debug.log_warning(f"[AnimClipEditor] Render error: {exc}")

    # ------------------------------------------------------------------
    # Header / empty state helpers
    # ------------------------------------------------------------------

    def _on_texture_clear(self):
        def clear_texture() -> None:
            self._cleanup_texture()
            self._tex = None

        self._apply_authoring_mutation(
            "Clear 2D animation texture",
            clear_texture,
        )

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

        # A .animclip2d editor owns exactly one document.  Document commands
        # belong in one compact toolbar; clip collections are authored by other
        # assets rather than hidden inside this panel session.
        ctx.begin_child("##animclip_toolbar", avail_w, _TOOLBAR_CARD_H, True)
        try:
            self._pad_child_cursor(ctx, 10.0, 4.0)
            self._render_document_toolbar(ctx, clip)
        finally:
            ctx.dummy(0.0, 0.0)
            ctx.end_child()
        ctx.dummy(0, 8)
        ctx.begin_child("##animclip_info", avail_w, _INFO_CARD_H, True)
        try:
            self._pad_child_cursor(ctx, 10.0, 6.0)
            self._render_clip_info(ctx, clip, max(avail_w - 20.0, 120.0))
        finally:
            ctx.dummy(0.0, 0.0)
            ctx.end_child()

        ctx.dummy(0, 8)

        if wide:
            preview_w = min(max(avail_w * 0.42, 320.0), 520.0)
            seq_w = max(avail_w - preview_w - 12.0, 340.0)

            ctx.begin_child("##animclip_preview", preview_w, _PREVIEW_CARD_H, True)
            try:
                self._pad_child_cursor(ctx)
                self._render_preview(ctx, clip, max(preview_w - 24.0, 120.0))
            finally:
                ctx.dummy(0.0, 0.0)
                ctx.end_child()

            ctx.same_line(0, 12)

            ctx.begin_child("##animclip_sequence", seq_w, _PREVIEW_CARD_H, True)
            try:
                self._pad_child_cursor(ctx)
                self._render_sequence_content(ctx, clip)
            finally:
                # The padded child uses explicit cursor positioning. Commit
                # its final layout position before closing the parent card.
                ctx.dummy(0.0, 0.0)
                ctx.end_child()
        else:
            ctx.begin_child("##animclip_preview", avail_w, _DETAILS_CARD_H, True)
            try:
                self._pad_child_cursor(ctx)
                self._render_preview(ctx, clip, max(avail_w - 24.0, 120.0))
            finally:
                ctx.dummy(0.0, 0.0)
                ctx.end_child()

            ctx.dummy(0, 8)

            ctx.begin_child("##animclip_sequence", avail_w, 190.0, True)
            try:
                self._pad_child_cursor(ctx)
                self._render_sequence_content(ctx, clip)
            finally:
                ctx.dummy(0.0, 0.0)
                ctx.end_child()

        ctx.dummy(0, 8)
        self._render_frame_palette(ctx, avail_w)

    @staticmethod
    def _pad_child_cursor(
        ctx: InxGUIContext,
        horizontal: float = 12.0,
        vertical: float = 8.0,
    ) -> None:
        ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + horizontal)
        ctx.set_cursor_pos_y(ctx.get_cursor_pos_y() + vertical)

    @staticmethod
    def _calc_grid_cols(width: float, thumb_size: float) -> int:
        cell_w = thumb_size + 16.0
        return max(1, int(max(width, thumb_size) / max(cell_w, 1.0)))

    # ------------------------------------------------------------------
    # Document toolbar
    # ------------------------------------------------------------------

    def _execute_document_command(self, command_id: str) -> bool:
        from Infernux.engine.interaction import CommandSource

        return self.execute_owned_command(command_id, source=CommandSource.TOOLBAR)

    def can_new_clip_document(self) -> bool:
        from Infernux.engine.interaction import (
            DocumentRegistry,
            EditorInteractionCore,
        )

        document = DocumentRegistry.instance().get(self.document_id)
        if (
            document is None
            or self.window_id not in document.view_ids
            or self._save_as_dialog.is_open
            or bool(self._pending_save_ticket_id)
        ):
            return False
        core = EditorInteractionCore.instance()
        return core is None or not core.close_coordinator.is_active

    def command_new_clip_document(self) -> bool:
        if not self.can_new_clip_document():
            return False
        return self.request_document_replacement(
            self._new_clip_document_immediate,
        )

    def _render_document_toolbar(self, ctx: InxGUIContext, clip: _ClipState) -> None:
        new_label = t("animtimeline_editor.new")
        if ctx.button(new_label + "##animclip_new"):
            self._execute_document_command("animclip2d.new")
        ctx.record_semantic_item(
            "button",
            new_label,
            self.can_new_clip_document(),
            "animclip2d.toolbar.new",
        )

        ctx.same_line(0, 4)
        save_label = t("animclip_editor.save_clip")
        can_save = self._tex is not None and bool(clip.frames)
        if not can_save:
            ctx.begin_disabled(True)
        if ctx.button(save_label + "##animclip_save"):
            self._execute_document_command("file.save")
        ctx.record_semantic_item("button", save_label, can_save, "animclip2d.toolbar.save")
        ctx.same_line(0, 4)
        save_as_label = t("animclip_editor.save_as")
        if ctx.button(save_as_label + "##animclip_save_as"):
            self._execute_document_command("file.save_as")
        ctx.record_semantic_item(
            "button", save_as_label, can_save, "animclip2d.toolbar.save_as"
        )
        if not can_save:
            ctx.end_disabled()

        document = self._animclip_document()
        resource_path = document.resource_path if document is not None else ""
        if resource_path:
            ctx.same_line(0, 12)
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(os.path.basename(resource_path))
            ctx.pop_style_color(1)

    def _new_clip_document_immediate(self) -> bool:
        self._cancel_pending_save_as()
        if self.document_id:
            self.unbind_document()
        self._clips = [_ClipState()]
        self._active_clip_idx = 0
        self._stop_playback()
        self._replace_animclip_document(resource_path="", dirty=True)
        return True

    def current_child_context_id(self) -> str:
        return ""

    def restore_child_context(self, context_id: str) -> bool:
        return not str(context_id or "")

    # ------------------------------------------------------------------
    # Clip info — name, fps, save (compact top bar)
    # ------------------------------------------------------------------

    def _render_clip_info(self, ctx: InxGUIContext, clip: _ClipState, avail_w: float):
        # Authored clip properties. Document actions live in the toolbar above.
        name_label = t("animclip_editor.clip_name")

        ctx.label(name_label)
        ctx.same_line(0, 4)
        ctx.set_next_item_width(min(140, avail_w * 0.2))
        new_name = ctx.text_input("##clip_name", clip.name, 256)
        ctx.record_semantic_item(
            "text_input", f"{name_label}: {new_name}", True, "animclip2d.clip.name",
        )
        if new_name != clip.name:
            self._apply_authoring_mutation(
                "Rename 2D animation clip",
                lambda: setattr(clip, "name", new_name),
                merge_key=f"clip:{clip.stable_id}:name",
            )

        ctx.same_line(0, 14)
        fps_label = t("animclip_editor.clip_fps")
        ctx.label(fps_label)
        ctx.same_line(0, 4)
        ctx.set_next_item_width(72.0)
        new_fps = ctx.drag_float("##clip_fps", clip.fps, 0.1, 0.1, 120.0)
        ctx.record_semantic_item(
            "drag_float", f"{fps_label}: {new_fps:g}", True,
            "animclip2d.clip.fps", numeric_value=new_fps,
        )
        normalized_fps = max(0.1, float(new_fps))
        if normalized_fps != clip.fps:
            self._apply_authoring_mutation(
                "Set 2D animation frame rate",
                lambda: setattr(clip, "fps", normalized_fps),
                merge_key=f"clip:{clip.stable_id}:fps",
            )

        ctx.same_line(0, 14)
        loop_label = t("animclip_editor.clip_loop")
        new_loop = bool(ctx.checkbox(f"{loop_label}##clip_loop", clip.loop))
        if new_loop != clip.loop:
            self._apply_authoring_mutation(
                "Set 2D animation loop",
                lambda: setattr(clip, "loop", new_loop),
            )
        ctx.record_semantic_item(
            "checkbox", loop_label, True, "animclip2d.clip.loop", clip.loop,
        )

        fc = len(clip.frames)
        duration = fc / max(clip.fps, 0.1)
        ctx.same_line(0, 16)
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label(f"{fc} frames   {duration:.2f}s")
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
        preview_source_id = ""
        if clip.frames:
            safe_idx = max(0, min(self._preview_frame_idx, len(clip.frames) - 1))
            preview_source_id = clip.frames[safe_idx].sprite_frame_id
            preview_frame = _sprite_frame_by_id(tex.frames, preview_source_id)

        if preview_frame is not None:
            ctx.same_line(0, 8)
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(f"{preview_frame.name}  {preview_frame.w}x{preview_frame.h}")
            ctx.pop_style_color(1)

        ctx.separator()

        # Transport controls row
        fc = len(clip.frames)
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
                self._execute_animclip_command("animclip2d.play_pause")
        else:
            play_label = t("animclip_editor.play")
            play_clicked = ctx.button(play_label + "##transport")
            ctx.record_semantic_item(
                "button", play_label, bool(fc), "animclip2d.transport.play_pause",
            )
            if play_clicked:
                self._execute_animclip_command("animclip2d.play_pause")

        ctx.same_line(0, 4)
        stop_label = t("animclip_editor.stop")
        stop_clicked = ctx.button(stop_label + "##transport")
        ctx.record_semantic_item(
            "button", stop_label, bool(fc), "animclip2d.transport.stop",
        )
        if stop_clicked:
            self._execute_animclip_command("animclip2d.stop")

        ctx.same_line(0, 8)
        step_back_clicked = ctx.button("|<##step_back")
        ctx.record_semantic_item(
            "button", "Previous Frame", bool(fc), "animclip2d.transport.previous",
        )
        if step_back_clicked:
            self._execute_animclip_command("animclip2d.previous_frame")
        ctx.same_line(0, 2)
        step_forward_clicked = ctx.button(">|##step_fwd")
        ctx.record_semantic_item(
            "button", "Next Frame", bool(fc), "animclip2d.transport.next",
        )
        if step_forward_clicked:
            self._execute_animclip_command("animclip2d.next_frame")

        if fc:
            ctx.same_line(0, 12)
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(f"{self._preview_frame_idx + 1}/{fc}")
            ctx.pop_style_color(1)

        if not fc:
            ctx.end_disabled()

        # Preview area
        ctx.begin_child("##preview_area", 0, 0, False)
        try:
            child_w = ctx.get_content_region_avail_width()
            child_h = ctx.get_content_region_avail_height()

            if not clip.frames:
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
                frame = _sprite_frame_by_id(
                    tex.frames,
                    clip.frames[self._preview_frame_idx].sprite_frame_id,
                )

                if frame is not None:
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
            # Centering uses SetCursorPos on both axes. The image can be
            # unavailable for a frame, so always submit the resulting cursor
            # position instead of relying on the conditional image item.
            ctx.dummy(0.0, 0.0)
            ctx.end_child()

    # ------------------------------------------------------------------
    # Sequence strip (ordered frame thumbnails)
    # ------------------------------------------------------------------

    @staticmethod
    def _render_frame_cell(
        ctx: InxGUIContext,
        *,
        item_id: str,
        texture_id: int,
        thumb: float,
        image_w: float,
        image_h: float,
        uv: Tuple[float, float, float, float],
        label: str,
        selected: bool,
        semantic_label: str,
        semantic_id: str,
    ) -> Tuple[bool, bool]:
        """Render one square frame target with a separate, inert label row."""
        ctx.invisible_button(item_id, thumb, thumb)
        clicked = ctx.is_item_clicked(0)
        hovered = ctx.is_item_hovered()
        x0 = ctx.get_item_rect_min_x()
        y0 = ctx.get_item_rect_min_y()
        x1 = ctx.get_item_rect_max_x()
        y1 = ctx.get_item_rect_max_y()
        ctx.record_semantic_item(
            "button", semantic_label, True, semantic_id,
            bool_value=selected,
        )

        fill = (
            _FRAME_SELECTED_FILL
            if selected
            else (_FRAME_HOVER_FILL if hovered else _FRAME_IDLE_FILL)
        )
        border = Theme.APPLY_BUTTON if selected else (
            Theme.BTN_HOVERED if hovered else _FRAME_IDLE_BORDER
        )
        ctx.draw_filled_rect(x0, y0, x1, y1, *fill, 3.0)
        ctx.draw_rect(x0, y0, x1, y1, *border, 2.0 if selected else 1.0, 3.0)

        image_side = max(1.0, thumb - _FRAME_IMAGE_PAD * 2.0)
        scale = min(image_side / max(image_w, 1.0), image_side / max(image_h, 1.0))
        draw_w = max(1.0, image_w * scale)
        draw_h = max(1.0, image_h * scale)
        image_x0 = x0 + (thumb - draw_w) * 0.5
        image_y0 = y0 + (thumb - draw_h) * 0.5
        uv0_x, uv0_y, uv1_x, uv1_y = uv
        ctx.draw_image_rect(
            texture_id,
            image_x0,
            image_y0,
            image_x0 + draw_w,
            image_y0 + draw_h,
            uv0_x,
            uv0_y,
            uv1_x,
            uv1_y,
            1.0,
            1.0,
            1.0,
            1.0,
            0.0,
            False,
            False,
            1.0,
        )
        label_y0 = y1 + 3.0
        label_y1 = label_y0 + _FRAME_LABEL_H
        ctx.draw_text_aligned(
            x0 + 3.0,
            label_y0,
            x1 - 3.0,
            label_y1,
            label,
            *Theme.META_TEXT,
            0.5,
            0.5,
            0.0,
            True,
        )
        ctx.dummy(thumb, _FRAME_LABEL_H + 5.0)
        return clicked, hovered

    def _render_sequence_content(self, ctx: InxGUIContext, clip: _ClipState):
        """Render frame sequence content (called inside a wrapping begin_child)."""
        tex = self._tex
        if tex is None or tex.texture_id == 0:
            return

        ctx.label(t("animclip_editor.sequence"))
        if clip.frames:
            clear_label = t("animclip_editor.clear_sequence")
            clear_w = ctx.calc_text_width(clear_label) + 20.0
            ctx.same_line(max(ctx.get_window_width() - clear_w - 18.0, 180.0))
            clear_clicked = ctx.button(clear_label + "##seq_clear")
            ctx.record_semantic_item(
                "button", clear_label, True, "animclip2d.sequence.clear",
            )
            if clear_clicked:
                self._execute_animclip_command("animclip2d.clear_sequence")

        ctx.separator()
        ctx.begin_child("##seq_strip", 0, 0, False)
        try:
            if not clip.frames:
                ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
                ctx.label(t("animclip_editor.sequence_empty_hint"))
                ctx.pop_style_color(1)
            else:
                thumb = _SEQ_THUMB_SIZE
                child_w = ctx.get_content_region_avail_width()
                cols = self._calc_grid_cols(child_w, thumb)
                selection = SelectionService.instance()
                sequence_targets = tuple(
                    self._sequence_target(entry) for entry in clip.frames
                )
                selection.set_ordered_targets(self.window_id, sequence_targets)
                selected_target = (
                    selection.snapshot.primary
                    if selection.snapshot.domain is SelectionDomain.TIMELINE_ELEMENT
                    else None
                )

                if ctx.begin_table("##seq_grid", cols, 0, 0.0):
                    for seq_i, entry in enumerate(clip.frames):
                        ctx.table_next_column()
                        frame_id = entry.sprite_frame_id
                        target = sequence_targets[seq_i]
                        frame = _sprite_frame_by_id(tex.frames, frame_id)

                        if frame is not None:
                            uv0_x, uv0_y, uv1_x, uv1_y = _sprite_frame_imgui_uv(frame, tex.tex_w, tex.tex_h)

                            clicked, hovered = self._render_frame_cell(
                                ctx,
                                item_id=f"##seq_{seq_i}",
                                texture_id=tex.texture_id,
                                thumb=thumb,
                                image_w=float(frame.w),
                                image_h=float(frame.h),
                                uv=(uv0_x, uv0_y, uv1_x, uv1_y),
                                label=str(seq_i + 1),
                                selected=target == selected_target,
                                semantic_label=f"Sequence {seq_i}: {frame.name}",
                                semantic_id=f"animclip2d.sequence.frame.{entry.stable_id}",
                            )

                            if clicked:
                                self._playback = _PLAYBACK_STOPPED
                                selection.select(
                                    target,
                                    owner_id=self.window_id,
                                    reason="animclip2d_sequence_select",
                                    record_history=True,
                                )

                            if hovered and ctx.is_mouse_button_clicked(1):
                                selection.select(
                                    target,
                                    owner_id=self.window_id,
                                    reason="animclip2d_sequence_context_select",
                                    record_history=True,
                                )
                        else:
                            missing_clicked = ctx.button(
                                f"?##seq_{seq_i}", width=thumb, height=thumb
                            )
                            ctx.record_semantic_item(
                                "button", f"Sequence {seq_i}: Missing Frame {frame_id}", True,
                                f"animclip2d.sequence.frame.{entry.stable_id}",
                            )
                            if missing_clicked:
                                selection.select(
                                    target,
                                    owner_id=self.window_id,
                                    reason="animclip2d_sequence_select",
                                    record_history=True,
                                )

                    ctx.end_table()
        finally:
            # Table navigation may leave the child cursor beyond its last
            # submitted item. Commit that final position before ending the
            # child so ImGui can account for the sequence strip boundaries.
            ctx.dummy(0.0, 0.0)
            ctx.end_child()

    # ------------------------------------------------------------------
    # Frame palette — grid of sprite slices at the bottom
    # ------------------------------------------------------------------

    def _render_frame_palette(self, ctx: InxGUIContext, avail_w: float):
        tex = self._tex
        palette_h = max(ctx.get_content_region_avail_height(), _PALETTE_H)
        ctx.begin_child("##animclip_palette_card", avail_w, palette_h, True)
        try:
            ctx.label(t("animclip_editor.frame_palette"))
            field_width = min(360.0, max(220.0, avail_w * 0.38))
            ctx.same_line(max(160.0, avail_w - field_width - 12.0))
            self._render_palette_texture_field(ctx)
            ctx.separator()

            if tex is None or tex.texture_id == 0:
                ctx.dummy(0, 12.0)
                ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
                ctx.label(t("animclip_editor.drop_texture_hint"))
                ctx.pop_style_color(1)
                return

            ctx.begin_child("##frame_palette", 0, 0, False)
            try:
                self._pad_child_cursor(ctx, 8.0, 8.0)
                active_clip = self._active_clip
                thumb = _PALETTE_THUMB_SIZE
                child_w = ctx.get_content_region_avail_width()
                cols = self._calc_grid_cols(child_w, thumb)

                if ctx.begin_table("##palette_grid", cols, 0, 0.0):
                    for i, frame in enumerate(tex.frames):
                        ctx.table_next_column()
                        uv0_x, uv0_y, uv1_x, uv1_y = _sprite_frame_imgui_uv(frame, tex.tex_w, tex.tex_h)

                        clicked, hovered = self._render_frame_cell(
                            ctx,
                            item_id=f"##palette_{i}",
                            texture_id=tex.texture_id,
                            thumb=thumb,
                            image_w=float(frame.w),
                            image_h=float(frame.h),
                            uv=(uv0_x, uv0_y, uv1_x, uv1_y),
                            label=str(i + 1),
                            selected=False,
                            semantic_label=f"Frame {i}: {frame.name}",
                            semantic_id=f"animclip2d.palette.frame.{frame.stable_id}",
                        )

                        if clicked and active_clip is not None:
                            self._execute_animclip_command(
                                "animclip2d.add_frame",
                                payload={"frame_id": frame.stable_id},
                            )

                        if hovered:
                            ctx.set_tooltip(f"#{i}  {frame.name}  ({frame.w}x{frame.h})")

                    ctx.end_table()
            finally:
                ctx.dummy(0.0, 0.0)
                ctx.end_child()

        except Exception as exc:
            Debug.log_warning(f"[AnimClipEditor] Frame palette error: {exc}")
        finally:
            ctx.dummy(0.0, 0.0)
            ctx.end_child()

    def _render_palette_texture_field(self, ctx: InxGUIContext) -> None:
        tex = self._tex
        display = (
            os.path.basename(tex.file_path)
            if tex is not None
            else t("animclip_editor.drop_texture_hint")
        )
        IGUI.asset_reference_field(
            ctx,
            "animclip_tex_slot",
            display,
            "Texture",
            asset_type="Texture",
            accept="TEXTURE_FILE",
            on_assign=self._on_texture_drop,
            on_clear=self._on_texture_clear if tex is not None else None,
            ping_path=tex.file_path if tex is not None else None,
            has_value=tex is not None,
            reference_value=(
                {
                    "asset_type": "Texture",
                    "guid": tex.guid,
                    "path_hint": tex.file_path,
                }
                if tex is not None
                else None
            ),
            semantic_id="animclip2d.texture",
        )

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
        from Infernux.core.asset_reference_types import resolve_asset_reference_path

        try:
            path = resolve_asset_reference_path("Texture", payload)
        except (KeyError, TypeError, ValueError) as exc:
            Debug.log_error(f"[AnimClipEditor] Texture assignment rejected: {exc}")
            return
        self._apply_authoring_mutation(
            "Set 2D animation texture",
            lambda: self._load_texture(path),
        )

    def _on_animclip_drop(self, payload):
        """Handle ANIMCLIP_FILE drop — open an existing .animclip2d."""
        if isinstance(payload, str) and payload:
            from Infernux.engine.interaction import DocumentKind

            self.request_document_resource_open(DocumentKind.ANIMATION_CLIP, payload)

    def _load_texture(self, file_path: str):
        """Load a sprite-sheet texture and extract frame data from its .meta."""
        if not file_path or not os.path.isfile(file_path):
            return False

        native = self._get_native_engine()
        if not native:
            Debug.log_warning("[AnimClipEditor] No native engine")
            return False

        filter_tag, srgb_tag, use_nearest, use_srgb = self._read_texture_sampling(file_path)
        norm_path = resolved_path(file_path)
        resource_key = f"animclip_editor|{srgb_tag}_{filter_tag}|{norm_path}"
        stamp = self._build_texture_stamp(norm_path, filter_tag, srgb_tag)
        if stamp == 0:
            return False

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

        # Resolve GUID
        guid = ""
        try:
            from Infernux.engine.bootstrap import EditorBootstrap
            adb = EditorBootstrap.instance().engine.get_asset_database()
            if adb:
                guid = adb.get_guid_from_path(file_path) or ""
        except Exception:
            pass

        next_texture = _TextureState(
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
        self._cleanup_texture()
        self._tex = next_texture
        return True

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

        tex.frames = frames

        return True

    # ------------------------------------------------------------------
    # Open existing .animclip2d
    # ------------------------------------------------------------------

    def _load_animclip_model(self, animclip_path: str) -> bool:
        """Replace only the live authoring model from one disk asset."""
        from Infernux.core.animation_clip import AnimationClip

        clip_data = AnimationClip.load(animclip_path)
        if clip_data is None:
            Debug.log_warning(f"[AnimClipEditor] Failed to load: {animclip_path}")
            return False

        # Resolve texture from clip's GUID or path
        if self._tex is not None:
            self._cleanup_texture()
            self._tex = None
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
            frames=copy.deepcopy(clip_data.frames),
            fps=clip_data.fps,
            loop=clip_data.loop,
        )
        self._clips = [cs]
        self._active_clip_idx = 0
        self._stop_playback()
        return True

    def open_document_resource_immediate(self, animclip_path: str):
        """Load an existing .animclip2d file into the editor."""
        if not self._load_animclip_model(animclip_path):
            return False
        if self.document_id:
            self.unbind_document()
        self._replace_animclip_document(
            resource_path=resolved_path(animclip_path),
            dirty=False,
        )
        return True

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_clip(self, clip: _ClipState):
        """Save the active clip as a .animclip2d file."""
        if clip is not self._active_clip:
            return False
        return self._request_document_save(save_as=False)

    def _request_document_save(self, *, save_as: bool) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        document = self._animclip_document()
        if document is None:
            return False
        return DocumentRegistry.instance().request_save(
            document.document_id,
            save_as=save_as,
        ).accepted

    def request_authoring_save_as(self, ticket):
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
        )

        clip = self._active_clip
        if clip is None or self._tex is None or not clip.frames:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "2D animation clip requires a texture and at least one frame",
            )
        self._pending_save_ticket_id = ticket.ticket_id
        if self._show_save_as_dialog(clip):
            return DocumentActionResult(DocumentActionStatus.PENDING)
        self._pending_save_ticket_id = ""
        return DocumentActionResult(
            DocumentActionStatus.REJECTED,
            "no project root is available",
        )

    def discard(self, *, document_id: str) -> bool:
        if document_id != self.document_id:
            return False
        return self._discard_unsaved_changes()

    def _do_save_clip(
        self,
        clip: _ClipState,
        save_path: str,
        *,
        ticket_id: str = "",
    ) -> bool:
        """Continue an existing SaveTicket with a user-selected path."""
        if clip is not self._active_clip:
            return False
        active_ticket_id = ticket_id or self._pending_save_ticket_id
        if not active_ticket_id:
            raise RuntimeError("2D animation clip saves require a DocumentRegistry SaveTicket")
        result = self._authoring_document_controller.continue_save_to_resource(
            active_ticket_id,
            save_path,
        )
        if result.accepted:
            self._pending_save_ticket_id = ""
        return result.accepted

    def _capture_animation_clip(self, *, name: str = ""):
        from Infernux.core.animation_clip import AnimationClip

        clip = self._active_clip
        tex = self._tex
        if clip is None or tex is None or not clip.frames:
            raise ValueError(
                "2D animation clip requires a texture and at least one frame"
            )
        return AnimationClip(
            name=name or clip.name,
            authoring_texture_guid=tex.guid if tex else "",
            authoring_texture_path=tex.file_path if tex else "",
            frames=copy.deepcopy(clip.frames),
            fps=clip.fps,
            loop=clip.loop,
        )

    def capture_authoring_save_snapshot(self, save_path: str):
        from Infernux.engine.interaction import (
            AuthoringAssetSnapshot,
            document_content_token,
        )

        normalized = resolved_path(save_path)
        if not normalized:
            raise ValueError("2D animation clip save target is invalid")
        title = os.path.splitext(os.path.basename(normalized))[0]
        asset = self._capture_animation_clip(name=title)
        asset.file_path = normalized
        asset.validate_sprite_frame_references(
            project_root=asset._project_root_for_asset(normalized),
        )
        document = asset.serialize_document()
        return AuthoringAssetSnapshot(
            normalized,
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            document_content_token(document),
            title,
            document,
        )

    def publish_authoring_save_snapshot(self, snapshot) -> str:
        clip = self._active_clip
        if clip is None:
            return "2D animation clip disappeared before publication"
        clip.name = snapshot.title
        Debug.log(f"[AnimClipEditor] Saved: {snapshot.target_path}")
        try:
            from Infernux.core.assets import AssetManager

            result = AssetManager.reimport_asset(snapshot.target_path)
            if not result:
                result = AssetManager.import_asset(snapshot.target_path)
            if not result:
                return str(
                    getattr(result, "error", "")
                    or f"2D animation clip publication failed: {snapshot.target_path}"
                )
        except Exception as exc:
            return str(exc)
        return ""

    def current_authoring_content_token(self) -> str:
        from Infernux.engine.interaction import document_content_token

        try:
            return document_content_token(
                self._capture_animation_clip().serialize_document()
            )
        except ValueError:
            return ""

    def _show_save_as_dialog(self, clip: _ClipState) -> bool:
        """Open the editor-owned Save As modal for one animation clip."""
        safe_name = clip.name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        if not self._save_as_dialog.request(
            title="Save 2D Animation Clip",
            extension="animclip2d",
            default_name=safe_name,
            current_path=(
                self._animclip_document().resource_path
                if self._animclip_document() is not None
                else ""
            ),
            save_callback=self._save_pending_clip,
            cancel_callback=self._cancel_pending_save_as,
        ):
            Debug.log_warning("[AnimClipEditor] No project root set - cannot save animation clip.")
            return False
        self._pending_save_as_clip = clip
        return True

    def _save_pending_clip(self, save_path: str) -> bool:
        clip = self._pending_save_as_clip
        if clip is None:
            Debug.log_warning("[AnimClipEditor] Save As completed without a pending animation clip.")
            return False
        saved = self._do_save_clip(
            clip,
            save_path,
            ticket_id=self._pending_save_ticket_id,
        )
        if saved:
            self._pending_save_as_clip = None
        return saved

    def _cancel_pending_save_as(self) -> None:
        self._pending_save_as_clip = None
        ticket_id = self._pending_save_ticket_id
        self._pending_save_ticket_id = ""
        if ticket_id:
            from Infernux.engine.interaction import DocumentRegistry

            DocumentRegistry.instance().complete_save(
                ticket_id,
                success=False,
                cancelled=True,
                message="save was cancelled",
            )

    def _discard_unsaved_changes(self) -> bool:
        self._cancel_pending_save_as()
        document = self._animclip_document()
        if document is not None and document.resource_path:
            if not self._load_animclip_model(document.resource_path):
                return False
        else:
            if self._tex is not None:
                self._cleanup_texture()
                self._tex = None
            self._clips = [_ClipState()]
            self._active_clip_idx = 0
            self._stop_playback()
        # DocumentRegistry.request_discard() commits the revision rollback.
        return True

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
