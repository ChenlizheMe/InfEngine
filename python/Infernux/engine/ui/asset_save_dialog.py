"""Editor-owned Save As modal shared by asset authoring panels."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.path_utils import is_path_within, path_key, resolved_path
from Infernux.engine.project_context import get_project_root
from Infernux.engine.interaction import ModalService
from Infernux.engine.ui._dialogs import is_synthetic_input_frame, save_file_dialog
from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


class AssetSaveAsDialog:
    """Persist an asset below ``Assets/`` through visible Editor controls."""

    _ALLOWED_PARENT_IDS = (
        "editor.unsaved_changes",
        "editor.external_document_conflict",
    )

    def __init__(
        self,
        semantic_prefix: str,
        asset_label: str,
        *,
        owner_id: str = "",
        modal_service: Optional[ModalService] = None,
    ) -> None:
        self._semantic_prefix = semantic_prefix
        self._asset_label = asset_label
        self._owner_id = str(owner_id or semantic_prefix.split(".", 1)[0]).strip()
        self._modal_id = f"asset.save_as:{semantic_prefix}"
        self._modal_service: Optional[ModalService] = None
        self._title = "Save Asset"
        self._extension = ""
        self._project_root = ""
        self._current_path = ""
        self._folder = "Assets"
        self._name = ""
        self._error = ""
        self._open = False
        self._requested = False
        self._focus_name = False
        self._agent_modal = False
        self._native_dialog_pending = False
        self._save_callback: Optional[Callable[[str], bool]] = None
        self._cancel_callback: Optional[Callable[[], None]] = None
        if modal_service is not None:
            self.bind_modal_service(modal_service)

    def bind_modal_service(self, modal_service: ModalService) -> None:
        """Bind this presenter to the project-session modal authority."""
        if not isinstance(modal_service, ModalService):
            raise TypeError("modal_service must be a ModalService")
        if self._modal_service is modal_service:
            return
        if self._modal_service is not None:
            self._modal_service.unregister(self._modal_id, cancel=True)
        self._modal_service = modal_service
        modal_service.register(
            self._modal_id,
            is_active=lambda: self.is_open,
            render=self.render,
            cancel=self.cancel,
            allowed_parent_ids=self._ALLOWED_PARENT_IDS,
        )

    def _require_modal_service(self) -> ModalService:
        if self._modal_service is None:
            from Infernux.engine.interaction import EditorInteractionCore

            core = EditorInteractionCore.instance()
            if core is None:
                raise RuntimeError(
                    "AssetSaveAsDialog requires EditorInteractionCore or an explicit ModalService"
                )
            self.bind_modal_service(core.modals)
        return self._modal_service

    @property
    def is_open(self) -> bool:
        return self._open or self._native_dialog_pending

    @property
    def folder(self) -> str:
        return self._folder

    @folder.setter
    def folder(self, value: str) -> None:
        self._folder = str(value or "")

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = str(value or "")

    def request(
        self,
        *,
        title: str,
        extension: str,
        default_name: str,
        current_path: str = "",
        project_root: Optional[str] = None,
        save_callback: Callable[[str], bool],
        cancel_callback: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self.is_open or not callable(save_callback):
            return False
        raw_root = project_root or get_project_root()
        if not raw_root:
            return False
        root = resolved_path(raw_root)

        normalized_extension = str(extension or "").strip().lstrip(".")
        if not normalized_extension:
            raise ValueError("extension must be non-empty")

        modal_service = self._require_modal_service()
        if not modal_service.activate(self._modal_id, owner_id=self._owner_id):
            return False

        self._title = str(title or "Save Asset")
        self._extension = normalized_extension
        self._project_root = root
        self._current_path = path_key(current_path) if current_path else ""
        self._folder = "Assets"
        self._name = self._strip_extension(default_name)
        self._error = ""
        self._agent_modal = is_synthetic_input_frame()
        self._open = self._agent_modal
        self._requested = self._agent_modal
        self._focus_name = self._agent_modal
        self._native_dialog_pending = not self._agent_modal
        self._save_callback = save_callback
        self._cancel_callback = cancel_callback
        return True

    def resolve_path(self) -> tuple[str, str]:
        """Return the requested absolute asset path, or a validation error."""
        if not self._project_root:
            return "", "No project root is available."

        folder = self._folder.strip().replace("\\", "/") or "Assets"
        if os.path.isabs(folder):
            return "", "Folder must be a project-relative path under Assets."

        target_folder = resolved_path(os.path.join(self._project_root, folder))
        assets_root = resolved_path(os.path.join(self._project_root, "Assets"))
        if not is_path_within(target_folder, assets_root):
            return "", "Assets must be saved under the project's Assets directory."

        name = self._strip_extension(self._name)
        if not name:
            return "", f"Enter a {self._asset_label} name."
        if name != os.path.basename(name) or any(ch in name for ch in '<>:"/\\|?*'):
            return "", f"{self._asset_label.capitalize()} name contains an invalid path or filename character."

        return self._validate_path(os.path.join(target_folder, f"{name}.{self._extension}"))

    def _validate_path(self, path: str) -> tuple[str, str]:
        """Normalize and validate an absolute target chosen by either workflow."""
        if not self._project_root:
            return "", "No project root is available."

        target = resolved_path(str(path or ""))
        suffix = f".{self._extension}"
        if not target.lower().endswith(suffix.lower()):
            target += suffix

        assets_root = resolved_path(os.path.join(self._project_root, "Assets"))
        if not is_path_within(target, assets_root):
            return "", "Assets must be saved under the project's Assets directory."

        name = os.path.basename(target[: -len(suffix)])
        if not name or any(ch in name for ch in '<>:"/\\|?*'):
            return "", f"{self._asset_label.capitalize()} name contains an invalid path or filename character."
        return target, ""

    def render(self, ctx) -> None:
        """Render through the global ModalPortal."""
        if self._native_dialog_pending:
            self._native_dialog_pending = False
            self._save_with_native_dialog()
            return

        if not self._open:
            return

        popup_id = f"{self._title}###{self._semantic_prefix.replace('.', '_')}"
        request_open = self._requested
        self._requested = False
        if not begin_editor_modal(
            ctx,
            popup_id=popup_id,
            title=self._title,
            semantic_id=f"{self._semantic_prefix}.dialog",
            request_open=request_open,
            height=280.0,
        ):
            return
        ctx.text_wrapped(t("editor.asset_save.message").format(asset=self._asset_label))
        ctx.spacing()

        self._folder = ctx.text_input(
            f"{t('editor.asset_save.folder')}##{self._semantic_prefix}_folder", self._folder, 512
        )
        ctx.record_semantic_item(
            "text_input", t("editor.asset_save.folder"), True,
            f"{self._semantic_prefix}.folder",
        )
        if self._focus_name:
            ctx.set_keyboard_focus_here()
            self._focus_name = False
        self._name = ctx.text_input(
            f"{t('editor.asset_save.name')}##{self._semantic_prefix}_name", self._name, 256
        )
        ctx.record_semantic_item(
            "text_input", t("editor.asset_save.name"), True,
            f"{self._semantic_prefix}.name",
        )

        if self._error:
            ctx.spacing()
            ctx.text_wrapped(self._error)

        def _save() -> None:
            path, error = self.resolve_path()
            if error:
                self._error = error
                return
            if not self._save_path(path):
                self._error = self._error or f"The {self._asset_label} could not be saved. Check the Console for details."
                return
            ctx.close_current_popup()
            self._finish()

        def _cancel() -> None:
            ctx.close_current_popup()
            self.cancel()

        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction(t("editor.unsaved.save"), "confirm", _save),
                EditorModalAction(t("editor.unsaved.cancel"), "cancel", _cancel),
            ],
            semantic_prefix=self._semantic_prefix,
        )
        end_editor_modal(ctx)

    def _save_with_native_dialog(self) -> None:
        assets_dir = os.path.join(self._project_root, "Assets")
        default_filename = f"{self._strip_extension(self._name)}.{self._extension}"
        label = self._asset_label.capitalize()
        path = save_file_dialog(
            title=self._title,
            win32_filter=f"{label} (*.{self._extension})\0*.{self._extension}\0\0",
            initial_dir=assets_dir,
            default_filename=default_filename,
            default_ext=self._extension,
            tk_filetypes=[(f"{label} (*.{self._extension})", f"*.{self._extension}")],
        )
        if not path:
            self.cancel()
            return

        path, error = self._validate_path(path)
        if error:
            Debug.log_warning(f"[AssetSaveAsDialog] {error}")
            self.cancel()
            return
        if self._save_path(path):
            self._finish()
        else:
            self.cancel()

    def _save_path(self, path: str) -> bool:
        save_callback = self._save_callback
        if save_callback is None:
            raise RuntimeError("asset Save As has no save callback")
        normalized_path = path_key(path)
        if os.path.exists(path) and normalized_path != self._current_path:
            self._error = "An asset already exists at this location. Choose another name to avoid overwriting it."
            Debug.log_warning(f"[AssetSaveAsDialog] {self._error}")
            return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            saved = bool(save_callback(path))
        except Exception as exc:
            Debug.log_warning(f"[AssetSaveAsDialog] Save failed: {exc}")
            saved = False
        if not saved:
            self._error = f"The {self._asset_label} could not be saved. Check the Console for details."
        return saved

    def _strip_extension(self, value: str) -> str:
        name = str(value or "").strip()
        suffix = f".{self._extension}" if self._extension else ""
        if suffix and name.lower().endswith(suffix.lower()):
            name = name[: -len(suffix)]
        return name

    def cancel(self) -> None:
        callback = self._cancel_callback
        self._reset()
        if callback is not None:
            callback()

    def _finish(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._open = False
        self._requested = False
        self._focus_name = False
        self._agent_modal = False
        self._native_dialog_pending = False
        self._error = ""
        self._save_callback = None
        self._cancel_callback = None
        if self._modal_service is not None:
            self._modal_service.deactivate(self._modal_id)
