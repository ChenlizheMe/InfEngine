from __future__ import annotations

import math


def _send_scene_camera_input(engine, *, right_down: bool, delta_x: float = 0.0, delta_y: float = 0.0) -> None:
    engine.process_scene_view_input(
        0.0,
        right_down,
        False,
        delta_x,
        delta_y,
        0.0,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )


def test_first_editor_camera_look_delta_does_not_snap_focus(engine):
    camera = engine.editor_camera
    _send_scene_camera_input(engine, right_down=False)
    camera.reset()

    before = camera.focus_point
    try:
        _send_scene_camera_input(engine, right_down=True)
        _send_scene_camera_input(engine, right_down=True, delta_x=1.0)
        after = camera.focus_point
    finally:
        _send_scene_camera_input(engine, right_down=False)
        camera.reset()

    focus_shift = math.dist(
        (before.x, before.y, before.z),
        (after.x, after.y, after.z),
    )
    assert focus_shift < 0.05
