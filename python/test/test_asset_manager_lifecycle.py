from __future__ import annotations

from Infernux.core.assets import AssetManager


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
    monkeypatch.setattr(AssetManager, "_pending_document_writes", {"asset": object()})
    monkeypatch.setattr(AssetManager, "_meta_write_suppression", {"asset": 1.0})

    AssetManager.release_engine(engine)

    assert AssetManager._engine is None
    assert AssetManager._asset_database is None
    assert AssetManager._registry is None
    assert AssetManager._cache == {}
    assert AssetManager._texture_cache == {}
    assert AssetManager._pending_gpu_texture_reloads == {}
    assert AssetManager._scheduled_saves == {}
    assert AssetManager._pending_document_writes == {}
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
