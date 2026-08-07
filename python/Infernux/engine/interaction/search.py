"""Renderer-independent search state shared by editor surfaces."""

from __future__ import annotations

from dataclasses import dataclass


def normalize_search_text(value: object) -> str:
    """Return the canonical query representation used by Python surfaces."""

    return str(value or "").strip().casefold()


@dataclass(frozen=True, slots=True)
class SearchToken:
    query_revision: int
    source_generation: int = 0
    scope_key: str = ""


class SearchQueryModel:
    """Owns query lifecycle without owning a panel's result projection."""

    __slots__ = ("_query", "_normalized", "_revision")

    def __init__(self, query: str = "") -> None:
        self._query = ""
        self._normalized = ""
        self._revision = 0
        if query:
            self.set_query(query)

    @property
    def query(self) -> str:
        return self._query

    @property
    def normalized_query(self) -> str:
        return self._normalized

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def active(self) -> bool:
        return bool(self._normalized)

    def set_query(self, value: object) -> bool:
        raw = str(value or "")
        normalized = normalize_search_text(raw)
        if raw == self._query and normalized == self._normalized:
            return False
        self._query = raw
        self._normalized = normalized
        self._revision += 1
        return True

    def clear(self) -> bool:
        return self.set_query("")

    def matches(self, value: object) -> bool:
        return not self.active or self._normalized in normalize_search_text(value)

    def matches_normalized(self, value: str) -> bool:
        return not self.active or self._normalized in str(value)

    def token(self, source_generation: int = 0, scope_key: str = "") -> SearchToken:
        return SearchToken(
            query_revision=self._revision,
            source_generation=int(source_generation),
            scope_key=str(scope_key or ""),
        )

    def accepts(
        self,
        token: SearchToken,
        source_generation: int = 0,
        scope_key: str = "",
    ) -> bool:
        return token == self.token(source_generation, scope_key)
