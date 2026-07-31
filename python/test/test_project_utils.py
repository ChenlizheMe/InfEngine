import platform

from Infernux.engine.ui import project_utils
from Infernux.engine.ui.project_file_ops import copy_path_as_new_asset
from Infernux.particle import ParticleGraphAsset


def test_code_file_open_refreshes_ides_and_uses_preference(tmp_path, monkeypatch):
    script = tmp_path / "player.py"
    script.write_text("pass\n", encoding="utf-8")

    refreshes = []
    launches = []
    monkeypatch.setattr(project_utils, "get_ide", lambda: "vscode")
    monkeypatch.setattr(
        project_utils,
        "detect_available_ides",
        lambda force_refresh=False: refreshes.append(force_refresh) or ["vscode", "pycharm"],
    )
    monkeypatch.setattr(
        project_utils,
        "open_in_vscode",
        lambda path, line=0, project_root="": launches.append(
            ("vscode", path, project_root)
        )
        or True,
    )
    monkeypatch.setattr(
        project_utils,
        "open_in_pycharm",
        lambda *args, **kwargs: launches.append(("pycharm", args, kwargs)) or True,
    )

    assert project_utils.open_file_with_system(str(script), project_root=str(tmp_path)) is True

    assert refreshes == [True]
    assert launches == [("vscode", str(script), str(tmp_path))]


def test_system_open_failure_does_not_escape_ui_render(tmp_path, monkeypatch):
    model = tmp_path / "model.obj"
    model.write_text("o Model\n", encoding="utf-8")
    warnings = []

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        project_utils.os,
        "startfile",
        lambda _path: (_ for _ in ()).throw(OSError(1155, "no associated application")),
        raising=False,
    )
    monkeypatch.setattr(project_utils.Debug, "log_warning", warnings.append)

    assert project_utils.open_file_with_system(str(model)) is False
    assert len(warnings) == 1
    assert str(model) in warnings[0]


def test_particle_graph_copy_regenerates_only_asset_identity(tmp_path):
    source = tmp_path / "Source.particlegraph"
    destination = tmp_path / "Copied.particlegraph"
    original = ParticleGraphAsset(name="Source")
    original.save(str(source))

    copied_path = copy_path_as_new_asset(str(source), str(destination))
    copied = ParticleGraphAsset.load(str(destination))

    assert copied_path == str(destination.resolve())
    assert copied.stable_id != original.stable_id
    assert copied.name == "Copied"
    assert tuple(emitter.stable_id for emitter in copied.emitters) == tuple(
        emitter.stable_id for emitter in original.emitters
    )
    assert destination.with_suffix(".particlegraph.meta").exists() is False


def test_folder_copy_excludes_meta_and_regenerates_nested_particle_graph(tmp_path):
    source = tmp_path / "Source"
    destination = tmp_path / "Copied"
    source.mkdir()
    graph_path = source / "Smoke.particlegraph"
    graph = ParticleGraphAsset(name="Smoke")
    graph.save(str(graph_path))
    (source / "Smoke.particlegraph.meta").write_text("stale-guid", encoding="utf-8")
    (source / "note.txt").write_text("hello", encoding="utf-8")

    assert copy_path_as_new_asset(str(source), str(destination)) == str(
        destination.resolve()
    )
    copied = ParticleGraphAsset.load(str(destination / "Smoke.particlegraph"))
    assert copied.stable_id != graph.stable_id
    assert (destination / "Smoke.particlegraph.meta").exists() is False
    assert (destination / "note.txt").read_text(encoding="utf-8") == "hello"
