from __future__ import annotations

import pytest

from Infernux.engine.engine import Engine


class _NativeEngine:
    def __init__(self) -> None:
        self.present_modes: list[int] = []
        self.fps_caps: list[float] = []

    def set_present_mode(self, mode: int) -> None:
        self.present_modes.append(mode)

    def set_play_fps_cap(self, fps: float) -> None:
        self.fps_caps.append(fps)


def _engine() -> tuple[Engine, _NativeEngine]:
    wrapper = Engine.__new__(Engine)
    native = _NativeEngine()
    wrapper._engine = native
    return wrapper, native


def test_startup_render_timing_applies_current_environment_contract(monkeypatch) -> None:
    wrapper, native = _engine()
    monkeypatch.setenv("INFERNUX_PRESENT_MODE", "mailbox")
    monkeypatch.setenv("INFERNUX_PLAYER_FPS_CAP", "144")

    wrapper._apply_startup_present_mode()
    wrapper._apply_startup_play_fps_cap()

    assert native.present_modes == [1]
    assert native.fps_caps == [144.0]


@pytest.mark.parametrize("value", ["", "adaptive", "4", "-1"])
def test_startup_present_mode_rejects_invalid_values(monkeypatch, value: str) -> None:
    wrapper, native = _engine()
    monkeypatch.setenv("INFERNUX_PRESENT_MODE", value)

    with pytest.raises(ValueError, match="INFERNUX_PRESENT_MODE"):
        wrapper._apply_startup_present_mode()

    assert native.present_modes == []


@pytest.mark.parametrize("value", ["", "fast", "-1", "nan", "inf"])
def test_startup_fps_cap_rejects_invalid_values(monkeypatch, value: str) -> None:
    wrapper, native = _engine()
    monkeypatch.setenv("INFERNUX_PLAYER_FPS_CAP", value)

    with pytest.raises(ValueError, match="INFERNUX_PLAYER_FPS_CAP"):
        wrapper._apply_startup_play_fps_cap()

    assert native.fps_caps == []
