from .documents import DocumentActionResult

class FocusedSaveResult:
    result: DocumentActionResult
    target: str
    document_id: str
    panel_id: str
    dirty_before: bool
    @property
    def accepted(self) -> bool: ...

class EditorSaveService:
    def __init__(self, registry) -> None: ...
    @classmethod
    def instance(cls): ...
    def save_focused(self, *, save_as: bool = False) -> FocusedSaveResult: ...
    def shutdown(self) -> None: ...
