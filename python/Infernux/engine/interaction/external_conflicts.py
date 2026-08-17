"""Editor-wide external document conflict state and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import uuid

from .documents import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentRegistry,
    DocumentKind,
    DocumentState,
)


@dataclass(frozen=True, slots=True)
class ExternalDocumentConflict:
    conflict_id: str
    document_id: str
    external_revision: int
    resource_path: str
    title: str


class ExternalDocumentConflictService:
    """Serialize conflict resolution without depending on an ImGui presenter."""

    def __init__(self, registry: DocumentRegistry) -> None:
        self._registry = registry
        self._observed_registry_revision = -1
        self._queue: list[ExternalDocumentConflict] = []
        self._active: Optional[ExternalDocumentConflict] = None
        self._waiting_for_save_copy = False
        self._waiting_for_reload = False
        self._error = ""

    @property
    def active(self) -> Optional[ExternalDocumentConflict]:
        return self._active

    @property
    def active_document(self):
        return (
            self._registry.get(self._active.document_id)
            if self._active is not None
            else None
        )

    @property
    def is_active(self) -> bool:
        return self._active is not None

    @property
    def waiting_for_save_copy(self) -> bool:
        return self._waiting_for_save_copy

    @property
    def waiting_for_reload(self) -> bool:
        return self._waiting_for_reload

    @property
    def error(self) -> str:
        return self._error

    def poll(self) -> None:
        if self._observed_registry_revision != self._registry.revision:
            self._observed_registry_revision = self._registry.revision
            known = {
                conflict.document_id
                for conflict in self._queue
            }
            if self._active is not None:
                known.add(self._active.document_id)
            for document in self._registry.documents:
                if (
                    document.state is DocumentState.CONFLICT
                    and document.kind is DocumentKind.SCENE
                    and document.document_id not in known
                ):
                    self._queue.append(self._snapshot(document))
                    known.add(document.document_id)
            self._queue = [
                conflict
                for conflict in self._queue
                if self._matches_live_conflict(conflict)
            ]

        if self._active is not None:
            document = self._registry.get(self._active.document_id)
            if self._waiting_for_reload:
                result = self._registry.take_external_reload_result(
                    self._active.document_id
                )
                if result is None:
                    if document is None or document.state is not DocumentState.CONFLICT:
                        self._finish_active()
                    return
                self._waiting_for_reload = False
                if result.accepted:
                    self._finish_active()
                    return
                self._error = result.message
                return
            if self._waiting_for_save_copy:
                if document is not None and self._registry.is_save_pending(
                    document.document_id
                ):
                    return
                self._waiting_for_save_copy = False
                if document is None or document.state is not DocumentState.CONFLICT:
                    self._finish_active()
                    return
                self._error = "save_copy_failed"
                return
            if document is None or document.state is not DocumentState.CONFLICT:
                self._finish_active()
                return
            if document.external_revision != self._active.external_revision:
                self._active = self._snapshot(document)
            return

        while self._queue:
            conflict = self._queue.pop(0)
            if self._matches_live_conflict(conflict):
                self._active = conflict
                self._error = ""
                return

    def reload(self, conflict_id: str) -> DocumentActionResult:
        conflict = self._require_active(conflict_id)
        if conflict is None:
            return self._stale_result()
        result = self._registry.request_reload_external(conflict.document_id)
        if result.status is DocumentActionStatus.PENDING:
            self._waiting_for_reload = True
            self._error = ""
        elif result.accepted:
            self._finish_active()
        else:
            self._error = result.message
        return result

    def keep_local(self, conflict_id: str) -> DocumentActionResult:
        conflict = self._require_active(conflict_id)
        if conflict is None:
            return self._stale_result()
        result = self._registry.resolve_conflict_keep_local(conflict.document_id)
        if result.accepted:
            self._finish_active()
        else:
            self._error = result.message
        return result

    def save_copy(self, conflict_id: str) -> DocumentActionResult:
        conflict = self._require_active(conflict_id)
        if conflict is None:
            return self._stale_result()
        result = self._registry.request_save(conflict.document_id, save_as=True)
        if result.status is DocumentActionStatus.PENDING:
            self._waiting_for_save_copy = True
            self._error = ""
        elif result.accepted:
            self._finish_active()
        else:
            self._error = result.message
        return result

    def clear(self) -> None:
        self._queue.clear()
        self._active = None
        self._waiting_for_save_copy = False
        self._waiting_for_reload = False
        self._error = ""
        self._observed_registry_revision = -1

    def _snapshot(self, document) -> ExternalDocumentConflict:
        return ExternalDocumentConflict(
            uuid.uuid4().hex,
            document.document_id,
            document.external_revision,
            document.resource_path,
            document.title,
        )

    def _matches_live_conflict(self, conflict: ExternalDocumentConflict) -> bool:
        document = self._registry.get(conflict.document_id)
        return bool(
            document is not None
            and document.kind is DocumentKind.SCENE
            and document.state is DocumentState.CONFLICT
        )

    def _require_active(
        self,
        conflict_id: str,
    ) -> Optional[ExternalDocumentConflict]:
        conflict = self._active
        if conflict is None or conflict.conflict_id != str(conflict_id or ""):
            return None
        document = self._registry.get(conflict.document_id)
        if (
            document is None
            or document.state is not DocumentState.CONFLICT
            or document.external_revision != conflict.external_revision
        ):
            return None
        return conflict

    @staticmethod
    def _stale_result() -> DocumentActionResult:
        return DocumentActionResult(
            DocumentActionStatus.REJECTED,
            "the external conflict changed before it was resolved",
        )

    def _finish_active(self) -> None:
        self._active = None
        self._waiting_for_save_copy = False
        self._waiting_for_reload = False
        self._error = ""


__all__ = ["ExternalDocumentConflict", "ExternalDocumentConflictService"]
