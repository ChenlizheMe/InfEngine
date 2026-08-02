from typing import Optional
from .contexts import FocusService
from .action_journal import EditorActionJournal, EditorContextSnapshot
from .selection import SelectionService

class EditorInteractionCore:
    selection: SelectionService
    focus: FocusService
    action_journal: EditorActionJournal
    def __init__(self) -> None: ...
    @classmethod
    def instance(cls) -> Optional[EditorInteractionCore]: ...
    def shutdown(self) -> None: ...
    def capture_context(self) -> EditorContextSnapshot: ...
