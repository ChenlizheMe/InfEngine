"""Editor-owned asset, clip, and scene Save As workflow contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from Infernux.core.animation_timeline import TimelineKeyframe
from Infernux.engine.interaction import ModalService
from Infernux.engine.ui.animclip2d_editor_panel import AnimClip2DEditorPanel
from Infernux.engine.ui.animfsm_editor_panel import AnimFSMEditorPanel
from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel
from Infernux.engine.ui.asset_save_dialog import AssetSaveAsDialog
from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel


class _SemanticContext:
    def __init__(self) -> None:
        self.semantic_ids: list[str] = []
        self.semantic_values: dict[str, object] = {}

    def button(self, *_args, **_kwargs):
        return False

    def combo(self, _label, value, *_args):
        return value

    def drag_float(self, _label, value, *_args):
        return value

    def text_input(self, _label, value, *_args):
        return value

    def record_semantic_item(
        self,
        _kind,
        _label,
        _enabled=True,
        semantic_id="",
        bool_value=None,
        numeric_value=None,
        string_value=None,
    ):
        self.semantic_ids.append(semantic_id)
        if bool_value is not None:
            self.semantic_values[semantic_id] = bool_value
        elif numeric_value is not None:
            self.semantic_values[semantic_id] = numeric_value
        elif string_value is not None:
            self.semantic_values[semantic_id] = string_value

    def same_line(self, *_args):
        pass

    def label(self, *_args):
        pass

    def set_next_item_width(self, *_args):
        pass

    def push_style_color(self, *_args):
        pass

    def pop_style_color(self, *_args):
        pass

    def separator(self):
        pass

    def dummy(self, *_args):
        pass

    def get_content_region_avail_width(self):
        return 320.0


@pytest.mark.parametrize(
    "panel_factory",
    (
        pytest.param(AnimClip2DEditorPanel, id="animclip2d"),
        pytest.param(AnimFSMEditorPanel, id="animfsm"),
        pytest.param(AnimTimelineEditorPanel, id="timeline"),
        pytest.param(ParticleGraphEditorPanel, id="particle_graph"),
    ),
)
def test_authoring_windows_open_with_a_clean_blank_document(panel_factory):
    panel = panel_factory()

    assert panel.document_id
    assert panel._document_is_dirty() is False


def test_asset_save_dialog_resolves_project_relative_asset_path(tmp_path):
    dialog = AssetSaveAsDialog(
        "animtimeline.save_as",
        "timeline",
        modal_service=ModalService(),
    )

    assert dialog.request(
        title="Save Timeline",
        extension="animtimeline",
        default_name="Results Light Lift.animtimeline",
        project_root=str(tmp_path),
        save_callback=lambda _path: True,
    )

    dialog.folder = "Assets/Animation"
    dialog.name = "ResultsLightLift"
    path, error = dialog.resolve_path()

    assert error == ""
    assert Path(path) == tmp_path / "Assets" / "Animation" / "ResultsLightLift.animtimeline"


def test_asset_save_dialog_rejects_paths_outside_assets(tmp_path):
    dialog = AssetSaveAsDialog(
        "animtimeline.save_as",
        "timeline",
        modal_service=ModalService(),
    )
    dialog.request(
        title="Save Timeline",
        extension="animtimeline",
        default_name="Lift",
        project_root=str(tmp_path),
        save_callback=lambda _path: True,
    )

    dialog.folder = "../outside"
    path, error = dialog.resolve_path()

    assert path == ""
    assert "Assets" in error

    dialog.folder = "Assets"
    dialog.name = "../outside"
    path, error = dialog.resolve_path()

    assert path == ""
    assert "invalid" in error.lower()


def test_timeline_authoring_controls_publish_stable_semantics():
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(
        time=0.0,
        position=[0.0, 0.0, 0.0],
        rotation=[0.0, 0.0, 0.0],
        scale=[1.0, 1.0, 1.0],
    )
    panel._timeline.keyframes.append(key)
    panel._select_key(key, record_history=False)
    ctx = _SemanticContext()

    panel._render_toolbar(ctx)
    panel._render_transport(ctx)
    panel._render_keyframe_inspector(ctx)

    assert {
        "animtimeline.toolbar.new",
        "animtimeline.toolbar.save",
        "animtimeline.toolbar.save_as",
        "animtimeline.toolbar.name",
        "animtimeline.toolbar.duration",
        "animtimeline.toolbar.apply_mode",
        "animtimeline.transport.play_pause",
        "animtimeline.transport.stop",
        "animtimeline.transport.add_key",
        "animtimeline.transport.delete_key",
        "animtimeline.keyframe.time",
        "animtimeline.keyframe.interpolation",
        "animtimeline.keyframe.pos.x",
        "animtimeline.keyframe.rot.y",
        "animtimeline.keyframe.scl.z",
    }.issubset(set(ctx.semantic_ids))
    assert ctx.semantic_values["animtimeline.toolbar.name"] == "Timeline"
    assert ctx.semantic_values["animtimeline.toolbar.duration"] == panel._timeline.duration
    assert ctx.semantic_values["animtimeline.toolbar.apply_mode"] == panel._timeline.apply_mode
    assert ctx.semantic_values["animtimeline.keyframe.time"] == 0.0
    assert ctx.semantic_values["animtimeline.keyframe.interpolation"] == key.interp
    assert ctx.semantic_values["animtimeline.keyframe.scl.z"] == 1.0
    panel._clear_key_selection(record_history=False)


def test_timeline_authoring_skips_semantics_outside_requested_capture():
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(
        time=0.0,
        position=[0.0, 0.0, 0.0],
        rotation=[0.0, 0.0, 0.0],
        scale=[1.0, 1.0, 1.0],
    )
    panel._timeline.keyframes.append(key)
    panel._select_key(key, record_history=False)
    ctx = _SemanticContext()
    ctx.semantic_capture_enabled = False

    panel._render_toolbar(ctx)
    panel._render_transport(ctx)
    panel._render_keyframe_inspector(ctx)

    assert ctx.semantic_ids == []
    panel._clear_key_selection(record_history=False)


def test_timeline_playback_requests_full_speed_editor_frames():
    panel = AnimTimelineEditorPanel()

    assert panel._needs_full_speed_frames() is False

    panel._playing = True
    assert panel._needs_full_speed_frames() is True

    panel._playing = False
    panel._bar_was_active = True
    assert panel._needs_full_speed_frames() is True

    panel._bar_was_active = False
    panel._orbiting = True
    assert panel._needs_full_speed_frames() is True


def test_timeline_new_document_and_dirty_draft_round_trip_through_registry_session():
    from Infernux.engine.interaction import DocumentRegistry

    panel = AnimTimelineEditorPanel()
    assert panel._document_is_dirty() is False
    assert panel._new_timeline_immediate()
    assert panel._document_is_dirty() is True
    panel._timeline.duration = 7.5
    panel._timeline.keyframes.append(TimelineKeyframe(time=1.25))

    view_state = panel.save_state()
    session_state = DocumentRegistry.instance().capture_session_state()
    restored_registry = DocumentRegistry()
    assert restored_registry.queue_session_restore(session_state) == 1
    restored = AnimTimelineEditorPanel()
    assert restored.restore_persisted_session_document()
    restored.load_state(view_state)

    assert restored._document_is_dirty() is True
    assert restored._timeline.duration == 7.5
    assert [key.time for key in restored._timeline.keyframes] == [1.25]


def test_timeline_discard_cleans_an_unsaved_draft():
    from Infernux.engine.interaction import DocumentActionStatus, DocumentRegistry

    panel = AnimTimelineEditorPanel()
    panel._timeline.keyframes.append(TimelineKeyframe(time=1.25))

    result = DocumentRegistry.instance().request_discard(panel.document_id)

    assert result.status is DocumentActionStatus.APPLIED
    assert panel._file_path == ""
    assert panel._timeline.keyframes == []
    assert panel._document_is_dirty() is False


def test_timeline_gpu_preview_is_polled_until_first_texture(monkeypatch):
    class _Native:
        def __init__(self):
            self.calls = 0

        def render_timeline_cube_preview(self, *_args):
            self.calls += 1
            return 23 if self.calls >= 2 else 0

    native = _Native()
    from Infernux.engine.ui import asset_resource_preview

    monkeypatch.setattr(asset_resource_preview, "_resolve_native_engine", lambda _panel: native)
    panel = AnimTimelineEditorPanel()
    transform = ([0, 0, 0], [0, 0, 0], [1, 1, 1])

    assert panel._cube_preview_texture(*transform) == 0
    assert panel._cube_preview_texture(*transform) == 23
    assert native.calls == 2


from Infernux.engine.ui import asset_save_dialog
from Infernux.engine.ui.animclip2d_editor_panel import AnimClip2DEditorPanel, _ClipState


class _AnimClipSaveAsContext:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.semantic_ids: list[str] = []

    def open_popup(self, popup_id: str) -> None:
        self.events.append(("open_popup", popup_id))

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
    def set_next_window_size(_width, _height, _condition) -> None:
        pass

    @staticmethod
    def is_key_down(_key: int) -> bool:
        return False

    @staticmethod
    def is_key_pressed(_key: int) -> bool:
        return False

    @staticmethod
    def get_content_region_avail_width() -> float:
        return 320.0

    @staticmethod
    def dummy(_width: float, _height: float) -> None:
        pass

    @staticmethod
    def begin_popup_modal(_popup_id: str, _flags: int) -> bool:
        return True

    def record_semantic_window(self, _kind: str, _label: str, semantic_id: str) -> None:
        self.semantic_ids.append(semantic_id)

    @staticmethod
    def label(_text: str) -> None:
        pass

    @staticmethod
    def text_wrapped(_text: str) -> None:
        pass

    @staticmethod
    def spacing() -> None:
        pass

    def text_input(self, label: str, value: str, _max_length: int) -> str:
        self.events.append(("text_input", label))
        return value

    def record_semantic_item(self, _kind: str, _label: str, _enabled: bool, semantic_id: str) -> None:
        self.semantic_ids.append(semantic_id)

    def set_keyboard_focus_here(self) -> None:
        self.events.append(("focus", ""))

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def button(_label: str, _callback, width: float = 0.0, height: float = 0.0) -> bool:
        return False

    @staticmethod
    def same_line() -> None:
        pass

    @staticmethod
    def end_popup() -> None:
        pass

    def close_current_popup(self) -> None:
        self.events.append(("close_popup", ""))


class _ConfirmSaveAsContext(_AnimClipSaveAsContext):
    def button(self, label: str, callback, **_kwargs) -> bool:
        if label.endswith("##confirm"):
            callback()
            return True
        return False


def test_animclip_agent_save_as_uses_editor_modal_and_focuses_name(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_save_dialog, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: True)
    panel = AnimClip2DEditorPanel()
    modals = ModalService()
    panel._save_as_dialog.bind_modal_service(modals)
    clip = _ClipState(name="Player / Idle")
    ctx = _AnimClipSaveAsContext()

    panel._show_save_as_dialog(clip)
    modals.render(ctx)

    assert panel._save_as_dialog.is_open is True
    assert panel._pending_save_as_clip is clip
    assert panel._save_as_dialog.folder == "Assets"
    assert panel._save_as_dialog.name == "Player___Idle"
    assert ctx.events[0][0] == "open_popup"
    assert ctx.events[0][1].endswith("###animclip2d_save_as")
    assert ctx.events[1][0] == "text_input"
    assert ctx.events[1][1].endswith("##animclip2d.save_as_folder")
    assert ctx.events[2] == ("focus", "")
    assert ctx.events[3][0] == "text_input"
    assert ctx.events[3][1].endswith("##animclip2d.save_as_name")
    assert {
        "animclip2d.save_as.dialog",
        "animclip2d.save_as.folder",
        "animclip2d.save_as.name",
        "animclip2d.save_as.confirm",
        "animclip2d.save_as.cancel",
    }.issubset(ctx.semantic_ids)


def test_asset_save_as_uses_native_dialog_for_user_input(tmp_path, monkeypatch):
    modals = ModalService()
    dialog = AssetSaveAsDialog(
        "animtimeline.save_as",
        "timeline",
        modal_service=modals,
    )
    target = tmp_path / "Assets" / "Animation" / "Lift.animtimeline"
    saved: list[str] = []
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: False)
    monkeypatch.setattr(asset_save_dialog, "save_file_dialog", lambda **_kwargs: str(target))

    assert dialog.request(
        title="Save Timeline",
        extension="animtimeline",
        default_name="Lift",
        project_root=str(tmp_path),
        save_callback=lambda path: saved.append(path) or True,
    )
    assert dialog.is_open is True

    modals.render(None)

    assert saved == [str(target)]


def test_asset_save_as_nests_under_unsaved_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: True)
    modals = ModalService()
    parent_active = {"value": True}
    modals.register(
        "editor.unsaved_changes",
        is_active=lambda: parent_active["value"],
        render=lambda _ctx: None,
        cancel=lambda: parent_active.update(value=False),
    )
    assert modals.activate("editor.unsaved_changes", owner_id="particle_graph_editor")

    dialog = AssetSaveAsDialog(
        "particle_graph.save_as",
        "particle graph",
        owner_id="particle_graph_editor",
        modal_service=modals,
    )
    assert dialog.request(
        title="Save Particle Graph",
        extension="particlegraph",
        default_name="Smoke",
        project_root=str(tmp_path),
        save_callback=lambda _path: True,
    )
    assert [entry.modal_id for entry in modals.active_stack] == [
        "editor.unsaved_changes",
        "asset.save_as:particle_graph.save_as",
    ]


def test_asset_save_as_nests_under_external_document_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: True)
    modals = ModalService()
    modals.register(
        "editor.external_document_conflict",
        is_active=lambda: True,
        render=lambda _ctx: None,
        cancel=lambda: None,
    )
    assert modals.activate(
        "editor.external_document_conflict",
        owner_id="particle_graph_editor",
    )
    dialog = AssetSaveAsDialog(
        "particle_graph.save_as",
        "particle graph",
        owner_id="particle_graph_editor",
        modal_service=modals,
    )

    assert dialog.request(
        title="Save Particle Graph",
        extension="particlegraph",
        default_name="Smoke Copy",
        project_root=str(tmp_path),
        save_callback=lambda _path: True,
    )
    assert [entry.modal_id for entry in modals.active_stack] == [
        "editor.external_document_conflict",
        "asset.save_as:particle_graph.save_as",
    ]


def test_asset_save_as_is_rejected_while_unrelated_root_modal_is_active(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: True)
    modals = ModalService()
    modals.register(
        "project.delete",
        is_active=lambda: True,
        render=lambda _ctx: None,
        cancel=lambda: None,
    )
    assert modals.activate("project.delete", owner_id="project")
    dialog = AssetSaveAsDialog(
        "particle_graph.save_as",
        "particle graph",
        modal_service=modals,
    )

    assert not dialog.request(
        title="Save Particle Graph",
        extension="particlegraph",
        default_name="Smoke",
        project_root=str(tmp_path),
        save_callback=lambda _path: True,
    )
    assert dialog.is_open is False
    assert modals.active_modal_id == "project.delete"


def test_particle_graph_save_as_clears_document_dirty_state(
    tmp_path,
    monkeypatch,
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import (
        DocumentActionStatus,
        EditorInteractionCore,
    )
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    previous_core = EditorInteractionCore._instance
    core = EditorInteractionCore()
    monkeypatch.setattr(asset_save_dialog, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: True)
    monkeypatch.setattr(
        AssetManager,
        "reimport_asset",
        classmethod(lambda cls, _path: None),
    )
    try:
        panel = ParticleGraphEditorPanel()
        core.documents.mark_changed(
            panel.document_id,
            view_id=panel.window_id,
        )
        result = core.documents.request_save(panel.document_id)
        assert result.status is DocumentActionStatus.PENDING
        assert core.documents.require(panel.document_id).is_dirty is True

        panel._save_as_dialog.folder = "Assets/VFX"
        panel._save_as_dialog.name = "Smoke"
        core.modals.render(_ConfirmSaveAsContext())

        from Infernux.core.document_store import DocumentStore

        target = tmp_path / "Assets" / "VFX" / "Smoke.particlegraph"
        DocumentStore.flush(str(target))
        panel._authoring_document_controller.poll_pending_writes()

        document = core.documents.require(panel.document_id)
        assert document.is_dirty is False
        assert panel._document_is_dirty() is False
        assert panel._file_path == str(
            target.resolve()
        )
        assert core.modals.active_modal_id == ""
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core


def test_animclip_save_as_callback_keeps_the_requested_clip_target():
    panel = AnimClip2DEditorPanel()
    requested = _ClipState(name="Requested")
    panel._pending_save_as_clip = requested
    saved: list[tuple[_ClipState, str]] = []
    panel._do_save_clip = (
        lambda clip, path, **_kwargs: saved.append((clip, path)) or True
    )

    assert panel._save_pending_clip("C:/project/Assets/Requested.animclip2d") is True
    assert saved == [(requested, "C:/project/Assets/Requested.animclip2d")]
    assert panel._pending_save_as_clip is None


import os

from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.interaction import (
    DocumentCapability,
    DocumentKind,
    DocumentRegistry,
    EditorInteractionCore,
)
from Infernux.engine.ui.dirty_panel_confirmation import DirtyPanelConfirmationCoordinator
import Infernux.engine._scene_save as scene_save


class _TestResourceDocumentController:
    def __init__(self, discard=None) -> None:
        self._discard = discard

    def discard(self, *, document_id):
        del document_id
        if not callable(self._discard):
            return False
        return self._discard()


def _open_test_dirty_document(panel_id: str, title: str, *, discard=None):
    registry = DocumentRegistry.instance()
    registry.close_view(panel_id)
    document = registry.create(
        DocumentKind.GENERIC,
        title,
        revision=1,
        saved_revision=0,
        capabilities=(
            DocumentCapability.DISCARD
            if callable(discard)
            else DocumentCapability.NONE
        ),
        controller=_TestResourceDocumentController(discard),
    )
    registry.attach_view(document.document_id, panel_id)
    return document


def _mark_test_document_saved(panel_id: str) -> None:
    registry = DocumentRegistry.instance()
    document = registry.document_for_view(panel_id)
    assert document is not None
    registry.mark_saved(document.document_id)


def _scene_manager() -> SceneFileManager:
    previous = SceneFileManager._instance
    previous_core = EditorInteractionCore._instance
    previous_confirmation = DirtyPanelConfirmationCoordinator._instance
    core = EditorInteractionCore()
    DirtyPanelConfirmationCoordinator._instance = None
    manager = SceneFileManager()
    manager._test_previous_instance = previous
    manager._test_interaction_core = core
    manager._test_previous_interaction_core = previous_core
    manager._test_previous_confirmation = previous_confirmation
    return manager


def _restore_scene_manager(manager: SceneFileManager) -> None:
    DirtyPanelConfirmationCoordinator._instance = manager._test_previous_confirmation
    manager._test_interaction_core.shutdown()
    EditorInteractionCore._instance = manager._test_previous_interaction_core
    SceneFileManager._instance = manager._test_previous_instance


def _set_scene_dirty(manager: SceneFileManager, dirty: bool) -> None:
    registry = DocumentRegistry.instance()
    document = registry.require(manager.document_id)
    if dirty and not document.is_dirty:
        registry.mark_changed(document.document_id)
    elif not dirty and document.is_dirty:
        registry.restore_saved_revision(document.document_id)


def test_unsaved_scene_agent_save_uses_editor_owned_save_as_state(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_save, "_effective_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(scene_save, "is_synthetic_input_frame", lambda: True)
    manager = _scene_manager()
    try:
        manager._current_scene_path = None
        manager._show_save_as_dialog()

        assert manager._save_as_popup_open is True
        assert manager._save_as_popup_requested is True
        assert manager._save_as_focus_name is True
        assert manager._save_as_folder == "Assets"
        assert manager._save_as_name == "UntitledScene"
        assert manager._test_interaction_core.modals.active_modal_id == "scene.save_as"
    finally:
        _restore_scene_manager(manager)


def test_unsaved_scene_user_save_uses_native_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_save, "_effective_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(scene_save, "is_synthetic_input_frame", lambda: False)
    target = tmp_path / "Assets" / "Scenes" / "RacingEntry.scene"
    saved: list[str] = []
    monkeypatch.setattr(scene_save, "save_file_dialog", lambda **_kwargs: str(target))
    manager = _scene_manager()
    try:
        manager._current_scene_path = None
        manager._do_save = lambda path: saved.append(path) or True
        manager._show_save_as_dialog()

        assert manager._save_as_popup_open is False
        assert manager._save_as_native_dialog_pending is True

        manager._test_interaction_core.modals.render(None)

        assert saved == [str(target)]
        assert manager._test_interaction_core.modals.active_modal_id == ""
    finally:
        _restore_scene_manager(manager)


def test_save_as_path_is_constrained_to_assets_and_valid_names(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_save, "_effective_project_root", lambda: str(tmp_path))
    manager = _scene_manager()
    try:
        manager._save_as_folder = "Assets/Scenes"
        manager._save_as_name = "RacingEntry"
        path, error = manager._resolve_save_as_path()

        assert error == ""
        assert Path(path) == tmp_path / "Assets" / "Scenes" / "RacingEntry.scene"

        manager._save_as_folder = "../outside"
        path, error = manager._resolve_save_as_path()
        assert path == ""
        assert "Assets" in error

        manager._save_as_folder = "Assets"
        manager._save_as_name = "../invalid"
        path, error = manager._resolve_save_as_path()
        assert path == ""
        assert "invalid" in error.lower()
    finally:
        _restore_scene_manager(manager)


def test_is_under_assets_resolves_native_path_aliases(tmp_path, monkeypatch):
    project_root = tmp_path / "InfernuxRacingPilot"
    assets_root = project_root / "Assets"
    scene_path = assets_root / "RaceTrack.scene"
    scene_path.parent.mkdir(parents=True)
    scene_path.write_text("{}", encoding="utf-8")
    native_alias_root = tmp_path / "INFERN~1"
    native_alias_scene = native_alias_root / "Assets" / "RaceTrack.scene"
    original_realpath = os.path.realpath

    def resolve_native_alias(path: str) -> str:
        normalized = os.path.normcase(os.path.abspath(path))
        alias = os.path.normcase(os.path.abspath(native_alias_root))
        if normalized == alias:
            return str(project_root)
        if normalized.startswith(alias + os.sep):
            return str(project_root / os.path.relpath(normalized, alias))
        return original_realpath(path)

    monkeypatch.setattr(scene_save, "_effective_project_root", lambda: str(project_root))
    monkeypatch.setattr(scene_save.os.path, "realpath", resolve_native_alias)
    manager = _scene_manager()
    try:
        assert manager._is_under_assets(str(native_alias_scene)) is True
        assert manager._is_under_assets(str(native_alias_root / "Outside.scene")) is False
    finally:
        _restore_scene_manager(manager)


def test_dirty_scene_close_uses_editor_owned_confirmation(tmp_path, monkeypatch):
    manager = _scene_manager()

    class _Native:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def confirm_close(self) -> None:
            self.calls.append("confirm")

        def cancel_close(self) -> None:
            self.calls.append("cancel")

    native = _Native()
    camera_paths: list[str] = []
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    try:
        if coordinator.is_active:
            coordinator.choose_cancel()
        manager._engine = native
        _set_scene_dirty(manager, True)
        manager._current_scene_path = str(tmp_path / "Assets" / "UnsavedChanges.scene")
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        monkeypatch.setattr(manager, "_save_camera_state", camera_paths.append)
        manager.request_close()

        assert manager._close_in_progress is True
        assert coordinator.active_document_id == manager.document_id
        assert camera_paths == []
        assert native.calls == []
        coordinator.choose_discard()
        assert camera_paths == [manager._current_scene_path]
        assert native.calls == ["confirm"]
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        _restore_scene_manager(manager)


def test_scene_save_as_nests_under_unsaved_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_save, "_effective_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(scene_save, "is_synthetic_input_frame", lambda: True)
    manager = _scene_manager()
    parent_active = {"value": True}
    modals = manager._test_interaction_core.modals
    modals.register(
        "editor.unsaved_changes",
        is_active=lambda: parent_active["value"],
        render=lambda _ctx: None,
        cancel=lambda: parent_active.update(value=False),
    )
    try:
        assert modals.activate("editor.unsaved_changes", owner_id="scene")
        assert manager._show_save_as_dialog()
        assert [entry.modal_id for entry in modals.active_stack] == [
            "editor.unsaved_changes",
            "scene.save_as",
        ]
    finally:
        _restore_scene_manager(manager)


def test_dirty_panel_confirmation_precedes_dirty_scene_confirmation(tmp_path, monkeypatch):
    manager = _scene_manager()
    panel_id = "scene_close_dirty_panel_order"

    class _Native:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def confirm_close(self) -> None:
            self.calls.append("confirm")

        def cancel_close(self) -> None:
            self.calls.append("cancel")

    native = _Native()
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    try:
        assert coordinator.is_active is False
        manager._engine = native
        _set_scene_dirty(manager, True)
        manager._current_scene_path = str(tmp_path / "Assets" / "Ordered.scene")
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        monkeypatch.setattr(manager, "_save_camera_state", lambda _path: None)

        def discard_panel() -> None:
            _mark_test_document_saved(panel_id)

        _open_test_dirty_document(
            panel_id,
            "Animation Editor",
            discard=discard_panel,
        )
        manager.request_close()

        assert coordinator.active_panel_id == panel_id
        assert native.calls == []
        coordinator.choose_discard()
        assert coordinator.is_active is True
        assert coordinator.active_document_id == manager.document_id
        assert native.calls == []
        coordinator.choose_discard()
        assert coordinator.is_active is False
        assert native.calls == ["confirm"]
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        DocumentRegistry.instance().close_view(panel_id)
        _restore_scene_manager(manager)


def test_dirty_scene_open_uses_replace_document_transaction(monkeypatch):
    manager = _scene_manager()
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    try:
        if coordinator.is_active:
            coordinator.choose_cancel()
        _set_scene_dirty(manager, True)
        manager._current_scene_path = "current.scene"
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        monkeypatch.setattr(manager, "_save_camera_state", lambda _path: None)

        assert manager.open_scene("next.scene") is False
        assert coordinator.active_document_id == manager.document_id
        assert manager._deferred_load_path is None

        coordinator.choose_discard()

        assert coordinator.is_active is False
        assert manager._deferred_load_path == "next.scene"
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        _restore_scene_manager(manager)


def _enter_fake_prefab_mode(manager: SceneFileManager, prefab_path: str) -> tuple[str, str]:
    previous_document_id = manager.document_id
    manager._previous_scene_document_id = previous_document_id
    manager._previous_scene_path = manager._current_scene_path
    manager.is_prefab_mode = True
    manager.prefab_mode_path = prefab_path
    manager._current_scene_path = prefab_path
    manager._replace_scene_document(
        kind="prefab",
        resource_path=prefab_path,
        title="InteractionPrefab",
        dirty=True,
        preserve_previous=True,
    )
    return previous_document_id, manager.document_id


def test_dirty_prefab_exit_waits_for_explicit_close_decision(tmp_path, monkeypatch):
    manager = _scene_manager()
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    scheduled: list[object] = []
    saved: list[str] = []
    try:
        if coordinator.is_active:
            coordinator.choose_cancel()
        prefab_path = str(tmp_path / "Assets" / "Interaction.prefab")
        _previous_id, prefab_id = _enter_fake_prefab_mode(manager, prefab_path)
        monkeypatch.setattr(
            manager,
            "_schedule_prefab_exit",
            lambda callback=None, **_kwargs: scheduled.append(callback) or True,
        )
        def save_prefab(*, ticket_id=""):
            saved.append(ticket_id)
            registry = DocumentRegistry.instance()
            registry.capture_save_revision(ticket_id)
            registry.complete_save(ticket_id, success=True)
            return True

        monkeypatch.setattr(manager, "_save_prefab", save_prefab)

        assert manager.exit_prefab_mode() is True
        assert coordinator.active_document_id == prefab_id
        assert scheduled == []
        assert saved == []

        coordinator.choose_cancel()
        assert coordinator.is_active is False
        assert scheduled == []
        assert saved == []

        assert manager.exit_prefab_mode() is True
        coordinator.choose_discard()
        assert coordinator.is_active is False
        assert len(scheduled) == 1
        assert saved == []

        scheduled.clear()
        assert manager.exit_prefab_mode() is True
        coordinator.choose_save()
        assert coordinator.is_active is False
        assert len(saved) == 1
        assert len(scheduled) == 1
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        _restore_scene_manager(manager)


def test_open_from_prefab_resolves_prefab_then_previous_scene(tmp_path, monkeypatch):
    manager = _scene_manager()
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    scheduled: list[object] = []
    saved: list[str] = []
    try:
        if coordinator.is_active:
            coordinator.choose_cancel()
        manager._current_scene_path = str(tmp_path / "Assets" / "Current.scene")
        _set_scene_dirty(manager, True)
        previous_id, prefab_id = _enter_fake_prefab_mode(
            manager,
            str(tmp_path / "Assets" / "Interaction.prefab"),
        )
        monkeypatch.setattr(
            manager,
            "_schedule_prefab_exit",
            lambda callback=None, **_kwargs: scheduled.append(callback) or True,
        )
        monkeypatch.setattr(
            manager,
            "_save_prefab",
            lambda *, ticket_id="": saved.append(ticket_id) or True,
        )
        monkeypatch.setattr(manager, "_save_camera_state", lambda _path: None)

        assert manager.open_scene("next.scene") is True
        assert coordinator.active_document_id == prefab_id
        assert manager._deferred_load_path is None
        assert saved == []

        coordinator.choose_discard()
        assert coordinator.is_active is False
        assert len(scheduled) == 1
        assert manager._deferred_load_path is None

        # The deferred Prefab swap restores the previous Scene before running
        # the continuation. The previous Scene must then get its own decision.
        manager.is_prefab_mode = False
        manager._scene_document_id = previous_id
        manager._current_scene_path = manager._previous_scene_path
        callback = scheduled[0]
        assert callable(callback)
        callback()

        assert coordinator.active_document_id == previous_id
        assert manager._deferred_load_path is None
        coordinator.choose_cancel()
        assert manager._deferred_load_path is None
        assert saved == []
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        _restore_scene_manager(manager)


def test_prefab_exit_records_history_only_after_deferred_transition_completes(
    tmp_path,
    monkeypatch,
):
    from Infernux.engine.interaction import EditorContextSnapshot, PrefabCommandService
    from Infernux.engine.undo import PrefabModeCommand, UndoManager

    manager = _scene_manager()
    previous_undo = UndoManager._instance
    undo = UndoManager()
    previous_service = PrefabCommandService._instance
    service = PrefabCommandService(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        context_provider=lambda: EditorContextSnapshot(),
    )
    requested: list[tuple[object, bool]] = []
    try:
        prefab_path = str(tmp_path / "Assets" / "Interaction.prefab")
        _enter_fake_prefab_mode(manager, prefab_path)
        monkeypatch.setattr(
            manager,
            "_request_prefab_exit",
            lambda on_complete=None, *, preserve_undo_history=False: (
                requested.append((on_complete, preserve_undo_history)) or True
            ),
        )

        assert service.exit() is True
        assert undo.action_journal.applied_entries() == ()
        assert len(requested) == 1
        callback, preserve_history = requested[0]
        assert preserve_history is True

        manager.is_prefab_mode = False
        callback()

        entries = undo.action_journal.applied_entries()
        assert len(entries) == 1
        assert isinstance(entries[0].action, PrefabModeCommand)
        assert entries[0].action.description == "Exit Prefab Mode"
    finally:
        service.shutdown()
        PrefabCommandService._instance = previous_service
        UndoManager._instance = previous_undo
        _restore_scene_manager(manager)


def test_deferred_prefab_exit_preserves_history_for_tracked_transition(monkeypatch):
    manager = _scene_manager()
    calls: list[bool] = []
    completed: list[bool] = []
    try:
        manager._deferred_exit_prefab = True
        manager._post_prefab_exit_callback = lambda: completed.append(True)
        monkeypatch.setattr(
            manager,
            "_do_exit_prefab_mode",
            lambda preserve_undo_history=False: (
                calls.append(bool(preserve_undo_history)) or True
            ),
        )

        assert manager._run_deferred_exit_prefab_task(
            preserve_undo_history=True,
        ) is True
        assert calls == [True]
        assert completed == [True]
        assert manager._deferred_exit_prefab is False
    finally:
        _restore_scene_manager(manager)


def test_dirty_panel_cancel_releases_native_close_request(monkeypatch):
    manager = _scene_manager()
    panel_id = "scene_close_dirty_panel_cancel"

    class _Native:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def confirm_close(self) -> None:
            self.calls.append("confirm")

        def cancel_close(self) -> None:
            self.calls.append("cancel")

    native = _Native()
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    try:
        assert coordinator.is_active is False
        manager._engine = native
        _set_scene_dirty(manager, False)
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        _open_test_dirty_document(panel_id, "Animation Editor")
        manager.request_close()
        coordinator.choose_cancel()

        assert native.calls == ["cancel"]
        assert manager._close_in_progress is False
        assert coordinator.is_active is False
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        DocumentRegistry.instance().close_view(panel_id)
        _restore_scene_manager(manager)


def test_play_mode_still_confirms_dirty_resource_panels(monkeypatch):
    manager = _scene_manager()
    panel_id = "play_close_dirty_resource"

    class _Native:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def confirm_close(self) -> None:
            self.calls.append("confirm")

        def cancel_close(self) -> None:
            self.calls.append("cancel")

    native = _Native()
    coordinator = DirtyPanelConfirmationCoordinator.instance()
    try:
        assert coordinator.is_active is False
        manager._engine = native
        monkeypatch.setattr(manager, "_is_play_mode", lambda: True)
        _open_test_dirty_document(panel_id, "Timeline")
        manager.request_close()

        assert coordinator.active_panel_id == panel_id
        assert native.calls == []
        coordinator.choose_discard()
        assert native.calls == ["confirm"]
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        DocumentRegistry.instance().close_view(panel_id)
        _restore_scene_manager(manager)
