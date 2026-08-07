from __future__ import annotations

from Infernux.engine.interaction import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    DocumentState,
    ExternalDocumentConflictService,
    ModalService,
)
from Infernux.engine.ui.external_document_conflict import (
    ExternalDocumentConflictCoordinator,
)


class _Controller:
    def __init__(self, registry: DocumentRegistry) -> None:
        self.registry = registry
        self.discard_calls = 0
        self.reload_calls = 0
        self.pending_save = None

    def discard(self, *, document_id: str) -> bool:
        del document_id
        self.discard_calls += 1
        return True

    def reload_from_resource(self, *, document_id: str, resource_path: str) -> bool:
        del document_id, resource_path
        self.reload_calls += 1
        return True

    def save(self, *, ticket, save_as: bool = False):
        assert save_as
        self.registry.capture_save_revision(ticket.ticket_id)
        self.pending_save = ticket
        return DocumentActionResult(DocumentActionStatus.PENDING)

    def poll_save(self, ticket):
        assert ticket is self.pending_save
        self.registry.complete_save(
            ticket.ticket_id,
            success=True,
            key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, "copy.particlegraph"),
            resource_path="copy.particlegraph",
            title="Copy",
        )
        self.pending_save = None
        return True


def _conflicted_document(registry: DocumentRegistry, title: str = "Smoke"):
    controller = _Controller(registry)
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        title,
        key=DocumentKey.resource(
            DocumentKind.PARTICLE_GRAPH,
            f"{title}.particlegraph",
        ),
        resource_path=f"{title}.particlegraph",
        revision=1,
        saved_revision=0,
        capabilities=(
            DocumentCapability.SAVE
            | DocumentCapability.SAVE_AS
            | DocumentCapability.DISCARD
        ),
        controller=controller,
    )
    registry.mark_conflict(document.document_id)
    return document, controller


def test_external_conflict_is_presented_and_keep_local_preserves_dirty_draft():
    registry = DocumentRegistry()
    modals = ModalService()
    document, _controller = _conflicted_document(registry)
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)

    coordinator.poll()

    assert coordinator.active_document_id == document.document_id
    assert modals.active_modal_id == coordinator.MODAL_ID
    assert coordinator.choose_keep_local()
    assert document.state is DocumentState.READY
    assert document.is_dirty
    assert not coordinator.is_active
    assert modals.active_modal_id == ""


def test_external_conflict_reload_establishes_the_disk_version_as_clean_baseline():
    registry = DocumentRegistry()
    modals = ModalService()
    document, controller = _conflicted_document(registry)
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)
    coordinator.poll()

    assert coordinator.choose_reload()

    assert controller.reload_calls == 1
    assert controller.discard_calls == 0
    assert document.state is DocumentState.READY
    assert document.revision == document.saved_revision
    assert not document.is_dirty


def test_external_conflict_save_copy_waits_for_document_save_completion():
    registry = DocumentRegistry()
    modals = ModalService()
    document, controller = _conflicted_document(registry)
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)
    coordinator.poll()

    assert coordinator.choose_save_copy()
    assert coordinator.waiting_for_save_copy
    assert controller.pending_save is not None
    assert document.state is DocumentState.SAVING

    coordinator.poll()

    assert controller.pending_save is None
    assert document.state is DocumentState.READY
    assert document.resource_path == "copy.particlegraph"
    assert not coordinator.is_active


def test_external_conflicts_are_serialized_one_document_at_a_time():
    registry = DocumentRegistry()
    modals = ModalService()
    first, _first_controller = _conflicted_document(registry, "First")
    second, _second_controller = _conflicted_document(registry, "Second")
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)

    coordinator.poll()
    assert coordinator.active_document_id == first.document_id
    assert coordinator.choose_keep_local()

    coordinator.poll()
    assert coordinator.active_document_id == second.document_id
    assert coordinator.choose_keep_local()
    assert not coordinator.is_active


def test_stale_conflict_revision_cannot_resolve_a_new_external_change():
    registry = DocumentRegistry()
    document, _controller = _conflicted_document(registry)
    service = ExternalDocumentConflictService(registry)
    service.poll()
    conflict = service.active
    assert conflict is not None

    registry.publish_external_resource_change(document.resource_path)
    result = service.keep_local(conflict.conflict_id)

    assert result.status is DocumentActionStatus.REJECTED
    assert document.state is DocumentState.CONFLICT
