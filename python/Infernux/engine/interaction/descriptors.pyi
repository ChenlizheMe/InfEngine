from enum import Enum
from typing import Iterable, Optional

class SelectionDomain(str, Enum):
    SCENE_OBJECT: SelectionDomain
    ASSET: SelectionDomain
    COMPONENT: SelectionDomain
    GRAPH_ELEMENT: SelectionDomain
    TIMELINE_ELEMENT: SelectionDomain
    UI_ELEMENT: SelectionDomain

class SelectionTarget:
    domain: SelectionDomain
    target_id: str
    document_id: str
    sub_kind: str
    def __init__(self, domain: SelectionDomain, target_id: str, document_id: str = "", sub_kind: str = "") -> None: ...
    @classmethod
    def scene_object(cls, object_id: int) -> SelectionTarget: ...
    @classmethod
    def asset(cls, path: str) -> SelectionTarget: ...
    def scene_object_id(self) -> int: ...

class SelectionSnapshot:
    owner_id: str
    targets: tuple[SelectionTarget, ...]
    primary_index: int
    anchor_index: int
    def __init__(self, owner_id: str = "", targets: tuple[SelectionTarget, ...] = (), primary_index: int = -1, anchor_index: int = -1) -> None: ...
    @classmethod
    def create(cls, targets: Iterable[SelectionTarget], *, owner_id: str, primary: Optional[SelectionTarget] = None, anchor: Optional[SelectionTarget] = None) -> SelectionSnapshot: ...
    @property
    def primary(self) -> Optional[SelectionTarget]: ...
    @property
    def anchor(self) -> Optional[SelectionTarget]: ...
    @property
    def domain(self) -> Optional[SelectionDomain]: ...
    @property
    def is_empty(self) -> bool: ...
