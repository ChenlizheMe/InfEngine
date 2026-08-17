"""Single command authority for Project asset structure mutations."""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Optional

from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    resolved_path,
    same_path,
)

from .action_journal import ActionOrigin
from .selection import SelectionService


class ProjectAssetCommandService:
    """Build and execute every editor-visible Project asset mutation.

    ``project_file_ops`` remains the low-level filesystem/AssetDatabase layer.
    Panels and automation must enter through this service so they share one
    Undo action, context snapshot, selection consequence and rollback policy.
    """

    _instance: Optional["ProjectAssetCommandService"] = None

    def __init__(self, selection: SelectionService) -> None:
        self._selection = selection
        self._project_root = ""
        self._backup_root = ""
        self._asset_database: Any = None
        self._change_listeners: list[Callable[[], None]] = []
        ProjectAssetCommandService._instance = self

    @classmethod
    def instance(cls) -> Optional["ProjectAssetCommandService"]:
        return cls._instance

    @property
    def configured(self) -> bool:
        return bool(self._project_root)

    @property
    def project_root(self) -> str:
        return self._project_root

    @property
    def asset_database(self) -> Any:
        return self._asset_database

    def configure(self, project_root: str, asset_database: Any = None) -> None:
        root = resolved_path(project_root)
        if not root:
            raise ValueError("Project asset commands require a project root")
        if self._project_root and not same_path(self._project_root, root):
            raise RuntimeError("Project asset commands are already bound to another project")
        self._project_root = root
        self._backup_root = os.path.join(root, "Library", "EditorUndo")
        self._asset_database = asset_database

    def add_change_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._change_listeners:
            self._change_listeners.append(callback)

    def remove_change_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._change_listeners.remove(callback)
        except ValueError:
            pass

    def create(
        self,
        current_path: str,
        creator: Callable[[], Any],
        *,
        description: str = "Create Asset",
        origin: ActionOrigin = ActionOrigin.USER,
        replace_path: str = "",
    ) -> Any:
        command = self._create_command(current_path, creator, description=description)
        if replace_path:
            from Infernux.engine.undo import (
                ProjectAssetDeleteCommand,
                ProjectAssetPasteCommand,
            )

            replacement = self._project_path(replace_path)
            if not os.path.exists(replacement):
                raise FileNotFoundError(f"Asset replacement target does not exist: {replacement}")
            delete_command = ProjectAssetDeleteCommand(
                [replacement],
                project_root=self._project_root,
                backup_root=self._backup_root,
                asset_database=self._asset_database,
                on_deleted=self._notify_changed,
                on_restored=self._notify_changed,
                description="Replace Existing Asset",
            )
            compound = ProjectAssetPasteCommand(
                [delete_command, command],
                [replacement],
                on_applied=lambda _paths: self._notify_changed(),
                on_reverted=self._notify_changed,
                description=description,
            )
            self._execute(compound, origin)
        else:
            self._execute(command, origin)
        return command.result

    def create_with_path(
        self,
        current_path: str,
        creator: Callable[[], Any],
        *,
        description: str = "Create Asset",
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> str:
        """Create one asset and return its authoritative discovered path."""
        command = self._create_command(
            current_path,
            creator,
            description=description,
        )
        self._execute(command, origin)
        return str(command.created_path or "")

    def create_prefab(
        self,
        current_path: str,
        creator: Callable[[], Any],
        capture_linkage: Callable[[], Any],
        restore_linkage: Callable[[Any], None],
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> Any:
        from Infernux.engine.undo import ProjectPrefabCreateCommand

        asset_command = self._create_command(
            current_path,
            creator,
            description="Create Prefab",
        )
        command = ProjectPrefabCreateCommand(
            asset_command,
            capture_linkage,
            restore_linkage,
        )
        self._execute(command, origin)
        return asset_command.result

    def rename(
        self,
        source_path: str,
        new_name: str,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> str:
        from Infernux.engine.ui import project_file_ops
        from Infernux.engine.undo import ProjectAssetRenameCommand

        self._require_configured()
        source = self._project_path(source_path)
        destination = project_file_ops.rename_destination(source, new_name)
        if not destination or same_path(source, destination):
            return destination
        self._project_path(destination)
        command = ProjectAssetRenameCommand(
            source,
            destination,
            asset_database=self._asset_database,
            on_changed=self._notify_changed,
        )
        self._execute(command, origin)
        self._select_project_paths(
            (destination,),
            reason="project_asset_rename",
        )
        return destination

    def can_rename(self, source_path: str, new_name: str) -> bool:
        if not self.configured:
            return False
        from Infernux.engine.ui import project_file_ops

        try:
            source = self._project_path(source_path)
            if not os.path.exists(source):
                return False
            destination = project_file_ops.rename_destination(source, new_name)
            if not destination or same_path(source, destination):
                return False
            self._project_path(destination)
            return not os.path.exists(destination)
        except (OSError, RuntimeError, ValueError):
            return False

    def delete(
        self,
        paths: Iterable[str],
        *,
        origin: ActionOrigin = ActionOrigin.USER,
        clear_asset_selection: bool = True,
    ) -> tuple[str, ...]:
        from Infernux.engine.undo import ProjectAssetDeleteCommand

        self._require_configured()
        normalized = self.preflight_delete(paths)

        def on_deleted() -> None:
            if clear_asset_selection:
                self._clear_project_selection_if_needed(normalized)
            self._notify_changed()

        command = ProjectAssetDeleteCommand(
            list(normalized),
            project_root=self._project_root,
            backup_root=self._backup_root,
            asset_database=self._asset_database,
            on_deleted=on_deleted,
            on_restored=self._notify_changed,
            description="Delete Asset" if len(normalized) == 1 else "Delete Assets",
        )
        self._execute(command, origin)
        return normalized

    def preflight_delete(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Validate a complete delete before confirmation or filesystem work.

        Scene documents are graph roots and must never be removed while they
        are active or open.  Non-scene authoring resources are intentionally
        allowed: the committed AssetMutation is then consumed by the common
        WindowManager/DocumentRegistry deletion contract, which closes their
        views and discards their dormant state.  This check is shared by the
        visible Project panel and MCP because both eventually call ``delete``.
        """
        self._require_configured()
        normalized = tuple(self._project_path(path) for path in paths)
        if not normalized:
            raise ValueError("Asset deletion requires at least one path")

        from .documents import DocumentKind, DocumentRegistry

        registry = DocumentRegistry._instance
        opened = tuple(
            document
            for root in normalized
            if registry is not None
            for document in registry.documents_under_resource(root)
        )
        opened_scene = next(
            (
                document
                for document in opened
                if document.kind is DocumentKind.SCENE
            ),
            None,
        )

        active_scene_path = ""
        try:
            from Infernux.engine.scene_manager import SceneFileManager

            manager = SceneFileManager.instance()
            active_scene_path = str(
                getattr(manager, "current_scene_path", "") or ""
            )
        except (ImportError, RuntimeError, AttributeError):
            pass

        if active_scene_path:
            for root in normalized:
                if same_path(active_scene_path, root) or is_path_within(
                    active_scene_path,
                    root,
                    allow_root=True,
                ):
                    raise ValueError(
                        "Refusing to delete the active scene. "
                        "Save/close it through the scene API first."
                    )

        if opened_scene is not None:
            raise ValueError(
                "Refusing to delete an open scene document. "
                "Close it through the scene API first."
            )

        return normalized

    def transfer_to_directory(
        self,
        paths: Iterable[str],
        destination: str,
        *,
        cut: bool,
        description: str = "",
        origin: ActionOrigin = ActionOrigin.USER,
        select_results: bool = False,
    ) -> tuple[str, ...]:
        from Infernux.engine.ui import project_file_ops

        self._require_configured()
        sources = tuple(self._project_path(path) for path in paths)
        destination_dir = self._project_path(destination, allow_root=True)
        planned = project_file_ops.plan_asset_paste(
            sources,
            destination_dir,
            cut=bool(cut),
        )
        if not planned:
            return ()
        return self._execute_plan(
            planned,
            cut=bool(cut),
            overwrite_paths=(),
            description=description or ("Move Assets" if cut else "Paste Assets"),
            origin=origin,
            select_results=select_results,
        )

    def import_external(
        self,
        paths: Iterable[str],
        destination: str,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
        select_results: bool = True,
    ) -> tuple[str, ...]:
        """Import one OS-drop batch through the global asset history.

        External sidecar metadata is deliberately ignored. Imported assets
        receive identities from this project and the whole drop is one atomic
        Undo/Redo action, including directories containing multiple assets.
        """
        from Infernux.engine.ui import project_file_ops

        self._require_configured()
        sources = tuple(
            self._external_source_path(path)
            for path in paths
            if str(path or "").strip()
            and not str(path).lower().endswith(".meta")
        )
        if not sources:
            return ()
        destination_dir = self._project_path(destination, allow_root=True)
        planned = project_file_ops.plan_asset_paste(
            sources,
            destination_dir,
            cut=False,
        )
        if not planned:
            return ()
        return self._execute_plan(
            planned,
            cut=False,
            overwrite_paths=(),
            description=(
                "Import External Asset"
                if len(planned) == 1
                else "Import External Assets"
            ),
            origin=origin,
            select_results=select_results,
            allow_external_sources=True,
        )

    def can_import_external(
        self,
        paths: Iterable[str],
        destination: str,
    ) -> bool:
        if not self.configured:
            return False
        try:
            sources = tuple(
                self._external_source_path(path)
                for path in paths
                if str(path or "").strip()
                and not str(path).lower().endswith(".meta")
            )
            if not sources:
                return False
            destination_dir = self._project_path(destination, allow_root=True)
            return os.path.isdir(destination_dir)
        except (OSError, RuntimeError, ValueError):
            return False

    def move(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        origin: ActionOrigin = ActionOrigin.USER,
        select_result: bool = False,
    ) -> str:
        source = self._project_path(source_path)
        destination = self._project_path(destination_path)
        if same_path(source, destination):
            return destination
        overwrite_paths = (destination,) if overwrite and os.path.exists(destination) else ()
        result = self._execute_plan(
            ((source, destination),),
            cut=True,
            overwrite_paths=overwrite_paths,
            description="Move Asset",
            origin=origin,
            select_results=select_result,
        )
        return result[0]

    def copy(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        origin: ActionOrigin = ActionOrigin.USER,
        select_result: bool = False,
    ) -> str:
        source = self._project_path(source_path)
        destination = self._project_path(destination_path)
        if same_path(source, destination):
            raise ValueError("Asset copy requires a different destination")
        overwrite_paths = (destination,) if overwrite and os.path.exists(destination) else ()
        result = self._execute_plan(
            ((source, destination),),
            cut=False,
            overwrite_paths=overwrite_paths,
            description="Copy Asset",
            origin=origin,
            select_results=select_result,
        )
        return result[0]

    def shutdown(self) -> None:
        self._change_listeners.clear()
        self._project_root = ""
        self._backup_root = ""
        self._asset_database = None
        if ProjectAssetCommandService._instance is self:
            ProjectAssetCommandService._instance = None

    def _execute_plan(
        self,
        planned: Iterable[tuple[str, str]],
        *,
        cut: bool,
        overwrite_paths: Iterable[str],
        description: str,
        origin: ActionOrigin,
        select_results: bool,
        allow_external_sources: bool = False,
    ) -> tuple[str, ...]:
        from Infernux.engine.undo import (
            ProjectAssetCopyCommand,
            ProjectAssetDeleteCommand,
            ProjectAssetMoveBatchCommand,
            ProjectAssetPasteCommand,
        )

        self._require_configured()
        normalize_source = (
            self._external_source_path
            if allow_external_sources
            else self._project_path
        )
        pairs = tuple(
            (normalize_source(source), self._project_path(destination))
            for source, destination in planned
        )
        if not pairs:
            return ()
        overwrites = tuple(self._project_path(path) for path in overwrite_paths)
        occupied = [
            destination
            for _source, destination in pairs
            if os.path.exists(destination)
            and not any(same_path(destination, path) for path in overwrites)
        ]
        if occupied:
            raise FileExistsError(f"Asset destination already exists: {occupied[0]}")

        commands: list[Any] = []
        if overwrites:
            commands.append(
                ProjectAssetDeleteCommand(
                    list(overwrites),
                    project_root=self._project_root,
                    backup_root=self._backup_root,
                    asset_database=self._asset_database,
                    on_deleted=self._notify_changed,
                    on_restored=self._notify_changed,
                    description="Replace Existing Asset",
                )
            )
        if cut:
            commands.append(
                ProjectAssetMoveBatchCommand(
                    pairs,
                    asset_database=self._asset_database,
                    description=description,
                )
            )
        else:
            commands.extend(
                ProjectAssetCopyCommand(
                    source,
                    destination,
                    project_root=self._project_root,
                    backup_root=self._backup_root,
                    asset_database=self._asset_database,
                )
                for source, destination in pairs
            )
        result_paths = tuple(destination for _source, destination in pairs)

        def on_applied(paths: list[str]) -> None:
            self._notify_changed()
            if select_results:
                self._select_project_paths(paths)

        command = ProjectAssetPasteCommand(
            commands,
            list(result_paths),
            on_applied=on_applied,
            on_reverted=self._notify_changed,
            description=description,
        )
        self._execute(command, origin)
        return result_paths

    def _execute(self, command: Any, origin: ActionOrigin) -> None:
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            command.dispose()
            raise RuntimeError("Global editor history is unavailable")
        if not manager.execute(command, origin=ActionOrigin(origin)):
            raise RuntimeError(f"Editor asset command was rejected: {command.description}")

    def _create_command(
        self,
        current_path: str,
        creator: Callable[[], Any],
        *,
        description: str,
    ) -> Any:
        from Infernux.engine.undo import ProjectAssetCreateCommand

        self._require_configured()
        return ProjectAssetCreateCommand(
            self._project_path(current_path, allow_root=True),
            creator,
            project_root=self._project_root,
            backup_root=self._backup_root,
            asset_database=self._asset_database,
            on_changed=self._notify_changed,
            description=description,
        )

    def _project_path(self, value: str, *, allow_root: bool = False) -> str:
        path = resolved_path(value)
        if not path or not is_path_within(path, self._project_root, allow_root=allow_root):
            raise ValueError(f"Asset path is outside the project: {value}")
        return path

    @staticmethod
    def _external_source_path(value: str) -> str:
        path = resolved_path(value)
        if not path or not os.path.exists(path):
            raise ValueError(f"External asset source does not exist: {value}")
        return path

    def _notify_changed(self) -> None:
        for callback in tuple(self._change_listeners):
            try:
                callback()
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("ProjectAssetCommandService.listener", exc)

    def _select_project_paths(
        self,
        paths: Iterable[str],
        *,
        reason: str = "project_asset_transfer",
    ) -> None:
        from .descriptors import SelectionTarget

        targets = tuple(SelectionTarget.asset(path) for path in paths)
        if not targets:
            return
        self._selection.replace(
            targets,
            owner_id="project",
            primary=targets[-1],
            reason=reason,
            record_history=False,
        )

    def _clear_project_selection_if_needed(self, paths: tuple[str, ...]) -> None:
        from .descriptors import SelectionDomain

        snapshot = self._selection.snapshot
        if snapshot.domain not in {
            SelectionDomain.ASSET,
            SelectionDomain.ASSET_SUBRESOURCE,
        }:
            return
        for target in snapshot.targets:
            target_path = target.document_id or target.target_id
            if any(is_path_within(target_path, path, allow_root=True) for path in paths):
                self._selection.clear(
                    reason="project_asset_delete",
                    record_history=False,
                )
                return

    def _require_configured(self) -> None:
        if not self._project_root:
            raise RuntimeError("Project asset command service is not configured")


class ProjectAssetInteractionService:
    """Own Project-window user intents above filesystem asset commands.

    The native Project panel is a view and input source.  It must never own
    clipboard semantics, confirmation policy, asset creation, opening, or
    filesystem mutations.  Those application adapters are configured once
    during editor bootstrap and every menu, shortcut, pointer gesture, and
    automation request enters through this service.
    """

    def __init__(
        self,
        assets: ProjectAssetCommandService,
        clipboard: Any,
    ) -> None:
        self._assets = assets
        self._clipboard = clipboard
        self._unique_name: Optional[Callable[[str, str, str], str]] = None
        self._create: Optional[
            Callable[[str, str, str, str], Any]
        ] = None
        self._open: Optional[Callable[[str, str], bool]] = None
        self._reveal: Optional[Callable[[str], bool]] = None
        self._read_external_clipboard: Optional[Callable[[], Iterable[str]]] = None
        self._request_delete: Optional[
            Callable[[tuple[str, ...], Callable[[list[str]], bool]], bool]
        ] = None

    @property
    def configured(self) -> bool:
        return bool(
            self._assets.configured
            and self._unique_name is not None
            and self._create is not None
            and self._open is not None
            and self._reveal is not None
            and self._read_external_clipboard is not None
            and self._request_delete is not None
        )

    def configure(
        self,
        *,
        unique_name: Callable[[str, str, str], str],
        create: Callable[[str, str, str, str], Any],
        open_asset: Callable[[str, str], bool],
        reveal: Callable[[str], bool],
        read_external_clipboard: Callable[[], Iterable[str]],
        request_delete: Callable[
            [tuple[str, ...], Callable[[list[str]], bool]], bool
        ],
    ) -> None:
        callbacks = {
            "unique_name": unique_name,
            "create": create,
            "open_asset": open_asset,
            "reveal": reveal,
            "read_external_clipboard": read_external_clipboard,
            "request_delete": request_delete,
        }
        invalid = tuple(name for name, callback in callbacks.items() if not callable(callback))
        if invalid:
            raise TypeError(f"Project asset interaction callbacks are invalid: {invalid}")
        self._unique_name = unique_name
        self._create = create
        self._open = open_asset
        self._reveal = reveal
        self._read_external_clipboard = read_external_clipboard
        self._request_delete = request_delete

    def can_copy(self, paths: Iterable[str]) -> bool:
        return bool(self._valid_project_paths(paths))

    def copy(self, paths: Iterable[str], *, cut: bool) -> bool:
        from .clipboard import (
            ClipboardDomain,
            ClipboardItem,
            ClipboardOperation,
        )

        resolved = self._valid_project_paths(paths)
        if not resolved:
            return False
        self._clipboard.write(
            ClipboardDomain.ASSET,
            tuple(ClipboardItem(path) for path in resolved),
            operation=(ClipboardOperation.CUT if cut else ClipboardOperation.COPY),
            source_owner_id="project",
            reason="cut_assets" if cut else "copy_assets",
        )
        return True

    def can_paste(self, destination: str) -> bool:
        # OS clipboard inspection can enter platform APIs. Keep can_execute a
        # pure, cheap query and let paste() report a no-op when no payload is
        # available.
        return bool(self.configured and self._valid_destination(destination))

    def paste(
        self,
        destination: str,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> tuple[str, ...]:
        from .clipboard import ClipboardDomain, ClipboardOperation

        if not self.configured:
            raise RuntimeError("Project asset interactions are not configured")
        payload = self._clipboard.peek(ClipboardDomain.ASSET)
        if payload is None:
            paths = self._external_clipboard_paths()
            if not paths:
                return ()
            return self._assets.import_external(
                paths,
                destination,
                origin=origin,
                select_results=True,
            )

        paths = tuple(item.target_id for item in payload.items)
        cut = payload.operation is ClipboardOperation.CUT
        results = self._assets.transfer_to_directory(
            paths,
            destination,
            cut=cut,
            description="Move Assets" if cut else "Paste Assets",
            origin=origin,
            select_results=True,
        )
        if results and cut:
            self._clipboard.consume_cut(payload.revision)
        return results

    def transfer(
        self,
        paths: Iterable[str],
        destination: str,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> tuple[str, ...]:
        return self._assets.transfer_to_directory(
            paths,
            destination,
            cut=True,
            description="Move Assets",
            origin=origin,
            select_results=True,
        )

    def request_delete(
        self,
        paths: Iterable[str],
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> bool:
        if not self.configured or self._request_delete is None:
            return False
        resolved = self._valid_project_paths(paths)
        if not resolved:
            return False

        # Fail closed before opening a confirmation dialog.  ``delete``
        # repeats the same preflight at execution time for TOCTOU safety.
        try:
            self._assets.preflight_delete(resolved)
        except (OSError, RuntimeError, ValueError):
            return False

        def delete_confirmed(confirmed: list[str]) -> bool:
            return bool(self._assets.delete(confirmed, origin=origin))

        return bool(self._request_delete(resolved, delete_confirmed))

    def can_create(
        self,
        kind: str,
        directory: str,
        base_name: str,
        extension: str,
    ) -> bool:
        return bool(
            self.configured
            and str(kind or "").strip()
            and str(base_name or "").strip()
            and self._valid_destination(directory)
            and self._unique_name is not None
            and self._create is not None
        )

    def create(
        self,
        kind: str,
        directory: str,
        base_name: str,
        extension: str,
        variant: str = "",
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> str:
        if not self.can_create(kind, directory, base_name, extension):
            return ""
        assert self._unique_name is not None and self._create is not None
        name = str(self._unique_name(directory, base_name, extension) or "").strip()
        if not name:
            return ""
        return self._assets.create_with_path(
            directory,
            lambda: self._create(kind, directory, name, variant),
            description=f"Create {str(kind).replace('_', ' ').title()}",
            origin=origin,
        )

    def can_open(self, kind: str, path: str) -> bool:
        return bool(
            self.configured
            and str(kind or "").strip()
            and self._valid_project_paths((path,))
        )

    def open(self, kind: str, path: str) -> bool:
        if not self.can_open(kind, path) or self._open is None:
            return False
        return bool(self._open(str(kind), resolved_path(path)))

    def reveal(self, path: str) -> bool:
        if not self.configured or self._reveal is None:
            return False
        resolved = self._valid_project_paths((path,))
        return bool(resolved and self._reveal(resolved[0]))

    def shutdown(self) -> None:
        self._unique_name = None
        self._create = None
        self._open = None
        self._reveal = None
        self._read_external_clipboard = None
        self._request_delete = None

    def _valid_project_paths(self, paths: Iterable[str]) -> tuple[str, ...]:
        resolved: list[str] = []
        seen: set[str] = set()
        for value in paths:
            try:
                path = self._assets._project_path(str(value or ""))
            except (OSError, RuntimeError, ValueError):
                continue
            key = path_key(path)
            if key in seen or not os.path.exists(path):
                continue
            seen.add(key)
            resolved.append(path)
        return tuple(resolved)

    def _valid_destination(self, destination: str) -> bool:
        try:
            path = self._assets._project_path(destination, allow_root=True)
        except (OSError, RuntimeError, ValueError):
            return False
        return os.path.isdir(path)

    def _external_clipboard_paths(self) -> tuple[str, ...]:
        if self._read_external_clipboard is None:
            return ()
        try:
            values = self._read_external_clipboard()
        except Exception:
            return ()
        result: list[str] = []
        for value in values or ():
            path = resolved_path(str(value or ""))
            if path and os.path.exists(path) and not path.lower().endswith(".meta"):
                result.append(path)
        return tuple(result)
