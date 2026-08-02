"""Stable value types shared by editor interaction services."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class SelectionDomain(str, Enum):
    """The editor model addressed by a selection target."""

    SCENE_OBJECT = "scene_object"
    ASSET = "asset"
    COMPONENT = "component"
    GRAPH_ELEMENT = "graph_element"
    TIMELINE_ELEMENT = "timeline_element"
    UI_ELEMENT = "ui_element"


@dataclass(frozen=True, slots=True)
class SelectionTarget:
    """Stable reference to one selected editor item.

    Runtime Python or native object references are intentionally excluded.
    Replaying selection resolves ``target_id`` through the owning domain.
    """

    domain: SelectionDomain
    target_id: str
    document_id: str = ""
    sub_kind: str = ""

    def __post_init__(self) -> None:
        target_id = str(self.target_id).strip()
        if not target_id:
            raise ValueError("selection target_id must not be empty")
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "document_id", str(self.document_id or ""))
        object.__setattr__(self, "sub_kind", str(self.sub_kind or ""))

    @classmethod
    def scene_object(cls, object_id: int) -> "SelectionTarget":
        object_id = int(object_id)
        if object_id <= 0:
            raise ValueError("scene object selection requires a positive object id")
        return cls(SelectionDomain.SCENE_OBJECT, str(object_id))

    @classmethod
    def asset(cls, path: str) -> "SelectionTarget":
        from Infernux.engine.path_utils import lexical_path

        normalized = lexical_path(path)
        if not normalized:
            raise ValueError("asset selection requires a path")
        return cls(SelectionDomain.ASSET, normalized)

    def scene_object_id(self) -> int:
        if self.domain is not SelectionDomain.SCENE_OBJECT:
            return 0
        try:
            return int(self.target_id)
        except (TypeError, ValueError):
            return 0


@dataclass(frozen=True, slots=True)
class SelectionSnapshot:
    """Immutable, replayable global editor selection."""

    owner_id: str = ""
    targets: tuple[SelectionTarget, ...] = ()
    primary_index: int = -1
    anchor_index: int = -1

    def __post_init__(self) -> None:
        unique = tuple(dict.fromkeys(self.targets))
        if unique != self.targets:
            object.__setattr__(self, "targets", unique)

        count = len(unique)
        if count == 0:
            object.__setattr__(self, "primary_index", -1)
            object.__setattr__(self, "anchor_index", -1)
            return

        if not 0 <= self.primary_index < count:
            object.__setattr__(self, "primary_index", count - 1)
        if not 0 <= self.anchor_index < count:
            object.__setattr__(self, "anchor_index", self.primary_index)

    @classmethod
    def create(
        cls,
        targets: Iterable[SelectionTarget],
        *,
        owner_id: str,
        primary: Optional[SelectionTarget] = None,
        anchor: Optional[SelectionTarget] = None,
    ) -> "SelectionSnapshot":
        items = tuple(dict.fromkeys(targets))
        if not items:
            return cls()
        primary_index = items.index(primary) if primary in items else len(items) - 1
        anchor_index = items.index(anchor) if anchor in items else primary_index
        return cls(str(owner_id or ""), items, primary_index, anchor_index)

    @property
    def primary(self) -> Optional[SelectionTarget]:
        if self.primary_index < 0:
            return None
        return self.targets[self.primary_index]

    @property
    def anchor(self) -> Optional[SelectionTarget]:
        if self.anchor_index < 0:
            return None
        return self.targets[self.anchor_index]

    @property
    def domain(self) -> Optional[SelectionDomain]:
        primary = self.primary
        return primary.domain if primary is not None else None

    @property
    def is_empty(self) -> bool:
        return not self.targets
