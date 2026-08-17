from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def animation_replacement_services(monkeypatch):
    from Infernux.engine.interaction import (
        CloseCoordinator,
        DocumentRegistry,
        EditorCommandRegistry,
        EditorInteractionCore,
        FocusService,
        ModalService,
        SelectionService,
    )
    from Infernux.engine.ui.dirty_panel_confirmation import (
        DirtyPanelConfirmationCoordinator,
    )

    previous_documents = DocumentRegistry._instance
    previous_focus = FocusService._instance
    previous_selection = SelectionService._instance
    previous_commands = EditorCommandRegistry._instance
    previous_core = EditorInteractionCore._instance
    documents = DocumentRegistry()
    FocusService()
    SelectionService()
    close = CloseCoordinator(documents)
    confirmation = DirtyPanelConfirmationCoordinator(close, ModalService())
    EditorInteractionCore._instance = SimpleNamespace(
        close_coordinator=close,
        commands=EditorCommandRegistry(),
    )
    monkeypatch.setattr(
        DirtyPanelConfirmationCoordinator,
        "instance",
        classmethod(lambda cls: confirmation),
    )
    try:
        yield documents, close, confirmation
    finally:
        documents.clear()
        DocumentRegistry._instance = previous_documents
        FocusService._instance = previous_focus
        SelectionService._instance = previous_selection
        EditorCommandRegistry._instance = previous_commands
        EditorInteractionCore._instance = previous_core


@pytest.mark.parametrize(
    ("panel_factory", "command_name", "can_name", "model_identity"),
    (
        (
            lambda: __import__(
                "Infernux.engine.ui.animclip2d_editor_panel",
                fromlist=["AnimClip2DEditorPanel"],
            ).AnimClip2DEditorPanel(),
            "command_new_clip_document",
            "can_new_clip_document",
            lambda panel: panel._active_clip.stable_id,
        ),
        (
            lambda: __import__(
                "Infernux.engine.ui.animfsm_editor_panel",
                fromlist=["AnimFSMEditorPanel"],
            ).AnimFSMEditorPanel(),
            "command_new_fsm",
            "can_new_fsm",
            lambda panel: id(panel._fsm),
        ),
    ),
    ids=("animclip2d", "animfsm"),
)
def test_animation_new_command_uses_shared_save_discard_cancel_replacement(
    animation_replacement_services,
    panel_factory,
    command_name,
    can_name,
    model_identity,
):
    from Infernux.engine.interaction import CloseIntentKind

    documents, close, confirmation = animation_replacement_services
    panel = panel_factory()
    command = getattr(panel, command_name)
    can_execute = getattr(panel, can_name)
    original_document_id = panel.document_id
    original_model = model_identity(panel)
    documents.mark_changed(
        original_document_id,
        view_id=panel.window_id,
    )

    assert can_execute()
    assert command()
    assert close.intent is not None
    assert close.intent.kind is CloseIntentKind.REPLACE_DOCUMENT
    assert close.intent.document_ids == (original_document_id,)
    assert model_identity(panel) == original_model
    assert panel.document_id == original_document_id
    assert not can_execute()

    confirmation.choose_cancel()
    assert model_identity(panel) == original_model
    assert panel.document_id == original_document_id
    assert can_execute()

    def save_current_document(*, ticket, save_as=False):
        del save_as
        documents.capture_save_revision(ticket.ticket_id)
        documents.complete_save(ticket.ticket_id, success=True)
        return True

    controller = documents.require(panel.document_id).controller
    assert controller is not None
    controller.save = save_current_document
    assert command()
    confirmation.choose_save()
    saved_replacement_model = model_identity(panel)
    assert saved_replacement_model != original_model
    assert panel.document_id != original_document_id
    assert documents.require(panel.document_id).is_dirty
    assert can_execute()

    saved_replacement_document = panel.document_id
    assert command()
    confirmation.choose_discard()
    assert model_identity(panel) != saved_replacement_model
    assert panel.document_id != saved_replacement_document
    assert documents.require(panel.document_id).is_dirty
    assert can_execute()
