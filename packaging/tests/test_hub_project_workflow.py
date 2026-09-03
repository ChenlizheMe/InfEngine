from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import database as database_module
from database import ProjectDatabase
import model.project_model as project_model_module
from model.project_model import ProjectModel
from hub_utils import HubLaunchContext
from viewmodel.control_pane_viewmodel import ControlPaneViewModel
from project_paths import ProjectPathError, inspect_existing_project, validate_project_name
from project_migration import ProjectMigrationService
from i18n import configure_language, resolve_language, tr


def _make_project(path: Path, *, name: str | None = None, version: str = "") -> Path:
    (path / "Assets").mkdir(parents=True)
    (path / "ProjectSettings").mkdir()
    if name:
        (path / f"{name}.ini").write_text(
            f"[Project]\nname = {name}\npath = {path}\n",
            encoding="utf-8",
        )
    if version:
        (path / ".infernux-version").write_text(f"# pin\n{version}\n", encoding="utf-8")
    return path


def test_default_project_database_lives_in_hub_state_root(tmp_path, monkeypatch):
    root = tmp_path / "HubData"
    monkeypatch.setattr(
        database_module,
        "get_hub_user_data_dir",
        lambda: str(root),
    )

    database = ProjectDatabase()
    database.close()

    assert (root / "State" / "projects.db").is_file()
    assert not (tmp_path / ".infernux").exists()


def test_source_import_does_not_apply_installed_version_catalog_rules(
    tmp_path: Path, monkeypatch
):
    project = _make_project(tmp_path / "Imported", name="Imported", version="9.9.9")
    database = ProjectDatabase(str(tmp_path / "hub.db"))

    class ProjectList:
        def refresh(self):
            pass

        def select_project(self, _project_id):
            pass

    class VersionManager:
        def is_installed(self, _version):
            raise AssertionError("source imports must not query installed Hub versions")

    monkeypatch.setattr(
        "viewmodel.control_pane_viewmodel.QFileDialog.getExistingDirectory",
        lambda *_args: str(project),
    )
    viewmodel = ControlPaneViewModel(
        ProjectModel(database),
        ProjectList(),
        VersionManager(),
        launch_context=HubLaunchContext.SOURCE,
    )

    viewmodel.open_existing_project(None)

    assert database.find_project_by_path(str(project)) is not None
    database.close()


def test_installed_import_keeps_version_catalog_warning(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path / "Imported", name="Imported", version="9.9.9")
    database = ProjectDatabase(str(tmp_path / "hub.db"))
    messages = []

    class ProjectList:
        def refresh(self):
            pass

        def select_project(self, _project_id):
            pass

    class VersionManager:
        @staticmethod
        def is_installed(_version):
            return False

    monkeypatch.setattr(
        "viewmodel.control_pane_viewmodel.QFileDialog.getExistingDirectory",
        lambda *_args: str(project),
    )
    monkeypatch.setattr(
        "viewmodel.control_pane_viewmodel.QMessageBox.information",
        lambda _parent, title, body: messages.append((title, body)),
    )
    viewmodel = ControlPaneViewModel(
        ProjectModel(database),
        ProjectList(),
        VersionManager(),
        launch_context=HubLaunchContext.INSTALLED,
    )

    viewmodel.open_existing_project(None)

    assert messages and "9.9.9" in messages[0][1]
    database.close()


def test_source_project_creation_accepts_current_environment_without_version(
    monkeypatch,
):
    class NewProjectDialog:
        def __init__(self, *_args):
            pass

        @staticmethod
        def exec():
            return 1

        @staticmethod
        def get_data():
            return "SourceProject", "C:/Projects", ""

    class InitializationReached(RuntimeError):
        pass

    monkeypatch.setattr("view.new_project_view.NewProjectView", NewProjectDialog)
    monkeypatch.setattr(
        "viewmodel.control_pane_viewmodel.CustomProgressDialog",
        lambda _parent: (_ for _ in ()).throw(InitializationReached()),
    )

    viewmodel = ControlPaneViewModel(
        model=object(),
        project_list=object(),
        launch_context=HubLaunchContext.SOURCE,
    )

    with pytest.raises(InitializationReached):
        viewmodel.create_project(None)


def test_project_creation_requires_the_current_bundled_support_template(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        project_model_module,
        "__file__",
        str(tmp_path / "packaging" / "model" / "project_model.py"),
    )
    destination = tmp_path / "project.gitignore"

    with pytest.raises(RuntimeError, match="Required Infernux project template"):
        ProjectModel(None)._copy_bundled_support_file(
            "project.gitignore.txt", str(destination), ""
        )

    assert not destination.exists()


def test_source_project_runtime_uses_active_environment_without_wheel_lookup(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "Project"
    project_python = project / ".venv" / "Scripts" / "python.exe"
    project_python.parent.mkdir(parents=True)
    project_python.write_bytes(b"")
    validated = []

    monkeypatch.setattr(project_model_module, "is_frozen", lambda: False)
    monkeypatch.setattr(
        ProjectModel,
        "validate_python_runtime",
        staticmethod(lambda path: validated.append(path)),
    )

    model = ProjectModel(None)
    model._install_infernux_in_runtime(str(project), "0.2.9")
    assert validated == [str(project_python)]


@pytest.mark.parametrize("name", ["..", ".", "foo/bar", "foo\\bar", "C:drive", "CON", "trail."])
def test_project_name_rejects_path_and_windows_reserved_values(name: str):
    with pytest.raises(ProjectPathError):
        validate_project_name(name)


def test_inspect_existing_project_reads_name_and_version_without_modifying(tmp_path: Path):
    project = _make_project(tmp_path / "folder-name", name="Display Name", version="0.2.1")
    before = sorted(item.relative_to(project) for item in project.rglob("*"))

    info = inspect_existing_project(str(project))

    assert info.name == "Display Name"
    assert info.engine_version == "0.2.1"
    assert Path(info.path) == project.resolve()
    assert sorted(item.relative_to(project) for item in project.rglob("*")) == before


def test_inspect_existing_project_requires_core_directories(tmp_path: Path):
    invalid = tmp_path / "not-a-project"
    invalid.mkdir()
    with pytest.raises(ProjectPathError, match="Missing"):
        inspect_existing_project(str(invalid))


def test_database_allows_same_name_but_deduplicates_canonical_path(tmp_path: Path):
    first = _make_project(tmp_path / "one")
    second = _make_project(tmp_path / "two")
    with ProjectDatabase(tmp_path / "projects.db") as db:
        first_record = db.add_project("Same Name", str(first))
        second_record = db.add_project("Same Name", str(second))

        assert first_record is not None
        assert second_record is not None
        assert first_record.project_id != second_record.project_id
        assert db.add_project("Another Name", str(first / ".")) is None
        assert len(db.all_projects()) == 2


def test_database_rejects_noncurrent_schema(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    parent = tmp_path / "projects"
    _make_project(parent / "Legacy")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE projects ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, "
        "created_at TEXT NOT NULL, path TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO projects (name, created_at, path) VALUES (?, ?, ?)",
        ("Legacy", "2026-01-01T00:00:00", str(parent)),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="current schema"):
        ProjectDatabase(db_path)


def test_register_existing_project_rejects_duplicate_path(tmp_path: Path):
    project = _make_project(tmp_path / "Existing", version="0.2.1")
    with ProjectDatabase(tmp_path / "projects.db") as db:
        model = ProjectModel(db)
        record, info = model.register_existing_project(str(project))
        assert record.name == "Existing"
        assert info.engine_version == "0.2.1"
        with pytest.raises(RuntimeError, match="already"):
            model.register_existing_project(str(project))


def test_relocate_project_updates_existing_record(tmp_path: Path):
    original = _make_project(tmp_path / "Original")
    relocated = _make_project(tmp_path / "Relocated", version="0.3.0")
    with ProjectDatabase(tmp_path / "projects.db") as db:
        model = ProjectModel(db)
        record, _ = model.register_existing_project(str(original))

        updated, info = model.relocate_project(record.project_id, str(relocated))

        assert updated.project_id == record.project_id
        assert updated.name == "Relocated"
        assert Path(updated.path) == relocated.resolve()
        assert info.engine_version == "0.3.0"
        assert db.find_project_by_path(str(original)) is None


def test_new_project_uses_structural_staging_but_creates_runtime_at_final_path(tmp_path: Path, monkeypatch):
    model = ProjectModel(None)
    runtime_locations = []

    monkeypatch.setattr(model, "_copy_bundled_requirements", lambda dest, _version: Path(dest).write_text("", encoding="utf-8"))

    def create_runtime(project, python_version="", on_status=None):
        runtime_locations.append(Path(project))
        (Path(project) / ".venv").mkdir()

    monkeypatch.setattr(model, "_create_project_runtime", create_runtime)
    monkeypatch.setattr(model, "_install_infernux_in_runtime", lambda *_args, **_kwargs: None)
    default_library_locations = []
    monkeypatch.setattr(
        model,
        "_install_default_libraries",
        lambda project, on_status=None: default_library_locations.append(Path(project)),
    )
    monkeypatch.setattr(model, "_create_vscode_workspace", lambda project: (Path(project) / ".vscode").mkdir())

    result = model.init_project_folder("SafeProject", str(tmp_path), "0.2.1")

    assert Path(result) == (tmp_path / "SafeProject").resolve()
    assert runtime_locations == [Path(result)]
    assert default_library_locations == [Path(result)]
    assert (Path(result) / "Assets").is_dir()
    gitignore = (Path(result) / ".gitignore").read_text(encoding="utf-8")
    assert "/Library/" in gitignore
    assert "/.venv/" in gitignore
    assert "/.runtime/" in gitignore
    assert "*.meta\n" not in gitignore
    gitattributes = (Path(result) / ".gitattributes").read_text(encoding="utf-8")
    assert "*.scene text eol=lf" in gitattributes
    assert "*.meta text eol=lf" in gitattributes
    assert "*.fbx binary" in gitattributes
    assert not (Path(result) / "Assets" / "README.md").exists()
    assert (Path(result) / "Assets" / "Scenes" / "Start.scene").is_file()
    assert (Path(result) / "Assets" / "Rendering" / "Bloom.effect").is_file()
    assert (Path(result) / "Assets" / "Rendering" / "ACES Tone Mapping.effect").is_file()
    assert (Path(result) / "Assets" / "Rendering" / "Default Post Processing.effectgroup").is_file()
    bloom = json.loads(
        (Path(result) / "Assets" / "Rendering" / "Bloom.effect.meta").read_text(encoding="utf-8")
    )
    tone_mapping = json.loads(
        (Path(result) / "Assets" / "Rendering" / "ACES Tone Mapping.effect.meta").read_text(encoding="utf-8")
    )
    effect_group_meta = json.loads(
        (
            Path(result)
            / "Assets"
            / "Rendering"
            / "Default Post Processing.effectgroup.meta"
        ).read_text(encoding="utf-8")
    )
    bloom_guid = bloom["metadata"]["guid"]["value"]
    tone_mapping_guid = tone_mapping["metadata"]["guid"]["value"]
    effect_group_guid = effect_group_meta["metadata"]["guid"]["value"]
    assert all(
        len(guid) == 32 and set(guid) <= set("0123456789abcdef")
        for guid in (bloom_guid, tone_mapping_guid, effect_group_guid)
    )
    effect_group = json.loads(
        (
            Path(result)
            / "Assets"
            / "Rendering"
            / "Default Post Processing.effectgroup"
        ).read_text(encoding="utf-8")
    )
    assert [entry["asset"]["guid"] for entry in effect_group["entries"]] == [
        bloom_guid,
        tone_mapping_guid,
    ]
    build_settings = json.loads(
        (Path(result) / "ProjectSettings" / "BuildSettings.json").read_text(encoding="utf-8")
    )
    assert build_settings["scenes"] == [
        str(Path(result) / "Assets" / "Scenes" / "Start.scene")
    ]
    editor_settings = json.loads(
        (Path(result) / "ProjectSettings" / "EditorSettings.json").read_text(encoding="utf-8")
    )
    assert editor_settings == {
        "lastOpenedScene": str(Path(result) / "Assets" / "Scenes" / "Start.scene")
    }
    scene = json.loads(
        (Path(result) / "Assets" / "Scenes" / "Start.scene").read_text(encoding="utf-8")
    )
    assert scene["mainCameraComponentId"] == 2
    assert [item["name"] for item in scene["objects"]] == [
        "Main Camera",
        "Directional Light",
        "RenderStack",
    ]
    assert (
        scene["objects"][2]["components"][0]["data"]["effect_slots"][0]
        ["fields"]["effect"]["guid"]
        == effect_group_guid
    )
    camera_data = scene["objects"][0]["components"][0]["data"]
    assert camera_data["projectionMode"] == 0
    assert set(camera_data) == {
        "aspectRatio",
        "backgroundColor",
        "clearFlags",
        "cullingMask",
        "depth",
        "farClip",
        "fov",
        "nearClip",
        "orthoSize",
        "projectionMode",
    }
    light_data = scene["objects"][1]["components"][0]["data"]
    assert set(light_data) == {
        "areaSize",
        "areaTwoSided",
        "baked",
        "color",
        "cullingMask",
        "influenceDomains",
        "intensity",
        "lightType",
        "outerSpotAngle",
        "range",
        "renderMode",
        "shadowBias",
        "shadowNormalBias",
        "shadowSoftness",
        "shadowStrength",
        "shadows",
        "spotAngle",
    }
    assert light_data["shadowBias"] == 1.0
    assert light_data["shadowNormalBias"] == 1.0
    assert (Path(result) / ".vscode").is_dir()
    assert not list(tmp_path.glob(".infernux-create-*"))


def test_new_project_failure_removes_only_staging(tmp_path: Path, monkeypatch):
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()
    model = ProjectModel(None)

    monkeypatch.setattr(model, "_copy_bundled_requirements", lambda dest, _version: Path(dest).write_text("", encoding="utf-8"))
    monkeypatch.setattr(model, "_create_project_runtime", lambda *_args, **_kwargs: None)

    def fail_install(*_args, **_kwargs):
        raise RuntimeError("expected install failure")

    monkeypatch.setattr(model, "_install_infernux_in_runtime", fail_install)

    with pytest.raises(RuntimeError, match="expected install failure"):
        model.init_project_folder("FailedProject", str(tmp_path), "0.2.1")

    assert unrelated.is_dir()
    assert not (tmp_path / "FailedProject").exists()
    assert not list(tmp_path.glob(".infernux-create-*"))


class _MigrationVersionManager:
    def is_installed(self, version):
        return version == "0.3.0"

    @staticmethod
    def python_version_for_engine(_version):
        return "3.13"

    @staticmethod
    def write_project_version(project, version):
        (Path(project) / ".infernux-version").write_text(version + "\n", encoding="utf-8")


class _MigrationProjectModel:
    def __init__(self, *, fail_install=False):
        self.fail_install = fail_install

    def _create_project_runtime(self, project, on_status=None):
        (Path(project) / ".venv").mkdir()

    def _install_infernux_in_runtime(self, project, version, on_status=None):
        if self.fail_install:
            raise RuntimeError("migration install failed")
        (Path(project) / ".venv" / "engine.txt").write_text(version, encoding="utf-8")

    @staticmethod
    def validate_project_runtime(project):
        assert (Path(project) / ".venv" / "engine.txt").is_file()

    @staticmethod
    def _copy_bundled_requirements(destination, version):
        Path(destination).write_text(f"Infernux=={version}\n", encoding="utf-8")

    @staticmethod
    def _create_vscode_workspace(project):
        (Path(project) / ".vscode").mkdir(exist_ok=True)


def test_project_migration_backs_up_and_replaces_runtime(tmp_path: Path):
    project = _make_project(tmp_path / "Migrating", version="0.2.0")
    (project / "Assets" / "scene.inx").write_text("scene", encoding="utf-8")
    (project / "ProjectSettings" / "requirements.txt").write_text("old", encoding="utf-8")
    (project / ".venv").mkdir()
    (project / ".venv" / "old.txt").write_text("old runtime", encoding="utf-8")
    service = ProjectMigrationService(_MigrationProjectModel(), _MigrationVersionManager())

    result = service.migrate(str(project), "0.3.0")

    assert result.source_version == "0.2.0"
    assert (project / ".infernux-version").read_text(encoding="utf-8").strip() == "0.3.0"
    assert (project / ".venv" / "engine.txt").read_text(encoding="utf-8") == "0.3.0"
    assert json.loads(
        (project / "ProjectSettings" / "PythonRuntime.json").read_text(
            encoding="utf-8"
        )
    )["pythonVersion"] == "3.13"
    assert not (project / ".venv" / "old.txt").exists()
    with zipfile.ZipFile(result.backup_path) as archive:
        assert "Assets/scene.inx" in archive.namelist()
        assert "ProjectSettings/requirements.txt" in archive.namelist()


def test_project_migration_restores_runtime_and_metadata_on_failure(tmp_path: Path):
    project = _make_project(tmp_path / "Rollback", version="0.2.0")
    requirements = project / "ProjectSettings" / "requirements.txt"
    requirements.write_text("old requirements", encoding="utf-8")
    (project / ".venv").mkdir()
    (project / ".venv" / "old.txt").write_text("old runtime", encoding="utf-8")
    service = ProjectMigrationService(
        _MigrationProjectModel(fail_install=True), _MigrationVersionManager(),
    )

    with pytest.raises(RuntimeError, match="migration install failed"):
        service.migrate(str(project), "0.3.0")

    assert (project / ".venv" / "old.txt").read_text(encoding="utf-8") == "old runtime"
    assert "0.2.0" in (project / ".infernux-version").read_text(encoding="utf-8")
    assert requirements.read_text(encoding="utf-8") == "old requirements"
    assert list((project / ".infernux-backups").glob("*.zip"))


def test_language_resolution_uses_windows_style_zh_prefix():
    assert resolve_language("system", system_locale="zh-CN") == "zh"
    assert resolve_language("system", system_locale="en-US") == "en"
    assert resolve_language("zh", system_locale="en-US") == "zh"
    configure_language("zh")
    assert tr("Projects") == "项目"
    configure_language("en")
