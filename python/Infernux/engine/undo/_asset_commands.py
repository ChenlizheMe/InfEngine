"""Undo commands for editor-owned Project asset mutations."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Optional

from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    resolved_path,
    same_path,
)
from Infernux.engine.undo._base import UndoCommand


class ProjectAssetRenameCommand(UndoCommand):
    """Replay a GUID-stable asset rename without creating another action."""

    marks_dirty = False

    def __init__(
        self,
        old_path: str,
        new_path: str,
        *,
        asset_database: Any = None,
        on_changed: Optional[Callable[[], None]] = None,
        move_fn: Optional[Callable[[str, str, Any], Optional[str]]] = None,
        description: str = "Rename Asset",
    ) -> None:
        super().__init__(description)
        self._old_path = resolved_path(old_path)
        self._new_path = resolved_path(new_path)
        if same_path(self._old_path, self._new_path):
            raise ValueError("asset rename command requires two different paths")
        if not same_path(
            os.path.dirname(self._old_path),
            os.path.dirname(self._new_path),
        ):
            raise ValueError("asset rename command cannot move between directories")
        self._asset_database = asset_database
        self._on_changed = on_changed
        self._move_fn = move_fn or self._rename

    @staticmethod
    def _rename(source: str, destination: str, asset_database: Any) -> Optional[str]:
        from Infernux.engine.ui import project_file_ops

        return project_file_ops.do_rename(
            source,
            os.path.basename(destination),
            asset_database,
        )

    def _apply(self, source: str, destination: str) -> None:
        if not os.path.exists(source):
            raise RuntimeError(f"asset rename source no longer exists: {source}")
        if os.path.exists(destination) and not same_path(source, destination):
            raise RuntimeError(
                f"asset rename destination is occupied by an external change: {destination}"
            )
        result = self._move_fn(source, destination, self._asset_database)
        if not result or not same_path(result, destination):
            raise RuntimeError(f"asset rename failed: {source} -> {destination}")
        if self._on_changed is not None:
            self._on_changed()

    def execute(self) -> None:
        self._apply(self._old_path, self._new_path)

    def undo(self) -> None:
        self._apply(self._new_path, self._old_path)

    def redo(self) -> None:
        self.execute()


@dataclass(frozen=True, slots=True)
class _AssetBackupEntry:
    original_path: str
    backup_path: str
    original_meta_path: str
    backup_meta_path: str
    is_directory: bool
    import_paths: tuple[str, ...]


class ProjectAssetDeleteCommand(UndoCommand):
    """Atomically delete and restore Project assets with their GUID metadata."""

    marks_dirty = False

    def __init__(
        self,
        paths: list[str] | tuple[str, ...],
        *,
        project_root: str = "",
        backup_root: str = "",
        asset_database: Any = None,
        on_deleted: Optional[Callable[[], None]] = None,
        on_restored: Optional[Callable[[], None]] = None,
        delete_fn: Optional[Callable[[str, Any], bool]] = None,
        import_fn: Optional[Callable[[str, Any], bool]] = None,
        description: str = "Delete Assets",
    ) -> None:
        super().__init__(description)
        self._project_root = resolved_path(project_root) if project_root else ""
        self._paths = self._normalize_roots(paths)
        if not self._paths:
            raise ValueError("asset delete command requires at least one path")
        if self._project_root:
            for path in self._paths:
                if not is_path_within(path, self._project_root, allow_root=False):
                    raise ValueError(f"asset delete path is outside the project: {path}")
        self._backup_root = resolved_path(backup_root) if backup_root else ""
        self._asset_database = asset_database
        self._on_deleted = on_deleted
        self._on_restored = on_restored
        self._delete_fn = delete_fn or self._delete_asset
        self._import_fn = import_fn or self._import_asset
        self._backup_directory = ""
        self._entries: tuple[_AssetBackupEntry, ...] = ()
        self._disposed = False

    @staticmethod
    def _normalize_roots(paths) -> tuple[str, ...]:
        candidates: list[str] = []
        seen: set[str] = set()
        for value in paths or ():
            path = resolved_path(str(value or ""))
            key = path_key(path)
            if not path or path.lower().endswith(".meta") or key in seen:
                continue
            seen.add(key)
            candidates.append(path)
        candidates.sort(key=lambda value: (len(path_key(value)), path_key(value)))
        roots: list[str] = []
        for candidate in candidates:
            if any(is_path_within(candidate, root, allow_root=True) for root in roots):
                continue
            roots.append(candidate)
        return tuple(roots)

    @staticmethod
    def _delete_asset(path: str, asset_database: Any) -> bool:
        from Infernux.engine.ui import project_file_ops

        return bool(project_file_ops.delete_item(path, asset_database))

    @staticmethod
    def _import_asset(path: str, asset_database: Any) -> bool:
        from Infernux.core.assets import AssetManager

        return bool(AssetManager.import_asset(path, database=asset_database))

    def _create_backup(self) -> None:
        if self._entries:
            return
        for path in self._paths:
            if not os.path.exists(path):
                raise RuntimeError(f"asset delete source no longer exists: {path}")

        if self._backup_root:
            os.makedirs(self._backup_root, exist_ok=True)
            directory = tempfile.mkdtemp(prefix="asset-delete-", dir=self._backup_root)
        else:
            directory = tempfile.mkdtemp(prefix="infernux-asset-delete-")
        entries: list[_AssetBackupEntry] = []
        try:
            for index, original in enumerate(self._paths):
                slot = os.path.join(directory, f"{index:04d}")
                os.makedirs(slot)
                backup = os.path.join(slot, "asset")
                is_directory = os.path.isdir(original)
                if is_directory:
                    shutil.copytree(original, backup, copy_function=shutil.copy2)
                else:
                    shutil.copy2(original, backup)

                original_meta = original + ".meta"
                backup_meta = os.path.join(slot, "asset.meta")
                if os.path.isfile(original_meta):
                    shutil.copy2(original_meta, backup_meta)
                import_paths = tuple(self._registered_assets_under(original))
                entries.append(
                    _AssetBackupEntry(
                        original,
                        backup,
                        original_meta,
                        backup_meta,
                        is_directory,
                        import_paths,
                    )
                )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self._backup_directory = directory
        self._entries = tuple(entries)

    def _registered_assets_under(self, root: str):
        candidates: list[str] = []
        if os.path.isdir(root):
            for directory, _dirnames, filenames in os.walk(root):
                candidates.extend(
                    os.path.join(directory, filename)
                    for filename in filenames
                    if not filename.lower().endswith(".meta")
                )
        else:
            candidates.append(root)
        for path in candidates:
            if os.path.isfile(path + ".meta"):
                yield path
                continue
            if self._asset_database is None:
                continue
            try:
                if self._asset_database.get_guid_from_path(path):
                    yield path
            except Exception:
                continue

    @staticmethod
    def _remove_path(path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    def _restore_entries(
        self,
        entries: tuple[_AssetBackupEntry, ...] | list[_AssetBackupEntry],
        *,
        replace_existing: bool,
    ) -> None:
        if not replace_existing:
            occupied = [
                entry.original_path
                for entry in entries
                if os.path.exists(entry.original_path)
                or os.path.exists(entry.original_meta_path)
            ]
            if occupied:
                raise RuntimeError(
                    "asset restore destination is occupied by an external change: "
                    + occupied[0]
                )

        restored: list[_AssetBackupEntry] = []
        try:
            for entry in entries:
                if replace_existing:
                    self._remove_path(entry.original_path)
                    self._remove_path(entry.original_meta_path)
                os.makedirs(os.path.dirname(entry.original_path), exist_ok=True)
                if entry.is_directory:
                    shutil.copytree(
                        entry.backup_path,
                        entry.original_path,
                        copy_function=shutil.copy2,
                    )
                else:
                    shutil.copy2(entry.backup_path, entry.original_path)
                if os.path.isfile(entry.backup_meta_path):
                    shutil.copy2(entry.backup_meta_path, entry.original_meta_path)
                restored.append(entry)

            for entry in restored:
                for asset_path in entry.import_paths:
                    if not self._import_fn(asset_path, self._asset_database):
                        raise RuntimeError(f"asset restore import failed: {asset_path}")
        except Exception:
            if not replace_existing:
                for entry in reversed(restored):
                    self._remove_path(entry.original_path)
                    self._remove_path(entry.original_meta_path)
            raise

    def _delete_entries(self) -> None:
        for entry in self._entries:
            if not os.path.exists(entry.original_path):
                raise RuntimeError(
                    f"asset delete source no longer exists: {entry.original_path}"
                )

        attempted: list[_AssetBackupEntry] = []
        try:
            for entry in reversed(self._entries):
                attempted.append(entry)
                if not self._delete_fn(entry.original_path, self._asset_database):
                    raise RuntimeError(f"asset delete failed: {entry.original_path}")
                if os.path.exists(entry.original_path):
                    raise RuntimeError(
                        f"asset delete reported success but source remains: {entry.original_path}"
                    )
                if os.path.exists(entry.original_meta_path):
                    self._remove_path(entry.original_meta_path)
        except Exception as delete_error:
            try:
                self._restore_entries(tuple(reversed(attempted)), replace_existing=True)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"asset delete failed and rollback also failed: {rollback_error}"
                ) from delete_error
            raise

    def execute(self) -> None:
        if self._disposed:
            raise RuntimeError("asset delete command has already been disposed")
        self._create_backup()
        self._delete_entries()
        if self._on_deleted is not None:
            self._on_deleted()

    def undo(self) -> None:
        if self._disposed or not self._entries:
            raise RuntimeError("asset delete backup is unavailable")
        self._restore_entries(self._entries, replace_existing=False)
        if self._on_restored is not None:
            self._on_restored()

    def redo(self) -> None:
        self.execute()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._backup_directory:
            shutil.rmtree(self._backup_directory, ignore_errors=True)
        self._backup_directory = ""
        self._entries = ()
