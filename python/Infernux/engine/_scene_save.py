"""
SceneSaveMixin for strict, durable scene persistence.

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
from __future__ import annotations

import json
import os
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.project_context import get_project_root
from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    resolved_path,
    safe_path as _safe_path,
    same_path,
)
from .scene_manager import (
    SCENE_EXTENSION,
    DEFAULT_SCENE_FILE_BASE,
    _effective_project_root,
    _load_editor_settings,
    _save_editor_settings,
    _get_scene_root_objects,
)


def is_synthetic_input_frame() -> bool:
    """Resolve the editor input probe lazily so headless imports stay UI-free."""
    from Infernux.engine.ui._dialogs import is_synthetic_input_frame as _probe

    return _probe()


def save_file_dialog(**kwargs):
    """Resolve the native editor dialog lazily so headless imports stay UI-free."""
    from Infernux.engine.ui._dialogs import save_file_dialog as _save_file_dialog

    return _save_file_dialog(**kwargs)


class SceneSaveMixin:
    """SceneSaveMixin method group for SceneFileManager."""

    _SAVE_AS_MODAL_ID = "scene.save_as"
    _SAVE_AS_ALLOWED_PARENT_IDS = ("editor.unsaved_changes",)

    def _ensure_save_as_modal_presenter(self):
        """Lazily bind Scene Save As to the current Editor session."""
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            return None
        service = core.modals
        if self._save_as_modal_service is service:
            return service
        if self._save_as_modal_service is not None:
            self._save_as_modal_service.unregister(
                self._SAVE_AS_MODAL_ID,
                cancel=True,
            )
        self._save_as_modal_service = service
        service.register(
            self._SAVE_AS_MODAL_ID,
            is_active=lambda: bool(
                self._save_as_popup_open
                or self._save_as_native_dialog_pending
            ),
            render=self.render_save_as_popup,
            cancel=self._cancel_save_as,
            allowed_parent_ids=self._SAVE_AS_ALLOWED_PARENT_IDS,
        )
        return service

    def save_current_scene(self) -> bool:
        """Save the current scene.  If no file is associated, show a Save-As dialog.

        Returns True if the save happened synchronously, False if a dialog was
        opened (the actual save happens asynchronously via the dialog callback).
        """
        if self._is_play_mode():
            Debug.log_warning("Cannot save scene while in Play mode.")
            return False

        from Infernux.engine.interaction import (
            DocumentActionStatus,
            DocumentRegistry,
        )

        result = DocumentRegistry.instance().request_save(self.document_id)
        return result.status in {
            DocumentActionStatus.APPLIED,
            DocumentActionStatus.NO_OP,
        }

    def save(self, *, ticket, save_as: bool = False):
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
            DocumentKind,
            DocumentRegistry,
        )

        if self._is_play_mode():
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "cannot save a scene while in Play mode",
            )
        registry = DocumentRegistry.instance()
        document = registry.get(ticket.document_id)
        if document is None:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the document is no longer open",
            )
        if document.kind is DocumentKind.PREFAB:
            if save_as:
                return DocumentActionResult(
                    DocumentActionStatus.REJECTED,
                    "prefab Save As is not supported in Prefab Mode",
                )
            return self._save_prefab(ticket_id=ticket.ticket_id)
        if document.document_id != self.document_id:
            if document.document_id != self._previous_scene_document_id:
                return DocumentActionResult(
                    DocumentActionStatus.REJECTED,
                    "the scene document is not active or suspended",
                )
            if document.resource_path and not save_as:
                return self._save_suspended_scene(
                    document.resource_path,
                    ticket_id=ticket.ticket_id,
                )
            self._pending_save_ticket_id = ticket.ticket_id
            self._pending_save_document_id = document.document_id
            if self._show_save_as_dialog(document_id=document.document_id):
                return DocumentActionResult(DocumentActionStatus.PENDING)
            self._pending_save_ticket_id = ""
            self._pending_save_document_id = ""
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "no project root is available",
            )
        if save_as or not self._current_scene_path:
            self._pending_save_ticket_id = ticket.ticket_id
            self._pending_save_document_id = document.document_id
            if self._show_save_as_dialog(document_id=document.document_id):
                return DocumentActionResult(DocumentActionStatus.PENDING)
            self._pending_save_ticket_id = ""
            self._pending_save_document_id = ""
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "no project root is available",
            )
        return self._do_save(
            self._current_scene_path,
            ticket_id=ticket.ticket_id,
        )

    def save_to_resource(self, *, ticket, resource_path: str):
        """Persist an automation-selected path under the normal save contract."""
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
            DocumentKind,
            DocumentRegistry,
        )

        if self._is_play_mode():
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "cannot save a scene while in Play mode",
            )
        document = DocumentRegistry.instance().get(ticket.document_id)
        if document is None or document.document_id != self.document_id:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "the scene document is not active",
            )
        if document.kind is not DocumentKind.SCENE:
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "explicit resource save only supports Scene documents",
            )
        return self._do_save(resource_path, ticket_id=ticket.ticket_id)

    def discard(self, *, document_id: str) -> bool:
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry

        registry = DocumentRegistry.instance()
        document = registry.get(document_id)
        if document is None:
            return False
        if document.document_id != self.document_id:
            # Suspended scenes are resolved by replace/exit transactions; a
            # view-level discard must never mutate whichever scene is active.
            return False
        if document.kind is DocumentKind.PREFAB:
            # Prefab replacement uses CloseCoordinator's explicit discard
            # authorization and then exits to the suspended Scene. Reloading
            # a live Prefab in-place requires its own command transaction.
            return False
        if self._current_scene_path:
            return bool(
                self._do_open_scene(
                    self._current_scene_path,
                    record_navigation=False,
                )
            )
        return True

    def _save_prefab(self, *, ticket_id: str = "") -> bool:
        """Save the currently-edited prefab in Prefab Mode."""
        if not self.prefab_mode_path:
            Debug.log_warning("No prefab path in Prefab Mode.")
            return False

        from Infernux.lib import SceneManager
        from Infernux.engine.prefab_manager import (
            save_prefab_document,
            serialize_prefab_document,
        )
        from Infernux.engine.interaction import (
            DocumentRegistry,
            document_content_token,
        )

        scene = SceneManager.instance().get_active_scene()
        roots = _get_scene_root_objects(scene)
        if not roots:
            Debug.log_warning("No root objects in Prefab Mode scene.")
            return False

        source_canvas_name = ""
        if isinstance(self.prefab_envelope, dict):
            source_canvas_name = self.prefab_envelope.get("source_canvas_name", "")
        registry = DocumentRegistry.instance()
        document = registry.get(self.document_id)
        active_ticket_id = str(ticket_id or "")
        if document is None:
            Debug.log_error("Prefab save requires a bound editor document.")
            return False
        if not active_ticket_id:
            active_ticket_id = registry.begin_save(document.document_id).ticket_id
        try:
            prefab_document = serialize_prefab_document(
                roots[0],
                source_canvas_name=source_canvas_name,
            )
            serialized_token = document_content_token(prefab_document)
            registry.capture_save_revision(
                active_ticket_id,
                content_token=serialized_token,
            )
        except Exception as exc:
            registry.complete_save(
                active_ticket_id,
                success=False,
                message=f"failed to serialize prefab: {exc}",
            )
            return False
        if not save_prefab_document(
            prefab_document,
            self.prefab_mode_path,
            asset_database=self._asset_database,
        ):
            registry.complete_save(
                active_ticket_id,
                success=False,
                message=f"failed to save prefab: {self.prefab_mode_path}",
            )
            return False

        current_token = None
        try:
            current_token = document_content_token(
                serialize_prefab_document(
                    roots[0],
                    source_canvas_name=source_canvas_name,
                )
            )
        except Exception as exc:
            Debug.log_suppressed("prefab_save.current_content_token", exc)
        registry.complete_save(
            active_ticket_id,
            success=True,
            key=self._document_key("prefab", self.prefab_mode_path),
            resource_path=self.prefab_mode_path,
            title=os.path.splitext(os.path.basename(self.prefab_mode_path))[0],
            content_token=current_token,
        )
        Debug.log_internal(f"Prefab saved: {self.prefab_mode_path}")
        return True

    def save_scene_as(self):
        """Force a Save-As dialog regardless of whether a path exists."""
        if self._is_play_mode():
            Debug.log_warning("Cannot save scene while in Play mode.")
            return False
        if self.is_prefab_mode:
            return False
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().request_save(
            self.document_id,
            save_as=True,
        ).accepted

    def _do_save(self, path: str, *, ticket_id: str = "") -> bool:
        """Actually write the scene to *path*."""
        from Infernux.engine.ui.engine_status import EngineStatus
        target_path = str(path or "")
        if not target_path.lower().endswith(SCENE_EXTENSION):
            target_path += SCENE_EXTENSION
        active_ticket_id = ticket_id or self._pending_save_ticket_id
        from Infernux.engine.interaction import (
            DocumentRegistry,
            document_content_token,
        )

        registry = DocumentRegistry.instance()
        document = registry.get(self.document_id)
        if document is None:
            Debug.log_error("Scene save requires a bound editor document.")
            return False
        if not active_ticket_id:
            current = resolved_path(self._current_scene_path) if self._current_scene_path else ""
            target = resolved_path(target_path)
            active_ticket_id = registry.begin_save(
                document.document_id,
                save_as=not current or not same_path(target, current),
            ).ticket_id
        ok = self._do_save_inner(target_path, ticket_id=active_ticket_id)
        if ok:
            normalized = resolved_path(target_path)
            current_token = None
            try:
                from Infernux.lib import SceneManager

                scene = SceneManager.instance().get_active_scene()
                if scene is not None:
                    current_token = document_content_token(json.loads(scene.serialize()))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                Debug.log_suppressed("scene_save.current_content_token", exc)
            registry.complete_save(
                active_ticket_id,
                success=True,
                key=self._document_key("scene", normalized),
                resource_path=normalized,
                title=os.path.splitext(os.path.basename(normalized))[0],
                content_token=current_token,
                committed_file_state=getattr(
                    self,
                    "_last_scene_commit_file_state",
                    None,
                ),
            )
            self._pending_save_ticket_id = ""
            self._pending_save_document_id = ""
            EngineStatus.flash("保存完成 Saved", 1.0, duration=1.5)
        else:
            registry.complete_save(
                active_ticket_id,
                success=False,
                conflict=bool(getattr(self, "_last_scene_save_conflict", False)),
                message=f"failed to save scene: {target_path}",
            )
            self._pending_save_ticket_id = ""
            self._pending_save_document_id = ""
            EngineStatus.flash("保存失败 Save Failed", 0.0, duration=2.0)
        return ok

    def _do_save_inner(self, path: str, *, ticket_id: str = "") -> bool:
        """Internal save implementation.

        Serializes the scene on the main thread, then durably replaces the file
        synchronously. The future DocumentStore will own background writes,
        generation ordering, coalescing, and shutdown drain as one contract.
        """
        if not self._is_under_assets(path):
            Debug.log_warning("Cannot save scene outside of Assets/ directory.")
            return False

        # Ensure .scene extension
        if not path.lower().endswith(SCENE_EXTENSION):
            path += SCENE_EXTENSION

        from Infernux.lib import SceneManager
        sm = SceneManager.instance()
        scene = sm.get_active_scene()
        if not scene:
            Debug.log_warning("No active scene to save.")
            return False

        # The scene document owns its display name.  Update it before
        # serialization so Save As survives an Editor restart, but restore it
        # if the persistence operation fails.
        previous_scene_name = scene.name
        target_scene_name = os.path.splitext(os.path.basename(path))[0]
        scene.name = target_scene_name

        # Step 1 (main thread): serialize scene graph → JSON string
        try:
            json_str = scene.serialize()
        except Exception as exc:
            scene.name = previous_scene_name
            Debug.log_error(f"Failed to serialize scene: {exc}")
            return False

        if not json_str:
            scene.name = previous_scene_name
            Debug.log_error("Scene serialization returned empty data.")
            return False

        from Infernux.engine.interaction import (
            DocumentRegistry,
            document_content_token,
        )

        if ticket_id:
            try:

                DocumentRegistry.instance().capture_save_revision(
                    ticket_id,
                    content_token=document_content_token(json.loads(json_str)),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                scene.name = previous_scene_name
                Debug.log_error(f"Scene serialization produced an invalid document: {exc}")
                return False

        self._last_scene_commit_file_state = None
        self._last_scene_save_conflict = False

        # Step 2: durably replace the scene file through the shared CAS write
        # ledger.  The exact committed fingerprint is registered before this
        # method returns, so a queued watcher event can acknowledge it.
        abs_path = resolved_path(path)
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            from Infernux.core.assets import AssetManager
            from Infernux.core.document_store import (
                capture_document_file_state,
                write_document_text,
            )

            registry = DocumentRegistry.instance()
            if ticket_id:
                registry.capture_save_target(ticket_id, abs_path)
                ticket = registry.get_save_ticket(ticket_id)
                expected_file_state = ticket.expected_file_state if ticket else None
                commit_token = ticket.commit_token if ticket else ""
            else:
                expected_file_state = None
                commit_token = "scene-save"
            content_token = document_content_token(json.loads(json_str))
            write_document_text(
                abs_path,
                json_str,
                expected_file_state=expected_file_state,
                commit_chain_token=commit_token,
            )
            committed_file_state = capture_document_file_state(abs_path)
            self._last_scene_commit_file_state = AssetManager.register_local_commit(
                abs_path,
                commit_token=commit_token or "scene-save",
                content_token=content_token,
                file_state=committed_file_state,
                edit_revision=(
                    registry.get_save_ticket(ticket_id).captured_revision
                    if ticket_id and registry.get_save_ticket(ticket_id) is not None
                    else 0
                ),
                document_id=self.document_id,
            )
        except (OSError, RuntimeError) as exc:
            scene.name = previous_scene_name
            self._last_scene_save_conflict = "changed outside the editor" in str(exc).casefold()
            Debug.log_error(f"Failed to write scene file: {exc}")
            return False

        self._current_scene_path = abs_path

        # Save As publishes a brand-new asset through DocumentStore. Register
        # it synchronously so the Project panel can expose it on the next frame;
        # the file watcher remains a fallback for transient database contention.
        if self._asset_database is not None and not self._asset_database.contains_path(abs_path):
            try:
                from Infernux.core.assets import AssetManager

                result = AssetManager.import_asset(abs_path, database=self._asset_database)
                if not result:
                    detail = getattr(result, "error", "") or "asset import was rejected"
                    Debug.log_warning(f"Scene saved but asset registration is pending: {detail}")
            except Exception as exc:
                Debug.log_warning(f"Scene saved but asset registration is pending: {exc}")

        # Persist editor camera state for this scene
        self._save_camera_state(self._current_scene_path)

        self._remember_last_scene(self._current_scene_path)
        Debug.log_internal(f"Scene saved: {path}")
        return True

    def _default_scene_save_path(self) -> Optional[str]:
        """Return a unique default scene path under Assets/ for untitled saves."""
        root = _effective_project_root()
        if not root:
            return None

        assets_dir = os.path.join(root, "Assets")
        os.makedirs(assets_dir, exist_ok=True)

        base_name = DEFAULT_SCENE_FILE_BASE
        candidate = os.path.join(assets_dir, f"{base_name}{SCENE_EXTENSION}")
        if not os.path.exists(candidate):
            return candidate

        index = 1
        while True:
            candidate = os.path.join(assets_dir, f"{base_name} {index}{SCENE_EXTENSION}")
            if not os.path.exists(candidate):
                return candidate
            index += 1

    def _show_save_as_dialog(self, *, document_id: str = "") -> bool:
        """Open the appropriate Save As workflow for a user or automation agent."""
        if self._save_as_popup_open or self._save_as_native_dialog_pending:
            return False
        root = _effective_project_root()
        if not root:
            Debug.log_warning("No project root set — cannot save scene.")
            return False

        modal_service = self._ensure_save_as_modal_presenter()
        if modal_service is None:
            Debug.log_warning("Scene Save As requires an active Editor interaction session.")
            return False
        if not modal_service.activate(
            self._SAVE_AS_MODAL_ID,
            owner_id="scene",
        ):
            return False

        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().get(document_id or self.document_id)
        document_path = document.resource_path if document is not None else ""
        if document_path:
            default_name = os.path.splitext(os.path.basename(document_path))[0]
        else:
            default_name = DEFAULT_SCENE_FILE_BASE

        self._save_as_folder = "Assets"
        self._save_as_name = default_name
        self._save_as_error = ""
        self._save_as_agent_modal = is_synthetic_input_frame()
        self._save_as_focus_name = self._save_as_agent_modal
        self._save_as_popup_requested = self._save_as_agent_modal
        self._save_as_popup_open = self._save_as_agent_modal
        self._save_as_native_dialog_pending = not self._save_as_agent_modal
        return True

    def render_save_as_popup(self, ctx) -> None:
        """Render Scene Save As through the global ModalPortal."""
        if self._save_as_native_dialog_pending:
            self._save_as_native_dialog_pending = False
            self._save_with_native_dialog()
            return

        if not self._save_as_popup_open:
            return

        from Infernux.engine.ui.editor_modal import (
            EditorModalAction,
            begin_editor_modal,
            end_editor_modal,
            render_editor_modal_actions,
        )

        request_open = self._save_as_popup_requested
        self._save_as_popup_requested = False
        if not begin_editor_modal(
            ctx,
            popup_id="Save Scene As###scene_save_as",
            title="Save Scene As",
            semantic_id="scene.save_as",
            request_open=request_open,
            height=280.0,
        ):
            return
        ctx.label("保存场景到项目 Assets 目录")
        ctx.label("Save the scene under this project's Assets directory.")
        ctx.spacing()

        self._save_as_folder = ctx.text_input(
            "Folder##scene_save_as_folder", self._save_as_folder, 512
        )
        ctx.record_semantic_item("text_input", "Folder", True, "scene.save_as.folder")
        if self._save_as_focus_name:
            ctx.set_keyboard_focus_here()
            self._save_as_focus_name = False
        self._save_as_name = ctx.text_input(
            "Name##scene_save_as_name", self._save_as_name, 256
        )
        ctx.record_semantic_item("text_input", "Name", True, "scene.save_as.name")

        if self._save_as_error:
            ctx.spacing()
            ctx.text_wrapped(self._save_as_error)

        ctx.spacing()
        ctx.separator()
        ctx.spacing()

        def _save() -> None:
            path, error = self._resolve_save_as_path()
            if error:
                self._save_as_error = error
                return
            if not self._save_as_path(path):
                return
            self._close_save_as_popup(ctx)

        def _cancel() -> None:
            ctx.close_current_popup()
            self._cancel_save_as()

        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction("Save", "confirm", _save),
                EditorModalAction("Cancel", "cancel", _cancel),
            ],
            semantic_prefix="scene.save_as",
        )
        end_editor_modal(ctx)

    def _resolve_save_as_path(self) -> tuple[str, str]:
        root = _effective_project_root()
        if not root:
            return "", "No project root is available."

        folder = str(self._save_as_folder or "").strip().replace("\\", "/")
        if not folder:
            folder = "Assets"
        if os.path.isabs(folder):
            return "", "Folder must be a project-relative path under Assets."

        target_folder = resolved_path(os.path.join(root, folder))
        if not self._is_under_assets(target_folder):
            return "", "Scenes must be saved under the project's Assets directory."

        name = str(self._save_as_name or "").strip()
        if name.lower().endswith(SCENE_EXTENSION):
            name = name[: -len(SCENE_EXTENSION)]
        if not name:
            return "", "Enter a scene name."
        if name != os.path.basename(name) or any(ch in name for ch in '<>:"/\\|?*'):
            return "", "Scene name contains an invalid path or filename character."

        return os.path.join(target_folder, name + SCENE_EXTENSION), ""

    def _resolve_native_save_as_path(self, path: str) -> tuple[str, str]:
        """Validate a platform dialog result using the same Assets boundary."""
        target = resolved_path(str(path or ""))
        if not target.lower().endswith(SCENE_EXTENSION):
            target += SCENE_EXTENSION
        if not self._is_under_assets(target):
            return "", "Scenes must be saved under the project's Assets directory."
        return target, ""

    def _save_with_native_dialog(self) -> None:
        root = _effective_project_root()
        if not root:
            return

        path = save_file_dialog(
            title="Save Scene As",
            win32_filter="Scene (*.scene)\0*.scene\0\0",
            initial_dir=os.path.join(root, "Assets"),
            default_filename=f"{self._save_as_name}{SCENE_EXTENSION}",
            default_ext=SCENE_EXTENSION.lstrip("."),
            tk_filetypes=[("Scene (*.scene)", "*.scene")],
        )
        if not path:
            self._cancel_save_as()
            return

        path, error = self._resolve_native_save_as_path(path)
        if error:
            Debug.log_warning(error)
            self._cancel_save_as(message=error)
            return
        if not self._save_as_path(path):
            message = self._save_as_error or "The scene could not be saved. Check the Console for details."
            Debug.log_warning(message)
            self._cancel_save_as(message=message)
            return
        self._finish_save_as()

    def _save_as_path(self, path: str) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        target_document = DocumentRegistry.instance().get(
            self._pending_save_document_id or self.document_id
        )
        current_path = target_document.resource_path if target_document is not None else ""
        if os.path.exists(path) and path_key(path) != path_key(current_path):
            self._save_as_error = "A scene already exists at this location. Choose another name to avoid overwriting it."
            return False
        if (
            target_document is not None
            and target_document.document_id == self._previous_scene_document_id
        ):
            saved = self._save_suspended_scene(
                path,
                ticket_id=self._pending_save_ticket_id,
            )
        else:
            saved = self._do_save(path)
        if not saved:
            self._save_as_error = "The scene could not be saved. Check the Console for details."
            return False
        return True

    def _save_suspended_scene(self, path: str, *, ticket_id: str) -> bool:
        if not isinstance(self._previous_scene_document, dict):
            from Infernux.engine.interaction import DocumentRegistry

            DocumentRegistry.instance().complete_save(
                ticket_id,
                success=False,
                message="the suspended scene snapshot is unavailable",
            )
            return False
        if not self._is_under_assets(path):
            return False
        target = resolved_path(path)
        if not target.lower().endswith(SCENE_EXTENSION):
            target += SCENE_EXTENSION
        from Infernux.engine.interaction import (
            DocumentRegistry,
            document_content_token,
        )

        registry = DocumentRegistry.instance()
        try:
            payload = dict(self._previous_scene_document)
            payload["name"] = os.path.splitext(os.path.basename(target))[0]
            serialized_token = document_content_token(payload)
            registry.capture_save_revision(
                ticket_id,
                content_token=serialized_token,
            )
            from Infernux.core.document_store import DocumentStore

            DocumentStore.instance().write_and_wait(
                target,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as exc:
            registry.complete_save(
                ticket_id,
                success=False,
                message=str(exc),
            )
            return False
        if self._asset_database is not None and not self._asset_database.contains_path(target):
            try:
                from Infernux.core.assets import AssetManager

                AssetManager.import_asset(target, database=self._asset_database)
            except Exception as exc:
                Debug.log_warning(f"Scene saved but asset registration is pending: {exc}")
        registry.complete_save(
            ticket_id,
            success=True,
            key=self._document_key("scene", target),
            resource_path=target,
            title=os.path.splitext(os.path.basename(target))[0],
            content_token=serialized_token,
        )
        self._previous_scene_path = target
        self._pending_save_ticket_id = ""
        self._pending_save_document_id = ""
        return True

    def _cancel_save_as(self, *, message: str = "save was cancelled") -> None:
        ticket_id = self._pending_save_ticket_id
        self._pending_save_ticket_id = ""
        self._pending_save_document_id = ""
        self._reset_save_as_state()
        if ticket_id:
            from Infernux.engine.interaction import DocumentRegistry

            DocumentRegistry.instance().complete_save(
                ticket_id,
                success=False,
                cancelled=True,
                message=message,
            )

    def _close_save_as_popup(self, ctx) -> None:
        ctx.close_current_popup()
        self._finish_save_as()

    def _finish_save_as(self) -> None:
        self._reset_save_as_state()

    def _reset_save_as_state(self) -> None:
        self._save_as_popup_open = False
        self._save_as_popup_requested = False
        self._save_as_focus_name = False
        self._save_as_agent_modal = False
        self._save_as_native_dialog_pending = False
        self._save_as_error = ""
        if self._save_as_modal_service is not None:
            self._save_as_modal_service.deactivate(self._SAVE_AS_MODAL_ID)

    def _is_under_assets(self, path: str) -> bool:
        """Check if *path* is within the project's Assets/ directory."""
        root = _effective_project_root()
        if not root:
            return False
        return is_path_within(path, os.path.join(root, "Assets"))

    def _remember_last_scene(self, path: str):
        settings = _load_editor_settings()
        settings["lastOpenedScene"] = path
        _save_editor_settings(settings)

