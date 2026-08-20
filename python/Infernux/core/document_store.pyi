from typing import Optional

from Infernux.lib import (
    DocumentFileState,
    DocumentWriteCancelled,
    DocumentWriteOptions,
    DocumentWriteSuperseded,
    DocumentWriteTicket,
    NativeDocumentStore,
)


class DocumentStore:
    @classmethod
    def instance(cls) -> NativeDocumentStore: ...
    @classmethod
    def shutdown(cls) -> None: ...
    @classmethod
    def flush(cls, path: Optional[str] = ...) -> None: ...


def capture_document_file_state(path: str) -> DocumentFileState: ...
def write_document_text(path: str, content: str, *, create_backup: bool = ..., expected_file_state: DocumentFileState | None = ..., commit_chain_token: str = ..., chain_id: str = ...) -> int: ...
def submit_document_text(path: str, content: str, *, create_backup: bool = ..., expected_file_state: DocumentFileState | None = ..., commit_chain_token: str = ..., chain_id: str = ...) -> DocumentWriteTicket: ...


__all__: list[str]
