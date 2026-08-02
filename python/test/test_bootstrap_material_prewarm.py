from pathlib import Path

from Infernux.engine.bootstrap import _iter_project_material_paths


def test_material_preview_prewarm_only_scans_project_assets(tmp_path):
    asset_material = tmp_path / "Assets" / "Materials" / "visible.mat"
    runtime_material = (
        tmp_path
        / ".venv"
        / "Lib"
        / "site-packages"
        / "Infernux"
        / "resources"
        / "materials"
        / "internal.mat"
    )
    cache_material = tmp_path / "Library" / "Preview" / "cached.mat"

    for path in (asset_material, runtime_material, cache_material):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    found = [Path(path) for path in _iter_project_material_paths(str(tmp_path))]

    assert found == [asset_material]


def test_material_preview_prewarm_skips_hidden_asset_directories(tmp_path):
    hidden_material = tmp_path / "Assets" / ".generated" / "hidden.mat"
    visible_material = tmp_path / "Assets" / "visible.MAT"
    hidden_material.parent.mkdir(parents=True)
    hidden_material.write_text("{}", encoding="utf-8")
    visible_material.write_text("{}", encoding="utf-8")

    found = [Path(path) for path in _iter_project_material_paths(str(tmp_path))]

    assert found == [visible_material]
