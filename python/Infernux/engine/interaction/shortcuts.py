"""Focus-aware shortcut routing for editor commands."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, IntFlag, auto
from typing import Optional
import uuid

from .commands import (
    CommandResult,
    CommandSource,
    CommandStatus,
    EditorCommandRegistry,
)
from .contexts import FocusService
from .modals import ModalService


class ShortcutModifier(IntFlag):
    NONE = 0
    CTRL = auto()
    SHIFT = auto()
    ALT = auto()
    SUPER = auto()


class ShortcutPhase(str, Enum):
    PRESS = "press"
    RELEASE = "release"
    REPEAT = "repeat"


class ShortcutScope(str, Enum):
    GLOBAL = "global"
    PANEL = "panel"
    CHILD_CONTEXT = "child_context"


class ShortcutRouteStatus(str, Enum):
    EXECUTED = "executed"
    NO_OP = "no_op"
    NO_MATCH = "no_match"
    DISABLED = "disabled"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    FAILED = "failed"


_MODIFIER_NAMES = {
    "CTRL": ShortcutModifier.CTRL,
    "CONTROL": ShortcutModifier.CTRL,
    "SHIFT": ShortcutModifier.SHIFT,
    "ALT": ShortcutModifier.ALT,
    "OPTION": ShortcutModifier.ALT,
    "SUPER": ShortcutModifier.SUPER,
    "CMD": ShortcutModifier.SUPER,
    "COMMAND": ShortcutModifier.SUPER,
    "WIN": ShortcutModifier.SUPER,
}


@dataclass(frozen=True, slots=True)
class KeyChord:
    key: str
    modifiers: ShortcutModifier = ShortcutModifier.NONE

    def __post_init__(self) -> None:
        key = str(self.key or "").strip().upper()
        if not key or "+" in key:
            raise ValueError("shortcut key must be one normalized key name")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "modifiers", ShortcutModifier(self.modifiers))

    @classmethod
    def parse(cls, value: str) -> "KeyChord":
        parts = [part.strip().upper() for part in str(value or "").split("+")]
        if not parts or any(not part for part in parts):
            raise ValueError("shortcut chord must not be empty")
        modifiers = ShortcutModifier.NONE
        key = ""
        for part in parts:
            modifier = _MODIFIER_NAMES.get(part)
            if modifier is not None:
                modifiers |= modifier
                continue
            if key:
                raise ValueError("shortcut chord must contain exactly one non-modifier key")
            key = part
        if not key:
            raise ValueError("shortcut chord must contain a non-modifier key")
        return cls(key, modifiers)

    def display_name(self) -> str:
        parts: list[str] = []
        for modifier, name in (
            (ShortcutModifier.CTRL, "Ctrl"),
            (ShortcutModifier.SHIFT, "Shift"),
            (ShortcutModifier.ALT, "Alt"),
            (ShortcutModifier.SUPER, "Super"),
        ):
            if self.modifiers & modifier:
                parts.append(name)
        parts.append(self.key)
        return "+".join(parts)


@dataclass(frozen=True, slots=True)
class ShortcutBinding:
    command_id: str
    chord: KeyChord
    scope: ShortcutScope = ShortcutScope.GLOBAL
    owner_id: str = ""
    phase: ShortcutPhase = ShortcutPhase.PRESS
    priority: int = 0
    allow_when_text_input: bool = False
    allow_when_modal: bool = False
    allow_when_captured: bool = False
    binding_id: str = ""

    def __post_init__(self) -> None:
        command_id = str(self.command_id or "").strip()
        owner_id = str(self.owner_id or "").strip()
        scope = ShortcutScope(self.scope)
        if not command_id:
            raise ValueError("shortcut command_id must not be empty")
        if scope is not ShortcutScope.GLOBAL and not owner_id:
            raise ValueError("contextual shortcut bindings require owner_id")
        object.__setattr__(self, "command_id", command_id)
        object.__setattr__(self, "owner_id", owner_id)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "phase", ShortcutPhase(self.phase))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "binding_id", self.binding_id or uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class ShortcutEvent:
    chord: KeyChord
    phase: ShortcutPhase = ShortcutPhase.PRESS
    text_input_active: bool = False
    modal_active: bool = False
    game_view_captured: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", ShortcutPhase(self.phase))


@dataclass(frozen=True, slots=True)
class ShortcutRouteResult:
    status: ShortcutRouteStatus
    command_id: str = ""
    binding_id: str = ""
    conflicts: tuple[str, ...] = ()
    command_result: Optional[CommandResult] = None

    @property
    def consumed(self) -> bool:
        return self.status in {
            ShortcutRouteStatus.EXECUTED,
            ShortcutRouteStatus.NO_OP,
            ShortcutRouteStatus.DISABLED,
            ShortcutRouteStatus.CONFLICT,
            ShortcutRouteStatus.BLOCKED,
            ShortcutRouteStatus.FAILED,
        }


class ShortcutRouter:
    """Resolve one physical shortcut edge into at most one editor command."""

    _instance: Optional["ShortcutRouter"] = None

    def __init__(
        self,
        commands: Optional[EditorCommandRegistry] = None,
        focus: Optional[FocusService] = None,
        modals: Optional[ModalService] = None,
    ) -> None:
        self._commands = commands
        self._focus = focus
        self._modals = modals
        self._bindings: dict[str, ShortcutBinding] = {}
        self._revision = 0
        self._route_revision = 0
        self._last_event: Optional[ShortcutEvent] = None
        self._last_result: Optional[ShortcutRouteResult] = None
        ShortcutRouter._instance = self

    @classmethod
    def instance(cls) -> "ShortcutRouter":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def bindings(self) -> tuple[ShortcutBinding, ...]:
        return tuple(self._bindings.values())

    @property
    def route_revision(self) -> int:
        return self._route_revision

    @property
    def last_event(self) -> Optional[ShortcutEvent]:
        return self._last_event

    @property
    def last_result(self) -> Optional[ShortcutRouteResult]:
        return self._last_result

    def register(self, binding: ShortcutBinding, *, replace: bool = False) -> None:
        existing = self._bindings.get(binding.binding_id)
        if existing is not None and not replace:
            raise ValueError(f"shortcut binding already registered: {binding.binding_id}")
        self._bindings[binding.binding_id] = binding
        self._revision += 1

    def unregister(self, binding_id: str) -> bool:
        if self._bindings.pop(str(binding_id or "").strip(), None) is None:
            return False
        self._revision += 1
        return True

    def conflicts_for(self, binding: ShortcutBinding) -> tuple[ShortcutBinding, ...]:
        return tuple(
            candidate
            for candidate in self._bindings.values()
            if candidate.binding_id != binding.binding_id
            and candidate.chord == binding.chord
            and candidate.phase is binding.phase
            and candidate.scope is binding.scope
            and candidate.owner_id == binding.owner_id
            and candidate.priority == binding.priority
        )

    def route(self, event: ShortcutEvent) -> ShortcutRouteResult:
        # The native adapter only knows whether ImGui has presented a popup.
        # ModalService is authoritative from activation onward, including the
        # frame before the popup is first drawn.
        modals = self._modals
        if (
            not event.modal_active
            and modals is not None
            and bool(modals.active_modal_id)
        ):
            event = replace(event, modal_active=True)
        matching = [
            binding
            for binding in self._bindings.values()
            if binding.chord == event.chord and binding.phase is event.phase
        ]
        if not matching:
            return self._publish_route(
                event,
                ShortcutRouteResult(ShortcutRouteStatus.NO_MATCH),
            )

        focus = self._focus or FocusService.instance()
        snapshot = focus.snapshot
        ranked: list[tuple[tuple[int, int, int], ShortcutBinding]] = []
        blocked = False
        context_priorities = {
            context.context_id: context.priority
            for context in focus.input_contexts.ordered()
        }
        for index, binding in enumerate(matching):
            scope_rank = self._scope_rank(binding, snapshot, context_priorities)
            if scope_rank is None:
                continue
            if self._is_blocked(binding, event, snapshot.capture_owner_id):
                blocked = True
                continue
            ranked.append(((scope_rank, binding.priority, -index), binding))

        if not ranked:
            return self._publish_route(
                event,
                ShortcutRouteResult(
                    ShortcutRouteStatus.BLOCKED if blocked else ShortcutRouteStatus.NO_MATCH,
                ),
            )

        best_rank = max(rank for rank, _binding in ranked)[:2]
        best = [
            binding
            for rank, binding in ranked
            if rank[:2] == best_rank
        ]
        command_ids = tuple(dict.fromkeys(binding.command_id for binding in best))
        if len(command_ids) != 1:
            return self._publish_route(
                event,
                ShortcutRouteResult(
                    ShortcutRouteStatus.CONFLICT,
                    conflicts=command_ids,
                ),
            )

        binding = best[0]
        commands = self._commands or EditorCommandRegistry.instance()
        result = commands.execute(binding.command_id, source=CommandSource.SHORTCUT)
        status = {
            CommandStatus.EXECUTED: ShortcutRouteStatus.EXECUTED,
            CommandStatus.NO_OP: ShortcutRouteStatus.NO_OP,
            CommandStatus.DISABLED: ShortcutRouteStatus.DISABLED,
            CommandStatus.FAILED: ShortcutRouteStatus.FAILED,
            CommandStatus.NOT_FOUND: ShortcutRouteStatus.FAILED,
        }[result.status]
        return self._publish_route(
            event,
            ShortcutRouteResult(
                status,
                command_id=binding.command_id,
                binding_id=binding.binding_id,
                command_result=result,
            ),
        )

    def _publish_route(
        self,
        event: ShortcutEvent,
        result: ShortcutRouteResult,
    ) -> ShortcutRouteResult:
        self._last_event = event
        self._last_result = result
        self._route_revision += 1
        return result

    @staticmethod
    def _scope_rank(binding, snapshot, context_priorities) -> Optional[int]:
        if binding.scope is ShortcutScope.GLOBAL:
            return 100
        if binding.scope is ShortcutScope.PANEL:
            return 200 if binding.owner_id == snapshot.active_panel_id else None
        if binding.owner_id == snapshot.child_context_id:
            return 300 + context_priorities.get(binding.owner_id, 0)
        if binding.owner_id in context_priorities:
            return 300 + context_priorities[binding.owner_id]
        return None

    @staticmethod
    def _is_blocked(
        binding: ShortcutBinding,
        event: ShortcutEvent,
        capture_owner_id: str,
    ) -> bool:
        if event.text_input_active and not binding.allow_when_text_input:
            return True
        if event.modal_active and not binding.allow_when_modal:
            return True
        if (event.game_view_captured or capture_owner_id) and not binding.allow_when_captured:
            return True
        return False

    def clear(self) -> None:
        if self._bindings:
            self._bindings.clear()
            self._revision += 1
        self._last_event = None
        self._last_result = None
        self._route_revision = 0
