from __future__ import annotations

import Infernux.lib as native_lib
from Infernux.core.assets import AssetManager
import pytest


def test_require_asset_database_rejects_uninitialized_lifecycle(monkeypatch):
    monkeypatch.setattr(AssetManager, "_asset_database", None)

    with pytest.raises(RuntimeError, match="not initialized with an AssetDatabase"):
        AssetManager.require_asset_database()


def test_initialize_rejects_engine_without_native_asset_database(monkeypatch):
    monkeypatch.setattr(AssetManager, "_engine", None)
    monkeypatch.setattr(AssetManager, "_asset_database", None)

    with pytest.raises(RuntimeError, match="initialized native engine"):
        AssetManager.initialize(object())

    assert AssetManager._engine is None
    assert AssetManager._asset_database is None


def test_initialize_rejects_missing_native_asset_database(monkeypatch):
    class NativeEngine:
        @staticmethod
        def get_asset_database():
            return None

    monkeypatch.setattr(AssetManager, "_engine", None)
    monkeypatch.setattr(AssetManager, "_asset_database", None)

    with pytest.raises(RuntimeError, match="initialized AssetDatabase"):
        AssetManager.initialize(NativeEngine())

    assert AssetManager._engine is None
    assert AssetManager._asset_database is None


def test_release_engine_clears_native_and_project_lifetime_state(monkeypatch):
    engine = object()
    monkeypatch.setattr(AssetManager, "_engine", engine)
    monkeypatch.setattr(AssetManager, "_asset_database", object())
    monkeypatch.setattr(AssetManager, "_registry", object())
    monkeypatch.setattr(AssetManager, "_cache", {"asset": object()})
    monkeypatch.setattr(AssetManager, "_texture_cache", {"texture": object()})
    monkeypatch.setattr(
        AssetManager, "_pending_gpu_texture_reloads", {"texture": "path"}
    )
    monkeypatch.setattr(AssetManager, "_scheduled_saves", {"asset": object()})
    monkeypatch.setattr(
        AssetManager, "_pending_document_write_records", {"asset": [object()]}
    )
    monkeypatch.setattr(AssetManager, "_meta_write_suppression", {"asset": 1.0})

    AssetManager.release_engine(engine)

    assert AssetManager._engine is None
    assert AssetManager._asset_database is None
    assert AssetManager._registry is None
    assert AssetManager._cache == {}
    assert AssetManager._texture_cache == {}
    assert AssetManager._pending_gpu_texture_reloads == {}
    assert AssetManager._scheduled_saves == {}
    assert AssetManager._pending_document_write_records == {}
    assert AssetManager._meta_write_suppression == {}


def test_release_engine_does_not_clear_a_newer_engine(monkeypatch):
    previous = object()
    current = object()
    database = object()
    monkeypatch.setattr(AssetManager, "_engine", current)
    monkeypatch.setattr(AssetManager, "_asset_database", database)

    AssetManager.release_engine(previous)

    assert AssetManager._engine is current
    assert AssetManager._asset_database is database


@pytest.mark.parametrize(
    "operation",
    [
        lambda: AssetManager.find_assets("*.mat"),
        lambda: AssetManager._get_guid_from_path("Assets/Test.mat"),
        lambda: AssetManager._get_path_from_guid("test-guid"),
    ],
)
def test_asset_database_failures_are_not_suppressed(monkeypatch, operation):
    class BrokenDatabase:
        def get_all_guids(self):
            raise RuntimeError("asset database failed")

        def get_guid_from_path(self, _path):
            raise RuntimeError("asset database failed")

        def get_path_from_guid(self, _guid):
            raise RuntimeError("asset database failed")

    monkeypatch.setattr(AssetManager, "_asset_database", BrokenDatabase())

    with pytest.raises(RuntimeError, match="asset database failed"):
        operation()


def test_registry_resolution_failure_is_not_suppressed(monkeypatch):
    class BrokenRegistry:
        @staticmethod
        def instance():
            raise RuntimeError("registry failed")

    monkeypatch.setattr(native_lib, "AssetRegistry", BrokenRegistry)

    with pytest.raises(RuntimeError, match="registry failed"):
        AssetManager._resolve_registry()
