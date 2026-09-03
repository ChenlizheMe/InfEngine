import pytest

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
        self.capture_requests = []
        self.opened_urls = []

    @staticmethod
    def get_native_engine():
        return _Native()

    def request_exit(self):
        self.exit_requests += 1

    def request_capture(self, source, output_path):
        self.capture_requests.append((source, output_path))
        return 41

    @staticmethod
    def query_capture(capture_id):
        return {"id": capture_id, "status": "completed"}

    @staticmethod
    def cancel_capture(capture_id):
        return capture_id == 41

    def open_url(self, url):
        self.opened_urls.append(url)
        return True


def test_renderer_state_has_shared_editor_player_schema():
    state = _renderer_state_from_native(_Native())

    assert state == {
        "frame": _Native.renderer_frame_snapshot,
        "gpu_residency": {"tracked_bytes": 4096},
        "msaa": {"active_samples": 4},
        "submission_ready": True,
    }


def test_renderer_state_accepts_particle_only_render_graph_submission():
    class _ParticleOnlyNative:
        renderer_frame_snapshot = {
            "frame": 18,
            "game_camera_available": True,
            "game_target_ready": True,
            "game_draw_call_count": 0,
            "game_render_graph_execution_count": 4,
            "game_render_graph_current_executed": True,
        }
        gpu_residency_snapshot = {}
        msaa_state = {}

    state = _renderer_state_from_native(_ParticleOnlyNative())

    assert state["submission_ready"] is True


def test_renderer_state_rejects_target_before_first_graph_execution():
    class _IdleNative:
        renderer_frame_snapshot = {
            "frame": 1,
            "game_camera_available": True,
            "game_target_ready": True,
            "game_draw_call_count": 0,
            "game_render_graph_execution_count": 0,
            "game_render_graph_current_executed": False,
        }
        gpu_residency_snapshot = {}
        msaa_state = {}

    state = _renderer_state_from_native(_IdleNative())

    assert state["submission_ready"] is False


def test_editor_quit_is_ignored():
    engine = _Engine()
    Application._bind_engine(engine, "editor")

    assert Application.is_editor() is True
    assert Application.is_player() is False
    assert Application.quit(7) is False
    assert engine.exit_requests == 0
    assert Application._requested_exit_code() == 0

    Application._unbind_engine(engine)


def test_headless_runtime_kind_is_public_and_distinct():
    engine = _Engine()
    Application._bind_engine(engine, "headless")

    assert Application.is_headless() is True
    assert Application.is_editor() is False
    assert Application.is_player() is False

    Application._unbind_engine(engine)
    assert Application.is_headless() is False


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


def test_asset_path_resolves_editor_asset_and_rejects_outside_file(tmp_path):
    from Infernux.engine.project_context import set_project_root

    asset = tmp_path / "Assets" / "Data" / "cache.npy"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"cache")
    package_asset = tmp_path / "Packages" / "vendor" / "tool" / "runtime" / "server.exe"
    package_asset.parent.mkdir(parents=True)
    package_asset.write_bytes(b"server")
    outside = tmp_path / "Library" / "cache.npy"
    outside.parent.mkdir()
    outside.write_bytes(b"cache")
    set_project_root(str(tmp_path))
    try:
        assert Application.asset_path("Assets/Data/cache.npy") == str(asset.resolve())
        assert Application.asset_path(
            "Packages/vendor/tool/runtime/server.exe"
        ) == str(package_asset.resolve())
        with pytest.raises(FileNotFoundError, match="not available"):
            Application.asset_path(str(outside))
    finally:
        set_project_root(None)


def test_package_path_resolves_verbatim_payload_and_rejects_escape(tmp_path):
    from Infernux.engine.project_context import set_project_root

    payload = tmp_path / "Packages" / "vendor" / "server" / "runtime" / "config.json"
    payload.parent.mkdir(parents=True)
    payload.write_text('{"port": 8080}\n', encoding="utf-8")
    set_project_root(str(tmp_path))
    try:
        assert Application.package_path(
            "vendor/server", "runtime/config.json"
        ) == str(payload.resolve())
        with pytest.raises(ValueError, match="escapes"):
            Application.package_path("vendor/server", "../outside.json")
        with pytest.raises(ValueError, match="relative"):
            Application.package_path("vendor/server", "C:/outside.json")
        with pytest.raises(ValueError, match="reference is invalid"):
            Application.package_path("vendor\\server", "runtime/config.json")
        with pytest.raises(FileNotFoundError, match="not available"):
            Application.package_path("vendor/server", "runtime/missing.json")
    finally:
        set_project_root(None)


def test_open_url_observes_one_canonical_asset_url(tmp_path):
    asset = tmp_path / "Assets" / "Plugins" / "abc" / "web" / "index.html"
    asset.parent.mkdir(parents=True)
    asset.write_text("<button>plugin page</button>", encoding="utf-8")
    engine = _Engine()
    Application._bind_engine(engine, "player")
    try:
        assert Application.open_url(str(asset)) is True
        assert engine.opened_urls == [asset.resolve().as_uri()]
    finally:
        Application._unbind_engine(engine)


def test_open_url_rejects_relative_files_and_unsupported_schemes(tmp_path):
    with pytest.raises(ValueError, match="asset_path"):
        Application.open_url("Assets/Plugins/abc/web/index.html")
    with pytest.raises(ValueError, match="HTTP"):
        Application.open_url("javascript:alert(1)")
    with pytest.raises(FileNotFoundError, match="not available"):
        Application.open_url(str(tmp_path / "missing.html"))


def test_persistent_data_path_uses_project_root_in_editor(monkeypatch, tmp_path):
    engine = _Engine()
    Application._bind_engine(engine, "editor")
    monkeypatch.setattr(
        "Infernux.engine.project_context.get_project_root",
        lambda: str(tmp_path),
    )
    monkeypatch.setenv("_INFERNUX_PLAYER_DATA_ROOT", str(tmp_path / "PlayerData"))

    assert Application.persistent_data_path() == str(tmp_path.resolve())

    Application._unbind_engine(engine)


def test_persistent_data_path_uses_separate_writable_player_root(monkeypatch, tmp_path):
    engine = _Engine()
    Application._bind_engine(engine, "player")
    packaged_root = tmp_path / "PlayerData"
    monkeypatch.setenv("_INFERNUX_PLAYER_DATA_ROOT", str(packaged_root))
    writable_root = tmp_path / "WritableData"
    monkeypatch.setenv("_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT", str(writable_root))

    assert Application.persistent_data_path() == str(writable_root.resolve())

    Application._unbind_engine(engine)


def test_persistent_data_path_rejects_player_without_writable_root(monkeypatch):
    engine = _Engine()
    Application._bind_engine(engine, "player")
    monkeypatch.delenv("_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="writable persistent data root"):
        Application.persistent_data_path()

    Application._unbind_engine(engine)


def test_render_target_capture_uses_engine_and_stays_under_persistent_data(
    monkeypatch, tmp_path
):
    engine = _Engine()
    Application._bind_engine(engine, "player")
    monkeypatch.setenv("_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT", str(tmp_path))
    output = tmp_path / "Logs" / "ribbon.png"

    capture_id = Application.request_render_target_capture("GAME", str(output))

    assert capture_id == 41
    assert engine.capture_requests == [("game", str(output.resolve()))]
    assert Application.query_render_target_capture(capture_id) == {
        "id": 41,
        "status": "completed",
    }
    assert Application.cancel_render_target_capture(capture_id) is True

    with pytest.raises(ValueError, match="persistent_data_path"):
        Application.request_render_target_capture("game", str(tmp_path.parent / "escape.png"))

    Application._unbind_engine(engine)
