"""Unit tests for editor theme colour-space utilities and tokens.

Covers the sRGB→linear conversion, the ``srgb3`` / ``hex_to_linear`` helpers,
token presence/shape, and the native theme-registry switch API.
"""
from __future__ import annotations

import pytest

from Infernux.engine.ui.theme import (
    Theme,
    srgb_to_linear,
    srgb3,
    hex_to_linear,
    list_editor_themes,
    active_editor_theme,
)


# ── srgb_to_linear ──────────────────────────────────────────────────────────

def test_srgb_to_linear_zero():
    assert srgb_to_linear(0.0) == 0.0


def test_srgb_to_linear_one():
    assert srgb_to_linear(1.0) == pytest.approx(1.0)


def test_srgb_to_linear_low_segment_is_linear():
    # Below the 0.04045 threshold it is a plain /12.92 division.
    assert srgb_to_linear(0.02) == pytest.approx(0.02 / 12.92)


def test_srgb_to_linear_threshold():
    assert srgb_to_linear(0.04045) == pytest.approx(0.04045 / 12.92, abs=1e-6)


def test_srgb_to_linear_midpoint_known_value():
    assert srgb_to_linear(0.5) == pytest.approx(0.21404, abs=1e-4)


def test_srgb_to_linear_is_monotonic():
    prev = -1.0
    for i in range(0, 101):
        v = srgb_to_linear(i / 100.0)
        assert v >= prev
        prev = v


def test_srgb_to_linear_darkens_midtones():
    # sRGB encoding means linear value is below the sRGB value in the midrange.
    assert srgb_to_linear(0.5) < 0.5


# ── srgb3 ───────────────────────────────────────────────────────────────────

def test_srgb3_returns_four_components():
    c = srgb3(0.5, 0.5, 0.5)
    assert len(c) == 4


def test_srgb3_default_alpha():
    assert srgb3(0.0, 0.0, 0.0)[3] == 1.0


def test_srgb3_alpha_passthrough():
    assert srgb3(0.0, 0.0, 0.0, 0.3)[3] == 0.3


def test_srgb3_converts_each_channel():
    c = srgb3(1.0, 0.0, 0.5)
    assert c[0] == pytest.approx(1.0)
    assert c[1] == pytest.approx(0.0)
    assert c[2] == pytest.approx(srgb_to_linear(0.5))


# ── hex_to_linear ───────────────────────────────────────────────────────────

def test_hex_to_linear_black():
    assert hex_to_linear(0, 0, 0) == (0.0, 0.0, 0.0, 1.0)


def test_hex_to_linear_white():
    c = hex_to_linear(255, 255, 255)
    assert c[0] == pytest.approx(1.0)
    assert c[3] == 1.0


def test_hex_to_linear_alpha():
    assert hex_to_linear(0, 0, 0, 0.5)[3] == 0.5


def test_hex_to_linear_matches_srgb3():
    assert hex_to_linear(128, 128, 128) == srgb3(128 / 255.0, 128 / 255.0, 128 / 255.0)


# ── Theme tokens ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token", [
    "TEXT", "TEXT_DIM", "TEXT_DISABLED", "WINDOW_BG", "FRAME_BG", "HEADER",
    "APPLY_BUTTON", "BORDER",
])
def test_theme_token_exists(token):
    assert hasattr(Theme, token)


@pytest.mark.parametrize("token", [
    "TEXT", "WINDOW_BG", "FRAME_BG", "HEADER", "APPLY_BUTTON",
])
def test_theme_color_token_is_rgba(token):
    value = getattr(Theme, token)
    assert isinstance(value, tuple)
    assert len(value) == 4
    assert all(isinstance(c, (int, float)) for c in value)


def test_theme_color_components_in_range():
    for token in ("TEXT", "WINDOW_BG", "FRAME_BG", "HEADER", "APPLY_BUTTON"):
        for c in getattr(Theme, token):
            assert 0.0 <= c <= 1.0


def test_theme_accent_is_reddish():
    r, g, b, _a = Theme.APPLY_BUTTON
    assert r > g and r > b  # the engine accent is red


def test_theme_apply_button_alpha_opaque():
    assert Theme.APPLY_BUTTON[3] == pytest.approx(1.0)


# ── native theme registry ───────────────────────────────────────────────────

def test_list_editor_themes_returns_list():
    themes = list_editor_themes()
    assert isinstance(themes, list)


def test_active_editor_theme_returns_str():
    assert isinstance(active_editor_theme(), str)


def test_theme_helpers_are_exposed():
    assert callable(Theme.set_theme)
    assert callable(Theme.list_themes)
    assert callable(Theme.active_theme)
    assert callable(Theme.refresh)


def test_native_theme_failures_are_not_replaced_with_empty_values(monkeypatch):
    import Infernux.lib as native

    def fail():
        raise RuntimeError("native theme registry unavailable")

    monkeypatch.setattr(native, "list_editor_themes", fail)
    monkeypatch.setattr(native, "get_editor_theme", fail)

    with pytest.raises(RuntimeError, match="native theme registry unavailable"):
        list_editor_themes()
    with pytest.raises(RuntimeError, match="native theme registry unavailable"):
        active_editor_theme()
