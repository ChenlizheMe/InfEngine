"""Graphical-mode manual stepping.

Headless automation drives frames through ``engine.tick(dt)``. With a window
open the same call must stay usable: one tick simulates AND renders exactly one
frame with the caller-provided delta time, so headless scripts keep working
unmodified when a renderer is attached. See Infernux::Tick.
"""
from __future__ import annotations

import pytest


def test_graphical_tick_advances_one_rendered_frame_with_exact_delta(engine, scene):
    observed: list[float] = []

    engine.set_pre_scene_update_callback(observed.append)
    try:
        engine.tick(1.0 / 60.0)
        # Larger than DrawFrame's 1/3 s wall-clock clamp: the manual override
        # must win so deterministic stepping is not silently rescaled.
        engine.tick(0.5)
    finally:
        engine.set_pre_scene_update_callback(None)

    assert observed == [pytest.approx(1.0 / 60.0), pytest.approx(0.5)]


def test_graphical_tick_rejects_invalid_delta(engine):
    with pytest.raises(ValueError):
        engine.tick(-0.1)
    with pytest.raises(ValueError):
        engine.tick(float("nan"))
