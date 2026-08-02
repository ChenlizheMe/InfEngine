"""Stable editor documents, revisions, views, and authoring operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntFlag, auto
from typing import Any, Optional, Protocol
import uuid


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


class DocumentController(Protocol):
    def save(self, *, save_as: bool = False) -> Any: ...

    def discard(self) -> Any: ...

    def is_save_pending(self) -> bool: ...


@dataclass(slots=True)
class EditorDocument:
    document_id: str
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
        self._view_documents: dict[str, str] = {}
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
        current_revision = max(0, int(revision))
        clean_revision = (
            current_revision if saved_revision is None else max(0, int(saved_revision))
        )
        if clean_revision > current_revision:
            raise ValueError("saved_revision cannot exceed revision")
        document = EditorDocument(
            document_id=identifier,
            kind=DocumentKind(kind),
            title=str(title or identifier),
            resource_path=str(resource_path or ""),
            revision=current_revision,
            saved_revision=clean_revision,
            capabilities=DocumentCapability(capabilities),
            controller=controller,
        )
        self._documents[identifier] = document
        self._touch()
        return document

    def get(self, document_id: str) -> Optional[EditorDocument]:
        return self._documents.get(str(document_id or ""))

    def require(self, document_id: str) -> EditorDocument:
        document = self.get(document_id)
        if document is None:
            raise KeyError(f"unknown editor document: {document_id}")
        return document

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
        if document.state is not DocumentState.CONFLICT:
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
        controller = document.controller
        if controller is None:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "document has no controller")
        previous_saved_revision = document.saved_revision
        try:
            result = controller.save(save_as=save_as)
        except Exception as exc:
            return DocumentActionResult(DocumentActionStatus.FAILED, str(exc))
        if not document.is_dirty:
            return DocumentActionResult(DocumentActionStatus.APPLIED)
        if document.saved_revision != previous_saved_revision:
            # A save may commit the captured revision while edits made during
            # the write keep the live document dirty. The operation still
            # succeeded and must not be presented as a cancelled save.
            return DocumentActionResult(DocumentActionStatus.APPLIED)
        if self.is_save_pending(document_id):
            document.state = DocumentState.SAVING
            self._touch()
            return DocumentActionResult(DocumentActionStatus.PENDING)
        if result is False:
            return DocumentActionResult(DocumentActionStatus.REJECTED, "save was cancelled")
        return DocumentActionResult(DocumentActionStatus.REJECTED, "document remains dirty")

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
        controller = document.controller
        if controller is None:
            return False
        try:
            pending = bool(controller.is_save_pending())
        except Exception:
            pending = False
        if not pending and document.state is DocumentState.SAVING:
            document.state = DocumentState.READY
            self._touch()
        return pending

    def clear(self) -> None:
        if not self._documents and not self._view_documents:
            return
        for document in self._documents.values():
            document.state = DocumentState.CLOSED
            document.view_ids.clear()
        self._documents.clear()
        self._view_documents.clear()
        self._touch()

    def _touch(self) -> None:
        self._revision += 1
