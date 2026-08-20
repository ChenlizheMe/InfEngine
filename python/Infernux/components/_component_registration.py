"""Candidate component registration isolation for transactional script loading."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


_PENDING_ATTRIBUTE = "_component_registration_pending_"


@dataclass
class CandidateComponentRegistrationScope:
    """Collect component classes created by one candidate execution tree."""

    _types: list[type] = field(default_factory=list)
    _type_ids: set[int] = field(default_factory=set)

    def record(self, component_type: type) -> None:
        identity = id(component_type)
        if identity in self._type_ids:
            return
        self._type_ids.add(identity)
        self._types.append(component_type)

    @property
    def pending_types(self) -> tuple[type, ...]:
        return tuple(self._types)


_candidate_scope: ContextVar[CandidateComponentRegistrationScope | None] = (
    ContextVar("infernux_candidate_component_registration_scope", default=None)
)


@contextmanager
def candidate_component_registration_scope() -> Iterator[CandidateComponentRegistrationScope]:
    """Defer class/CDS publication for every component defined in the scope."""
    current = _candidate_scope.get()
    if current is not None:
        yield current
        return

    scope = CandidateComponentRegistrationScope()
    token = _candidate_scope.set(scope)
    try:
        yield scope
    finally:
        _candidate_scope.reset(token)


def record_candidate_component_definition(component_type: type) -> bool:
    """Mark a class defined by candidate execution and record its pending schema."""
    scope = _candidate_scope.get()
    if scope is not None:
        setattr(component_type, _PENDING_ATTRIBUTE, True)
        scope.record(component_type)
        return True
    return False


def stage_candidate_component_type(component_type: type) -> bool:
    """Return whether a registration call must remain private to a candidate."""
    scope = _candidate_scope.get()
    if scope is not None:
        if component_type.__dict__.get(_PENDING_ATTRIBUTE, False):
            scope.record(component_type)
        return True
    return is_component_registration_pending(component_type)


def is_component_registration_pending(component_type: type) -> bool:
    return bool(component_type.__dict__.get(_PENDING_ATTRIBUTE, False))


def mark_component_registration_published(component_type: type) -> None:
    if component_type.__dict__.get(_PENDING_ATTRIBUTE, False):
        setattr(component_type, _PENDING_ATTRIBUTE, False)
