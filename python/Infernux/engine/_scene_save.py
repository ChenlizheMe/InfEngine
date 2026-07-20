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

import os
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.project_context import get_project_root
from Infernux.engine.path_utils import is_path_within, path_key, resolved_path, safe_path as _safe_path
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

    def save_current_scene(self) -> bool:
        """Save the current scene.  If no file is associated, show a Save-As dialog.

        Returns True if the save happened synchronously, False if a dialog was
        opened (the actual save happens asynchronously via the dialog callback).
        """
        if self._is_play_mode():
            Debug.log_warning("Cannot save scene while in Play mode.")
            return False

        # In Prefab Mode, Ctrl+S is ignored — prefab auto-saves on exit.
        if self.is_prefab_mode:
            return False

        if self._current_scene_path:
            return self._do_save(self._current_scene_path)

        # No file yet — show a Save As dialog
        self._show_save_as_dialog()
        return False

    def _save_prefab(self) -> bool:
        """Save the currently-edited prefab in Prefab Mode."""
        if not self.prefab_mode_path:
            Debug.log_warning("No prefab path in Prefab Mode.")
            return False

        from Infernux.lib import SceneManager
        from Infernux.engine.prefab_manager import save_prefab

        scene = SceneManager.instance().get_active_scene()
        roots = _get_scene_root_objects(scene)
        if not roots:
            Debug.log_warning("No root objects in Prefab Mode scene.")
            return False

        source_canvas_name = ""
        if isinstance(self.prefab_envelope, dict):
            source_canvas_name = self.prefab_envelope.get("source_canvas_name", "")
        if not save_prefab(
            roots[0],
            self.prefab_mode_path,
            asset_database=self._asset_database,
            source_canvas_name=source_canvas_name,
        ):
            return False

        self._dirty = False
        Debug.log_internal(f"Prefab saved: {self.prefab_mode_path}")
        return True

    def save_scene_as(self):
        """Force a Save-As dialog regardless of whether a path exists."""
        if self._is_play_mode():
            Debug.log_warning("Cannot save scene while in Play mode.")
            return
        self._show_save_as_dialog()

    def _do_save(self, path: str) -> bool:
        """Actually write the scene to *path*."""
        from Infernux.engine.ui.engine_status import EngineStatus
        ok = self._do_save_inner(path)
        if ok:
            EngineStatus.flash("保存完成 Saved", 1.0, duration=1.5)
        else:
            EngineStatus.flash("保存失败 Save Failed", 0.0, duration=2.0)
        return ok

    def _do_save_inner(self, path: str) -> bool:
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

        # Step 2: durably replace the scene file. The old daemon+immediate-join
        # path was synchronous in practice and could outlive a timeout.
        abs_path = resolved_path(path)
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            from Infernux.core.document_store import DocumentStore
            DocumentStore.instance().write_and_wait(abs_path, json_str)
        except (OSError, RuntimeError) as exc:
            scene.name = previous_scene_name
            Debug.log_error(f"Failed to write scene file: {exc}")
            return False

        self._current_scene_path = abs_path
        self._dirty = False

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

        # Notify undo system of clean state
        from Infernux.engine.undo import UndoManager
        mgr = UndoManager.instance()
        if mgr:
            mgr.mark_save_point()

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

    def _show_save_as_dialog(self):
        """Open the appropriate Save As workflow for a user or automation agent."""
        root = _effective_project_root()
        if not root:
            Debug.log_warning("No project root set — cannot save scene.")
            return

        if self._current_scene_path:
            default_name = os.path.splitext(os.path.basename(self._current_scene_path))[0]
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

    def render_save_as_popup(self, ctx) -> None:
        """Render the scene Save As workflow inside the Editor process."""
        if self._save_as_native_dialog_pending:
            self._save_as_native_dialog_pending = False
            self._save_with_native_dialog()
            return

        if not self._save_as_popup_open:
            return

        popup_id = "Save Scene As###scene_save_as"
        if self._save_as_popup_requested:
            ctx.open_popup(popup_id)
            self._save_as_popup_requested = False

        # ImGuiWindowFlags_AlwaysAutoResize = 1 << 6 = 64.
        if not ctx.begin_popup_modal(popup_id, 64):
            return

        ctx.record_semantic_window("modal", "Save Scene As", "scene.save_as")
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
            self._run_post_save_callback()

        def _cancel() -> None:
            self._close_save_as_popup(ctx)
            self._cancel_save_as()

        ctx.button("Save##scene_save_as_confirm", _save)
        ctx.record_semantic_item("button", "Save", True, "scene.save_as.confirm")
        ctx.same_line()
        ctx.button("Cancel##scene_save_as_cancel", _cancel)
        ctx.record_semantic_item("button", "Cancel", True, "scene.save_as.cancel")
        ctx.end_popup()

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
            return
        if not self._save_as_path(path):
            Debug.log_warning(self._save_as_error or "The scene could not be saved. Check the Console for details.")
            return
        self._run_post_save_callback()

    def _save_as_path(self, path: str) -> bool:
        if os.path.exists(path) and path_key(path) != path_key(self._current_scene_path or ""):
            self._save_as_error = "A scene already exists at this location. Choose another name to avoid overwriting it."
            return False
        if not self._do_save(path):
            self._save_as_error = "The scene could not be saved. Check the Console for details."
            return False
        return True

    def _run_post_save_callback(self) -> None:
        if self._post_save_callback:
            callback = self._post_save_callback
            self._post_save_callback = None
            callback()

    def _cancel_save_as(self) -> None:
        self._save_as_popup_open = False
        self._save_as_popup_requested = False
        self._save_as_focus_name = False
        self._save_as_agent_modal = False
        self._save_as_native_dialog_pending = False
        self._save_as_error = ""
        if self._post_save_callback is not None:
            if self._pending_action == "close" and self._engine:
                self._engine.cancel_close()
            self._close_in_progress = False
            self._clear_pending_action()
            self._post_save_callback = None

    def _close_save_as_popup(self, ctx) -> None:
        self._save_as_popup_open = False
        self._save_as_popup_requested = False
        self._save_as_focus_name = False
        self._save_as_agent_modal = False
        self._save_as_native_dialog_pending = False
        self._save_as_error = ""
        ctx.close_current_popup()

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

