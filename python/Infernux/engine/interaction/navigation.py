"""Atomic editor navigation across panels and typed selection domains."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from .descriptors import SelectionDomain, SelectionTarget
from .selection import SelectionService


@dataclass(frozen=True)
class NavigationRequest:
    """Presentation policy for one typed navigation operation."""

    record_history: bool = True
    activate_panel: bool = True


NavigationAdapter = Callable[[SelectionTarget, NavigationRequest], bool]


@dataclass(frozen=True, slots=True)
class DirectoryNavigationSnapshot:
    """Immutable browser-style state for one Project/FileManager view."""

    current_path: str = ""
    back_paths: tuple[str, ...] = ()
    forward_paths: tuple[str, ...] = ()


class DirectoryNavigationHistory:
    """Keep directory Back/Forward state separate from document undo history."""

    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max(1, int(max_entries))
        self._current = ""
        self._back: list[str] = []
        self._forward: list[str] = []

    @property
    def snapshot(self) -> DirectoryNavigationSnapshot:
        return DirectoryNavigationSnapshot(
            self._current,
            tuple(self._back),
            tuple(self._forward),
        )

    @property
    def can_go_back(self) -> bool:
        return bool(self._back)

    @property
    def can_go_forward(self) -> bool:
        return bool(self._forward)

    def sync(self, current_path: str) -> None:
        """Adopt an external path change and invalidate stale browser state."""
        current = str(current_path or "").strip()
        if not self._current:
            self._current = current
            return
        if current == self._current:
            return
        self._current = current
        self._back.clear()
        self._forward.clear()

    def navigate(self, target_path: str, apply: Callable[[str], object]) -> bool:
        target = str(target_path or "").strip()
        if not target or target == self._current:
            return False
        if apply(target) is False:
            return False
        if self._current:
            self._back.append(self._current)
            del self._back[:-self.max_entries]
        self._current = target
        self._forward.clear()
        return True

    def back(self, apply: Callable[[str], object]) -> bool:
        if not self._back:
            return False
        target = self._back[-1]
        if apply(target) is False:
            return False
        self._back.pop()
        if self._current:
            self._forward.append(self._current)
            del self._forward[:-self.max_entries]
        self._current = target
        return True

    def forward(self, apply: Callable[[str], object]) -> bool:
        if not self._forward:
            return False
        target = self._forward[-1]
        if apply(target) is False:
            return False
        self._forward.pop()
        if self._current:
            self._back.append(self._current)
            del self._back[:-self.max_entries]
        self._current = target
        return True

    def clear(self) -> None:
        self._current = ""
        self._back.clear()
        self._forward.clear()


class NavigationService:
    """Locate a typed target through one panel activation and selection action."""

    def __init__(self, selection: Optional[SelectionService] = None) -> None:
        self._selection = selection or SelectionService.instance()
        self._adapters: dict[SelectionDomain, NavigationAdapter] = {}

    def register(
        self,
        domain: SelectionDomain,
        adapter: NavigationAdapter,
        *,
        replace: bool = False,
    ) -> None:
        domain = SelectionDomain(domain)
        if not callable(adapter):
            raise TypeError("navigation adapter must be callable")
        if domain in self._adapters and not replace:
            raise ValueError(f"navigation adapter already registered: {domain.value}")
        self._adapters[domain] = adapter

    def unregister(self, domain: SelectionDomain) -> bool:
        return self._adapters.pop(SelectionDomain(domain), None) is not None

    def locate(
        self,
        target: SelectionTarget,
        *,
        owner_id: str,
        reason: str = "locate",
        record_history: bool = True,
        activate_panel: bool = True,
    ) -> bool:
        if not isinstance(target, SelectionTarget):
            raise TypeError("navigation target must be a SelectionTarget")
        adapter = self._adapters.get(target.domain)
        if adapter is None:
            return False

        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if record_history and (
            manager is None or not manager.enabled or manager.is_executing
        ):
            return False
        request = NavigationRequest(
            record_history=bool(record_history),
            activate_panel=bool(activate_panel),
        )

        def apply() -> bool:
            if not adapter(target, request):
                return False
            self._selection.select(
                target,
                owner_id=owner_id,
                reason=reason,
                record_history=record_history,
            )
            return True

        if not record_history or manager.is_user_action_active:
            return apply()
        with manager.user_action("Navigate Editor Target"):
            return apply()

    def reveal(
        self,
        target: SelectionTarget,
        *,
        record_history: bool = True,
        activate_panel: bool = False,
    ) -> bool:
        """Reveal a target without changing the global selection."""
        if not isinstance(target, SelectionTarget):
            raise TypeError("navigation target must be a SelectionTarget")
        adapter = self._adapters.get(target.domain)
        if adapter is None:
            return False

        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if record_history and (
            manager is None or not manager.enabled or manager.is_executing
        ):
            return False
        request = NavigationRequest(
            record_history=bool(record_history),
            activate_panel=bool(activate_panel),
        )
        if not record_history or manager.is_user_action_active:
            return bool(adapter(target, request))
        with manager.user_action("Reveal Editor Target"):
            return bool(adapter(target, request))

    def clear(self) -> None:
        self._adapters.clear()


__all__ = [
    "DirectoryNavigationHistory",
    "DirectoryNavigationSnapshot",
    "NavigationAdapter",
    "NavigationRequest",
    "NavigationService",
]
