"""Focus and input-context state shared by all editor panels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True, slots=True)
class InputContext:
    context_id: str
    owner_id: str
    priority: int = 0
    blocks_lower: bool = False

    def __post_init__(self) -> None:
        if not str(self.context_id).strip():
            raise ValueError("input context_id must not be empty")
        if not str(self.owner_id).strip():
            raise ValueError("input context owner_id must not be empty")


class InputContextStack:
    """Deterministic context precedence for command and shortcut routing."""

    def __init__(self) -> None:
        self._contexts: list[InputContext] = []

    def push(self, context: InputContext) -> None:
        self.remove(context.context_id)
        self._contexts.append(context)

    def remove(self, context_id: str) -> None:
        self._contexts = [
            context for context in self._contexts
            if context.context_id != context_id
        ]

    def remove_owner(self, owner_id: str) -> None:
        self._contexts = [
            context for context in self._contexts
            if context.owner_id != owner_id
        ]

    def ordered(self) -> tuple[InputContext, ...]:
        indexed = list(enumerate(self._contexts))
        indexed.sort(key=lambda item: (item[1].priority, item[0]), reverse=True)
        result: list[InputContext] = []
        for _, context in indexed:
            result.append(context)
            if context.blocks_lower:
                break
        return tuple(result)


@dataclass(frozen=True, slots=True)
class FocusSnapshot:
    active_panel_id: str = ""
    active_view_id: str = ""
    active_document_id: str = ""
    child_context_id: str = ""
    capture_owner_id: str = ""


class FocusService:
    """Single authority for editor focus, focus requests, and child context."""

    _instance: Optional["FocusService"] = None

    def __init__(self) -> None:
        self._snapshot = FocusSnapshot()
        self._pending_panel_id = ""
        self._revision = 0
        self._listeners: list[Callable[[FocusSnapshot], None]] = []
        self.input_contexts = InputContextStack()
        FocusService._instance = self

    @classmethod
    def instance(cls) -> "FocusService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def snapshot(self) -> FocusSnapshot:
        return self._snapshot

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def pending_panel_id(self) -> str:
        return self._pending_panel_id

    def add_listener(self, callback: Callable[[FocusSnapshot], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[FocusSnapshot], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def request_panel_focus(self, panel_id: str) -> bool:
        panel_id = str(panel_id or "").strip()
        if not panel_id:
            return False
        changed = self._pending_panel_id != panel_id
        self._pending_panel_id = panel_id
        return changed

    def consume_panel_focus_request(self, panel_id: str) -> bool:
        panel_id = str(panel_id or "").strip()
        if not panel_id or self._pending_panel_id != panel_id:
            return False
        self._pending_panel_id = ""
        return True

    def activate_panel(
        self,
        panel_id: str,
        *,
        view_id: Optional[str] = None,
        document_id: Optional[str] = None,
        child_context_id: Optional[str] = None,
    ) -> bool:
        panel_id = str(panel_id or "").strip()
        if not panel_id:
            return False
        current = self._snapshot
        preserve = current.active_panel_id == panel_id
        next_snapshot = FocusSnapshot(
            panel_id,
            current.active_view_id if view_id is None and preserve else str(view_id or ""),
            current.active_document_id if document_id is None and preserve else str(document_id or ""),
            current.child_context_id if child_context_id is None and preserve else str(child_context_id or ""),
            self._snapshot.capture_owner_id,
        )
        return self._apply(next_snapshot)

    def deactivate_panel(self, panel_id: str) -> bool:
        panel_id = str(panel_id or "").strip()
        if self._snapshot.active_panel_id != panel_id:
            return False
        self.input_contexts.remove_owner(panel_id)
        return self._apply(FocusSnapshot(capture_owner_id=self._snapshot.capture_owner_id))

    def set_child_context(self, panel_id: str, context_id: str) -> bool:
        if self._snapshot.active_panel_id != str(panel_id or "").strip():
            return False
        current = self._snapshot
        return self._apply(FocusSnapshot(
            current.active_panel_id,
            current.active_view_id,
            current.active_document_id,
            str(context_id or ""),
            current.capture_owner_id,
        ))

    def set_capture_owner(self, owner_id: str) -> bool:
        current = self._snapshot
        return self._apply(FocusSnapshot(
            current.active_panel_id,
            current.active_view_id,
            current.active_document_id,
            current.child_context_id,
            str(owner_id or ""),
        ))

    def _apply(self, snapshot: FocusSnapshot) -> bool:
        if snapshot == self._snapshot:
            return False
        self._snapshot = snapshot
        self._revision += 1
        for callback in tuple(self._listeners):
            try:
                callback(snapshot)
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("FocusService.listener", exc)
        return True
