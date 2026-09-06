from __future__ import annotations

from types import SimpleNamespace

import Infernux.screen as screen_module
from Infernux.screen import Insets, Rect, Screen


class _Manager:
    screen_state = SimpleNamespace(
        revision=7,
        logical_width=1080,
        logical_height=2400,
        framebuffer_width=2160,
        framebuffer_height=4800,
        pixel_ratio=2.0,
        safe_area_x=30,
        safe_area_y=80,
        safe_area_width=1020,
        safe_area_height=2200,
        keyboard_inset=720,
        keyboard_inset_known=True,
        focused=True,
        occluded=False,
    )


class _Native:
    @staticmethod
    def instance():
        return _Manager()


def test_screen_exposes_safe_area_framebuffer_and_keyboard(monkeypatch):
    monkeypatch.setattr(screen_module, "_NativeInputManager", _Native)

    assert Screen.revision == 7
    assert Screen.size == (1080, 2400)
    assert Screen.framebuffer_size == (2160, 4800)
    assert Screen.pixel_ratio == 2.0
    assert Screen.safe_area == Rect(30, 80, 1020, 2200)
    assert Screen.safe_insets == Insets(30, 80, 30, 120)
    assert Screen.keyboard_inset == 720
    assert Screen.focused
    assert not Screen.occluded


def test_screen_reports_unknown_keyboard_inset_as_none(monkeypatch):
    state = SimpleNamespace(**vars(_Manager.screen_state))
    state.keyboard_inset_known = False
    manager = SimpleNamespace(screen_state=state)
    native = SimpleNamespace(instance=lambda: manager)
    monkeypatch.setattr(screen_module, "_NativeInputManager", native)

    assert Screen.keyboard_inset is None
