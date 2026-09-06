from __future__ import annotations

import pytest

from Infernux.engine import splash_player as splash_module
from Infernux.engine.splash_player import SplashPlayer


class _DrawContext:
    def __init__(self):
        self.draws = []

    def draw_image_rect(self, *args):
        self.draws.append(args)


class _Native:
    def __init__(self):
        self.full_speed_requests = 0

    def request_full_speed_frame(self):
        self.full_speed_requests += 1


def test_image_splash_polls_async_texture_until_ready(tmp_path, monkeypatch):
    image = tmp_path / "opening.png"
    image.write_bytes(b"image")
    results = iter(((0, 0, 0), (42, 640, 360)))
    calls = []

    def query(native, resource_key, path, stamp, **options):
        calls.append((native, resource_key, path, stamp, options))
        return next(results)

    monkeypatch.setattr(splash_module, "texture_stamp", lambda *_args: 77)
    monkeypatch.setattr(splash_module, "query_or_schedule_texture", query)
    player = SplashPlayer(
        [{
            "type": "image",
            "path": "opening.png",
            "layout": "contain",
            "duration": 30.0,
            "fade_in": 0.0,
            "fade_out": 0.0,
        }],
        str(tmp_path),
    )
    context = _DrawContext()
    native = _Native()

    player.update(context, native, 0.0, 0.0, 1280.0, 720.0)
    assert context.draws == []
    player.update(context, native, 0.0, 0.0, 1280.0, 720.0)

    assert len(calls) == 2
    assert all(call[4]["pump"] is True and call[4]["srgb"] is True for call in calls)
    assert context.draws and context.draws[-1][0] == 42
    assert player._img_w == 640 and player._img_h == 360
    assert native.full_speed_requests == 2


def test_splash_uses_monotonic_time_and_keeps_every_frame_active(
    tmp_path, monkeypatch
):
    image = tmp_path / "opening.png"
    image.write_bytes(b"image")
    times = iter((10.0, 10.1, 10.2))
    monkeypatch.setattr(splash_module._time, "monotonic", lambda: next(times))
    monkeypatch.setattr(splash_module, "texture_stamp", lambda *_args: 77)
    monkeypatch.setattr(
        splash_module,
        "query_or_schedule_texture",
        lambda *_args, **_kwargs: (42, 256, 256),
    )
    player = SplashPlayer(
        [{
            "type": "image",
            "path": "opening.png",
            "layout": "logo",
            "duration": 1.5,
            "fade_in": 0.25,
            "fade_out": 0.25,
        }],
        str(tmp_path),
    )
    context = _DrawContext()
    native = _Native()

    for _ in range(3):
        player.update(context, native, 0.0, 0.0, 3200.0, 1440.0)

    assert native.full_speed_requests == 3
    assert [draw[-1] for draw in context.draws] == pytest.approx([0.0, 0.4, 0.8])


def test_logo_splash_stays_inside_center_safe_area():
    assert SplashPlayer._layout_rect("logo", 3200.0, 1440.0, 256, 256) == (
        1276.0,
        396.0,
        648.0,
        648.0,
    )


def test_splash_rejects_incomplete_previous_shape(tmp_path):
    try:
        SplashPlayer(
            [{"type": "image", "path": "opening.png", "duration": 3.0}],
            str(tmp_path),
        )
    except ValueError as error:
        assert "current exact field set" in str(error)
    else:
        raise AssertionError("Incomplete splash record must fail")
