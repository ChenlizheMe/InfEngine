from __future__ import annotations

from Infernux.engine.project_view_settings import (
    load_project_view_settings,
    write_project_view_settings_section,
)


def test_shared_project_view_settings_preserve_other_sections(tmp_path):
    path = tmp_path / "ProjectSettings" / "GameView.ini"

    write_project_view_settings_section(path, "UIEditor", {"zoom": "1.25"})
    write_project_view_settings_section(path, "GameView", {"preset_index": "2"})
    parser = load_project_view_settings(path)
    assert parser["UIEditor"]["zoom"] == "1.25"
    assert parser["GameView"]["preset_index"] == "2"

    write_project_view_settings_section(path, "UIEditor", {"zoom": "0.75"})
    parser = load_project_view_settings(path)
    assert parser["UIEditor"]["zoom"] == "0.75"
    assert parser["GameView"]["preset_index"] == "2"
