from __future__ import annotations

from Infernux.engine.interaction import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    SaveTicketStatus,
)


class _Controller:
    def __init__(self, registry: DocumentRegistry, document_id: str) -> None:
        self.registry = registry
        self.document_id = document_id
        self.pending = False
        self.saved_revision = None
        self.discarded = False
        self.save_calls = 0
        self.ticket = None

    def save(self, *, ticket, save_as: bool = False):
        self.save_calls += 1
        self.ticket = ticket
        if self.pending:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        if self.saved_revision is not None:
            ticket.captured_revision = self.saved_revision
        return True

    def discard(self, *, document_id: str):
        assert document_id == self.document_id
        self.discarded = True
        self.registry.mark_saved(self.document_id)
        return True

def _document(registry: DocumentRegistry, *, dirty: bool = False):
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        document_id="particle:smoke",
        revision=1 if dirty else 0,
        saved_revision=0,
        capabilities=(
            DocumentCapability.SAVE
            | DocumentCapability.SAVE_AS
            | DocumentCapability.DISCARD
        ),
    )
    controller = _Controller(registry, document.document_id)
    registry.update_metadata(document.document_id, controller=controller)
    return document, controller


def test_document_dirty_state_is_derived_from_revisions():
    registry = DocumentRegistry()
    document, _ = _document(registry)

    registry.mark_changed(document.document_id)
    assert document.is_dirty
    assert document.revision == 1
    assert document.saved_revision == 0

    registry.mark_saved(document.document_id)
    assert not document.is_dirty
    assert document.saved_revision == document.revision


def test_clean_document_save_is_a_no_op():
    registry = DocumentRegistry()
    document, controller = _document(registry)

    result = registry.request_save(document.document_id)

    assert result.status is DocumentActionStatus.NO_OP
    assert controller.save_calls == 0


def test_saving_an_older_revision_does_not_clear_newer_edits():
    registry = DocumentRegistry()
    document, _controller = _document(registry, dirty=True)
    ticket = registry.begin_save(document.document_id)
    registry.mark_changed(document.document_id)

    registry.complete_save(ticket.ticket_id, success=True)

    assert document.saved_revision == 1
    assert document.revision == 2
    assert document.is_dirty
    assert ticket.status is SaveTicketStatus.SUCCEEDED


def test_undo_can_cross_a_save_point_without_moving_the_save_point():
    registry = DocumentRegistry()
    document, _controller = _document(registry)
    original_revision = document.revision
    edited_revision = registry.mark_changed(document.document_id)
    registry.mark_saved(document.document_id)

    registry.restore_content_revision(document.document_id, original_revision)

    assert document.revision == original_revision
    assert document.saved_revision == edited_revision
    assert document.is_dirty

    registry.restore_content_revision(document.document_id, edited_revision)
    assert not document.is_dirty


def test_edit_after_undo_allocates_a_fresh_revision_token():
    registry = DocumentRegistry()
    document, _controller = _document(registry)
    original_revision = document.revision
    abandoned_revision = registry.mark_changed(document.document_id)
    registry.mark_saved(document.document_id)
    registry.restore_content_revision(document.document_id, original_revision)

    replacement_revision = registry.mark_changed(document.document_id)

    assert replacement_revision > abandoned_revision
    assert replacement_revision != document.saved_revision
    assert document.is_dirty


def test_async_save_records_the_exact_captured_revision_after_undo():
    registry = DocumentRegistry()
    document, _controller = _document(registry)
    saved_revision = registry.mark_changed(document.document_id)
    ticket = registry.begin_save(document.document_id)
    newer_revision = registry.mark_changed(document.document_id)
    registry.restore_content_revision(document.document_id, 0)

    registry.complete_save(ticket.ticket_id, success=True)

    assert document.saved_revision == saved_revision
    assert document.revision == 0
    assert newer_revision > saved_revision
    assert document.is_dirty


def test_one_document_can_have_multiple_views_without_duplicate_dirty_entries():
    registry = DocumentRegistry()
    document, _ = _document(registry, dirty=True)

    registry.attach_view(document.document_id, "particle-left")
    registry.attach_view(document.document_id, "particle-right")

    assert registry.document_for_view("particle-left") is document
    assert registry.document_for_view("particle-right") is document
    assert registry.dirty_documents() == (document,)

    registry.detach_view("particle-left")
    assert document.view_ids == {"particle-right"}


def test_pending_save_is_explicit_and_keeps_document_dirty():
    registry = DocumentRegistry()
    document, controller = _document(registry, dirty=True)
    controller.pending = True

    result = registry.request_save(document.document_id)

    assert result.status is DocumentActionStatus.PENDING
    assert document.is_dirty
    assert registry.is_save_pending(document.document_id)
    assert controller.ticket is registry.active_save_ticket(document.document_id)


def test_document_key_deduplicates_views_of_the_same_asset(tmp_path):
    registry = DocumentRegistry()
    asset_path = tmp_path / "Smoke.particlegraph"
    key = DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(asset_path))

    first, created = registry.open_or_create(key, "Smoke", resource_path=str(asset_path))
    second, created_again = registry.open_or_create(
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(asset_path)),
        "Smoke",
        resource_path=str(asset_path),
    )

    assert created
    assert not created_again
    assert second is first
    assert registry.get_by_key(key) is first


def test_save_as_rekeys_document_atomically_without_changing_document_id(tmp_path):
    registry = DocumentRegistry()
    document, _ = _document(registry, dirty=True)
    old_key = document.key
    new_path = tmp_path / "Saved.particlegraph"
    new_key = DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(new_path))
    ticket = registry.begin_save(document.document_id, save_as=True)

    registry.complete_save(
        ticket.ticket_id,
        success=True,
        key=new_key,
        resource_path=str(new_path),
        title="Saved",
    )

    assert document.document_id == "particle:smoke"
    assert registry.get_by_key(old_key) is None
    assert registry.get_by_key(new_key) is document
    assert document.title == "Saved"
    assert not document.is_dirty


def test_save_as_key_collision_fails_without_rekeying_or_clearing_dirty(tmp_path):
    registry = DocumentRegistry()
    document, _ = _document(registry, dirty=True)
    original_key = document.key
    occupied_key = DocumentKey.resource(
        DocumentKind.PARTICLE_GRAPH,
        str(tmp_path / "Occupied.particlegraph"),
    )
    registry.create(DocumentKind.PARTICLE_GRAPH, "Occupied", key=occupied_key)
    ticket = registry.begin_save(document.document_id, save_as=True)

    registry.complete_save(ticket.ticket_id, success=True, key=occupied_key)

    assert ticket.status is SaveTicketStatus.FAILED
    assert registry.get_by_key(original_key) is document
    assert document.is_dirty


def test_discard_must_reconcile_the_authoritative_revision():
    registry = DocumentRegistry()
    document, controller = _document(registry, dirty=True)

    result = registry.request_discard(document.document_id)

    assert result.status is DocumentActionStatus.APPLIED
    assert controller.discarded
    assert not document.is_dirty


def test_legacy_panel_adapter_uses_document_registry_as_its_only_state():
    from Infernux.engine.project_context import (
        clear_panel_tracking,
        get_dirty_panel_entries,
        is_panel_dirty,
        set_panel_dirty,
    )

    registry = DocumentRegistry()
    panel_id = "particle_graph_editor"
    try:
        set_panel_dirty(panel_id, True, title="Particle Graph")

        document = registry.document_for_view(panel_id)
        assert document is not None
        assert document.document_id == f"legacy-panel:{panel_id}"
        assert document.is_dirty
        assert is_panel_dirty(panel_id)
        assert get_dirty_panel_entries()[0]["document_id"] == document.document_id

        set_panel_dirty(panel_id, False)
        assert not document.is_dirty
    finally:
        clear_panel_tracking(panel_id)


def test_timeline_panel_binds_a_real_revisioned_document():
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    registry = DocumentRegistry.instance()
    panel = AnimTimelineEditorPanel()
    document = registry.get(panel.document_id)

    assert document is not None
    assert document.kind is DocumentKind.TIMELINE
    assert document.view_ids == {panel.window_id}
    assert document.is_dirty

    previous_revision = document.revision
    panel._set_dirty(True)
    assert document.revision == previous_revision + 1

    panel._set_dirty(False)
    assert document.saved_revision == document.revision
    assert not document.is_dirty


def test_scene_file_manager_uses_document_revisions_as_its_only_dirty_state():
    from Infernux.engine.scene_manager import SceneFileManager

    registry = DocumentRegistry.instance()
    previous = SceneFileManager._instance
    try:
        manager = SceneFileManager()
        document = registry.get(manager.document_id)

        assert document is not None
        assert document.kind is DocumentKind.SCENE
        assert not document.is_dirty

        manager.mark_dirty()
        first_revision = document.revision
        manager.mark_dirty()

        assert manager.is_dirty
        assert document.revision == first_revision + 1
        manager.clear_dirty()
        assert not manager.is_dirty
        assert document.saved_revision == document.revision
    finally:
        SceneFileManager._instance = previous


def test_focused_save_uses_the_document_registry_before_panel_fallback():
    from Infernux.engine._bootstrap_wiring import BootstrapWiringMixin
    from Infernux.engine.interaction import FocusService

    registry = DocumentRegistry.instance()
    document, controller = _document(registry, dirty=True)
    registry.attach_view(document.document_id, "timeline")
    previous_focus = FocusService._instance
    focus = FocusService()
    focus.activate_panel(
        "timeline",
        view_id="timeline",
        document_id=document.document_id,
    )
    calls = []

    class _WindowManager:
        @staticmethod
        def get_window_instance(_panel_id):
            raise AssertionError("real documents must not use panel save handlers")

    class _SceneFiles:
        @staticmethod
        def save_current_scene():
            calls.append("scene")

        @staticmethod
        def save_scene_as():
            calls.append("scene_as")

    try:
        BootstrapWiringMixin._save_focused_document(_WindowManager, _SceneFiles)

        assert controller.save_calls == 1
        assert not document.is_dirty
        assert calls == []
    finally:
        FocusService._instance = previous_focus
