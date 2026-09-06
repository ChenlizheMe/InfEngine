from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_managed_player_control_keeps_native_window_hidden() -> None:
    source = (
        ROOT / "cpp/infernux/platform/window/InxView.cpp"
    ).read_text(encoding="utf-8")
    start = source.index("void InxView::Show()")
    end = source.index("bool InxView::PumpStartupEvents()", start)
    body = source[start:end]
    assert '_INFERNUX_PLAYER_CONTROL_FILE' in body
    assert body.index('_INFERNUX_PLAYER_CONTROL_FILE') < body.index("SDL_ShowWindow")
