import pytest

from Infernux.engine import prebuilt_runtime


def test_player_host_path_prefers_explicit_build_artifact(monkeypatch, tmp_path):
    host = tmp_path / "InfernuxPlayerHost.exe"
    monkeypatch.setenv("INFERNUX_PLAYER_HOST_PATH", str(host))

    assert prebuilt_runtime._player_host_path() == host


def test_player_host_path_requires_the_cmake_build_artifact(monkeypatch):
    monkeypatch.delenv("INFERNUX_PLAYER_HOST_PATH", raising=False)
    with pytest.raises(RuntimeError, match="CMake prebuild_player_runtime"):
        prebuilt_runtime._player_host_path()
