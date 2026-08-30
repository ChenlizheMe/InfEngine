"""One project-wide authoring document for Editor project settings."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
from typing import Any, Callable, Optional

from Infernux.core.document_store import submit_document_text
from Infernux.engine.path_utils import resolved_path

from .documents import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    document_content_token,
)


BUILD_SETTINGS_DEFAULTS: dict[str, Any] = {
    "build_target": "",
    "android_artifact": "apk",
    "game_name": "",
    "scenes": [],
    "output_dir": "",
    "icon_path": "",
    "display_mode": "fullscreen_borderless",
    "window_width": 1280,
    "window_height": 720,
    "window_resizable": True,
    "debug_mode": False,
    "lto": True,
    "enable_jit": False,
    "splash_items": [],
}

_SECTION_FILENAMES = {
    "build": "BuildSettings.json",
    "tag_layers": "TagLayerSettings.json",
    "physics": "PhysicsSettings.json",
}


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def normalize_build_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("build settings must be a JSON object")
    value = copy.deepcopy(value)
    # Assets is the only Player content boundary.  Drop the former authoring
    # option when an existing project is next loaded and saved.
    value.pop("additional_cook_roots", None)
    unknown = set(value) - set(BUILD_SETTINGS_DEFAULTS)
    if unknown:
        raise ValueError(
            f"unknown build settings fields: {', '.join(sorted(unknown))}"
        )
    result = copy.deepcopy(BUILD_SETTINGS_DEFAULTS)
    result.update(copy.deepcopy(value))
    if not isinstance(result["scenes"], list) or not all(
        isinstance(item, str) and item for item in result["scenes"]
    ):
        raise TypeError("build settings scenes must contain non-empty strings")
    if not isinstance(result["splash_items"], list) or not all(
        isinstance(item, dict) for item in result["splash_items"]
    ):
        raise TypeError("build settings splash_items must contain objects")
    for field in (
        "build_target",
        "android_artifact",
        "game_name",
        "output_dir",
        "icon_path",
        "display_mode",
    ):
        if not isinstance(result[field], str):
            raise TypeError(f"build settings {field} must be a string")
    if result["display_mode"] not in {"fullscreen_borderless", "windowed"}:
        raise ValueError("build settings display_mode is invalid")
    if result["android_artifact"] not in {"apk", "aab"}:
        raise ValueError("build settings android_artifact is invalid")
    if result["build_target"]:
        from Infernux.engine.build import BuildTargetId

        BuildTargetId(result["build_target"])
    for field in ("window_width", "window_height"):
        if isinstance(result[field], bool) or not isinstance(result[field], int):
            raise TypeError(f"build settings {field} must be an integer")
    for field in ("window_resizable", "debug_mode", "lto", "enable_jit"):
        if not isinstance(result[field], bool):
            raise TypeError(f"build settings {field} must be a boolean")
    return _json_copy(result)


@dataclass(frozen=True)
class _PendingSettingsWrite:
    tickets: tuple[Any, ...]
    revision: int
    document: dict[str, Any]
    save_ticket_id: str = ""


class ProjectSettingsDocumentController:
    """Own all project setting sections and their live runtime projection."""

    def __init__(
        self,
        project_path: str,
        *,
        tag_layer_manager: Any = None,
        physics_module: Any = None,
        submitter: Callable[..., Any] = submit_document_text,
    ) -> None:
        root = resolved_path(project_path)
        if not root:
            raise ValueError("project settings require a project path")
        self.project_path = root
        self.settings_path = os.path.join(root, "ProjectSettings")
        self.document_id = ""
        self._tag_layer_manager = tag_layer_manager
        self._physics_module = physics_module
        self._submitter = submitter
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._pending_writes: dict[int, _PendingSettingsWrite] = {}
        self._document = self._load_document()
        self._saved_document = copy.deepcopy(self._document)
        self._apply_runtime(self._document)

    def _manager(self):
        if self._tag_layer_manager is None:
            from Infernux.lib import TagLayerManager

            self._tag_layer_manager = TagLayerManager.instance()
        return self._tag_layer_manager

    def _physics(self):
        if self._physics_module is None:
            from Infernux.physics import settings

            self._physics_module = settings
        return self._physics_module

    def _section_path(self, section: str) -> str:
        return os.path.join(self.settings_path, _SECTION_FILENAMES[section])

    @staticmethod
    def _read_json(path: str) -> Any:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def _load_document(self) -> dict[str, Any]:
        build_path = self._section_path("build")
        build = (
            self._read_json(build_path)
            if os.path.isfile(build_path)
            else copy.deepcopy(BUILD_SETTINGS_DEFAULTS)
        )
        tag_path = self._section_path("tag_layers")
        tag_layers = (
            self._read_json(tag_path)
            if os.path.isfile(tag_path)
            else json.loads(self._manager().serialize())
        )
        physics = self._physics().load(self.project_path)
        return self._normalize_document(
            {"build": build, "tag_layers": tag_layers, "physics": physics}
        )

    def _normalize_document(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != set(_SECTION_FILENAMES):
            raise ValueError(
                "project settings must contain build, tag_layers, and physics sections"
            )
        build = normalize_build_settings(value["build"])
        tag_layers = _json_copy(value["tag_layers"])
        if not isinstance(tag_layers, dict):
            raise TypeError("tag/layer settings must be a JSON object")
        physics = self._physics().normalize(value["physics"])
        return {
            "build": build,
            "tag_layers": tag_layers,
            "physics": _json_copy(physics),
        }

    def _apply_runtime(
        self,
        document: dict[str, Any],
        previous: Optional[dict[str, Any]] = None,
    ) -> None:
        if previous is None or previous["tag_layers"] != document["tag_layers"]:
            tag_payload = json.dumps(
                document["tag_layers"], ensure_ascii=False, allow_nan=False
            )
            if self._manager().deserialize(tag_payload) is False:
                raise ValueError("tag/layer settings were rejected by the runtime")
        if previous is None or previous["physics"] != document["physics"]:
            self._physics().apply(document["physics"])

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _notify(self) -> None:
        snapshot = self.capture_document()
        for callback in tuple(self._listeners):
            callback(copy.deepcopy(snapshot))

    def capture_document(self) -> dict[str, Any]:
        return copy.deepcopy(self._document)

    def section(self, name: str) -> dict[str, Any]:
        if name not in _SECTION_FILENAMES:
            raise KeyError(f"unknown project settings section: {name}")
        return copy.deepcopy(self._document[name])

    def restore_document(
        self,
        document: dict[str, Any],
        revision: Optional[int],
        *,
        persist: bool = True,
    ) -> None:
        normalized = self._normalize_document(document)
        self._apply_runtime(normalized, self._document)
        self._document = normalized
        if revision is not None:
            DocumentRegistry.instance().restore_content_revision(
                self.document_id, int(revision)
            )
        self._notify()
        if persist:
            self.schedule_autosave()

    def apply_document(
        self,
        document: dict[str, Any],
        *,
        edit_key: str,
        description: str,
        view_id: str = "",
    ) -> bool:
        from Infernux.engine.undo import EditableDocumentDraftCommand, UndoManager

        previous = self.capture_document()
        following = self._normalize_document(document)
        if previous == following:
            return False
        registry = DocumentRegistry.instance()
        editor_document = registry.require(self.document_id)
        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return False
        next_revision = registry.reserve_changed_revision(
            self.document_id,
            view_id=view_id,
        )
        return bool(
            manager.execute(
                EditableDocumentDraftCommand(
                    self,
                    previous,
                    following,
                    editor_document.revision,
                    next_revision,
                    edit_key=str(edit_key or "project_settings"),
                    description=str(description or "Edit Project Settings"),
                )
            )
        )

    def apply_section(
        self,
        name: str,
        value: dict[str, Any],
        *,
        edit_key: str,
        description: str,
        view_id: str = "",
    ) -> bool:
        document = self.capture_document()
        if name not in document:
            raise KeyError(f"unknown project settings section: {name}")
        document[name] = copy.deepcopy(value)
        return self.apply_document(
            document,
            edit_key=edit_key,
            description=description,
            view_id=view_id,
        )

    def apply_derived_section(self, name: str, value: dict[str, Any]) -> bool:
        """Apply a consequence already owned by another journal command.

        Asset rename/move commands, for example, also update Build Settings
        scene references. The asset command remains the one user action; this
        method keeps the shared settings document and asynchronous persistence
        current without publishing a duplicate action.
        """
        document = self.capture_document()
        if name not in document:
            raise KeyError(f"unknown project settings section: {name}")
        document[name] = copy.deepcopy(value)
        following = self._normalize_document(document)
        if following == self._document:
            return False
        registry = DocumentRegistry.instance()
        revision = registry.reserve_changed_revision(
            self.document_id,
            view_id="build_settings" if name == "build" else "",
        )
        self.restore_document(following, revision, persist=True)
        return True

    @staticmethod
    def _ticket_status(ticket: Any) -> str:
        return str(getattr(ticket, "status", "") or "").rsplit(".", 1)[-1].lower()

    def _submit_snapshot(
        self,
        document: dict[str, Any],
        *,
        revision: int,
        save_ticket_id: str = "",
    ) -> Optional[_PendingSettingsWrite]:
        os.makedirs(self.settings_path, exist_ok=True)
        try:
            tickets = tuple(
                self._submitter(
                    self._section_path(section),
                    json.dumps(
                        document[section],
                        indent=2,
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    + "\n",
                )
                for section in _SECTION_FILENAMES
            )
        except Exception as exc:
            from Infernux.debug import Debug

            Debug.log_error(f"Project settings persistence submission failed: {exc}")
            return None
        pending = _PendingSettingsWrite(
            tickets=tickets,
            revision=int(revision),
            document=copy.deepcopy(document),
            save_ticket_id=str(save_ticket_id or ""),
        )
        self._pending_writes[id(pending)] = pending
        return pending

    def schedule_autosave(self) -> bool:
        document = DocumentRegistry.instance().require(self.document_id)
        return self._submit_snapshot(
            self.capture_document(), revision=document.revision
        ) is not None

    def poll_pending_writes(self) -> int:
        registry = DocumentRegistry.instance()
        completed = 0
        for key, pending in tuple(self._pending_writes.items()):
            if not all(bool(getattr(ticket, "is_complete", False)) for ticket in pending.tickets):
                continue
            statuses = tuple(self._ticket_status(ticket) for ticket in pending.tickets)
            succeeded = all(status == "succeeded" for status in statuses)
            superseded = any(status == "superseded" for status in statuses)
            message = "" if succeeded else f"project settings persistence failed: {statuses}"
            if pending.save_ticket_id:
                save_ticket = registry.get_save_ticket(pending.save_ticket_id)
                if save_ticket is not None and save_ticket.is_pending:
                    if succeeded:
                        self._saved_document = copy.deepcopy(pending.document)
                    registry.complete_save(
                        pending.save_ticket_id,
                        success=succeeded,
                        content_token=(
                            document_content_token(self.capture_document())
                            if succeeded
                            else None
                        ),
                        message=message,
                    )
            elif succeeded:
                self._saved_document = copy.deepcopy(pending.document)
                editor_document = registry.get(self.document_id)
                if (
                    editor_document is not None
                    and registry.active_save_ticket(self.document_id) is None
                ):
                    registry.mark_saved(self.document_id, pending.revision)
            elif not superseded:
                from Infernux.debug import Debug

                Debug.log_error(message)
            self._pending_writes.pop(key, None)
            completed += 1
        return completed

    def save(self, *, ticket, save_as: bool = False):
        if save_as:
            return False
        registry = DocumentRegistry.instance()
        document = self.capture_document()
        registry.capture_save_revision(
            ticket.ticket_id,
            content_token=document_content_token(document),
        )
        pending = self._submit_snapshot(
            document,
            revision=ticket.captured_revision,
            save_ticket_id=ticket.ticket_id,
        )
        if pending is None:
            return False
        self.poll_pending_writes()
        return (
            True
            if not ticket.is_pending
            else DocumentActionResult(DocumentActionStatus.PENDING)
        )

    def poll_save(self, ticket):
        self.poll_pending_writes()
        current = DocumentRegistry.instance().get_save_ticket(ticket.ticket_id)
        if current is None or current.is_pending:
            return None
        return current.status.value == "succeeded"

    def discard(self, *, document_id: str):
        if str(document_id or "") != self.document_id:
            return False
        registry = DocumentRegistry.instance()
        editor_document = registry.require(self.document_id)
        draft = self.capture_document()
        self.restore_document(self._saved_document, None, persist=False)
        pending = self._submit_snapshot(
            self.capture_document(), revision=editor_document.saved_revision
        )
        if pending is None:
            self.restore_document(draft, None, persist=False)
            return False
        return True

    def resource_moved(self, **_kwargs) -> None:
        raise RuntimeError("project settings cannot be moved as an asset")


def ensure_project_settings_document(
    project_path: str,
    *,
    view_id: str = "",
    tag_layer_manager: Any = None,
    physics_module: Any = None,
) -> ProjectSettingsDocumentController:
    root = resolved_path(project_path)
    settings_path = os.path.join(root, "ProjectSettings")
    key = DocumentKey.resource(DocumentKind.PROJECT_SETTINGS, settings_path)
    registry = DocumentRegistry.instance()
    document = registry.get_by_key(key)
    controller = document.controller if document is not None else None
    if controller is not None and not isinstance(
        controller, ProjectSettingsDocumentController
    ):
        # Older workspace snapshots could claim this shared document through a
        # generic Panel before that Panel had a chance to bind the real
        # ProjectSettings controller.  The document identity is authoritative;
        # the generic controller owns no durable Project Settings state, so
        # replace it instead of making editor startup depend on stale UI state.
        controller = ProjectSettingsDocumentController(
            root,
            tag_layer_manager=tag_layer_manager,
            physics_module=physics_module,
        )
        registry.update_metadata(
            document.document_id,
            title="Project Settings",
            resource_path=settings_path,
            capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
            controller=controller,
        )
        controller.document_id = document.document_id
    if controller is None:
        controller = ProjectSettingsDocumentController(
            root,
            tag_layer_manager=tag_layer_manager,
            physics_module=physics_module,
        )
        document = registry.create(
            DocumentKind.PROJECT_SETTINGS,
            "Project Settings",
            key=key,
            resource_path=settings_path,
            capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
            controller=controller,
        )
        controller.document_id = document.document_id
    if view_id:
        registry.attach_view(document.document_id, view_id)
    return controller


__all__ = [
    "BUILD_SETTINGS_DEFAULTS",
    "ProjectSettingsDocumentController",
    "ensure_project_settings_document",
    "normalize_build_settings",
]
