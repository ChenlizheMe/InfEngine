from typing import Callable, Iterable, Optional
from .action_journal import ActionOrigin
from .documents import DocumentRegistry
from .selection import SelectionService

class AssetMutationKind(str):
    CREATED: AssetMutationKind
    MODIFIED: AssetMutationKind
    MOVED: AssetMutationKind
    DELETED: AssetMutationKind

class AssetMutation:
    kind: AssetMutationKind
    source_path: str
    destination_path: str
    guid: str
    origin: ActionOrigin
    operation_id: str
    @property
    def path(self) -> str: ...
    @property
    def previous_path(self) -> str: ...

class AssetMutationChange:
    mutation: AssetMutation
    remapped_document_ids: tuple[str, ...]
    selection_changed: bool

class AssetContentChange:
    mutation: AssetMutation
    revision: int
    @property
    def mutations(self) -> tuple[AssetMutation, ...]: ...

class AssetRelocationPlan:
    mutations: tuple[AssetMutation, ...]
    operation_id: str
    origin: ActionOrigin
    def inverse(self, *, origin: Optional[ActionOrigin] = ...) -> AssetRelocationPlan: ...

class AssetRelocationChange:
    plan: AssetRelocationPlan
    changes: tuple[AssetMutationChange, ...]
    @property
    def operation_id(self) -> str: ...
    @property
    def mutations(self) -> tuple[AssetMutation, ...]: ...

AssetMutationNotification = AssetContentChange | AssetRelocationChange

def iter_asset_mutations(change: AssetMutationNotification | AssetMutationChange | AssetMutation) -> tuple[AssetMutation, ...]: ...

class AssetMutationService:
    def __init__(self, documents: DocumentRegistry, selection: SelectionService) -> None: ...
    @classmethod
    def instance(cls) -> Optional[AssetMutationService]: ...
    @property
    def revision(self) -> int: ...
    def add_listener(self, callback: Callable[[AssetMutationNotification], None]) -> None: ...
    def remove_listener(self, callback: Callable[[AssetMutationNotification], None]) -> None: ...
    def resolve_path_hint(self, guid: str, fallback: str = ...) -> str: ...
    def prepare_relocation(self, entries: Iterable[tuple[str, str, str]], *, origin: ActionOrigin = ..., operation_id: str = ...) -> AssetRelocationPlan: ...
    def abort_relocation(self, plan: AssetRelocationPlan) -> None: ...
    def commit_relocation(self, plan: AssetRelocationPlan) -> AssetRelocationChange: ...
    def publish_content_change(self, path: str, kind: AssetMutationKind, *, guid: str = ..., origin: ActionOrigin = ..., operation_id: str = ...) -> AssetContentChange: ...
    def publish_move(self, source_path: str, destination_path: str, *, guid: str = ..., origin: ActionOrigin = ..., operation_id: str = ...) -> AssetMutationChange: ...
    def shutdown(self) -> None: ...
