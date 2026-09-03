"""
Shared preferences storage for the Infernux editor.

This module provides a minimal JSON-backed preference store used by
different preference classes. It preserves the original persistence logic:

- preferences file: <Infernux data root>/State/Editor/preferences.json
- load the whole JSON object
- update only owned fields
- keep unrelated fields intact
"""

from __future__ import annotations

from typing import Any

_PREFS_FILE: str

def _prefs_path() -> str: ...

class PreferencesStore:
    _path: str

    def __init__(self) -> None: ...
    def load(self) -> dict[Any, Any]: ...
    def save(self, data: dict[Any, Any]) -> None: ...
    def get(self, key: str, default: Any = ...) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
