from __future__ import annotations

from pathlib import Path
import sys
import zipfile


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from project_migration import ProjectMigrationService


def test_backup_excludes_only_root_owned_runtime_and_cache_directories(tmp_path):
    excluded = ("Cache", "Library", "Logs", ".git", ".runtime", ".venv")
    for name in excluded:
        root_file = tmp_path / name / "generated.bin"
        root_file.parent.mkdir(parents=True)
        root_file.write_bytes(b"generated")
        author_file = tmp_path / "Assets" / name / "author.txt"
        author_file.parent.mkdir(parents=True)
        author_file.write_text(name, encoding="utf-8")
    package_file = tmp_path / "Packages" / "example" / "Library" / "resource.txt"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("package resource", encoding="utf-8")

    backup = ProjectMigrationService._create_backup(str(tmp_path), "0.3.0", "0.4.0")

    with zipfile.ZipFile(backup) as archive:
        assert set(archive.namelist()) == {
            *(f"Assets/{name}/author.txt" for name in excluded),
            "Packages/example/Library/resource.txt",
        }
        for name in excluded:
            assert archive.read(f"Assets/{name}/author.txt").decode("utf-8") == name


def test_repeated_migration_backup_does_not_overwrite_an_existing_snapshot(tmp_path, monkeypatch):
    import project_migration

    class FixedClock:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, _format):
            return "20260905-210000"

    monkeypatch.setattr(project_migration._datetime, "datetime", FixedClock)
    author_file = tmp_path / "scene.txt"
    author_file.write_text("before first migration", encoding="utf-8")
    first = ProjectMigrationService._create_backup(str(tmp_path), "0.3.0", "0.4.0")
    author_file.write_text("before second migration", encoding="utf-8")
    second = ProjectMigrationService._create_backup(str(tmp_path), "0.3.0", "0.4.0")

    assert first != second
    with zipfile.ZipFile(first) as archive:
        assert archive.read("scene.txt") == b"before first migration"
    with zipfile.ZipFile(second) as archive:
        assert archive.namelist() == ["scene.txt"]
        assert archive.read("scene.txt") == b"before second migration"
