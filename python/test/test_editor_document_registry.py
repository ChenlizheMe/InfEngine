from __future__ import annotations

from pathlib import Path

from Infernux.engine.interaction import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKey,
    DocumentKind,
    DocumentOpenService,
    DocumentOpenStatus,
    DocumentRegistry,
    DocumentState,
    SaveTicketStatus,
)


class _Controller:
    def __init__(self, registry: DocumentRegistry, document_id: str) -> None:
        self.registry = registry
        self.document_id = document_id
        self.pending = False
        self.saved_revision = None
        self.discarded = False
        self.reloaded = False
        self.save_calls = 0
        self.ticket = None
        self.resource_path = ""

    def save(self, *, ticket, save_as: bool = False):
        del save_as
        self.save_calls += 1
        self.ticket = ticket
        if self.pending:
            return DocumentActionResult(DocumentActionStatus.PENDING)
        self.registry.capture_save_revision(ticket.ticket_id)
        if self.saved_revision is not None:
            ticket.captured_revision = self.saved_revision
        self.registry.complete_save(ticket.ticket_id, success=True)
        return True

    def discard(self, *, document_id: str):
        assert document_id == self.document_id
        self.discarded = True
        return True

    def reload_from_resource(self, *, document_id: str, resource_path: str):
        assert document_id == self.document_id
        self.resource_path = resource_path
        self.reloaded = True
        return True

    def save_to_resource(self, *, ticket, resource_path: str):
        self.resource_path = resource_path
        return self.save(ticket=ticket, save_as=True)

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


def test_external_change_conflicts_with_dirty_document_and_blocks_plain_save(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    document, controller = _document(registry, dirty=True)
    registry.rekey(
        document.document_id,
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
        resource_path=str(path),
    )

    assert registry.publish_external_resource_change(str(path)) == (
        document.document_id,
    )
    assert document.external_revision == 1
    assert document.state is DocumentState.CONFLICT
    assert not controller.discarded

    result = registry.request_save(document.document_id)
    assert result.status is DocumentActionStatus.REJECTED
    assert "outside the Editor" in result.message
    assert controller.save_calls == 0


def test_clean_external_change_reloads_and_establishes_a_new_baseline(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    document, controller = _document(registry)
    registry.rekey(
        document.document_id,
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
        resource_path=str(path),
    )
    previous_revision = document.revision

    registry.publish_external_resource_change(str(path))

    assert controller.reloaded
    assert not controller.discarded
    assert document.external_revision == 1
    assert document.revision > previous_revision
    assert document.saved_revision == document.revision
    assert document.state is DocumentState.READY
    assert not document.is_dirty


def test_external_change_during_save_rejects_stale_completion(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    document, controller = _document(registry, dirty=True)
    registry.rekey(
        document.document_id,
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
        resource_path=str(path),
    )
    controller.pending = True

    result = registry.request_save(document.document_id)
    assert result.status is DocumentActionStatus.PENDING
    ticket = registry.active_save_ticket(document.document_id)
    assert ticket is not None

    registry.publish_external_resource_change(str(path))
    completed = registry.complete_save(ticket.ticket_id, success=True)

    assert completed.status is SaveTicketStatus.FAILED
    assert document.state is DocumentState.CONFLICT
    assert document.saved_revision == 0
    assert document.is_dirty


def test_keep_local_acknowledges_conflict_before_intentional_overwrite(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    document, controller = _document(registry, dirty=True)
    registry.rekey(
        document.document_id,
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
        resource_path=str(path),
    )
    registry.publish_external_resource_change(str(path))

    resolved = registry.resolve_conflict_keep_local(document.document_id)
    saved = registry.request_save(document.document_id)

    assert resolved.status is DocumentActionStatus.APPLIED
    assert saved.status is DocumentActionStatus.APPLIED
    assert controller.save_calls == 1
    assert document.state is DocumentState.READY
    assert not document.is_dirty


def test_abandon_session_changes_skips_live_dirty_snapshot_without_reloading():
    registry = DocumentRegistry()

    class CaptureController:
        def __init__(self):
            self.capture_calls = 0
            self.discard_calls = 0

        def capture_document_restore_state(self, _document_id):
            self.capture_calls += 1
            return {"draft": "unsaved"}

        def discard(self, *, document_id):
            del document_id
            self.discard_calls += 1
            return True

    controller = CaptureController()
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Unsaved Graph",
        revision=1,
        saved_revision=0,
        capabilities=DocumentCapability.DISCARD,
        controller=controller,
    )
    registry.attach_view(document.document_id, "particle_graph_editor")

    assert len(registry.capture_session_state()["documents"]) == 1
    registry.abandon_session_changes(document.document_id)

    assert not document.is_dirty
    assert controller.discard_calls == 0
    assert registry.is_session_restore_suppressed("particle_graph_editor")
    assert not registry.has_pending_session_document("particle_graph_editor")
    assert registry.capture_session_state()["documents"] == []


def test_replace_view_document_retires_old_document_without_restore_capture():
    registry = DocumentRegistry()

    class CaptureProbe:
        def __init__(self):
            self.capture_count = 0

        def capture_document_restore_state(self, _document_id):
            self.capture_count += 1
            raise AssertionError("explicit replacement must not capture the old document")

    probe = CaptureProbe()
    previous = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Previous",
        controller=probe,
    )
    replacement = registry.create(DocumentKind.PARTICLE_GRAPH, "Replacement")
    registry.attach_view(previous.document_id, "particle_graph_editor")

    assert registry.replace_view_document(
        replacement.document_id,
        "particle_graph_editor",
    )
    assert registry.document_for_view("particle_graph_editor") is replacement
    assert registry.get(previous.document_id) is None
    assert registry.locate(previous.document_id) is None
    assert probe.capture_count == 0


def test_document_open_service_resolves_live_document_without_adapter(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path)),
        resource_path=str(path),
    )
    service = DocumentOpenService(registry)

    result = service.resolve_or_open(registry.locate(document.document_id))

    assert result.status is DocumentOpenStatus.READY
    assert result.document is document


def test_document_open_service_polls_idempotent_adapter_until_registered(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    key = DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(path))
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=key,
        resource_path=str(path),
    )
    locator = registry.locate(document.document_id)
    assert locator is not None
    registry.unregister(document.document_id)
    service = DocumentOpenService(registry)
    calls = []

    def _open(_locator):
        calls.append(1)
        if len(calls) == 1:
            return DocumentOpenStatus.PENDING
        registry.create(
            DocumentKind.PARTICLE_GRAPH,
            "Smoke",
            key=key,
            resource_path=str(path),
        )
        return DocumentOpenStatus.READY

    service.register(DocumentKind.PARTICLE_GRAPH, _open)

    assert service.resolve_or_open(locator).status is DocumentOpenStatus.PENDING
    result = service.resolve_or_open(locator)

    assert result.status is DocumentOpenStatus.READY
    assert result.document is not None
    assert result.document.stable_id == locator.stable_id
    assert len(calls) == 2


def test_document_open_service_fails_closed_when_adapter_does_not_register(tmp_path):
    registry = DocumentRegistry()
    locator = registry.locate(
        registry.create(
            DocumentKind.TIMELINE,
            "Timeline",
            key=DocumentKey.resource(
                DocumentKind.TIMELINE,
                str(tmp_path / "Timeline.timeline"),
            ),
        ).document_id
    )
    assert locator is not None
    registry.unregister(registry.resolve_locator(locator).document_id)
    service = DocumentOpenService(registry)
    service.register(DocumentKind.TIMELINE, lambda _locator: DocumentOpenStatus.READY)

    result = service.resolve_or_open(locator)

    assert result.status is DocumentOpenStatus.FAILED
    assert "without registering" in result.message


def test_document_open_service_opens_resource_through_typed_adapter(tmp_path):
    registry = DocumentRegistry()
    service = DocumentOpenService(registry)
    path = tmp_path / "Smoke.particlegraph"
    path.write_text("{}", encoding="utf-8")
    calls = []

    def _open(locator):
        calls.append(locator)
        if registry.resolve_locator(locator) is None:
            registry.create(
                DocumentKind.PARTICLE_GRAPH,
                "Smoke",
                key=locator.key_hint,
                resource_path=locator.resource_path,
            )
        return True

    service.register(DocumentKind.PARTICLE_GRAPH, _open)
    result = service.open_resource(
        DocumentKind.PARTICLE_GRAPH,
        str(path),
        guid="particle-guid",
        title="Smoke",
    )

    assert result.status is DocumentOpenStatus.READY
    assert result.document is not None
    assert result.document.key == DocumentKey.asset(
        DocumentKind.PARTICLE_GRAPH, "particle-guid"
    )
    assert len(calls) == 1

    second = service.open_resource(
        DocumentKind.PARTICLE_GRAPH,
        str(path),
        guid="particle-guid",
        title="Smoke",
    )
    assert second.status is DocumentOpenStatus.READY
    assert second.document is result.document
    assert len(calls) == 2


def test_document_open_service_rejects_unsupported_dormant_kind(tmp_path):
    registry = DocumentRegistry()
    document = registry.create(
        DocumentKind.SCENE,
        "Level",
        key=DocumentKey.resource(DocumentKind.SCENE, str(tmp_path / "Level.scene")),
    )
    locator = registry.locate(document.document_id)
    registry.unregister(document.document_id)

    result = DocumentOpenService(registry).resolve_or_open(locator)

    assert result.status is DocumentOpenStatus.FAILED
    assert "scene" in result.message


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


def test_loaded_baseline_is_not_misreported_as_a_save_completion():
    registry = DocumentRegistry()
    document, _ = _document(registry, dirty=True)
    previous_revision = document.revision

    loaded_revision = registry.establish_loaded_baseline(document.document_id)

    assert loaded_revision > previous_revision
    assert document.revision == loaded_revision
    assert document.saved_revision == loaded_revision
    assert document.is_dirty is False
    assert registry.active_save_ticket(document.document_id) is None


def test_clean_document_save_is_a_no_op():
    registry = DocumentRegistry()
    document, controller = _document(registry)

    result = registry.request_save(document.document_id)

    assert result.status is DocumentActionStatus.NO_OP
    assert controller.save_calls == 0


def test_explicit_resource_save_uses_the_controller_and_same_save_ticket_contract():
    registry = DocumentRegistry()
    document, controller = _document(registry, dirty=True)

    result = registry.request_save_to_resource(
        document.document_id,
        "Assets/Automation.scene",
    )

    assert result.status is DocumentActionStatus.APPLIED
    assert controller.resource_path == "Assets/Automation.scene"
    assert controller.save_calls == 1
    assert controller.ticket.save_as is True
    assert document.is_dirty is False


def test_save_controller_must_explicitly_complete_its_ticket():
    registry = DocumentRegistry()

    class NonCompliantController:
        @staticmethod
        def save(*, ticket, save_as=False):
            del ticket, save_as
            return True

    document = registry.create(
        DocumentKind.GENERIC,
        "Unsafe",
        revision=1,
        saved_revision=0,
        capabilities=DocumentCapability.SAVE,
        controller=NonCompliantController(),
    )

    result = registry.request_save(document.document_id)

    assert result.status is DocumentActionStatus.FAILED
    assert "without completing" in result.message
    assert document.saved_revision == 0
    assert document.is_dirty


def test_deferred_save_captures_edits_committed_later_in_the_ui_frame():
    registry = DocumentRegistry()
    document, controller = _document(registry, dirty=True)

    result = registry.defer_save(document.document_id)
    final_frame_revision = registry.mark_changed(document.document_id)

    assert result.status is DocumentActionStatus.PENDING
    assert controller.save_calls == 0
    assert registry.process_deferred_saves()[0].status is DocumentActionStatus.APPLIED
    assert controller.save_calls == 1
    assert document.saved_revision == final_frame_revision
    assert document.is_dirty is False


def test_deferred_save_preserves_edits_made_after_an_async_save_starts():
    registry = DocumentRegistry()
    document, controller = _document(registry, dirty=True)
    controller.pending = True

    registry.defer_save(document.document_id)
    captured_revision = registry.mark_changed(document.document_id)
    result = registry.process_deferred_saves()[0]
    newer_revision = registry.mark_changed(document.document_id)
    registry.complete_save(controller.ticket.ticket_id, success=True)

    assert result.status is DocumentActionStatus.PENDING
    assert document.saved_revision == captured_revision
    assert document.revision == newer_revision
    assert document.is_dirty is True


def test_async_save_dialog_commits_the_revision_actually_serialized():
    registry = DocumentRegistry()
    document, _controller = _document(registry, dirty=True)
    ticket = registry.begin_save(document.document_id, save_as=True)
    serialized_revision = registry.mark_changed(document.document_id)

    assert ticket.captured_revision != serialized_revision
    assert registry.capture_save_revision(ticket.ticket_id) == serialized_revision
    registry.complete_save(ticket.ticket_id, success=True)

    assert document.saved_revision == serialized_revision
    assert document.revision == serialized_revision
    assert document.is_dirty is False


def test_save_ticket_captures_the_document_resource_path_as_save_authority():
    registry = DocumentRegistry()
    document, _controller = _document(registry, dirty=True)
    registry.update_metadata(
        document.document_id,
        resource_path="C:/Project/Assets/Smoke.particlegraph",
    )

    ticket = registry.begin_save(document.document_id)
    registry.update_metadata(
        document.document_id,
        resource_path="C:/Project/Assets/Moved.particlegraph",
    )

    assert ticket.resource_path == "C:/Project/Assets/Smoke.particlegraph"


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


def test_save_publication_absorbs_only_matching_content_bookkeeping_revision():
    registry = DocumentRegistry()
    document, _controller = _document(registry, dirty=True)
    ticket = registry.begin_save(document.document_id)
    registry.capture_save_revision(ticket.ticket_id, content_token="serialized-a")
    publication_revision = registry.mark_changed(document.document_id)

    registry.complete_save(
        ticket.ticket_id,
        success=True,
        content_token="serialized-a",
    )

    assert document.revision == publication_revision
    assert document.saved_revision == publication_revision
    assert document.is_dirty is False


def test_save_publication_preserves_newer_content_when_token_changed():
    registry = DocumentRegistry()
    document, _controller = _document(registry, dirty=True)
    ticket = registry.begin_save(document.document_id)
    registry.capture_save_revision(ticket.ticket_id, content_token="serialized-a")
    newer_revision = registry.mark_changed(document.document_id)

    registry.complete_save(
        ticket.ticket_id,
        success=True,
        content_token="serialized-b",
    )

    assert document.saved_revision == 1
    assert document.revision == newer_revision
    assert document.is_dirty is True


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
    assert document.revision == 0
    assert document.saved_revision == 0


def test_discard_is_rejected_while_the_document_has_an_active_save_ticket():
    registry = DocumentRegistry()
    document, controller = _document(registry, dirty=True)
    controller.pending = True

    assert registry.request_save(document.document_id).status is DocumentActionStatus.PENDING
    result = registry.request_discard(document.document_id)

    assert result.status is DocumentActionStatus.REJECTED
    assert "saving" in result.message
    assert controller.discarded is False
    assert document.is_dirty is True


def test_close_view_retires_a_document_after_its_last_view_closes():
    registry = DocumentRegistry()
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Particle Graph",
        revision=1,
        saved_revision=0,
    )
    registry.attach_view(document.document_id, "particle_graph_editor")
    locator = registry.locate(document.document_id)

    closed_id = registry.close_view("particle_graph_editor")

    assert closed_id == document.document_id
    assert registry.document_for_view("particle_graph_editor") is None
    assert registry.get(document.document_id) is None
    assert registry.resolve_locator(locator) is None


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
    registry.mark_changed(document.document_id, view_id=panel.window_id)
    assert document.revision == previous_revision + 1

    registry.mark_saved(document.document_id)
    assert document.saved_revision == document.revision
    assert not document.is_dirty
    assert not hasattr(panel, "_set_dirty")


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
        saved_revision = document.saved_revision
        manager.mark_dirty()

        assert manager.is_dirty
        assert document.revision == first_revision + 1
        registry.restore_saved_revision(document.document_id)
        assert not manager.is_dirty
        assert document.revision == saved_revision
        assert document.saved_revision == saved_revision
    finally:
        SceneFileManager._instance = previous


def test_scene_undo_replays_document_revisions_without_owning_the_save_point():
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.undo import SetPropertyCommand, UndoManager

    class Target:
        value = 0

    registry = DocumentRegistry.instance()
    previous_scene = SceneFileManager._instance
    previous_undo = UndoManager.instance()
    try:
        scene_files = SceneFileManager()
        manager = UndoManager()
        document = registry.require(scene_files.document_id)
        target = Target()
        saved_revision = document.saved_revision

        assert manager.execute(SetPropertyCommand(target, "value", 0, 1))
        first_revision = document.revision
        assert first_revision > saved_revision
        assert document.is_dirty

        manager.undo()
        assert target.value == 0
        assert document.revision == saved_revision
        assert document.saved_revision == saved_revision
        assert not document.is_dirty

        manager.redo()
        assert target.value == 1
        assert document.revision == first_revision
        assert document.saved_revision == saved_revision
        assert document.is_dirty
    finally:
        UndoManager._instance = previous_undo
        SceneFileManager._instance = previous_scene


def test_scene_undo_crosses_a_registry_save_point_without_moving_it():
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.undo import SetPropertyCommand, UndoManager

    class Target:
        value = 0

    registry = DocumentRegistry.instance()
    previous_scene = SceneFileManager._instance
    previous_undo = UndoManager.instance()
    try:
        scene_files = SceneFileManager()
        manager = UndoManager()
        document = registry.require(scene_files.document_id)
        target = Target()

        first = SetPropertyCommand(target, "value", 0, 1)
        first.timestamp = 0.0
        assert manager.execute(first)
        saved_revision = document.revision
        registry.mark_saved(document.document_id, saved_revision)

        second = SetPropertyCommand(target, "value", 1, 2)
        second.timestamp = 10.0
        assert manager.execute(second)
        second_revision = document.revision
        assert second_revision > saved_revision

        manager.undo()
        assert target.value == 1
        assert document.revision == saved_revision
        assert document.saved_revision == saved_revision
        assert not document.is_dirty

        manager.undo()
        assert target.value == 0
        assert document.revision != saved_revision
        assert document.saved_revision == saved_revision
        assert document.is_dirty

        manager.redo()
        assert target.value == 1
        assert document.revision == saved_revision
        assert document.saved_revision == saved_revision
        assert not document.is_dirty
    finally:
        UndoManager._instance = previous_undo
        SceneFileManager._instance = previous_scene


def test_scene_command_merge_cannot_cross_completed_or_pending_save_points():
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.undo import SetPropertyCommand, UndoManager

    class Target:
        value = 0

    registry = DocumentRegistry.instance()
    previous_scene = SceneFileManager._instance
    previous_undo = UndoManager.instance()
    try:
        scene_files = SceneFileManager()
        manager = UndoManager()
        document = registry.require(scene_files.document_id)
        target = Target()

        first = SetPropertyCommand(target, "value", 0, 1)
        first.timestamp = 1.0
        assert manager.execute(first)
        first_revision = document.revision

        pending = registry.begin_save(document.document_id)
        second = SetPropertyCommand(target, "value", 1, 2)
        second.timestamp = 1.1
        assert manager.execute(second)
        assert len(manager.action_journal.applied_entries()) == 2
        registry.complete_save(pending.ticket_id, success=True)
        assert document.saved_revision == first_revision
        assert document.is_dirty

        third = SetPropertyCommand(target, "value", 2, 3)
        third.timestamp = 2.0
        assert manager.execute(third)
        saved = registry.begin_save(document.document_id)
        registry.complete_save(saved.ticket_id, success=True)
        saved_revision = document.saved_revision

        fourth = SetPropertyCommand(target, "value", 3, 4)
        fourth.timestamp = 2.1
        assert manager.execute(fourth)
        assert len(manager.action_journal.applied_entries()) == 4
        assert document.saved_revision == saved_revision
        assert document.is_dirty
    finally:
        UndoManager._instance = previous_undo
        SceneFileManager._instance = previous_scene


def test_failed_scene_save_never_moves_the_registry_save_point():
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.undo import SetPropertyCommand, UndoManager

    class Target:
        value = 0

    registry = DocumentRegistry.instance()
    previous_scene = SceneFileManager._instance
    previous_undo = UndoManager.instance()
    try:
        scene_files = SceneFileManager()
        manager = UndoManager()
        document = registry.require(scene_files.document_id)
        target = Target()
        saved_revision = document.saved_revision

        assert manager.execute(SetPropertyCommand(target, "value", 0, 1))
        ticket = registry.begin_save(document.document_id)
        registry.complete_save(ticket.ticket_id, success=False, message="disk full")

        assert document.saved_revision == saved_revision
        assert document.is_dirty
        manager.undo()
        assert document.revision == saved_revision
        assert document.saved_revision == saved_revision
        assert not document.is_dirty
    finally:
        UndoManager._instance = previous_undo
        SceneFileManager._instance = previous_scene


def test_focused_save_uses_the_document_registry_without_panel_fallback():
    from Infernux.engine.interaction import EditorSaveService, FocusService

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
    previous_saving = EditorSaveService._instance
    saving = EditorSaveService(registry)

    try:
        result = saving.save_focused()

        assert result.accepted
        assert result.document_id == document.document_id
        assert controller.save_calls == 0
        registry.process_deferred_saves()
        assert controller.save_calls == 1
        assert not document.is_dirty
    finally:
        EditorSaveService._instance = previous_saving
        FocusService._instance = previous_focus


def test_document_locator_resolves_a_reopened_registry_entry(tmp_path):
    registry = DocumentRegistry()
    asset_path = tmp_path / "Smoke.particlegraph"
    key = DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(asset_path))
    first = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        document_id="first-live-entry",
        key=key,
        resource_path=str(asset_path),
    )

    locator = registry.locate(first.document_id)
    registry.unregister(first.document_id)
    replacement = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        document_id="replacement-live-entry",
        key=key,
        resource_path=str(asset_path),
    )

    assert locator is not None
    assert registry.resolve_locator(locator) is replacement
    assert locator.resource_path == str(asset_path)


def test_document_locator_survives_rekey_close_and_reopen(tmp_path):
    registry = DocumentRegistry()
    old_path = tmp_path / "Draft.particlegraph"
    new_path = tmp_path / "Smoke.particlegraph"
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Draft",
        key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(old_path)),
        resource_path=str(old_path),
    )
    old_locator = registry.locate(document.document_id)
    registry.rekey(
        document.document_id,
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(new_path)),
        resource_path=str(new_path),
    )
    registry.unregister(document.document_id)

    reopened = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(new_path)),
        resource_path=str(new_path),
    )

    assert old_locator is not None
    assert reopened.stable_id == old_locator.stable_id
    assert registry.resolve_locator(old_locator) is reopened


def test_document_stable_identity_survives_path_to_guid_promotion(tmp_path):
    registry = DocumentRegistry()
    asset_path = tmp_path / "Walk.animclip2d"
    path_key_hint = DocumentKey.resource(
        DocumentKind.ANIMATION_CLIP,
        str(asset_path),
    )
    first = registry.create(
        DocumentKind.ANIMATION_CLIP,
        "Walk",
        key=path_key_hint,
        resource_path=str(asset_path),
    )
    locator = registry.locate(first.document_id)
    registry.unregister(first.document_id)

    reopened = registry.create(
        DocumentKind.ANIMATION_CLIP,
        "Walk",
        key=DocumentKey.asset(DocumentKind.ANIMATION_CLIP, "asset-guid"),
        resource_path=str(asset_path),
    )

    assert locator is not None
    assert reopened.stable_id == locator.stable_id
    assert registry.resolve_locator(locator) is reopened


def test_live_document_is_rekeyed_when_asset_guid_becomes_available(tmp_path):
    registry = DocumentRegistry()
    asset_path = tmp_path / "Walk.animclip2d"
    first, _ = registry.open_or_create(
        DocumentKey.resource(DocumentKind.ANIMATION_CLIP, str(asset_path)),
        "Walk",
        resource_path=str(asset_path),
    )
    guid_key = DocumentKey.asset(DocumentKind.ANIMATION_CLIP, "asset-guid")

    reopened, created = registry.open_or_create(
        guid_key,
        "Walk",
        resource_path=str(asset_path),
    )

    assert created is False
    assert reopened is first
    assert reopened.key == guid_key


def test_dormant_document_is_rekeyed_without_losing_command_identity(tmp_path):
    registry = DocumentRegistry()
    asset_path = tmp_path / "Walk.animclip2d"
    first, _ = registry.open_or_create(
        DocumentKey.resource(DocumentKind.ANIMATION_CLIP, str(asset_path)),
        "Walk",
        resource_path=str(asset_path),
    )
    original_id = first.document_id
    registry.unregister(original_id)

    guid_key = DocumentKey.asset(DocumentKind.ANIMATION_CLIP, "asset-guid")
    reopened, created = registry.open_or_create(
        guid_key,
        "Walk",
        resource_path=str(asset_path),
    )

    assert created is False
    assert reopened is first
    assert reopened.document_id == original_id
    assert reopened.key == guid_key


def test_clear_removes_dormant_document_records():
    registry = DocumentRegistry()
    document = registry.create(DocumentKind.TIMELINE, "Timeline")
    locator = registry.locate(document.document_id)
    assert locator is not None
    registry.unregister(document.document_id)

    registry.clear()

    assert registry.dormant_restore_state(locator) is None
    assert registry.restore_dormant(locator) is None


def test_interaction_context_captures_active_document_locator(tmp_path):
    from Infernux.engine.interaction import EditorInteractionCore

    core = EditorInteractionCore()
    asset_path = tmp_path / "Smoke.particlegraph"
    document = core.documents.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(asset_path)),
        resource_path=str(asset_path),
    )
    core.focus.activate_panel(
        "particle_graph_editor",
        view_id="particle_graph_editor",
        document_id=document.document_id,
        record_history=False,
    )

    context = core.capture_context()

    assert context.document is not None
    assert context.document.key == document.key
    assert context.document.resource_path == str(asset_path)


def test_dormant_document_restores_original_identity_revision_and_draft():
    class _Controller:
        def __init__(self, value):
            self.value = value

        def capture_document_restore_state(self, _document_id):
            return {"value": self.value}

    registry = DocumentRegistry()
    controller = _Controller(17)
    document = registry.create(
        DocumentKind.TIMELINE,
        "Timeline",
        document_id="timeline-session",
        key=DocumentKey.session(DocumentKind.TIMELINE, "timeline-session-key"),
        revision=4,
        saved_revision=1,
        controller=controller,
    )
    locator = registry.locate(document.document_id)
    assert locator is not None

    registry.unregister(document.document_id)
    controller.value = 99

    assert registry.dormant_restore_state(locator) == {"value": 17}
    restored = registry.restore_dormant(locator, controller=controller)
    assert restored is document
    assert restored.document_id == "timeline-session"
    assert restored.revision == 4
    assert restored.saved_revision == 1
    assert restored.controller is controller


def test_dormant_capture_observes_the_still_registered_document():
    registry = DocumentRegistry()

    class _Controller:
        def capture_document_restore_state(self, document_id):
            live = registry.require(document_id)
            return {
                "resource_path": live.resource_path,
                "revision": live.revision,
            }

    document = registry.create(
        DocumentKind.TIMELINE,
        "Timeline",
        resource_path="Assets/Timeline.animtimeline",
        revision=3,
        saved_revision=1,
        controller=_Controller(),
    )
    locator = registry.locate(document.document_id)

    assert registry.unregister(document.document_id)
    assert registry.dormant_restore_state(locator) == {
        "resource_path": "Assets/Timeline.animtimeline",
        "revision": 3,
    }


def test_restore_capture_failure_does_not_leave_registry_half_closed():
    class _Controller:
        @staticmethod
        def capture_document_restore_state(_document_id):
            raise RuntimeError("capture failed")

    registry = DocumentRegistry()
    document = registry.create(
        DocumentKind.TIMELINE,
        "Timeline",
        controller=_Controller(),
    )
    locator = registry.locate(document.document_id)
    assert locator is not None

    assert registry.unregister(document.document_id)
    assert registry.get(document.document_id) is None
    assert registry.dormant_restore_state(locator) is None
    assert registry.restore_dormant(locator) is document


def test_document_registry_is_the_only_scene_save_point_authority():
    package_root = Path(__file__).resolve().parents[1] / "Infernux" / "engine"
    forbidden_tokens = (
        "mark_save_point",
        "save_signature",
        "scene_base_dirty",
        "set_scene_dirty_baseline",
        "dirty_signature",
    )
    authoritative_sources = (
        package_root / "undo" / "_manager.py",
        package_root / "interaction" / "action_journal.py",
        package_root / "interaction" / "action_journal.pyi",
        package_root / "scene_manager.py",
    )

    for source_path in authoritative_sources:
        source = source_path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in source, f"{source_path.name} restored legacy {token}"

    scene_source = (package_root / "scene_manager.py").read_text(encoding="utf-8")
    assert "def ensure_dirty(" not in scene_source
    assert "def clear_dirty(" not in scene_source
    assert "@_dirty.setter" not in scene_source
    assert "self._dirty =" not in scene_source


def test_authoring_panels_do_not_restore_private_dirty_or_open_authority():
    package_root = Path(__file__).resolve().parents[1] / "Infernux" / "engine"
    panel_sources = (
        package_root / "ui" / "particle_graph_editor_panel.py",
        package_root / "ui" / "animfsm_editor_panel.py",
        package_root / "ui" / "animtimeline_editor_panel.py",
        package_root / "ui" / "animclip2d_editor_panel.py",
    )
    forbidden_tokens = (
        "self._dirty",
        "_open_particlegraph",
        "_open_animfsm",
        "_open_timeline_immediate",
        "_open_animclip",
    )

    for source_path in panel_sources:
        source = source_path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in source, f"{source_path.name} restored legacy {token}"


def test_scene_document_dirty_ownership_is_scoped_per_authoring_view():
    registry = DocumentRegistry()
    document = registry.create(DocumentKind.SCENE, "Main")
    for view_id in ("scene_view", "game_view", "ui_editor"):
        registry.attach_view(document.document_id, view_id)

    ui_revision = registry.mark_changed(
        document.document_id,
        view_id="ui_editor",
    )

    assert document.is_dirty_for_view("ui_editor")
    assert not document.is_dirty_for_view("scene_view")
    assert not document.is_dirty_for_view("game_view")

    scene_revision = registry.mark_changed(
        document.document_id,
        view_id="scene_view",
    )
    assert document.is_dirty_for_view("ui_editor")
    assert document.is_dirty_for_view("scene_view")
    assert not document.is_dirty_for_view("game_view")

    registry.restore_content_revision(document.document_id, ui_revision)
    assert document.is_dirty_for_view("ui_editor")
    assert not document.is_dirty_for_view("scene_view")

    registry.restore_content_revision(document.document_id, scene_revision)
    registry.mark_saved(document.document_id)
    assert not document.is_dirty
    assert not document.is_dirty_for_view("ui_editor")
    assert not document.is_dirty_for_view("scene_view")


def test_scene_dirty_view_ownership_survives_session_restore():
    class SessionController:
        @staticmethod
        def capture_document_restore_state(_document_id):
            return {"scene": "snapshot"}

    controller = SessionController()
    source = DocumentRegistry()
    document = source.create(
        DocumentKind.SCENE,
        "Main",
        controller=controller,
    )
    for view_id in ("scene_view", "game_view", "ui_editor"):
        source.attach_view(document.document_id, view_id)
    source.mark_changed(document.document_id, view_id="ui_editor")
    state = source.capture_session_state()

    restored_registry = DocumentRegistry()
    assert restored_registry.queue_session_restore(state) == 1
    restored, _ = restored_registry.claim_session_document(
        "scene_view",
        controller=controller,
    )
    restored_registry.claim_session_document("game_view", controller=controller)
    restored_registry.claim_session_document("ui_editor", controller=controller)

    assert restored.is_dirty_for_view("ui_editor")
    assert not restored.is_dirty_for_view("scene_view")
    assert not restored.is_dirty_for_view("game_view")


def test_pending_session_prunes_closed_views_and_drops_orphan_drafts():
    class SessionController:
        @staticmethod
        def capture_document_restore_state(_document_id):
            return {"draft": True}

    source = DocumentRegistry()
    shared = source.create(
        DocumentKind.SCENE,
        "Shared",
        controller=SessionController(),
    )
    source.attach_view(shared.document_id, "scene_view")
    source.attach_view(shared.document_id, "ui_editor")
    source.mark_changed(shared.document_id, view_id="ui_editor")
    orphan = source.create(
        DocumentKind.PARTICLE_GRAPH,
        "Orphan",
        controller=SessionController(),
    )
    source.attach_view(orphan.document_id, "particle_graph_editor")
    source.mark_changed(orphan.document_id, view_id="particle_graph_editor")

    restored = DocumentRegistry()
    assert restored.queue_session_restore(source.capture_session_state()) == 2
    removed = restored.prune_pending_session_views({"scene_view"})

    assert removed == ("particle_graph_editor", "ui_editor")
    assert restored.pending_session_view_ids() == ("scene_view",)
    assert restored.is_session_restore_suppressed("ui_editor")
    assert restored.is_session_restore_suppressed("particle_graph_editor")
    session = restored.capture_session_state()
    assert len(session["documents"]) == 1
    record = session["documents"][0]
    assert record["view_ids"] == ["scene_view"]
    assert record["dirty_view_ids"] == ["scene_view"]
