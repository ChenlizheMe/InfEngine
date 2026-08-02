"""Document-aware close transactions independent from their UI presentation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import uuid

from Infernux.debug import Debug

from .documents import (
    DocumentActionStatus,
    DocumentKind,
    DocumentRegistry,
    EditorDocument,
)


class CloseIntentKind(str, Enum):
    CLOSE_VIEW = "close_view"
    REPLACE_DOCUMENT = "replace_document"
    CLOSE_PROJECT = "close_project"
    EXIT_EDITOR = "exit_editor"
    RESET_LAYOUT = "reset_layout"


class CloseState(str, Enum):
    IDLE = "idle"
    AWAITING_DECISION = "awaiting_decision"
    WAITING_FOR_SAVE = "waiting_for_save"


class CloseIssue(str, Enum):
    NONE = "none"
    SAVE_NOT_SUPPORTED = "save_not_supported"
    SAVE_CANCELLED = "save_cancelled"
    SAVE_FAILED = "save_failed"
    DISCARD_NOT_SUPPORTED = "discard_not_supported"
    DISCARD_FAILED = "discard_failed"
    STILL_DIRTY = "still_dirty"


@dataclass(frozen=True, slots=True)
class CloseIntent:
    kind: CloseIntentKind
    view_id: str = ""
    document_ids: tuple[str, ...] = ()
    intent_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", CloseIntentKind(self.kind))
        object.__setattr__(self, "view_id", str(self.view_id or "").strip())
        object.__setattr__(
            self,
            "document_ids",
            tuple(str(value).strip() for value in self.document_ids if str(value).strip()),
        )
        object.__setattr__(self, "intent_id", self.intent_id or uuid.uuid4().hex)


class CloseCoordinator:
    """Serialize close decisions over unique editor documents."""

    def __init__(self, registry: Optional[DocumentRegistry] = None) -> None:
        self._registry = registry
        self._intent: Optional[CloseIntent] = None
        self._document_ids: tuple[str, ...] = ()
        self._cursor = 0
        self._state = CloseState.IDLE
        self._issue = CloseIssue.NONE
        self._message = ""
        self._on_complete: Optional[Callable[[], None]] = None
        self._on_cancel: Optional[Callable[[], None]] = None

    @property
    def registry(self) -> DocumentRegistry:
        return self._registry or DocumentRegistry.instance()

    @property
    def intent(self) -> Optional[CloseIntent]:
        return self._intent

    @property
    def state(self) -> CloseState:
        return self._state

    @property
    def issue(self) -> CloseIssue:
        return self._issue

    @property
    def message(self) -> str:
        return self._message

    @property
    def is_active(self) -> bool:
        return self._state is not CloseState.IDLE

    @property
    def active_document(self) -> Optional[EditorDocument]:
        if not self.is_active or self._cursor >= len(self._document_ids):
            return None
        return self.registry.get(self._document_ids[self._cursor])

    def request(
        self,
        intent: CloseIntent,
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self.is_active:
            return False
        self._intent = intent
        self._document_ids = self._resolve_document_ids(intent)
        self._cursor = 0
        self._state = CloseState.AWAITING_DECISION
        self._issue = CloseIssue.NONE
        self._message = ""
        self._on_complete = on_complete
        self._on_cancel = on_cancel
        self._advance()
        return True

    def decide_save(self) -> None:
        document = self.active_document
        if document is None or self._state is not CloseState.AWAITING_DECISION:
            return
        result = self.registry.request_save(document.document_id)
        if result.status is DocumentActionStatus.PENDING:
            self._state = CloseState.WAITING_FOR_SAVE
            self._clear_issue()
            return
        if result.status in {
            DocumentActionStatus.APPLIED,
            DocumentActionStatus.NO_OP,
        } and not document.is_dirty:
            self._cursor += 1
            self._advance()
            return
        if result.status is DocumentActionStatus.REJECTED:
            issue = (
                CloseIssue.SAVE_NOT_SUPPORTED
                if "not supported" in result.message
                else CloseIssue.SAVE_CANCELLED
            )
        else:
            issue = CloseIssue.SAVE_FAILED
        self._set_issue(issue, result.message)

    def decide_discard(self) -> None:
        document = self.active_document
        if document is None or self._state is not CloseState.AWAITING_DECISION:
            return
        intent = self._intent
        if intent is not None and intent.kind in {
            CloseIntentKind.REPLACE_DOCUMENT,
            CloseIntentKind.CLOSE_PROJECT,
            CloseIntentKind.EXIT_EDITOR,
            CloseIntentKind.RESET_LAYOUT,
        }:
            self._cursor += 1
            self._advance()
            return
        result = self.registry.request_discard(document.document_id)
        if result.status is DocumentActionStatus.REJECTED:
            self._set_issue(CloseIssue.DISCARD_NOT_SUPPORTED, result.message)
            return
        if result.status is DocumentActionStatus.FAILED:
            self._set_issue(CloseIssue.DISCARD_FAILED, result.message)
            return
        if document.is_dirty:
            self._set_issue(CloseIssue.STILL_DIRTY, result.message)
            return
        self._cursor += 1
        self._advance()

    def poll(self) -> None:
        if self._state is not CloseState.WAITING_FOR_SAVE:
            return
        document = self.active_document
        if document is None:
            self._cursor += 1
            self._advance()
            return
        if not document.is_dirty:
            self._cursor += 1
            self._advance()
            return
        if self.registry.is_save_pending(document.document_id):
            return
        self._state = CloseState.AWAITING_DECISION
        self._set_issue(CloseIssue.SAVE_CANCELLED, "save was cancelled")

    def cancel(self) -> None:
        if not self.is_active:
            return
        callback = self._on_cancel
        self._reset()
        self._invoke(callback, "cancel")

    def _resolve_document_ids(self, intent: CloseIntent) -> tuple[str, ...]:
        candidates: Iterable[str]
        if intent.document_ids:
            candidates = intent.document_ids
        elif intent.view_id:
            document = self.registry.document_for_view(intent.view_id)
            if (
                document is not None
                and intent.kind is CloseIntentKind.CLOSE_VIEW
                and any(view_id != intent.view_id for view_id in document.view_ids)
            ):
                candidates = ()
            else:
                candidates = (document.document_id,) if document is not None else ()
        else:
            documents = sorted(
                self.registry.dirty_documents(),
                key=lambda document: document.kind
                in {DocumentKind.SCENE, DocumentKind.PREFAB},
            )
            candidates = (document.document_id for document in documents)
        result: list[str] = []
        seen: set[str] = set()
        for document_id in candidates:
            if document_id in seen:
                continue
            document = self.registry.get(document_id)
            if document is None or not document.is_dirty:
                continue
            seen.add(document_id)
            result.append(document_id)
        return tuple(result)

    def _advance(self) -> None:
        while self._cursor < len(self._document_ids):
            document = self.registry.get(self._document_ids[self._cursor])
            if document is not None and document.is_dirty:
                self._state = CloseState.AWAITING_DECISION
                self._clear_issue()
                return
            self._cursor += 1
        callback = self._on_complete
        self._reset()
        self._invoke(callback, "complete")

    def _set_issue(self, issue: CloseIssue, message: str) -> None:
        self._state = CloseState.AWAITING_DECISION
        self._issue = issue
        self._message = str(message or "")

    def _clear_issue(self) -> None:
        self._issue = CloseIssue.NONE
        self._message = ""

    def _reset(self) -> None:
        self._intent = None
        self._document_ids = ()
        self._cursor = 0
        self._state = CloseState.IDLE
        self._clear_issue()
        self._on_complete = None
        self._on_cancel = None

    @staticmethod
    def _invoke(callback: Optional[Callable[[], None]], action: str) -> None:
        if not callable(callback):
            return
        try:
            callback()
        except Exception as exc:
            Debug.log_suppressed(f"CloseCoordinator.{action}", exc)
