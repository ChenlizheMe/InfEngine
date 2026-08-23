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

from database import ProjectDatabase
import model.project_model as project_model_module
from model.project_model import ProjectModel
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


def test_dev_wheel_selection_prefers_the_newest_compatible_build(tmp_path: Path, monkeypatch):
    output = tmp_path / "out" / "build-release" / "python_wheel"
    dist = tmp_path / "dist"
    output.mkdir(parents=True)
    dist.mkdir()
    old_wheel = dist / "infernux-0.2.9-cp312-cp312-win_amd64.whl"
    new_wheel = output / "infernux-0.2.9-cp312-cp312-win_amd64.whl"
    old_wheel.write_bytes(b"old")
    new_wheel.write_bytes(b"new")
    old_wheel.touch()
    new_wheel.touch()
    old_wheel_stat = old_wheel.stat()
    new_wheel_stat = new_wheel.stat()
    import os
    os.utime(old_wheel, ns=(old_wheel_stat.st_atime_ns, new_wheel_stat.st_mtime_ns - 1_000_000))

    monkeypatch.setattr(project_model_module, "_engine_root", lambda: str(tmp_path))
    monkeypatch.setattr(project_model_module, "_python_cp_tag", lambda _python="": "cp312")
    monkeypatch.setattr(project_model_module, "_wheel_matches_python", lambda *_args: True)

    assert project_model_module._find_dev_wheel("python.exe") == str(new_wheel)


def test_dev_runtime_reinstalls_same_version_only_when_wheel_fingerprint_changes(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "Project"
    project_python = project / ".venv" / "Scripts" / "python.exe"
    site_packages = project / ".venv" / "Lib" / "site-packages"
    project_python.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    project_python.write_bytes(b"")
    wheel = tmp_path / "infernux-0.2.9-cp312-cp312-win_amd64.whl"
    wheel.write_bytes(b"current wheel")
    commands = []

    monkeypatch.setattr(project_model_module, "is_frozen", lambda: False)
    monkeypatch.setattr(project_model_module, "_find_dev_wheel", lambda *_args, **_kwargs: str(wheel))
    monkeypatch.setattr(project_model_module, "_distribution_files_present", lambda *_args: True)
    monkeypatch.setattr(project_model_module, "_installed_distribution_version", lambda *_args: "0.2.9")
    monkeypatch.setattr(project_model_module, "_run_hidden", lambda args, **_kwargs: commands.append(args))
    monkeypatch.setattr(ProjectModel, "validate_python_runtime", staticmethod(lambda _path: None))

    model = ProjectModel(None)
    model._install_infernux_in_runtime(str(project), "0.2.9")
    assert len(commands) == 1
    assert "--force-reinstall" in commands[0]

    model._install_infernux_in_runtime(str(project), "0.2.9")
    assert len(commands) == 1


def test_launch_runtime_sync_trusts_matching_wheel_marker_without_spawning_python(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "Project"
    project_python = project / ".venv" / "Scripts" / "python.exe"
    site_packages = project / ".venv" / "Lib" / "site-packages"
    project_python.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    project_python.write_bytes(b"")
    wheel = tmp_path / "infernux-0.2.9-cp312-cp312-win_amd64.whl"
    wheel.write_bytes(b"current wheel")

    monkeypatch.setattr(project_model_module, "is_frozen", lambda: False)
    monkeypatch.setattr(
        project_model_module,
        "_find_dev_wheel",
        lambda *_args, **_kwargs: str(wheel),
    )
    monkeypatch.setattr(
        project_model_module,
        "_distribution_files_present",
        lambda *_args: True,
    )

    model = ProjectModel(None)
    marker = Path(project_model_module._project_wheel_marker(str(project)))
    marker.write_text(
        project_model_module._wheel_install_fingerprint(str(wheel)),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        project_model_module,
        "_installed_distribution_version",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("matching marker must not start project Python")
        ),
    )
    monkeypatch.setattr(
        ProjectModel,
        "validate_python_runtime",
        staticmethod(
            lambda _path: (_ for _ in ()).throw(
                AssertionError("launch fast path must defer validation to the editor")
            )
        ),
    )

    model._install_infernux_in_runtime(
        str(project),
        "0.2.9",
        validate_current=False,
    )


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


def test_database_migrates_legacy_parent_paths(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    parent = tmp_path / "projects"
    project = _make_project(parent / "Legacy")
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

    with ProjectDatabase(db_path) as db:
        records = db.all_projects()
        assert len(records) == 1
        assert Path(records[0].path) == project.resolve()
        assert db.find_project_by_path(str(project)).project_id == records[0].project_id


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

    def create_runtime(project, on_status=None):
        runtime_locations.append(Path(project))
        (Path(project) / ".venv").mkdir()

    monkeypatch.setattr(model, "_create_project_runtime", create_runtime)
    monkeypatch.setattr(model, "_install_infernux_in_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model, "_create_vscode_workspace", lambda project: (Path(project) / ".vscode").mkdir())

    result = model.init_project_folder("SafeProject", str(tmp_path), "0.2.1")

    assert Path(result) == (tmp_path / "SafeProject").resolve()
    assert runtime_locations == [Path(result)]
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
