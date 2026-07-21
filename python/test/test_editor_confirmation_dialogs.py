"""Editor-owned dirty-resource and asset-deletion confirmation contracts."""

from __future__ import annotations

from pathlib import Path

from Infernux.engine.project_context import clear_panel_tracking, set_panel_dirty
from Infernux.engine.ui.dirty_panel_confirmation import (
    DirtyPanelConfirmationCoordinator,
)
from Infernux.engine.ui.closable_panel import ClosablePanel


class _SemanticContext:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.semantics: list[str] = []
        self.buttons: dict[str, object] = {}
        self.window_positions: list[tuple[float, float, int, float, float]] = []

    def open_popup(self, popup_id: str) -> None:
        self.opened.append(popup_id)

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
        set_panel_dirty(first, False)
        return True

    def save_second() -> bool:
        set_panel_dirty(second, False)
        return True

    set_panel_dirty(first, True, title="First", save_handler=save_first)
    set_panel_dirty(second, True, title="Second", save_handler=save_second)
    coordinator = DirtyPanelConfirmationCoordinator()
    try:
        assert coordinator.request_exit(lambda: completed.append("done"), lambda: None)
        assert coordinator.active_panel_id == first

        coordinator.choose_save()
        assert coordinator.active_panel_id == second
        coordinator.choose_save()

        assert completed == ["done"]
        assert coordinator.is_active is False
    finally:
        clear_panel_tracking(first)
        clear_panel_tracking(second)


def test_async_save_as_cancel_reopens_confirmation_without_cancelling_exit():
    panel_id = "dirty_test_async"
    pending = False
    cancelled: list[str] = []

    def begin_save_as() -> bool:
        nonlocal pending
        pending = True
        return False

    set_panel_dirty(
        panel_id,
        True,
        title="Async",
        save_handler=begin_save_as,
        save_pending_handler=lambda: pending,
    )
    coordinator = DirtyPanelConfirmationCoordinator()
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
        clear_panel_tracking(panel_id)


def test_panel_discard_runs_panel_handler_before_approving_close():
    panel_id = "dirty_test_discard"
    approved: list[str] = []
    discarded: list[str] = []

    def discard() -> None:
        discarded.append(panel_id)
        set_panel_dirty(panel_id, False)

    set_panel_dirty(panel_id, True, title="Discard", discard_handler=discard)
    coordinator = DirtyPanelConfirmationCoordinator()
    try:
        assert coordinator.request_panel_close(panel_id, lambda: approved.append(panel_id))
        coordinator.choose_discard()

        assert discarded == [panel_id]
        assert approved == [panel_id]
        assert coordinator.is_active is False
    finally:
        clear_panel_tracking(panel_id)


def test_exit_discard_keeps_panel_dirty_if_later_close_stage_is_cancelled():
    panel_id = "dirty_test_exit_discard"
    completed: list[str] = []
    discarded: list[str] = []

    set_panel_dirty(
        panel_id,
        True,
        title="Exit Discard",
        discard_handler=lambda: discarded.append(panel_id),
    )
    coordinator = DirtyPanelConfirmationCoordinator()
    try:
        coordinator.request_exit(lambda: completed.append("next"), lambda: None)
        coordinator.choose_discard()

        assert completed == ["next"]
        assert discarded == []
        from Infernux.engine.project_context import is_panel_dirty

        assert is_panel_dirty(panel_id) is True
    finally:
        clear_panel_tracking(panel_id)


def test_panel_discard_failure_does_not_approve_close():
    panel_id = "dirty_test_discard_failure"
    approved: list[str] = []
    set_panel_dirty(panel_id, True, title="Cannot Discard")
    coordinator = DirtyPanelConfirmationCoordinator()
    try:
        coordinator.request_panel_close(panel_id, lambda: approved.append("closed"))
        coordinator.choose_discard()

        assert coordinator.is_active is True
        assert approved == []
    finally:
        coordinator.choose_cancel()
        clear_panel_tracking(panel_id)


def test_direct_panel_close_routes_through_shared_confirmation():
    panel_id = "dirty_test_direct_close"
    panel = ClosablePanel("Direct Close", panel_id)
    panel._dirty = True
    reopen_requests: list[tuple[str, bool]] = []

    class _WindowManager:
        @staticmethod
        def set_window_open(window_id: str, is_open: bool) -> None:
            reopen_requests.append((window_id, is_open))

    panel.set_window_manager(_WindowManager())
    coordinator = DirtyPanelConfirmationCoordinator()
    previous = DirtyPanelConfirmationCoordinator._instance
    DirtyPanelConfirmationCoordinator._instance = coordinator
    try:
        panel.close()

        assert panel.is_open is True
        assert coordinator.active_panel_id == panel_id
        coordinator.choose_cancel()
        assert panel.is_open is True
        assert reopen_requests == [(panel_id, True)]
    finally:
        DirtyPanelConfirmationCoordinator._instance = previous
        clear_panel_tracking(panel_id)


def test_titlebar_close_keeps_panel_open_without_stealing_modal_focus():
    panel_id = "dirty_test_titlebar_close"
    panel = ClosablePanel("Titlebar Close", panel_id)
    panel._dirty = True

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
    coordinator = DirtyPanelConfirmationCoordinator()
    previous = DirtyPanelConfirmationCoordinator._instance
    previous_active = ClosablePanel._active_panel_id
    DirtyPanelConfirmationCoordinator._instance = coordinator
    ClosablePanel._active_panel_id = "game"
    try:
        assert panel._begin_closable_window(ctx) is True
        assert panel.is_open is True
        assert ctx.focus_calls == 0
        assert ClosablePanel.get_active_panel_id() == panel_id
        assert coordinator.active_panel_id == panel_id
    finally:
        coordinator.choose_cancel()
        DirtyPanelConfirmationCoordinator._instance = previous
        ClosablePanel._active_panel_id = previous_active
        clear_panel_tracking(panel_id)


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
    assert "RequestFrame();" in apply_implementation

    promote_end = source.index("void InxGUI::RecordCommand", promote_begin)
    promote_implementation = source[promote_begin:promote_end]
    assert "ImGui::BringWindowToFocusFront(modal->RootWindow)" in promote_implementation
    assert "ImGui::BringWindowToDisplayFront(modal->RootWindowDockTree)" in promote_implementation


import Infernux.lib as native
from Infernux.engine.ui import project_file_ops
from Infernux.engine.ui.project_delete_confirmation import ProjectDeleteConfirmationCoordinator


class _ProjectDeleteSemanticContext:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed = False
        self.semantics: list[str] = []
        self.buttons: dict[str, object] = {}

    def open_popup(self, popup_id: str) -> None:
        self.opened.append(popup_id)

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
    coordinator = ProjectDeleteConfirmationCoordinator()

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
    coordinator = ProjectDeleteConfirmationCoordinator()

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


def test_prefab_asset_detach_marks_scene_dirty(monkeypatch, tmp_path):
    prefab = tmp_path / "Checkpoint.prefab"
    prefab.write_text("prefab", encoding="utf-8")

    class _Object:
        def __init__(self, guid: str, children=None):
            self.prefab_guid = guid
            self.prefab_root = True
            self._children = list(children or [])

        def get_children(self):
            return list(self._children)

    child = _Object("prefab-guid")
    root = _Object("prefab-guid", [child])

    class _Scene:
        @staticmethod
        def get_root_objects():
            return [root]

    class _SceneManager:
        @staticmethod
        def instance():
            return _SceneManager()

        @staticmethod
        def get_active_scene():
            return _Scene()

    dirty: list[bool] = []

    class _FileManager:
        @staticmethod
        def mark_dirty():
            dirty.append(True)

    class _Database:
        @staticmethod
        def get_guid_from_path(_path):
            return "prefab-guid"

    monkeypatch.setattr(native, "SceneManager", _SceneManager)
    from Infernux.engine import scene_manager as scene_manager_module

    monkeypatch.setattr(scene_manager_module.SceneFileManager, "instance", lambda: _FileManager())

    assert project_file_ops._detach_prefab_instances(str(prefab), _Database()) == 2
    assert root.prefab_guid == ""
    assert child.prefab_guid == ""
    assert root.prefab_root is False
    assert child.prefab_root is False
    assert dirty == [True]


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


def test_project_script_rename_maps_snake_case_stem_to_pascal_class(tmp_path):
    old_path = tmp_path / "regression_probe.py"
    new_path = tmp_path / "regression_probe_renamed.py"
    old_path.write_text(
        "from Infernux.components import InxComponent\n"
        "class RegressionProbe(InxComponent):\n"
        "    report_after_seconds: float = 2.0\n",
        encoding="utf-8",
    )

    project_file_ops._sync_python_script_class_name_on_rename(
        str(old_path),
        str(new_path),
    )

    content = old_path.read_text(encoding="utf-8")
    assert "class RegressionProbeRenamed(InxComponent):" in content
    assert "class RegressionProbe(InxComponent):" not in content
