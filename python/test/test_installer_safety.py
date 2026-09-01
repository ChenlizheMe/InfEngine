from __future__ import annotations

import importlib
from pathlib import Path


def _load_installer_safety(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "packaging"))
    return importlib.import_module("installer_safety")


def test_installer_safety_rejects_project_directories(tmp_path, monkeypatch):
    safety = _load_installer_safety(monkeypatch)
    project_dir = tmp_path / "Project"
    (project_dir / "Assets").mkdir(parents=True)
    (project_dir / "ProjectSettings").mkdir()

    assert safety.is_dangerous_install_target(str(project_dir))
    assert "project" in safety.install_target_error(str(project_dir)).lower()


def test_installer_safety_rejects_infernux_cache_directories(tmp_path, monkeypatch):
    safety = _load_installer_safety(monkeypatch)
    cache_dir = tmp_path / ".infernux"
    (cache_dir / "versions").mkdir(parents=True)
    (cache_dir / "projects.db").write_text("", encoding="utf-8")

    assert safety.is_dangerous_install_target(str(cache_dir))
    assert "user data" in safety.install_target_error(str(cache_dir)).lower()


def test_installer_safety_only_removes_marked_install_dirs(tmp_path, monkeypatch):
    safety = _load_installer_safety(monkeypatch)
    install_dir = tmp_path / "Infernux Hub"
    install_dir.mkdir()

    assert not safety.can_remove_install_dir(str(install_dir))

    safety.write_install_marker(str(install_dir))

    assert safety.can_remove_install_dir(str(install_dir))


def test_installer_safety_does_not_infer_install_ownership_from_files(
    tmp_path, monkeypatch
):
    safety = _load_installer_safety(monkeypatch)
    install_dir = tmp_path / "Infernux Hub"
    data_dir = install_dir / "InfernuxHubData"
    data_dir.mkdir(parents=True)
    (install_dir / "Infernux Hub.exe").write_text("", encoding="utf-8")

    assert not safety.is_recognized_install_dir(str(install_dir))
    assert not safety.can_remove_install_dir(str(install_dir))
