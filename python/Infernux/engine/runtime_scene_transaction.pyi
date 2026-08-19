"""Runtime-safe owner-thread transactions for complete Scene documents."""

from __future__ import annotations

from enum import Enum
from os import PathLike
from typing import Any, Callable, Optional


class SceneDocumentTransactionError(RuntimeError): ...


class SceneDocumentTransactionState(Enum):
    CREATED: str
    READING: str
    DOCUMENT_READY: str
    RESOURCE_PREFLIGHTING: str
    RESOURCES_READY: str
    PREFLIGHTING: str
    READY_TO_COMMIT: str
    COMMITTING: str
    ROLLING_BACK: str
    COMPLETED: str
    FAILED: str
    CANCELLED: str


class SceneDocumentTransaction:
    def __init__(
        self,
        scene: Any,
        *,
        path: Optional[PathLike[str] | str] = None,
        document: Any = None,
        asset_database: Any = None,
        clear_registries: bool = True,
        borrow_document: bool = False,
        prefer_loaded_types: bool = False,
        before_commit: Optional[Callable[[], None]] = None,
        after_publish: Optional[Callable[[], None]] = None,
    ) -> None: ...
    @property
    def state(self) -> SceneDocumentTransactionState: ...
    @property
    def status(self) -> str: ...
    @property
    def error(self) -> str: ...
    @property
    def failure_exception(self) -> Optional[BaseException]: ...
    @property
    def is_complete(self) -> bool: ...
    @property
    def succeeded(self) -> bool: ...
    @property
    def ran_on_worker(self) -> bool: ...
    @property
    def rolled_back(self) -> bool: ...
    @property
    def rollback_error(self) -> str: ...
    @property
    def document(self) -> Any: ...
    def start(self) -> SceneDocumentTransaction: ...
    def poll(self) -> bool: ...
    def cancel(self) -> bool: ...
    def run_to_completion(
        self, *, raise_on_failure: bool = True, on_tick: Any = ...
    ) -> bool: ...
