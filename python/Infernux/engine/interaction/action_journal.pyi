from enum import Enum
from typing import Any, ContextManager, Optional
from .contexts import FocusSnapshot
from .descriptors import SelectionSnapshot
from .documents import DocumentLocator
from .windows import WindowLocator

class ActionOrigin(str, Enum):
    USER: ActionOrigin
    AUTOMATION: ActionOrigin
    SYSTEM: ActionOrigin
    EXTERNAL: ActionOrigin

def current_action_origin() -> ActionOrigin: ...
def action_origin_scope(origin: ActionOrigin) -> ContextManager[None]: ...

class ContextRestoreStatus(str, Enum):
    READY: ContextRestoreStatus
    PENDING: ContextRestoreStatus
    FAILED: ContextRestoreStatus

class EditorContextSnapshot:
    focus: FocusSnapshot
    selection: SelectionSnapshot
    document: Optional[DocumentLocator]
    window: Optional[WindowLocator]
    scene: Optional[DocumentLocator]
    def __init__(self, focus: FocusSnapshot = ..., selection: SelectionSnapshot = ..., document: Optional[DocumentLocator] = None, window: Optional[WindowLocator] = None, scene: Optional[DocumentLocator] = None) -> None: ...
    def with_selection(self, selection: SelectionSnapshot) -> EditorContextSnapshot: ...

class JournalEntry:
    action: Any
    before_context: Optional[EditorContextSnapshot]
    after_context: Optional[EditorContextSnapshot]
    origin: ActionOrigin
    operation_id: str
    transaction_id: str
    command_id: str
    timestamp: float
    revision: int

class JournalPushResult:
    recorded: bool
    merged: bool
    dropped: tuple[JournalEntry, ...]
    discarded_redo: tuple[JournalEntry, ...]

class EditorActionJournal:
    max_entries: int
    def __init__(self, max_entries: int = 200) -> None: ...
    @property
    def revision(self) -> int: ...
    @property
    def entries(self) -> tuple[JournalEntry, ...]: ...
    @property
    def cursor(self) -> int: ...
    @property
    def can_undo(self) -> bool: ...
    @property
    def can_redo(self) -> bool: ...
    def applied_entries(self) -> tuple[JournalEntry, ...]: ...
    def redo_entries(self) -> tuple[JournalEntry, ...]: ...
    def record(self, action: Any, *, before_context: Optional[EditorContextSnapshot] = None, after_context: Optional[EditorContextSnapshot] = None, origin: ActionOrigin = ActionOrigin.USER, transaction_id: str = "", operation_id: str = "", command_id: str = "") -> JournalPushResult: ...
    def peek_undo(self) -> Optional[JournalEntry]: ...
    def peek_redo(self) -> Optional[JournalEntry]: ...
    def commit_undo(self, entry: JournalEntry) -> None: ...
    def commit_redo(self, entry: JournalEntry) -> None: ...
    def operation_entries(self, operation_id: str) -> tuple[JournalEntry, ...]: ...
    def replace_operation(self, operation_id: str, action: Any, *, before_context: Optional[EditorContextSnapshot], after_context: Optional[EditorContextSnapshot], origin: ActionOrigin, transaction_id: str = "", command_id: str = "") -> JournalEntry: ...
    def clear(self) -> None: ...
