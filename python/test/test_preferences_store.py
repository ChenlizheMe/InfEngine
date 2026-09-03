from __future__ import annotations

import json
import os

import pytest

from Infernux.engine.preferences_store import PreferencesStore, _prefs_path
from Infernux.engine.user_data import (
    get_infernux_data_root,
    get_project_editor_layout_root,
)


def _store_at(path) -> PreferencesStore:
    store = object.__new__(PreferencesStore)
    store._path = str(path)
    return store


def test_preferences_use_configured_infernux_data_root(tmp_path, monkeypatch):
    root = tmp_path / "hub-data"
    monkeypatch.setenv("INFERNUX_DATA_ROOT", str(root))

    assert get_infernux_data_root() == str(root.resolve())
    assert _prefs_path() == os.path.join(
        str(root.resolve()), "State", "Editor", "preferences.json"
    )


def test_editor_layout_is_owned_by_the_project(tmp_path):
    project = tmp_path / "same-name" / "project"

    assert get_project_editor_layout_root(project) == os.path.join(
        str(project.resolve()), "Cache", "Editor", "Layout"
    )


def test_missing_preferences_file_has_empty_current_state(tmp_path):
    store = _store_at(tmp_path / "preferences.json")

    assert store.load() == {}


def test_preferences_require_a_json_object(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text("[]\n", encoding="utf-8")
    store = _store_at(path)

    with pytest.raises(TypeError, match="must contain a JSON object"):
        store.load()


def test_invalid_preferences_json_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text("{\n", encoding="utf-8")
    store = _store_at(path)

    with pytest.raises(json.JSONDecodeError):
        store.load()


def test_set_preserves_unowned_preferences(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text('{"language": "zh"}\n', encoding="utf-8")
    store = _store_at(path)

    store.set("preferred_ide", "vscode")

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "language": "zh",
        "preferred_ide": "vscode",
    }


def test_save_failure_reaches_the_caller(tmp_path, monkeypatch):
    store = _store_at(tmp_path / "preferences.json")

    def fail_write(_path, _text):
        raise OSError("disk rejected write")

    monkeypatch.setattr(
        "Infernux.core.document_store.write_document_text",
        fail_write,
    )

    with pytest.raises(OSError, match="disk rejected write"):
        store.save({"language": "zh"})


def test_save_rejects_non_dictionary_state(tmp_path):
    store = _store_at(tmp_path / "preferences.json")

    with pytest.raises(TypeError, match="must be a dictionary"):
        store.save([])
