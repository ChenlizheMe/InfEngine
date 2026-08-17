from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
            key=DocumentKey.resource(DocumentKind.SCENE, "copy.scene"),
            resource_path="copy.scene",
            title="Copy",
        )
        self.pending_save = None
        return True


def _conflicted_document(
    registry: DocumentRegistry,
    title: str = "Smoke",
    *,
    resource_path: Path | None = None,
):
    controller = _Controller(registry)
    path = resource_path or Path(f"{title}.scene")
    document = registry.create(
        DocumentKind.SCENE,
        title,
        key=DocumentKey.resource(
            DocumentKind.SCENE,
            str(path),
        ),
        resource_path=str(path),
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


def test_scene_conflict_reload_stops_play_and_waits_for_edit_restore(
    monkeypatch,
    tmp_path,
):
    from Infernux.engine.deferred_task import DeferredTaskRunner
    from Infernux.engine.play_mode import PlayModeManager
    from Infernux.engine.scene_manager import SceneFileManager

    registry = DocumentRegistry()
    manager = SceneFileManager()
    path = tmp_path / "PlayConflict.scene"
    path.write_text("disk-version", encoding="utf-8")
    registry.update_metadata(
        manager.document_id,
        resource_path=str(path),
        controller=manager,
    )
    document = registry.require(manager.document_id)
    manager._current_scene_path = str(path)
    registry.mark_changed(document.document_id, view_id="scene_view")
    registry.mark_conflict(document.document_id)

    state = {"playing": True}
    runner = SimpleNamespace(is_busy=False)
    stop_calls = []

    def exit_play_mode():
        stop_calls.append(True)
        state["playing"] = False
        runner.is_busy = True
        return True

    monkeypatch.setattr(manager, "_is_play_mode", lambda: state["playing"])
    monkeypatch.setattr(
        DeferredTaskRunner,
        "instance",
        classmethod(lambda _cls: runner),
    )
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(
            lambda _cls: SimpleNamespace(exit_play_mode=exit_play_mode)
        ),
    )
    loaded = []
    monkeypatch.setattr(
        manager,
        "reload_from_resource",
        lambda **payload: loaded.append(
            Path(payload["resource_path"]).read_text(encoding="utf-8")
        )
        or True,
    )

    modals = ModalService()
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)
    coordinator.poll()

    assert coordinator.choose_reload()
    assert coordinator.waiting_for_reload
    assert modals.active_modal_id == ""
    assert stop_calls == [True]
    assert loaded == []
    assert manager.poll_pending_writes() == 0

    runner.is_busy = False
    assert manager.poll_pending_writes() == 1
    coordinator.poll()

    assert loaded == ["disk-version"]
    assert not coordinator.is_active
    assert document.state is DocumentState.READY
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
    assert document.resource_path == "copy.scene"
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


def test_non_scene_conflict_never_opens_the_external_change_dialog():
    registry = DocumentRegistry()
    document = registry.create(
        DocumentKind.PARTICLE_GRAPH,
        "Smoke",
        key=DocumentKey.resource(
            DocumentKind.PARTICLE_GRAPH,
            "Smoke.particlegraph",
        ),
        resource_path="Smoke.particlegraph",
        revision=1,
        saved_revision=0,
    )
    registry.mark_conflict(document.document_id)
    service = ExternalDocumentConflictService(registry)

    service.poll()

    assert service.active is None


def test_stale_conflict_revision_cannot_resolve_a_new_external_change(tmp_path):
    registry = DocumentRegistry()
    path = tmp_path / "Smoke.particlegraph"
    path.write_text("baseline", encoding="utf-8")
    document, _controller = _conflicted_document(
        registry,
        resource_path=path,
    )
    service = ExternalDocumentConflictService(registry)
    service.poll()
    conflict = service.active
    assert conflict is not None

    path.write_text("external-change", encoding="utf-8")
    registry.publish_external_resource_change(document.resource_path)
    result = service.keep_local(conflict.conflict_id)

    assert result.status is DocumentActionStatus.REJECTED
    assert document.state is DocumentState.CONFLICT


class _ConflictModalContext:
    def __init__(self, begin_results):
        self.begin_results = list(begin_results)
        self.opened = []
        self.semantic_windows = []

    def open_popup(self, popup_id):
        self.opened.append(popup_id)

    def get_main_viewport_bounds(self):
        return (0.0, 0.0, 1280.0, 720.0)

    def set_next_window_pos(self, *_args):
        return None

    def set_next_window_size(self, *_args):
        return None

    def begin_popup_modal(self, _popup_id, _flags=0):
        return self.begin_results.pop(0)

    def record_semantic_window(self, *args):
        self.semantic_windows.append(args)

    def text_wrapped(self, *_args):
        return None

    def spacing(self):
        return None

    def separator(self):
        return None

    def get_content_region_avail_height(self):
        return 400.0

    def get_cursor_pos_y(self):
        return 0.0

    def set_cursor_pos_y(self, _value):
        return None

    def get_content_region_avail_width(self):
        return 600.0

    def get_cursor_pos_x(self):
        return 0.0

    def set_cursor_pos_x(self, _value):
        return None

    def button(self, *_args, **_kwargs):
        return False

    def record_semantic_item(self, *_args):
        return None

    def begin_disabled(self, *_args):
        return None

    def end_disabled(self):
        return None

    def same_line(self):
        return None

    def end_popup(self):
        return None


def test_external_conflict_popup_reopens_after_imgui_closes_it():
    registry = DocumentRegistry()
    modals = ModalService()
    document, _controller = _conflicted_document(registry)
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)
    coordinator.poll()

    ctx = _ConflictModalContext([False, True])
    modals.render(ctx)

    # The conflict remains a domain transaction, but the invisible popup no
    # longer blocks Ctrl+S/Ctrl+Z or other editor shortcuts.
    assert coordinator.is_active
    assert modals.active_modal_id == ""
    assert modals.is_presented(coordinator.MODAL_ID) is False

    # The next overlay frame reopens the popup and restores real modal input
    # ownership. The action remains selectable through the coordinator API.
    coordinator.poll()
    modals.render(ctx)
    assert modals.is_presented(coordinator.MODAL_ID) is True
    assert modals.active_modal_id == coordinator.MODAL_ID
    assert coordinator.choose_keep_local()
    assert not coordinator.is_active


def test_external_conflict_reacquires_overlay_after_editor_lifecycle_displaces_it():
    registry = DocumentRegistry()
    modals = ModalService()
    document, _controller = _conflicted_document(registry, "PlayStopDock")
    coordinator = ExternalDocumentConflictCoordinator(registry, modals)

    coordinator.poll()
    assert coordinator.active_document_id == document.document_id
    assert modals.active_modal_id == coordinator.MODAL_ID

    # Play/Stop and dock focus restoration may rebuild the overlay stack. They
    # must not discard the domain conflict itself.
    assert modals.deactivate(coordinator.MODAL_ID)
    assert coordinator.is_active
    assert modals.active_modal_id == ""

    coordinator.poll()
    assert coordinator.is_active
    assert modals.active_modal_id == coordinator.MODAL_ID
