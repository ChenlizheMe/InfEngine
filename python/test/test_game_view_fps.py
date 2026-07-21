from types import SimpleNamespace

import pytest

from Infernux.engine.ui import game_view_panel as game_view_module
from Infernux.engine.ui.game_view_panel import GameViewPanel


class _Context:
    semantic_capture_enabled = False

    @staticmethod
    def calc_text_size(text):
        return float(len(text)), 1.0

    @staticmethod
    def get_window_width():
        return 800.0

    @staticmethod
    def same_line(_offset):
        pass

    def label(self, text):
        self.last_label = text


class _Engine:
    def __init__(self):
        self._engine = SimpleNamespace(
            renderer_frame_snapshot={"frame": 100, "game_only_frame_ms": 0.5}
        )

    @staticmethod
    def get_play_mode_manager():
        return None


def test_game_view_fps_samples_renderer_frames_once_per_second(monkeypatch):
    times = iter((10.0, 10.5, 11.0, 12.0))
    monkeypatch.setattr(game_view_module, "_pc", lambda: next(times))
    engine = _Engine()
    panel = GameViewPanel(engine=engine)
    panel._GameViewPanel__is_playing = True
    context = _Context()

    panel._render_fps_counter(context)
    engine._engine.renderer_frame_snapshot = {"frame": 600, "game_only_frame_ms": 0.5}
    panel._render_fps_counter(context)
    assert panel._display_fps == 0.0

    engine._engine.renderer_frame_snapshot = {"frame": 1100, "game_only_frame_ms": 0.5}
    panel._render_fps_counter(context)
    assert panel._display_fps == pytest.approx(1000.0)
    assert panel._display_frame_ms == pytest.approx(1.0)

    engine._engine.renderer_frame_snapshot = {"frame": 2600, "game_only_frame_ms": 0.4}
    panel._render_fps_counter(context)
    assert panel._display_fps == pytest.approx(1500.0)
    assert panel._display_frame_ms == pytest.approx(2.0 / 3.0)
    assert panel._display_game_fps == pytest.approx(2500.0)


def test_game_view_fps_is_hidden_outside_play_mode(monkeypatch):
    monkeypatch.setattr(game_view_module, "_pc", lambda: 10.0)
    panel = GameViewPanel(engine=_Engine())
    context = _Context()

    panel._render_fps_counter(context)

    assert context.last_label == "FPS: --"
    assert panel._fps_sample_time is None
