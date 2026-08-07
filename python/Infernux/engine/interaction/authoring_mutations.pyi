from collections.abc import Callable
from typing import Any, Optional

from .documents import DocumentRegistry

class AuthoringMutationService:
    def __init__(self, documents: DocumentRegistry) -> None: ...
    @classmethod
    def instance(cls) -> Optional[AuthoringMutationService]: ...
    @classmethod
    def require(cls) -> AuthoringMutationService: ...
    def apply(
        self,
        document_id: str,
        description: str,
        mutation: Callable[[], Any],
        *,
        view_id: str,
        merge_key: str = ...,
        before_selection: Any = ...,
        after_selection: Any = ...,
    ) -> bool: ...
    def can_record(self, *, require_edit_mode: bool = ...) -> bool: ...
    def execute_command(
        self,
        document_id: str,
        command_factory: Callable[[int, int], Any],
        *,
        view_id: str,
        before_selection: Any = ...,
        after_selection: Any = ...,
        require_edit_mode: bool = ...,
    ) -> bool: ...
    def record_applied_command(
        self,
        document_id: str,
        command: Any,
        *,
        view_id: str,
        before_revision: int,
        after_revision: int,
        rollback: Callable[[], Any],
        require_edit_mode: bool = ...,
    ) -> bool: ...
    def shutdown(self) -> None: ...
