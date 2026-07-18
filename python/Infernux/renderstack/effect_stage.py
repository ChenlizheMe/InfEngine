"""Stable user-facing attachment points declared by render pipelines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Iterable, Tuple


_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")


class EffectScope(str, Enum):
    """The image set made available to an effect mounted at a stage."""

    ROUTE = "route"
    LAYER = "layer"
    STAGE = "stage"
    COMPOSITE = "composite"


@dataclass(frozen=True)
class EffectResourceContract:
    """Semantic resources and capabilities guaranteed by an effect stage."""

    inputs: FrozenSet[str] = field(default_factory=frozenset)
    outputs: FrozenSet[str] = field(default_factory=frozenset)
    capabilities: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _normalized_names(self.inputs, "inputs"))
        object.__setattr__(self, "outputs", _normalized_names(self.outputs, "outputs"))
        object.__setattr__(
            self,
            "capabilities",
            _normalized_names(self.capabilities, "capabilities"),
        )


@dataclass(frozen=True)
class EffectStage:
    """A stable slot list exposed by a pipeline to a scene RenderStack."""

    stable_id: str
    scope: EffectScope
    display_name: str = ""
    contract: EffectResourceContract = field(default_factory=EffectResourceContract)
    aliases: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        stable_id = _validated_stable_id(self.stable_id, "stable_id")
        object.__setattr__(self, "stable_id", stable_id)
        if not isinstance(self.scope, EffectScope):
            object.__setattr__(self, "scope", EffectScope(str(self.scope)))
        if not self.display_name:
            object.__setattr__(self, "display_name", stable_id.replace("_", " ").title())
        aliases = tuple(_validated_stable_id(value, "alias") for value in self.aliases)
        if stable_id in aliases:
            raise ValueError("effect stage aliases cannot repeat stable_id")
        if len(set(aliases)) != len(aliases):
            raise ValueError("effect stage aliases must be unique")
        object.__setattr__(self, "aliases", aliases)

    def accepts_id(self, value: str) -> bool:
        """Return whether *value* names this stage or one of its migrations."""
        return value == self.stable_id or value in self.aliases


def _validated_stable_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _STABLE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must be a lowercase stable identifier "
            "(letters, digits, underscores, and dotted namespaces)"
        )
    return normalized


def _normalized_names(values: Iterable[str], field_name: str) -> FrozenSet[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of names, not a string")
    normalized = frozenset(str(value or "").strip() for value in values)
    if "" in normalized:
        raise ValueError(f"{field_name} cannot contain an empty name")
    return normalized
