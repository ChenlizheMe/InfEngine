"""Thin Python access to the native cross-language DocumentStore."""
from __future__ import annotations

from typing import Optional

from Infernux.lib import (
    DocumentWriteCancelled,
    DocumentWriteOptions,
    DocumentWriteSuperseded,
    DocumentWriteTicket,
    NativeDocumentStore,
)


class DocumentStore:
    """Access the C++-owned generation/coalescing write service."""

    @classmethod
    def instance(cls) -> NativeDocumentStore:
        return NativeDocumentStore.instance()

    @classmethod
    def shutdown(cls) -> None:
        NativeDocumentStore.instance().shutdown()

    @classmethod
    def flush(cls, path: Optional[str] = None) -> None:
        store = NativeDocumentStore.instance()
        if path is None:
            store.flush_all()
        else:
            store.flush_path(path)


def write_document_text(path: str, content: str, *, create_backup: bool = False) -> int:
    """Write one UTF-8 document and return its path generation."""
    options = DocumentWriteOptions()
    options.create_backup = create_backup
    return NativeDocumentStore.instance().write_and_wait(path, content, options)


def submit_document_text(path: str, content: str, *, create_backup: bool = False) -> DocumentWriteTicket:
    """Queue one UTF-8 document write without blocking the caller.

    The native store coalesces newer generations for the same path and writes
    them atomically on its IO workers.  Callers that need durability before
    shutdown should use :meth:`DocumentStore.flush`.
    """
    options = DocumentWriteOptions()
    options.create_backup = create_backup
    return NativeDocumentStore.instance().submit(path, content, options)


__all__ = [
    "DocumentStore",
    "DocumentWriteCancelled",
    "DocumentWriteOptions",
    "DocumentWriteSuperseded",
    "DocumentWriteTicket",
    "submit_document_text",
    "write_document_text",
]
