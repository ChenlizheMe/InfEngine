"""
Base class for closable editor panels.
"""

import time

from Infernux.lib import InxGUIRenderable, InxGUIContext
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .window_manager import WindowManager


_HOVERED_CHILD_WINDOWS = 1  # ImGuiHoveredFlags_ChildWindows
_HOVERED_NO_POPUP_HIERARCHY = 8  # ImGuiHoveredFlags_NoPopupHierarchy
_PANEL_ACTIVATION_HOVER_FLAGS = _HOVERED_CHILD_WINDOWS | _HOVERED_NO_POPUP_HIERARCHY
_FOCUSED_CURRENT_AND_CHILD_WINDOWS = 1  # ImGuiFocusedFlags_ChildWindows
_POINTER_FOCUS_GRACE_SECONDS = 0.25


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
        self._panel_type_id = (
            str(getattr(self.__class__, "WINDOW_TYPE_ID", "") or "").strip()
            or self._window_id
        )
        self._is_open = True
        self._window_manager: Optional['WindowManager'] = None
        self._panel_was_focused: bool = False
        self._last_pointer_press_at: float = 0.0
        self._document_id: str = ""
        self._dormant_document_locator = None
        self._dirty_close_approved: bool = False
        self._dirty_registry_snapshot = None
        self._missing_document_binding_reported: bool = False
        self._discard_document_on_unbind: bool = False
    
    @property
    def window_id(self) -> str:
        return self._window_id

    @property
    def panel_type_id(self) -> str:
        """Stable panel capability type, independent from this view instance."""
        return self._panel_type_id

    def set_panel_identity(self, panel_type_id: str, view_id: str) -> None:
        """Assign the type/view identity owned by :class:`WindowManager`.

        A factory may bind a document before the manager knows the final
        instance id. Attach the replacement view before detaching the old one
        so the document never becomes dormant during this identity transfer.
        """
        resolved_type_id = str(panel_type_id or "").strip()
        resolved_view_id = str(view_id or "").strip()
        if not resolved_type_id or not resolved_view_id:
            raise ValueError("panel identity requires panel_type_id and view_id")
        old_view_id = self._window_id
        old_type_id = self._panel_type_id
        if old_view_id == resolved_view_id and old_type_id == resolved_type_id:
            return

        if self._document_id and old_view_id != resolved_view_id:
            from Infernux.engine.interaction import DocumentRegistry

            registry = DocumentRegistry.instance()
            registry.attach_view(self._document_id, resolved_view_id)
            registry.detach_view(old_view_id)

        self._window_id = resolved_view_id
        self._panel_type_id = resolved_type_id

        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.active_view_id == old_view_id:
            focus.activate_panel(
                resolved_type_id,
                view_id=resolved_view_id,
                document_id=self._document_id,
                child_context_id=self.current_child_context_id(),
                reason="panel_identity_assigned",
                record_history=False,
            )
    
    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def document_id(self) -> str:
        return self._document_id

    def bind_document(
        self,
        document_id: str,
        *,
        replace_existing: bool = False,
    ) -> None:
        """Bind this view to one stable document identity."""
        from Infernux.engine.interaction import DocumentRegistry

        identifier = str(document_id or "").strip()
        if not identifier:
            raise ValueError("panel document_id must not be empty")
        registry = DocumentRegistry.instance()
        registry.require(identifier)
        previous_id = self._document_id
        if previous_id and previous_id != identifier:
            if replace_existing:
                registry.replace_view_document(identifier, self._window_id)
            else:
                registry.close_view(self._window_id)
            self._document_id = ""
        if registry.document_for_view(self._window_id) is not registry.require(identifier):
            registry.attach_view(identifier, self._window_id)
        self._document_id = identifier
        self._dormant_document_locator = None
        self._discard_document_on_unbind = False
        self._dirty_registry_snapshot = None
        self._missing_document_binding_reported = False
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.active_view_id == self._window_id:
            focus.activate_panel(
                self._panel_type_id,
                view_id=self._window_id,
                document_id=identifier,
                reason="panel_document_bound",
                record_history=False,
            )

    def _bind_replaced_document(self, document_id: str, *, dirty: bool) -> None:
        """Bind an explicitly loaded/new document and reconcile its save point.

        ``DocumentRegistry.open_or_create`` deliberately preserves an existing
        document's revision state.  Authoring panels, however, also use it
        after replacing their in-memory model from disk or creating a new
        model.  That operation has authoritative dirty semantics which must be
        projected into the registry before close/discard coordination runs.
        """
        from Infernux.engine.interaction import DocumentRegistry

        self.bind_document(document_id, replace_existing=True)
        registry = DocumentRegistry.instance()
        document = registry.require(document_id)
        if dirty and not document.is_dirty:
            registry.mark_changed(document_id, view_id=self._window_id)
        elif not dirty and document.is_dirty:
            registry.establish_loaded_baseline(document_id)

    def unbind_document(self) -> None:
        from Infernux.engine.interaction import DocumentRegistry

        registry = DocumentRegistry.instance()
        terminal_discard = self._discard_document_on_unbind
        locator = None if terminal_discard else registry.locate(self._document_id)
        # The absence of a locator is authoritative too.  A terminal discard
        # may retire the document before the native window finishes closing;
        # retaining an older locator would make the next open revive a stale
        # authoring session.
        self._dormant_document_locator = locator
        registry.close_view(
            self._window_id,
            preserve_dormant=not terminal_discard,
        )
        self._document_id = ""
        self._discard_document_on_unbind = False
        self._dirty_registry_snapshot = None
        self._missing_document_binding_reported = False
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.active_view_id == self._window_id:
            focus.activate_panel(
                self._panel_type_id,
                view_id=self._window_id,
                document_id="",
                reason="panel_document_unbound",
                record_history=False,
            )

    def retire_deleted_document(self, document_id: str) -> bool:
        """Make the current resource document terminal for the next close."""
        if not self._document_id or self._document_id != str(document_id or ""):
            return False
        self._discard_document_on_unbind = True
        self._dormant_document_locator = None
        return True

    def request_document_replacement(self, replace) -> bool:
        """Run one destructive new/reset operation through shared close policy."""
        if not callable(replace):
            raise TypeError("document replacement callback must be callable")
        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().get(self._document_id)
        if document is None or not document.is_dirty:
            return replace() is not False

        from .dirty_panel_confirmation import DirtyPanelConfirmationCoordinator

        return DirtyPanelConfirmationCoordinator.instance().request_document_replace(
            document.document_id,
            on_complete=replace,
            owner_id=self._window_id,
        )

    def request_document_resource_open(self, kind, path: str) -> bool:
        """Open one asset through DocumentOpenService and its close transaction."""
        from Infernux.engine.interaction import (
            DocumentOpenStatus,
            EditorInteractionCore,
        )

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("document open requires EditorInteractionCore")
        result = core.document_open.open_resource(kind, str(path or ""))
        return result.status is not DocumentOpenStatus.FAILED

    def _document_controller_for_registry(self):
        """Return the formal controller connected to this document view."""
        return getattr(self, "_authoring_document_controller", self)

    def restore_dormant_document(self, locator) -> bool:
        """Rebind a closed session document and restore its authoring snapshot."""
        from Infernux.engine.interaction import DocumentRegistry

        registry = DocumentRegistry.instance()
        state = registry.dormant_restore_state(locator)
        controller = self._document_controller_for_registry()
        document = registry.restore_dormant(locator, controller=controller)
        if document is None:
            return False
        self.bind_document(document.document_id)
        restore = getattr(controller, "restore_document_restore_state", None)
        if state is not None and callable(restore):
            restore(state)
        return True

    def capture_document_restore_state(self, document_id: str):
        """Return authoring state for document suspension when explicitly supported."""
        if document_id != self._document_id:
            raise ValueError("document restore capture targeted another document")
        return None

    def restore_document_restore_state(self, state) -> None:
        """Restore explicitly captured authoring state for a suspended document."""
        if state is not None:
            raise RuntimeError(
                f"panel '{self._panel_type_id}' has no document restore contract"
            )

    def restore_persisted_session_document(self) -> bool:
        """Claim this View's persisted authoring document from the Registry."""
        from Infernux.engine.interaction import DocumentRegistry

        registry = DocumentRegistry.instance()
        controller = self._document_controller_for_registry()
        claimed = registry.claim_session_document(
            self._window_id,
            controller=controller,
        )
        if claimed is None:
            return False
        document, restore_state = claimed
        self._document_id = ""
        self.bind_document(document.document_id)
        try:
            if restore_state is not None:
                controller.restore_document_restore_state(restore_state)
        except (KeyError, TypeError, ValueError) as exc:
            registry.unregister(document.document_id, preserve_dormant=False)
            self._document_id = ""
            recover = getattr(
                self,
                "recover_incompatible_document_restore_state",
                None,
            )
            if not callable(recover) or recover(restore_state, exc) is not True:
                raise
            from Infernux.debug import Debug

            Debug.log_warning(
                f"Discarded incompatible {document.kind.value} editor session: {exc}"
            )
        self._dirty_registry_snapshot = None
        return True

    def recover_incompatible_document_restore_state(
        self,
        state,
        error: Exception,
    ) -> bool:
        """Recover from a private session schema break without compatibility parsing."""
        del state, error
        return False
    
    def set_window_manager(self, window_manager: 'WindowManager'):
        """Set the window manager reference."""
        self._window_manager = window_manager

    def open(self):
        """Ensure this panel is visible."""
        if not self._document_id and self._dormant_document_locator is not None:
            locator = self._dormant_document_locator
            if not self.restore_dormant_document(locator):
                raise RuntimeError(
                    f"Could not restore document binding for panel '{self._window_id}'"
                )
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
        # Closing a View of a document that remains represented elsewhere is
        # resolved synchronously by CloseCoordinator and needs no modal. Consume
        # that approval here so WindowManager can complete the same lifecycle
        # transaction instead of requiring an otherwise unrelated render frame.
        if self._dirty_close_approved:
            self._dirty_close_approved = False
            return True
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

    def _sync_dirty_registry(self) -> None:
        if self._document_id:
            from Infernux.engine.interaction import DocumentRegistry

            document = DocumentRegistry.instance().get(self._document_id)
            if document is not None:
                return
        if not self._is_authoring_panel() or self._missing_document_binding_reported:
            return
        self._missing_document_binding_reported = True
        from Infernux.debug import Debug

        Debug.log_error(
            "[EditorInteraction] Authoring panel "
            f"'{self._window_id}' has no formal DocumentRegistry binding; "
            "its edits and close behavior are disabled until the panel binds a document"
        )

    def _is_authoring_panel(self) -> bool:
        descriptor = getattr(type(self), "PANEL_INTERACTION", None)
        return bool(descriptor and descriptor.document_backed)

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
        document_id = getattr(self, "_document_id", "")
        if document_id:
            from Infernux.engine.interaction import DocumentRegistry

            document = DocumentRegistry.instance().get(document_id)
            return bool(
                document
                and document.is_dirty_for_view(getattr(self, "_window_id", ""))
            )
        return False

    def _restore_after_cancelled_close(self) -> None:
        """Restore the dock tab consumed by ImGui's titlebar close request."""
        self._is_open = True
        ClosablePanel.focus_panel_by_id(self._window_id)
        if self._window_manager is not None:
            self._window_manager.set_window_open(self._window_id, True)

    def request_focus(self, ctx: InxGUIContext):
        """Programmatically focus this panel on the next frame."""
        ctx.set_next_window_focus()

    def _activate_panel(
        self,
        ctx: InxGUIContext,
        *,
        focus_window: bool = False,
        record_history: bool = True,
    ):
        from Infernux.engine.interaction import FocusService

        if focus_window:
            ctx.set_window_focus()

        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is not None:
            record_history = bool(
                record_history
                and core.panels.records_focus_history(
                    type_id=self._panel_type_id,
                    view_id=self._window_id,
                )
            )

        if not FocusService.instance().activate_panel(
            self._panel_type_id,
            view_id=self._window_id,
            document_id=self._document_id,
            child_context_id=self.current_child_context_id(),
            record_history=record_history,
        ):
            return

    def current_child_context_id(self) -> str:
        """Return the stable, user-visible subview owned by this panel."""
        return ""

    def restore_child_context(self, context_id: str) -> bool:
        """Restore a child subview during history replay.

        Panels with tabs or modes override this method. An empty context is
        universally valid and means that the panel has no tracked subview.
        """
        return not str(context_id or "")

    def publish_child_context(
        self,
        context_id: str,
        *,
        reason: str = "panel_child_context",
        record_history: bool = True,
    ) -> bool:
        """Publish one explicit, independently undoable subview transition."""
        from Infernux.engine.interaction import FocusService

        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is not None:
            record_history = bool(
                record_history
                and core.panels.records_focus_history(
                    type_id=self._panel_type_id,
                    view_id=self._window_id,
                )
            )

        focus = FocusService.instance()
        if focus.snapshot.active_view_id != self._window_id:
            return focus.activate_panel(
                self._panel_type_id,
                view_id=self._window_id,
                document_id=self._document_id,
                child_context_id=str(context_id or ""),
                reason=reason,
                record_history=record_history,
            )
        return focus.set_child_context(
            self._panel_type_id,
            str(context_id or ""),
            view_id=self._window_id,
            reason=reason,
            record_history=record_history,
        )

    @staticmethod
    def _is_window_or_child_focused(ctx: InxGUIContext) -> bool:
        """Treat focused child regions as part of their owning editor panel."""
        return bool(ctx.is_window_focused(_FOCUSED_CURRENT_AND_CHILD_WINDOWS))

    @classmethod
    def get_active_panel_id(cls) -> Optional[str]:
        from Infernux.engine.interaction import FocusService

        return FocusService.instance().snapshot.active_panel_id or None

    @classmethod
    def get_active_view_id(cls) -> Optional[str]:
        from Infernux.engine.interaction import FocusService

        return FocusService.instance().snapshot.active_view_id or None

    def _window_title_suffix(self) -> str:
        """Project only this View's contribution to document dirty state."""
        return " *" if self._document_is_dirty() else ""

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
        presentation_probe = getattr(ctx, "is_current_window_content_presented", None)
        content_presented = (
            bool(presentation_probe())
            if callable(presentation_probe)
            else bool(visible and self._is_open)
        )
        self._content_presented_this_frame = bool(
            content_presented and self._is_open
        )

        # A hidden dock tab cannot remain locally focused.  Keep the global
        # FocusService snapshot until another panel publishes its edge, but
        # clear this per-view latch immediately so revealing the tab later is
        # observed as a fresh transition.  Without this reset, returning to a
        # panel that had once been focused silently skipped its focus-history
        # entry and the next document edit appeared to jump across panels.
        if not self._content_presented_this_frame:
            self._panel_was_focused = False

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
                    # ImGui consumes the dock tab's close request immediately.
                    # Restore the source tab before submitting the shared modal
                    # so the document remains visibly in place behind it.  The
                    # modal is promoted after dock processing and remains the
                    # final keyboard-focus owner for this frame.
                    self._activate_panel(ctx, record_history=False)
                    if self._window_manager is not None:
                        self._window_manager.restore_close_confirmation_source(
                            self._window_id
                        )
                else:
                    self._is_open = False
                if not self._is_open and self._window_manager:
                    self._window_manager.set_window_open(self._window_id, False)

        pointer_pressed = any(
            ctx.is_mouse_button_clicked(button) for button in (0, 1, 2)
        )
        popup_capture_probe = getattr(
            ctx, "is_pointer_activation_blocked_by_popup", None
        )
        pointer_captured_by_popup = bool(
            popup_capture_probe() if callable(popup_capture_probe) else False
        )
        pointer_gesture = bool(pointer_pressed and not pointer_captured_by_popup)
        now = time.perf_counter()
        if pointer_gesture:
            # Dock activation becomes visible one ImGui frame after the tab
            # press on some layouts. Every open panel observes the press, but
            # only the panel receiving the subsequent focus edge consumes it.
            self._last_pointer_press_at = now
        elif pointer_pressed:
            # The click belongs to a menu/popup that may already have closed
            # earlier in this frame. Do not reuse an older panel press as a
            # focus-history grace edge.
            self._last_pointer_press_at = 0.0

        # ── Focus tracking ──
        if visible and self._is_open:
            pointer_activated = (
                not pointer_captured_by_popup
                and ctx.is_window_hovered(_PANEL_ACTIVATION_HOVER_FLAGS)
                and pointer_gesture
            )
            if pointer_activated:
                self._activate_panel(
                    ctx,
                    focus_window=True,
                    record_history=not bool(
                        getattr(self, "_content_visible_previous_frame", False)
                    ),
                )

            focused = self._is_window_or_child_focused(ctx)
            if (
                focused
                and not self._panel_was_focused
                and not pointer_captured_by_popup
            ):
                # Clicking a dock tab focuses the window without making its
                # content region hovered. The focus edge and same-frame mouse
                # press are therefore the shared signal for an explicit tab
                # switch. Layout restoration and programmatic replay have no
                # pointer press and remain passive synchronization.
                self._activate_panel(
                    ctx,
                    record_history=bool(
                        not bool(
                            getattr(self, "_content_visible_previous_frame", False)
                        )
                        and (
                            pointer_gesture
                            or now - self._last_pointer_press_at
                            <= _POINTER_FOCUS_GRACE_SECONDS
                        )
                    ),
                )
                self._last_pointer_press_at = 0.0
            # Keep the last active panel until another panel publishes focus.
            # This turns a visual A -> B transition into one history action
            # instead of the render-order-dependent A -> empty -> B pair.
            # Real close operations deactivate through WindowManager.
            self._panel_was_focused = focused
        
        return visible and self._is_open
