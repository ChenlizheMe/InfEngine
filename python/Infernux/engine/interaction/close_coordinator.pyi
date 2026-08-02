from typing import Callable, Iterable, Optional
from enum import Enum
from .documents import DocumentRegistry, EditorDocument

class CloseIntentKind(str, Enum):
    CLOSE_VIEW: CloseIntentKind
    REPLACE_DOCUMENT: CloseIntentKind
    CLOSE_PROJECT: CloseIntentKind
    EXIT_EDITOR: CloseIntentKind
    RESET_LAYOUT: CloseIntentKind

class CloseState(str, Enum):
    IDLE: CloseState
    AWAITING_DECISION: CloseState
    WAITING_FOR_SAVE: CloseState

class CloseIssue(str, Enum):
    NONE: CloseIssue
    SAVE_NOT_SUPPORTED: CloseIssue
    SAVE_CANCELLED: CloseIssue
    SAVE_FAILED: CloseIssue
    DISCARD_NOT_SUPPORTED: CloseIssue
    DISCARD_FAILED: CloseIssue
    STILL_DIRTY: CloseIssue

class CloseIntent:
    kind: CloseIntentKind
    view_id: str
    document_ids: tuple[str, ...]
    intent_id: str
    def __init__(self, kind: CloseIntentKind, view_id: str = "", document_ids: tuple[str, ...] = (), intent_id: str = "") -> None: ...

class CloseCoordinator:
    def __init__(self, registry: Optional[DocumentRegistry] = None) -> None: ...
    @property
    def registry(self) -> DocumentRegistry: ...
    @property
    def intent(self) -> Optional[CloseIntent]: ...
    @property
    def state(self) -> CloseState: ...
    @property
    def issue(self) -> CloseIssue: ...
    @property
    def message(self) -> str: ...
    @property
    def is_active(self) -> bool: ...
    @property
    def active_document(self) -> Optional[EditorDocument]: ...
    def request(self, intent: CloseIntent, on_complete: Callable[[], None], on_cancel: Optional[Callable[[], None]] = None) -> bool: ...
    def decide_save(self) -> None: ...
    def decide_discard(self) -> None: ...
    def poll(self) -> None: ...
    def cancel(self) -> None: ...
