from __future__ import annotations

import importlib
import json
import zipfile
from pathlib import Path


def _load_project_model(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "packaging"))
    monkeypatch.syspath_prepend(str(repo_root / "packaging" / "model"))
    return importlib.import_module("project_model")


def _load_embed_runtime_manager(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "packaging"))
    return importlib.import_module("embed_runtime_manager")


class _FakeVersionManager:
    def __init__(self, wheel_path: str) -> None:
        self._wheel_path = wheel_path

    def get_wheel_path(
        self, _engine_version: str, _python_version: str | None = None
    ) -> str:
        return self._wheel_path


def test_vscode_workspace_uses_current_pyright_interpreter_settings(
    tmp_path, monkeypatch
):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "is_frozen", lambda: False)
    project_dir = tmp_path / "project"

    project_model.ProjectModel._create_vscode_workspace(str(project_dir))

    settings = json.loads(
        (project_dir / ".vscode" / "settings.json").read_text(encoding="utf-8")
    )
    pyright = json.loads(
        (project_dir / "pyrightconfig.json").read_text(encoding="utf-8")
    )
    assert settings["python.defaultInterpreterPath"] == (
        project_model.ProjectModel._vscode_python_path(str(project_dir))
    )
    assert "python.pythonPath" not in settings
    assert pyright["venvPath"] == "."
    assert pyright["venv"] == ".venv"
    assert "pythonPath" not in pyright


def _write_infernux_wheel(path: Path, version: str = "0.1.6") -> None:
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("Infernux/__init__.py", "__version__ = '0.1.6'\n")
        wheel.writestr("Infernux/lib/__init__.py", "")
        wheel.writestr("Infernux/lib/_Infernux.cp312-win_amd64.pyd", b"native")
        wheel.writestr(
            f"infernux-{version}.dist-info/METADATA",
            f"Name: Infernux\nVersion: {version}\nRequires-Dist: numpy>=1.21.0\n",
        )
        wheel.writestr(f"infernux-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        wheel.writestr(f"infernux-{version}.dist-info/RECORD", "")


def test_frozen_project_runtime_installs_infernux_by_extracting_wheel(tmp_path, monkeypatch):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "is_frozen", lambda: True)

    def fail_metadata_check(_python: str, _name: str) -> str:
        raise AssertionError("metadata check should be skipped")

    monkeypatch.setattr(project_model, "_installed_distribution_version", fail_metadata_check)
    monkeypatch.setattr(project_model.ProjectModel, "validate_python_runtime", staticmethod(lambda _python: None))

    wheel_path = tmp_path / "infernux-0.1.6-cp312-cp312-win_amd64.whl"
    _write_infernux_wheel(wheel_path)
    project_dir = tmp_path / "project"
    runtime_dir = project_dir / ".runtime" / "python312"
    runtime_dir.mkdir(parents=True)
    project_python = Path(
        project_model.ProjectModel._get_project_python(str(project_dir))
    )
    site_packages = Path(
        project_model.ProjectModel._get_site_packages(str(project_dir))
    )
    (site_packages / "numpy").mkdir(parents=True)
    project_python.parent.mkdir(parents=True, exist_ok=True)
    project_python.write_text("", encoding="utf-8")

    captured_args: list[list[str]] = []

    def fake_run_hidden(args: list[str], *, timeout: int):
        captured_args.append(args)

    monkeypatch.setattr(project_model, "_run_hidden", fake_run_hidden)

    model = project_model.ProjectModel(None, version_manager=_FakeVersionManager(str(wheel_path)))
    model._install_infernux_in_runtime(str(project_dir), "0.1.6")

    assert captured_args == []
    assert (site_packages / "Infernux" / "__init__.py").is_file()
    assert (site_packages / "Infernux" / "lib" / "_Infernux.cp312-win_amd64.pyd").is_file()
    assert (site_packages / "infernux-0.1.6.dist-info" / "METADATA").is_file()
    assert (site_packages / "numpy").is_dir()


def test_matching_frozen_project_runtime_skips_reinstall_when_native_import_valid(tmp_path, monkeypatch):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "is_frozen", lambda: True)
    monkeypatch.setattr(project_model, "_installed_distribution_version", lambda _python, _name: "0.1.6")
    monkeypatch.setattr(project_model.ProjectModel, "validate_python_runtime", staticmethod(lambda _python: None))

    wheel_path = tmp_path / "infernux-0.1.6-cp312-cp312-win_amd64.whl"
    wheel_path.write_bytes(b"wheel")
    project_dir = tmp_path / "project"
    runtime_dir = project_dir / ".runtime" / "python312"
    runtime_dir.mkdir(parents=True)
    project_python = Path(
        project_model.ProjectModel._get_project_python(str(project_dir))
    )
    site_packages = Path(
        project_model.ProjectModel._get_site_packages(str(project_dir))
    )
    (site_packages / "infernux-0.1.6.dist-info").mkdir(parents=True)
    project_python.parent.mkdir(parents=True, exist_ok=True)
    project_python.write_text("", encoding="utf-8")

    def fail_run_hidden(_args: list[str], *, timeout: int):
        raise AssertionError("pip should not run for a valid matching runtime")

    monkeypatch.setattr(project_model, "_run_hidden", fail_run_hidden)

    model = project_model.ProjectModel(None, version_manager=_FakeVersionManager(str(wheel_path)))
    model._install_infernux_in_runtime(str(project_dir), "0.1.6")


def test_frozen_project_runtime_direct_install_replaces_old_infernux_only(tmp_path, monkeypatch):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "is_frozen", lambda: True)
    monkeypatch.setattr(project_model, "_installed_distribution_version", lambda _python, _name: "0.1.5")
    monkeypatch.setattr(project_model.ProjectModel, "validate_python_runtime", staticmethod(lambda _python: None))

    wheel_path = tmp_path / "infernux-0.1.6-cp312-cp312-win_amd64.whl"
    _write_infernux_wheel(wheel_path)
    project_dir = tmp_path / "project"
    runtime_dir = project_dir / ".runtime" / "python312"
    runtime_dir.mkdir(parents=True)
    project_python = Path(
        project_model.ProjectModel._get_project_python(str(project_dir))
    )
    site_packages = Path(
        project_model.ProjectModel._get_site_packages(str(project_dir))
    )
    old_package = site_packages / "Infernux"
    old_dist_info = site_packages / "infernux-0.1.5.dist-info"
    dependency_dir = site_packages / "numba"
    old_package.mkdir(parents=True)
    old_dist_info.mkdir()
    dependency_dir.mkdir()
    (old_package / "old.py").write_text("old = True\n", encoding="utf-8")
    project_python.parent.mkdir(parents=True, exist_ok=True)
    project_python.write_text("", encoding="utf-8")

    model = project_model.ProjectModel(None, version_manager=_FakeVersionManager(str(wheel_path)))
    model._install_infernux_in_runtime(str(project_dir), "0.1.6")

    assert not (site_packages / "Infernux" / "old.py").exists()
    assert not old_dist_info.exists()
    assert (site_packages / "Infernux" / "__init__.py").is_file()
    assert (site_packages / "infernux-0.1.6.dist-info" / "METADATA").is_file()
    assert dependency_dir.is_dir()


def test_dev_wheel_selection_matches_current_python_tag(tmp_path, monkeypatch):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "_engine_root", lambda: str(tmp_path))
    dist_dir = tmp_path / "dist" / "releases" / "test"
    dist_dir.mkdir(parents=True)
    old_wheel = dist_dir / "infernux-0.1.6-cp312-cp312-win_amd64.whl"
    new_wheel = dist_dir / "infernux-0.1.6-cp313-cp313-win_amd64.whl"
    old_wheel.write_bytes(b"old")
    new_wheel.write_bytes(b"new")
    monkeypatch.setattr(project_model, "_python_cp_tag", lambda _python="": "cp313")
    monkeypatch.setattr(project_model.sysconfig, "get_platform", lambda: "win-amd64")
    assert Path(project_model._find_dev_wheel()).name == new_wheel.name


def test_dev_project_runtime_uses_selected_python_and_system_site_packages(tmp_path, monkeypatch):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "is_frozen", lambda: False)
    source_python = tmp_path / "python.exe"
    source_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(project_model, "_select_dev_project_python", lambda: str(source_python))

    captured_args: list[list[str]] = []

    def fake_run_hidden(args: list[str], *, timeout: int):
        captured_args.append(args)

    monkeypatch.setattr(project_model, "_run_hidden", fake_run_hidden)

    model = project_model.ProjectModel(None)
    model._create_project_runtime(str(tmp_path / "project"))

    assert captured_args == [
        [str(source_python), "-m", "venv", "--copies", "--system-site-packages", str(tmp_path / "project" / ".venv")]
    ]


def test_dev_project_install_uses_local_wheel_without_dependency_resolution(tmp_path, monkeypatch):
    project_model = _load_project_model(monkeypatch)
    monkeypatch.setattr(project_model, "is_frozen", lambda: False)
    monkeypatch.setattr(project_model, "_distribution_files_present", lambda _site_packages, _name: False)
    monkeypatch.setattr(project_model.ProjectModel, "validate_python_runtime", staticmethod(lambda _python: None))
    wheel_path = tmp_path / "infernux-0.1.6-cp312-cp312-win_amd64.whl"
    wheel_path.write_bytes(b"wheel")
    monkeypatch.setattr(project_model, "_find_dev_wheel", lambda _python, strict=False: str(wheel_path))

    project_dir = tmp_path / "project"
    python_exe = Path(
        project_model.ProjectModel._get_project_python(str(project_dir))
    )
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("", encoding="utf-8")

    captured_args: list[list[str]] = []

    def fake_run_hidden(args: list[str], *, timeout: int):
        captured_args.append(args)

    monkeypatch.setattr(project_model, "_run_hidden", fake_run_hidden)

    model = project_model.ProjectModel(None)
    model._install_infernux_in_runtime(str(project_dir))

    assert len(captured_args) == 1
    assert captured_args[0][:4] == [str(python_exe), "-m", "pip", "install"]
    assert "--no-deps" in captured_args[0]
    assert captured_args[0][-1] == str(wheel_path)


def test_project_runtime_copy_skips_cache_and_test_artifacts(tmp_path, monkeypatch):
    runtime_manager = _load_embed_runtime_manager(monkeypatch)
    source = tmp_path / "source"
    package_dir = source / "Lib" / "site-packages" / "pkg"
    cache_dir = package_dir / "__pycache__"
    tests_dir = package_dir / "tests"
    cache_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (package_dir / "module.py").write_text("x = 1\n", encoding="utf-8")
    (cache_dir / "module.cpython-312.pyc").write_bytes(b"cache")
    (tests_dir / "test_module.py").write_text("def test_x(): pass\n", encoding="utf-8")

    dest = tmp_path / "dest"
    runtime_manager._copy_project_runtime_tree(str(source), str(dest))

    assert (dest / "Lib" / "site-packages" / "pkg" / "module.py").is_file()
    assert not (dest / "Lib" / "site-packages" / "pkg" / "__pycache__").exists()
    assert not (dest / "Lib" / "site-packages" / "pkg" / "tests").exists()
