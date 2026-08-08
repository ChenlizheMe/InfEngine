"""Formal controller boundary for document-backed authoring views."""

from __future__ import annotations

from dataclasses import dataclass
import weakref
from typing import Any

from Infernux.core.document_store import submit_document_text

from .documents import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentKey,
    DocumentRegistry,
    SaveTicketStatus,
)


@dataclass(frozen=True, slots=True)
class AuthoringAssetSnapshot:
    """Immutable source snapshot submitted by a manual authoring document."""

    target_path: str
    source_text: str
    content_token: str
    title: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class _PendingAuthoringWrite:
    io_ticket: Any
    save_ticket_id: str
    snapshot: AuthoringAssetSnapshot
    save_as: bool


class AuthoringDocumentController:
    """Expose document operations without registering a Panel as authority.

    Domain models are still rendered by their owning view, but the registry,
    save coordinator, and undo commands communicate only through this explicit
    controller contract. This prevents private Panel fields from becoming an
    accidental cross-system API.
    """

    def __init__(self, view: Any) -> None:
        if view is None:
            raise TypeError("authoring document controller requires a view")
        self._view_ref = weakref.ref(view)
        self._pending_writes: dict[int, _PendingAuthoringWrite] = {}
        self._publication_error = ""

    @property
    def view(self) -> Any:
        view = self._view_ref()
        if view is None:
            raise RuntimeError("authoring document view is no longer available")
        return view

    def _call(self, method_name: str, *args, **kwargs):
        method = getattr(self.view, method_name, None)
        if not callable(method):
            raise RuntimeError(
                f"authoring document view has no {method_name!r} contract"
            )
        return method(*args, **kwargs)

    def save(self, *, ticket, save_as: bool = False):
        if callable(getattr(self.view, "capture_authoring_save_snapshot", None)):
            target = str(ticket.resource_path or "")
            if save_as or not target:
                return self._call("request_authoring_save_as", ticket)
            return self.continue_save_to_resource(ticket.ticket_id, target)
        return self._call("save", ticket=ticket, save_as=save_as)

    @property
    def publication_error(self) -> str:
        return self._publication_error

    def continue_save_to_resource(
        self,
        save_ticket_id: str,
        resource_path: str,
    ) -> DocumentActionResult:
        """Capture and submit one immutable authoring snapshot without blocking UI."""
        registry = DocumentRegistry.instance()
        ticket = registry.get_save_ticket(str(save_ticket_id or ""))
        if ticket is None or not ticket.is_pending:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the authoring save ticket is no longer pending",
            )
        target = str(resource_path or "").strip()
        if not target:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the authoring save target is missing",
            )
        try:
            snapshot = self._call("capture_authoring_save_snapshot", target)
        except Exception as exc:
            registry.complete_save(
                ticket.ticket_id,
                success=False,
                message=str(exc),
            )
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if not isinstance(snapshot, AuthoringAssetSnapshot):
            raise TypeError(
                "capture_authoring_save_snapshot() must return AuthoringAssetSnapshot"
            )
        if not snapshot.target_path or not snapshot.content_token:
            raise ValueError("authoring save snapshot requires a target and content token")
        registry.capture_save_revision(
            ticket.ticket_id,
            content_token=snapshot.content_token,
        )
        try:
            expected_file_state = registry.capture_save_target(
                ticket.ticket_id,
                snapshot.target_path,
            )
            io_ticket = submit_document_text(
                snapshot.target_path,
                snapshot.source_text,
                expected_file_state=expected_file_state,
            )
        except Exception as exc:
            registry.complete_save(
                ticket.ticket_id,
                success=False,
                message=str(exc),
            )
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        self._pending_writes[id(io_ticket)] = _PendingAuthoringWrite(
            io_ticket,
            ticket.ticket_id,
            snapshot,
            bool(ticket.save_as),
        )
        self.poll_pending_writes()
        if ticket.is_pending:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        if ticket.status is SaveTicketStatus.SUCCEEDED:
            return DocumentActionResult(DocumentActionStatus.APPLIED, ticket.message)
        if ticket.status is SaveTicketStatus.CANCELLED:
            return DocumentActionResult(DocumentActionStatus.REJECTED, ticket.message)
        return DocumentActionResult(DocumentActionStatus.FAILED, ticket.message)

    def save_to_resource(self, *, ticket, resource_path: str):
        return self.continue_save_to_resource(ticket.ticket_id, resource_path)

    def poll_pending_writes(self) -> int:
        """Finalize durable writes and publish runtime changes on the Editor thread."""
        registry = DocumentRegistry.instance()
        completed = 0
        for key, pending in tuple(self._pending_writes.items()):
            io_ticket = pending.io_ticket
            if not bool(getattr(io_ticket, "is_complete", False)):
                continue
            self._pending_writes.pop(key, None)
            completed += 1
            ticket = registry.get_save_ticket(pending.save_ticket_id)
            if ticket is None or not ticket.is_pending:
                continue
            status = str(getattr(io_ticket, "status", "") or "").casefold()
            if status != "succeeded":
                message = str(getattr(io_ticket, "error", "") or "").strip()
                conflict = "changed outside the editor" in message.casefold()
                registry.complete_save(
                    ticket.ticket_id,
                    success=False,
                    cancelled=status == "cancelled",
                    conflict=conflict,
                    message=message or f"authoring asset persistence {status or 'failed'}",
                )
                continue

            self._publication_error = ""
            publication_start_revision = registry.require(
                ticket.document_id
            ).revision
            try:
                publication = self._call(
                    "publish_authoring_save_snapshot",
                    pending.snapshot,
                )
                if publication:
                    self._publication_error = str(publication)
            except Exception as exc:
                self._publication_error = str(exc)

            published_document = registry.get(ticket.document_id)
            if (
                not self._publication_error
                and published_document is not None
                and published_document.revision != publication_start_revision
            ):
                registry.mark_save_publication_bookkeeping(ticket.ticket_id)

            document = registry.get(ticket.document_id)
            if document is None:
                registry.complete_save(
                    ticket.ticket_id,
                    success=False,
                    cancelled=True,
                    message="the authoring document closed before publication",
                )
                continue
            current_token = str(
                self._call("current_authoring_content_token") or ""
            )
            key_update = None
            if pending.save_as or not document.resource_path:
                key_update = DocumentKey.resource(
                    document.kind,
                    pending.snapshot.target_path,
                )
            registry.complete_save(
                ticket.ticket_id,
                success=True,
                key=key_update,
                resource_path=pending.snapshot.target_path,
                title=pending.snapshot.title,
                content_token=current_token,
                committed_file_state=getattr(
                    io_ticket,
                    "committed_file_state",
                    None,
                ),
                message=self._publication_error,
            )
        return completed

    def poll_save(self, ticket):
        self.poll_pending_writes()
        current = DocumentRegistry.instance().get_save_ticket(ticket.ticket_id)
        if current is None or current.is_pending:
            return None
        return current.status.value == "succeeded"

    def discard(self, *, document_id: str):
        return self._call("discard", document_id=document_id)

    def reload_from_resource(self, *, document_id: str, resource_path: str):
        del document_id
        return self.open_resource_immediate(resource_path)

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ):
        return self._call(
            "resource_moved",
            document_id=document_id,
            source_path=source_path,
            destination_path=destination_path,
            guid=guid,
        )

    def capture_document_restore_state(self, document_id: str):
        return self._call("capture_document_restore_state", document_id)

    def restore_document_restore_state(self, state) -> None:
        self._call("restore_document_restore_state", state)

    def open_resource_immediate(self, resource_path: str) -> bool:
        """Load one resource after DocumentOpenService approved replacement."""
        return bool(
            self._call(
                "open_document_resource_immediate",
                str(resource_path or ""),
            )
        )

    def capture_authoring_snapshot(self):
        return self._call("capture_authoring_snapshot")

    def restore_authoring_snapshot(self, state) -> None:
        self._call("restore_authoring_snapshot", state)

    def capture_graph_diff_checkpoint(self):
        return self._call("capture_graph_diff_checkpoint")

    def restore_graph_diff_checkpoint(self, checkpoint) -> None:
        self._call("restore_graph_diff_checkpoint", checkpoint)

    def apply_diff(self, diff) -> None:
        self._call("apply_diff", diff)

    def on_graph_diff_applied(self, diff) -> None:
        self._call("on_graph_diff_applied", diff)

    def timeline_authoring_model(self):
        return self._call("timeline_authoring_model")

    def on_timeline_authoring_applied(self) -> None:
        self._call("on_timeline_authoring_applied")


__all__ = ["AuthoringAssetSnapshot", "AuthoringDocumentController"]
