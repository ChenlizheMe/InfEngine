"""Editor-owned asset, clip, and scene Save As workflow contracts."""

from __future__ import annotations

from pathlib import Path

from Infernux.core.animation_timeline import TimelineKeyframe
from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel
from Infernux.engine.ui.asset_save_dialog import AssetSaveAsDialog


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


def test_asset_save_dialog_resolves_project_relative_asset_path(tmp_path):
    dialog = AssetSaveAsDialog("animtimeline.save_as", "timeline")

    assert dialog.request(
        title="Save Timeline",
        extension="animtimeline",
        default_name="Results Light Lift.animtimeline",
        project_root=str(tmp_path),
    )

    dialog.folder = "Assets/Animation"
    dialog.name = "ResultsLightLift"
    path, error = dialog.resolve_path()

    assert error == ""
    assert Path(path) == tmp_path / "Assets" / "Animation" / "ResultsLightLift.animtimeline"


def test_asset_save_dialog_rejects_paths_outside_assets(tmp_path):
    dialog = AssetSaveAsDialog("animtimeline.save_as", "timeline")
    dialog.request(
        title="Save Timeline",
        extension="animtimeline",
        default_name="Lift",
        project_root=str(tmp_path),
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
    panel._sel_key = key
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


def test_timeline_authoring_skips_semantics_outside_requested_capture():
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(
        time=0.0,
        position=[0.0, 0.0, 0.0],
        rotation=[0.0, 0.0, 0.0],
        scale=[1.0, 1.0, 1.0],
    )
    panel._timeline.keyframes.append(key)
    panel._sel_key = key
    ctx = _SemanticContext()
    ctx.semantic_capture_enabled = False

    panel._render_toolbar(ctx)
    panel._render_transport(ctx)
    panel._render_keyframe_inspector(ctx)

    assert ctx.semantic_ids == []


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


def test_timeline_new_document_and_dirty_draft_round_trip():
    panel = AnimTimelineEditorPanel()
    assert panel._dirty is True
    panel._timeline.duration = 7.5
    panel._timeline.keyframes.append(TimelineKeyframe(time=1.25))

    restored = AnimTimelineEditorPanel()
    restored.load_state(panel.save_state())

    assert restored._dirty is True
    assert restored._timeline.duration == 7.5
    assert [key.time for key in restored._timeline.keyframes] == [1.25]


def test_timeline_discard_cleans_an_unsaved_draft():
    panel = AnimTimelineEditorPanel()
    panel._timeline.keyframes.append(TimelineKeyframe(time=1.25))

    assert panel._discard_unsaved_changes() is True
    assert panel._file_path == ""
    assert panel._timeline.keyframes == []
    assert panel._dirty is False


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


def test_animclip_agent_save_as_uses_editor_modal_and_focuses_name(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_save_dialog, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: True)
    panel = AnimClip2DEditorPanel()
    clip = _ClipState(name="Player / Idle")
    ctx = _AnimClipSaveAsContext()
    monkeypatch.setattr(panel, "_render_texture_slot", lambda *_args: None)
    monkeypatch.setattr(panel, "_render_empty_state", lambda *_args: None)

    panel._show_save_as_dialog(clip)
    panel.on_render_content(ctx)

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
    dialog = AssetSaveAsDialog("animtimeline.save_as", "timeline")
    target = tmp_path / "Assets" / "Animation" / "Lift.animtimeline"
    saved: list[str] = []
    monkeypatch.setattr(asset_save_dialog, "is_synthetic_input_frame", lambda: False)
    monkeypatch.setattr(asset_save_dialog, "save_file_dialog", lambda **_kwargs: str(target))

    assert dialog.request(
        title="Save Timeline",
        extension="animtimeline",
        default_name="Lift",
        project_root=str(tmp_path),
    )
    assert dialog.is_open is True

    dialog.render(None, lambda path: saved.append(path) or True)

    assert saved == [str(target)]


def test_animclip_save_as_callback_keeps_the_requested_clip_target():
    panel = AnimClip2DEditorPanel()
    requested = _ClipState(name="Requested")
    panel._pending_save_as_clip = requested
    saved: list[tuple[_ClipState, str]] = []
    panel._do_save_clip = lambda clip, path: saved.append((clip, path)) or True

    assert panel._save_pending_clip("C:/project/Assets/Requested.animclip2d") is True
    assert saved == [(requested, "C:/project/Assets/Requested.animclip2d")]
    assert panel._pending_save_as_clip is None


import os

from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.project_context import clear_panel_tracking, set_panel_dirty
from Infernux.engine.ui.dirty_panel_confirmation import DirtyPanelConfirmationCoordinator
import Infernux.engine._scene_save as scene_save


def _scene_manager() -> SceneFileManager:
    previous = SceneFileManager._instance
    manager = SceneFileManager()
    manager._test_previous_instance = previous
    return manager


def _restore_scene_manager(manager: SceneFileManager) -> None:
    SceneFileManager._instance = manager._test_previous_instance


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

        manager.render_save_as_popup(None)

        assert saved == [str(target)]
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
    try:
        manager._engine = native
        manager._dirty = True
        manager._current_scene_path = str(tmp_path / "Assets" / "UnsavedChanges.scene")
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        monkeypatch.setattr(manager, "_save_camera_state", camera_paths.append)
        manager.request_close()

        assert manager._close_in_progress is True
        assert manager._pending_action == "close"
        assert manager._show_confirm is True
        assert camera_paths == [manager._current_scene_path]
        assert native.calls == []
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
        manager._dirty = True
        manager._current_scene_path = str(tmp_path / "Assets" / "Ordered.scene")
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        monkeypatch.setattr(manager, "_save_camera_state", lambda _path: None)

        def discard_panel() -> None:
            set_panel_dirty(panel_id, False)

        set_panel_dirty(panel_id, True, title="Animation Editor", discard_handler=discard_panel)
        manager.request_close()

        assert coordinator.active_panel_id == panel_id
        assert manager._pending_action is None
        assert manager._show_confirm is False
        assert native.calls == []
        coordinator.choose_discard()
        assert coordinator.is_active is False
        assert manager._pending_action == "close"
        assert manager._show_confirm is True
        assert native.calls == []
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        clear_panel_tracking(panel_id)
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
        manager._dirty = False
        monkeypatch.setattr(manager, "_is_play_mode", lambda: False)
        set_panel_dirty(panel_id, True, title="Animation Editor")
        manager.request_close()
        coordinator.choose_cancel()

        assert native.calls == ["cancel"]
        assert manager._close_in_progress is False
        assert coordinator.is_active is False
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        clear_panel_tracking(panel_id)
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
        set_panel_dirty(panel_id, True, title="Timeline")
        manager.request_close()

        assert coordinator.active_panel_id == panel_id
        assert native.calls == []
        coordinator.choose_discard()
        assert native.calls == ["confirm"]
    finally:
        if coordinator.is_active:
            coordinator.choose_cancel()
        clear_panel_tracking(panel_id)
        _restore_scene_manager(manager)
