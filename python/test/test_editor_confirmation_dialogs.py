"""Editor-owned dirty-resource and asset-deletion confirmation contracts."""

from __future__ import annotations

import inspect
import importlib
from pathlib import Path

import pytest

from Infernux.engine.interaction import (
    CloseCoordinator,
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentKind,
    DocumentRegistry,
    ModalService,
)
from Infernux.engine.ui.dirty_panel_confirmation import (
    DirtyPanelConfirmationCoordinator,
)
from Infernux.engine.ui.closable_panel import ClosablePanel


@pytest.fixture(autouse=True)
def _isolate_dirty_confirmation_singleton():
    from Infernux.engine.interaction import EditorInteractionCore

    coordinator_type = _current_confirmation_type()
    previous = coordinator_type._instance
    previous_core = EditorInteractionCore._instance
    coordinator_type._instance = None
    EditorInteractionCore._instance = None
    try:
        yield
    finally:
        coordinator_type._instance = previous
        EditorInteractionCore._instance = previous_core


def _current_confirmation_type():
    module = importlib.import_module(
        "Infernux.engine.ui.dirty_panel_confirmation"
    )
    return module.DirtyPanelConfirmationCoordinator


class _DocumentController:
    def __init__(self, save=None, save_pending=None, discard=None) -> None:
        self._save = save
        self._save_pending = save_pending
        self._discard = discard

    def save(self, *, ticket, save_as=False):
        del save_as
        if not callable(self._save):
            return False
        result = self._save()
        if callable(self._save_pending) and self._save_pending():
            return DocumentActionResult(DocumentActionStatus.PENDING)
        if result:
            registry = DocumentRegistry.instance()
            registry.capture_save_revision(ticket.ticket_id)
            registry.complete_save(ticket.ticket_id, success=True)
        return result

    def poll_save(self, _ticket):
        if callable(self._save_pending) and self._save_pending():
            return None
        return False

    def discard(self, *, document_id):
        del document_id
        if not callable(self._discard):
            return False
        return self._discard()


def _open_dirty_document(
    panel_id: str,
    *,
    title: str,
    save=None,
    save_pending=None,
    discard=None,
):
    registry = DocumentRegistry.instance()
    registry.close_view(panel_id)
    capabilities = DocumentCapability.NONE
    if callable(save):
        capabilities |= DocumentCapability.SAVE
    if callable(discard):
        capabilities |= DocumentCapability.DISCARD
    document = registry.create(
        DocumentKind.GENERIC,
        title,
        revision=1,
        saved_revision=0,
        capabilities=capabilities,
        controller=_DocumentController(save, save_pending, discard),
    )
    registry.attach_view(document.document_id, panel_id)
    return document


def _close_document_view(panel_id: str) -> None:
    DocumentRegistry.instance().close_view(panel_id)


def _dirty_confirmation() -> DirtyPanelConfirmationCoordinator:
    return _current_confirmation_type()(
        CloseCoordinator(DocumentRegistry.instance()),
        ModalService(),
    )


class _SemanticContext:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.semantics: list[str] = []
        self.buttons: dict[str, object] = {}
        self.window_positions: list[tuple[float, float, int, float, float]] = []

    def open_popup(self, popup_id: str) -> None:
        self.opened.append(popup_id)

    @staticmethod
    def get_dpi_scale() -> float:
        return 1.0

    @staticmethod
    def get_main_viewport_bounds():
        return 100.0, 50.0, 1200.0, 800.0

    def set_next_window_pos(self, x, y, condition, pivot_x, pivot_y) -> None:
        self.window_positions.append((x, y, condition, pivot_x, pivot_y))

    @staticmethod
    def begin_popup_modal(_popup_id: str, _flags: int) -> bool:
        return True

    def record_semantic_window(self, _kind, _label, semantic_id) -> None:
        self.semantics.append(semantic_id)

    @staticmethod
    def label(_value: str) -> None:
        pass

    @staticmethod
    def spacing() -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def text_wrapped(_value: str) -> None:
        pass

    def button(self, label: str, callback, width: float = 0.0, height: float = 0.0) -> None:
        self.buttons[label] = callback

    def record_semantic_item(self, _kind, _label, _enabled, semantic_id) -> None:
        self.semantics.append(semantic_id)

    @staticmethod
    def same_line() -> None:
        pass

    @staticmethod
    def end_popup() -> None:
        pass

    @staticmethod
    def close_current_popup() -> None:
        pass


def test_exit_confirmation_saves_panels_sequentially():
    first = "dirty_test_first"
    second = "dirty_test_second"
    completed: list[str] = []

    def save_first() -> bool:
        return True

    def save_second() -> bool:
        return True

    _open_dirty_document(first, title="First", save=save_first)
    _open_dirty_document(second, title="Second", save=save_second)
    coordinator = _dirty_confirmation()
    try:
        assert coordinator.request_exit(lambda: completed.append("done"), lambda: None)
        assert coordinator.active_panel_id == first

        coordinator.choose_save()
        assert coordinator.active_panel_id == second
        coordinator.choose_save()

        assert completed == ["done"]
        assert coordinator.is_active is False
    finally:
        _close_document_view(first)
        _close_document_view(second)


def test_exit_prompts_once_for_a_document_with_two_views():
    from Infernux.engine.interaction import (
        DocumentCapability,
        DocumentKind,
        DocumentRegistry,
    )

    registry = DocumentRegistry.instance()

    class _Controller:
        calls = 0

        def save(self, *, ticket, save_as=False):
            del save_as
            self.calls += 1
            registry.capture_save_revision(ticket.ticket_id)
            registry.complete_save(ticket.ticket_id, success=True)
            return True

        @staticmethod
        def discard():
            return False

    controller = _Controller()
    document = registry.create(
        DocumentKind.TIMELINE,
        "Shared Timeline",
        document_id="shared-document",
        revision=1,
        saved_revision=0,
        capabilities=DocumentCapability.SAVE,
        controller=controller,
    )
    registry.attach_view(document.document_id, "timeline-left")
    registry.attach_view(document.document_id, "timeline-right")
    completed = []
    coordinator = _dirty_confirmation()

    coordinator.request_exit(lambda: completed.append(True), lambda: None)
    assert coordinator.active_document_id == document.document_id
    coordinator.choose_save()

    assert controller.calls == 1
    assert completed == [True]
    assert not coordinator.is_active


def test_async_save_as_cancel_reopens_confirmation_without_cancelling_exit():
    panel_id = "dirty_test_async"
    pending = False
    cancelled: list[str] = []

    def begin_save_as() -> bool:
        nonlocal pending
        pending = True
        return False

    _open_dirty_document(
        panel_id,
        title="Async",
        save=begin_save_as,
        save_pending=lambda: pending,
    )
    coordinator = _dirty_confirmation()
    try:
        coordinator.request_exit(lambda: None, lambda: cancelled.append("cancel"))
        coordinator.choose_save()
        assert coordinator.waiting_for_save is True

        pending = False
        ctx = _SemanticContext()
        coordinator.render(ctx)

        assert coordinator.is_active is True
        assert coordinator.waiting_for_save is False
        assert "editor.dirty_panel.dialog" in ctx.semantics
        assert "editor.dirty_panel.save" in ctx.semantics
        assert "editor.dirty_panel.discard" in ctx.semantics
        assert "editor.dirty_panel.cancel" in ctx.semantics
        assert ctx.window_positions == [(700.0, 450.0, 1, 0.5, 0.5)]
        assert cancelled == []

        coordinator.choose_cancel()
        assert cancelled == ["cancel"]
    finally:
        _close_document_view(panel_id)


def test_panel_discard_runs_panel_handler_before_approving_close():
    panel_id = "dirty_test_discard"
    approved: list[str] = []
    discarded: list[str] = []

    def discard() -> None:
        discarded.append(panel_id)

    _open_dirty_document(panel_id, title="Discard", discard=discard)
    coordinator = _dirty_confirmation()
    try:
        assert coordinator.request_panel_close(panel_id, lambda: approved.append(panel_id))
        coordinator.choose_discard()

        assert discarded == [panel_id]
        assert approved == [panel_id]
        assert coordinator.is_active is False
    finally:
        _close_document_view(panel_id)


def test_exit_discard_abandons_panel_draft_without_reloading_it():
    panel_id = "dirty_test_exit_discard"
    completed: list[str] = []
    discarded: list[str] = []

    document = _open_dirty_document(
        panel_id,
        title="Exit Discard",
        discard=lambda: discarded.append(panel_id),
    )
    coordinator = _dirty_confirmation()
    try:
        coordinator.request_exit(lambda: completed.append("next"), lambda: None)
        coordinator.choose_discard()

        assert completed == ["next"]
        assert discarded == []
        assert not document.is_dirty
        assert DocumentRegistry.instance().capture_session_state()["documents"] == []
    finally:
        _close_document_view(panel_id)


def test_panel_discard_failure_does_not_approve_close():
    panel_id = "dirty_test_discard_failure"
    approved: list[str] = []
    _open_dirty_document(panel_id, title="Cannot Discard")
    coordinator = _dirty_confirmation()
    try:
        coordinator.request_panel_close(panel_id, lambda: approved.append("closed"))
        coordinator.choose_discard()

        assert coordinator.is_active is True
        assert approved == []
    finally:
        coordinator.choose_cancel()
        _close_document_view(panel_id)


def test_direct_panel_close_routes_through_shared_confirmation():
    panel_id = "dirty_test_direct_close"
    panel = ClosablePanel("Direct Close", panel_id)
    document = _open_dirty_document(panel_id, title="Direct Close")
    panel.bind_document(document.document_id)
    reopen_requests: list[tuple[str, bool]] = []

    class _WindowManager:
        @staticmethod
        def set_window_open(window_id: str, is_open: bool) -> None:
            reopen_requests.append((window_id, is_open))

    panel.set_window_manager(_WindowManager())
    coordinator = _dirty_confirmation()
    coordinator_type = _current_confirmation_type()
    previous = coordinator_type._instance
    coordinator_type._instance = coordinator
    try:
        panel.close()

        assert panel.is_open is True
        assert coordinator.active_panel_id == panel_id
        coordinator.choose_cancel()
        assert panel.is_open is True
        assert reopen_requests == [(panel_id, True)]
    finally:
        coordinator_type._instance = previous
        _close_document_view(panel_id)


def test_panel_confirmation_renders_only_from_global_modal_portal():
    panel_id = "dirty_test_portal_modal"
    _open_dirty_document(panel_id, title="Portal Modal")
    modal_service = ModalService()
    coordinator = DirtyPanelConfirmationCoordinator(
        CloseCoordinator(DocumentRegistry.instance()),
        modal_service,
    )
    try:
        assert coordinator.request_panel_close(panel_id, lambda: None)

        first_portal_frame = _SemanticContext()
        modal_service.render(first_portal_frame)
        assert first_portal_frame.opened == []

        second_portal_frame = _SemanticContext()
        modal_service.render(second_portal_frame)
        assert second_portal_frame.opened == [
            "Unsaved Changes###editor_dirty_panel_confirm"
        ]
        assert "editor.dirty_panel.dialog" in second_portal_frame.semantics
        assert modal_service.active_modal_id == coordinator.MODAL_ID
    finally:
        coordinator.choose_cancel()
        _close_document_view(panel_id)


def test_exit_confirmation_renders_from_global_modal_portal():
    panel_id = "dirty_test_global_modal"
    _open_dirty_document(panel_id, title="Global Modal")
    modal_service = ModalService()
    coordinator = DirtyPanelConfirmationCoordinator(
        CloseCoordinator(DocumentRegistry.instance()),
        modal_service,
    )
    try:
        assert coordinator.request_exit(lambda: None, lambda: None)

        focus_frame = _SemanticContext()
        modal_service.render(focus_frame)
        assert focus_frame.opened == []

        global_ctx = _SemanticContext()
        modal_service.render(global_ctx)
        assert global_ctx.opened == ["Unsaved Changes###editor_dirty_panel_confirm"]
    finally:
        coordinator.choose_cancel()
        _close_document_view(panel_id)


def test_exit_confirmation_reveals_the_view_that_owns_the_dirty_revision():
    from Infernux.engine.interaction import FocusService
    from Infernux.engine.ui.window_manager import WindowManager

    registry = DocumentRegistry.instance()
    document = registry.create(
        DocumentKind.SCENE,
        "Main",
        revision=1,
        saved_revision=0,
        dirty_view_ids=("ui_editor",),
    )
    for view_id in ("scene_view", "game_view", "ui_editor"):
        registry.attach_view(document.document_id, view_id)

    class _WindowManager:
        def __init__(self):
            self.revealed = []

        @staticmethod
        def is_window_open(_view_id):
            return True

        def restore_close_confirmation_source(self, view_id):
            self.revealed.append(view_id)

    previous_manager = WindowManager._instance
    previous_focus = FocusService._instance
    manager = _WindowManager()
    WindowManager._instance = manager
    FocusService()
    coordinator = _dirty_confirmation()
    try:
        assert coordinator.request_exit(lambda: None, lambda: None)
        assert coordinator.active_panel_id == "ui_editor"
        assert manager.revealed == ["ui_editor"]
        assert FocusService.instance().consume_panel_focus_request("ui_editor")
    finally:
        coordinator.choose_cancel()
        WindowManager._instance = previous_manager
        FocusService._instance = previous_focus
        for view_id in ("scene_view", "game_view", "ui_editor"):
            registry.close_view(view_id)


def test_titlebar_close_restores_source_tab_before_modal_focus():
    panel_id = "dirty_test_titlebar_close"
    panel = ClosablePanel("Titlebar Close", panel_id)
    document = _open_dirty_document(panel_id, title="Titlebar Close")
    panel.bind_document(document.document_id)

    class _Context:
        focus_calls = 0

        @staticmethod
        def begin_window_closable(_title, _is_open, _flags):
            return True, False

        @staticmethod
        def set_next_window_focus() -> None:
            pass

        def set_window_focus(self) -> None:
            self.focus_calls += 1

        @staticmethod
        def is_window_hovered(_flags) -> bool:
            return False

        @staticmethod
        def is_mouse_button_clicked(_button) -> bool:
            return False

        @staticmethod
        def is_window_focused(_flags) -> bool:
            return True

    ctx = _Context()
    class _WindowManager:
        def __init__(self):
            self.restored = []

        def restore_close_confirmation_source(self, window_id):
            self.restored.append(window_id)

    window_manager = _WindowManager()
    panel.set_window_manager(window_manager)
    coordinator = _dirty_confirmation()
    coordinator_type = _current_confirmation_type()
    previous = coordinator_type._instance
    from Infernux.engine.interaction import FocusService

    previous_focus = FocusService._instance
    focus = FocusService()
    coordinator_type._instance = coordinator
    focus.activate_panel("game")
    try:
        assert panel._begin_closable_window(ctx) is True
        assert panel.is_open is True
        assert ctx.focus_calls == 0
        assert window_manager.restored == [panel_id]
        assert ClosablePanel.get_active_panel_id() == panel_id
        assert coordinator.active_panel_id == panel_id
    finally:
        coordinator.choose_cancel()
        coordinator_type._instance = previous
        FocusService._instance = previous_focus
        _close_document_view(panel_id)


def test_native_modals_use_a_dedicated_main_window_child_viewport():
    source = Path(
        "cpp/infernux/function/renderer/gui/InxGUIContext.cpp"
    ).read_text(encoding="utf-8")

    begin = source.index("bool InxGUIContext::BeginPopupModal")
    end = source.index("bool InxGUIContext::BeginPopupContextItem", begin)
    implementation = source[begin:end]

    assert "ImGui::SetNextWindowClass(&modalClass)" in implementation
    assert "modalClass.ParentViewportId = ImGui::GetMainViewport()->ID" in implementation
    assert "ImGuiViewportFlags_NoAutoMerge" in implementation
    assert "ImGuiViewportFlags_NoTaskBarIcon" in implementation
    assert "ImGuiViewportFlags_TopMost" in implementation
    assert "viewport->PlatformWindowCreated" in implementation
    assert "window->StateStorage.SetBool(nativeRaiseState, false)" in implementation


def test_native_modal_is_promoted_after_late_dock_focus_processing():
    source = Path("cpp/infernux/function/renderer/gui/InxGUI.cpp").read_text(
        encoding="utf-8"
    )

    apply_index = source.index("    ApplyPendingDockTabSelections();")
    promote_index = source.index("    PromoteActiveModal();", apply_index)
    render_index = source.index("    ImGui::Render();", promote_index)
    assert apply_index < promote_index < render_index

    apply_begin = source.index("void InxGUI::ApplyPendingDockTabSelections()")
    promote_begin = source.index("void InxGUI::PromoteActiveModal()", apply_begin)
    apply_implementation = source[apply_begin:promote_begin]
    assert "ImGui::GetTopMostPopupModal() != nullptr" in apply_implementation
    assert "if (dockNode == nullptr)" in apply_implementation
    assert "m_pendingDockTabSelections.push_back(selection);" in apply_implementation
    assert "dockNode->WantCloseTabId == window->TabId" in apply_implementation
    assert "dockNode->WantCloseTabId = 0;" in apply_implementation
    assert "window->DockTabWantClose = false;" in apply_implementation
    assert "BringDockTreeToDisplayFront(window);" in apply_implementation
    assert "ImGui::BringWindowToDisplayFront(dockTreeRoot);" not in apply_implementation
    assert "RequestFrame();" in apply_implementation

    promote_end = source.index("void InxGUI::RecordCommand", promote_begin)
    promote_implementation = source[promote_begin:promote_end]
    assert "ImGui::FocusWindow(modal)" in promote_implementation
    assert "ImGui::BringWindowToFocusFront(modal->RootWindow)" in promote_implementation
    assert "ImGui::BringWindowToDisplayFront(modal)" in promote_implementation


def test_dock_presentation_moves_the_complete_tree_without_reordering_its_children():
    source = Path("cpp/infernux/function/renderer/gui/InxGUI.cpp").read_text(
        encoding="utf-8"
    )

    begin = source.index("void BringDockTreeToDisplayFront")
    end = source.index("} // namespace", begin)
    implementation = source[begin:end]
    assert "window->RootWindowDockTree" in implementation
    assert "std::stable_partition(imgui.Windows.begin(), imgui.Windows.end()" in implementation
    assert "candidate->RootWindowDockTree != root" in implementation


def test_imgui_renders_modals_in_the_overlay_layer():
    source = Path("cpp/infernux/function/renderer/gui/InxGUI.cpp").read_text(
        encoding="utf-8"
    )

    begin = source.index("    ApplyPendingDockTabSelections();")
    end = source.index("    const ImDrawData *drawData", begin)
    implementation = source[begin:end]

    promote_index = implementation.index("    PromoteActiveModal();")
    overlay_index = implementation.index(
        "activeModal->Flags |= ImGuiWindowFlags_Tooltip;"
    )
    render_index = implementation.index("    ImGui::Render();")
    restore_index = implementation.index("activeModal->Flags = activeModalFlags;")
    assert promote_index < overlay_index < render_index < restore_index

    publication = source[end : source.index("void InxGUI::QueueDockTabSelection", end)]
    assert "drawData != nullptr && drawData->Valid" in publication


import Infernux.lib as native
from Infernux.engine.ui import project_file_ops
from Infernux.engine.ui.project_delete_confirmation import ProjectDeleteConfirmationCoordinator


def _project_delete_confirmation() -> ProjectDeleteConfirmationCoordinator:
    return ProjectDeleteConfirmationCoordinator(ModalService())


class _ProjectDeleteSemanticContext:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed = False
        self.semantics: list[str] = []
        self.buttons: dict[str, object] = {}

    def open_popup(self, popup_id: str) -> None:
        self.opened.append(popup_id)

    @staticmethod
    def get_dpi_scale() -> float:
        return 1.0

    @staticmethod
    def get_main_viewport_bounds():
        return 0.0, 0.0, 1280.0, 720.0

    @staticmethod
    def set_next_window_pos(_x, _y, _condition, _pivot_x, _pivot_y) -> None:
        pass

    @staticmethod
    def begin_popup_modal(_popup_id: str, _flags: int) -> bool:
        return True

    def record_semantic_window(self, _kind, _label, semantic_id) -> None:
        self.semantics.append(semantic_id)

    def record_semantic_item(self, _kind, _label, _enabled, semantic_id) -> None:
        self.semantics.append(semantic_id)

    @staticmethod
    def text_wrapped(_value: str) -> None:
        pass

    @staticmethod
    def spacing() -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def same_line() -> None:
        pass

    @staticmethod
    def end_popup() -> None:
        pass

    def button(self, label: str, callback, width: float = 0.0, height: float = 0.0) -> None:
        self.buttons[label] = callback

    def close_current_popup(self) -> None:
        self.closed = True


def test_project_delete_modal_publishes_semantics_and_cancel_preserves_asset(tmp_path):
    asset = tmp_path / "Checkpoint.prefab"
    asset.write_text("prefab", encoding="utf-8")
    deleted: list[list[str]] = []
    coordinator = _project_delete_confirmation()

    assert coordinator.request([str(asset)], lambda paths: deleted.append(paths) or True)
    ctx = _ProjectDeleteSemanticContext()
    coordinator.render(ctx)

    assert len(ctx.opened) == 1
    assert ctx.opened[0].endswith("###project_delete_confirm")
    assert {
        "project.delete.dialog",
        "project.delete.confirm",
        "project.delete.cancel",
    }.issubset(ctx.semantics)
    next(callback for label, callback in ctx.buttons.items() if label.endswith("##cancel"))()
    assert coordinator.is_active is False
    assert asset.exists()
    assert deleted == []


def test_project_delete_modal_confirms_deduplicated_existing_paths(tmp_path):
    first = tmp_path / "First.prefab"
    second = tmp_path / "Second.prefab"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    received: list[list[str]] = []
    coordinator = _project_delete_confirmation()

    assert coordinator.request(
        [str(first), str(first), str(tmp_path / "missing.prefab"), str(second)],
        lambda paths: received.append(paths) or True,
    )
    ctx = _ProjectDeleteSemanticContext()
    coordinator.render(ctx)
    next(callback for label, callback in ctx.buttons.items() if label.endswith("##confirm"))()

    assert received == [[str(first.resolve()), str(second.resolve())]]
    assert coordinator.is_active is False
    assert ctx.closed is True


def test_prefab_delete_preserves_missing_linkage_for_undo():
    source = inspect.getsource(project_file_ops.delete_item)
    assert "prefab_guid" not in source
    assert "detach_prefab" not in source


def test_project_delete_uses_editor_modal_not_platform_message_box():
    source = Path("python/Infernux/engine/bootstrap_project.py").read_text(encoding="utf-8")
    assert "ProjectDeleteConfirmationCoordinator" in source
    assert "MessageBoxW" not in source
    assert "ctypes.windll" not in source


def test_project_script_delete_uses_meta_guid_when_database_path_lookup_misses(monkeypatch, tmp_path):
    script = tmp_path / "Attached.py"
    script.write_text("class Attached:\n    pass\n", encoding="utf-8")
    script.with_suffix(".py.meta").write_text(
        '{"metadata":{"guid":{"type":"string","value":"attached-guid"}}}',
        encoding="utf-8",
    )

    class _Database:
        @staticmethod
        def get_guid_from_path(_path):
            return ""

    from Infernux.core.assets import AssetManager

    delete_calls = []

    monkeypatch.setattr(
        AssetManager,
        "delete_asset",
        classmethod(
            lambda _cls, path, **kwargs: delete_calls.append((path, kwargs)) or True
        ),
    )

    assert project_file_ops.delete_item(str(script), _Database()) is True
    assert not script.exists()
    assert len(delete_calls) == 1
    deleted_path, delete_kwargs = delete_calls[0]
    assert deleted_path == str(script)
    assert isinstance(delete_kwargs["database"], _Database)
    assert delete_kwargs["guid_hint"] == "attached-guid"
