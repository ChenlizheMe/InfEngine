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
        self.progress_bars: list[tuple] = []
        self.labels: list[str] = []
        self.wrapped_texts: list[str] = []
        self.child_ids: list[str] = []
        self.same_line_count = 0
        self.cursor_x = 0.0

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

    def begin_child(self, *args) -> bool:
        if args:
            self.child_ids.append(str(args[0]))
        return True

    @staticmethod
    def end_child() -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    def progress_bar(self, *args) -> None:
        self.progress_bars.append(args)

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

    def same_line(self, *_args) -> None:
        self.same_line_count += 1

    @staticmethod
    def get_window_width() -> float:
        return 600.0

    def label(self, text: str) -> None:
        self.labels.append(text)

    def text_wrapped(self, text: str) -> None:
        self.wrapped_texts.append(text)

    def get_cursor_pos_x(self) -> float:
        return self.cursor_x

    def set_cursor_pos_x(self, x: float) -> None:
        self.cursor_x = float(x)

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
        "Assets/racetrack.scene",
        "Assets/results.scene",
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

    assert panel._scenes == ["Assets/racetrack.scene"]
    assert saves == [["Assets/racetrack.scene"]]


def test_build_settings_rejects_scene_outside_assets(monkeypatch):
    import Infernux.engine.ui.build_settings_panel as module

    monkeypatch.setattr(module, "get_project_root", lambda: "C:/RacingPilot")
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._scenes = []
    saves = []
    panel._save = lambda: saves.append(True)

    panel._add_scene("C:/RacingPilot/Legacy.scene")

    assert panel._scenes == []
    assert saves == []


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
    panel._scenes = ["Assets/MainMenu.scene"]
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
    assert len(building.progress_bars) == 1
    assert "##build_status_message" in failed.child_ids
    assert failed.wrapped_texts
    assert failed.same_line_count == 0


def test_build_window_hides_its_slider_while_preflight_owns_progress(monkeypatch):
    import Infernux.engine.ui.build_preflight_progress as preflight

    monkeypatch.setattr(
        preflight.BuildPreflightProgressService,
        "instance",
        staticmethod(lambda: SimpleNamespace(is_active=True)),
    )
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._building = True
    panel._build_message = "Scanning project resources..."
    panel._build_progress = 0.0
    panel._cancel_event = threading.Event()
    panel._execute_build_command = lambda _command_id: True
    ctx = _Context()

    panel._render_build_controls(ctx)

    assert ctx.progress_bars == []
    assert "build_settings.progress" not in ctx.semantic_values
    assert ctx.semantic_values["build_settings.status"] == "building"
    assert "build_settings.cancel" in {item[3] for item in ctx.semantic_items}


def test_build_error_log_does_not_push_the_dismiss_button_offscreen():
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._building = False
    panel._build_cancelled = False
    panel._build_error = (
        "Library artifact is stale for 432e6ee7b2e0d6dbbb11ca04725e8100:\n"
        + ("D:/Temp/惊梦/Library/Artifacts/Particle/" + "a" * 80 + ".inxparticle\n") * 12
        + "See: D:/very/long/path/to/infernux-player-build/build.log"
    )
    panel._build_output_dir = None
    ctx = _Context()

    panel._render_build_controls(ctx)

    assert ctx.progress_bars == []
    assert ctx.same_line_count == 0
    assert "##build_status_message" in ctx.child_ids
    assert any(panel._build_error in text for text in ctx.wrapped_texts)
    assert "build_settings.error.dismiss" in {item[3] for item in ctx.semantic_items}


def test_build_progress_does_not_drive_a_second_status_bar_slider(monkeypatch):
    import Infernux.engine.ui.engine_status as engine_status

    recorded: list[tuple] = []

    @classmethod
    def _capture(cls, text, progress=-1.0, kind=None, **kwargs):
        recorded.append((text, progress, kind, kwargs))

    monkeypatch.setattr(engine_status.EngineStatus, "set", _capture)
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._cancel_event = threading.Event()

    panel._on_build_progress("Packing project content", 0.981)

    assert panel._build_progress == 0.981
    assert recorded == [
        ("Packing project content", -1.0, "activity", {"source": "build", "priority": 20})
    ]


def test_build_commands_gate_start_and_cancel_without_entering_undo():
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._building = False
    panel._scenes = ["Assets/Main.scene"]
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


def test_build_preparation_flushes_writes_before_publishing_asset_index(
    monkeypatch, tmp_path
):
    import Infernux.core.assets as assets_module

    events: list[str] = []
    index_path = tmp_path / "Library" / "AssetIndex.json"

    class _Database:
        asset_index_path = str(index_path)

        def refresh(self) -> None:
            events.append("refresh")
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text('{"project_root":"test","entries":[]}\n', encoding="utf-8")

        def flush_derived_index(self) -> None:
            events.append("flush_index")

    monkeypatch.setattr(
        assets_module.AssetManager,
        "flush_all_asset_writes",
        classmethod(lambda cls: events.append("flush_writes")),
    )
    monkeypatch.setattr(
        "Infernux.engine.runtime_artifact_catalog.load_asset_index",
        lambda _root: [],
    )
    monkeypatch.setattr(
        "Infernux.renderstack.discovery.discover_effect_features",
        lambda: None,
    )
    monkeypatch.setattr(
        "Infernux.particle.artifact.ParticleArtifactRegistry.ensure_project_compiled",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "Infernux.engine.ui.build_settings_panel.get_project_root",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        BuildSettingsPanel,
        "services",
        property(lambda _self: SimpleNamespace(asset_database=_Database())),
    )

    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    assert panel._prepare_asset_catalog_for_build() == str(index_path)
    assert events == ["flush_writes", "refresh", "flush_index"]


def test_bind_published_catalog_keeps_snapshot_when_index_file_vanishes(tmp_path):
    index_path = tmp_path / "Library" / "AssetIndex.json"
    entries = [
        {
            "guid": "a" * 32,
            "normalized_path": "assets/main.scene",
            "source": {"size": 1, "modified_ns": 1},
            "content_hash": "a" * 16,
            "dependencies": [],
        }
    ]
    captured: dict[str, object] = {}
    panel = BuildSettingsPanel.__new__(BuildSettingsPanel)
    panel._make_builder = lambda: SimpleNamespace(
        project_path=str(tmp_path),
        freeze_asset_index_entries=lambda value: captured.setdefault(
            "entries", list(value)
        ),
    )

    builder = panel._bind_published_player_catalog(
        {"path": str(index_path), "entries": entries}
    )

    assert not index_path.exists()
    assert captured["entries"] == entries
    assert builder.project_path == str(tmp_path)
