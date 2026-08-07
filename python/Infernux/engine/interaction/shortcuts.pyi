from enum import Enum, IntFlag
from typing import Optional
from .commands import CommandResult, EditorCommandRegistry
from .contexts import FocusService
from .modals import ModalService

class ShortcutModifier(IntFlag):
    NONE: ShortcutModifier
    CTRL: ShortcutModifier
    SHIFT: ShortcutModifier
    ALT: ShortcutModifier
    SUPER: ShortcutModifier

class ShortcutPhase(str, Enum):
    PRESS: ShortcutPhase
    RELEASE: ShortcutPhase
    REPEAT: ShortcutPhase

class ShortcutScope(str, Enum):
    GLOBAL: ShortcutScope
    PANEL: ShortcutScope
    CHILD_CONTEXT: ShortcutScope

class ShortcutRouteStatus(str, Enum):
    EXECUTED: ShortcutRouteStatus
    NO_OP: ShortcutRouteStatus
    NO_MATCH: ShortcutRouteStatus
    DISABLED: ShortcutRouteStatus
    CONFLICT: ShortcutRouteStatus
    BLOCKED: ShortcutRouteStatus
    FAILED: ShortcutRouteStatus

class KeyChord:
    key: str
    modifiers: ShortcutModifier
    def __init__(self, key: str, modifiers: ShortcutModifier = ShortcutModifier.NONE) -> None: ...
    @classmethod
    def parse(cls, value: str) -> KeyChord: ...
    def display_name(self) -> str: ...

class ShortcutBinding:
    command_id: str
    chord: KeyChord
    scope: ShortcutScope
    owner_id: str
    phase: ShortcutPhase
    priority: int
    allow_when_text_input: bool
    allow_when_modal: bool
    allow_when_captured: bool
    binding_id: str
    def __init__(self, command_id: str, chord: KeyChord, scope: ShortcutScope = ShortcutScope.GLOBAL, owner_id: str = "", phase: ShortcutPhase = ShortcutPhase.PRESS, priority: int = 0, allow_when_text_input: bool = False, allow_when_modal: bool = False, allow_when_captured: bool = False, binding_id: str = "") -> None: ...

class ShortcutEvent:
    chord: KeyChord
    phase: ShortcutPhase
    text_input_active: bool
    modal_active: bool
    game_view_captured: bool
    def __init__(self, chord: KeyChord, phase: ShortcutPhase = ShortcutPhase.PRESS, text_input_active: bool = False, modal_active: bool = False, game_view_captured: bool = False) -> None: ...

class ShortcutRouteResult:
    status: ShortcutRouteStatus
    command_id: str
    binding_id: str
    conflicts: tuple[str, ...]
    command_result: Optional[CommandResult]
    @property
    def consumed(self) -> bool: ...

class ShortcutRouter:
    def __init__(self, commands: Optional[EditorCommandRegistry] = None, focus: Optional[FocusService] = None, modals: Optional[ModalService] = None) -> None: ...
    @classmethod
    def instance(cls) -> ShortcutRouter: ...
    @property
    def revision(self) -> int: ...
    @property
    def bindings(self) -> tuple[ShortcutBinding, ...]: ...
    @property
    def route_revision(self) -> int: ...
    @property
    def last_event(self) -> Optional[ShortcutEvent]: ...
    @property
    def last_result(self) -> Optional[ShortcutRouteResult]: ...
    def register(self, binding: ShortcutBinding, *, replace: bool = False) -> None: ...
    def unregister(self, binding_id: str) -> bool: ...
    def conflicts_for(self, binding: ShortcutBinding) -> tuple[ShortcutBinding, ...]: ...
    def route(self, event: ShortcutEvent) -> ShortcutRouteResult: ...
    def clear(self) -> None: ...
