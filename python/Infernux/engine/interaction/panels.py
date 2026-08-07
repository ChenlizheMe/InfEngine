"""Declarative interaction capabilities owned by editor panel types."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .commands import CommandContext
from .descriptors import SelectionDomain
from .shortcuts import KeyChord, ShortcutBinding, ShortcutPhase, ShortcutScope


@dataclass(frozen=True, slots=True)
class PanelCommandSpec:
    """One global command implemented by a panel type."""

    command_id: str

    def __post_init__(self) -> None:
        command_id = str(self.command_id or "").strip()
        if not command_id:
            raise ValueError("panel command_id must not be empty")
        object.__setattr__(self, "command_id", command_id)


@dataclass(frozen=True, slots=True)
class PanelShortcutSpec:
    """Shortcut metadata projected into the shared shortcut router."""

    command_id: str
    chord: KeyChord
    child_context_id: str = ""
    phase: ShortcutPhase = ShortcutPhase.PRESS
    priority: int = 0
    allow_when_text_input: bool = False
    allow_when_modal: bool = False
    allow_when_captured: bool = False

    def __post_init__(self) -> None:
        command_id = str(self.command_id or "").strip()
        child_context_id = str(self.child_context_id or "").strip()
        if not command_id:
            raise ValueError("panel shortcut command_id must not be empty")
        if not isinstance(self.chord, KeyChord):
            raise TypeError("panel shortcut chord must be a KeyChord")
        object.__setattr__(self, "command_id", command_id)
        object.__setattr__(self, "child_context_id", child_context_id)
        object.__setattr__(self, "phase", ShortcutPhase(self.phase))
        object.__setattr__(self, "priority", int(self.priority))


class ExternalDropKind(str, Enum):
    """Typed external payload families a panel may explicitly accept."""

    FILES = "files"


@dataclass(frozen=True, slots=True)
class BoundPanelCommand:
    """Concrete command callbacks bound to one live panel view."""

    execute: Callable[[CommandContext], object]
    can_execute: Callable[[CommandContext], bool]

    def __post_init__(self) -> None:
        if not callable(self.execute) or not callable(self.can_execute):
            raise TypeError("bound panel commands require callable handlers")


class PanelCommandAdapter:
    """Command handlers for one live panel instance."""

    def __init__(self, handlers: Mapping[str, BoundPanelCommand]) -> None:
        normalized: dict[str, BoundPanelCommand] = {}
        for command_id, handler in handlers.items():
            identifier = str(command_id or "").strip()
            if not identifier:
                raise ValueError("panel adapter command_id must not be empty")
            if not isinstance(handler, BoundPanelCommand):
                raise TypeError("panel adapter handlers must be BoundPanelCommand values")
            if identifier in normalized:
                raise ValueError(f"duplicate panel adapter command: {identifier}")
            normalized[identifier] = handler
        self._handlers = normalized

    @property
    def command_ids(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def handler(self, command_id: str) -> Optional[BoundPanelCommand]:
        return self._handlers.get(str(command_id or "").strip())


@dataclass(frozen=True, slots=True)
class PanelInteractionDescriptor:
    """Interaction contract declared once by an editor panel type."""

    commands: tuple[PanelCommandSpec, ...] = ()
    shortcuts: tuple[PanelShortcutSpec, ...] = ()
    owned_selection_domains: frozenset[SelectionDomain] = frozenset()
    external_drop_kinds: frozenset[ExternalDropKind] = frozenset()
    records_focus_history: bool = True
    document_backed: bool = False
    view_command_target_id: str = ""
    adapter_factory: Optional[Callable[[object], PanelCommandAdapter]] = None

    def __post_init__(self) -> None:
        commands = tuple(self.commands)
        shortcuts = tuple(self.shortcuts)
        domains = frozenset(
            SelectionDomain(domain) for domain in self.owned_selection_domains
        )
        external_drop_kinds = frozenset(
            ExternalDropKind(kind) for kind in self.external_drop_kinds
        )
        view_command_target_id = str(self.view_command_target_id or "").strip()
        command_ids = tuple(command.command_id for command in commands)
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("panel interaction command ids must be unique")
        unknown_shortcuts = {
            shortcut.command_id for shortcut in shortcuts
        } - set(command_ids)
        if unknown_shortcuts:
            names = ", ".join(sorted(unknown_shortcuts))
            raise ValueError(f"panel shortcuts reference unknown commands: {names}")
        binding_keys = tuple(
            (
                shortcut.command_id,
                shortcut.chord,
                shortcut.child_context_id,
                shortcut.phase,
            )
            for shortcut in shortcuts
        )
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("panel interaction shortcuts must be unique")
        if commands and not callable(self.adapter_factory):
            raise ValueError("panel commands require an adapter_factory")
        object.__setattr__(self, "commands", commands)
        object.__setattr__(self, "shortcuts", shortcuts)
        object.__setattr__(self, "owned_selection_domains", domains)
        object.__setattr__(self, "external_drop_kinds", external_drop_kinds)
        object.__setattr__(self, "view_command_target_id", view_command_target_id)

    @property
    def command_ids(self) -> frozenset[str]:
        return frozenset(command.command_id for command in self.commands)


@dataclass(slots=True)
class _BoundPanelView:
    type_id: str
    instance: object
    adapter: PanelCommandAdapter


class PanelInteractionRegistry:
    """Resolve focused-view commands without panel-id dispatch branches."""

    def __init__(self) -> None:
        self._descriptors: dict[str, PanelInteractionDescriptor] = {}
        self._views: dict[str, _BoundPanelView] = {}
        self._selection_authorities: dict[str, frozenset[SelectionDomain]] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def register_type(
        self,
        type_id: str,
        descriptor: PanelInteractionDescriptor,
        *,
        replace: bool = False,
    ) -> None:
        identifier = str(type_id or "").strip()
        if not identifier:
            raise ValueError("panel interaction registration requires a type_id")
        if not isinstance(descriptor, PanelInteractionDescriptor):
            raise TypeError("panel interaction descriptor has the wrong type")
        if identifier in self._descriptors and not replace:
            raise ValueError(f"panel interaction already registered: {identifier}")
        replacements: dict[str, _BoundPanelView] = {}
        if replace:
            for view_id, binding in self._views.items():
                if binding.type_id == identifier:
                    replacements[view_id] = self._make_binding(
                        identifier,
                        binding.instance,
                        descriptor,
                    )
        self._descriptors[identifier] = descriptor
        self._views.update(replacements)
        self._revision += 1

    def unregister_type(self, type_id: str) -> bool:
        identifier = str(type_id or "").strip()
        if self._descriptors.pop(identifier, None) is None:
            return False
        for view_id in tuple(self._views):
            if self._views[view_id].type_id == identifier:
                self._views.pop(view_id)
        self._revision += 1
        return True

    def descriptor(self, type_id: str) -> Optional[PanelInteractionDescriptor]:
        return self._descriptors.get(str(type_id or "").strip())

    def descriptor_for_view(
        self,
        view_id: str,
    ) -> Optional[PanelInteractionDescriptor]:
        binding = self._views.get(str(view_id or "").strip())
        return self._descriptors.get(binding.type_id) if binding is not None else None

    def type_id_for_view(self, view_id: str) -> str:
        binding = self._views.get(str(view_id or "").strip())
        return binding.type_id if binding is not None else ""

    def instance_for_view(self, view_id: str) -> Optional[object]:
        binding = self._views.get(str(view_id or "").strip())
        return binding.instance if binding is not None else None

    def accepts_external_drop(
        self,
        view_id: str,
        kind: ExternalDropKind,
    ) -> bool:
        descriptor = self.descriptor_for_view(view_id)
        return bool(
            descriptor is not None
            and ExternalDropKind(kind) in descriptor.external_drop_kinds
        )

    def records_focus_history(self, *, type_id: str = "", view_id: str = "") -> bool:
        descriptor = self.descriptor_for_view(view_id) if view_id else None
        if descriptor is None and type_id:
            descriptor = self.descriptor(type_id)
        return False if descriptor is None else bool(descriptor.records_focus_history)

    def is_document_backed(self, *, type_id: str = "", view_id: str = "") -> bool:
        descriptor = self.descriptor_for_view(view_id) if view_id else None
        if descriptor is None and type_id:
            descriptor = self.descriptor(type_id)
        return False if descriptor is None else bool(descriptor.document_backed)

    def view_command_target(
        self,
        *,
        type_id: str = "",
        view_id: str = "",
    ) -> str:
        """Return the stable View that owns chrome-originated view commands."""
        descriptor = self.descriptor_for_view(view_id) if view_id else None
        if descriptor is None and type_id:
            descriptor = self.descriptor(type_id)
        return "" if descriptor is None else descriptor.view_command_target_id

    def allows_selection(self, owner_id: str, domain: SelectionDomain) -> bool:
        """Validate selection against a panel or explicit core authority."""
        identifier = str(owner_id or "").strip()
        binding = self._views.get(identifier)
        descriptor = (
            self._descriptors.get(binding.type_id)
            if binding is not None
            else self._descriptors.get(identifier)
        )
        resolved_domain = SelectionDomain(domain)
        if descriptor is not None:
            return resolved_domain in descriptor.owned_selection_domains
        return resolved_domain in self._selection_authorities.get(
            identifier,
            frozenset(),
        )

    def register_selection_authority(
        self,
        owner_id: str,
        domains: Iterable[SelectionDomain],
        *,
        replace: bool = False,
    ) -> None:
        """Register a non-panel producer such as the automation command API."""
        identifier = str(owner_id or "").strip()
        if not identifier:
            raise ValueError("selection authority requires an owner id")
        if identifier in self._selection_authorities and not replace:
            raise ValueError(f"selection authority already registered: {identifier}")
        self._selection_authorities[identifier] = frozenset(
            SelectionDomain(domain) for domain in domains
        )
        self._revision += 1

    def descriptors(self) -> tuple[tuple[str, PanelInteractionDescriptor], ...]:
        return tuple(self._descriptors.items())

    def require_types(self, type_ids: Iterable[str]) -> None:
        """Fail before UI construction when a declared panel type is uncovered."""
        required = {
            str(type_id or "").strip()
            for type_id in type_ids
            if str(type_id or "").strip()
        }
        missing = required.difference(self._descriptors)
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(
                "editor surfaces require formal interaction descriptors: "
                f"{names}"
            )

    def bind_view(self, view_id: str, type_id: str, instance: object) -> None:
        resolved_view_id = str(view_id or "").strip()
        resolved_type_id = str(type_id or "").strip()
        if not resolved_view_id or not resolved_type_id:
            raise ValueError("panel view bindings require view_id and type_id")
        descriptor = self.descriptor(resolved_type_id)
        if descriptor is None:
            raise KeyError(
                f"panel interaction descriptor is not registered: {resolved_type_id}"
            )
        self._views[resolved_view_id] = self._make_binding(
            resolved_type_id,
            instance,
            descriptor,
        )
        self._revision += 1

    @staticmethod
    def _make_binding(
        type_id: str,
        instance: object,
        descriptor: PanelInteractionDescriptor,
    ) -> _BoundPanelView:
        factory = descriptor.adapter_factory
        adapter = factory(instance) if factory is not None else PanelCommandAdapter({})
        if not isinstance(adapter, PanelCommandAdapter):
            raise TypeError("panel adapter_factory returned the wrong type")
        if adapter.command_ids != descriptor.command_ids:
            missing = descriptor.command_ids - adapter.command_ids
            extra = adapter.command_ids - descriptor.command_ids
            details: list[str] = []
            if missing:
                details.append(f"missing={sorted(missing)}")
            if extra:
                details.append(f"extra={sorted(extra)}")
            raise ValueError(
                f"panel adapter contract mismatch for {type_id}: "
                + ", ".join(details)
            )
        return _BoundPanelView(
            type_id,
            instance,
            adapter,
        )

    def unbind_view(self, view_id: str) -> bool:
        if self._views.pop(str(view_id or "").strip(), None) is None:
            return False
        self._revision += 1
        return True

    def _handler_for(
        self,
        context: CommandContext,
        command_id: str,
    ) -> Optional[BoundPanelCommand]:
        view_id = str(
            context.focus.active_view_id
            or context.focus.active_panel_id
            or ""
        ).strip()
        return self._handler_for_view(view_id, command_id)

    def _handler_for_view(
        self,
        view_id: str,
        command_id: str,
    ) -> Optional[BoundPanelCommand]:
        binding = self._views.get(str(view_id or "").strip())
        return binding.adapter.handler(command_id) if binding is not None else None

    def owns_active(self, context: CommandContext, command_id: str) -> bool:
        return self._handler_for(context, command_id) is not None

    def can_execute_active(self, context: CommandContext, command_id: str) -> bool:
        handler = self._handler_for(context, command_id)
        return bool(handler is not None and handler.can_execute(context))

    def execute_active(self, context: CommandContext, command_id: str) -> bool:
        handler = self._handler_for(context, command_id)
        if handler is None or not handler.can_execute(context):
            return False
        return bool(handler.execute(context))

    def owns_view(self, view_id: str, command_id: str) -> bool:
        """Return whether one concrete destination view owns a command."""
        return self._handler_for_view(view_id, command_id) is not None

    def can_execute_view(
        self,
        view_id: str,
        context: CommandContext,
        command_id: str,
    ) -> bool:
        """Query a destination view without changing global keyboard focus."""
        handler = self._handler_for_view(view_id, command_id)
        return bool(handler is not None and handler.can_execute(context))

    def execute_view(
        self,
        view_id: str,
        context: CommandContext,
        command_id: str,
    ) -> bool:
        """Execute against a destination view independently of input focus."""
        handler = self._handler_for_view(view_id, command_id)
        if handler is None or not handler.can_execute(context):
            return False
        return bool(handler.execute(context))

    def iter_shortcut_bindings(self) -> Iterable[ShortcutBinding]:
        for type_id, descriptor in self._descriptors.items():
            for index, shortcut in enumerate(descriptor.shortcuts):
                scope = (
                    ShortcutScope.CHILD_CONTEXT
                    if shortcut.child_context_id
                    else ShortcutScope.PANEL
                )
                owner_id = shortcut.child_context_id or type_id
                yield ShortcutBinding(
                    shortcut.command_id,
                    shortcut.chord,
                    scope,
                    owner_id,
                    phase=shortcut.phase,
                    priority=shortcut.priority,
                    allow_when_text_input=shortcut.allow_when_text_input,
                    allow_when_modal=shortcut.allow_when_modal,
                    allow_when_captured=shortcut.allow_when_captured,
                    binding_id=f"panel.{type_id}.{shortcut.command_id}.{index}",
                )

    def clear(self) -> None:
        if self._descriptors or self._views or self._selection_authorities:
            self._descriptors.clear()
            self._views.clear()
            self._selection_authorities.clear()
            self._revision += 1
