from Infernux.application import Application, _renderer_state_from_native


class _Native:
    renderer_frame_snapshot = {
        "frame": 12,
        "game_camera_available": True,
        "game_target_ready": True,
        "game_draw_call_count": 3,
    }
    gpu_residency_snapshot = {"tracked_bytes": 4096}
    msaa_state = {"active_samples": 4}


class _Engine:
    def __init__(self):
        self.exit_requests = 0

    @staticmethod
    def get_native_engine():
        return _Native()

    def request_exit(self):
        self.exit_requests += 1


def test_renderer_state_has_shared_editor_player_schema():
    state = _renderer_state_from_native(_Native())

    assert state == {
        "frame": _Native.renderer_frame_snapshot,
        "gpu_residency": {"tracked_bytes": 4096},
        "msaa": {"active_samples": 4},
        "submission_ready": True,
    }


def test_editor_quit_is_ignored():
    engine = _Engine()
    Application._bind_engine(engine, "editor")

    assert Application.is_editor() is True
    assert Application.is_player() is False
    assert Application.quit(7) is False
    assert engine.exit_requests == 0
    assert Application._requested_exit_code() == 0

    Application._unbind_engine(engine)


def test_player_quit_requests_exit_and_keeps_exit_code():
    engine = _Engine()
    Application._bind_engine(engine, "player")

    assert Application.is_player() is True
    assert Application.renderer_state()["frame"]["frame"] == 12
    assert Application.quit(7) is True
    assert engine.exit_requests == 1
    assert Application._requested_exit_code() == 7

    Application._unbind_engine(engine)
    assert Application.is_player() is False
    assert Application._requested_exit_code() == 7


def test_data_path_uses_active_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "Infernux.engine.project_context.get_project_root",
        lambda: str(tmp_path),
    )

    assert Application.data_path() == str(tmp_path.resolve())
