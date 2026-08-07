"""Command-backed context-menu presentation shared by editor surfaces."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, TypeAlias

from .commands import (
    CommandContext,
    CommandResult,
    CommandSource,
    EditorCommandRegistry,
)


@dataclass(frozen=True, slots=True)
class ContextMenuCommand:
    """Presentation overrides for one registered editor command."""

    command_id: str
    label: str = ""
    shortcut: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    separator_before: bool = False
    hide_when_disabled: bool = False
    close_on_accept: bool = True
    semantic_id: str = ""

    def __post_init__(self) -> None:
        identifier = str(self.command_id or "").strip()
        if not identifier:
            raise ValueError("context-menu command_id must not be empty")
        object.__setattr__(self, "command_id", identifier)
        object.__setattr__(self, "label", str(self.label or "").strip())
        object.__setattr__(self, "shortcut", str(self.shortcut or "").strip())
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "semantic_id", str(self.semantic_id or "").strip())


@dataclass(frozen=True, slots=True)
class ContextMenuSubmenu:
    """One presentation-only branch whose leaves are registered commands."""

    label: str
    entries: Sequence["ContextMenuEntry"]
    separator_before: bool = False
    enabled: bool = True
    hide_when_empty: bool = True
    semantic_id: str = ""

    def __post_init__(self) -> None:
        label = str(self.label or "").strip()
        if not label:
            raise ValueError("context-menu submenu label must not be empty")
        semantic_id = str(self.semantic_id or "").strip()
        if not semantic_id:
            raise ValueError("context-menu submenu semantic_id must not be empty")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "entries", tuple(self.entries or ()))
        object.__setattr__(self, "semantic_id", semantic_id)


ContextMenuEntry: TypeAlias = ContextMenuCommand | ContextMenuSubmenu


@dataclass(frozen=True, slots=True)
class ResolvedContextMenuCommand:
    """Immutable command presentation resolved for one menu opening."""

    spec: ContextMenuCommand
    context: CommandContext
    label: str
    shortcut: str
    enabled: bool
    checked: bool
    disabled_reason: str = ""


@dataclass(frozen=True, slots=True)
class ContextMenuRenderResult:
    """Result returned when a menu item was invoked."""

    command: ResolvedContextMenuCommand
    result: CommandResult


ContextMenuSemanticRecorder = Callable[
    [object, ResolvedContextMenuCommand], None
]


class ContextMenuBuilder:
    """Resolve and draw context menus exclusively from Command Registry data."""

    def __init__(self, registry: Optional[EditorCommandRegistry] = None) -> None:
        self._registry = registry

    @property
    def registry(self) -> EditorCommandRegistry:
        return self._registry or EditorCommandRegistry.instance()

    def resolve(
        self,
        specs: Sequence[ContextMenuCommand],
        *,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> tuple[ResolvedContextMenuCommand, ...]:
        registry = self.registry
        base_payload = dict(payload or {})
        resolved: list[ResolvedContextMenuCommand] = []
        for spec in specs:
            if not isinstance(spec, ContextMenuCommand):
                raise TypeError("context-menu entries must be ContextMenuCommand values")
            command = registry.get(spec.command_id)
            if command is None:
                raise KeyError(f"context-menu command is not registered: {spec.command_id}")
            item_payload = dict(base_payload)
            item_payload.update(spec.payload)
            context = registry.context(CommandSource.CONTEXT_MENU, item_payload)
            enabled = registry.can_execute(spec.command_id, context)
            if not enabled and spec.hide_when_disabled:
                continue
            resolved.append(
                ResolvedContextMenuCommand(
                    spec=spec,
                    context=context,
                    label=spec.label or command.display_name or command.command_id,
                    shortcut=spec.shortcut or command.default_shortcut,
                    enabled=enabled,
                    checked=registry.is_checked(spec.command_id, context),
                    disabled_reason=(
                        "" if enabled else registry.disabled_reason(spec.command_id, context)
                    ),
                )
            )
        return tuple(resolved)

    def render(
        self,
        ctx: object,
        specs: Sequence[ContextMenuEntry],
        *,
        payload: Optional[Mapping[str, Any]] = None,
        semantic_recorder: Optional[ContextMenuSemanticRecorder] = None,
    ) -> Optional[ContextMenuRenderResult]:
        result = self._render_entries(
            ctx,
            specs,
            payload=dict(payload or {}),
            semantic_recorder=semantic_recorder,
            execute=True,
        )
        if result is None or isinstance(result, ContextMenuRenderResult):
            return result
        raise RuntimeError("executing context menu returned a deferred request")

    def render_deferred(
        self,
        ctx: object,
        specs: Sequence[ContextMenuEntry],
        *,
        payload: Optional[Mapping[str, Any]] = None,
        semantic_recorder: Optional[ContextMenuSemanticRecorder] = None,
    ) -> Optional[ResolvedContextMenuCommand]:
        """Render a menu and return its request without executing it.

        ImGui callers whose command can invalidate the current widget tree use
        this form, close every popup/ID scope, and then call
        :meth:`execute_resolved`.  The command metadata, frozen payload and
        enablement are otherwise identical to :meth:`render`.
        """

        result = self._render_entries(
            ctx,
            specs,
            payload=dict(payload or {}),
            semantic_recorder=semantic_recorder,
            execute=False,
        )
        if result is None or isinstance(result, ResolvedContextMenuCommand):
            return result
        raise RuntimeError("deferred context menu executed its request")

    def execute_resolved(
        self,
        command: ResolvedContextMenuCommand,
    ) -> ContextMenuRenderResult:
        """Execute one request previously returned by ``render_deferred``."""

        if not isinstance(command, ResolvedContextMenuCommand):
            raise TypeError("resolved context-menu command is required")
        result = self.registry.execute_context(
            command.spec.command_id,
            command.context,
        )
        return ContextMenuRenderResult(command, result)

    def _visible_entries(
        self,
        specs: Sequence[ContextMenuEntry],
        payload: Mapping[str, Any],
    ) -> tuple[ContextMenuEntry | ResolvedContextMenuCommand, ...]:
        visible: list[ContextMenuEntry | ResolvedContextMenuCommand] = []
        for spec in specs:
            if isinstance(spec, ContextMenuCommand):
                resolved = self.resolve((spec,), payload=payload)
                if resolved:
                    visible.append(resolved[0])
                continue
            if isinstance(spec, ContextMenuSubmenu):
                children = self._visible_entries(spec.entries, payload)
                if children or not spec.hide_when_empty:
                    visible.append(spec)
                continue
            raise TypeError(
                "context-menu entries must be ContextMenuCommand or "
                "ContextMenuSubmenu values"
            )
        return tuple(visible)

    def _render_entries(
        self,
        ctx: object,
        specs: Sequence[ContextMenuEntry],
        *,
        payload: Mapping[str, Any],
        semantic_recorder: Optional[ContextMenuSemanticRecorder],
        execute: bool,
    ) -> Optional[ContextMenuRenderResult | ResolvedContextMenuCommand]:
        rendered_any = False
        for entry in self._visible_entries(specs, payload):
            separator_before = (
                entry.spec.separator_before
                if isinstance(entry, ResolvedContextMenuCommand)
                else entry.separator_before
            )
            if separator_before and rendered_any:
                ctx.separator()
            if isinstance(entry, ContextMenuSubmenu):
                opened = bool(
                    ctx.begin_menu(
                        entry.label,
                        entry.enabled,
                        entry.semantic_id,
                    )
                )
                rendered_any = True
                if not opened:
                    continue
                try:
                    result = self._render_entries(
                        ctx,
                        entry.entries,
                        payload=payload,
                        semantic_recorder=semantic_recorder,
                        execute=execute,
                    )
                finally:
                    ctx.end_menu()
                if result is not None:
                    return result
                continue

            command = entry
            requested = bool(
                ctx.menu_item(
                    command.label,
                    command.shortcut,
                    command.checked,
                    command.enabled,
                )
            )
            rendered_any = True
            if semantic_recorder is not None:
                semantic_recorder(ctx, command)
            else:
                recorder = getattr(ctx, "record_semantic_item", None)
                if callable(recorder):
                    recorder(
                        "menu_item",
                        command.label,
                        command.enabled,
                        command.spec.semantic_id or command.spec.command_id,
                    )
            if not requested or not command.enabled:
                continue
            if command.spec.close_on_accept:
                ctx.close_current_popup()
            if not execute:
                return command
            return self.execute_resolved(command)
        return None
