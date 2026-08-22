"""Cross-frame editor edit sessions owned by the interaction core."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from Infernux.debug import Debug


@dataclass(slots=True)
class ContinuousEditSession:
    key: str
    owner_id: str
    document_id: str
    description: str
    initial_value: Any
    current_value: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    on_commit: Optional[Callable[["ContinuousEditSession"], Optional[bool]]] = None
    on_cancel: Optional[Callable[["ContinuousEditSession"], None]] = None
    last_update_at: float = field(default_factory=time.monotonic)

    @property
    def changed(self) -> bool:
        return self.current_value != self.initial_value


class ContinuousEditService:
    """Own slider, drag, and text edits that span multiple rendered frames."""

    _instance: Optional["ContinuousEditService"] = None

    def __init__(self) -> None:
        self._sessions: dict[str, ContinuousEditSession] = {}
        ContinuousEditService._instance = self

    @classmethod
    def instance(cls) -> "ContinuousEditService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def begin(
        self,
        key: str,
        *,
        owner_id: str,
        document_id: str = "",
        description: str,
        initial_value: Any,
        metadata: Optional[dict[str, Any]] = None,
        on_commit: Optional[
            Callable[[ContinuousEditSession], Optional[bool]]
        ] = None,
        on_cancel: Optional[Callable[[ContinuousEditSession], None]] = None,
    ) -> ContinuousEditSession:
        normalized_key = str(key or "").strip()
        normalized_owner = str(owner_id or "").strip()
        if not normalized_key:
            raise ValueError("continuous edit key must not be empty")
        if not normalized_owner:
            raise ValueError("continuous edit owner must not be empty")
        existing = self._sessions.get(normalized_key)
        if existing is not None:
            return existing
        session = ContinuousEditSession(
            key=normalized_key,
            owner_id=normalized_owner,
            document_id=str(document_id or ""),
            description=str(description or "Edit Property"),
            initial_value=copy.deepcopy(initial_value),
            current_value=copy.deepcopy(initial_value),
            metadata=copy.deepcopy(metadata or {}),
            on_commit=on_commit,
            on_cancel=on_cancel,
        )
        self._sessions[normalized_key] = session
        return session

    def get(self, key: str) -> Optional[ContinuousEditSession]:
        return self._sessions.get(str(key or ""))

    def update(self, key: str, value: Any) -> bool:
        session = self.get(key)
        if session is None:
            return False
        session.current_value = copy.deepcopy(value)
        session.last_update_at = time.monotonic()
        return True

    def commit_if_idle(self, key: str, *, idle_seconds: float) -> bool:
        """Commit a session after input has stopped changing for a grace period."""
        session = self.get(key)
        if session is None:
            return False
        threshold = max(0.0, float(idle_seconds))
        if (time.monotonic() - session.last_update_at) < threshold:
            return False
        return self.commit(session.key)

    def commit(self, key: str) -> bool:
        session = self._sessions.pop(str(key or ""), None)
        if session is None:
            return False
        callback = session.on_commit
        if callback is None:
            return session.changed
        try:
            accepted = callback(session)
            if accepted is False:
                self._rollback_rejected_commit(session)
                return False
            return session.changed
        except Exception as exc:
            Debug.log_error(
                f"Continuous edit commit failed for '{session.description}': {exc}"
            )
            self._rollback_rejected_commit(session)
            return False

    @staticmethod
    def _rollback_rejected_commit(session: ContinuousEditSession) -> None:
        callback = session.on_cancel
        if callback is None:
            return
        try:
            callback(session)
        except Exception as exc:
            Debug.log_error(
                f"Continuous edit rollback failed for '{session.description}': {exc}"
            )

    def cancel(self, key: str) -> bool:
        session = self._sessions.pop(str(key or ""), None)
        if session is None:
            return False
        callback = session.on_cancel
        if callback is not None:
            try:
                callback(session)
            except Exception as exc:
                Debug.log_error(
                    f"Continuous edit rollback failed for '{session.description}': {exc}"
                )
        return True

    def commit_owner(self, owner_id: str) -> int:
        owner = str(owner_id or "")
        keys = [key for key, session in self._sessions.items() if session.owner_id == owner]
        for key in keys:
            self.commit(key)
        return len(keys)

    def cancel_owner(self, owner_id: str) -> int:
        owner = str(owner_id or "")
        keys = [key for key, session in self._sessions.items() if session.owner_id == owner]
        for key in keys:
            self.cancel(key)
        return len(keys)

    def commit_document(self, document_id: str) -> int:
        document = str(document_id or "")
        keys = [
            key
            for key, session in self._sessions.items()
            if session.document_id == document
        ]
        for key in keys:
            self.commit(key)
        return len(keys)

    def commit_all(self) -> int:
        keys = tuple(self._sessions)
        for key in keys:
            self.commit(key)
        return len(keys)

    def clear(self, *, commit: bool = True) -> None:
        if commit:
            self.commit_all()
            return
        for key in tuple(self._sessions):
            self.cancel(key)

    @property
    def active_count(self) -> int:
        return len(self._sessions)
