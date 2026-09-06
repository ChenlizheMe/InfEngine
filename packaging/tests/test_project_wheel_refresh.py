import subprocess
import zipfile
from types import SimpleNamespace

import pytest

import model.project_model as project_model


@pytest.mark.parametrize("marker_state", ["missing", "stale", "current"])
def test_project_uses_wheel_identity_not_only_distribution_version(tmp_path, monkeypatch, marker_state):
    project = tmp_path / "Project"
    site_packages = project / ".runtime" / "site-packages"
    package = site_packages / "Infernux"
    package.mkdir(parents=True)
    installed_source = package / "__init__.py"
    installed_source.write_text("OLD", encoding="utf-8")
    python = project / ".runtime" / "python.exe"
    python.touch()
    wheel = tmp_path / "infernux-0.4.0-cp313-cp313-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("Infernux/__init__.py", "NEW")

    monkeypatch.setattr(project_model, "is_frozen", lambda: True)
    monkeypatch.setattr(project_model.ProjectModel, "_get_project_python", staticmethod(lambda path: str(python)))
    monkeypatch.setattr(project_model.ProjectModel, "_get_site_packages", staticmethod(lambda path: str(site_packages)))
    monkeypatch.setattr(project_model, "_project_python_version", lambda path: "3.13")
    monkeypatch.setattr(project_model.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "0.4.0\n", ""))
    validations = []
    monkeypatch.setattr(project_model.ProjectModel, "validate_python_runtime", staticmethod(validations.append))
    marker = project / ".runtime" / ".infernux-wheel"
    if marker_state != "missing":
        marker.write_text(
            project_model._wheel_install_fingerprint(str(wheel)) if marker_state == "current" else "old wheel\n",
            encoding="utf-8",
        )
    manager = SimpleNamespace(get_wheel_path=lambda *args: str(wheel))
    model = project_model.ProjectModel(None, manager, runtime_manager=object())
    model._install_infernux_in_runtime(str(project), "0.4.0", validate_current=False)
    assert installed_source.read_text(encoding="utf-8") == ("OLD" if marker_state == "current" else "NEW")
    assert len(validations) == (0 if marker_state == "current" else 1)
    assert marker.read_text(encoding="utf-8") == project_model._wheel_install_fingerprint(str(wheel))
    model._install_infernux_in_runtime(str(project), "0.4.0", validate_current=False)
    assert len(validations) == (0 if marker_state == "current" else 1)
