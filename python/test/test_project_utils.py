from Infernux.engine.ui import project_utils


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

    project_utils.open_file_with_system(str(script), project_root=str(tmp_path))

    assert refreshes == [True]
    assert launches == [("vscode", str(script), str(tmp_path))]
