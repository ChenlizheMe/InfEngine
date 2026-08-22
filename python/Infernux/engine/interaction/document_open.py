"""Idempotent document restoration for cross-panel editor history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from .documents import DocumentKind, DocumentLocator, DocumentRegistry, EditorDocument


class DocumentOpenStatus(str, Enum):
    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DocumentOpenResult:
    status: DocumentOpenStatus
    document: Optional[EditorDocument] = None
    message: str = ""


class DocumentOpenAdapter(Protocol):
    """Domain adapter invoked repeatedly until a locator resolves or fails."""

    def __call__(self, locator: DocumentLocator) -> object: ...


class DocumentOpenService:
    """Resolve live documents and delegate dormant resources by kind.

    Adapters must be idempotent. A pending adapter can be polled on later
    frames without issuing the same destructive open operation twice.
    """

    def __init__(self, registry: Optional[DocumentRegistry] = None) -> None:
        self._registry = registry
        self._adapters: dict[DocumentKind, DocumentOpenAdapter] = {}

    @property
    def registry(self) -> DocumentRegistry:
        return self._registry or DocumentRegistry.instance()

    def register(
        self,
        kind: DocumentKind,
        adapter: DocumentOpenAdapter,
        *,
        replace: bool = False,
    ) -> None:
        document_kind = DocumentKind(kind)
        if not callable(adapter):
            raise TypeError("document open adapter must be callable")
        if document_kind in self._adapters and not replace:
            raise ValueError(
                f"document open adapter already registered: {document_kind.value}"
            )
        self._adapters[document_kind] = adapter

    def unregister(self, kind: DocumentKind) -> bool:
        return self._adapters.pop(DocumentKind(kind), None) is not None

    def resolve_or_open(self, locator: Optional[DocumentLocator]) -> DocumentOpenResult:
        if locator is None:
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message="document locator is missing",
            )
        document = self.registry.resolve_locator(locator)
        if document is not None:
            return DocumentOpenResult(DocumentOpenStatus.READY, document)

        adapter = self._adapters.get(locator.key_hint.kind)
        if adapter is None:
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message=(
                    "document restore is not supported for "
                    f"{locator.key_hint.kind.value}"
                ),
            )
        try:
            raw_result = adapter(locator)
        except Exception as exc:
            return DocumentOpenResult(DocumentOpenStatus.FAILED, message=str(exc))

        result = self._normalize_result(raw_result)
        document = self.registry.resolve_locator(locator)
        if document is not None:
            return DocumentOpenResult(DocumentOpenStatus.READY, document)
        if result.status is DocumentOpenStatus.READY:
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message=result.message or "adapter completed without registering the document",
            )
        return result

    def open_resource(
        self,
        kind: DocumentKind,
        resource_path: str,
        *,
        guid: str = "",
        title: str = "",
    ) -> DocumentOpenResult:
        """Resolve or open one asset through its registered domain adapter."""
        locator = self.registry.locate_resource(
            kind,
            resource_path,
            guid=guid,
            title=title,
        )
        document = self.registry.resolve_locator(locator)
        if document is not None:
            adapter = self._adapters.get(locator.key_hint.kind)
            if adapter is not None:
                try:
                    result = self._normalize_result(adapter(locator))
                except Exception as exc:
                    return DocumentOpenResult(
                        DocumentOpenStatus.FAILED,
                        message=str(exc),
                    )
                if result.status is DocumentOpenStatus.FAILED:
                    return result
            return DocumentOpenResult(DocumentOpenStatus.READY, document)
        return self.resolve_or_open(locator)

    def clear(self) -> None:
        self._adapters.clear()

    @staticmethod
    def _normalize_result(value: object) -> DocumentOpenResult:
        if isinstance(value, DocumentOpenResult):
            return value
        if isinstance(value, DocumentOpenStatus):
            return DocumentOpenResult(value)
        if value is None:
            return DocumentOpenResult(DocumentOpenStatus.PENDING)
        if isinstance(value, bool):
            return DocumentOpenResult(
                DocumentOpenStatus.READY if value else DocumentOpenStatus.FAILED
            )
        raise TypeError(
            "document open adapter must return DocumentOpenResult, "
            "DocumentOpenStatus, bool, or None"
        )
