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
    DocumentState,
)


class _Controller:
    def __init__(self, registry: DocumentRegistry, document_id: str) -> None:
        self.registry = registry
        self.document_id = document_id
        self.pending_ticket = None
        self.discard_calls = 0
        self.reload_calls = 0

    def save(self, *, ticket, save_as: bool = False):
        del save_as
        if self.pending_ticket is not None:
            self.pending_ticket = ticket
            return DocumentActionResult(DocumentActionStatus.PENDING)
        self.registry.capture_save_revision(ticket.ticket_id)
        self.registry.complete_save(ticket.ticket_id, success=True)
        return True

    def discard(self, *, document_id: str):
        assert document_id == self.document_id
        self.discard_calls += 1
        return True

    def reload_from_resource(self, *, document_id: str, resource_path: str):
        assert document_id == self.document_id
        del resource_path
        self.reload_calls += 1
        return True


class _AutosaveController(_Controller):
    autosave_on_close = True

    def __init__(self, registry: DocumentRegistry, document_id: str) -> None:
        super().__init__(registry, document_id)
        self.pending_ticket = True
        self.save_calls = 0

    def save(self, *, ticket, save_as: bool = False):
        self.save_calls += 1
        return super().save(ticket=ticket, save_as=save_as)


def _dirty_document(
    registry: DocumentRegistry,
    document_id: str,
    *,
    kind: DocumentKind = DocumentKind.GENERIC,
):
    document = registry.create(
        kind,
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


def test_closing_one_of_multiple_views_never_prompts_for_the_document():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "shared")
    registry.attach_view(document.document_id, "scene-view")
    registry.attach_view(document.document_id, "game-view")
    completed: list[str] = []
    close = CloseCoordinator(registry)

    close.request(
        CloseIntent(CloseIntentKind.CLOSE_VIEW, view_id="scene-view"),
        lambda: completed.append("closed"),
    )

    assert completed == ["closed"]
    assert close.state is CloseState.IDLE
    assert document.is_dirty
    assert controller.discard_calls == 0


def test_reset_layout_with_explicit_empty_scope_does_not_prompt_for_all_dirty_documents():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "shared")
    completed: list[str] = []
    close = CloseCoordinator(registry)

    assert close.request(
        CloseIntent(CloseIntentKind.RESET_LAYOUT, document_ids=()),
        lambda: completed.append("reset"),
    )

    assert completed == ["reset"]
    assert close.state is CloseState.IDLE
    assert document.is_dirty
    assert controller.discard_calls == 0


def test_exit_discard_abandons_session_state_without_rebuilding_documents():
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
    assert not first.is_dirty and not second.is_dirty
    assert registry.capture_session_state()["documents"] == []


def test_exit_orders_prefab_before_suspended_scene():
    registry = DocumentRegistry()
    scene, _scene_controller = _dirty_document(
        registry,
        "scene",
        kind=DocumentKind.SCENE,
    )
    prefab, _prefab_controller = _dirty_document(
        registry,
        "prefab",
        kind=DocumentKind.PREFAB,
    )
    close = CloseCoordinator(registry)

    close.request(CloseIntent(CloseIntentKind.EXIT_EDITOR), lambda: None)

    assert close.active_document is prefab
    close.decide_discard()
    assert close.active_document is scene


def test_replace_document_discard_approves_replacement_without_reloading_source():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "replace")
    completed: list[str] = []
    close = CloseCoordinator(registry)
    close.request(
        CloseIntent(
            CloseIntentKind.REPLACE_DOCUMENT,
            document_ids=(document.document_id,),
        ),
        lambda: completed.append("replace"),
    )

    close.decide_discard()

    assert completed == ["replace"]
    assert controller.discard_calls == 0
    assert document.is_dirty


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


def test_close_drains_autosaved_resource_without_showing_unsaved_decision():
    registry = DocumentRegistry()
    document, _ = _dirty_document(registry, "autosaved-material")
    controller = _AutosaveController(registry, document.document_id)
    registry.update_metadata(document.document_id, controller=controller)
    completed: list[str] = []
    close = CloseCoordinator(registry)

    close.request(
        CloseIntent(
            CloseIntentKind.CLOSE_VIEW,
            document_ids=(document.document_id,),
        ),
        lambda: completed.append("closed"),
    )

    assert controller.save_calls == 1
    assert close.state is CloseState.WAITING_FOR_SAVE
    ticket = registry.active_save_ticket(document.document_id)
    assert ticket is not None

    registry.complete_save(ticket.ticket_id, success=True)
    close.poll()

    assert completed == ["closed"]
    assert close.state is CloseState.IDLE
    assert document.is_dirty is False


def test_failed_close_autosave_falls_back_to_explicit_decision_once():
    registry = DocumentRegistry()
    document, _ = _dirty_document(registry, "failed-autosave")
    controller = _AutosaveController(registry, document.document_id)
    registry.update_metadata(document.document_id, controller=controller)
    close = CloseCoordinator(registry)

    close.request(
        CloseIntent(
            CloseIntentKind.EXIT_EDITOR,
            document_ids=(document.document_id,),
        ),
        lambda: None,
    )
    ticket = registry.active_save_ticket(document.document_id)
    assert ticket is not None

    registry.complete_save(ticket.ticket_id, success=False)
    close.poll()

    assert controller.save_calls == 1
    assert close.state is CloseState.AWAITING_DECISION
    assert close.active_document is document
    assert close.issue is CloseIssue.SAVE_CANCELLED


def test_conflicted_close_autosave_enters_document_conflict_arbitration():
    registry = DocumentRegistry()
    document, _ = _dirty_document(registry, "conflicted-autosave")
    controller = _AutosaveController(registry, document.document_id)
    registry.update_metadata(document.document_id, controller=controller)
    close = CloseCoordinator(registry)

    close.request(
        CloseIntent(
            CloseIntentKind.EXIT_EDITOR,
            document_ids=(document.document_id,),
        ),
        lambda: None,
    )
    ticket = registry.active_save_ticket(document.document_id)
    assert ticket is not None

    registry.complete_save(ticket.ticket_id, success=False, conflict=True)
    close.poll()

    assert controller.save_calls == 1
    assert close.state is CloseState.WAITING_FOR_CONFLICT
    assert close.active_document is document
    assert document.state is DocumentState.CONFLICT


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


def test_close_waits_for_external_conflict_before_dirty_decision():
    registry = DocumentRegistry()
    document, _controller = _dirty_document(registry, "conflicted")
    registry.mark_conflict(document.document_id)
    close = CloseCoordinator(registry)

    close.request(
        CloseIntent(
            CloseIntentKind.CLOSE_VIEW,
            document_ids=(document.document_id,),
        ),
        lambda: None,
    )

    assert close.state is CloseState.WAITING_FOR_CONFLICT
    assert close.active_document is document

    registry.resolve_conflict_keep_local(document.document_id)
    close.poll()

    assert close.state is CloseState.AWAITING_DECISION
    assert document.state is DocumentState.READY
    assert document.is_dirty


def test_clean_conflict_reload_allows_close_to_finish():
    registry = DocumentRegistry()
    document, controller = _dirty_document(registry, "reload-conflict")
    registry.mark_saved(document.document_id)
    registry.mark_conflict(document.document_id)
    completed: list[str] = []
    close = CloseCoordinator(registry)
    close.request(
        CloseIntent(
            CloseIntentKind.CLOSE_VIEW,
            document_ids=(document.document_id,),
        ),
        lambda: completed.append("closed"),
    )

    assert close.state is CloseState.WAITING_FOR_CONFLICT
    assert registry.request_reload_external(document.document_id).accepted
    close.poll()

    assert controller.reload_calls == 1
    assert completed == ["closed"]
    assert close.state is CloseState.IDLE
