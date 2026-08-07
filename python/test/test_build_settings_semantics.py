from __future__ import annotations

import os
import threading
from types import SimpleNamespace

from Infernux.engine.ui.build_settings_panel import BuildSettingsPanel


class _Context:
    def __init__(self, button_results: list[bool] | None = None) -> None:
        self.semantic_items: list[tuple[str, str, bool, str]] = []
        self.semantic_values: dict[str, object] = {}
        self._button_results = iter(button_results or [])
        self.disabled_depth = 0
        self.disabled_transitions: list[str] = []

    def begin_disabled(self, _disabled: bool) -> None:
        self.disabled_depth += 1
        self.disabled_transitions.append("begin")

    def end_disabled(self) -> None:
        self.disabled_depth -= 1
        self.disabled_transitions.append("end")
        assert self.disabled_depth >= 0

    def button(self, *args, **kwargs) -> bool:
        clicked = next(self._button_results, False)
        callback = args[1] if len(args) > 1 else kwargs.get("on_click")
        if clicked and callable(callback):
            callback()
        return clicked

    @staticmethod
    def text_input(_label: str, value: str, _capacity: int) -> str:
        return value

    @staticmethod
    def checkbox(_label: str, value: bool) -> bool:
        return value

    @staticmethod
    def set_next_item_width(_width: float) -> None:
        pass

    @staticmethod
    def push_style_color(*_args) -> None:
        pass

    @staticmethod
    def pop_style_color(_count: int) -> None:
        pass

    @staticmethod
    def get_content_region_avail_width() -> float:
        return 600.0

    @staticmethod
    def get_content_region_avail_height() -> float:
        return 600.0

    @staticmethod
    def dummy(*_args) -> None:
        pass

    @staticmethod
    def push_style_var_float(*_args) -> None:
        pass

    @staticmethod
    def begin_child(*_args) -> bool:
        return True

    @staticmethod
    def end_child() -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def progress_bar(*_args) -> None:
        pass

    @staticmethod
    def is_item_hovered() -> bool:
        return False

    @staticmethod
    def set_tooltip(_text: str) -> None:
        pass

    @staticmethod
    def push_id(_value: int) -> None:
        pass

    @staticmethod
    def pop_id() -> None:
        pass

    @staticmethod
    def push_style_var_vec2(*_args) -> None:
        pass

    @staticmethod
    def pop_style_var(_count: int) -> None:
        pass

    @staticmethod
    def selectable(*_args, **_kwargs) -> bool:
        return False

    @staticmethod
    def begin_drag_drop_source(_flags: int) -> bool:
        return False

    @staticmethod
    def same_line(*_args) -> None:
        pass

    @staticmethod
    def get_window_width() -> float:
        return 600.0

    @staticmethod
    def label(_text: str) -> None:
        pass

    def record_semantic_item(
        self,
        kind: str,
        label: str,
        enabled: bool,
        semantic_id: str,
        bool_value: bool | None = None,
        numeric_value: float | None = None,
        string_value: str | None = None,
    ) -> None:
        self.semantic_items.append((kind, label, enabled, semantic_id))
        values = [value for value in (bool_value, numeric_value, string_value) if value is not None]
        if values:
            assert len(values) == 1
            self.semantic_values[semantic_id] = values[0]


def test_build_settings_scene_controls_expose_stable_semantic_ids(monkeypatch):
    import Infernux.engine.scene_manager as scene_manager
    import Infernux.engine.ui.build_settings_panel as module
    import Infernux.engine.ui.igui as igui

    monkeypatch.setattr(module, "get_project_root", lambda: "C:/RacingPilot")
    monkeypatch.setattr(
        scene_manager.SceneFileManager,
        "instance",
        staticmethod(lambda: SimpleNamespace(current_scene_path="C:/RacingPilot/Assets/racetrack.scene")),
    )
    monkeypatch.setattr(igui.IGUI, "multi_drop_target", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(igui.IGUI, "drop_target", staticmethod(lambda *_args, **_kwargs: None))

    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._scenes = [
        "C:/RacingPilot/Assets/racetrack.scene",
        "C:/RacingPilot/Assets/results.scene",
    ]
    panel._save = lambda: None
    ctx = _Context()

    panel._render_scene_section(ctx)

    semantic_ids = {item[3] for item in ctx.semantic_items}
    assert {
        "build_settings.scene.add_open",
        "build_settings.scene.0.row",
        "build_settings.scene.0.move_down",
        "build_settings.scene.0.remove",
        "build_settings.scene.1.row",
        "build_settings.scene.1.move_up",
        "build_settings.scene.1.remove",
    } <= semantic_ids
    assert ctx.semantic_values["build_settings.scene.0.row"] == "Assets/racetrack.scene"
    assert ctx.semantic_values["build_settings.scene.1.row"] == "Assets/results.scene"


def test_build_settings_does_not_turn_external_splash_deletion_into_user_edit():
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._splash_items = [
        {
            "type": "image",
            "path": "C:/Missing/splash.png",
            "duration": 3.0,
        }
    ]
    panel._bind_project_settings_document = lambda: None
    saves = []
    panel._save = lambda: saves.append(True)

    panel.on_enable()

    assert panel._splash_items[0]["path"] == "C:/Missing/splash.png"
    assert saves == []


def test_build_settings_add_open_scene_uses_the_button_result(monkeypatch):
    import Infernux.engine.scene_manager as scene_manager
    import Infernux.engine.ui.build_settings_panel as module
    import Infernux.engine.ui.igui as igui

    current_scene = "C:/RacingPilot/Assets/racetrack.scene"
    monkeypatch.setattr(module, "get_project_root", lambda: "C:/RacingPilot")
    monkeypatch.setattr(
        scene_manager.SceneFileManager,
        "instance",
        staticmethod(lambda: SimpleNamespace(current_scene_path=current_scene)),
    )
    monkeypatch.setattr(igui.IGUI, "multi_drop_target", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(igui.IGUI, "drop_target", staticmethod(lambda *_args, **_kwargs: None))

    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._scenes = []
    saves: list[list[str]] = []
    panel._save = lambda: saves.append(list(panel._scenes))

    panel._render_scene_section(_Context(button_results=[True]))

    assert panel._scenes == [os.path.abspath(current_scene)]
    assert saves == [[os.path.abspath(current_scene)]]


def test_build_settings_output_controls_expose_stable_semantic_ids(monkeypatch):
    import Infernux.engine.ui.build_settings_panel as module

    monkeypatch.setattr(module, "get_project_root", lambda: "C:/RacingPilot")
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._game_name = "RacingPilot"
    panel._debug_mode = False
    panel._lto = True
    panel._enable_jit = False
    panel._output_dir = "C:/Builds/RacingPilot"
    panel._icon_path = ""
    panel._save = lambda: None
    ctx = _Context()

    panel._render_output_section(ctx)

    semantic_ids = {item[3] for item in ctx.semantic_items}
    assert {
        "build_settings.game_name",
            "build_settings.debug_mode",
            "build_settings.lto",
            "build_settings.enable_jit",
            "build_settings.output_dir",
        "build_settings.output_dir.browse",
        "build_settings.icon",
        "build_settings.icon.browse",
    } <= semantic_ids
    assert ctx.semantic_values == {
        "build_settings.game_name": "RacingPilot",
            "build_settings.debug_mode": False,
            "build_settings.lto": True,
            "build_settings.enable_jit": False,
            "build_settings.output_dir": "C:/Builds/RacingPilot",
        "build_settings.icon": "",
    }


def test_build_settings_output_error_stays_inside_editor(monkeypatch):
    import Infernux.engine.ui.build_settings_panel as module
    from Infernux.engine.game_builder import BuildOutputDirectoryError, GameBuilder

    assert not hasattr(module, "show_system_error_dialog")
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._build_error = None
    error = BuildOutputDirectoryError(
        "required",
        "",
        marker_filename=GameBuilder.OUTPUT_MARKER_FILENAME,
    )

    panel._show_output_directory_error(error)

    assert panel._build_error


def test_build_settings_disables_only_the_settings_body_while_building(monkeypatch):
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._building = True
    panel._build_message = "Building"
    panel._build_progress = 0.5
    panel._cancel_event = threading.Event()
    panel._execute_build_command = lambda _command_id: True
    for name in (
        "_render_output_section",
        "_render_display_section",
        "_render_splash_section",
        "_render_scene_section",
    ):
        monkeypatch.setattr(panel, name, lambda _ctx: None)
    ctx = _Context()

    panel._render_body(ctx)

    assert ctx.disabled_transitions == ["begin", "end"]
    assert ctx.disabled_depth == 0


def test_build_click_cannot_unbalance_the_disabled_stack_mid_frame():
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._building = False
    panel._build_cancelled = False
    panel._build_error = None
    panel._build_output_dir = None
    panel._scenes = ["C:/RacingPilot/Assets/MainMenu.scene"]
    panel._output_dir = "C:/Builds/RacingPilot"
    commands: list[str] = []
    panel._execute_build_command = lambda command_id: (
        commands.append(command_id)
        or setattr(panel, "_building", command_id == "build.start")
        or True
    )
    ctx = _Context(button_results=[True, False])

    panel._render_build_controls(ctx)

    assert panel._building is True
    assert commands == ["build.start"]
    assert ctx.disabled_transitions == []
    assert ctx.disabled_depth == 0


def test_build_status_actions_expose_stable_semantic_ids():
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._build_message = "Building"
    panel._build_progress = 0.5
    panel._cancel_event = threading.Event()
    panel._execute_build_command = lambda _command_id: True

    panel._building = True
    panel._build_cancelled = False
    panel._build_error = None
    panel._build_output_dir = None
    building = _Context()
    panel._render_build_controls(building)

    panel._building = False
    panel._build_cancelled = True
    cancelled = _Context()
    panel._render_build_controls(cancelled)

    panel._build_cancelled = False
    panel._build_error = "Failed"
    failed = _Context()
    panel._render_build_controls(failed)

    panel._build_error = None
    panel._build_output_dir = "C:/Builds/RacingPilot"
    succeeded = _Context()
    panel._render_build_controls(succeeded)

    assert {item[3] for item in building.semantic_items} == {
        "build_settings.status",
        "build_settings.progress",
        "build_settings.progress_message",
        "build_settings.cancel",
    }
    assert building.semantic_values["build_settings.status"] == "building"
    assert building.semantic_values["build_settings.progress"] == 0.5
    assert building.semantic_values["build_settings.progress_message"] == "Building"
    assert {item[3] for item in cancelled.semantic_items} == {
        "build_settings.status",
        "build_settings.cancelled.dismiss",
    }
    assert cancelled.semantic_values["build_settings.status"] == "cancelled"
    assert {item[3] for item in failed.semantic_items} == {
        "build_settings.status",
        "build_settings.error",
        "build_settings.error.dismiss",
    }
    assert failed.semantic_values["build_settings.status"] == "failed"
    assert failed.semantic_values["build_settings.error"] == "Failed"
    assert {item[3] for item in succeeded.semantic_items} == {
        "build_settings.status",
        "build_settings.result.output_dir",
        "build_settings.result.open_folder",
        "build_settings.result.dismiss",
    }
    assert succeeded.semantic_values["build_settings.status"] == "succeeded"
    assert succeeded.semantic_values["build_settings.result.output_dir"] == "C:/Builds/RacingPilot"


def test_build_commands_gate_start_and_cancel_without_entering_undo():
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._building = False
    panel._scenes = ["C:/RacingPilot/Assets/Main.scene"]
    panel._output_dir = "C:/Builds/RacingPilot"
    panel._cancel_event = threading.Event()
    starts: list[bool] = []
    panel._do_build = lambda *, run_after: starts.append(run_after) or True

    assert panel.can_start_build()
    assert panel.command_start_build(run_after=False)
    assert panel.command_start_build(run_after=True)
    assert starts == [False, True]

    panel._building = True
    assert not panel.can_start_build()
    assert panel.can_cancel_build()
    assert panel.command_cancel_build()
    assert panel._cancel_event.is_set()
    assert not panel.can_cancel_build()
    assert not panel.command_cancel_build()
