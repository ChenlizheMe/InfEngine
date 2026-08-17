"""Document controller for directly editable, autosaved asset resources."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .documents import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    document_content_token,
)


@dataclass(frozen=True)
class _PendingResourceWrite:
    ticket: Any
    revision: int
    submission_sequence: int
    document: dict
    save_ticket_id: str = ""


class EditableResourceDocumentController:
    """Own one live resource and its last durable serialized document."""

    # These resources persist every accepted edit.  A close transaction must
    # drain that persistence work instead of presenting the short interval
    # between the edit revision and its IO completion as an unsaved document.
    autosave_on_close = True

    def __init__(
        self,
        category: str,
        file_path: str,
        resource: Any,
        *,
        autosave_debounce_sec: float = 0.35,
        on_restored: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.category = str(category or "")
        self.file_path = str(file_path or "")
        self.resource = resource
        self.autosave_debounce_sec = float(autosave_debounce_sec)
        self.on_restored = on_restored
        self.document_id = ""
        self.exec_layer: Any = None
        self.state: Any = None
        self._saved_document = self.capture_document()
        self._pending_writes: dict[int, _PendingResourceWrite] = {}
        self._write_submission_sequence = 0

    def capture_document(self) -> dict:
        serializer = getattr(self.resource, "serialize_document", None)
        if not callable(serializer):
            raise TypeError(
                f"editable resource '{self.category}' must implement serialize_document()"
            )
        document = serializer()
        if not isinstance(document, dict):
            raise TypeError("editable resource serialization must return a dictionary")
        return copy.deepcopy(document)

    def bind(self, *, file_path: str, resource: Any, exec_layer: Any, state: Any) -> None:
        registry = DocumentRegistry.instance()
        document = registry.get(self.document_id)
        if document is not None and document.is_dirty and resource is not self.resource:
            # Inspector/Project views may independently resolve the same asset
            # while its authoritative document still has an asynchronous local
            # write in flight.  Keep the existing live resource; replacing it
            # here would regress the preview and discard the newer edit.
            resource = self.resource
        self.file_path = str(file_path or "")
        self.resource = resource
        self.exec_layer = exec_layer
        self.state = state
        if self.exec_layer is not None:
            self.exec_layer.refresh_binding(self.category, self.file_path)
        if document is None or not document.is_dirty:
            self._saved_document = self.capture_document()
        if self.state is not None:
            self.state.resource_controller = self
            self.state.settings = self.resource

    def restore_document(
        self,
        document: dict,
        revision: Optional[int],
        *,
        persist: bool = True,
    ) -> None:
        registry = DocumentRegistry.instance()
        if registry.get(self.document_id) is None:
            raise RuntimeError("editable resource document is no longer available")
        deserializer = getattr(self.resource, "deserialize_document", None)
        if not callable(deserializer):
            raise TypeError(
                f"editable resource '{self.category}' must implement deserialize_document()"
            )
        result = deserializer(copy.deepcopy(document))
        if result is False:
            raise RuntimeError("editable resource document was rejected")
        if self.on_restored is not None:
            self.on_restored(self.resource)
        if revision is not None:
            registry.restore_content_revision(self.document_id, int(revision))
        if self.state is not None and self.state.resource_controller is self:
            self.state.settings = self.resource
        if persist:
            self.schedule_autosave()

    def schedule_autosave(self) -> None:
        from Infernux.core.assets import AssetManager

        editor_document = DocumentRegistry.instance().get(self.document_id)
        if editor_document is not None and self.file_path:
            AssetManager.set_document_save_expected_state(
                self.file_path,
                editor_document.durable_file_state,
                edit_revision=editor_document.edit_revision or editor_document.revision,
                document_id=self.document_id,
            )
        if self.category == "material" and self.file_path:
            snapshot = json.dumps(self.capture_document())
            AssetManager.set_material_save_snapshot(
                self.file_path,
                snapshot,
                edit_revision=(
                    editor_document.edit_revision
                    if editor_document is not None
                    else 0
                ),
                document_id=self.document_id,
                expected_file_state=(
                    editor_document.durable_file_state
                    if editor_document is not None
                    else None
                ),
            )
        effect_asset = getattr(self.resource, "to_asset", None)
        if self.category == "render_effect" and self.file_path and callable(effect_asset):
            from Infernux.renderstack.render_effect_asset import (
                dump_render_effect_document,
            )

            snapshot = dump_render_effect_document(effect_asset())
            AssetManager.set_render_effect_save_snapshot(
                self.file_path,
                snapshot,
                edit_revision=(
                    editor_document.edit_revision
                    if editor_document is not None
                    else 0
                ),
                document_id=self.document_id,
                expected_file_state=(
                    editor_document.durable_file_state
                    if editor_document is not None
                    else None
                ),
            )
        if self.exec_layer is not None and not bool(
            getattr(self.exec_layer, "view_scoped_persistence", False)
        ):
            self.exec_layer.schedule_rw_save(self.resource)
            return
        AssetManager.schedule_asset_save(
            self.category,
            self.file_path,
            self.resource,
            debounce_sec=self.autosave_debounce_sec,
        )

    def apply_document(
        self,
        document: dict,
        *,
        view_id: str,
        edit_key: str,
        description: str,
        origin=None,
    ) -> bool:
        """Commit one user edit through the shared resource transaction."""
        old_document = self.capture_document()
        return self.commit_applied_document(
            old_document,
            document,
            view_id=view_id,
            edit_key=edit_key,
            description=description,
            origin=origin,
        )

    def commit_applied_document(
        self,
        old_document: dict,
        document: dict,
        *,
        view_id: str,
        edit_key: str,
        description: str,
        origin=None,
    ) -> bool:
        """Commit a live-preview edit that already changed the resource.

        Continuous controls may update native memory every frame.  The
        resulting gesture is still recorded exactly once through the shared
        document journal when it ends.
        """
        from .action_journal import ActionOrigin
        from Infernux.engine.undo import EditableDocumentDraftCommand, UndoManager

        registry = DocumentRegistry.instance()
        editor_document = registry.require(self.document_id)
        previous_document = copy.deepcopy(old_document)
        next_document = copy.deepcopy(document)
        if next_document == previous_document:
            return False
        owner_view_id = str(view_id or "").strip()
        if not owner_view_id:
            raise ValueError("editable resource mutation requires an authoring view id")
        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return False
        next_revision = registry.reserve_changed_revision(
            self.document_id,
            view_id=owner_view_id,
        )
        return bool(
            manager.execute(
                EditableDocumentDraftCommand(
                    self,
                    previous_document,
                    next_document,
                    editor_document.revision,
                    next_revision,
                    edit_key=edit_key,
                    description=description,
                ),
                origin=(ActionOrigin.USER if origin is None else ActionOrigin(origin)),
            )
        )

    def _flush_submission(self, *, force: bool = False):
        if self.exec_layer is not None and not bool(
            getattr(self.exec_layer, "view_scoped_persistence", False)
        ):
            return self.exec_layer.flush_rw_autosave(force=force)
        from Infernux.core.assets import AssetManager

        return AssetManager.flush_scheduled_saves(self.file_path, force=force)

    @staticmethod
    def _is_async_submission(submission: Any) -> bool:
        return hasattr(submission, "is_complete") and hasattr(submission, "status")

    def _track_async_submission(
        self,
        submission: Any,
        *,
        revision: int,
        document: dict,
        save_ticket_id: str = "",
    ) -> None:
        self._write_submission_sequence += 1
        self._pending_writes[id(submission)] = _PendingResourceWrite(
            ticket=submission,
            revision=int(revision),
            submission_sequence=self._write_submission_sequence,
            document=copy.deepcopy(document),
            save_ticket_id=str(save_ticket_id or ""),
        )

    def poll_pending_writes(self) -> int:
        """Publish save points only after the underlying IO ticket completes."""
        registry = DocumentRegistry.instance()
        completed = 0
        for key, pending in tuple(self._pending_writes.items()):
            if not bool(getattr(pending.ticket, "is_complete", False)):
                continue
            status = str(getattr(pending.ticket, "status", "") or "").lower()
            succeeded = status == "succeeded"
            diagnostic = str(getattr(pending.ticket, "error", "") or "").strip()
            message = (
                ""
                if succeeded
                else diagnostic or f"asset persistence {status or 'failed'}"
            )
            newer_submission_exists = any(
                other is not pending
                and other.submission_sequence > pending.submission_sequence
                for other in self._pending_writes.values()
            )
            if pending.save_ticket_id:
                save_ticket = registry.get_save_ticket(pending.save_ticket_id)
                if save_ticket is not None and save_ticket.is_pending:
                    if succeeded and not newer_submission_exists:
                        self._saved_document = copy.deepcopy(pending.document)
                    registry.complete_save(
                        pending.save_ticket_id,
                        success=succeeded,
                        content_token=(
                            document_content_token(self.capture_document())
                            if succeeded
                            else None
                        ),
                        committed_file_state=(
                            getattr(pending.ticket, "committed_file_state", None)
                            if succeeded
                            else None
                        ),
                        conflict=(
                            "changed outside the editor" in diagnostic.casefold()
                        ),
                        message=message,
                    )
            elif succeeded:
                editor_document = registry.get(self.document_id)
                if (
                    editor_document is not None
                    and registry.active_save_ticket(self.document_id) is None
                    and not newer_submission_exists
                ):
                    self._saved_document = copy.deepcopy(pending.document)
                    registry.mark_saved(self.document_id, pending.revision)
                    editor_document.durable_file_state = getattr(
                        pending.ticket,
                        "committed_file_state",
                        editor_document.durable_file_state,
                    )
            elif (
                "changed outside the editor" in diagnostic.casefold()
            ):
                editor_document = registry.get(self.document_id)
                if (
                    editor_document is not None
                    and editor_document.kind is DocumentKind.SCENE
                ):
                    registry.mark_conflict(editor_document.document_id)
            self._pending_writes.pop(key, None)
            completed += 1
        return completed

    def poll_save(self, ticket):
        """CloseCoordinator hook for a pending document SaveTicket."""
        self.poll_pending_writes()
        current = DocumentRegistry.instance().get_save_ticket(ticket.ticket_id)
        if current is None or current.is_pending:
            return None
        return current.status.value == "succeeded"

    def flush_autosave(self, *, force: bool = False) -> bool:
        registry = DocumentRegistry.instance()
        editor_document = registry.get(self.document_id)
        if editor_document is None:
            return False
        serialized_document = self.capture_document()
        submission = self._flush_submission(force=force)
        if submission is False or submission is None:
            return False
        if self._is_async_submission(submission):
            self._track_async_submission(
                submission,
                revision=editor_document.revision,
                document=serialized_document,
            )
            self.poll_pending_writes()
            return True
        self._saved_document = copy.deepcopy(serialized_document)
        if registry.active_save_ticket(self.document_id) is None:
            registry.mark_saved(self.document_id, editor_document.revision)
        return True

    def save(self, *, ticket, save_as: bool = False):
        if save_as:
            return False
        registry = DocumentRegistry.instance()
        serialized_document = self.capture_document()
        registry.capture_save_revision(
            ticket.ticket_id,
            content_token=document_content_token(serialized_document),
        )
        self.schedule_autosave()
        submission = self._flush_submission(force=True)
        if submission is False or submission is None:
            return False
        if self._is_async_submission(submission):
            self._track_async_submission(
                submission,
                revision=ticket.captured_revision,
                document=serialized_document,
                save_ticket_id=ticket.ticket_id,
            )
            self.poll_pending_writes()
            if ticket.is_pending:
                return DocumentActionResult(DocumentActionStatus.PENDING)
            return True
        self._saved_document = copy.deepcopy(serialized_document)
        registry.complete_save(
            ticket.ticket_id,
            success=True,
            content_token=document_content_token(self.capture_document()),
        )
        return True

    def discard(self, *, document_id: str):
        if str(document_id or "") != self.document_id:
            return False
        registry = DocumentRegistry.instance()
        editor_document = registry.require(self.document_id)
        draft_document = self.capture_document()
        had_submitted_writes = bool(self._pending_writes)
        cancel = (
            getattr(self.exec_layer, "cancel_rw_autosave", None)
            if not bool(getattr(self.exec_layer, "view_scoped_persistence", False))
            else None
        )
        if callable(cancel):
            cancel()
        else:
            from Infernux.core.assets import AssetManager

            AssetManager.cancel_scheduled_save(self.file_path)
        self.restore_document(
            self._saved_document,
            None,
            persist=False,
        )
        if had_submitted_writes:
            baseline = self.capture_document()
            self.schedule_autosave()
            submission = self._flush_submission(force=True)
            if submission is False or submission is None:
                self.restore_document(draft_document, None, persist=False)
                return False
            if self._is_async_submission(submission):
                self._track_async_submission(
                    submission,
                    revision=editor_document.saved_revision,
                    document=baseline,
                )
                self.poll_pending_writes()
        return True

    def reload_from_resource(self, *, document_id: str, resource_path: str):
        if str(document_id or "") != self.document_id:
            return False
        target = str(resource_path or self.file_path or "").strip()
        if not target:
            return False
        cancel = (
            getattr(self.exec_layer, "cancel_rw_autosave", None)
            if not bool(getattr(self.exec_layer, "view_scoped_persistence", False))
            else None
        )
        if callable(cancel):
            cancel()
        else:
            from Infernux.core.assets import AssetManager

            AssetManager.cancel_scheduled_save(self.file_path)
        with open(target, "r", encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict):
            raise ValueError("editable resource source must contain a JSON object")
        self.restore_document(document, None, persist=False)
        self._saved_document = copy.deepcopy(document)
        return True

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del source_path, guid
        if str(document_id or "") != self.document_id:
            return
        self.file_path = str(destination_path or "")
        if self.exec_layer is not None:
            self.exec_layer.refresh_binding(self.category, self.file_path)


def ensure_editable_resource_document(
    *,
    category: str,
    document_kind: DocumentKind,
    file_path: str,
    resource: Any,
    guid: str = "",
    title: str = "",
    view_id: str = "",
    state: Any = None,
    exec_layer: Any = None,
    autosave_debounce_sec: float = 0.35,
    on_restored: Optional[Callable[[Any], None]] = None,
) -> EditableResourceDocumentController:
    """Resolve one shared asset document independently of any Inspector view."""
    path = str(file_path or "").strip()
    if not path:
        raise ValueError("editable resource document requires a file path")
    kind = DocumentKind(document_kind)
    asset_guid = str(guid or getattr(resource, "guid", "") or "").strip()
    key = (
        DocumentKey.asset(kind, asset_guid)
        if asset_guid
        else DocumentKey.resource(kind, path)
    )
    registry = DocumentRegistry.instance()
    document = registry.get_by_key(key)
    controller = document.controller if document is not None else None
    if controller is not None and not isinstance(
        controller, EditableResourceDocumentController
    ):
        raise RuntimeError("editable resource document has an incompatible controller")

    if controller is None:
        controller = EditableResourceDocumentController(
            category,
            path,
            resource,
            autosave_debounce_sec=autosave_debounce_sec,
            on_restored=on_restored,
        )
        document = registry.create(
            kind,
            title or os.path.basename(path),
            key=key,
            resource_path=path,
            capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
            controller=controller,
        )
        controller.document_id = document.document_id
        controller.bind(
            file_path=path,
            resource=resource,
            exec_layer=exec_layer,
            state=state,
        )
    else:
        if on_restored is not None:
            controller.on_restored = on_restored
        registry.update_metadata(
            document.document_id,
            title=title or os.path.basename(path),
            resource_path=path,
            capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
            controller=controller,
        )
        if (
            controller.file_path != path
            or controller.resource is not resource
            or (exec_layer is not None and controller.exec_layer is not exec_layer)
            or (state is not None and controller.state is not state)
        ):
            controller.bind(
                file_path=path,
                resource=resource,
                exec_layer=exec_layer if exec_layer is not None else controller.exec_layer,
                state=state if state is not None else controller.state,
            )

    if view_id:
        registry.attach_view(document.document_id, view_id)
    return controller
