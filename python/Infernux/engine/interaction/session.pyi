from typing import Optional
from .contexts import FocusService
from .action_journal import EditorActionJournal, EditorContextSnapshot
from .documents import DocumentRegistry
from .close_coordinator import CloseCoordinator
from .selection import SelectionService

class EditorInteractionCore:
    selection: SelectionService
    focus: FocusService
    documents: DocumentRegistry
    close_coordinator: CloseCoordinator
    action_journal: EditorActionJournal
    def __init__(self) -> None: ...
    @classmethod
    def instance(cls) -> Optional[EditorInteractionCore]: ...
    def shutdown(self) -> None: ...
    def capture_context(self) -> EditorContextSnapshot: ...
