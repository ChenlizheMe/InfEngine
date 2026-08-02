from __future__ import annotations

from Infernux.engine.project_context import get_project_root, set_project_root
from Infernux.scene import SceneManager


def test_load_scene_accepts_name_filename_and_project_path(tmp_path, monkeypatch):
    project = tmp_path / "Project"
    scenes_dir = project / "Assets" / "Scenes"
    scenes_dir.mkdir(parents=True)
    start = scenes_dir / "Start.scene"
    gallery = scenes_dir / "VFX Gallery.scene"
    start.write_text("{}", encoding="utf-8")
    gallery.write_text("{}", encoding="utf-8")
    scenes = [str(start), str(gallery)]
    loaded: list[str] = []
    previous_root = get_project_root()

    try:
        set_project_root(str(project))
        monkeypatch.setattr(SceneManager, "_load_build_list", staticmethod(lambda: scenes))
        monkeypatch.setattr(SceneManager, "_is_in_play_mode", staticmethod(lambda: False))
        monkeypatch.setattr(
            SceneManager,
            "_do_load",
            staticmethod(lambda path: loaded.append(path) or True),
        )

        references = [
            "VFX Gallery",
            "vfx gallery.scene",
            "Assets/Scenes/VFX Gallery.scene",
            "assets\\scenes\\vfx gallery.scene",
            str(gallery),
        ]
        for reference in references:
            assert SceneManager.load_scene(reference) is True
            assert loaded[-1] == str(gallery)
    finally:
        set_project_root(previous_root)


def test_path_reference_does_not_fall_back_to_an_unrelated_basename(tmp_path):
    project = tmp_path / "Project"
    gallery = project / "Assets" / "Scenes" / "VFX Gallery.scene"
    gallery.parent.mkdir(parents=True)
    gallery.write_text("{}", encoding="utf-8")
    previous_root = get_project_root()

    try:
        set_project_root(str(project))
        assert SceneManager._resolve_build_scene(
            "Assets/Other/VFX Gallery.scene",
            [str(gallery)],
        ) is None
    finally:
        set_project_root(previous_root)
