from __future__ import annotations

from types import SimpleNamespace

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
    assert calls[-1] == "end"


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
