from typing import Any, Callable, Mapping, Optional, Sequence, TypeAlias
from .commands import CommandContext, CommandResult, EditorCommandRegistry

class ContextMenuCommand:
    command_id: str
    label: str
    shortcut: str
    payload: Mapping[str, Any]
    separator_before: bool
    hide_when_disabled: bool
    close_on_accept: bool
    semantic_id: str
    def __init__(self, command_id: str, label: str = "", shortcut: str = "", payload: Mapping[str, Any] = ..., separator_before: bool = False, hide_when_disabled: bool = False, close_on_accept: bool = True, semantic_id: str = "") -> None: ...

class ContextMenuSubmenu:
    label: str
    entries: Sequence[ContextMenuEntry]
    separator_before: bool
    enabled: bool
    hide_when_empty: bool
    semantic_id: str
    def __init__(self, label: str, entries: Sequence[ContextMenuEntry], separator_before: bool = False, enabled: bool = True, hide_when_empty: bool = True, semantic_id: str = "") -> None: ...

ContextMenuEntry: TypeAlias = ContextMenuCommand | ContextMenuSubmenu

class ResolvedContextMenuCommand:
    spec: ContextMenuCommand
    context: CommandContext
    label: str
    shortcut: str
    enabled: bool
    checked: bool
    disabled_reason: str

class ContextMenuRenderResult:
    command: ResolvedContextMenuCommand
    result: CommandResult

ContextMenuSemanticRecorder = Callable[[object, ResolvedContextMenuCommand], None]

class ContextMenuBuilder:
    def __init__(self, registry: Optional[EditorCommandRegistry] = None) -> None: ...
    @property
    def registry(self) -> EditorCommandRegistry: ...
    def resolve(self, specs: Sequence[ContextMenuCommand], *, payload: Optional[Mapping[str, Any]] = None) -> tuple[ResolvedContextMenuCommand, ...]: ...
    def render(self, ctx: object, specs: Sequence[ContextMenuEntry], *, payload: Optional[Mapping[str, Any]] = None, semantic_recorder: Optional[ContextMenuSemanticRecorder] = None) -> Optional[ContextMenuRenderResult]: ...
    def render_deferred(self, ctx: object, specs: Sequence[ContextMenuEntry], *, payload: Optional[Mapping[str, Any]] = None, semantic_recorder: Optional[ContextMenuSemanticRecorder] = None) -> Optional[ResolvedContextMenuCommand]: ...
    def execute_resolved(self, command: ResolvedContextMenuCommand) -> ContextMenuRenderResult: ...
