"""Public ShaderInfo operations exercise the real asset/renderer boundary."""
from types import SimpleNamespace

import pytest

from Infernux.application import Application
from Infernux.core.assets import AssetManager
from Infernux.core.shader import Shader
from Infernux.lib import AssetRegistry


@pytest.fixture
def shader_assets(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(AssetManager, "_engine", engine)
    monkeypatch.setattr(AssetManager, "_asset_database", engine.get_asset_database())
    monkeypatch.setattr(AssetManager, "_registry", AssetRegistry.instance())
    paths = []

    def create(name, stage="fragment", filename=None):
        path = tmp_path / (filename or (name + (".frag" if stage == "fragment" else ".vert")))
        if stage == "fragment":
            source = (f'#version 450\nShaderInfo {{ Name "{name}" Hidden On '
                      'Capabilities [Fullscreen] Outputs { Float4 outColor } }\n'
                      'void main() { outColor = vec4(1,0,0,1); }\n')
        else:
            source = (f'#version 450\nShaderInfo {{ Name "{name}" Hidden On '
                      'Capabilities [Fullscreen] }\nvoid main(){gl_Position=vec4(0,0,0,1);}\n')
        path.write_text(source, encoding="utf-8")
        result = AssetManager.import_asset(str(path))
        assert result, result.error
        paths.append(path)
        return path, result.guid

    yield create
    for path in paths:
        AssetManager.delete_asset(str(path))


def test_query_does_not_load_missing_shader(engine, monkeypatch):
    monkeypatch.setattr(AssetManager, "_engine", engine)
    assert not Shader.is_loaded("Missing_Public_Query_040", "fragment")
    assert Shader.is_loaded("default", "vertex")
    assert Shader.is_loaded("default", "fragment")
    with pytest.raises(ValueError, match="vertex or fragment"):
        engine.is_shader_loaded("default", "compute")


def test_query_tracks_resource_host_lifetime(engine, monkeypatch):
    monkeypatch.setattr(AssetManager, "_engine", None)
    assert not Shader.is_loaded("default")
    monkeypatch.setattr(AssetManager, "_engine", SimpleNamespace(_engine=engine))
    assert Shader.is_loaded("default")
    monkeypatch.setattr(AssetManager, "_engine", None)
    assert not Shader.is_loaded("default")


def test_reload_publishes_standalone_asset_by_name_and_path(shader_assets):
    path, guid = shader_assets("Public_Reload_040")
    assert not Shader.is_loaded("Public_Reload_040", "fragment")
    assert Shader.reload("Public_Reload_040", "fragment")
    assert Shader.is_loaded("Public_Reload_040", "fragment")
    registry = AssetRegistry.instance()
    version = registry.get_asset_version(guid)
    path.write_text(path.read_text(encoding="utf-8").replace("vec4(1,0,0,1)", "vec4(0,1,0,1)"), encoding="utf-8")
    assert Shader.reload(str(path))
    assert registry.get_asset_version(guid) > version
    assert Shader.is_loaded("Public_Reload_040", "fragment")


def test_reload_rejects_unregistered_and_wrong_stage(shader_assets):
    path, _ = shader_assets("Public_Wrong_Stage_040")
    with pytest.raises(FileNotFoundError, match="not registered"):
        Shader.reload("Unregistered_Shader_040")
    with pytest.raises(ValueError, match="matching ShaderInfo"):
        Shader.reload(str(path), "vertex")


def test_project_relative_path_does_not_depend_on_cwd(shader_assets, monkeypatch):
    path, _ = shader_assets("Public_Relative_040")
    database = AssetManager.require_asset_database()
    import os
    relative = os.path.relpath(path, database.project_root)
    monkeypatch.chdir(path.parent.parent)
    assert Shader.reload(relative, "fragment")


def test_duplicate_name_requires_explicit_path(shader_assets):
    first, _ = shader_assets("Public_Duplicate_040", filename="First.frag")
    shader_assets("Public_Duplicate_040", filename="Second.frag")
    with pytest.raises(ValueError, match="Ambiguous"):
        Shader.reload("Public_Duplicate_040")
    assert Shader.reload(str(first))


def test_same_name_in_two_stages_reloads_both(shader_assets):
    shader_assets("Public_Pair_040", "vertex")
    shader_assets("Public_Pair_040", "fragment")
    assert Shader.reload("Public_Pair_040")
    assert Shader.is_loaded("Public_Pair_040", "vertex")
    assert Shader.is_loaded("Public_Pair_040", "fragment")
    for stage in ("vertex", "fragment"):
        assert Shader.reload("Public_Pair_040", stage)
        assert Shader.is_loaded("Public_Pair_040", "vertex")
        assert Shader.is_loaded("Public_Pair_040", "fragment")


def test_reload_reports_compile_failure(shader_assets):
    path, _ = shader_assets("Public_Broken_040")
    assert Shader.reload(str(path))
    path.write_text(path.read_text(encoding="utf-8").replace("outColor =", "outColor = invalid_symbol +"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Shader reload failed"):
        Shader.reload(str(path))
    assert Shader.is_loaded("Public_Broken_040", "fragment")


def test_frozen_player_reload_is_not_an_authoring_write(monkeypatch):
    monkeypatch.setattr(Application, "is_player", staticmethod(lambda: True))
    with pytest.raises(RuntimeError, match="read-only"):
        Shader.reload("Assets/Shader.frag")


def test_reload_without_resource_host_fails(monkeypatch):
    monkeypatch.setattr(AssetManager, "_asset_database", None)
    with pytest.raises(RuntimeError, match="not initialized"):
        Shader.reload("NotInitialized")


def test_no_unbound_raw_shader_api():
    for retired in ("_engine", "_set_engine", "invalidate", "refresh_materials", "load_spirv"):
        assert not hasattr(Shader, retired)
