from __future__ import annotations

from dataclasses import dataclass
import copy
import os

import pytest

from Infernux.engine.interaction import (
    AuthoringAssetSnapshot,
    AuthoringDocumentController,
    AuthoringMutationService,
    CloseCoordinator,
    CloseIntent,
    CloseIntentKind,
    CloseState,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    document_content_token,
)
from Infernux.engine.undo import UndoManager


@dataclass
class _IOTicket:
    status: str = "pending"
    error: str = ""
    committed_file_state: object = None

    @property
    def is_complete(self) -> bool:
        return self.status != "pending"


class _MatrixAuthoringView:
    def __init__(self, *, title: str, path: str) -> None:
        self.title = title
        self.path = path
        self.value = 1
        self.saved_snapshot = self.capture_authoring_snapshot()

    def _payload(self, *, title: str | None = None) -> dict:
        return {"title": title or self.title, "value": self.value}

    def capture_authoring_save_snapshot(self, target_path: str):
        title = os.path.splitext(os.path.basename(target_path))[0]
        payload = self._payload(title=title)
        return AuthoringAssetSnapshot(
            target_path,
            repr(payload) + "\n",
            document_content_token(payload),
            title,
            copy.deepcopy(payload),
        )

    def publish_authoring_save_snapshot(self, snapshot) -> str:
        self.title = snapshot.title
        self.path = snapshot.target_path
        self.saved_snapshot = copy.deepcopy(snapshot.payload)
        return ""

    def current_authoring_content_token(self) -> str:
        return document_content_token(self._payload())

    def capture_authoring_snapshot(self):
        return self._payload()

    def restore_authoring_snapshot(self, snapshot) -> None:
        self.title = str(snapshot["title"])
        self.value = int(snapshot["value"])

    def discard(self, *, document_id: str) -> bool:
        del document_id
        self.restore_authoring_snapshot(copy.deepcopy(self.saved_snapshot))
        return True


_AUTHORING_WINDOWS = (
    (DocumentKind.PARTICLE_GRAPH, "particle_graph_editor", ".particlegraph"),
    (DocumentKind.ANIMATION_FSM, "animfsm_editor", ".animfsm"),
    (DocumentKind.TIMELINE, "animtimeline_editor", ".animtimeline"),
    (DocumentKind.ANIMATION_CLIP, "animclip2d_editor", ".animclip2d"),
)


@pytest.mark.parametrize("kind,view_id,extension", _AUTHORING_WINDOWS)
def test_authoring_window_save_close_and_history_matrix(
    monkeypatch,
    tmp_path,
    kind,
    view_id,
    extension,
):
    import Infernux.engine.interaction.authoring_documents as persistence

    io_tickets: list[_IOTicket] = []

    def submit(*_args, **_kwargs):
        ticket = _IOTicket()
        io_tickets.append(ticket)
        return ticket

    monkeypatch.setattr(persistence, "submit_document_text", submit)
    registry = DocumentRegistry()
    undo = UndoManager()
    source = str(tmp_path / f"Source{extension}")
    target = str(tmp_path / f"SavedCopy{extension}")
    view = _MatrixAuthoringView(title="Source", path=source)
    controller = AuthoringDocumentController(view)
    document = registry.create(
        kind,
        "Source",
        key=DocumentKey.resource(kind, source),
        resource_path=source,
        revision=1,
        saved_revision=0,
        capabilities=(
            DocumentCapability.SAVE
            | DocumentCapability.SAVE_AS
            | DocumentCapability.DISCARD
        ),
        controller=controller,
    )
    registry.attach_view(document.document_id, view_id)

    # Save As owns identity publication only after durable completion.
    result = registry.request_save_to_resource(document.document_id, target)
    assert result.status is DocumentActionStatus.PENDING
    assert document.resource_path == source
    io_tickets[-1].status = "succeeded"
    assert controller.poll_pending_writes() == 1
    assert document.resource_path == target
    assert not document.is_dirty

    # Every authoring kind records model edits in the same global history.
    assert AuthoringMutationService.require().apply(
        document.document_id,
        "Change authoring value",
        lambda: setattr(view, "value", 2),
        view_id=view_id,
    )
    assert view.value == 2 and document.is_dirty
    undo.undo()
    assert view.value == 1 and not document.is_dirty
    undo.redo()
    assert view.value == 2 and document.is_dirty

    # Cancel leaves both the document and its View attached and unchanged.
    cancelled: list[str] = []
    close = CloseCoordinator(registry)
    assert close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, view_id=view_id),
        lambda: pytest.fail("cancelled close must not complete"),
        lambda: cancelled.append("cancelled"),
    )
    assert close.state is CloseState.AWAITING_DECISION
    close.cancel()
    assert cancelled == ["cancelled"]
    assert document.is_dirty and document.view_ids == {view_id}

    # Discard restores the last durable authoring snapshot exactly once.
    discarded: list[str] = []
    assert close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, view_id=view_id),
        lambda: discarded.append("discarded"),
    )
    close.decide_discard()
    assert discarded == ["discarded"]
    assert view.value == 1 and not document.is_dirty

    # Ordinary Save follows the same asynchronous transaction and lets close
    # finish only after the write has become durable.
    assert AuthoringMutationService.require().apply(
        document.document_id,
        "Change before save",
        lambda: setattr(view, "value", 3),
        view_id=view_id,
    )
    saved: list[str] = []
    assert close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, view_id=view_id),
        lambda: saved.append("saved"),
    )
    close.decide_save()
    assert close.state is CloseState.WAITING_FOR_SAVE
    io_tickets[-1].status = "succeeded"
    assert controller.poll_pending_writes() == 1
    close.poll()
    assert saved == ["saved"]
    assert close.state is CloseState.IDLE
    assert view.value == 3 and not document.is_dirty

