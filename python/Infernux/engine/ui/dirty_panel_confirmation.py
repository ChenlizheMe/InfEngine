"""Non-blocking Editor confirmation for dirty authoring panels."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t


class DirtyPanelConfirmationCoordinator:
    """Serialize panel save/discard/cancel decisions through one ImGui modal."""

    _instance: Optional["DirtyPanelConfirmationCoordinator"] = None

    def __init__(self) -> None:
        self._scope = ""
        self._panel_id = ""
        self._handled_ids: set[str] = set()
        self._active_entry: Optional[dict[str, Any]] = None
        self._on_complete: Optional[Callable[[], None]] = None
        self._on_cancel: Optional[Callable[[], None]] = None
        self._show_popup = False
        self._waiting_for_save = False
        self._error = ""

    @classmethod
    def instance(cls) -> "DirtyPanelConfirmationCoordinator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def is_active(self) -> bool:
        return bool(self._scope)

    @property
    def active_panel_id(self) -> str:
        return str((self._active_entry or {}).get("panel_id") or "")

    @property
    def waiting_for_save(self) -> bool:
        return self._waiting_for_save

    def request_exit(
        self,
        on_complete: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> bool:
        """Begin a global close transaction, superseding a panel-only prompt."""
        if self.is_active:
            if self._scope == "exit":
                return False
            self._reset(notify_cancel=True)
        self._begin("exit", "", on_complete, on_cancel)
        return True

    def request_panel_close(
        self,
        panel_id: str,
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Begin a titlebar-close transaction for one panel."""
        identifier = str(panel_id or "").strip()
        if not identifier or self.is_active:
            return False
        self._begin("panel", identifier, on_complete, on_cancel)
        return True

    def render(self, ctx, *, panel_host_id: Optional[str] = None) -> None:
        """Poll saves and render from the window that owns the transaction.

        A panel-close popup must be submitted while its source panel is the
        current ImGui window. Otherwise an undocked dock host can remain above
        the modal even when the modal is registered as a late global overlay.
        Exit confirmations have no single panel host and stay on the global
        overlay path.
        """
        if not self.is_active:
            return
        if self._scope == "panel":
            if str(panel_host_id or "") != self._panel_id:
                return
        elif panel_host_id is not None:
            return
        if self._waiting_for_save:
            self._poll_save()
        if not self.is_active or self._waiting_for_save:
            return

        entry = self._active_entry
        if entry is None:
            self._advance()
            entry = self._active_entry
        if entry is None:
            return
        if not self._entry_is_dirty(entry):
            self._resolve_active()
            entry = self._active_entry
            if entry is None:
                return

        from .unsaved_changes_dialog import render_unsaved_changes_dialog

        popup_id = "Unsaved Changes###editor_dirty_panel_confirm"
        title = str(entry.get("title") or entry.get("panel_id") or "Panel")
        choice = render_unsaved_changes_dialog(
            ctx,
            popup_id=popup_id,
            semantic_prefix="editor.dirty_panel",
            document_title=title,
            action="exit" if self._scope == "exit" else "close",
            error=self._error,
            request_open=self._show_popup,
        )
        self._show_popup = False
        if choice == "save":
            self.choose_save()
        elif choice == "discard":
            self.choose_discard()
        elif choice == "cancel":
            self.choose_cancel()

    def choose_save(self) -> None:
        entry = self._active_entry
        if entry is None:
            return
        save_handler = entry.get("save_handler")
        if not callable(save_handler):
            self._error = t("editor.unsaved.no_save_action")
            self._show_popup = True
            return
        try:
            save_handler()
        except Exception as exc:
            Debug.log_suppressed(
                f"DirtyPanelConfirmation.save[{self.active_panel_id}]", exc
            )
            self._error = t("editor.unsaved.save_failed")
            self._show_popup = True
            return

        if not self._entry_is_dirty(entry):
            self._resolve_active()
            return
        if self._entry_save_pending(entry):
            self._waiting_for_save = True
            self._error = ""
            return
        self._error = t("editor.unsaved.save_cancelled")
        self._show_popup = True

    def choose_discard(self) -> None:
        entry = self._active_entry
        if entry is None:
            return
        # A global discard applies only to this close transaction. If a later
        # scene confirmation is cancelled, the still-open panel must remain
        # dirty instead of silently treating its in-memory edits as saved.
        if self._scope == "panel":
            discard_handler = entry.get("discard_handler")
            if not callable(discard_handler):
                self._error = t("editor.unsaved.no_discard_action")
                self._show_popup = True
                return
            try:
                discard_handler()
            except Exception as exc:
                Debug.log_suppressed(
                    f"DirtyPanelConfirmation.discard[{self.active_panel_id}]", exc
                )
                self._error = t("editor.unsaved.discard_failed")
                self._show_popup = True
                return
            if self._entry_is_dirty(entry):
                self._error = t("editor.unsaved.still_dirty")
                self._show_popup = True
                return
        self._resolve_active()

    def choose_cancel(self) -> None:
        self._reset(notify_cancel=True)

    def _begin(
        self,
        scope: str,
        panel_id: str,
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]],
    ) -> None:
        self._scope = scope
        self._panel_id = panel_id
        self._handled_ids.clear()
        self._active_entry = None
        self._on_complete = on_complete
        self._on_cancel = on_cancel
        self._show_popup = False
        self._waiting_for_save = False
        self._error = ""
        self._advance()

    def _advance(self) -> None:
        from Infernux.engine.project_context import get_dirty_panel_entries

        entries = list(get_dirty_panel_entries())
        if self._scope == "panel":
            entries = [
                entry
                for entry in entries
                if str(entry.get("panel_id") or "") == self._panel_id
            ]
        else:
            entries = [
                entry
                for entry in entries
                if str(entry.get("panel_id") or "") not in self._handled_ids
            ]

        if entries:
            self._active_entry = entries[0]
            self._show_popup = True
            self._waiting_for_save = False
            self._error = ""
            return

        callback = self._on_complete
        self._reset(notify_cancel=False)
        self._invoke(callback, "complete")

    def _resolve_active(self) -> None:
        panel_id = self.active_panel_id
        if panel_id:
            self._handled_ids.add(panel_id)
        self._active_entry = None
        self._waiting_for_save = False
        self._error = ""
        self._advance()

    def _poll_save(self) -> None:
        entry = self._active_entry
        if entry is None:
            self._waiting_for_save = False
            self._advance()
            return
        if not self._entry_is_dirty(entry):
            self._resolve_active()
            return
        if self._entry_save_pending(entry):
            return
        self._waiting_for_save = False
        self._error = t("editor.unsaved.save_cancelled")
        self._show_popup = True

    @staticmethod
    def _entry_is_dirty(entry: dict[str, Any]) -> bool:
        from Infernux.engine.project_context import is_panel_dirty

        return is_panel_dirty(str(entry.get("panel_id") or ""))

    @staticmethod
    def _entry_save_pending(entry: dict[str, Any]) -> bool:
        handler = entry.get("save_pending_handler")
        if not callable(handler):
            return False
        try:
            return bool(handler())
        except Exception as exc:
            Debug.log_suppressed("DirtyPanelConfirmation.save_pending", exc)
            return False

    def _reset(self, *, notify_cancel: bool) -> None:
        callback = self._on_cancel if notify_cancel else None
        self._scope = ""
        self._panel_id = ""
        self._handled_ids.clear()
        self._active_entry = None
        self._on_complete = None
        self._on_cancel = None
        self._show_popup = False
        self._waiting_for_save = False
        self._error = ""
        self._invoke(callback, "cancel")

    @staticmethod
    def _invoke(callback: Optional[Callable[[], None]], action: str) -> None:
        if not callable(callback):
            return
        try:
            callback()
        except Exception as exc:
            Debug.log_suppressed(f"DirtyPanelConfirmation.{action}", exc)
