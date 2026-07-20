from __future__ import annotations

import json

from Infernux.mcp import session
from Infernux.mcp.tools import player


class _FakeMcp:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


class _Supervisor:
    def __init__(self) -> None:
        self.calls = []
        self.launch_status = {"player_running": True, "player_ready": True}

    def launch_player(self, executable, **kwargs):
        self.calls.append(("launch", executable, kwargs))
        return dict(self.launch_status)

    def status(self):
        return {"player_running": True, "player_ready": True}

    def player_observe(self, names, **kwargs):
        self.calls.append(("observe", names, kwargs))
        return {"scene_name": "RaceTrack", "objects": {}}

    def player_send_key(self, key, pressed, **kwargs):
        self.calls.append(("key", key, pressed, kwargs))
        return {"delivered": True, "sequence": 9}

    def player_press_key(self, key, duration_seconds, **kwargs):
        self.calls.append(("press", key, duration_seconds, kwargs))
        return {"delivered": True, "sequence": 10, "actual_duration_seconds": duration_seconds}

    def player_motion_capture_arm(self, names, **kwargs):
        self.calls.append(("motion_capture_arm", names, kwargs))
        return {"capture_id": "player-motion-test", "status": "armed"}

    def player_motion_capture_status(self, capture_id, **kwargs):
        self.calls.append(("motion_capture_status", capture_id, kwargs))
        return {"capture_id": capture_id, "status": "completed", "terminal": True}

    def player_motion_capture_cancel(self, capture_id, **kwargs):
        self.calls.append(("motion_capture_cancel", capture_id, kwargs))
        return {"capture_id": capture_id, "status": "cancelled", "cancelled": True}

    def player_read_logs(self, **kwargs):
        return {"runtime_lines": [], "stdout_lines": []}

    def stop_player(self, **kwargs):
        self.calls.append(("shutdown", kwargs))
        return {"stopped": True, "player_running": False}


def _configure(tmp_path, monkeypatch, *, build_profile="debug_feedback"):
    project = tmp_path / "PlayerProject"
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True)
    output = tmp_path / "PlayerBuild"
    (settings / "BuildSettings.json").write_text(json.dumps({
        "game_name": "Pilot",
        "output_dir": str(output),
    }), encoding="utf-8")
    session.configure(str(project), {
        "profile": "global_validation",
        "session": {"session_id": "player-tools", "build_profile": build_profile},
    })
    supervisor = _Supervisor()
    monkeypatch.setattr(player.SupervisorSession, "resume", lambda *_args, **_kwargs: supervisor)
    mcp = _FakeMcp()
    player.register_player_tools(mcp, str(project))
    return mcp, supervisor, output


def test_player_validation_tools_proxy_only_constrained_operations(tmp_path, monkeypatch):
    mcp, supervisor, output = _configure(tmp_path, monkeypatch)

    launched = mcp.tools["player_validation_launch"]()
    observed = mcp.tools["player_validation_observe"](
        ["PlayerCar"],
        [{"object_name": "PlayerCar", "component_type": "RaceHUDController", "fields": ["current_speed_kph"]}],
        include_scene_objects=True,
        discovery_component_types=["SceneNavigationController"],
        max_discovered_objects=12,
    )
    keyed = mcp.tools["player_validation_key"]("W", True)
    pressed = mcp.tools["player_validation_press"]("W", 0.5)
    armed = mcp.tools["player_validation_motion_capture_arm"](
        ["PlayerCar"],
        seconds=1.5,
        trigger_scene_name="racetrack",
        hold_keys=["W", "A"],
        hold_mouse_buttons=[1],
        mouse_x=640.0,
        mouse_y=360.0,
        hold_frame_count=90,
        wait_frame_count=30,
        pause_on_complete=True,
        component_probes=[{
            "object_name": "PlayerCar",
            "component_type": "RaceHUDController",
            "fields": ["current_speed_kph"],
        }],
        stop_assertions=[{
            "kind": "component_field",
            "object_name": "PlayerCar",
            "component_type": "RaceHUDController",
            "field": "current_speed_kph",
            "operator": "greater_or_equal",
            "value": 40.0,
        }],
        stop_mode="any",
        pause_on_condition=True,
    )
    captured = mcp.tools["player_validation_motion_capture_status"]("player-motion-test")
    cancelled = mcp.tools["player_validation_motion_capture_cancel"]("player-motion-test")
    stopped = mcp.tools["player_validation_shutdown"]()

    assert launched["data"]["player_ready"] is True
    assert supervisor.calls[0][0:2] == ("launch", str(output / "Pilot.exe"))
    assert observed["data"]["scene_name"] == "RaceTrack"
    assert supervisor.calls[1][2]["component_probes"][0]["fields"] == ["current_speed_kph"]
    assert supervisor.calls[1][2]["include_scene_objects"] is True
    assert supervisor.calls[1][2]["discovery_component_types"] == ["SceneNavigationController"]
    assert supervisor.calls[1][2]["max_discovered_objects"] == 12
    assert keyed["data"]["delivered"] is True
    assert pressed["data"]["actual_duration_seconds"] == 0.5
    assert armed["data"] == {"capture_id": "player-motion-test", "status": "armed"}
    assert supervisor.calls[4][0:2] == ("motion_capture_arm", ["PlayerCar"])
    assert supervisor.calls[4][2]["trigger_scene_name"] == "racetrack"
    assert supervisor.calls[4][2]["component_probes"][0]["fields"] == ["current_speed_kph"]
    assert supervisor.calls[4][2]["hold_keys"] == ["W", "A"]
    assert supervisor.calls[4][2]["hold_mouse_buttons"] == [1]
    assert supervisor.calls[4][2]["mouse_x"] == 640.0
    assert supervisor.calls[4][2]["mouse_y"] == 360.0
    assert supervisor.calls[4][2]["hold_frame_count"] == 90
    assert supervisor.calls[4][2]["wait_frame_count"] == 30
    assert supervisor.calls[4][2]["pause_on_complete"] is True
    assert supervisor.calls[4][2]["stop_assertions"][0]["field"] == "current_speed_kph"
    assert supervisor.calls[4][2]["stop_mode"] == "any"
    assert supervisor.calls[4][2]["pause_on_condition"] is True
    assert captured["data"]["terminal"] is True
    assert cancelled["data"]["cancelled"] is True
    assert stopped["data"]["stopped"] is True


def test_player_validation_tools_reject_release_profile(tmp_path, monkeypatch):
    mcp, _supervisor, _output = _configure(tmp_path, monkeypatch, build_profile="release_exploration")

    result = mcp.tools["player_validation_status"]()

    assert result["ok"] is False
    assert result["error"]["code"] == "error.player_validation"


def test_player_validation_launch_returns_startup_logs_on_readiness_failure(tmp_path, monkeypatch):
    mcp, supervisor, _output = _configure(tmp_path, monkeypatch)
    supervisor.launch_status = {
        "player_running": False,
        "player_ready": False,
        "player_exit_code": 1,
        "ready_error": "Player exited before readiness with code 1.",
    }

    result = mcp.tools["player_validation_launch"]()

    assert result["ok"] is False
    assert result["error"]["code"] == "error.player_startup"
    assert result["data"]["player_exit_code"] == 1
    assert result["logs"] == {"runtime_lines": [], "stdout_lines": []}
