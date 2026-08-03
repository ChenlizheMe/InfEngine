"""Single registry for editor user intents, independent from presentation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from Infernux.debug import Debug

from .contexts import FocusService, FocusSnapshot
from .descriptors import SelectionSnapshot
from .selection import SelectionService


class CommandSource(str, Enum):
    MENU = "menu"
    SHORTCUT = "shortcut"
    TOOLBAR = "toolbar"
    CONTEXT_MENU = "context_menu"
    DRAG_DROP = "drag_drop"
    PALETTE = "palette"
    AUTOMATION = "automation"
    API = "api"


class CommandStatus(str, Enum):
    EXECUTED = "executed"
    NO_OP = "no_op"
    DISABLED = "disabled"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommandContext:
    source: CommandSource
    focus: FocusSnapshot
    selection: SelectionSnapshot
    input_context_ids: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    status: CommandStatus
    message: str = ""
    value: Any = None

    @property
    def accepted(self) -> bool:
        return self.status in {CommandStatus.EXECUTED, CommandStatus.NO_OP}


CommandHandler = Callable[[CommandContext], Any]
CommandPredicate = Callable[[CommandContext], bool]


@dataclass(slots=True)
class EditorCommand:
    command_id: str
    execute: CommandHandler
    display_name: str = ""
    category: str = ""
    can_execute: Optional[CommandPredicate] = None
    is_checked: Optional[CommandPredicate] = None
    default_shortcut: str = ""

    def __post_init__(self) -> None:
        self.command_id = str(self.command_id or "").strip()
        if not self.command_id:
            raise ValueError("editor command_id must not be empty")
        if not callable(self.execute):
            raise TypeError("editor command execute handler must be callable")


class EditorCommandRegistry:
    """Authoritative command metadata, enablement, and execution service."""

    _instance: Optional["EditorCommandRegistry"] = None

    def __init__(
        self,
        *,
        focus: Optional[FocusService] = None,
        selection: Optional[SelectionService] = None,
    ) -> None:
        self._commands: dict[str, EditorCommand] = {}
        self._focus = focus
        self._selection = selection
        self._revision = 0
        EditorCommandRegistry._instance = self

    @classmethod
    def instance(cls) -> "EditorCommandRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def commands(self) -> tuple[EditorCommand, ...]:
        return tuple(self._commands.values())

    def register(self, command: EditorCommand, *, replace: bool = False) -> None:
        existing = self._commands.get(command.command_id)
        if existing is not None and not replace:
            raise ValueError(f"editor command already registered: {command.command_id}")
        self._commands[command.command_id] = command
        self._revision += 1

    def unregister(self, command_id: str) -> bool:
        if self._commands.pop(str(command_id or "").strip(), None) is None:
            return False
        self._revision += 1
        return True

    def get(self, command_id: str) -> Optional[EditorCommand]:
        return self._commands.get(str(command_id or "").strip())

    def context(
        self,
        source: CommandSource = CommandSource.API,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> CommandContext:
        focus = self._focus or FocusService.instance()
        selection = self._selection or SelectionService.instance()
        return CommandContext(
            CommandSource(source),
            focus.snapshot,
            selection.snapshot,
            tuple(context.context_id for context in focus.input_contexts.ordered()),
            dict(payload or {}),
        )

    def can_execute(
        self,
        command_id: str,
        context: Optional[CommandContext] = None,
    ) -> bool:
        command = self.get(command_id)
        if command is None:
            return False
        if command.can_execute is None:
            return True
        try:
            return bool(command.can_execute(context or self.context()))
        except Exception as exc:
            Debug.log_suppressed(f"EditorCommand.can_execute[{command.command_id}]", exc)
            return False

    def is_checked(
        self,
        command_id: str,
        context: Optional[CommandContext] = None,
    ) -> bool:
        command = self.get(command_id)
        if command is None or command.is_checked is None:
            return False
        try:
            return bool(command.is_checked(context or self.context()))
        except Exception as exc:
            Debug.log_suppressed(f"EditorCommand.is_checked[{command.command_id}]", exc)
            return False

    def execute(
        self,
        command_id: str,
        *,
        source: CommandSource = CommandSource.API,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> CommandResult:
        identifier = str(command_id or "").strip()
        command = self.get(identifier)
        if command is None:
            return CommandResult(identifier, CommandStatus.NOT_FOUND, "command is not registered")
        context = self.context(source, payload)
        if not self.can_execute(identifier, context):
            return CommandResult(identifier, CommandStatus.DISABLED)
        try:
            value = command.execute(context)
        except Exception as exc:
            Debug.log_suppressed(f"EditorCommand.execute[{identifier}]", exc)
            return CommandResult(identifier, CommandStatus.FAILED, str(exc))
        if isinstance(value, CommandResult):
            if value.command_id and value.command_id != identifier:
                return CommandResult(
                    identifier,
                    CommandStatus.FAILED,
                    "command handler returned a result for another command",
                )
            return CommandResult(identifier, value.status, value.message, value.value)
        if value is False:
            return CommandResult(identifier, CommandStatus.NO_OP, value=value)
        return CommandResult(identifier, CommandStatus.EXECUTED, value=value)

    def clear(self) -> None:
        if not self._commands:
            return
        self._commands.clear()
        self._revision += 1
