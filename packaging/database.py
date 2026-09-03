from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

from project_paths import canonical_project_path, normalize_project_path


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    name: str
    created_at: str
    path: str


class ProjectDatabase:
    """Persistent Hub project registry keyed by stable id and canonical path."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            home_dir = Path.home() / ".infernux"
            home_dir.mkdir(parents=True, exist_ok=True)
            db_path = home_dir / "projects.db"

        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    @staticmethod
    def _record(row: sqlite3.Row | None) -> ProjectRecord | None:
        if row is None:
            return None
        return ProjectRecord(row["project_id"], row["name"], row["created_at"], row["path"])

    def all_projects(self) -> List[ProjectRecord]:
        rows = self._conn.execute(
            "SELECT project_id, name, created_at, path FROM projects ORDER BY created_at DESC;"
        ).fetchall()
        return [self._record(row) for row in rows if row is not None]

    def get_project(self, project_id: str) -> ProjectRecord | None:
        row = self._conn.execute(
            "SELECT project_id, name, created_at, path FROM projects WHERE project_id = ?;",
            (project_id,),
        ).fetchone()
        return self._record(row)

    def find_project_by_path(self, path: str) -> ProjectRecord | None:
        normalized = normalize_project_path(path)
        row = self._conn.execute(
            "SELECT project_id, name, created_at, path FROM projects WHERE normalized_path = ?;",
            (normalized,),
        ).fetchone()
        return self._record(row)

    def add_project(self, name: str, path: str) -> ProjectRecord | None:
        project_path = canonical_project_path(path)
        normalized_path = normalize_project_path(project_path)
        project_id = uuid.uuid4().hex
        created_at = datetime.now().isoformat(timespec="seconds")
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO projects "
                    "(project_id, name, created_at, path, normalized_path) VALUES (?, ?, ?, ?, ?);",
                    (project_id, name, created_at, project_path, normalized_path),
                )
        except sqlite3.IntegrityError:
            return None
        return ProjectRecord(project_id, name, created_at, project_path)

    def remove_project(self, project_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM projects WHERE project_id = ?;", (project_id,))
        return cursor.rowcount > 0

    def relocate_project(self, project_id: str, name: str, path: str) -> ProjectRecord | None:
        """Update a registry entry after the project folder has moved."""
        project_path = canonical_project_path(path)
        normalized_path = normalize_project_path(project_path)
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE projects SET name = ?, path = ?, normalized_path = ? "
                    "WHERE project_id = ?;",
                    (name, project_path, normalized_path, project_id),
                )
        except sqlite3.IntegrityError:
            return None
        if cursor.rowcount == 0:
            return None
        return self.get_project(project_id)

    def _create_schema(self) -> None:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects';"
        ).fetchone()
        if not exists:
            with self._conn:
                self._create_projects_table("projects")
                self._create_settings_table()
            return

        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(projects);").fetchall()
        }
        required = {"project_id", "name", "created_at", "path", "normalized_path"}
        if columns != required:
            raise RuntimeError(
                "Hub project database does not match the current schema; "
                "remove projects.db and import projects again"
            )
        with self._conn:
            self._create_settings_table()

    def _create_projects_table(self, table_name: str) -> None:
        self._conn.execute(
            f"""
            CREATE TABLE {table_name} (
                project_id      TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                path            TEXT NOT NULL,
                normalized_path TEXT NOT NULL UNIQUE
            );
            """
        )

    def _create_settings_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);",
                (key, value),
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
