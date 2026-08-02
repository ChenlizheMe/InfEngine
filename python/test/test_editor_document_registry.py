from __future__ import annotations

from Infernux.engine.interaction import (
    DocumentActionStatus,
    DocumentCapability,
    DocumentKind,
    DocumentRegistry,
)


class _Controller:
    def __init__(self, registry: DocumentRegistry, document_id: str) -> None:
        self.registry = registry
        self.document_id = document_id
        self.pending = False
        self.saved_revision = None
        self.discarded = False
        self.save_calls = 0

    def save(self, *, save_as: bool = False):
        self.save_calls += 1
        if self.pending:
            return False
        self.registry.mark_saved(self.document_id, self.saved_revision)
        return True

    def discard(self):
        self.discarded = True
        self.registry.mark_saved(self.document_id)
        return True

    def is_save_pending(self) -> bool:
        return self.pending


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
    document, controller = _document(registry, dirty=True)
    controller.saved_revision = document.revision
    registry.mark_changed(document.document_id)

    result = registry.request_save(document.document_id)

    assert result.status is DocumentActionStatus.APPLIED
    assert document.saved_revision == 1
    assert document.revision == 2
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
