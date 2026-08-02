from __future__ import annotations

from Infernux.engine.interaction import (
    CloseCoordinator,
    CloseIntent,
    CloseIntentKind,
    CloseIssue,
    CloseState,
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKind,
    DocumentRegistry,
)


class _Controller:
    def __init__(self, registry: DocumentRegistry, document_id: str) -> None:
        self.registry = registry
        self.document_id = document_id
        self.pending_ticket = None
        self.discard_calls = 0

    def save(self, *, ticket, save_as: bool = False):
        if self.pending_ticket is not None:
            self.pending_ticket = ticket
            return DocumentActionResult(DocumentActionStatus.PENDING)
        return True

    def discard(self):
        self.discard_calls += 1
        self.registry.mark_saved(self.document_id)
        return True


def _dirty_document(registry: DocumentRegistry, document_id: str):
    document = registry.create(
        DocumentKind.GENERIC,
        document_id,
        document_id=document_id,
        revision=1,
        saved_revision=0,
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
    )
    controller = _Controller(registry, document_id)
    registry.update_metadata(document_id, controller=controller)
    return document, controller


def test_close_view_discard_uses_document_controller_once():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "view-document")
    registry.attach_view(document.document_id, "inspector-view")
    completed: list[str] = []
    close = CloseCoordinator(registry)

    assert close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, view_id="inspector-view"),
        lambda: completed.append("closed"),
    )
    close.decide_discard()

    assert completed == ["closed"]
    assert controller.discard_calls == 1
    assert not document.is_dirty
    assert close.state is CloseState.IDLE


def test_exit_discard_defers_mutation_and_visits_each_document_once():
    registry = DocumentRegistry()
    first, first_controller = _dirty_document(registry, "first")
    second, second_controller = _dirty_document(registry, "second")
    registry.attach_view(first.document_id, "first-left")
    registry.attach_view(first.document_id, "first-right")
    completed: list[str] = []
    close = CloseCoordinator(registry)

    close.request(
        CloseIntent(CloseIntentKind.EXIT_EDITOR),
        lambda: completed.append("exit"),
    )
    assert close.active_document is first
    close.decide_discard()
    assert close.active_document is second
    close.decide_discard()

    assert completed == ["exit"]
    assert first_controller.discard_calls == 0
    assert second_controller.discard_calls == 0
    assert first.is_dirty and second.is_dirty


def test_async_save_waits_for_captured_revision_and_then_advances():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "async")
    controller.pending_ticket = True
    completed: list[str] = []
    close = CloseCoordinator(registry)
    close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, document_ids=(document.document_id,)),
        lambda: completed.append("closed"),
    )

    close.decide_save()
    assert close.state is CloseState.WAITING_FOR_SAVE
    ticket = registry.active_save_ticket(document.document_id)
    assert ticket is not None

    registry.complete_save(ticket.ticket_id, success=True)
    close.poll()

    assert completed == ["closed"]
    assert close.state is CloseState.IDLE


def test_cancelled_async_save_returns_to_same_document_decision():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "cancelled")
    controller.pending_ticket = True
    close = CloseCoordinator(registry)
    close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, document_ids=(document.document_id,)),
        lambda: None,
    )
    close.decide_save()
    ticket = registry.active_save_ticket(document.document_id)
    assert ticket is not None

    registry.complete_save(ticket.ticket_id, success=False, cancelled=True)
    close.poll()

    assert close.state is CloseState.AWAITING_DECISION
    assert close.active_document is document
    assert close.issue is CloseIssue.SAVE_CANCELLED


def test_cancel_invokes_callback_without_mutating_document():
    registry = DocumentRegistry()
    document, _ = _dirty_document(registry, "cancel")
    cancelled: list[str] = []
    close = CloseCoordinator(registry)
    close.request(
        CloseIntent(CloseIntentKind.REPLACE_DOCUMENT, document_ids=(document.document_id,)),
        lambda: None,
        lambda: cancelled.append("cancelled"),
    )

    close.cancel()

    assert cancelled == ["cancelled"]
    assert document.is_dirty
    assert close.state is CloseState.IDLE
