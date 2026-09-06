from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from Infernux.engine.ui import asset_details_renderer as renderer


def test_material_load_does_not_replace_native_document_failure(monkeypatch):
    from Infernux.core.material import Material

    class NativeMaterial:
        @staticmethod
        def serialize_document():
            raise RuntimeError("native material unavailable")

    monkeypatch.setattr(
        Material,
        "load",
        lambda _path: SimpleNamespace(native=NativeMaterial()),
    )

    with pytest.raises(RuntimeError, match="native material unavailable"):
        renderer._load_material("Material.mat")


def test_material_shader_sync_does_not_hide_native_publication_failure(monkeypatch):
    from Infernux.core.material import Material

    class NativeMaterial:
        @staticmethod
        def serialize_document():
            return {"name": "Material", "properties": {}}

        @staticmethod
        def deserialize_document(_document):
            raise RuntimeError("publication failed")

    monkeypatch.setattr(
        Material,
        "load",
        lambda _path: SimpleNamespace(native=NativeMaterial()),
    )
    monkeypatch.setattr(
        renderer,
        "_sync_material_shader_metadata",
        lambda document: document["properties"].update({"roughness": 0.5}),
    )

    with pytest.raises(RuntimeError, match="publication failed"):
        renderer._load_material("Material.mat")


def test_material_refresh_does_not_keep_stale_cache_on_native_failure():
    class NativeMaterial:
        @staticmethod
        def get_version():
            raise RuntimeError("version unavailable")

    state = renderer._State()
    state.extra = {
        "native_mat": NativeMaterial(),
        "cached_data": {"name": "stale"},
        "_applied_version": 1,
    }

    with pytest.raises(RuntimeError, match="version unavailable"):
        renderer._refresh_material(state)

    assert state.extra["cached_data"] == {"name": "stale"}


def test_prefab_loader_rejects_invalid_document(tmp_path):
    prefab = tmp_path / "Broken.prefab"
    prefab.write_text(json.dumps({"name": "missing envelope"}), encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a root_object"):
        renderer._load_prefab(str(prefab))


def test_particle_graph_loader_exposes_schema_failure(tmp_path):
    from Infernux.particle.asset import ParticleGraphSchemaError

    graph = tmp_path / "Broken.particlegraph"
    graph.write_text("{}", encoding="utf-8")

    with pytest.raises(ParticleGraphSchemaError, match="keys mismatch"):
        renderer._load_particlegraph(str(graph))
