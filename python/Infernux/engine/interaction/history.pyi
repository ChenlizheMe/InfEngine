from .action_journal import EditorActionJournal

class HistoryEntrySnapshot:
    sequence: int
    operation_id: str
    command_id: str
    description: str
    action_kind: str
    context: str
    target: str
    document: str
    origin: str
    state: str
    timestamp: float
    is_next_undo: bool
    is_next_redo: bool

class HistorySnapshot:
    revision: int
    cursor: int
    total: int
    entries: tuple[HistoryEntrySnapshot, ...]

class HistoryModel:
    def __init__(self, journal: EditorActionJournal) -> None: ...
    @property
    def query(self) -> str: ...
    def set_query(self, value: object) -> bool: ...
    @property
    def snapshot(self) -> HistorySnapshot: ...
