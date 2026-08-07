"""Undo commands for editor-owned Project asset mutations."""

from __future__ import annotations

import os
import shutil
import tempfile
import copy
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Optional

from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    resolved_path,
    same_path,
)
from Infernux.engine.undo._base import CompoundCommand, UndoCommand


class ImportSettingsDraftCommand(UndoCommand):
    """Replay one in-memory import-settings edit against its document.

    Import settings are deferred-authoring documents: editing changes the
    draft, while Apply persists the current revision and triggers reimport.
    Consequently this command never marks the active Scene dirty.
    """

    marks_dirty = False
    _is_property_edit = True
    MERGE_WINDOW = 0.3

    def __init__(
        self,
        controller: Any,
        old_settings: Any,
        new_settings: Any,
        *,
        edit_key: str,
        description: str,
    ) -> None:
        super().__init__(description)
        self._controller = controller
        self._old_settings = copy.deepcopy(old_settings)
        self._new_settings = copy.deepcopy(new_settings)
        self._document_id = str(getattr(controller, "document_id", ""))
        self._edit_key = str(edit_key or "")

    def execute(self) -> None:
        self._controller.restore_draft(self._new_settings)

    def undo(self) -> None:
        self._controller.restore_draft(self._old_settings)

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        return (
            isinstance(other, ImportSettingsDraftCommand)
            and self._document_id == other._document_id
            and self._edit_key == other._edit_key
            and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW
        )

    def merge(self, other: "ImportSettingsDraftCommand") -> None:
        self._new_settings = copy.deepcopy(other._new_settings)
        self.timestamp = other.timestamp


class EditableDocumentDraftCommand(UndoCommand):
    """Replay an autosaved document through its stable controller."""

    marks_dirty = False
    _is_property_edit = True
    MERGE_WINDOW = 0.3

    def __init__(
        self,
        controller: Any,
        old_document: dict,
        new_document: dict,
        old_revision: int,
        new_revision: int,
        *,
        edit_key: str,
        description: str,
    ) -> None:
        super().__init__(description)
        self._controller = controller
        self._old_document = copy.deepcopy(old_document)
        self._new_document = copy.deepcopy(new_document)
        self._old_revision = int(old_revision)
        self._new_revision = int(new_revision)
        self._document_id = str(getattr(controller, "document_id", ""))
        self._edit_key = str(edit_key or "")

    def execute(self) -> None:
        self._controller.restore_document(self._new_document, self._new_revision)

    def undo(self) -> None:
        self._controller.restore_document(self._old_document, self._old_revision)

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        return (
            isinstance(other, EditableDocumentDraftCommand)
            and self._document_id == other._document_id
            and self._edit_key == other._edit_key
            and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW
        )

    def merge(self, other: "EditableDocumentDraftCommand") -> None:
        self._new_document = copy.deepcopy(other._new_document)
        self._new_revision = other._new_revision
        self.timestamp = other.timestamp


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
        self._move_fn = move_fn

    def _rename(self, source: str, destination: str) -> Optional[str]:
        from Infernux.engine.ui import project_file_ops

        return project_file_ops.do_rename(
            source,
            os.path.basename(destination),
            self._asset_database,
            origin="user",
            operation_id=self.operation_id,
        )

    def _apply(self, source: str, destination: str) -> None:
        if not os.path.exists(source):
            raise RuntimeError(f"asset rename source no longer exists: {source}")
        if os.path.exists(destination) and not same_path(source, destination):
            raise RuntimeError(
                f"asset rename destination is occupied by an external change: {destination}"
            )
        result = (
            self._move_fn(source, destination, self._asset_database)
            if self._move_fn is not None
            else self._rename(source, destination)
        )
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


class ProjectAssetCreateCommand(UndoCommand):
    """Create one Project asset and preserve its identity across Undo/Redo."""

    marks_dirty = False

    def __init__(
        self,
        current_path: str,
        creator: Callable[[], Any],
        *,
        project_root: str = "",
        backup_root: str = "",
        asset_database: Any = None,
        on_changed: Optional[Callable[[], None]] = None,
        delete_fn: Optional[Callable[[str, Any], bool]] = None,
        import_fn: Optional[Callable[[str, Any], bool]] = None,
        description: str = "Create Asset",
    ) -> None:
        super().__init__(description)
        self._current_path = resolved_path(current_path)
        self._creator = creator
        self._project_root = resolved_path(project_root) if project_root else ""
        self._backup_root = resolved_path(backup_root) if backup_root else ""
        self._asset_database = asset_database
        self._on_changed = on_changed
        self._delete_fn = delete_fn
        self._import_fn = import_fn
        self._created_path = ""
        self._result: Any = (False, "Asset creation has not run")
        self._delete_command: Optional[ProjectAssetDeleteCommand] = None

    @property
    def result(self) -> Any:
        return self._result

    @property
    def created_path(self) -> str:
        return self._created_path

    def _children(self) -> dict[str, str]:
        if not os.path.isdir(self._current_path):
            return {}
        result: dict[str, str] = {}
        with os.scandir(self._current_path) as entries:
            for entry in entries:
                if entry.name.lower().endswith(".meta"):
                    continue
                path = resolved_path(entry.path)
                result[path_key(path)] = path
        return result

    @staticmethod
    def _succeeded(result: Any) -> bool:
        if isinstance(result, tuple):
            return bool(result and result[0])
        return bool(result)

    def execute(self) -> None:
        if self._delete_command is not None:
            raise RuntimeError("asset creation is already active; use redo to restore it")
        before = self._children()
        self._result = self._creator()
        after = self._children()
        created = [path for key, path in after.items() if key not in before]
        if not self._succeeded(self._result) or len(created) != 1:
            from Infernux.engine.ui import project_file_ops

            for path in created:
                try:
                    project_file_ops.delete_item(path, self._asset_database)
                except Exception:
                    pass
            if not self._succeeded(self._result):
                detail = self._result[1] if isinstance(self._result, tuple) and len(self._result) > 1 else ""
                raise RuntimeError(str(detail or "asset creation failed"))
            raise RuntimeError(
                "asset creation must produce exactly one top-level Project item"
            )
        self._created_path = created[0]
        self._delete_command = ProjectAssetDeleteCommand(
            [self._created_path],
            project_root=self._project_root,
            backup_root=self._backup_root,
            asset_database=self._asset_database,
            on_deleted=self._on_changed,
            on_restored=self._on_changed,
            delete_fn=self._delete_fn,
            import_fn=self._import_fn,
            description=self.description,
        )
        if self._on_changed is not None:
            self._on_changed()

    def undo(self) -> None:
        if self._delete_command is None:
            raise RuntimeError("asset creation has not been executed")
        self._delete_command.execute()

    def redo(self) -> None:
        if self._delete_command is None:
            raise RuntimeError("asset creation has not been executed")
        self._delete_command.undo()

    def dispose(self) -> None:
        if self._delete_command is not None:
            self._delete_command.dispose()
            self._delete_command = None


class ProjectPrefabCreateCommand(UndoCommand):
    """Own Prefab asset creation and source-hierarchy linkage as one action."""

    def __init__(
        self,
        asset_command: ProjectAssetCreateCommand,
        capture_linkage: Callable[[], Any],
        restore_linkage: Callable[[Any], None],
        description: str = "Create Prefab",
    ) -> None:
        super().__init__(description)
        if not isinstance(asset_command, ProjectAssetCreateCommand):
            raise TypeError("Prefab creation requires a ProjectAssetCreateCommand")
        if not callable(capture_linkage) or not callable(restore_linkage):
            raise TypeError("Prefab creation requires linkage snapshot callbacks")
        self._asset_command = asset_command
        self._capture_linkage = capture_linkage
        self._restore_linkage = restore_linkage
        self._before_linkage = None
        self._after_linkage = None

    @property
    def result(self) -> Any:
        return self._asset_command.result

    @property
    def created_path(self) -> str:
        return self._asset_command.created_path

    def execute(self) -> None:
        self._before_linkage = self._capture_linkage()
        try:
            self._asset_command.execute()
            self._after_linkage = self._capture_linkage()
        except Exception:
            self._restore_linkage(self._before_linkage)
            try:
                if self._asset_command.created_path:
                    self._asset_command.undo()
            except Exception:
                pass
            self._before_linkage = None
            self._after_linkage = None
            raise

    def undo(self) -> None:
        if self._before_linkage is None:
            raise RuntimeError("Prefab creation has not executed")
        self._restore_linkage(self._before_linkage)
        self._asset_command.undo()

    def redo(self) -> None:
        if self._after_linkage is None:
            raise RuntimeError("Prefab creation has no committed linkage state")
        self._asset_command.redo()
        self._restore_linkage(self._after_linkage)

    def dispose(self) -> None:
        self._asset_command.dispose()


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


class ProjectAssetMoveCommand(UndoCommand):
    """Move one asset between exact paths while preserving its GUID."""

    marks_dirty = False

    def __init__(
        self,
        source_path: str,
        destination_path: str,
        *,
        asset_database: Any = None,
        move_fn: Optional[Callable[[str, str, Any], Optional[str]]] = None,
        description: str = "Move Asset",
    ) -> None:
        super().__init__(description)
        self._source_path = resolved_path(source_path)
        self._destination_path = resolved_path(destination_path)
        if same_path(self._source_path, self._destination_path):
            raise ValueError("asset move command requires two different paths")
        self._asset_database = asset_database
        self._move_fn = move_fn

    def _move(self, source: str, destination: str) -> Optional[str]:
        from Infernux.engine.ui import project_file_ops

        return project_file_ops.move_path(
            source,
            destination,
            self._asset_database,
            origin="user",
            operation_id=self.operation_id,
        )

    def _apply(self, source: str, destination: str) -> None:
        if not os.path.exists(source):
            raise RuntimeError(f"asset move source no longer exists: {source}")
        if os.path.exists(destination):
            raise RuntimeError(
                f"asset move destination is occupied by an external change: {destination}"
            )
        try:
            result = (
                self._move_fn(source, destination, self._asset_database)
                if self._move_fn is not None
                else self._move(source, destination)
            )
        except Exception as move_error:
            self._rollback_partial_move(source, destination, move_error)
            raise
        if not result or not same_path(result, destination):
            move_error = RuntimeError(f"asset move failed: {source} -> {destination}")
            self._rollback_partial_move(source, destination, move_error)
            raise move_error

    def _rollback_partial_move(
        self,
        source: str,
        destination: str,
        move_error: Exception,
    ) -> None:
        if os.path.exists(source) or not os.path.exists(destination):
            return
        try:
            result = (
                self._move_fn(destination, source, self._asset_database)
                if self._move_fn is not None
                else self._move(destination, source)
            )
        except Exception as rollback_error:
            raise RuntimeError(
                "asset move failed and rollback also failed: "
                f"{destination} -> {source}: {rollback_error}"
            ) from move_error
        if not result or not same_path(result, source):
            raise RuntimeError(
                "asset move failed and rollback did not restore the source: "
                f"{destination} -> {source}"
            ) from move_error

    def execute(self) -> None:
        self._apply(self._source_path, self._destination_path)

    def undo(self) -> None:
        self._apply(self._destination_path, self._source_path)

    def redo(self) -> None:
        self.execute()


class ProjectAssetMoveBatchCommand(UndoCommand):
    """Move multiple workspace roots through one relocation transaction."""

    marks_dirty = False

    def __init__(
        self,
        moves: list[tuple[str, str]] | tuple[tuple[str, str], ...],
        *,
        asset_database: Any = None,
        move_fn: Optional[Callable[..., Any]] = None,
        description: str = "Move Assets",
    ) -> None:
        super().__init__(description)
        normalized = tuple(
            (resolved_path(source), resolved_path(destination))
            for source, destination in moves
        )
        if not normalized:
            raise ValueError("asset move batch requires at least one move")
        if any(same_path(source, destination) for source, destination in normalized):
            raise ValueError("asset move batch requires different source and destination paths")
        self._moves = normalized
        self._asset_database = asset_database
        self._move_fn = move_fn

    def _apply(self, moves: tuple[tuple[str, str], ...]) -> None:
        for source, destination in moves:
            if not os.path.exists(source):
                raise RuntimeError(f"asset move source no longer exists: {source}")
            if os.path.exists(destination):
                raise RuntimeError(
                    f"asset move destination is occupied by an external change: {destination}"
                )

        if self._move_fn is not None:
            result = self._move_fn(
                moves,
                self._asset_database,
                origin="user",
                operation_id=self.operation_id,
            )
        else:
            from Infernux.engine.ui import project_file_ops

            result = project_file_ops.move_paths_batch(
                moves,
                self._asset_database,
                origin="user",
                operation_id=self.operation_id,
            )
        expected = tuple(destination for _source, destination in moves)
        if result is None or len(result) != len(expected) or any(
            not same_path(actual, wanted)
            for actual, wanted in zip(result, expected)
        ):
            raise RuntimeError("asset move batch failed")

    def execute(self) -> None:
        self._apply(self._moves)

    def undo(self) -> None:
        self._apply(tuple((destination, source) for source, destination in self._moves))

    def redo(self) -> None:
        self.execute()


class ProjectAssetCopyCommand(UndoCommand):
    """Copy one distinct asset and retain its generated identity for redo."""

    marks_dirty = False

    def __init__(
        self,
        source_path: str,
        destination_path: str,
        *,
        project_root: str = "",
        backup_root: str = "",
        asset_database: Any = None,
        copy_fn: Optional[Callable[[str, str, Any], Optional[str]]] = None,
        delete_fn: Optional[Callable[[str, Any], bool]] = None,
        import_fn: Optional[Callable[[str, Any], bool]] = None,
        description: str = "Copy Asset",
    ) -> None:
        super().__init__(description)
        self._source_path = resolved_path(source_path)
        self._destination_path = resolved_path(destination_path)
        if same_path(self._source_path, self._destination_path):
            raise ValueError("asset copy command requires two different paths")
        self._project_root = resolved_path(project_root) if project_root else ""
        self._backup_root = resolved_path(backup_root) if backup_root else ""
        if self._project_root and not is_path_within(
            self._destination_path,
            self._project_root,
            allow_root=False,
        ):
            raise ValueError(
                "asset copy destination is outside the project: "
                + self._destination_path
            )
        self._asset_database = asset_database
        self._copy_fn = copy_fn or self._copy
        self._delete_fn = delete_fn
        self._import_fn = import_fn
        self._delete_command: Optional[ProjectAssetDeleteCommand] = None
        self._disposed = False

    @staticmethod
    def _copy(source: str, destination: str, asset_database: Any) -> Optional[str]:
        from Infernux.engine.ui import project_file_ops

        return project_file_ops.copy_path_as_new_asset(
            source,
            destination,
            asset_database,
        )

    def execute(self) -> None:
        if self._disposed:
            raise RuntimeError("asset copy command has already been disposed")
        if self._delete_command is not None:
            self._delete_command.undo()
            return
        if not os.path.exists(self._source_path):
            raise RuntimeError(f"asset copy source no longer exists: {self._source_path}")
        if os.path.exists(self._destination_path):
            raise RuntimeError(
                "asset copy destination is occupied by an external change: "
                + self._destination_path
            )
        delete_command = ProjectAssetDeleteCommand(
            [self._destination_path],
            project_root=self._project_root,
            backup_root=self._backup_root,
            asset_database=self._asset_database,
            delete_fn=self._delete_fn,
            import_fn=self._import_fn,
            description="Remove Copied Asset",
        )
        try:
            result = self._copy_fn(
                self._source_path,
                self._destination_path,
                self._asset_database,
            )
        except Exception as copy_error:
            self._rollback_initial_copy(delete_command, copy_error)
            raise
        if not result or not same_path(result, self._destination_path):
            copy_error = RuntimeError(
                f"asset copy failed: {self._source_path} -> {self._destination_path}"
            )
            self._rollback_initial_copy(delete_command, copy_error)
            raise copy_error
        self._delete_command = delete_command

    def _rollback_initial_copy(
        self,
        delete_command: ProjectAssetDeleteCommand,
        copy_error: Exception,
    ) -> None:
        try:
            if os.path.exists(self._destination_path):
                delete_command.execute()
        except Exception as rollback_error:
            raise RuntimeError(
                "asset copy failed and rollback also failed: "
                f"{self._destination_path}: {rollback_error}"
            ) from copy_error
        finally:
            delete_command.dispose()

    def undo(self) -> None:
        if self._delete_command is None:
            raise RuntimeError("asset copy has not been executed")
        self._delete_command.execute()

    def redo(self) -> None:
        self.execute()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._delete_command is not None:
            self._delete_command.dispose()


class ProjectAssetPasteCommand(UndoCommand):
    """Apply a planned multi-asset paste as one chronological action."""

    marks_dirty = False

    def __init__(
        self,
        commands: list[UndoCommand] | tuple[UndoCommand, ...],
        result_paths: list[str] | tuple[str, ...],
        *,
        on_applied: Optional[Callable[[list[str]], None]] = None,
        on_reverted: Optional[Callable[[], None]] = None,
        description: str = "Paste Assets",
    ) -> None:
        super().__init__(description)
        if not commands:
            raise ValueError("asset paste command requires at least one mutation")
        if not result_paths:
            raise ValueError("asset paste command requires at least one result path")
        self._compound = CompoundCommand(list(commands), description)
        self._compound.bind_operation_id(self.operation_id)
        self._result_paths = [resolved_path(path) for path in result_paths]
        self._on_applied = on_applied
        self._on_reverted = on_reverted

    def _notify_applied_or_rollback(self) -> None:
        if self._on_applied is None:
            return
        try:
            self._on_applied(list(self._result_paths))
        except Exception:
            self._compound.undo()
            raise

    def execute(self) -> None:
        self._compound.execute()
        self._notify_applied_or_rollback()

    def undo(self) -> None:
        self._compound.undo()
        if self._on_reverted is not None:
            self._on_reverted()

    def redo(self) -> None:
        self._compound.redo()
        self._notify_applied_or_rollback()

    def dispose(self) -> None:
        self._compound.dispose()
