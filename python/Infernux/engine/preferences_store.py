"""
Shared preferences storage for the Infernux editor.

This module provides a minimal JSON-backed preference store used by
different preference classes:

- preferences file: Documents/Infernux/preferences.json
- load the whole JSON object
- update only owned fields
- keep unrelated fields intact
"""

from __future__ import annotations

import json
import os
import pathlib

_PREFS_FILE = "preferences.json"


def _prefs_path() -> str:
    """Return the path to the global preferences file."""
    if os.name == "nt":
        import ctypes.wintypes

        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
        if result != 0 or not buf.value:
            raise OSError(result, "Windows did not resolve the Documents directory")
        docs = pathlib.Path(buf.value)
    else:
        docs = pathlib.Path.home() / "Documents"

    prefs_dir = docs / "Infernux"
    os.makedirs(prefs_dir, exist_ok=True)
    return str(prefs_dir / _PREFS_FILE)


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
