from typing import Any, Callable, Mapping, Optional
from enum import Enum
from .contexts import FocusService, FocusSnapshot
from .descriptors import SelectionSnapshot
from .selection import SelectionService

class CommandSource(str, Enum):
    MENU: CommandSource
    SHORTCUT: CommandSource
    TOOLBAR: CommandSource
    CONTEXT_MENU: CommandSource
    PALETTE: CommandSource
    AUTOMATION: CommandSource
    API: CommandSource

class CommandStatus(str, Enum):
    EXECUTED: CommandStatus
    NO_OP: CommandStatus
    DISABLED: CommandStatus
    NOT_FOUND: CommandStatus
    FAILED: CommandStatus

class CommandContext:
    source: CommandSource
    focus: FocusSnapshot
    selection: SelectionSnapshot
    input_context_ids: tuple[str, ...]
    payload: Mapping[str, Any]

class CommandResult:
    command_id: str
    status: CommandStatus
    message: str
    value: Any
    @property
    def accepted(self) -> bool: ...

class EditorCommand:
    command_id: str
    execute: Callable[[CommandContext], Any]
    display_name: str
    category: str
    can_execute: Optional[Callable[[CommandContext], bool]]
    is_checked: Optional[Callable[[CommandContext], bool]]
    default_shortcut: str
    def __init__(self, command_id: str, execute: Callable[[CommandContext], Any], display_name: str = "", category: str = "", can_execute: Optional[Callable[[CommandContext], bool]] = None, is_checked: Optional[Callable[[CommandContext], bool]] = None, default_shortcut: str = "") -> None: ...

class EditorCommandRegistry:
    def __init__(self, *, focus: Optional[FocusService] = None, selection: Optional[SelectionService] = None) -> None: ...
    @classmethod
    def instance(cls) -> EditorCommandRegistry: ...
    @property
    def revision(self) -> int: ...
    @property
    def commands(self) -> tuple[EditorCommand, ...]: ...
    def register(self, command: EditorCommand, *, replace: bool = False) -> None: ...
    def unregister(self, command_id: str) -> bool: ...
    def get(self, command_id: str) -> Optional[EditorCommand]: ...
    def context(self, source: CommandSource = CommandSource.API, payload: Optional[Mapping[str, Any]] = None) -> CommandContext: ...
    def can_execute(self, command_id: str, context: Optional[CommandContext] = None) -> bool: ...
    def is_checked(self, command_id: str, context: Optional[CommandContext] = None) -> bool: ...
    def execute(self, command_id: str, *, source: CommandSource = CommandSource.API, payload: Optional[Mapping[str, Any]] = None) -> CommandResult: ...
    def clear(self) -> None: ...
