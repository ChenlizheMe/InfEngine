"""Small, deterministic state machines for editor text input boundaries."""

from __future__ import annotations


class ImeInputState:
    """Accept IME commits once while preserving legitimate repeated text."""

    def __init__(self) -> None:
        self._composition_id = ""
        self._commits: set[tuple[str, str]] = set()
        self._key_events: set[tuple[str, str]] = set()

    def begin_composition(self, composition_id: str) -> None:
        identifier = str(composition_id or "").strip()
        if identifier != self._composition_id:
            self._composition_id = identifier
            self._commits.clear()
            self._key_events.clear()

    def commit(
        self,
        text: str,
        *,
        composition_id: str = "",
        commit_id: str = "",
    ) -> str:
        value = str(text or "")
        if not value:
            return ""
        composition = str(composition_id or self._composition_id).strip()
        identifier = str(commit_id or "").strip()
        # Without composition metadata, repeated characters are legitimate.
        if not composition:
            return value
        key = (composition, identifier or value)
        if key in self._commits:
            return ""
        self._commits.add(key)
        return value

    def accept_key_down(
        self,
        key: str,
        *,
        event_id: str = "",
        repeat: bool = False,
        text_input_active: bool = False,
    ) -> bool:
        """Return whether a key edge may reach editor shortcut handling."""
        if text_input_active or repeat:
            return False
        identifier = str(event_id or "").strip()
        if not identifier:
            return True
        key_event = (str(key or "").strip().upper(), identifier)
        if key_event in self._key_events:
            return False
        self._key_events.add(key_event)
        return True

    def reset(self) -> None:
        self._composition_id = ""
        self._commits.clear()
        self._key_events.clear()


__all__ = ["ImeInputState"]
