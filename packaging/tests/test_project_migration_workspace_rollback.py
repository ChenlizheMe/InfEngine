from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import project_migration
import model.project_model as project_model


@pytest.mark.parametrize("frozen", (False, True))
@pytest.mark.parametrize("existing_workspace", (False, True))
def test_late_migration_failure_restores_workspace_and_abi(
    tmp_path, monkeypatch, frozen, existing_workspace
):
    monkeypatch.setattr(project_migration, "is_frozen", lambda: frozen)
    monkeypatch.setattr(project_model, "is_frozen", lambda: frozen)
    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    originals = {
        ".infernux-version": b"0.3.0\n",
        "ProjectSettings/requirements.txt": b"author-dependency==1.0\n",
        "ProjectSettings/PythonRuntime.json": b'{"pythonVersion": "3.12"}\n',
    }
    workspace_files = (".vscode/settings.json", ".vscode/extensions.json", "pyrightconfig.json")
    if existing_workspace:
        originals.update({path: b'{"author-setting": true}\n' for path in workspace_files})
        originals[".vscode/tasks.json"] = b'{"author-task": true}\n'
    for relative, content in originals.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    runtime = tmp_path / (".runtime" if frozen else ".venv")
    runtime.mkdir()
    (runtime / "old-runtime.txt").write_text("old ABI runtime", encoding="utf-8")

    class Versions:
        def is_installed(self, version):
            return version == "0.4.0"

        def python_version_for_engine(self, version):
            return "3.13"

        def write_project_version(self, project, version):
            (Path(project) / ".infernux-version").write_text(version, encoding="utf-8")

    class RuntimeOwner:
        def has_runtime(self, version):
            return version == "3.13"

    class Model:
        runtime_manager = RuntimeOwner()

        def _create_project_runtime(self, project, on_status=None):
            runtime.mkdir()
            (runtime / "new-runtime.txt").write_text("new ABI runtime", encoding="utf-8")

        def _install_infernux_in_runtime(self, *args, **kwargs):
            pass

        def validate_project_runtime(self, project):
            assert (runtime / "new-runtime.txt").is_file()

        def _copy_bundled_requirements(self, destination, version):
            Path(destination).write_text("new-dependency==2.0\n", encoding="utf-8")

        _create_vscode_workspace = staticmethod(project_model.ProjectModel._create_vscode_workspace)

    original_dump = json.dump

    def fail_last_workspace_write(value, stream, *args, **kwargs):
        if Path(stream.name) == tmp_path / "pyrightconfig.json":
            stream.write('{"partial":')
            raise OSError("workspace publication failed")
        return original_dump(value, stream, *args, **kwargs)

    monkeypatch.setattr(project_model.json, "dump", fail_last_workspace_write)
    service = project_migration.ProjectMigrationService(Model(), Versions())
    with pytest.raises(OSError, match="workspace publication failed"):
        service.migrate(str(tmp_path), "0.4.0")

    for relative, content in originals.items():
        assert (tmp_path / relative).read_bytes() == content, relative
    if not existing_workspace:
        assert not (tmp_path / ".vscode").exists()
        assert not (tmp_path / "pyrightconfig.json").exists()
    assert (runtime / "old-runtime.txt").read_text() == "old ABI runtime"
    assert not (runtime / "new-runtime.txt").exists()
    assert not list(tmp_path.glob(".infernux-runtime-rollback-*"))
