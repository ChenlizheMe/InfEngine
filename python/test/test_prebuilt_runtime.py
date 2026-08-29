from pathlib import Path

from Infernux.engine import prebuilt_runtime


def test_player_host_path_prefers_explicit_build_artifact(monkeypatch, tmp_path):
    host = tmp_path / "InfernuxPlayerHost.exe"
    monkeypatch.setenv("INFERNUX_PLAYER_HOST_PATH", str(host))

    assert prebuilt_runtime._player_host_path() == host


def test_player_host_path_falls_back_to_package_resources(monkeypatch, tmp_path):
    resources = tmp_path / "resources"
    monkeypatch.delenv("INFERNUX_PLAYER_HOST_PATH", raising=False)
    monkeypatch.setattr(prebuilt_runtime, "get_package_resources_path", lambda: str(resources))

    assert prebuilt_runtime._player_host_path() == (
        resources / "player_runtime" / "InfernuxPlayerHost.exe"
    )
