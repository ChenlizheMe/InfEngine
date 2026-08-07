from __future__ import annotations

from dataclasses import dataclass
import os

from Infernux.engine.interaction import (
    AuthoringAssetSnapshot,
    AuthoringDocumentController,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    SaveTicketStatus,
    document_content_token,
)


@dataclass
class _IOTicket:
    status: str = "pending"
    error: str = ""

    @property
    def is_complete(self) -> bool:
        return self.status != "pending"


class _AuthoringView:
    def __init__(self) -> None:
        self.value = 1
        self.title = "Smoke"
        self.path = ""
        self.published = []

    def capture_authoring_save_snapshot(self, target_path: str):
        title = os.path.splitext(os.path.basename(target_path))[0]
        payload = {"title": title, "value": self.value}
        return AuthoringAssetSnapshot(
            target_path,
            f"{payload!r}\n",
            document_content_token(payload),
            title,
            payload,
        )

    def publish_authoring_save_snapshot(self, snapshot) -> str:
        self.title = snapshot.title
        self.path = snapshot.target_path
        self.published.append(snapshot)
        return ""

    def current_authoring_content_token(self) -> str:
        return document_content_token({"title": self.title, "value": self.value})

    def discard(self, *, document_id: str):
        del document_id
        return True


def _bound_document(registry: DocumentRegistry, view: _AuthoringView, path: str):
    controller = AuthoringDocumentController(view)
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, path),
        resource_path=path,
        revision=1,
        saved_revision=0,
        capabilities=(
            DocumentCapability.SAVE
            | DocumentCapability.SAVE_AS
            | DocumentCapability.DISCARD
        ),
        controller=controller,
    )
    return document, controller


def test_async_authoring_save_keeps_newer_edit_dirty(monkeypatch, tmp_path):
    import Infernux.engine.interaction.authoring_documents as module

    io_ticket = _IOTicket()
    monkeypatch.setattr(module, "submit_document_text", lambda *_args, **_kwargs: io_ticket)
    registry = DocumentRegistry()
    view = _AuthoringView()
    path = str(tmp_path / "Smoke.particlegraph")
    document, controller = _bound_document(registry, view, path)

    result = registry.request_save(document.document_id)
    assert result.status is DocumentActionStatus.PENDING
    save_ticket = registry.active_save_ticket(document.document_id)
    assert save_ticket is not None
    captured_revision = document.revision
    view.value = 2
    registry.mark_changed(document.document_id, view_id="particle_graph_editor")

    io_ticket.status = "succeeded"
    assert controller.poll_pending_writes() == 1

    assert save_ticket.status is SaveTicketStatus.SUCCEEDED
    assert document.saved_revision == captured_revision
    assert document.revision > document.saved_revision
    assert document.is_dirty
    assert len(view.published) == 1


def test_async_authoring_save_reports_native_io_error_and_preserves_dirty(
    monkeypatch,
    tmp_path,
):
    import Infernux.engine.interaction.authoring_documents as module

    io_ticket = _IOTicket()
    monkeypatch.setattr(module, "submit_document_text", lambda *_args, **_kwargs: io_ticket)
    registry = DocumentRegistry()
    view = _AuthoringView()
    document, controller = _bound_document(
        registry,
        view,
        str(tmp_path / "Smoke.particlegraph"),
    )

    assert registry.request_save(document.document_id).status is DocumentActionStatus.PENDING
    save_ticket = registry.active_save_ticket(document.document_id)
    assert save_ticket is not None
    io_ticket.status = "failed"
    io_ticket.error = "disk full"
    assert controller.poll_pending_writes() == 1

    assert save_ticket.status is SaveTicketStatus.FAILED
    assert save_ticket.message == "disk full"
    assert document.saved_revision == 0
    assert document.is_dirty
    assert view.published == []


def test_save_as_changes_document_identity_only_after_durable_completion(
    monkeypatch,
    tmp_path,
):
    import Infernux.engine.interaction.authoring_documents as module

    io_ticket = _IOTicket()
    monkeypatch.setattr(module, "submit_document_text", lambda *_args, **_kwargs: io_ticket)
    registry = DocumentRegistry()
    view = _AuthoringView()
    source = str(tmp_path / "Source.particlegraph")
    target = str(tmp_path / "Copy.particlegraph")
    document, controller = _bound_document(registry, view, source)

    result = registry.request_save_to_resource(document.document_id, target)
    assert result.status is DocumentActionStatus.PENDING
    assert document.resource_path == source
    assert view.path == ""

    io_ticket.status = "succeeded"
    controller.poll_pending_writes()

    assert document.resource_path == target
    assert document.key == DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, target)
    assert document.title == "Copy"
    assert view.path == target
    assert not document.is_dirty
