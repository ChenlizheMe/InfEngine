"""Searchable presentation model over the global editor command registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from .commands import (
    CommandContext,
    CommandResult,
    CommandSource,
    CommandStatus,
    EditorCommand,
    EditorCommandRegistry,
)
from .contexts import FocusService, InputContext
from .modals import ModalService
from .search import SearchQueryModel, normalize_search_text
from .shortcuts import ShortcutRouter


COMMAND_PALETTE_MODAL_ID = "editor.command_palette"
COMMAND_PALETTE_CONTEXT_ID = "command_palette"


@dataclass(frozen=True, slots=True)
class CommandPaletteEntry:
    command_id: str
    display_name: str
    category: str
    shortcut: str
    enabled: bool
    disabled_reason: str = ""


class CommandPaletteService:
    """Own command discovery and execution without owning ImGui presentation."""

    def __init__(
        self,
        commands: EditorCommandRegistry,
        shortcuts: ShortcutRouter,
        focus: FocusService,
        modals: ModalService,
    ) -> None:
        if not isinstance(commands, EditorCommandRegistry):
            raise TypeError("command palette requires EditorCommandRegistry")
        if not isinstance(shortcuts, ShortcutRouter):
            raise TypeError("command palette requires ShortcutRouter")
        if not isinstance(focus, FocusService):
            raise TypeError("command palette requires FocusService")
        if not isinstance(modals, ModalService):
            raise TypeError("command palette requires ModalService")
        self._commands = commands
        self._shortcuts = shortcuts
        self._focus = focus
        self._modals = modals
        self._query = SearchQueryModel()
        self._active = False
        self._request_focus = False
        self._selected_index = 0
        self._source_context: Optional[CommandContext] = None
        self._last_result: Optional[CommandResult] = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def query(self) -> str:
        return self._query.query

    @property
    def selected_index(self) -> int:
        return self._selected_index

    @property
    def request_search_focus(self) -> bool:
        value = self._request_focus
        self._request_focus = False
        return value

    @property
    def last_result(self) -> Optional[CommandResult]:
        return self._last_result

    def register_commands(self) -> None:
        definitions = (
            EditorCommand(
                "command_palette.open",
                lambda context: self.open(context),
                display_name="Open Command Palette",
                category="Window",
                palette_visible=False,
                creates_user_action=False,
            ),
            EditorCommand(
                "command_palette.previous",
                lambda _context: self.move_selection(-1),
                display_name="Command Palette Previous",
                category="Internal",
                can_execute=lambda _context: self._active,
                palette_visible=False,
                creates_user_action=False,
            ),
            EditorCommand(
                "command_palette.next",
                lambda _context: self.move_selection(1),
                display_name="Command Palette Next",
                category="Internal",
                can_execute=lambda _context: self._active,
                palette_visible=False,
                creates_user_action=False,
            ),
            EditorCommand(
                "command_palette.execute",
                lambda _context: self._execute_selected_from_route(),
                display_name="Execute Command Palette Item",
                category="Internal",
                # Routing and execution are separate phases. Rebuilding the
                # filtered entry list from can_execute() lets a command-state
                # refresh race turn a visible selection into a disabled Enter
                # press. The palette modal owns Enter while it is active;
                # execute_selected() remains the single authority for the
                # empty-result case.
                can_execute=lambda _context: self._active,
                palette_visible=False,
                creates_user_action=False,
            ),
        )
        for command in definitions:
            self._commands.register(command, replace=True)

    def open(self, context: CommandContext) -> bool:
        if not isinstance(context, CommandContext):
            raise TypeError("command palette open requires a captured CommandContext")
        if self._active:
            self._request_focus = True
            return False
        if not self._modals.activate(
            COMMAND_PALETTE_MODAL_ID,
            owner_id=context.focus.active_view_id or context.focus.active_panel_id,
        ):
            return False
        self._source_context = replace(context, source=CommandSource.PALETTE)
        self._query.clear()
        self._selected_index = 0
        self._last_result = None
        self._active = True
        self._request_focus = True
        self._focus.input_contexts.push(
            InputContext(
                COMMAND_PALETTE_CONTEXT_ID,
                COMMAND_PALETTE_CONTEXT_ID,
                priority=10_000,
                blocks_lower=True,
            )
        )
        return True

    def close(self) -> bool:
        if not self._active:
            return False
        self._active = False
        self._request_focus = False
        self._source_context = None
        self._query.clear()
        self._selected_index = 0
        self._focus.input_contexts.remove(COMMAND_PALETTE_CONTEXT_ID)
        self._modals.deactivate(COMMAND_PALETTE_MODAL_ID)
        return True

    def shutdown(self) -> None:
        self.close()

    def set_query(self, value: object) -> bool:
        changed = self._query.set_query(value)
        if changed:
            self._selected_index = 0
        return changed

    @property
    def entries(self) -> tuple[CommandPaletteEntry, ...]:
        context = self._source_context
        if context is None:
            return ()
        result: list[CommandPaletteEntry] = []
        query = self._query.normalized_query
        for command in self._commands.commands:
            if not command.palette_visible or not command.display_name:
                continue
            if query and not self._matches(command, query):
                continue
            enabled = self._commands.can_execute(command.command_id, context)
            result.append(
                CommandPaletteEntry(
                    command.command_id,
                    command.display_name,
                    command.category,
                    self._shortcut_label(command.command_id),
                    enabled,
                    ""
                    if enabled
                    else self._commands.disabled_reason(command.command_id, context),
                )
            )
        result.sort(
            key=lambda entry: (
                normalize_search_text(entry.category),
                normalize_search_text(entry.display_name),
                entry.command_id,
            )
        )
        return tuple(result)

    def move_selection(self, delta: int) -> bool:
        entries = self.entries
        if not entries:
            self._selected_index = 0
            return False
        next_index = (self._selected_index + int(delta)) % len(entries)
        if next_index == self._selected_index:
            return False
        self._selected_index = next_index
        return True

    def select(self, index: int) -> bool:
        entries = self.entries
        if not entries:
            self._selected_index = 0
            return False
        next_index = max(0, min(int(index), len(entries) - 1))
        if next_index == self._selected_index:
            return False
        self._selected_index = next_index
        return True

    def execute_selected(self) -> CommandResult:
        entries = self.entries
        context = self._source_context
        if context is None or not entries:
            return CommandResult(
                "",
                CommandStatus.NO_OP,
                "command palette has no selected command",
            )
        index = max(0, min(self._selected_index, len(entries) - 1))
        entry = entries[index]
        if not entry.enabled:
            return CommandResult(
                entry.command_id,
                CommandStatus.DISABLED,
                entry.disabled_reason,
            )
        self.close()
        result = self._commands.execute_context(entry.command_id, context)
        self._last_result = result
        return result

    def _execute_selected_from_route(self) -> CommandResult:
        """Execute the target while preserving the forwarding command boundary."""
        result = self.execute_selected()
        return CommandResult(
            "command_palette.execute",
            result.status,
            result.message,
            result,
        )

    def execute(self, command_id: str) -> CommandResult:
        identity = str(command_id or "").strip()
        for index, entry in enumerate(self.entries):
            if entry.command_id == identity:
                self._selected_index = index
                return self.execute_selected()
        return CommandResult(
            identity,
            CommandStatus.NOT_FOUND,
            "command is not visible in the current palette",
        )

    def _shortcut_label(self, command_id: str) -> str:
        chords = tuple(
            dict.fromkeys(
                binding.chord.display_name()
                for binding in self._shortcuts.bindings
                if binding.command_id == command_id
            )
        )
        return ", ".join(chords)

    @staticmethod
    def _matches(command: EditorCommand, query: str) -> bool:
        haystack = normalize_search_text(
            " ".join(
                (
                    command.display_name,
                    command.category,
                    command.command_id,
                    *command.palette_keywords,
                )
            )
        )
        return all(token in haystack for token in query.split())


__all__ = [
    "COMMAND_PALETTE_CONTEXT_ID",
    "COMMAND_PALETTE_MODAL_ID",
    "CommandPaletteEntry",
    "CommandPaletteService",
]
