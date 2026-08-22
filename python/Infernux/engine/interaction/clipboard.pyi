from enum import Enum
from typing import Callable, Iterable, Optional

class ClipboardDomain(str, Enum):
    SCENE_OBJECT: ClipboardDomain
    ASSET: ClipboardDomain
    COMPONENT: ClipboardDomain
    GRAPH_ELEMENT: ClipboardDomain
    TIMELINE_ELEMENT: ClipboardDomain
    UI_ELEMENT: ClipboardDomain

class ClipboardOperation(str, Enum):
    COPY: ClipboardOperation
    CUT: ClipboardOperation

class ClipboardItem:
    target_id: str
    document_id: str
    sub_kind: str
    data: object
    def __init__(self, target_id: str, document_id: str = "", sub_kind: str = "", data: object = None) -> None: ...

class ClipboardPayload:
    domain: ClipboardDomain
    operation: ClipboardOperation
    items: tuple[ClipboardItem, ...]
    source_owner_id: str
    revision: int
    def __init__(self, domain: ClipboardDomain, operation: ClipboardOperation, items: tuple[ClipboardItem, ...], source_owner_id: str = "", revision: int = 0) -> None: ...

class ClipboardChange:
    before: Optional[ClipboardPayload]
    after: Optional[ClipboardPayload]
    reason: str

class ClipboardService:
    def __init__(self) -> None: ...
    @classmethod
    def instance(cls) -> ClipboardService: ...
    @classmethod
    def install(cls, service: ClipboardService) -> None: ...
    @property
    def revision(self) -> int: ...
    def add_listener(self, callback: Callable[[ClipboardChange], None]) -> None: ...
    def remove_listener(self, callback: Callable[[ClipboardChange], None]) -> None: ...
    def peek(self, domain: Optional[ClipboardDomain] = None) -> Optional[ClipboardPayload]: ...
    def has_payload(self, domain: Optional[ClipboardDomain] = None) -> bool: ...
    def publish(self, payload: ClipboardPayload, *, reason: str = "copy") -> ClipboardPayload: ...
    def write(self, domain: ClipboardDomain, items: Iterable[ClipboardItem], *, operation: ClipboardOperation = ClipboardOperation.COPY, source_owner_id: str = "", reason: str = "copy") -> ClipboardPayload: ...
    def clear(self, *, expected_revision: Optional[int] = None, reason: str = "clear") -> bool: ...
    def consume_cut(self, revision: int) -> bool: ...
