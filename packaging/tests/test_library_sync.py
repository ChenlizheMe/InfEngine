from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "python" / "Infernux" / "engine" / "library_sync.py"


def _load_library_sync():
    spec = importlib.util.spec_from_file_location(
        "Infernux.engine.library_sync_test", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_rebuilds_library_without_packaged_metadata(tmp_path: Path, monkeypatch):
    source = tmp_path / "package-resources"
    shader_dir = source / "shaders"
    shader_dir.mkdir(parents=True)
    (shader_dir / "surface.glsl").write_text("void main() {}", encoding="utf-8")
    (shader_dir / "surface.glsl.meta").write_text('{"meta_version": 2}', encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")

    project = tmp_path / "project"
    stale_library = project / "Library" / "Resources"
    stale_library.mkdir(parents=True)
    (stale_library / "stale.txt").write_text("stale", encoding="utf-8")
    (stale_library / "surface.glsl.meta").write_text("stale", encoding="utf-8")

    fake_package = types.ModuleType("Infernux")
    fake_package.__path__ = []
    fake_engine = types.ModuleType("Infernux.engine")
    fake_engine.__path__ = [str(MODULE_PATH.parent)]
    fake_resources = types.ModuleType("Infernux.resources")
    fake_resources.get_package_resources_path = lambda: str(source)
    monkeypatch.setitem(sys.modules, "Infernux", fake_package)
    monkeypatch.setitem(sys.modules, "Infernux.engine", fake_engine)
    monkeypatch.setitem(sys.modules, "Infernux.resources", fake_resources)

    module = _load_library_sync()
    result = Path(module.sync_resources(str(project)))

    assert result == stale_library
    assert (result / "shaders" / "surface.glsl").is_file()
    assert not (result / "shaders" / "surface.glsl.meta").exists()
    assert not (result / "stale.txt").exists()
    assert not (result / "surface.glsl.meta").exists()
    assert not (result / "__init__.py").exists()
