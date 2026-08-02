from typing import Optional
from .contexts import FocusService
from .action_journal import EditorActionJournal, EditorContextSnapshot
from .documents import DocumentRegistry
from .close_coordinator import CloseCoordinator
from .commands import EditorCommandRegistry
from .selection import SelectionService
from .shortcuts import ShortcutRouter

class EditorInteractionCore:
    selection: SelectionService
    focus: FocusService
    documents: DocumentRegistry
    close_coordinator: CloseCoordinator
    action_journal: EditorActionJournal
    commands: EditorCommandRegistry
    shortcuts: ShortcutRouter
    def __init__(self) -> None: ...
    @classmethod
    def instance(cls) -> Optional[EditorInteractionCore]: ...
    def shutdown(self) -> None: ...
    def capture_context(self) -> EditorContextSnapshot: ...
