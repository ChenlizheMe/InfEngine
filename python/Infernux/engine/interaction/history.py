"""Read-only projection of the global editor action journal."""

from __future__ import annotations

from dataclasses import dataclass

from .action_journal import EditorActionJournal, EditorContextSnapshot, JournalEntry
from .search import SearchQueryModel, normalize_search_text


@dataclass(frozen=True, slots=True)
class HistoryEntrySnapshot:
    sequence: int
    operation_id: str
    command_id: str
    description: str
    action_kind: str
    context: str
    target: str
    document: str
    origin: str
    state: str
    timestamp: float
    is_next_undo: bool = False
    is_next_redo: bool = False


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    revision: int
    cursor: int
    total: int
    entries: tuple[HistoryEntrySnapshot, ...]


class HistoryModel:
    """Filterable evidence view that never owns or reorders history."""

    def __init__(self, journal: EditorActionJournal) -> None:
        if not isinstance(journal, EditorActionJournal):
            raise TypeError("history model requires EditorActionJournal")
        self._journal = journal
        self._query = SearchQueryModel()
        self._cache_key = (-1, -1)
        self._snapshot = HistorySnapshot(0, 0, 0, ())

    @property
    def query(self) -> str:
        return self._query.query

    def set_query(self, value: object) -> bool:
        return self._query.set_query(value)

    @property
    def snapshot(self) -> HistorySnapshot:
        cache_key = (self._journal.revision, self._query.revision)
        if cache_key != self._cache_key:
            self._snapshot = self._build_snapshot()
            self._cache_key = cache_key
        return self._snapshot

    def _build_snapshot(self) -> HistorySnapshot:
        source = self._journal.entries
        cursor = self._journal.cursor
        query = self._query.normalized_query
        rows: list[HistoryEntrySnapshot] = []
        for index, entry in enumerate(source):
            row = self._project_entry(index, entry, cursor)
            if query and not self._matches(row, query):
                continue
            rows.append(row)
        return HistorySnapshot(
            self._journal.revision,
            cursor,
            len(source),
            tuple(rows),
        )

    @staticmethod
    def _project_entry(
        index: int,
        entry: JournalEntry,
        cursor: int,
    ) -> HistoryEntrySnapshot:
        context = entry.after_context or entry.before_context
        action = entry.action
        return HistoryEntrySnapshot(
            sequence=index + 1,
            operation_id=entry.operation_id,
            command_id=entry.command_id or type(action).__name__,
            description=str(getattr(action, "description", "") or type(action).__name__),
            action_kind=type(action).__name__,
            context=HistoryModel._context_label(context),
            target=HistoryModel._target_label(context),
            document=HistoryModel._document_label(context),
            origin=entry.origin.value,
            state="applied" if index < cursor else "redo",
            timestamp=float(entry.timestamp),
            is_next_undo=index == cursor - 1,
            is_next_redo=index == cursor,
        )

    @staticmethod
    def _context_label(context: EditorContextSnapshot | None) -> str:
        if context is None:
            return ""
        focus = context.focus
        return str(
            focus.child_context_id
            or focus.active_view_id
            or focus.active_panel_id
            or ""
        )

    @staticmethod
    def _target_label(context: EditorContextSnapshot | None) -> str:
        if context is None or context.selection.primary is None:
            return ""
        selection = context.selection
        primary = selection.primary
        suffix = f" +{len(selection.targets) - 1}" if len(selection.targets) > 1 else ""
        kind = primary.sub_kind or primary.domain.value
        return f"{kind}:{primary.target_id}{suffix}"

    @staticmethod
    def _document_label(context: EditorContextSnapshot | None) -> str:
        if context is None or context.document is None:
            return ""
        document = context.document
        return str(
            document.title
            or document.resource_path
            or document.key_hint.kind.value
        )

    @staticmethod
    def _matches(row: HistoryEntrySnapshot, query: str) -> bool:
        searchable = normalize_search_text(
            " ".join(
                (
                    row.description,
                    row.command_id,
                    row.action_kind,
                    row.context,
                    row.target,
                    row.document,
                    row.origin,
                    row.state,
                )
            )
        )
        return all(token in searchable for token in query.split())


__all__ = ["HistoryEntrySnapshot", "HistoryModel", "HistorySnapshot"]
