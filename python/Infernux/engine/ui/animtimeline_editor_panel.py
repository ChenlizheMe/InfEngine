"""Timeline Editor — visual editor for ``.animtimeline`` transform timelines.

A minimal Unity-Timeline-style editor for a single transform track: a horizontal
timeline bar (like a video scrubber) shows keyframes as diamonds and a draggable
playhead.  Add a keyframe at the playhead, select a keyframe to edit its
transform and the transition curve used to reach it (from the previous keyframe).

Timeline commands, including Space play/pause, are routed by the editor-wide
command and shortcut services.

Opened from Window menu → Timeline Editor, or by double-clicking a
``.animtimeline`` asset in the Project panel.
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    BoundPanelCommand,
    KeyChord,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    PanelShortcutSpec,
    PanelViewStateField,
    PanelViewStateSchema,
    SelectionDomain,
    SelectionService,
    ViewCommandService,
)
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
from .theme import ImGuiCol, ImGuiMouseCursor, Theme


# Combo label i18n keys (order matches INTERP_MODES / APPLY_MODES).
_INTERP_LABEL_KEYS = ("interp_constant", "interp_linear", "interp_ease_in", "interp_ease_out", "interp_ease_inout")
_APPLY_LABEL_KEYS = ("apply_additive", "apply_absolute")
_BAR_H = 54.0
_BAR_EDGE_PAD = 14.0  # inset track so t=0 / t=dur keyframes stay inside the bar
_TRACK_HEADER_W = 104.0
_RULER_H = 27.0
_PREVIEW_H = 196.0  # 3D preview height reserved at the bottom of the right (keyframe) panel
_KF_LABEL_W = 78.0  # fixed label column width so inline label+field rows align
_DRAG_THRESHOLD = 4.0  # px the mouse must move before a keyframe starts dragging (Blender-like)
_PREVIEW_RENDER_PX = 240  # fixed offscreen render size (stable → no per-frame framebuffer churn)

def _bind_timeline_panel(panel: object) -> PanelCommandAdapter:
    required = (
        "command_new_timeline",
        "command_toggle_playback",
        "command_stop_playback",
        "command_set_loop_preview",
        "command_add_keyframe",
        "command_delete_selected_keyframe",
        "can_delete_selected_keyframe",
    )
    missing = tuple(name for name in required if not callable(getattr(panel, name, None)))
    if missing:
        raise TypeError(f"timeline panel interaction contract is missing: {missing}")
    return PanelCommandAdapter(
        {
            "timeline.new": BoundPanelCommand(
                lambda _context: panel.command_new_timeline(),
                lambda _context: True,
            ),
            "timeline.play_pause": BoundPanelCommand(
                lambda _context: panel.command_toggle_playback(),
                lambda _context: True,
            ),
            "timeline.stop": BoundPanelCommand(
                lambda _context: panel.command_stop_playback(),
                lambda _context: True,
            ),
            "timeline.set_loop_preview": BoundPanelCommand(
                lambda context: panel.command_set_loop_preview(
                    bool(context.payload.get("value", False))
                ),
                lambda _context: True,
            ),
            "timeline.add_keyframe": BoundPanelCommand(
                lambda _context: panel.command_add_keyframe(),
                lambda _context: True,
            ),
            "edit.delete": BoundPanelCommand(
                lambda _context: panel.command_delete_selected_keyframe(),
                lambda _context: panel.can_delete_selected_keyframe(),
            ),
            "edit.deselect": BoundPanelCommand(
                lambda _context: SelectionService.instance().clear(
                    reason="timeline_deselect",
                    record_history=True,
                ),
                lambda context: bool(context.selection.targets),
            ),
        }
    )


_TIMELINE_PANEL_INTERACTION = PanelInteractionDescriptor(
    document_backed=True,
    owned_selection_domains=frozenset({SelectionDomain.TIMELINE_ELEMENT}),
    commands=(
        PanelCommandSpec("timeline.new"),
        PanelCommandSpec("timeline.play_pause"),
        PanelCommandSpec("timeline.stop"),
        PanelCommandSpec("timeline.set_loop_preview"),
        PanelCommandSpec("timeline.add_keyframe"),
        PanelCommandSpec("edit.delete"),
        PanelCommandSpec("edit.deselect"),
    ),
    shortcuts=(
        PanelShortcutSpec("timeline.play_pause", KeyChord.parse("Space")),
        PanelShortcutSpec("edit.delete", KeyChord.parse("Delete")),
        PanelShortcutSpec("edit.deselect", KeyChord.parse("Escape")),
    ),
    adapter_factory=_bind_timeline_panel,
)


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
    interaction=_TIMELINE_PANEL_INTERACTION,
)
class AnimTimelineEditorPanel(EditorPanel):
    """Visual single-track transform timeline editor."""

    window_id = "animtimeline_editor"
    VIEW_STATE_SCHEMA = PanelViewStateSchema(
        "animtimeline_editor.view",
        (
            PanelViewStateField("playhead", "_playhead", float),
            PanelViewStateField("loop_preview", "_loop_preview", bool),
            PanelViewStateField("cam_yaw", "_cam_yaw", float),
            PanelViewStateField("cam_pitch", "_cam_pitch", float),
            PanelViewStateField("cam_dist", "_cam_dist", float),
        ),
    )

    def __init__(self):
        super().__init__(title="Timeline Editor", window_id="animtimeline_editor")
        from Infernux.engine.interaction import AuthoringDocumentController

        self._authoring_document_controller = AuthoringDocumentController(self)
        self._timeline: AnimationTimeline = AnimationTimeline(name="Timeline")
        self._file_path: str = ""
        self._playhead: float = 0.0
        self._playing: bool = False
        self._loop_preview: bool = True
        self._last_tick: float = 0.0
        self._play_wall_start: float = 0.0
        self._playhead_at_play_start: float = 0.0
        self._drag_key_id: str = ""
        self._drag_armed: bool = False
        self._press_x: float = 0.0
        self._bar_was_active: bool = False
        self._playhead_scrub_before: Optional[float] = None
        self._idle_suppressed: bool = False
        self._idle_prev: bool = True
        # Orbit camera for the 3D preview viewport (yaw/pitch radians, distance = zoom).
        self._cam_yaw: float = -0.6
        self._cam_pitch: float = 0.5
        self._cam_dist: float = 6.0
        self._orbiting: bool = False
        self._preview_orbit_before: Optional[tuple[float, float, float]] = None
        self._preview_zoom_before: Optional[tuple[float, float, float]] = None
        self._preview_tex_cache: int = 0
        self._preview_request_signature = None
        self._save_as_dialog = AssetSaveAsDialog(
            "animtimeline.save_as",
            "timeline",
            owner_id=self._window_id,
        )
        self._pending_save_ticket_id: str = ""
        # An untouched window owns a clean blank baseline. User-authored New
        # timelines continue to be unsaved drafts.
        self._replace_timeline_document(resource_path="", dirty=False)

    def on_enable(self) -> None:
        pass

    def on_disable(self) -> None:
        self._commit_live_property_edits()
        self._commit_playhead_scrub()
        self._commit_preview_view_gesture("orbit")
        self._commit_preview_view_gesture("zoom")
        # Always restore the editor idle setting if we suppressed it.
        self._set_engine_active(False)

    def _apply_playhead_view(self, value: float) -> None:
        self._playhead = max(
            0.0,
            min(float(self._timeline.duration), float(value)),
        )

    def _commit_playhead_scrub(self) -> bool:
        before = self._playhead_scrub_before
        self._playhead_scrub_before = None
        if before is None:
            return False
        return ViewCommandService.require().set_value(
            float(before),
            float(self._playhead),
            self._apply_playhead_view,
            description="Scrub Timeline Playhead",
        )

    def _capture_preview_view(self) -> tuple[float, float, float]:
        return (float(self._cam_yaw), float(self._cam_pitch), float(self._cam_dist))

    def _apply_preview_view(self, state: tuple[float, float, float]) -> None:
        self._cam_yaw = float(state[0])
        self._cam_pitch = float(state[1])
        self._cam_dist = float(state[2])

    def _apply_loop_preview_view(self, value: bool) -> None:
        self._loop_preview = bool(value)

    def command_set_loop_preview(self, value: bool) -> bool:
        return ViewCommandService.require().set_value(
            bool(self._loop_preview),
            bool(value),
            self._apply_loop_preview_view,
            description="Toggle Timeline Loop Preview",
            owner_view_id=self.window_id,
        )

    def _commit_preview_view_gesture(self, kind: str) -> bool:
        field_name = {
            "orbit": "_preview_orbit_before",
            "zoom": "_preview_zoom_before",
        }[kind]
        before = getattr(self, field_name)
        setattr(self, field_name, None)
        if before is None:
            return False
        return ViewCommandService.require().set_value(
            before,
            self._capture_preview_view(),
            self._apply_preview_view,
            description=(
                "Orbit Timeline Preview"
                if kind == "orbit"
                else "Zoom Timeline Preview"
            ),
        )

    def _timeline_document(self):
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().get(self.document_id)

    def capture_document_restore_state(self, document_id: str) -> dict:
        if document_id != self.document_id:
            raise ValueError("timeline restore capture targeted another document")
        return {
            "timeline": self._timeline.to_dict(),
            "file_path": self._file_path,
        }

    def restore_document_restore_state(self, state: dict) -> None:
        if not isinstance(state, dict) or set(state) != {"timeline", "file_path"}:
            raise ValueError("Timeline document restore state is invalid")
        self._timeline = AnimationTimeline.from_dict(copy.deepcopy(state["timeline"]))
        self._file_path = self._normalize_timeline_path(state.get("file_path", ""))
        self._timeline.file_path = self._file_path
        self._playing = False
        self._clear_key_selection(record_history=False)
        self._drag_key_id = ""

    def recover_incompatible_document_restore_state(
        self,
        state,
        error: Exception,
    ) -> bool:
        del error
        path = str(state.get("file_path", "")) if isinstance(state, dict) else ""
        if path and os.path.isfile(path):
            return self.open_document_resource_immediate(path)
        return self._new_timeline_immediate()

    def _replace_timeline_document(self, *, resource_path: str, dirty: bool) -> None:
        from Infernux.engine.interaction import (
            DocumentCapability,
            DocumentKey,
            DocumentKind,
            DocumentRegistry,
        )

        path = self._normalize_timeline_path(resource_path) if resource_path else ""
        title = os.path.splitext(os.path.basename(path))[0] if path else "Timeline"
        registry = DocumentRegistry.instance()
        key = self._timeline_document_key(path) if path else DocumentKey.session(
            DocumentKind.TIMELINE
        )
        document, _created = registry.open_or_create(
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
        self._bind_replaced_document(document.document_id, dirty=dirty)

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del guid
        if document_id != self.document_id:
            return
        if self._file_path and same_path(self._file_path, source_path):
            self._file_path = self._normalize_timeline_path(destination_path)
            self._timeline.file_path = self._file_path
            self._timeline.name = os.path.splitext(os.path.basename(self._file_path))[0]
            self._persist_panel_state()

    @staticmethod
    def _timeline_document_key(path: str):
        from Infernux.engine.interaction import DocumentKey, DocumentKind

        normalized = resolved_path(path)
        try:
            from Infernux.core.asset_types import read_meta_guid

            guid = read_meta_guid(normalized)
        except Exception:
            guid = ""
        if guid:
            return DocumentKey.asset(DocumentKind.TIMELINE, guid)
        return DocumentKey.resource(DocumentKind.TIMELINE, normalized)

    # ── Lifecycle ──────────────────────────────────────────────────────
    def _initial_size(self):
        return (940, 560)

    def _open_timeline(self, path: str) -> bool:
        """Request a user-visible Timeline document replacement."""
        normalized = self._normalize_timeline_path(path)
        if not normalized:
            return False
        if self._file_path and same_path(self._file_path, normalized):
            return True
        from .dirty_panel_confirmation import DirtyPanelConfirmationCoordinator

        return DirtyPanelConfirmationCoordinator.instance().request_document_replace(
            self.document_id,
            on_complete=lambda: self.open_document_resource_immediate(normalized),
        )

    def open_document_resource_immediate(self, path: str) -> bool:
        """Replace the live Timeline after a close transaction has resolved."""
        self._commit_live_property_edits()
        tl = AnimationTimeline.load(path)
        if tl is None:
            Debug.log_warning(f"[TimelineEditor] Failed to load: {path}")
            return False
        if self.document_id:
            self.unbind_document()
        self._timeline = tl
        self._file_path = path
        self._playhead = 0.0
        self._playing = False
        self._clear_key_selection(record_history=False)
        self._drag_key_id = ""
        self._replace_timeline_document(resource_path=path, dirty=False)
        self._persist_panel_state()
        return True

    def _new_timeline(self) -> bool:
        """Request replacement of the current document with a new Timeline."""
        return self.request_document_replacement(
            self._new_timeline_immediate,
        )

    def _new_timeline_immediate(self) -> bool:
        self._commit_live_property_edits()
        if self.document_id:
            self.unbind_document()
        self._timeline = AnimationTimeline(name="Timeline")
        self._file_path = ""
        self._playhead = 0.0
        self._playing = False
        self._clear_key_selection(record_history=False)
        self._drag_key_id = ""
        self._replace_timeline_document(resource_path="", dirty=True)
        self._persist_panel_state()
        return True

    def _timeline_edit_target(self, target_id: str):
        if not target_id:
            return self._timeline
        target = self._timeline.find_keyframe(target_id)
        if target is None:
            raise RuntimeError(f"timeline keyframe no longer exists: {target_id}")
        return target

    def _set_live_property(
        self,
        edit_id: str,
        target_id: str,
        field_name: str,
        old_value,
        new_value,
        description: str,
    ) -> bool:
        if new_value == old_value:
            return False
        from Infernux.engine.interaction import (
            AuthoringMutationService,
            ContinuousEditService,
        )

        mutations = AuthoringMutationService.require()
        if not mutations.can_record():
            return False

        edits = ContinuousEditService.instance()
        session_key = self._continuous_edit_key(edit_id)
        session = edits.get(session_key)
        if session is None:
            edits.commit_owner(self.window_id)
            from Infernux.engine.interaction import DocumentRegistry

            document = self._timeline_document()
            if document is None:
                return False
            before_revision = document.revision
            after_revision = DocumentRegistry.instance().mark_changed(
                document.document_id,
                view_id=self.window_id,
            )
            session = edits.begin(
                session_key,
                owner_id=self.window_id,
                document_id=self.document_id,
                description=description,
                initial_value=old_value,
                metadata={
                    "target_id": str(target_id or ""),
                    "field_name": str(field_name),
                    "before_revision": before_revision,
                    "after_revision": after_revision,
                },
                on_commit=self._commit_timeline_property_edit,
                on_cancel=self._cancel_timeline_property_edit,
            )
        metadata = session.metadata
        if (
            metadata["target_id"] != target_id
            or metadata["field_name"] != field_name
        ):
            self._finish_live_property_edit(edit_id)
            return self._set_live_property(
                edit_id,
                target_id,
                field_name,
                old_value,
                new_value,
                description,
            )
        setattr(
            self._timeline_edit_target(metadata["target_id"]),
            metadata["field_name"],
            copy.deepcopy(new_value),
        )
        edits.update(session_key, new_value)
        return True

    def _finish_live_property_edit(self, edit_id: str) -> bool:
        from Infernux.engine.interaction import ContinuousEditService

        return ContinuousEditService.instance().commit(
            self._continuous_edit_key(edit_id)
        )

    def _continuous_edit_key(self, edit_id: str) -> str:
        return f"{self.window_id}:{self.document_id}:{edit_id}"

    def _commit_timeline_property_edit(self, session) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        metadata = session.metadata
        target_id = metadata["target_id"]
        field_name = metadata["field_name"]
        target = self._timeline_edit_target(target_id)
        current = copy.deepcopy(getattr(target, field_name))
        registry = DocumentRegistry.instance()
        if current == session.initial_value:
            registry.restore_content_revision(
                self.document_id,
                metadata["before_revision"],
            )
            return True
        from Infernux.engine.interaction import AuthoringMutationService
        from Infernux.engine.undo import TimelinePropertyCommand

        command = TimelinePropertyCommand(
            self._timeline,
            self.document_id,
            target_id,
            field_name,
            session.initial_value,
            current,
            metadata["before_revision"],
            metadata["after_revision"],
            session.description,
        )
        recorded = AuthoringMutationService.require().record_applied_command(
            self.document_id,
            command,
            view_id=self.window_id,
            before_revision=metadata["before_revision"],
            after_revision=metadata["after_revision"],
            rollback=lambda: setattr(
                self._timeline_edit_target(target_id),
                field_name,
                copy.deepcopy(session.initial_value),
            ),
        )
        return recorded

    def _cancel_timeline_property_edit(self, session) -> None:
        from Infernux.engine.interaction import DocumentRegistry

        metadata = session.metadata
        setattr(
            self._timeline_edit_target(metadata["target_id"]),
            metadata["field_name"],
            copy.deepcopy(session.initial_value),
        )
        DocumentRegistry.instance().restore_content_revision(
            self.document_id,
            metadata["before_revision"],
        )

    def _finish_widget_property_edit(self, ctx, edit_id: str) -> None:
        deactivated = getattr(ctx, "is_item_deactivated_after_edit", None)
        if callable(deactivated) and deactivated():
            self._finish_live_property_edit(edit_id)

    def _commit_live_property_edits(self) -> None:
        from Infernux.engine.interaction import ContinuousEditService

        ContinuousEditService.instance().commit_owner(self.window_id)

    def _apply_discrete_property(
        self,
        target_id: str,
        field_name: str,
        new_value,
        description: str,
    ) -> bool:
        self._commit_live_property_edits()
        target = self._timeline_edit_target(target_id)
        old_value = copy.deepcopy(getattr(target, field_name))
        if new_value == old_value:
            return False
        from Infernux.engine.interaction import AuthoringMutationService
        from Infernux.engine.undo import TimelinePropertyCommand

        document = self._timeline_document()
        if document is None:
            return False
        applied = AuthoringMutationService.require().execute_command(
            document.document_id,
            lambda before_revision, after_revision: TimelinePropertyCommand(
                self._timeline,
                document.document_id,
                target_id,
                field_name,
                old_value,
                new_value,
                before_revision,
                after_revision,
                description,
            ),
            view_id=self.window_id,
        )
        return applied

    def timeline_authoring_model(self) -> AnimationTimeline:
        return self._timeline

    def on_timeline_authoring_applied(self) -> None:
        self._playhead = max(0.0, min(float(self._timeline.duration), self._playhead))

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
        return super().save_state()

    def load_state(self, data: dict) -> None:
        super().load_state(data)
        self._playhead = max(
            0.0,
            min(float(self._timeline.duration), float(self._playhead)),
        )

    # ── Save ───────────────────────────────────────────────────────────
    def _do_save(self):
        return self._request_document_save(save_as=False)

    def _request_document_save(self, *, save_as: bool) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        self._commit_live_property_edits()
        document = self._timeline_document()
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

        self._pending_save_ticket_id = ticket.ticket_id
        if self._show_save_as_dialog():
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

    def _save_to(self, path: str, *, ticket_id: str = "") -> bool:
        active_ticket_id = ticket_id or self._pending_save_ticket_id
        if not active_ticket_id:
            raise RuntimeError("Timeline saves require a DocumentRegistry SaveTicket")
        result = self._authoring_document_controller.continue_save_to_resource(
            active_ticket_id,
            path,
        )
        if result.accepted:
            self._pending_save_ticket_id = ""
        return result.accepted

    def capture_authoring_save_snapshot(self, path: str):
        from Infernux.engine.interaction import (
            AuthoringAssetSnapshot,
            document_content_token,
        )

        normalized = self._normalize_timeline_path(path)
        if not normalized:
            raise ValueError("Timeline save target is invalid")
        title = os.path.splitext(os.path.basename(normalized))[0]
        document = copy.deepcopy(self._timeline.to_dict())
        document["name"] = title
        AnimationTimeline.from_dict(document)
        return AuthoringAssetSnapshot(
            normalized,
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            document_content_token(document),
            title,
            document,
        )

    def publish_authoring_save_snapshot(self, snapshot) -> str:
        self._timeline.name = snapshot.title
        self._timeline.file_path = snapshot.target_path
        self._file_path = snapshot.target_path
        self._persist_panel_state()
        Debug.log(f"[TimelineEditor] Saved: {snapshot.target_path}")
        try:
            from Infernux.core.assets import AssetManager

            result = AssetManager.reimport_asset(snapshot.target_path)
            if not result:
                result = AssetManager.import_asset(snapshot.target_path)
            if not result:
                return str(
                    getattr(result, "error", "")
                    or f"timeline publication failed: {snapshot.target_path}"
                )
        except Exception as exc:
            return str(exc)
        return ""

    def current_authoring_content_token(self) -> str:
        from Infernux.engine.interaction import document_content_token

        return document_content_token(self._timeline.to_dict())

    def _show_save_as_dialog(self) -> bool:
        safe = (self._timeline.name or "Timeline").replace(" ", "_")
        if not self._save_as_dialog.request(
            title="Save Timeline",
            extension="animtimeline",
            default_name=safe,
            current_path=self._file_path,
            save_callback=self._save_to,
            cancel_callback=self._cancel_pending_save,
        ):
            Debug.log_warning("[TimelineEditor] No project root set - cannot save timeline.")
            return False
        return True

    def _cancel_pending_save(self) -> None:
        ticket_id = self._pending_save_ticket_id
        self._pending_save_ticket_id = ""
        if not ticket_id:
            return
        from Infernux.engine.interaction import DocumentRegistry

        DocumentRegistry.instance().complete_save(
            ticket_id,
            success=False,
            cancelled=True,
            message="save was cancelled",
        )

    def _discard_unsaved_changes(self) -> bool:
        from Infernux.engine.interaction import ContinuousEditService

        ContinuousEditService.instance().cancel_owner(self.window_id)
        if self._file_path:
            timeline = AnimationTimeline.load(self._file_path)
            if timeline is None:
                return False
            self._timeline = timeline
            self._playhead = 0.0
            self._playing = False
            self._clear_key_selection(record_history=False)
            self._drag_key_id = ""
        else:
            self._timeline = AnimationTimeline(name="Timeline")
            self._playhead = 0.0
            self._playing = False
            self._clear_key_selection(record_history=False)
            self._drag_key_id = ""
        # DocumentRegistry.request_discard() is the sole revision authority.
        # This method only restores the Timeline model.
        return True

    # ── Selection helpers ──────────────────────────────────────────────
    @property
    def _selected_key_id(self) -> str:
        """Project the authoritative global selection onto this document."""
        from Infernux.engine.interaction import SelectionDomain, SelectionService

        primary = SelectionService.instance().snapshot.primary
        if (
            primary is not None
            and primary.domain is SelectionDomain.TIMELINE_ELEMENT
            and primary.document_id == self.document_id
            and primary.sub_kind == "keyframe"
        ):
            return primary.target_id
        return ""

    def _select_key(self, key: TimelineKeyframe, *, record_history: bool = True) -> None:
        from Infernux.engine.interaction import SelectionService, SelectionTarget

        SelectionService.instance().select(
            SelectionTarget.timeline_element(
                self.document_id,
                key.stable_id,
                sub_kind="keyframe",
            ),
            owner_id=self.window_id,
            reason="timeline_keyframe",
            record_history=record_history,
        )

    def _clear_key_selection(self, *, record_history: bool = True) -> None:
        from Infernux.engine.interaction import SelectionDomain, SelectionService

        selection = SelectionService.instance()
        snapshot = selection.snapshot
        primary = snapshot.primary
        if (
            primary is not None
            and snapshot.domain is SelectionDomain.TIMELINE_ELEMENT
            and primary.document_id == self.document_id
        ):
            selection.clear(
                reason="timeline_clear_keyframe",
                record_history=record_history,
            )

    def _current_sel_key(self) -> Optional[TimelineKeyframe]:
        """Resolve the selected keyframe through its persistent element identity."""
        if not self._selected_key_id:
            return None
        key = self._timeline.find_keyframe(self._selected_key_id)
        if key is None:
            self._clear_key_selection(record_history=False)
        return key

    def _add_keyframe_at_playhead(self):
        self._commit_live_property_edits()
        sampled = self._timeline.sample(self._playhead)
        if sampled is not None:
            pos, rot, scl = sampled
        else:
            pos, rot, scl = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
        key = TimelineKeyframe(
            time=float(self._playhead), position=pos, rotation=rot, scale=scl,
        )
        from Infernux.engine.interaction import (
            AuthoringMutationService,
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )
        from Infernux.engine.undo import TimelineInsertKeyframeCommand

        document = self._timeline_document()
        if document is None:
            return False
        mutations = AuthoringMutationService.require()
        before_selection = SelectionService.instance().snapshot
        after_selection = SelectionSnapshot.create(
            (
                SelectionTarget.timeline_element(
                    document.document_id,
                    key.stable_id,
                    sub_kind="keyframe",
                ),
            ),
            owner_id=self.window_id,
        )
        applied = mutations.execute_command(
            document.document_id,
            lambda before_revision, after_revision: TimelineInsertKeyframeCommand(
                self._timeline,
                document.document_id,
                key,
                len(self._timeline.keyframes),
                before_revision,
                after_revision,
            ),
            view_id=self.window_id,
            before_selection=before_selection,
            after_selection=after_selection,
        )
        if applied:
            inserted = self._timeline.find_keyframe(key.stable_id)
            if inserted is not None:
                self._select_key(inserted, record_history=False)
        return bool(applied)

    def _delete_selected_key(self):
        self._commit_live_property_edits()
        k = self._current_sel_key()
        if k is None:
            return False
        from Infernux.engine.interaction import (
            AuthoringMutationService,
            SelectionService,
            SelectionSnapshot,
        )
        from Infernux.engine.undo import TimelineRemoveKeyframeCommand

        document = self._timeline_document()
        if document is None:
            return False
        applied = AuthoringMutationService.require().execute_command(
            document.document_id,
            lambda before_revision, after_revision: TimelineRemoveKeyframeCommand(
                self._timeline,
                document.document_id,
                k,
                self._timeline.keyframes.index(k),
                before_revision,
                after_revision,
            ),
            view_id=self.window_id,
            before_selection=SelectionService.instance().snapshot,
            after_selection=SelectionSnapshot(),
        )
        if applied:
            self._clear_key_selection(record_history=False)
            self._drag_key_id = ""
        return bool(applied)

    # ── Playback ───────────────────────────────────────────────────────
    def _advance_playback(self):
        now = time.perf_counter()
        if self._playing:
            # Wall-clock playback: playhead advances at real time regardless of
            # frame rate or GPU preview stalls (dt accumulation was running slow).
            self._playhead = self._playhead_at_play_start + (now - self._play_wall_start)
            dur = max(1e-6, float(self._timeline.duration))
            if self._playhead >= dur:
                if self._loop_preview:
                    self._playhead %= dur
                    self._playhead_at_play_start = self._playhead
                    self._play_wall_start = now
                else:
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
            loop_label = t("animtimeline_editor.loop_preview")
            requested_loop = bool(
                ctx.checkbox(f"{loop_label}##timeline_loop_preview", self._loop_preview)
            )
            if requested_loop != self._loop_preview:
                self._execute_timeline_command(
                    "timeline.set_loop_preview",
                    payload={"value": requested_loop},
                )
            if _semantic_capture_enabled(ctx):
                ctx.record_semantic_item(
                    "checkbox",
                    loop_label,
                    True,
                    "animtimeline.preview.loop",
                    bool_value=self._loop_preview,
                )
            self._render_preview_viewport(ctx, ctx.get_content_region_avail_width(),
                                           max(120.0, ctx.get_content_region_avail_height()))
        ctx.end_child()

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

    def _toggle_play(self):
        # Replaying from the end restarts (the preview plays once, no auto-loop).
        if not self._playing and self._playhead >= float(self._timeline.duration) - 1e-4:
            self._playhead = 0.0
        if not self._playing:
            self._play_wall_start = time.perf_counter()
            self._playhead_at_play_start = float(self._playhead)
        self._playing = not self._playing
        self._last_tick = time.perf_counter()

    def _execute_timeline_command(self, command_id: str, *, payload=None) -> bool:
        from Infernux.engine.interaction import CommandSource

        return self.execute_owned_command(
            command_id,
            source=CommandSource.TOOLBAR,
            payload=payload,
        )

    def command_new_timeline(self) -> bool:
        return self._new_timeline()

    def command_toggle_playback(self) -> bool:
        self._toggle_play()
        return True

    def command_stop_playback(self) -> bool:
        changed = bool(self._playing or self._playhead != 0.0)
        self._playing = False
        self._playhead = 0.0
        return changed

    def command_add_keyframe(self) -> bool:
        return self._add_keyframe_at_playhead()

    def can_delete_selected_keyframe(self) -> bool:
        return self._current_sel_key() is not None

    def command_delete_selected_keyframe(self) -> bool:
        return self._delete_selected_key()

    def _render_toolbar(self, ctx: InxGUIContext):
        capture_semantics = _semantic_capture_enabled(ctx)
        new_label = t("animtimeline_editor.new")
        if ctx.button(new_label):
            self._execute_timeline_command("timeline.new")
        if capture_semantics:
            ctx.record_semantic_item("button", new_label, True, "animtimeline.toolbar.new")
        ctx.same_line()
        save_label = t("animtimeline_editor.save")
        if ctx.button(save_label):
            self._execute_timeline_command("file.save")
        if capture_semantics:
            ctx.record_semantic_item("button", save_label, True, "animtimeline.toolbar.save")
        ctx.same_line()
        save_as_label = t("animtimeline_editor.save_as")
        if ctx.button(save_as_label):
            self._execute_timeline_command("file.save_as")
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
        self._set_live_property(
            "timeline.name",
            "",
            "name",
            self._timeline.name,
            new_name,
            "Rename Timeline",
        )
        self._finish_widget_property_edit(ctx, "timeline.name")
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
        self._set_live_property(
            "timeline.duration",
            "",
            "duration",
            self._timeline.duration,
            max(0.05, float(new_dur)),
            "Set Timeline Duration",
        )
        self._finish_widget_property_edit(ctx, "timeline.duration")
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
            self._apply_discrete_property(
                "",
                "apply_mode",
                APPLY_MODES[new_mode],
                "Set Timeline Apply Mode",
            )

    def _render_transport(self, ctx: InxGUIContext):
        capture_semantics = _semantic_capture_enabled(ctx)
        play_label = t("animtimeline_editor.pause") if self._playing else t("animtimeline_editor.play")
        if ctx.button(play_label):
            self._execute_timeline_command("timeline.play_pause")
        if capture_semantics:
            ctx.record_semantic_item(
                "button", play_label, True, "animtimeline.transport.play_pause"
            )
        ctx.same_line()
        stop_label = t("animtimeline_editor.stop")
        if ctx.button(stop_label):
            self._execute_timeline_command("timeline.stop")
        if capture_semantics:
            ctx.record_semantic_item("button", stop_label, True, "animtimeline.transport.stop")
        ctx.same_line()
        add_key_label = t("animtimeline_editor.add_key")
        if ctx.button(add_key_label):
            self._execute_timeline_command("timeline.add_keyframe")
        if capture_semantics:
            ctx.record_semantic_item(
                "button", add_key_label, True, "animtimeline.transport.add_key"
            )
        ctx.same_line()
        delete_key_label = t("animtimeline_editor.delete_key")
        if ctx.button(delete_key_label):
            self._execute_timeline_command("edit.delete")
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
        ruler_y = y0 + _RULER_H
        lane_y = (ruler_y + y1) * 0.5
        ks = 6.5  # keyframe half-size (square)

        # A dedicated fixed-width track header keeps the title out of the time
        # ruler and leaves t=0 available for a real keyframe.
        header_x1 = min(x1 - 80.0, x0 + _TRACK_HEADER_W)
        pad = _BAR_EDGE_PAD
        tx0 = header_x1 + pad
        tx1 = x1 - pad
        tw = max(1.0, tx1 - tx0)

        def time_to_x(tm: float) -> float:
            return tx0 + (max(0.0, min(dur, tm)) / dur) * tw

        def x_to_time(xx: float) -> float:
            return max(0.0, min(1.0, (xx - tx0) / tw)) * dur

        # Two-row table: a dedicated Timeline header/ruler row and one compact
        # Transform track row. The track row deliberately inherits the panel
        # background instead of painting a second card behind the lane.
        ctx.draw_filled_rect(x0, y0, x1, ruler_y, *c["ruler"], 0.0)
        ctx.draw_rect(x0, y0, x1, y1, *c["tick"], 1.0, 0.0)
        ctx.draw_line(x0, ruler_y, x1, ruler_y, *c["tick"], 1.0)
        ctx.draw_line(header_x1, y0, header_x1, y1, *c["tick"], 1.0)
        ctx.draw_line(tx0, lane_y, tx1, lane_y, *c["lane"], 2.0)
        ctx.draw_text_aligned(
            x0 + 10.0,
            y0,
            header_x1 - 8.0,
            ruler_y,
            t("animtimeline_editor.timeline"),
            *c["text"],
            0.0,
            0.5,
            0.0,
            True,
        )
        ctx.draw_text_aligned(
            x0 + 10.0,
            ruler_y,
            header_x1 - 8.0,
            y1,
            t("animtimeline_editor.transform"),
            *c["text"],
            0.0,
            0.5,
            0.0,
            True,
        )

        # Ruler ticks + labels share the exact same time-to-x transform as
        # keyframes and the playhead. Labels are centered and clamped at both
        # ends so none of them float past the authored time range.
        for i in range(0, 11):
            frac = i / 10.0
            tick_x = tx0 + frac * tw
            major = (i % 2 == 0)
            ctx.draw_line(tick_x, ruler_y - (8.0 if major else 4.0), tick_x, ruler_y, *c["tick"], 1.0)
            if major:
                label_w = 58.0
                label_x0 = max(tx0, min(tick_x - label_w * 0.5, tx1 - label_w))
                ctx.draw_text_aligned(
                    label_x0,
                    y0,
                    label_x0 + label_w,
                    ruler_y - 7.0,
                    f"{frac * dur:.2f}",
                    *c["text"],
                    0.5,
                    0.5,
                    0.0,
                    True,
                )

        # Hover highlight: which keyframe would be grabbed
        hover_key = None
        my = ctx.get_mouse_pos_y()
        if hovered and not active and abs(my - lane_y) <= 18.0:
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
            if _semantic_capture_enabled(ctx):
                ctx.record_semantic_rect(
                    "timeline_keyframe",
                    f"{k.time:.2f}s",
                    kx - 9.0,
                    lane_y - 12.0,
                    18.0,
                    24.0,
                    True,
                    f"animtimeline.keyframe.{k.stable_id}",
                )

        if hover_key is not None:
            ctx.set_mouse_cursor(ImGuiMouseCursor.Hand)
            ctx.set_tooltip(f"{hover_key.time:.2f}s")

        # Playhead (line + top handle, theme accent)
        px = time_to_x(self._playhead)
        ctx.draw_line(px, y0, px, y1, *c["accent"], 2.0)
        ctx.draw_filled_rect(px - 4.0, y0, px + 4.0, y0 + 7.0, *c["accent"], 1.0)

        # ── Interaction: grab a keyframe to drag (after a threshold); empty area scrubs ──
        press_started = active and not self._bar_was_active
        if press_started:
            self._press_x = mx
            self._drag_key_id = ""
            self._drag_armed = False
            best_dx = max(9.0, ks + 2.0)
            for k in self._timeline.keyframes:
                if abs(my - lane_y) <= 18.0 and abs(time_to_x(k.time) - mx) <= best_dx:
                    best_dx = abs(time_to_x(k.time) - mx)
                    self._drag_key_id = k.stable_id
            drag_key = self._timeline.find_keyframe(self._drag_key_id)
            if drag_key is not None:
                self._select_key(drag_key)
                self._drag_armed = True          # but moving waits for the threshold
                self._playing = False
            else:
                self._playhead_scrub_before = float(self._playhead)
                self._playhead = x_to_time(mx)   # empty press scrubs right away
                self._playing = False
        elif active:
            drag_key = self._timeline.find_keyframe(self._drag_key_id)
            if drag_key is not None:
                if self._drag_armed and abs(mx - self._press_x) > _DRAG_THRESHOLD:
                    self._drag_armed = False
                if not self._drag_armed:
                    new_time = x_to_time(mx)
                    self._set_live_property(
                        f"timeline.keyframe.{drag_key.stable_id}.bar_time",
                        drag_key.stable_id,
                        "time",
                        drag_key.time,
                        new_time,
                        "Move Timeline Keyframe",
                    )
                    self._playhead = new_time
            else:
                self._playhead = x_to_time(mx)
                self._playing = False
        if not active:
            if self._drag_key_id:
                self._finish_live_property_edit(
                    f"timeline.keyframe.{self._drag_key_id}.bar_time"
                )
            self._drag_key_id = ""
            self._drag_armed = False
            self._commit_playhead_scrub()
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
                if self._preview_zoom_before is None:
                    self._preview_zoom_before = self._capture_preview_view()
                # Wide zoom range so the (now far-extending) grid can fill the view.
                self._cam_dist = max(2.0, min(40.0, self._cam_dist * (0.88 ** wheel)))
            elif self._preview_zoom_before is not None:
                self._commit_preview_view_gesture("zoom")
        elif self._preview_zoom_before is not None:
            self._commit_preview_view_gesture("zoom")
        drag_r = ctx.is_mouse_dragging(1)
        drag_m = ctx.is_mouse_dragging(2)
        if hovered and (drag_r or drag_m):
            if not self._orbiting:
                self._preview_orbit_before = self._capture_preview_view()
            self._orbiting = True
        if not (drag_r or drag_m):
            if self._orbiting:
                self._commit_preview_view_gesture("orbit")
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
        self._apply_inspector_key_time(k, nt)
        self._finish_widget_property_edit(
            ctx,
            f"timeline.keyframe.{k.stable_id}.time",
        )

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
            self._apply_discrete_property(
                k.stable_id,
                "interp",
                INTERP_MODES[nidx],
                "Set Keyframe Interpolation",
            )

        ctx.dummy(0, 4)
        ctx.push_style_color(ImGuiCol.Text, *Theme.TEXT_DIM)
        ctx.label(t("animtimeline_editor.transform"))
        ctx.pop_style_color(1)
        ctx.separator()

        self._vec3_row(
            ctx, "pos", t("animtimeline_editor.position"), k.stable_id,
            "position", 0.01, capture_semantics,
        )
        self._vec3_row(
            ctx, "rot", t("animtimeline_editor.rotation"), k.stable_id,
            "rotation", 0.25, capture_semantics,
        )
        self._vec3_row(
            ctx, "scl", t("animtimeline_editor.scale"), k.stable_id,
            "scale", 0.01, capture_semantics,
        )

    def _apply_inspector_key_time(self, key, value: float) -> bool:
        normalized_time = max(
            0.0,
            min(float(self._timeline.duration), float(value)),
        )
        # The inspector is rendered every frame.  Treat it as an editor for the
        # key time, not as the owner of the transport playhead: assigning the
        # unchanged value here used to snap every scrub/playback tick back onto
        # the selected keyframe.
        previous_time = float(key.time)
        if normalized_time == previous_time:
            return False
        changed = self._set_live_property(
            f"timeline.keyframe.{key.stable_id}.time",
            key.stable_id,
            "time",
            previous_time,
            normalized_time,
            "Move Timeline Keyframe",
        )
        if changed:
            self._playhead = normalized_time
        return bool(changed)

    def _vec3_row(
        self,
        ctx: InxGUIContext,
        vid: str,
        label: str,
        target_id: str,
        field_name: str,
        speed: float,
        capture_semantics: bool = True,
    ) -> bool:
        """Render a single-line ``label  [X][Y][Z]`` drag row."""
        ctx.label(label)
        ctx.same_line(_KF_LABEL_W)  # fixed column so all rows' fields align
        changed = False
        avail = ctx.get_content_region_avail_width()
        field_w = max(28.0, (avail - 12.0) / 3.0)
        for i, axis in enumerate(("X", "Y", "Z")):
            values = getattr(self._timeline_edit_target(target_id), field_name)
            ctx.set_next_item_width(field_w)
            nv = ctx.drag_float(f"##{vid}_{axis}", float(values[i]), speed, -1.0e9, 1.0e9)
            if capture_semantics:
                ctx.record_semantic_item(
                    "drag_float", f"{label} {axis}", True,
                    f"animtimeline.keyframe.{vid}.{axis.lower()}", numeric_value=nv,
                )
            if nv != values[i]:
                edited = list(values)
                edited[i] = float(nv)
                changed = self._set_live_property(
                    f"timeline.keyframe.{target_id}.{field_name}.{axis.lower()}",
                    target_id,
                    field_name,
                    values,
                    edited,
                    f"Set Keyframe {label} {axis}",
                ) or changed
            self._finish_widget_property_edit(
                ctx,
                f"timeline.keyframe.{target_id}.{field_name}.{axis.lower()}",
            )
            if i < 2:
                ctx.same_line(0, 6)
        return changed
