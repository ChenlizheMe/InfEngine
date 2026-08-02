from typing import Optional
from .contexts import FocusService
from .selection import SelectionService

class EditorInteractionCore:
    selection: SelectionService
    focus: FocusService
    def __init__(self) -> None: ...
    @classmethod
    def instance(cls) -> Optional[EditorInteractionCore]: ...
    def shutdown(self) -> None: ...
