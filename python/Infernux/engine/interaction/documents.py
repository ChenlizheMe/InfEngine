"""Stable editor documents, revisions, views, and authoring operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntFlag, auto
from typing import Any, Optional, Protocol
import uuid

from Infernux.engine.path_utils import path_key


class DocumentKind(str, Enum):
    SCENE = "scene"
    PREFAB = "prefab"
    MATERIAL = "material"
    RENDER_EFFECT = "render_effect"
    ANIMATION_CLIP = "animation_clip"
    ANIMATION_FSM = "animation_fsm"
    TIMELINE = "timeline"
    PARTICLE_GRAPH = "particle_graph"
    UI_DOCUMENT = "ui_document"
    GENERIC = "generic"


class DocumentIdentityKind(str, Enum):
    ASSET_GUID = "asset_guid"
    RESOURCE_PATH = "resource_path"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class DocumentKey:
    kind: DocumentKind
    identity_kind: DocumentIdentityKind
    identity: str

    def __post_init__(self) -> None:
        kind = DocumentKind(self.kind)
        identity_kind = DocumentIdentityKind(self.identity_kind)
        identity = str(self.identity or "").strip()
        if identity_kind is DocumentIdentityKind.RESOURCE_PATH:
            identity = path_key(identity)
        elif identity_kind is DocumentIdentityKind.ASSET_GUID:
            identity = identity.casefold()
        if not identity:
            raise ValueError("document identity must not be empty")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "identity_kind", identity_kind)
        object.__setattr__(self, "identity", identity)

    @classmethod
    def asset(cls, kind: DocumentKind, guid: str) -> "DocumentKey":
        return cls(kind, DocumentIdentityKind.ASSET_GUID, guid)

    @classmethod
    def resource(cls, kind: DocumentKind, path: str) -> "DocumentKey":
        return cls(kind, DocumentIdentityKind.RESOURCE_PATH, path)

    @classmethod
    def session(
        cls,
        kind: DocumentKind,
        session_id: str = "",
    ) -> "DocumentKey":
        return cls(kind, DocumentIdentityKind.SESSION, session_id or uuid.uuid4().hex)


class DocumentState(str, Enum):
    READY = "ready"
    SAVING = "saving"
    CONFLICT = "conflict"
    CLOSED = "closed"


class DocumentCapability(IntFlag):
    NONE = 0
    SAVE = auto()
    SAVE_AS = auto()
    DISCARD = auto()


class DocumentActionStatus(str, Enum):
    APPLIED = "applied"
    PENDING = "pending"
    NO_OP = "no_op"
    REJECTED = "rejected"
    FAILED = "failed"


class SaveTicketStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DocumentActionResult:
    status: DocumentActionStatus
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {
            DocumentActionStatus.APPLIED,
            DocumentActionStatus.PENDING,
            DocumentActionStatus.NO_OP,
        }


@dataclass(slots=True)
class SaveTicket:
    ticket_id: str
    document_id: str
    captured_revision: int
    save_as: bool = False
    operation_id: str = ""
    status: SaveTicketStatus = SaveTicketStatus.PENDING
    message: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status is SaveTicketStatus.PENDING


class DocumentController(Protocol):
    def save(self, *, ticket: SaveTicket, save_as: bool = False) -> Any: ...

    def discard(self) -> Any: ...


@dataclass(slots=True)
class EditorDocument:
    document_id: str
    key: DocumentKey
    kind: DocumentKind
    title: str
    resource_path: str = ""
    revision: int = 0
    saved_revision: int = 0
    state: DocumentState = DocumentState.READY
    capabilities: DocumentCapability = DocumentCapability.NONE
    view_ids: set[str] = field(default_factory=set)
    controller: Optional[DocumentController] = field(default=None, repr=False)

    @property
    def is_dirty(self) -> bool:
        return self.revision != self.saved_revision


class DocumentRegistry:
    """Single authority for editor document identity and dirty revisions."""

    _instance: Optional["DocumentRegistry"] = None

    def __init__(self) -> None:
        self._documents: dict[str, EditorDocument] = {}
        self._document_ids_by_key: dict[DocumentKey, str] = {}
        self._view_documents: dict[str, str] = {}
        self._save_tickets: dict[str, SaveTicket] = {}
        self._active_save_by_document: dict[str, str] = {}
        self._revision = 0
        DocumentRegistry._instance = self

    @classmethod
    def instance(cls) -> "DocumentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def documents(self) -> tuple[EditorDocument, ...]:
        return tuple(self._documents.values())

    def create(
        self,
        kind: DocumentKind,
        title: str,
        *,
        document_id: str = "",
        key: Optional[DocumentKey] = None,
        resource_path: str = "",
        revision: int = 0,
        saved_revision: Optional[int] = None,
        capabilities: DocumentCapability = DocumentCapability.NONE,
        controller: Optional[DocumentController] = None,
    ) -> EditorDocument:
        identifier = str(document_id or uuid.uuid4().hex).strip()
        if not identifier:
            raise ValueError("document_id must not be empty")
        if identifier in self._documents:
            raise ValueError(f"document already registered: {identifier}")
        document_kind = DocumentKind(kind)
        document_key = key
        if document_key is None:
            document_key = (
                DocumentKey.resource(document_kind, resource_path)
                if resource_path
                else DocumentKey.session(document_kind, identifier)
            )
        elif document_key.kind is not document_kind:
            raise ValueError("document key kind must match the document kind")
        existing_id = self._document_ids_by_key.get(document_key)
        if existing_id is not None:
            raise ValueError(
                f"document key already registered by {existing_id}: {document_key}"
            )
        current_revision = max(0, int(revision))
        clean_revision = (
            current_revision if saved_revision is None else max(0, int(saved_revision))
        )
        if clean_revision > current_revision:
            raise ValueError("saved_revision cannot exceed revision")
        document = EditorDocument(
            document_id=identifier,
            key=document_key,
            kind=document_kind,
            title=str(title or identifier),
            resource_path=str(resource_path or ""),
            revision=current_revision,
            saved_revision=clean_revision,
            capabilities=DocumentCapability(capabilities),
            controller=controller,
        )
        self._documents[identifier] = document
        self._document_ids_by_key[document_key] = identifier
        self._touch()
        return document

    def get(self, document_id: str) -> Optional[EditorDocument]:
        return self._documents.get(str(document_id or ""))

    def require(self, document_id: str) -> EditorDocument:
        document = self.get(document_id)
        if document is None:
            raise KeyError(f"unknown editor document: {document_id}")
        return document

    def get_by_key(self, key: DocumentKey) -> Optional[EditorDocument]:
        document_id = self._document_ids_by_key.get(key)
        return self.get(document_id or "")

    def open_or_create(
        self,
        key: DocumentKey,
        title: str,
        *,
        resource_path: str = "",
        revision: int = 0,
        saved_revision: Optional[int] = None,
        capabilities: DocumentCapability = DocumentCapability.NONE,
        controller: Optional[DocumentController] = None,
    ) -> tuple[EditorDocument, bool]:
        existing = self.get_by_key(key)
        if existing is not None:
            self.update_metadata(
                existing.document_id,
                title=title,
                resource_path=resource_path,
                capabilities=capabilities,
                controller=controller,
            )
            return existing, False
        return (
            self.create(
                key.kind,
                title,
                key=key,
                resource_path=resource_path,
                revision=revision,
                saved_revision=saved_revision,
                capabilities=capabilities,
                controller=controller,
            ),
            True,
        )

    def rekey(
        self,
        document_id: str,
        key: DocumentKey,
        *,
        resource_path: Optional[str] = None,
    ) -> bool:
        document = self.require(document_id)
        if key.kind is not document.kind:
            raise ValueError("document key kind must match the document kind")
        if key == document.key:
            if resource_path is not None:
                return self.update_metadata(document_id, resource_path=resource_path)
            return False
        existing_id = self._document_ids_by_key.get(key)
        if existing_id is not None and existing_id != document.document_id:
            raise ValueError(f"document key already registered by {existing_id}: {key}")
        del self._document_ids_by_key[document.key]
        self._document_ids_by_key[key] = document.document_id
        document.key = key
        if resource_path is not None:
            document.resource_path = str(resource_path)
        self._touch()
        return True

    def document_for_view(self, view_id: str) -> Optional[EditorDocument]:
        document_id = self._view_documents.get(str(view_id or ""))
        return self.get(document_id or "")

    def attach_view(self, document_id: str, view_id: str) -> bool:
        document = self.require(document_id)
        view_id = str(view_id or "").strip()
        if not view_id:
            raise ValueError("document view_id must not be empty")
        previous_id = self._view_documents.get(view_id)
        if previous_id == document.document_id:
            return False
        if previous_id:
            previous = self.get(previous_id)
            if previous is not None:
                previous.view_ids.discard(view_id)
        self._view_documents[view_id] = document.document_id
        document.view_ids.add(view_id)
        self._touch()
        return True

    def detach_view(self, view_id: str) -> Optional[str]:
        view_id = str(view_id or "").strip()
        document_id = self._view_documents.pop(view_id, None)
        if document_id is None:
            return None
        document = self.get(document_id)
        if document is not None:
            document.view_ids.discard(view_id)
        self._touch()
        return document_id

    def unregister(self, document_id: str) -> bool:
        document = self._documents.pop(str(document_id or ""), None)
        if document is None:
            return False
        self._document_ids_by_key.pop(document.key, None)
        ticket_id = self._active_save_by_document.pop(document.document_id, None)
        if ticket_id:
            ticket = self._save_tickets.get(ticket_id)
            if ticket is not None and ticket.is_pending:
                ticket.status = SaveTicketStatus.CANCELLED
                ticket.message = "document was closed"
        for view_id in tuple(document.view_ids):
            self._view_documents.pop(view_id, None)
        document.view_ids.clear()
        document.state = DocumentState.CLOSED
        self._touch()
        return True

    def update_metadata(
        self,
        document_id: str,
        *,
        title: Optional[str] = None,
        resource_path: Optional[str] = None,
        capabilities: Optional[DocumentCapability] = None,
        controller: Optional[DocumentController] = None,
    ) -> bool:
        document = self.require(document_id)
        changed = False
        if title is not None and document.title != str(title):
            document.title = str(title)
            changed = True
        if resource_path is not None and document.resource_path != str(resource_path):
            document.resource_path = str(resource_path)
            changed = True
        if capabilities is not None and document.capabilities != capabilities:
            document.capabilities = DocumentCapability(capabilities)
            changed = True
        if controller is not None and document.controller is not controller:
            document.controller = controller
            changed = True
        if changed:
            self._touch()
        return changed

    def mark_changed(self, document_id: str) -> int:
        document = self.require(document_id)
        document.revision += 1
        if (
            document.state is not DocumentState.CONFLICT
            and self.active_save_ticket(document_id) is None
        ):
            document.state = DocumentState.READY
        self._touch()
        return document.revision

    def mark_saved(self, document_id: str, revision: Optional[int] = None) -> int:
        document = self.require(document_id)
        target = document.revision if revision is None else int(revision)
        if target < 0 or target > document.revision:
            raise ValueError("saved revision must reference an existing document revision")
        document.saved_revision = target
        document.state = DocumentState.READY
        self._touch()
        return target

    def mark_conflict(self, document_id: str) -> None:
        document = self.require(document_id)
        if document.state is DocumentState.CONFLICT:
            return
        document.state = DocumentState.CONFLICT
        self._touch()

    def dirty_documents(self) -> tuple[EditorDocument, ...]:
        return tuple(document for document in self._documents.values() if document.is_dirty)

    def request_save(
        self,
        document_id: str,
        *,
        save_as: bool = False,
    ) -> DocumentActionResult:
        document = self.require(document_id)
        capability = DocumentCapability.SAVE_AS if save_as else DocumentCapability.SAVE
        if not document.capabilities & capability:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "save is not supported")
        if not save_as and not document.is_dirty:
            return DocumentActionResult(DocumentActionStatus.NO_OP)
        active = self.active_save_ticket(document_id)
        if active is not None:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        controller = document.controller
        if controller is None:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "document has no controller")
        ticket = self.begin_save(document_id, save_as=save_as)
        try:
            result = controller.save(ticket=ticket, save_as=save_as)
        except Exception as exc:
            self.complete_save(ticket.ticket_id, success=False, message=str(exc))
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if isinstance(result, DocumentActionResult):
            if result.status is DocumentActionStatus.PENDING:
                return result
            if result.status in {
                DocumentActionStatus.APPLIED,
                DocumentActionStatus.NO_OP,
            }:
                if ticket.is_pending:
                    self.complete_save(ticket.ticket_id, success=True)
                return result
            if ticket.is_pending:
                self.complete_save(
                    ticket.ticket_id,
                    success=False,
                    cancelled=result.status is DocumentActionStatus.REJECTED,
                    message=result.message,
                )
            return result
        if not ticket.is_pending:
            return self._result_for_ticket(ticket)
        if result is False:
            self.complete_save(ticket.ticket_id, success=False, cancelled=True)
            return DocumentActionResult(DocumentActionStatus.REJECTED, "save was cancelled")
        self.complete_save(ticket.ticket_id, success=True)
        return DocumentActionResult(DocumentActionStatus.APPLIED)

    def begin_save(self, document_id: str, *, save_as: bool = False) -> SaveTicket:
        document = self.require(document_id)
        active = self.active_save_ticket(document_id)
        if active is not None:
            raise RuntimeError(f"document already has a pending save: {document_id}")
        ticket = SaveTicket(
            ticket_id=uuid.uuid4().hex,
            document_id=document.document_id,
            captured_revision=document.revision,
            save_as=bool(save_as),
            operation_id=uuid.uuid4().hex,
        )
        self._save_tickets[ticket.ticket_id] = ticket
        self._active_save_by_document[document.document_id] = ticket.ticket_id
        document.state = DocumentState.SAVING
        self._touch()
        return ticket

    def active_save_ticket(self, document_id: str) -> Optional[SaveTicket]:
        ticket_id = self._active_save_by_document.get(str(document_id or ""))
        ticket = self._save_tickets.get(ticket_id or "")
        return ticket if ticket is not None and ticket.is_pending else None

    def get_save_ticket(self, ticket_id: str) -> Optional[SaveTicket]:
        return self._save_tickets.get(str(ticket_id or ""))

    def complete_save(
        self,
        ticket_id: str,
        *,
        success: bool,
        cancelled: bool = False,
        key: Optional[DocumentKey] = None,
        resource_path: Optional[str] = None,
        title: Optional[str] = None,
        message: str = "",
    ) -> SaveTicket:
        ticket = self._save_tickets.get(str(ticket_id or ""))
        if ticket is None:
            raise KeyError(f"unknown document save ticket: {ticket_id}")
        if not ticket.is_pending:
            return ticket
        document = self.get(ticket.document_id)
        if document is None:
            ticket.status = SaveTicketStatus.CANCELLED
            ticket.message = message or "document was closed"
            return ticket
        if success:
            try:
                if key is not None:
                    self.rekey(
                        document.document_id,
                        key,
                        resource_path=resource_path,
                    )
                    resource_path = None
                self.update_metadata(
                    document.document_id,
                    title=title,
                    resource_path=resource_path,
                )
            except Exception as exc:
                success = False
                message = str(exc)
        if success:
            document.saved_revision = max(
                document.saved_revision,
                min(ticket.captured_revision, document.revision),
            )
            document.state = DocumentState.READY
            ticket.status = SaveTicketStatus.SUCCEEDED
        else:
            document.state = DocumentState.READY
            ticket.status = (
                SaveTicketStatus.CANCELLED if cancelled else SaveTicketStatus.FAILED
            )
        ticket.message = str(message or "")
        if self._active_save_by_document.get(document.document_id) == ticket.ticket_id:
            del self._active_save_by_document[document.document_id]
        self._touch()
        return ticket

    @staticmethod
    def _result_for_ticket(ticket: SaveTicket) -> DocumentActionResult:
        if ticket.status is SaveTicketStatus.PENDING:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        if ticket.status is SaveTicketStatus.SUCCEEDED:
            return DocumentActionResult(DocumentActionStatus.APPLIED, ticket.message)
        if ticket.status is SaveTicketStatus.CANCELLED:
            return DocumentActionResult(DocumentActionStatus.REJECTED, ticket.message)
        return DocumentActionResult(DocumentActionStatus.FAILED, ticket.message)

    def request_discard(self, document_id: str) -> DocumentActionResult:
        document = self.require(document_id)
        if not document.capabilities & DocumentCapability.DISCARD:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "discard is not supported")
        controller = document.controller
        if controller is None:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "document has no controller")
        try:
            result = controller.discard()
        except Exception as exc:
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if not document.is_dirty:
            return DocumentActionResult(DocumentActionStatus.APPLIED)
        if result is False:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "discard was cancelled")
        return DocumentActionResult(DocumentActionStatus.REJECTED, "document remains dirty")

    def is_save_pending(self, document_id: str) -> bool:
        document = self.require(document_id)
        ticket = self.active_save_ticket(document_id)
        if ticket is None:
            return False
        poll = getattr(document.controller, "poll_save", None)
        if not callable(poll):
            return True
        try:
            outcome = poll(ticket)
        except Exception as exc:
            self.complete_save(ticket.ticket_id, success=False, message=str(exc))
            return False
        if outcome is None:
            return True
        self.complete_save(
            ticket.ticket_id,
            success=bool(outcome),
            cancelled=not bool(outcome),
        )
        return False

    def clear(self) -> None:
        if not self._documents and not self._view_documents:
            return
        for document in self._documents.values():
            document.state = DocumentState.CLOSED
            document.view_ids.clear()
        self._documents.clear()
        self._document_ids_by_key.clear()
        self._view_documents.clear()
        for ticket in self._save_tickets.values():
            if ticket.is_pending:
                ticket.status = SaveTicketStatus.CANCELLED
                ticket.message = "document registry was cleared"
        self._active_save_by_document.clear()
        self._touch()

    def _touch(self) -> None:
        self._revision += 1
