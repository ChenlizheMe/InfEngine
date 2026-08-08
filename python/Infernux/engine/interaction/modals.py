"""Global editor modal ownership independent from ImGui presentation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional


@dataclass(frozen=True, slots=True)
class ModalRegistration:
    """One modal presenter registered with the project-session core."""

    modal_id: str
    is_active: Callable[[], bool]
    render: Callable[[Any], object]
    cancel: Callable[[], None]
    allowed_parent_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ActiveModal:
    """Stable identity for one active modal stack entry."""

    modal_id: str
    owner_id: str = ""
    parent_id: str = ""
    # None means the modal has been activated but has not reached its first
    # presentation yet. False means its presenter ran but ImGui did not
    # produce a visible modal surface this frame. The distinction preserves
    # first-frame input exclusivity without allowing a lost popup to become a
    # permanent invisible input lock.
    presented: Optional[bool] = None


class ModalService:
    """Serialize every Editor modal through one top-level portal.

    Presenters own domain state and drawing, while this service owns modal
    exclusivity, nesting, Escape cancellation, and owner destruction.  A
    presenter may only become active after it has been registered here.

    ``render`` callbacks may return ``False`` when their ImGui surface was not
    actually begun. The stack entry is retained for domain recovery, but it no
    longer participates in shortcut input capture until a later frame renders
    it successfully. Returning ``None`` remains compatible with presenters
    that do not expose a visibility result and is treated as presented.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, ModalRegistration] = {}
        self._stack: list[ActiveModal] = []

    @property
    def active_modal_id(self) -> str:
        for entry in reversed(self._stack):
            if entry.presented is not False:
                return entry.modal_id
        return ""

    @property
    def active_owner_id(self) -> str:
        for entry in reversed(self._stack):
            if entry.presented is not False:
                return entry.owner_id
        return ""

    @property
    def active_stack(self) -> tuple[ActiveModal, ...]:
        return tuple(self._stack)

    def is_presented(self, modal_id: str) -> bool:
        """Return whether a registered stack entry is visibly presented.

        A pending first presentation is intentionally not considered visible;
        callers that need the input barrier should use ``active_modal_id``.
        """
        identifier = self._require_id(modal_id)
        return any(
            entry.modal_id == identifier and entry.presented is True
            for entry in self._stack
        )

    def register(
        self,
        modal_id: str,
        *,
        is_active: Callable[[], bool],
        render: Callable[[Any], object],
        cancel: Callable[[], None],
        allowed_parent_ids: Iterable[str] = (),
    ) -> None:
        identifier = self._require_id(modal_id)
        if not all(callable(value) for value in (is_active, render, cancel)):
            raise TypeError("modal registration callbacks must be callable")
        parents = frozenset(
            self._require_id(value) for value in allowed_parent_ids
        )
        registration = ModalRegistration(
            identifier,
            is_active,
            render,
            cancel,
            parents,
        )
        previous = self._registrations.get(identifier)
        if previous is not None and previous != registration:
            raise RuntimeError(f"modal presenter is already registered: {identifier}")
        self._registrations[identifier] = registration

    def unregister(self, modal_id: str, *, cancel: bool = True) -> None:
        identifier = self._require_id(modal_id)
        if cancel:
            self.cancel(identifier)
        else:
            self.deactivate(identifier)
        self._registrations.pop(identifier, None)

    def activate(
        self,
        modal_id: str,
        *,
        owner_id: str = "",
        parent_id: str = "",
    ) -> bool:
        identifier = self._require_id(modal_id)
        if identifier not in self._registrations:
            raise KeyError(f"modal presenter is not registered: {identifier}")
        if any(entry.modal_id == identifier for entry in self._stack):
            return self.active_modal_id == identifier

        registration = self._registrations[identifier]
        parent = str(parent_id or "").strip()
        if self._stack:
            if not parent and self.active_modal_id in registration.allowed_parent_ids:
                parent = self.active_modal_id
            if not parent or self.active_modal_id != parent:
                return False
            if parent not in registration.allowed_parent_ids:
                return False
        elif parent:
            return False

        self._stack.append(
            ActiveModal(
                identifier,
                str(owner_id or "").strip(),
                parent,
            )
        )
        return True

    def deactivate(self, modal_id: str) -> bool:
        identifier = self._require_id(modal_id)
        index = next(
            (i for i, entry in enumerate(self._stack) if entry.modal_id == identifier),
            -1,
        )
        if index < 0:
            return False
        del self._stack[index:]
        return True

    def cancel(self, modal_id: str) -> bool:
        identifier = self._require_id(modal_id)
        if not any(entry.modal_id == identifier for entry in self._stack):
            return False
        registration = self._registrations.get(identifier)
        if registration is not None:
            registration.cancel()
        self.deactivate(identifier)
        return True

    def cancel_active(self) -> bool:
        if not self._stack:
            return False
        # A lost popup is deliberately absent from ``active_modal_id`` so it
        # cannot block shortcuts. Explicit lifecycle cleanup must still be
        # able to cancel that pending domain entry during shutdown or owner
        # destruction.
        return self.cancel(self.active_modal_id or self._stack[-1].modal_id)

    def cancel_owner(self, owner_id: str) -> bool:
        owner = str(owner_id or "").strip()
        if not owner:
            return False
        index = next(
            (i for i, entry in enumerate(self._stack) if entry.owner_id == owner),
            -1,
        )
        if index < 0:
            return False
        for entry in reversed(self._stack[index:]):
            registration = self._registrations.get(entry.modal_id)
            if registration is not None:
                registration.cancel()
        del self._stack[index:]
        return True

    def render(self, ctx: Any) -> None:
        """Render exactly one top-most modal from the global overlay layer."""
        while self._stack:
            entry = self._stack[-1]
            registration = self._registrations.get(entry.modal_id)
            if registration is None or not bool(registration.is_active()):
                self._stack.pop()
                continue

            # Clear the previous heartbeat before entering the presenter. A
            # presenter that cannot begin its popup must explicitly return
            # False, which releases the shortcut barrier while keeping the
            # domain transaction alive for a retry on the next frame.
            index = len(self._stack) - 1
            self._stack[index] = replace(entry, presented=False)
            rendered = registration.render(ctx)
            if (
                index < len(self._stack)
                and self._stack[index].modal_id == entry.modal_id
                and rendered is not False
            ):
                self._stack[index] = replace(
                    self._stack[index],
                    presented=True,
                )
            if not bool(registration.is_active()):
                self.deactivate(entry.modal_id)
            return

    def clear(self, *, cancel: bool = True) -> None:
        if cancel:
            while self._stack:
                self.cancel_active()
        else:
            self._stack.clear()
        self._registrations.clear()

    @staticmethod
    def _require_id(value: str) -> str:
        identifier = str(value or "").strip()
        if not identifier:
            raise ValueError("modal_id must be non-empty")
        return identifier
