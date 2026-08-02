"""Stable value types shared by editor interaction services."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class SelectionDomain(str, Enum):
    """The editor model addressed by a selection target."""

    SCENE_OBJECT = "scene_object"
    ASSET = "asset"
    ASSET_SUBRESOURCE = "asset_subresource"
    COMPONENT = "component"
    GRAPH_ELEMENT = "graph_element"
    TIMELINE_ELEMENT = "timeline_element"
    UI_ELEMENT = "ui_element"
    DIAGNOSTIC_ENTRY = "diagnostic_entry"
    SETTINGS_ELEMENT = "settings_element"


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

    @classmethod
    def asset_subresource(
        cls,
        asset_path: str,
        subresource_id: str,
        *,
        sub_kind: str,
    ) -> "SelectionTarget":
        from Infernux.engine.path_utils import lexical_path

        normalized = lexical_path(asset_path)
        identifier = str(subresource_id or "").strip()
        kind = str(sub_kind or "").strip()
        if not normalized or not identifier or not kind:
            raise ValueError(
                "asset subresource selection requires asset path, id, and kind"
            )
        return cls(
            SelectionDomain.ASSET_SUBRESOURCE,
            identifier,
            document_id=normalized,
            sub_kind=kind,
        )

    @classmethod
    def component(
        cls,
        object_id: int,
        component_id: int,
        *,
        document_id: str = "",
        sub_kind: str = "",
    ) -> "SelectionTarget":
        object_id = int(object_id)
        component_id = int(component_id)
        if object_id <= 0 or component_id <= 0:
            raise ValueError(
                "component selection requires positive object and component ids"
            )
        return cls(
            SelectionDomain.COMPONENT,
            f"{object_id}:{component_id}",
            document_id=document_id,
            sub_kind=sub_kind,
        )

    @classmethod
    def graph_element(
        cls,
        document_id: str,
        element_id: str,
        *,
        sub_kind: str,
    ) -> "SelectionTarget":
        return cls._document_element(
            SelectionDomain.GRAPH_ELEMENT,
            document_id,
            element_id,
            sub_kind,
            "graph",
        )

    @classmethod
    def timeline_element(
        cls,
        document_id: str,
        element_id: str,
        *,
        sub_kind: str,
    ) -> "SelectionTarget":
        return cls._document_element(
            SelectionDomain.TIMELINE_ELEMENT,
            document_id,
            element_id,
            sub_kind,
            "timeline",
        )

    @classmethod
    def ui_element(
        cls,
        document_id: str,
        element_id: str,
        *,
        sub_kind: str = "",
    ) -> "SelectionTarget":
        return cls._document_element(
            SelectionDomain.UI_ELEMENT,
            document_id,
            element_id,
            sub_kind,
            "UI",
            require_kind=False,
        )

    @classmethod
    def diagnostic_entry(
        cls,
        owner_id: str,
        entry_id: str,
        *,
        sub_kind: str = "log",
    ) -> "SelectionTarget":
        return cls._document_element(
            SelectionDomain.DIAGNOSTIC_ENTRY,
            owner_id,
            entry_id,
            sub_kind,
            "diagnostic",
        )

    @classmethod
    def settings_element(
        cls,
        document_id: str,
        element_id: str,
        *,
        sub_kind: str,
    ) -> "SelectionTarget":
        return cls._document_element(
            SelectionDomain.SETTINGS_ELEMENT,
            document_id,
            element_id,
            sub_kind,
            "settings",
        )

    @classmethod
    def _document_element(
        cls,
        domain: SelectionDomain,
        document_id: str,
        element_id: str,
        sub_kind: str,
        label: str,
        *,
        require_kind: bool = True,
    ) -> "SelectionTarget":
        document = str(document_id or "").strip()
        identifier = str(element_id or "").strip()
        kind = str(sub_kind or "").strip()
        if not document or not identifier or (require_kind and not kind):
            suffix = ", and kind" if require_kind else ""
            raise ValueError(
                f"{label} element selection requires document, id{suffix}"
            )
        return cls(domain, identifier, document_id=document, sub_kind=kind)

    def scene_object_id(self) -> int:
        if self.domain is not SelectionDomain.SCENE_OBJECT:
            return 0
        try:
            return int(self.target_id)
        except (TypeError, ValueError):
            return 0

    def component_ids(self) -> tuple[int, int]:
        if self.domain is not SelectionDomain.COMPONENT:
            return 0, 0
        try:
            object_id, component_id = self.target_id.split(":", 1)
            return int(object_id), int(component_id)
        except (TypeError, ValueError):
            return 0, 0


@dataclass(frozen=True, slots=True)
class SelectionSnapshot:
    """Immutable, replayable global editor selection."""

    owner_id: str = ""
    targets: tuple[SelectionTarget, ...] = ()
    primary_index: int = -1
    anchor_index: int = -1

    def __post_init__(self) -> None:
        owner_id = str(self.owner_id or "").strip()
        object.__setattr__(self, "owner_id", owner_id)
        original = tuple(self.targets)
        if any(not isinstance(target, SelectionTarget) for target in original):
            raise TypeError("selection snapshot targets must be SelectionTarget values")
        if original and not owner_id:
            raise ValueError("non-empty selection snapshot requires an owner")
        domains = {target.domain for target in original}
        if len(domains) > 1:
            raise ValueError("selection snapshot cannot mix selection domains")

        original_primary = (
            original[self.primary_index]
            if 0 <= self.primary_index < len(original)
            else None
        )
        original_anchor = (
            original[self.anchor_index]
            if 0 <= self.anchor_index < len(original)
            else None
        )
        unique = tuple(dict.fromkeys(original))
        object.__setattr__(self, "targets", unique)

        count = len(unique)
        if count == 0:
            object.__setattr__(self, "primary_index", -1)
            object.__setattr__(self, "anchor_index", -1)
            return

        primary_index = (
            unique.index(original_primary)
            if original_primary in unique
            else count - 1
        )
        anchor_index = (
            unique.index(original_anchor)
            if original_anchor in unique
            else primary_index
        )
        object.__setattr__(self, "primary_index", primary_index)
        object.__setattr__(self, "anchor_index", anchor_index)

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
