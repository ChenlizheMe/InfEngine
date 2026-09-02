from types import SimpleNamespace

import pytest

from Infernux.engine.bootstrap import EditorBootstrap


class _Registry:
    def __init__(self, materials):
        self.materials = materials

    def get_builtin_material(self, name):
        return self.materials.get(name)


class _Native:
    def __init__(self):
        self.refreshed = []

    def refresh_material_pipeline(self, material):
        self.refreshed.append(material)


def _bootstrap(native):
    bootstrap = EditorBootstrap.__new__(EditorBootstrap)
    bootstrap.engine = SimpleNamespace(get_native_engine=lambda: native)
    return bootstrap


def _install_registry(monkeypatch, registry):
    monkeypatch.setattr(
        "Infernux.lib.AssetRegistry.instance",
        lambda: registry,
    )


def test_builtin_pipeline_prewarm_refreshes_every_required_material(monkeypatch):
    native = _Native()
    materials = {"DefaultLit": object(), "SkyboxProcedural": object()}
    _install_registry(monkeypatch, _Registry(materials))

    _bootstrap(native)._prewarm_builtin_pipelines()

    assert native.refreshed == [materials["DefaultLit"], materials["SkyboxProcedural"]]


def test_builtin_pipeline_prewarm_rejects_a_missing_required_material(monkeypatch):
    _install_registry(monkeypatch, _Registry({"DefaultLit": object()}))

    with pytest.raises(RuntimeError, match="SkyboxProcedural"):
        _bootstrap(_Native())._prewarm_builtin_pipelines()


def test_builtin_pipeline_prewarm_propagates_native_refresh_failure(monkeypatch):
    class _RejectingNative:
        def refresh_material_pipeline(self, _material):
            raise RuntimeError("pipeline rejected")

    _install_registry(
        monkeypatch,
        _Registry({"DefaultLit": object(), "SkyboxProcedural": object()}),
    )

    with pytest.raises(RuntimeError, match="pipeline rejected"):
        _bootstrap(_RejectingNative())._prewarm_builtin_pipelines()
