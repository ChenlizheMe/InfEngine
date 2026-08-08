"""
Scene file management for Infernux.

Handles:
- Tracking the current scene file path (.scene)
- Saving / loading scene files (delegates to C++ Scene::SaveToFile / LoadFromFile)
- Python component serialization during save, recreation during load
- Remembering last opened scene per project (EditorSettings.json)
- Default scene fallback when a scene file is missing
- File-dialog for "Save As" when the scene has no file yet
- Enforcing that scenes must be saved under Assets/

The C++ layer already provides ``Scene.serialize / deserialize / save_to_file /
load_from_file`` and ``PendingPyComponent`` for Python component recreation.
This module orchestrates those primitives into a complete workflow.
"""

import os
from Infernux.engine.path_utils import path_key, resolved_path
import json
from dataclasses import dataclass
from typing import Any, Optional, Callable

from Infernux.debug import Debug
from Infernux.engine.project_context import get_project_root


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCENE_EXTENSION = ".scene"
EDITOR_SETTINGS_FILE = "EditorSettings.json"
DEFAULT_SCENE_NAME = "Untitled Scene"
DEFAULT_SCENE_FILE_BASE = "UntitledScene"
PREFAB_MODE_SCENE_NAME = "__PrefabMode__"
PREFAB_RESTORE_SCENE_NAME = "__PrefabRestore__"


@dataclass(slots=True)
class _SceneRestoreSnapshot:
    locator: Any
    document: dict
    resource_path: str
    title: str
    revision: int
    saved_revision: int


def _empty_scene_document(name: str) -> dict:
    return {
        "name": name,
        "isPlaying": False,
        "objects": [],
    }


def _get_scene_root_objects(scene):
    if scene is None:
        return []
    if hasattr(scene, "get_root_objects"):
        roots = scene.get_root_objects()
        return roots if roots is not None else []
    if hasattr(scene, "get_root_game_objects"):
        roots = scene.get_root_game_objects()
        return roots if roots is not None else []
    return []


# ---------------------------------------------------------------------------
# Editor settings helpers (ProjectSettings/EditorSettings.json)
# ---------------------------------------------------------------------------

def _settings_path() -> Optional[str]:
    root = _effective_project_root()
    if not root:
        return None
    return os.path.join(root, "ProjectSettings", EDITOR_SETTINGS_FILE)


def _effective_project_root() -> Optional[str]:
    """Best-effort project-root resolution for editor/runtime edge cases."""
    root = get_project_root()
    if root and os.path.isdir(root):
        return root

    try:
        from Infernux.engine.ui.editor_services import EditorServices
        services = EditorServices.instance()
        if services and services.project_path and os.path.isdir(services.project_path):
            return resolved_path(services.project_path)
    except Exception as exc:
        Debug.log_suppressed("scene_manager._effective_project_root.editor_services", exc)

    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "Assets")):
        return cwd
    return None


def _load_editor_settings() -> dict:
    path = _settings_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _save_editor_settings(settings: dict):
    path = _settings_path()
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from Infernux.core.document_store import write_document_text
    write_document_text(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# SceneFileManager  — the main public API
# ---------------------------------------------------------------------------

from ._scene_prefab import ScenePrefabMixin
from ._scene_save import SceneSaveMixin


class SceneFileManager(ScenePrefabMixin, SceneSaveMixin):
    """Manages the mapping between the active C++ Scene and its file on disk.

    Typical usage (wired in ``release_engine``):

        sfm = SceneFileManager()
        # at startup:
        sfm.load_last_scene_or_default()
        # on Ctrl+S:
        sfm.save_current_scene()
        # on double-click a .scene in Project panel:
        sfm.open_scene(path)
    """

    _instance: Optional["SceneFileManager"] = None

    def __init__(self):
        SceneFileManager._instance = self
        self._current_scene_path: Optional[str] = None
        self._scene_document_id: str = ""
        self._previous_scene_document_id: str = ""
        self._on_scene_changed: Optional[Callable[[], None]] = None
        self._pending_save_path: Optional[str] = None
        self._pending_save_ticket_id: str = ""
        self._pending_save_document_id: str = ""
        # Automation receives an editor modal it can address semantically;
        # desktop users receive the platform-native Save As dialog.
        self._save_as_popup_open: bool = False
        self._save_as_popup_requested: bool = False
        self._save_as_focus_name: bool = False
        self._save_as_agent_modal: bool = False
        self._save_as_native_dialog_pending: bool = False
        self._save_as_folder: str = "Assets"
        self._save_as_name: str = ""
        self._save_as_error: str = ""
        self._save_as_modal_service = None
        self._asset_database = None  # Set via set_asset_database()
        self._engine = None  # set via set_engine()

        # Deferred scene loading — actual load runs on the NEXT frame so
        # the scene view has one frame to stop rendering old 3D content,
        # preventing in-flight GPU resources from being destroyed mid-use.
        self._deferred_load_path: Optional[str] = None   # non-None → load pending
        self._deferred_new_scene: bool = False            # True → new scene pending
        self._deferred_exit_prefab: bool = False           # True → exit prefab mode task pending
        self._post_prefab_exit_callback: Optional[Callable[[], None]] = None
        self._scene_transaction = None
        self._scene_transaction_path: Optional[str] = None
        self._pending_external_reload: Optional[tuple[str, str]] = None
        self._scene_restore_snapshots: dict[str, _SceneRestoreSnapshot] = {}
        self._pending_scene_before_context = None

        # True while _do_open_scene / _do_new_scene is running.
        # Prevents stacking deferred loads from rapid user clicks.
        self._load_in_progress: bool = False

        # Guard against repeated request_close() calls while the shared
        # document close transaction is active.
        self._close_in_progress: bool = False

        # Prefab Mode state
        self.is_prefab_mode = False
        self.prefab_mode_path = None
        self.prefab_envelope = {}
        self._previous_scene_path = None
        self._previous_scene_document = None
        self._replace_scene_document(
            kind="scene",
            resource_path="",
            title=DEFAULT_SCENE_NAME,
            dirty=False,
        )

    @classmethod
    def instance(cls) -> Optional["SceneFileManager"]:
        return cls._instance

    def set_asset_database(self, asset_db):
        """Set the AssetDatabase for GUID→path resolution during scene load."""
        self._asset_database = asset_db
        if self._current_scene_path and self._scene_document_id:
            from Infernux.engine.interaction import DocumentRegistry

            DocumentRegistry.instance().rekey(
                self._scene_document_id,
                self._document_key("scene", self._current_scene_path),
                resource_path=self._current_scene_path,
            )

    def set_engine(self, engine):
        """Set the native Infernux reference (for close-request handling)."""
        self._engine = engine

    def _native_engine_for_close(self):
        """Return native engine for close confirmation, with service fallback."""
        if self._engine is not None:
            return self._engine
        try:
            from Infernux.engine.ui.editor_services import EditorServices
            services = EditorServices.instance()
            native = services.native_engine if services else None
            if native is not None:
                self._engine = native
            return native
        except Exception as exc:
            Debug.log_suppressed("SceneFileManager._native_engine_for_close.editor_services", exc)
            return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_scene_path(self) -> Optional[str]:
        return self._current_scene_path

    @property
    def document_id(self) -> str:
        return self._scene_document_id

    def owns_document(self, document_id: str) -> bool:
        """Return whether this manager owns the active or suspended scene document."""
        identifier = str(document_id or "")
        return bool(
            identifier
            and identifier
            in {self._scene_document_id, self._previous_scene_document_id}
        )

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def _dirty(self) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().get(self._scene_document_id)
        return bool(document and document.is_dirty)

    @property
    def is_loading(self) -> bool:
        """True while a deferred scene load is pending."""
        return (
            self._scene_transaction is not None
            or self._deferred_load_path is not None
            or self._deferred_new_scene
            or self._deferred_exit_prefab
        )

    def mark_dirty(self):
        """Record one real scene-content mutation."""
        if self._is_play_mode():
            return
        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().get(self._scene_document_id)
        if document is not None:
            DocumentRegistry.instance().mark_changed(document.document_id)

    def _document_key(self, kind: str, resource_path: str):
        from Infernux.engine.interaction import DocumentKey, DocumentKind

        document_kind = DocumentKind(kind)
        path = resolved_path(resource_path) if resource_path else ""
        if path and self._asset_database is not None:
            try:
                guid = self._asset_database.get_guid_from_path(path) or ""
            except Exception:
                guid = ""
            if guid:
                return DocumentKey.asset(document_kind, guid)
        if path:
            return DocumentKey.resource(document_kind, path)
        return DocumentKey.session(document_kind)

    def _replace_scene_document(
        self,
        *,
        kind: str,
        resource_path: str,
        title: str,
        dirty: bool,
        preserve_previous: bool = False,
        key_override=None,
        stable_id: str = "",
        revision: Optional[int] = None,
        saved_revision: Optional[int] = None,
    ):
        from Infernux.engine.interaction import (
            DocumentCapability,
            DocumentRegistry,
        )

        registry = DocumentRegistry.instance()
        previous_id = self._scene_document_id
        path = resolved_path(resource_path) if resource_path else ""
        capabilities = DocumentCapability.SAVE | DocumentCapability.DISCARD
        if kind == "scene":
            capabilities |= DocumentCapability.SAVE_AS
        document, created = registry.open_or_create(
            key_override or self._document_key(kind, path),
            title,
            stable_id=stable_id,
            resource_path=path,
            revision=(1 if dirty else 0) if revision is None else int(revision),
            saved_revision=(
                0 if saved_revision is None else int(saved_revision)
            ),
            capabilities=capabilities,
            controller=self,
        )
        self._scene_document_id = document.document_id
        if not created:
            if revision is not None or saved_revision is not None:
                registry.restore_revision_state(
                    document.document_id,
                    revision=(document.revision if revision is None else int(revision)),
                    saved_revision=(
                        document.saved_revision
                        if saved_revision is None
                        else int(saved_revision)
                    ),
                )
            elif dirty and not document.is_dirty:
                registry.mark_changed(document.document_id)
            elif not dirty and document.is_dirty:
                registry.establish_loaded_baseline(document.document_id)
        if previous_id and previous_id != document.document_id and not preserve_previous:
            previous = registry.get(previous_id)
            if previous is not None and not previous.view_ids:
                # Scene history may reopen this document after several other
                # scene transitions. Retire it to dormant state so its
                # stable identity and controller snapshot remain addressable.
                registry.unregister(previous_id, preserve_dormant=True)
        if previous_id and previous_id != document.document_id:
            try:
                from Infernux.engine.interaction import FocusService

                focus = FocusService.instance()
                snapshot = focus.snapshot
                if snapshot.active_document_id == previous_id:
                    focus.activate_panel(
                        snapshot.active_panel_id,
                        view_id=snapshot.active_view_id,
                        document_id=document.document_id,
                        child_context_id=snapshot.child_context_id,
                        reason="scene_document_replaced",
                        record_history=False,
                    )
            except (AttributeError, ImportError, RuntimeError):
                pass
        return document

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del guid
        if document_id != self._scene_document_id:
            return
        if (
            self._current_scene_path
            and path_key(self._current_scene_path) == path_key(source_path)
        ):
            self._current_scene_path = resolved_path(destination_path)
            self._remember_last_scene(self._current_scene_path)

    def save_session_state(self) -> dict:
        """Capture a recoverable editor-session draft for an unsaved scene."""
        if not self._dirty or self.is_prefab_mode or self._is_play_mode():
            return {"dirty": False}
        try:
            from Infernux.lib import SceneManager

            scene = SceneManager.instance().get_active_scene()
            document = scene.serialize_document() if scene else None
        except Exception as exc:
            Debug.log_suppressed("SceneFileManager.save_session_state", exc)
            document = None
        state = {
            "dirty": True,
            "current_scene_path": self._current_scene_path or "",
        }
        if isinstance(document, dict):
            state["document"] = document
        return state

    def restore_session_state(self, data: dict) -> bool:
        """Restore the previous session's scene draft without writing an asset."""
        if not isinstance(data, dict) or not bool(data.get("dirty")):
            return False
        document = data.get("document")
        if isinstance(document, dict):
            try:
                from Infernux.lib import SceneManager
                from Infernux.engine.scene_document_transaction import SceneDocumentTransaction

                scene = SceneManager.instance().get_active_scene()
                if scene is None:
                    return False
                transaction = SceneDocumentTransaction(
                    scene,
                    document=document,
                    asset_database=self._asset_database,
                    clear_registries=True,
                    before_commit=self._prepare_native_scene_swap,
                )
                if not transaction.run_to_completion(raise_on_failure=False):
                    Debug.log_warning(f"Scene session draft restore failed: {transaction.error}")
                    return False
            except Exception as exc:
                Debug.log_suppressed("SceneFileManager.restore_session_state", exc)
                return False

        path = str(data.get("current_scene_path") or "").strip()
        self._current_scene_path = resolved_path(path) if path and os.path.isfile(path) else None
        self._replace_scene_document(
            kind="scene",
            resource_path=self._current_scene_path or "",
            title=(
                os.path.splitext(os.path.basename(self._current_scene_path))[0]
                if self._current_scene_path
                else DEFAULT_SCENE_NAME
            ),
            dirty=True,
        )
        self._reset_undo_history()
        if self._on_scene_changed:
            self._on_scene_changed()
        return True

    def set_on_scene_changed(self, cb: Callable[[], None]):
        """Register callback invoked after a scene is opened/created."""
        self._on_scene_changed = cb

    def _capture_active_scene_snapshot(self) -> Optional[_SceneRestoreSnapshot]:
        if self.is_prefab_mode or self._is_play_mode():
            return None
        try:
            from Infernux.engine.interaction import DocumentRegistry
            from Infernux.lib import SceneManager

            registry = DocumentRegistry.instance()
            document = registry.get(self._scene_document_id)
            locator = registry.locate(self._scene_document_id)
            scene = SceneManager.instance().get_active_scene()
            payload = scene.serialize_document() if scene is not None else None
            if document is None or locator is None or not isinstance(payload, dict):
                return None
            return _SceneRestoreSnapshot(
                locator=locator,
                document=payload,
                resource_path=self._current_scene_path or "",
                title=document.title,
                revision=document.revision,
                saved_revision=document.saved_revision,
            )
        except Exception as exc:
            Debug.log_suppressed("SceneFileManager.capture_history_snapshot", exc)
            return None

    def _archive_active_scene(self) -> Optional[_SceneRestoreSnapshot]:
        snapshot = self._capture_active_scene_snapshot()
        if snapshot is not None:
            self._scene_restore_snapshots[snapshot.locator.stable_id] = snapshot
        return snapshot

    def _stage_scene_navigation(self) -> None:
        """Capture the departure Scene before a save/discard decision mutates it."""
        if self._pending_scene_before_context is not None:
            return
        self._archive_active_scene()
        try:
            from Infernux.engine.interaction import EditorInteractionCore

            core = EditorInteractionCore.instance()
            self._pending_scene_before_context = (
                core.capture_context() if core is not None else None
            )
        except (AttributeError, ImportError, RuntimeError):
            self._pending_scene_before_context = None

    def _cancel_scene_navigation(self) -> None:
        self._pending_scene_before_context = None

    def _publish_scene_navigation(self, description: str) -> None:
        before = self._pending_scene_before_context
        self._pending_scene_before_context = None
        if before is None:
            return
        try:
            from Infernux.engine.interaction import EditorInteractionCore
            from Infernux.engine.undo import GlobalContextCommand, UndoManager

            manager = UndoManager.instance()
            core = EditorInteractionCore.instance()
            if manager is None or core is None or manager.is_executing:
                return
            after = core.capture_context()
            if before == after:
                return
            manager.record(
                GlobalContextCommand(before, after, description=description),
                before_context=before,
                after_context=after,
            )
        except Exception as exc:
            Debug.log_suppressed("SceneFileManager.publish_scene_navigation", exc)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def _is_play_mode(self) -> bool:
        """Return True if the engine is in Play or Pause mode."""
        from Infernux.engine.play_mode import PlayModeManager, PlayModeState
        pm = PlayModeManager.instance()
        if pm and pm.state != PlayModeState.EDIT:
            return True
        return False

    # ------------------------------------------------------------------
    # Prefab instance refresh
    # ------------------------------------------------------------------

    def open_scene(self, path: str) -> bool:
        """Load a .scene file, replacing the current scene.

        If the current scene is dirty, shows a save-confirmation popup first.
        The actual load is deferred to the next frame so the scene view can
        stop rendering old 3D content first.
        """
        if self.is_loading:
            Debug.log_warning("Scene load already pending or in progress — ignoring open_scene()")
            return False
        if self.is_prefab_mode:
            return self._request_prefab_exit(
                on_complete=lambda: self._continue_open_scene(path),
            )

        return self._continue_open_scene(path)

    def _continue_open_scene(self, path: str) -> bool:
        """Resolve the active Scene document before scheduling a replacement."""
        if self.is_prefab_mode:
            return False
        self._stage_scene_navigation()

        # Save current camera state before switching
        if self._current_scene_path:
            self._save_camera_state(self._current_scene_path)
        if self._dirty:
            from Infernux.engine.ui.dirty_panel_confirmation import (
                DirtyPanelConfirmationCoordinator,
            )

            DirtyPanelConfirmationCoordinator.instance().request_document_replace(
                self.document_id,
                on_complete=lambda: self._begin_deferred_open(path),
                on_cancel=self._cancel_scene_navigation,
            )
            return False
        self._begin_deferred_open(path)
        return True

    def new_scene(self):
        """Replace the current scene with a fresh default scene (no file).

        If the current scene is dirty, shows a save-confirmation popup first.
        The actual creation is deferred to the next frame.
        """
        if self.is_prefab_mode:
            self._request_prefab_exit(on_complete=self._continue_new_scene)
            return

        self._continue_new_scene()

    def _continue_new_scene(self) -> None:
        """Resolve the active Scene document before creating a replacement."""
        if self.is_prefab_mode:
            return
        self._stage_scene_navigation()

        # Persist camera state before switching away
        if self._current_scene_path:
            self._save_camera_state(self._current_scene_path)
        if self._dirty:
            from Infernux.engine.ui.dirty_panel_confirmation import (
                DirtyPanelConfirmationCoordinator,
            )

            DirtyPanelConfirmationCoordinator.instance().request_document_replace(
                self.document_id,
                on_complete=self._begin_deferred_new,
                on_cancel=self._cancel_scene_navigation,
            )
            return
        self._begin_deferred_new()

    def request_close(self):
        """Called when the window close button is pressed.

        If the scene is dirty, shows a save-confirmation popup.
        Otherwise, confirms the close immediately.

        During play mode the close is confirmed without a save dialog
        because the live scene is a temporary simulation snapshot — saving
        it would persist play-mode state, not the user's edit-mode work.
        ``engine.exit()`` will restore and clean up play-mode state before
        the C++ teardown begins.
        """
        # Guard: the menu bar polls is_close_requested() every frame.
        # Without this, a dirty scene would re-open the save dialog
        # each frame, preventing the user from clicking any button.
        if self._close_in_progress:
            return
        self._close_in_progress = True

        from Infernux.engine.ui.dirty_panel_confirmation import (
            DirtyPanelConfirmationCoordinator,
        )

        DirtyPanelConfirmationCoordinator.instance().request_exit(
            self._continue_close_after_dirty_panels,
            self._cancel_close_after_dirty_panels,
        )

    def _cancel_close_after_dirty_panels(self) -> None:
        native = self._native_engine_for_close()
        if native:
            native.cancel_close()
        self._close_in_progress = False

    def _continue_close_after_dirty_panels(self) -> None:
        """Continue the existing scene close transaction after panel decisions."""
        if self._is_play_mode():
            native = self._native_engine_for_close()
            if native:
                native.confirm_close()
            return

        # The global close transaction has already visited the active Prefab
        # and its suspended Scene as separate documents. Closing the process
        # needs no scene swap and must not perform another implicit save.
        if self.is_prefab_mode:
            native = self._native_engine_for_close()
            if native:
                native.confirm_close()
            return

        # Always persist camera state before closing
        if self._current_scene_path:
            self._save_camera_state(self._current_scene_path)

        native = self._native_engine_for_close()
        if native:
            native.confirm_close()

    def load_last_scene_or_default(self):
        """Called at startup — load the last opened scene, or create a default.

        Uses immediate (non-deferred) loading since no rendering occurs yet.
        """
        settings = _load_editor_settings()
        last_scene = settings.get("lastOpenedScene")
        if last_scene and os.path.isfile(last_scene):
            if self._do_open_scene(last_scene, record_navigation=False):
                return
            Debug.log_warning(f"Last scene file missing or invalid: {last_scene}")

        # Fallback to default (immediate — no rendering loop yet)
        self._do_new_scene()

    # ------------------------------------------------------------------
    # Deferred scene loading (called from menu_bar every frame)
    # ------------------------------------------------------------------

    def _begin_deferred_open(self, path: str):
        """Schedule a scene open for the next frame."""
        self._deferred_load_path = path
        self._deferred_new_scene = False

    def _begin_deferred_new(self):
        """Schedule a new-scene creation for the next frame."""
        self._deferred_load_path = None
        self._deferred_new_scene = True

    def poll_deferred_load(self):
        """Execute a pending deferred scene load/new/prefab-exit.

        Must be called every frame (from menu_bar).  The one-frame delay
        between _begin_deferred_open/new and this method gives the
        current frame's GPU submission a chance to complete before
        _prepare_native_scene_swap() calls WaitForGpuIdle(), which
        performs a full vkDeviceWaitIdle + FlushDeletionQueue.

        The old scene's texture naturally remains in the render target
        until the new scene's first Execute() overwrites it, so no
        placeholder or extra-frame delay is needed.
        """
        if self._scene_transaction is not None:
            transaction = self._scene_transaction
            if not transaction.poll():
                return
            path = self._scene_transaction_path
            self._scene_transaction = None
            self._scene_transaction_path = None
            self._load_in_progress = False
            if transaction.succeeded:
                self._finish_open_scene(path)
            else:
                Debug.log_error(f"Scene load failed for '{path}': {transaction.error}")
                self._cancel_scene_navigation()
            return

        if self._load_in_progress:
            return
        if self._deferred_load_path is not None:
            path = self._deferred_load_path
            self._deferred_load_path = None
            self._load_in_progress = True
            try:
                transaction = self._create_open_scene_transaction(path)
                if transaction is None:
                    self._load_in_progress = False
                    self._cancel_scene_navigation()
                    return
                transaction.start()
                self._scene_transaction = transaction
                self._scene_transaction_path = resolved_path(path)
            except Exception as exc:
                Debug.log_error(f"Scene load failed: {exc}")
                self._load_in_progress = False
                self._cancel_scene_navigation()
        elif self._deferred_new_scene:
            self._deferred_new_scene = False
            self._load_in_progress = True
            try:
                self._do_new_scene()
            except Exception as exc:
                Debug.log_error(f"New scene failed: {exc}")
                self._cancel_scene_navigation()
            finally:
                self._load_in_progress = False

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def get_display_name(self) -> str:
        """Return a short display string for the current scene (for title bars)."""
        if self._current_scene_path:
            name = os.path.splitext(os.path.basename(self._current_scene_path))[0]
        else:
            name = DEFAULT_SCENE_NAME
        if self._dirty:
            name += " *"
        return name

    # ------------------------------------------------------------------
    # Internal — actual scene operations (no dirty check)
    # ------------------------------------------------------------------

    def _prepare_native_scene_swap(self):
        """Clear native editor state and drain GPU work before scene replacement."""
        if not self._engine:
            return

        # Clear editor-only native state first so the replacement frame cannot
        # reference stale scene objects through outline/gizmo paths.
        try:
            self._engine.clear_selection_outline()
        except Exception as exc:
            Debug.log_warning(f"Failed to clear selection outline: {exc}")

        try:
            self._engine.clear_component_gizmos()
        except Exception as exc:
            Debug.log_warning(f"Failed to clear component gizmos: {exc}")

        try:
            self._engine.clear_component_gizmo_icons()
        except Exception as exc:
            Debug.log_warning(f"Failed to clear gizmo icons: {exc}")

        try:
            self._engine.wait_for_gpu_idle()
        except Exception as exc:
            Debug.log_warning(f"Failed to drain GPU before scene switch: {exc}")

    def _create_open_scene_transaction(self, path: str):
        """Build a path-backed transaction without mutating the live scene."""
        if not path or not os.path.isfile(path):
            Debug.log_warning(f"Scene file not found: {path}")
            return None

        if not self._is_under_assets(path):
            Debug.log_warning("Scene file must be under the project's Assets/ directory.")
            return None

        from Infernux.lib import SceneManager
        sm = SceneManager.instance()
        scene = sm.get_active_scene()

        if not scene:
            scene = sm.create_scene(DEFAULT_SCENE_NAME)

        def before_commit():
            if sm.get_active_scene() is not scene:
                raise RuntimeError("active scene changed while scene document was loading")
            self._prepare_native_scene_swap()
            from Infernux.renderstack.render_stack import RenderStack
            RenderStack._active_instance = None

        from Infernux.engine.scene_document_transaction import SceneDocumentTransaction
        return SceneDocumentTransaction(
            scene,
            path=path,
            asset_database=self._asset_database,
            clear_registries=True,
            before_commit=before_commit,
        )

    def _finish_open_scene(
        self,
        path: str,
        *,
        runtime_load: bool = False,
        record_navigation: bool = True,
        preserve_document: bool = False,
    ) -> None:
        """Publish bookkeeping after a successful Scene transaction.

        Runtime scene transitions update the live path for diagnostics and
        subsequent loads, but must not replace the Editor's persisted scene or
        clear its pre-play undo history.
        """
        self._current_scene_path = resolved_path(path)
        if not runtime_load and not preserve_document:
            self._replace_scene_document(
                kind="scene",
                resource_path=self._current_scene_path,
                title=os.path.splitext(os.path.basename(self._current_scene_path))[0],
                dirty=False,
            )

        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()

        from Infernux.renderstack.render_stack import RenderStack
        RenderStack.refresh_active_instance(scene)

        # Force-init SpriteRenderer wrappers so their materials (texture,
        # color, uvRect) are created before the first render frame.
        try:
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer
            SpriteRenderer.init_all_in_scene(scene)
        except Exception as exc:
            Debug.log_internal(f"SpriteRenderer init: {exc}")

        self._restore_camera_state(self._current_scene_path)
        if not runtime_load:
            self._remember_last_scene(self._current_scene_path)

        # Sync all prefab instances to the latest on-disk prefab data
        self.sync_all_prefab_instances(scene)

        Debug.log_internal(f"Scene loaded: {os.path.basename(path)}")
        if self._on_scene_changed:
            self._on_scene_changed()
        if not runtime_load and record_navigation:
            self._publish_scene_navigation(
                f"Open Scene {os.path.splitext(os.path.basename(path))[0]}"
            )

    def _do_open_scene(
        self,
        path: str,
        *,
        record_navigation: bool = True,
        preserve_document: bool = False,
    ) -> bool:
        """Synchronously run the same transaction used by deferred loading."""
        transaction = self._create_open_scene_transaction(path)
        if transaction is None:
            return False
        transaction.run_to_completion(raise_on_failure=False)
        if not transaction.succeeded:
            Debug.log_error(f"Scene load failed for '{path}': {transaction.error}")
            return False
        self._finish_open_scene(
            path,
            record_navigation=record_navigation,
            preserve_document=preserve_document,
        )
        return True

    def reload_from_resource(self, *, document_id: str, resource_path: str):
        """Reload the current scene from its current durable disk contents.

        Conflict resolution already happened in DocumentRegistry.  This
        controller always creates a fresh path-backed transaction, so reload
        cannot accidentally restore the session snapshot or the previous
        in-memory scene.  The existing document identity is retained and its
        controller binding is refreshed after the transaction publishes.
        """
        if str(document_id or "") != self._scene_document_id:
            return False
        target = resolved_path(resource_path or self._current_scene_path or "")
        if not target or not os.path.isfile(target):
            return False
        if self.is_prefab_mode or self._is_play_mode() or self.is_loading:
            return False
        # ``_do_open_scene`` constructs SceneDocumentTransaction(path=target),
        # which reads the current bytes on disk on every call.  Keep this
        # explicit check close to the public durable-reload contract so a
        # missing/replaced target cannot fall through to a stale active scene.
        if self._current_scene_path and path_key(target) != path_key(self._current_scene_path):
            return False
        reloaded = self._do_open_scene(
            target,
            record_navigation=False,
            preserve_document=True,
        )
        if not reloaded:
            return False
        from Infernux.engine.interaction import DocumentRegistry

        registry = DocumentRegistry.instance()
        document = registry.get(document_id)
        if document is None:
            return False
        registry.update_metadata(
            document.document_id,
            resource_path=target,
            controller=self,
        )
        return True

    def request_external_reload(self, *, document_id: str, resource_path: str):
        """Reload a scene after the user resolves an external-change conflict.

        Play-mode restoration and scene replacement both own deferred work. A
        confirmed disk reload therefore waits for those transactions instead
        of rejecting the user's choice or reading into the runtime scene.
        """
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
        )

        identifier = str(document_id or "")
        target = resolved_path(resource_path or self._current_scene_path or "")
        if not self.owns_document(identifier):
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the scene document is no longer owned by the active scene manager",
            )
        if not target or not os.path.isfile(target):
            return DocumentActionResult(
                DocumentActionStatus.FAILED,
                "the scene file no longer exists on disk",
            )
        pending = self._pending_external_reload
        if pending is not None and pending != (identifier, target):
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "another scene durable reload is already pending",
            )

        if self._is_play_mode() or self.is_loading or self.is_prefab_mode:
            self._pending_external_reload = (identifier, target)
            self._advance_pending_external_reload()
            return DocumentActionResult(DocumentActionStatus.PENDING)

        if not self.reload_from_resource(
            document_id=identifier,
            resource_path=target,
        ):
            return DocumentActionResult(
                DocumentActionStatus.FAILED,
                "the current scene could not be replaced from disk",
            )
        return DocumentActionResult(DocumentActionStatus.APPLIED)

    def _advance_pending_external_reload(self) -> int:
        pending = self._pending_external_reload
        if pending is None:
            return 0

        from Infernux.engine.deferred_task import DeferredTaskRunner

        runner = DeferredTaskRunner.instance()
        if self._is_play_mode():
            if runner.is_busy:
                return 0
            from Infernux.engine.play_mode import PlayModeManager

            play_mode = PlayModeManager.instance()
            if play_mode is None or not play_mode.exit_play_mode():
                return 0
            return 0
        if runner.is_busy or self.is_loading:
            return 0
        if self.is_prefab_mode:
            if not self._deferred_exit_prefab:
                self._request_prefab_exit()
            return 0

        document_id, target = pending
        self._pending_external_reload = None
        message = ""
        try:
            success = bool(
                self.reload_from_resource(
                    document_id=document_id,
                    resource_path=target,
                )
            )
            if not success:
                message = "the current scene could not be replaced from disk"
        except Exception as exc:
            success = False
            message = str(exc)

        from Infernux.engine.interaction import DocumentRegistry

        DocumentRegistry.instance().complete_external_reload(
            document_id,
            success=success,
            message=message,
        )
        return 1

    def poll_pending_writes(self) -> int:
        """Advance controller-owned persistence and durable reload work."""
        return self._advance_pending_external_reload()

    def restore_document_locator(self, locator) -> bool:
        """Restore a Scene required by the global history replay barrier."""
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry

        if locator is None or locator.key_hint.kind is not DocumentKind.SCENE:
            return False
        registry = DocumentRegistry.instance()
        active = registry.get(self._scene_document_id)
        if active is not None and active.stable_id == locator.stable_id:
            return True
        if self.is_loading or self.is_prefab_mode or self._is_play_mode():
            return False

        snapshot = self._scene_restore_snapshots.get(locator.stable_id)
        # A history entry may contain the pre-Save-As session key.  Resolve
        # the current registry address first so restoring the scene reuses the
        # original stable identity instead of creating a duplicate document.
        history_locator = snapshot.locator if snapshot is not None else locator
        canonical_locator = registry.canonical_locator(history_locator)
        path = str(
            (
                canonical_locator.resource_path
                or (snapshot.resource_path if snapshot is not None else "")
            )
            or ""
        ).strip()
        if snapshot is None and (not path or not os.path.isfile(path)):
            return False

        self._archive_active_scene()
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            scene = SceneManager.instance().create_scene(locator.title or DEFAULT_SCENE_NAME)

        def before_commit():
            self._prepare_native_scene_swap()
            from Infernux.renderstack.render_stack import RenderStack

            RenderStack._active_instance = None

        from Infernux.engine.scene_document_transaction import SceneDocumentTransaction

        transaction = SceneDocumentTransaction(
            scene,
            document=snapshot.document if snapshot is not None else None,
            path=None if snapshot is not None else path,
            asset_database=self._asset_database,
            clear_registries=True,
            before_commit=before_commit,
        )
        if not transaction.run_to_completion(raise_on_failure=False):
            Debug.log_error(
                f"Scene history restore failed for '{path or locator.title}': "
                f"{transaction.error}"
            )
            return False

        self._current_scene_path = resolved_path(path) if path else None
        dirty = bool(snapshot and snapshot.revision != snapshot.saved_revision)
        document = self._replace_scene_document(
            kind="scene",
            resource_path=self._current_scene_path or "",
            title=(canonical_locator.title or (snapshot.title if snapshot is not None else ""))
            or DEFAULT_SCENE_NAME,
            dirty=dirty,
            key_override=canonical_locator.key_hint,
            stable_id=locator.stable_id,
            revision=snapshot.revision if snapshot is not None else 0,
            saved_revision=snapshot.saved_revision if snapshot is not None else 0,
        )
        if document.stable_id != locator.stable_id:
            Debug.log_error(
                "Scene history restore resolved a different stable document identity: "
                f"requested={locator.stable_id} actual={document.stable_id} "
                f"requested_key={locator.key_hint!r} canonical_key={canonical_locator.key_hint!r} "
                f"document_key={document.key!r} path={path!r}"
            )
            return False

        from Infernux.renderstack.render_stack import RenderStack

        RenderStack.refresh_active_instance(scene)
        try:
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer

            SpriteRenderer.init_all_in_scene(scene)
        except Exception as exc:
            Debug.log_internal(f"SpriteRenderer init after history restore: {exc}")
        if self._current_scene_path:
            self._restore_camera_state(self._current_scene_path)
        self.sync_all_prefab_instances(scene)
        from Infernux.gizmos.collector import notify_scene_changed

        notify_scene_changed()
        if self._on_scene_changed:
            self._on_scene_changed()
        return True


    def _do_new_scene(self):
        """Create a blank scene with default Camera and Light (no dirty guard)."""
        from Infernux.lib import SceneManager
        sm = SceneManager.instance()

        scene = sm.get_active_scene()
        if not scene:
            scene = sm.create_scene(DEFAULT_SCENE_NAME)

        def before_commit():
            self._prepare_native_scene_swap()
            from Infernux.renderstack.render_stack import RenderStack
            RenderStack._active_instance = None

        from Infernux.engine.scene_document_transaction import SceneDocumentTransaction
        transaction = SceneDocumentTransaction(
            scene,
            document=_empty_scene_document(DEFAULT_SCENE_NAME),
            asset_database=self._asset_database,
            clear_registries=True,
            before_commit=before_commit,
        )
        if not transaction.run_to_completion(raise_on_failure=False):
            Debug.log_error(f"New scene transaction failed: {transaction.error}")
            return False

        try:
            self._populate_default_objects(scene)
        except Exception as exc:
            Debug.log_error(f"Error populating default objects: {exc}")

        self._current_scene_path = None
        self._replace_scene_document(
            kind="scene",
            resource_path="",
            title=DEFAULT_SCENE_NAME,
            dirty=True,
        )

        # Invalidate gizmos icon cache (scene objects are new)
        from Infernux.gizmos.collector import notify_scene_changed
        notify_scene_changed()

        Debug.log_internal("New scene created")
        if self._on_scene_changed:
            self._on_scene_changed()
        self._publish_scene_navigation("New Scene")

        try:
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer
            SpriteRenderer.init_all_in_scene(scene)
        except Exception as exc:
            Debug.log_internal(f"SpriteRenderer init after new scene: {exc}")
        return True

    @staticmethod
    def _populate_default_objects(scene) -> None:
        """Add a default Main Camera and Directional Light to *scene*.

        Called when creating a brand-new scene so the user doesn't start
        with a completely empty viewport.  Mirrors the Unity convention of
        providing a usable camera and a sun-like directional light by default.
        """
        from Infernux.lib import LightType, LightShadows
        from Infernux.math import Vector3

        # ---- Main Camera ----
        cam_obj = scene.create_game_object("Main Camera")
        cam_obj.tag = "MainCamera"
        cam_obj.add_component("Camera")
        cam_obj.transform.position = Vector3(0.0, 1.0, -10.0)

        # ---- Directional Light ----
        light_obj = scene.create_game_object("Directional Light")
        light_obj.transform.euler_angles = Vector3(50.0, -30.0, 0.0)
        light = light_obj.add_component("Light")
        if light is not None:
            light.light_type = LightType.Directional
            light.color = Vector3(1.0, 0.95, 0.9)
            light.intensity = 1.0
            light.shadows = LightShadows.Soft
  

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reset_undo_history(self):
        """Reset undo/redo history after a non-historical scene replacement."""
        from Infernux.engine.undo import UndoManager
        mgr = UndoManager.instance()
        if not mgr:
            return
        mgr.clear()


    def _save_camera_state(self, scene_path: str):
        """Save current editor camera state for the given scene path."""
        if not self._engine or not scene_path:
            return
        cam = self._engine.editor_camera
        if not cam:
            return
        pos = cam.position
        rot = cam.rotation
        fp = cam.focus_point
        fd = cam.focus_distance
        state = {
            "position": [pos.x, pos.y, pos.z],
            "focusPoint": [fp.x, fp.y, fp.z],
            "focusDistance": fd,
            "yaw": rot[0],
            "pitch": rot[1],
        }
        settings = _load_editor_settings()
        if "sceneCameraStates" not in settings:
            settings["sceneCameraStates"] = {}
        key = path_key(scene_path)
        settings["sceneCameraStates"][key] = state
        _save_editor_settings(settings)

    def _restore_camera_state(self, scene_path: str):
        """Restore editor camera state for the given scene path."""
        if not self._engine or not scene_path:
            return
        cam = self._engine.editor_camera
        if not cam:
            return
        settings = _load_editor_settings()
        states = settings.get("sceneCameraStates", {})
        key = path_key(scene_path)
        state = states.get(key)
        if not state:
            return
        p = state["position"]
        f = state["focusPoint"]
        cam.restore_state(
            p[0], p[1], p[2],
            f[0], f[1], f[2],
            state["focusDistance"],
            state["yaw"],
            state["pitch"],
        )

