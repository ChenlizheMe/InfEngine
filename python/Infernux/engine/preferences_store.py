"""
Shared preferences storage for the Infernux editor.

This module provides a minimal JSON-backed preference store used by
different preference classes:

- preferences file: <Infernux data root>/State/Editor/preferences.json
- load the whole JSON object
- update only owned fields
- keep unrelated fields intact
"""

from __future__ import annotations

import json
import os

from Infernux.engine.user_data import get_infernux_data_root

_PREFS_FILE = "preferences.json"


def _prefs_path() -> str:
    """Return the path to the global preferences file."""
    prefs_dir = os.path.join(get_infernux_data_root(), "State", "Editor")
    os.makedirs(prefs_dir, exist_ok=True)
    return os.path.join(prefs_dir, _PREFS_FILE)


class PreferencesStore:
    """Minimal JSON-backed preferences storage."""

    _instance: PreferencesStore | None = None
    _initialized: bool = False

    def __new__(cls) -> PreferencesStore:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if self.__class__._initialized:
            return
        self._path = _prefs_path()
        self.__class__._initialized = True

    def load(self) -> dict:
        """Load and return the full preferences dictionary."""
        if not os.path.isfile(self._path):
            return {}

        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise TypeError("preferences.json must contain a JSON object")
        return data

    def save(self, data: dict) -> None:
        """Save the full preferences dictionary."""
        if not isinstance(data, dict):
            raise TypeError("preferences must be a dictionary")
        from Infernux.core.document_store import write_document_text

        write_document_text(
            self._path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        )

    def get(self, key: str, default=None):
        """Return a single preference value."""
        data = self.load()
        return data.get(key, default)

    def set(self, key: str, value) -> None:
        """Update one preference key without overwriting unrelated keys."""
        data = self.load()
        data[key] = value
        self.save(data)
