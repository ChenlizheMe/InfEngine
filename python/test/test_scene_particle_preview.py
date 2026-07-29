from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from Infernux.components import ParticleSystem
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.ui.scene_view_panel import SceneViewPanel


def _panel() -> SceneViewPanel:
    panel = SceneViewPanel(engine=None)
    panel._play_mode_manager = SimpleNamespace(is_edit_mode=True)
    return panel


def test_scene_particle_preview_follows_primary_selection():
    panel = _panel()
    component = ParticleSystem()
    calls = []
    component.editor_preview_begin = lambda: calls.append("begin") or True
    component.editor_preview_pause = lambda: calls.append("pause") or True
    component.editor_preview_suspend = component.editor_preview_pause
    component.editor_preview_is_playing = lambda: True
    selected = SimpleNamespace(get_py_components=lambda: [component])

    panel._on_particle_preview_selection(selected)
    assert panel._particle_preview_component is component
    assert panel._particle_preview_playing is True
    assert calls == ["begin"]

    panel._on_particle_preview_selection(None)
    assert panel._particle_preview_component is None
    assert calls == ["begin", "pause"]


def test_scene_particle_preview_ticks_only_in_edit_mode():
    panel = _panel()
    component = ParticleSystem()
    calls = []
    component.editor_preview_begin = lambda: True
    component.editor_preview_is_playing = lambda: True
    component.editor_preview_update = lambda delta, speed: calls.append((delta, speed)) or True
    selected = SimpleNamespace(get_py_components=lambda: [component])
    panel._on_particle_preview_selection(selected)
    panel._particle_preview_is_live = lambda _component, _game_object: True

    panel._particle_preview_speed = 1.5
    panel._tick_particle_preview(0.02)
    assert calls == [(0.02, 1.5)]

    panel._play_mode_manager.is_edit_mode = False
    component.editor_preview_end = lambda: calls.append("end")
    panel._tick_particle_preview(0.02)
    assert calls == [(0.02, 1.5)]
    assert panel._particle_preview_component is None


def test_scene_particle_preview_never_binds_or_controls_play_mode_component():
    panel = _panel()
    panel._play_mode_manager.is_edit_mode = False
    calls = []
    component = SimpleNamespace(
        editor_preview_begin=lambda: calls.append("begin") or True,
        editor_preview_end=lambda: calls.append("end"),
    )
    selected = SimpleNamespace(get_py_components=lambda: [component])

    panel._on_particle_preview_selection(selected)

    assert panel._particle_preview_component is None
    assert panel._particle_preview_object is None
    assert calls == []


def test_scene_particle_preview_pre_render_ticks_hidden_scene_tab(monkeypatch):
    panel = _panel()
    calls = []
    panel._tick_particle_preview = calls.append
    clock = iter((10.0, 10.025))
    scene_view_module = importlib.import_module(
        "Infernux.engine.ui.scene_view_panel"
    )
    monkeypatch.setattr(scene_view_module.time, "monotonic", lambda: next(clock))

    panel._pre_render(None)
    panel._pre_render(None)

    assert calls == pytest.approx([0.0, 0.025])


def test_scene_particle_preview_controls_use_current_semantic_value_contract():
    panel = _panel()
    calls = []
    owner = SimpleNamespace(id=1)
    component = SimpleNamespace(
        game_object=owner,
        editor_preview_emitter_states=lambda: [
            {
                "index": 0,
                "name": "Emitter",
                "enabled": True,
                "visible": True,
                "solo": False,
            }
        ],
        editor_preview_pause=lambda: calls.append("pause"),
        editor_preview_stop=lambda: calls.append("stop"),
        editor_preview_time_seconds=lambda: 0.5,
        editor_preview_duration_seconds=lambda: 2.0,
        editor_preview_seek=lambda value: calls.append(("seek", value)) or True,
        editor_preview_set_emitter_muted=lambda *_args: calls.append("mute"),
        editor_preview_set_emitter_solo=lambda *_args: calls.append("solo"),
        editor_preview_restart_emitter=lambda index: calls.append(("restart", index)) or True,
    )
    panel._particle_preview_component = component
    panel._particle_preview_object = owner
    panel._particle_preview_playing = True

    semantics = []

    class Context:
        semantic_capture_enabled = True

        def __init__(self):
            self._button_results = iter((True, False, True))
            self.current_item = ""

        def set_cursor_pos_x(self, _value):
            pass

        def set_cursor_pos_y(self, _value):
            pass

        def push_style_color(self, *_args):
            pass

        def pop_style_color(self, _count=1):
            pass

        def push_style_var_float(self, *_args):
            pass

        def pop_style_var(self, _count=1):
            pass

        def begin_child(self, *_args):
            return True

        def end_child(self):
            pass

        def is_window_hovered(self):
            return True

        def invisible_button(self, *_args):
            pass

        def is_item_hovered(self):
            return False

        def is_item_active(self):
            return False

        def is_item_deactivated_after_edit(self):
            return False

        def get_window_pos_x(self):
            return 0.0

        def get_window_pos_y(self):
            return 0.0

        def draw_line(self, *_args):
            pass

        def label(self, _text):
            pass

        def align_text_to_frame_padding(self):
            pass

        def set_next_item_width(self, _value):
            pass

        def separator(self):
            pass

        def float_slider(self, _label, value, *_args):
            self.current_item = _label
            return value

        def drag_float(self, _label, value, *_args):
            self.current_item = _label
            return value

        def record_semantic_item(self, kind, label, enabled, semantic_id, **values):
            semantics.append((kind, label, enabled, semantic_id, values, self.current_item))

        def button(self, _label, width=0.0):
            self.current_item = _label
            return next(self._button_results)

        def same_line(self, *_args):
            pass

        def checkbox(self, _label, value):
            self.current_item = _label
            return value

        def begin_disabled(self, _disabled=True):
            pass

        def end_disabled(self):
            pass

    hovered = panel._draw_particle_preview_overlay(Context(), 0.0, 0.0, 640.0, 480.0)

    assert hovered is True
    assert calls == ["pause", ("restart", 0)]
    assert panel._particle_preview_prepared is True
    assert panel._particle_preview_playing is True
    speed = next(item for item in semantics if item[3] == "scene_view.particle_preview.speed")
    assert speed[4] == {"numeric_value": 1.0}
    seek = next(item for item in semantics if item[3] == "scene_view.particle_preview.time")
    assert seek[4] == {"numeric_value": 0.5}
    assert any(item[3] == "scene_view.particle_preview.pause" for item in semantics)
    assert any(item[3] == "scene_view.particle_preview.emitter.0.restart" for item in semantics)
    semantic_sources = {item[3]: item[5] for item in semantics}
    assert semantic_sources["scene_view.particle_preview.emitter.0.visible"].endswith(
        "##particle_preview_visible_0"
    )
    assert semantic_sources["scene_view.particle_preview.emitter.0.solo"].endswith(
        "##particle_preview_solo_0"
    )
    assert semantic_sources["scene_view.particle_preview.emitter.0.restart"].endswith(
        "##particle_preview_restart_0"
    )


def test_particle_preview_restart_resumes_a_stopped_preview():
    component = ParticleSystem()
    component._editor_preview_active = True
    component._editor_preview_play_requested = False
    calls = []
    component._ensure_editor_preview_runtime = (
        lambda: calls.append("ensure") or (True, True)
    )
    component.restart = lambda emitter: calls.append(("restart", emitter)) or True

    assert component.editor_preview_restart_emitter(2) is True
    assert calls == ["ensure", ("restart", 2)]
    assert component._editor_preview_play_requested is True
    assert component._playing is True


def test_particle_preview_begin_restarts_every_enabled_emitter_after_rebuild():
    component = ParticleSystem()
    component._editor_preview_play_requested = True
    component._ensure_editor_preview_runtime = lambda: (True, True)
    calls = []
    component.restart = lambda **kwargs: calls.append(kwargs) or True

    assert component.editor_preview_begin() is True
    assert calls == [{"honor_play_on_start": False}]


def test_particle_preview_stop_drops_native_draws_immediately():
    component = ParticleSystem()
    component._gpu_controllers = [object()]
    calls = []
    component.stop = lambda: calls.append("stop") or True
    component._remove_native_batch = lambda: calls.append("remove")

    assert component.editor_preview_stop() is True
    assert calls == ["stop", "remove"]
    assert component._editor_preview_play_requested is False
    assert component._playing is False


def test_particle_preview_update_repairs_lost_native_residency():
    component = ParticleSystem()
    component._editor_preview_active = True
    component._editor_preview_play_requested = True
    calls = []
    component._ensure_editor_preview_runtime = (
        lambda: calls.append("ensure") or (True, True)
    )
    component.restart = lambda **kwargs: calls.append(("restart", kwargs)) or True
    component.update = lambda delta: calls.append(("update", delta))
    component._gpu_runtime_resident = lambda: True

    assert component.editor_preview_update(0.25, 2.0) is True
    assert calls == [
        "ensure",
        ("restart", {"honor_play_on_start": False}),
        ("update", 0.5),
    ]


def test_particle_preview_republishes_stale_python_runtime(monkeypatch):
    component = ParticleSystem()
    component._gpu_controllers = [object()]
    component._gpu_emitter_ids = [7]
    component._gpu_emitter_indices = [0]
    native = SimpleNamespace(
        _gpu_particle_artifact_revision=lambda emitter_id: 1 if emitter_id == 8 else 0
    )
    monkeypatch.setattr(
        ParticleSystem,
        "_native_engine",
        staticmethod(lambda: native),
    )
    calls = []

    def compile_asset(*, force=False):
        calls.append(
            (
                force,
                list(component._gpu_controllers),
                list(component._gpu_emitter_ids),
            )
        )
        component._gpu_controllers = [object()]
        component._gpu_emitter_ids = [8]
        component._gpu_emitter_indices = [0]
        return True

    component._compile_asset = compile_asset

    assert component._ensure_editor_preview_runtime() == (True, True)
    assert calls == [(True, [], [])]


def test_particle_preview_rebinds_after_play_mode_restores_scene(monkeypatch):
    from Infernux.engine.play_mode import PlayModeState

    panel = _panel()
    old_component = SimpleNamespace(editor_preview_end=lambda: None)
    panel._particle_preview_component = old_component
    panel._particle_preview_object = object()
    panel._particle_preview_playing = True
    panel._particle_preview_prepared = True
    calls = []
    monkeypatch.setattr(
        panel,
        "_restore_particle_preview_selection",
        lambda: calls.append("restore"),
    )

    panel._on_particle_preview_play_mode_changed(
        SimpleNamespace(
            old_state=PlayModeState.PLAYING,
            new_state=PlayModeState.EDIT,
        )
    )

    assert panel._particle_preview_component is None
    assert panel._particle_preview_object is None
    assert panel._particle_preview_playing is False
    assert panel._particle_preview_prepared is False
    assert panel._particle_preview_restore_pending is True
    assert calls == []

    deferred_module = importlib.import_module("Infernux.engine.deferred_task")
    monkeypatch.setattr(
        deferred_module.DeferredTaskRunner,
        "instance",
        classmethod(lambda _cls: SimpleNamespace(is_busy=False)),
    )
    panel._restore_particle_preview_if_ready()

    assert panel._particle_preview_restore_pending is False
    assert calls == ["restore"]


def test_invalid_particle_preview_wrapper_never_removes_native_batch():
    panel = _panel()
    calls = []
    panel._particle_preview_component = SimpleNamespace(
        _remove_native_batch=lambda: calls.append("remove")
    )
    panel._particle_preview_object = object()

    panel._discard_invalid_particle_preview()

    assert panel._particle_preview_component is None
    assert calls == []


def test_particle_preview_component_controls_are_hard_disabled_in_play(monkeypatch):
    from Infernux.engine.play_mode import PlayModeManager

    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: SimpleNamespace(is_edit_mode=False)),
    )
    component = ParticleSystem()
    component._editor_preview_active = True
    component._editor_preview_play_requested = True
    calls = []
    component.pause = lambda: calls.append("pause") or True
    component.stop = lambda: calls.append("stop") or True
    component._remove_native_batch = lambda: calls.append("remove")

    assert component.editor_preview_pause() is False
    assert component.editor_preview_stop() is False
    component.editor_preview_end()
    assert component._editor_preview_emitter_visible(0) is True
    assert calls == []


def test_particle_validate_ignores_identical_inspector_values():
    component = ParticleSystem()
    component._runtime_definition_signature = component._definition_signature()
    component._runtime_rebuild_pending = False
    component._gpu_controllers = [object()]
    calls = []
    component._remove_native_batch = lambda: calls.append("remove")
    component._clear_runtime_state = lambda: calls.append("clear")

    component.on_validate()

    assert component._runtime_rebuild_pending is False
    assert component._gpu_controllers
    assert calls == []


def test_particle_validate_defers_definition_rebuild_to_update():
    from Infernux.core.asset_ref import ParticleGraphRef

    component = ParticleSystem()
    component.graph = ParticleGraphRef("graph-guid", "Assets/Test.particlegraph")
    component._runtime_definition_signature = component._definition_signature()
    component._runtime_rebuild_pending = False
    calls = []
    component._sync_serialized_instance_overrides = lambda: calls.append("sync")
    component._compile_asset = lambda *, force=False: calls.append(
        ("compile", force)
    ) or True
    component._gpu_controllers = [object()]
    component._particle_metadata = object()
    component._update_gpu_particle_graph = lambda delta: calls.append(
        ("update", delta)
    )

    component.random_seed = 17
    component.on_validate()

    assert component._runtime_rebuild_pending is True
    assert calls == []

    component.update(0.25)

    assert component._runtime_rebuild_pending is False
    assert calls == ["sync", ("compile", True), ("update", 0.25)]


def test_particle_preview_entering_play_forgets_handles_without_runtime_command():
    from Infernux.engine.play_mode import PlayModeState

    panel = _panel()
    calls = []
    panel._particle_preview_component = SimpleNamespace(
        editor_preview_end=lambda: calls.append("end")
    )
    panel._particle_preview_object = object()
    panel._particle_preview_prepared = True

    panel._on_particle_preview_play_mode_changed(
        SimpleNamespace(
            old_state=PlayModeState.EDIT,
            new_state=PlayModeState.PLAYING,
        )
    )

    assert panel._particle_preview_component is None
    assert panel._particle_preview_object is None
    assert calls == []


def test_scene_panel_subscribes_to_play_mode_lifecycle():
    panel = _panel()
    listeners = []
    manager = SimpleNamespace(
        is_edit_mode=True,
        add_state_change_listener=lambda callback: listeners.append(callback),
        remove_state_change_listener=lambda callback: listeners.remove(callback),
    )
    panel._play_mode_manager = None
    panel._enable_called = True

    panel.set_play_mode_manager(manager)
    assert listeners == [panel._on_particle_preview_play_mode_changed]

    panel.set_play_mode_manager(None)
    assert listeners == []


def test_prefab_exit_overlay_is_semantic_and_clickable(monkeypatch):
    panel = _panel()
    panel._draw_gizmo_overlay = lambda _ctx: False
    panel._draw_pos_overlay = lambda *_args: None
    panel._tick_particle_preview = lambda _delta: None
    panel._draw_particle_preview_overlay = lambda *_args: False

    exits = []
    manager = SimpleNamespace(
        is_prefab_mode=True,
        exit_prefab_mode_with_undo=lambda: exits.append(True),
    )
    monkeypatch.setattr(SceneFileManager, "instance", classmethod(lambda _cls: manager))

    semantics = []

    class Context:
        def set_cursor_pos_x(self, _value):
            pass

        def set_cursor_pos_y(self, _value):
            pass

        def push_style_color(self, *_args):
            pass

        def pop_style_color(self, _count=1):
            pass

        def button(self, _label):
            return True

        def record_semantic_item(self, kind, label, enabled, semantic_id):
            semantics.append((kind, label, enabled, semantic_id))

        def is_item_hovered(self):
            return False

        def is_mouse_button_down(self, _button):
            return False

        def want_text_input(self):
            return False

        def is_key_pressed(self, _key):
            return False

        def is_key_down(self, _key):
            return False

    panel._render_overlays_and_shortcuts(Context(), None, 0.0, 0.0, 640.0, 480.0, 0.016)

    assert exits == [True]
    assert len(semantics) == 1
    assert semantics[0][0] == "button"
    assert semantics[0][1]
    assert semantics[0][2:] == (True, "scene_view.prefab.exit")
