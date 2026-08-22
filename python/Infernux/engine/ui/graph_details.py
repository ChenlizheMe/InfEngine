"""Shared detail-panel dispatch for every node-authored editor."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from Infernux.lib import InxGUIContext


@dataclass(frozen=True, slots=True)
class GraphDetailContributor:
    """One domain-owned detail view hosted by the common graph shell."""

    contributor_id: str
    priority: int
    is_active: Callable[[], bool]
    render: Callable[[InxGUIContext], None]

    def __post_init__(self) -> None:
        if not str(self.contributor_id or "").strip():
            raise ValueError("graph detail contributor ID must be non-empty")
        if not callable(self.is_active) or not callable(self.render):
            raise TypeError("graph detail contributor requires active and render callbacks")


class GraphDetailHost:
    """Select and render exactly one detail contributor per frame."""

    @staticmethod
    def ordered(
        contributors: Iterable[GraphDetailContributor],
    ) -> tuple[GraphDetailContributor, ...]:
        result = tuple(contributors)
        ids = [item.contributor_id for item in result]
        if len(ids) != len(set(ids)):
            raise ValueError("graph detail contributor IDs must be unique")
        return tuple(
            sorted(
                result,
                key=lambda item: (-int(item.priority), item.contributor_id),
            )
        )

    @classmethod
    def render(
        cls,
        ctx: InxGUIContext,
        contributors: Iterable[GraphDetailContributor],
    ) -> str:
        for contributor in cls.ordered(contributors):
            if not bool(contributor.is_active()):
                continue
            contributor.render(ctx)
            return contributor.contributor_id
        return ""


__all__ = ["GraphDetailContributor", "GraphDetailHost"]
