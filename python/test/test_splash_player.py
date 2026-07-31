from __future__ import annotations

from Infernux.engine import splash_player as splash_module
from Infernux.engine.splash_player import SplashPlayer


class _DrawContext:
    def __init__(self):
        self.draws = []

    def draw_image_rect(self, *args):
        self.draws.append(args)


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
        [{"type": "image", "path": "opening.png", "duration": 30.0}],
        str(tmp_path),
    )
    context = _DrawContext()
    native = object()

    player.update(context, native, 0.0, 0.0, 1280.0, 720.0)
    assert context.draws == []
    player.update(context, native, 0.0, 0.0, 1280.0, 720.0)

    assert len(calls) == 2
    assert all(call[4]["pump"] is True and call[4]["srgb"] is True for call in calls)
    assert context.draws and context.draws[-1][0] == 42
    assert player._img_w == 640 and player._img_h == 360
