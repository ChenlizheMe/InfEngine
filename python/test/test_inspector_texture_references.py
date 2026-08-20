from __future__ import annotations


def test_project_texture_reference_resolves_relative_to_project_root(
    tmp_path, monkeypatch
):
    from Infernux.engine import project_context
    from Infernux.engine.ui import _inspector_references as references

    project = tmp_path / "Game"
    texture = project / "Assets" / "VFX" / "Smoke.tga"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"tga")
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(project))
    monkeypatch.setattr(references, "_asset_guid_from_path", lambda _path: "smoke-guid")

    guid, path = references._project_texture_guid_and_path(
        "Assets/VFX/Smoke.tga"
    )

    assert guid == "smoke-guid"
    assert path == str(texture)


def test_project_texture_reference_rejects_engine_owned_icons(tmp_path, monkeypatch):
    from Infernux.engine import project_context
    from Infernux.engine.ui import _inspector_references as references

    project = tmp_path / "Game"
    (project / "Assets").mkdir(parents=True)
    icon = tmp_path / "Engine" / "resources" / "icons" / "camera.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"png")
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(project))

    assert references._project_texture_guid_and_path(str(icon)) == ("", "")
