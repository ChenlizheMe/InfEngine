"""
Base class for closable editor panels.
"""

from Infernux.lib import InxGUIRenderable, InxGUIContext
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .window_manager import WindowManager


_HOVERED_CHILD_WINDOWS = 1  # ImGuiHoveredFlags_ChildWindows
_HOVERED_NO_POPUP_HIERARCHY = 8  # ImGuiHoveredFlags_NoPopupHierarchy
_PANEL_ACTIVATION_HOVER_FLAGS = _HOVERED_CHILD_WINDOWS | _HOVERED_NO_POPUP_HIERARCHY
_FOCUSED_ROOT_AND_CHILD_WINDOWS = 3  # ImGuiFocusedFlags_RootAndChildWindows


class ClosablePanel(InxGUIRenderable):
    """
    Base class for panels that can be closed via the window close button.
    """
    
    # Class-level registration info
    WINDOW_TYPE_ID: Optional[str] = None
    WINDOW_DISPLAY_NAME: Optional[str] = None
    WINDOW_TITLE_KEY: Optional[str] = None

    def __init__(self, title: str, window_id: Optional[str] = None):
        super().__init__()
        self._title = title
        self._title_key: Optional[str] = getattr(self.__class__, 'WINDOW_TITLE_KEY', None)
        self._window_id = window_id or self.__class__.__name__
        self._is_open = True
        self._window_manager: Optional['WindowManager'] = None
        self._panel_was_focused: bool = False
        self._document_id: str = ""
        self._dirty_close_approved: bool = False
        self._dirty_registry_snapshot = None
    
    @property
    def window_id(self) -> str:
        return self._window_id
    
    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def document_id(self) -> str:
        return self._document_id

    def bind_document(self, document_id: str) -> None:
        """Bind this view to one stable document identity."""
        from Infernux.engine.interaction import DocumentRegistry

        identifier = str(document_id or "").strip()
        if not identifier:
            raise ValueError("panel document_id must not be empty")
        registry = DocumentRegistry.instance()
        registry.require(identifier)
        previous_id = self._document_id
        registry.attach_view(identifier, self._window_id)
        self._document_id = identifier
        self._dirty_registry_snapshot = None
        if previous_id and previous_id != identifier:
            previous = registry.get(previous_id)
            if previous is not None and not previous.view_ids:
                registry.unregister(previous_id)
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.active_panel_id == self._window_id:
            focus.activate_panel(
                self._window_id,
                view_id=self._window_id,
                document_id=identifier,
            )

    def unbind_document(self) -> None:
        from Infernux.engine.interaction import DocumentRegistry

        registry = DocumentRegistry.instance()
        document_id = registry.detach_view(self._window_id)
        self._document_id = ""
        self._dirty_registry_snapshot = None
        if document_id:
            document = registry.get(document_id)
            if document is not None and not document.view_ids:
                registry.unregister(document_id)
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.active_panel_id == self._window_id:
            focus.activate_panel(
                self._window_id,
                view_id=self._window_id,
                document_id="",
            )
    
    def set_window_manager(self, window_manager: 'WindowManager'):
        """Set the window manager reference."""
        self._window_manager = window_manager

    def open(self):
        """Ensure this panel is visible."""
        self._is_open = True

    def close(self):
        """Request a close while preserving dirty-panel confirmation."""
        if not self.request_close():
            return
        self._is_open = False
        if self._window_manager:
            self._window_manager.set_window_open(self._window_id, False)

    def request_close(self) -> bool:
        """Return True when the panel may close immediately.

        Dirty panels remain visible while the shared Editor modal resolves the
        request asynchronously.
        """
        self._sync_dirty_registry()
        if not self._document_is_dirty():
            return True
        self._request_dirty_panel_close()
        return False

    def can_close(self, ctx: InxGUIContext) -> bool:
        """Return whether the panel can close when the titlebar X is clicked."""
        return True

    def _resolve_panel_display_title(self) -> str:
        if self._title_key:
            try:
                from Infernux.engine.i18n import t

                return t(self._title_key)
            except Exception:
                return self._title
        return self._title

    def _resolve_panel_save_handler(self):
        save_fn = getattr(self, "_do_save", None)
        if callable(save_fn):
            def _wrapped_save():
                try:
                    save_fn()
                except Exception:
                    return False
                self._sync_dirty_registry()
                # If panels expose _dirty, require it to be cleared after save.
                if hasattr(self, "_dirty"):
                    try:
                        return not bool(getattr(self, "_dirty"))
                    except Exception:
                        return False
                return True

            return _wrapped_save

        # Fallback for clip-like editors exposing _save_clip(active_clip).
        save_clip_fn = getattr(self, "_save_clip", None)
        if callable(save_clip_fn):
            def _wrapped_clip_save():
                try:
                    clip = getattr(self, "_active_clip", None)
                    if clip is None:
                        return False
                    save_clip_fn(clip)
                except Exception:
                    return False
                self._sync_dirty_registry()
                if hasattr(self, "_dirty"):
                    try:
                        return not bool(getattr(self, "_dirty"))
                    except Exception:
                        return False
                return True

            return _wrapped_clip_save

        return None

    def _sync_dirty_registry(self) -> None:
        if self._document_id:
            from Infernux.engine.interaction import DocumentRegistry

            document = DocumentRegistry.instance().get(self._document_id)
            if document is not None:
                if hasattr(self, "_dirty"):
                    self._dirty = document.is_dirty
                return
        try:
            from Infernux.engine.project_context import set_panel_dirty

            dirty = bool(getattr(self, "_dirty", False))
            title = self._resolve_panel_display_title()
            save_kind = (
                "save"
                if callable(getattr(self, "_do_save", None))
                else "clip"
                if callable(getattr(self, "_save_clip", None))
                else ""
            )
            dialog = getattr(self, "_save_as_dialog", None)
            has_save_pending = dialog is not None and hasattr(dialog, "is_open")
            discard_fn = getattr(self, "_discard_unsaved_changes", None)
            has_discard = hasattr(self, "_dirty")
            snapshot = (
                dirty,
                title,
                save_kind,
                id(dialog) if has_save_pending else 0,
                id(getattr(discard_fn, "__func__", discard_fn)) if callable(discard_fn) else 0,
                has_discard,
            )
            if snapshot == self._dirty_registry_snapshot:
                return

            set_panel_dirty(
                self._window_id,
                dirty,
                title=title,
                save_handler=self._resolve_panel_save_handler(),
                save_pending_handler=self._resolve_panel_save_pending_handler(),
                discard_handler=self._resolve_panel_discard_handler(),
            )
            self._dirty_registry_snapshot = snapshot
        except Exception:
            pass

    def _resolve_panel_save_pending_handler(self):
        dialog = getattr(self, "_save_as_dialog", None)
        if dialog is None or not hasattr(dialog, "is_open"):
            return None
        return lambda: bool(dialog.is_open)

    def _resolve_panel_discard_handler(self):
        if not hasattr(self, "_dirty"):
            return None

        discard_fn = getattr(self, "_discard_unsaved_changes", None)

        def _discard() -> None:
            if callable(discard_fn):
                discarded = discard_fn()
                if discarded is False:
                    return
            else:
                setattr(self, "_dirty", False)
            self._sync_dirty_registry()

        return _discard

    def _request_dirty_panel_close(self) -> bool:
        if not self._document_is_dirty():
            return False
        from .dirty_panel_confirmation import DirtyPanelConfirmationCoordinator

        return DirtyPanelConfirmationCoordinator.instance().request_panel_close(
            self._window_id,
            on_complete=lambda: setattr(self, "_dirty_close_approved", True),
            on_cancel=self._restore_after_cancelled_close,
        )

    def _document_is_dirty(self) -> bool:
        if self._document_id:
            from Infernux.engine.interaction import DocumentRegistry

            document = DocumentRegistry.instance().get(self._document_id)
            return bool(document and document.is_dirty)
        from Infernux.engine.project_context import is_panel_dirty

        return is_panel_dirty(self._window_id)

    def _restore_after_cancelled_close(self) -> None:
        """Restore the dock tab consumed by ImGui's titlebar close request."""
        self._is_open = True
        ClosablePanel.focus_panel_by_id(self._window_id)
        if self._window_manager is not None:
            self._window_manager.set_window_open(self._window_id, True)

    def request_focus(self, ctx: InxGUIContext):
        """Programmatically focus this panel on the next frame."""
        ctx.set_next_window_focus()

    def _activate_panel(self, ctx: InxGUIContext, *, focus_window: bool = False):
        from Infernux.engine.interaction import FocusService

        if focus_window:
            ctx.set_window_focus()

        if not FocusService.instance().activate_panel(
            self._window_id,
            view_id=self._window_id,
            document_id=self._document_id,
        ):
            return

        from .event_bus import EditorEvent, EditorEventBus
        EditorEventBus.instance().emit(EditorEvent.PANEL_FOCUSED, self._window_id)

    @staticmethod
    def _is_window_or_child_focused(ctx: InxGUIContext) -> bool:
        """Treat focused child regions as part of their owning editor panel."""
        return bool(ctx.is_window_focused(_FOCUSED_ROOT_AND_CHILD_WINDOWS))

    @classmethod
    def get_active_panel_id(cls) -> Optional[str]:
        from Infernux.engine.interaction import FocusService

        return FocusService.instance().snapshot.active_panel_id or None

    def _window_title_suffix(self) -> str:
        """Return a suffix appended to the window title (e.g. ' *' for dirty)."""
        return ""

    @classmethod
    def focus_panel_by_id(cls, panel_id: str):
        """Mark *panel_id* as active (used by undo replay to set focus target)."""
        from Infernux.engine.interaction import FocusService

        FocusService.instance().request_panel_focus(panel_id)
    
    def _begin_closable_window(self, ctx: InxGUIContext, flags: int = 0) -> bool:
        """
        Begin a closable window. Returns True if window content should be rendered.
        Handles close button automatically.
        """
        # If this panel was requested to be focused, do it before begin
        from Infernux.engine.interaction import FocusService

        if FocusService.instance().consume_panel_focus_request(self._window_id):
            ctx.set_next_window_focus()

        # Resolve title via i18n if a title_key is set
        if self._title_key:
            from Infernux.engine.i18n import t
            display = t(self._title_key)
        else:
            display = self._title

        self._sync_dirty_registry()

        display += self._window_title_suffix()
        safe_title = str(display).replace('\x00', '�').encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        # Use ### to keep a stable ImGui window ID independent of the
        # displayed title so docking layout survives locale changes.
        safe_title = f"{safe_title}###{self._window_id}"
        visible, self._is_open = ctx.begin_window_closable(safe_title, self._is_open, flags)

        if self._dirty_close_approved:
            self._dirty_close_approved = False
            self._is_open = False
            if self._window_manager:
                self._window_manager.set_window_open(self._window_id, False)
        
        # If the titlebar close button was pressed, let the panel veto close
        # (for example, unsaved-change confirmation popups).
        elif not self._is_open:
            if not self.can_close(ctx):
                self._is_open = True
            else:
                self._is_open = True
                if not self.request_close():
                    self._is_open = True
                    # Keep the document alive while the confirmation is open,
                    # but do not focus it here. The shared modal is submitted at
                    # the end of the frame and must be the sole focus owner.
                    # Cancel restores this panel through
                    # _restore_after_cancelled_close().
                else:
                    self._is_open = False
                if not self._is_open and self._window_manager:
                    self._window_manager.set_window_open(self._window_id, False)

        # ── Focus tracking ──
        if visible and self._is_open:
            pointer_activated = ctx.is_window_hovered(_PANEL_ACTIVATION_HOVER_FLAGS) and any(
                ctx.is_mouse_button_clicked(button) for button in (0, 1, 2)
            )
            if pointer_activated:
                self._activate_panel(ctx, focus_window=True)

            focused = self._is_window_or_child_focused(ctx)
            if focused and not self._panel_was_focused:
                self._activate_panel(ctx)
            # Focus lost
            elif not focused and self._panel_was_focused:
                FocusService.instance().deactivate_panel(self._window_id)
            self._panel_was_focused = focused
        
        return visible and self._is_open
