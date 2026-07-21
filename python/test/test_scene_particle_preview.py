from __future__ import annotations

from types import SimpleNamespace

from Infernux.components import ParticleSystem
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

    panel._particle_preview_speed = 1.5
    panel._tick_particle_preview(0.02)
    assert calls == [(0.02, 1.5)]

    panel._play_mode_manager.is_edit_mode = False
    component.editor_preview_end = lambda: calls.append("end")
    panel._tick_particle_preview(0.02)
    assert calls[-1] == "end"
