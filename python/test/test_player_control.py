from __future__ import annotations

import json

from Infernux.engine import player_control
from Infernux.engine.player_control import PlayerControlChannel
from Infernux.engine.player_gui import PlayerGUI
from Infernux.input import Input


class _Native:
    def __init__(self) -> None:
        self.last_processed_synthetic_input_sequence = 0
        self.pending_synthetic_input_count = 0
        self.keys: list[tuple[int, bool, bool]] = []

    def queue_synthetic_key_input(self, scancode: int, pressed: bool, repeat: bool) -> int:
        self.keys.append((scancode, pressed, repeat))
        self.pending_synthetic_input_count = 1
        return 11 + len(self.keys)


class _Engine:
    def __init__(self) -> None:
        self.native = _Native()

    def get_native_engine(self):
        return self.native


def _configure(tmp_path, monkeypatch, *, debug=True):
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    monkeypatch.setenv("_INFERNUX_PLAYER_DEBUG_BUILD", "1" if debug else "0")
    monkeypatch.setenv("_INFERNUX_PLAYER_CONTROL_FILE", str(request))
    monkeypatch.setenv("_INFERNUX_PLAYER_RESPONSE_FILE", str(response))
    monkeypatch.setenv("_INFERNUX_PLAYER_CONTROL_TOKEN", "control-token-123456789")
    return request, response, PlayerControlChannel.from_environment()


def _write_request(path, command_id: str, action: str, **values):
    path.write_text(json.dumps({
        "command_id": command_id,
        "token": "control-token-123456789",
        "action": action,
        **values,
    }), encoding="utf-8")


def test_release_player_does_not_enable_validation_control(tmp_path, monkeypatch):
    _request, _response, channel = _configure(tmp_path, monkeypatch, debug=False)

    assert channel.enabled is False


def test_standalone_player_keeps_gameplay_input_enabled_without_hover(monkeypatch):
    focused: list[bool] = []
    monkeypatch.setattr(Input, "set_game_focused", focused.append)

    player = PlayerGUI.__new__(PlayerGUI)
    player._engine = type("_PlayerEngine", (), {
        "get_native_engine": lambda self: type(
            "_NativeEngine", (), {"is_close_requested": lambda self: False}
        )(),
    })()
    player._control = type("_Control", (), {"poll": lambda self, engine: None})()
    player._tick(None)

    assert focused == [True]


def test_standalone_player_enables_input_before_game_texture_exists(monkeypatch):
    focused: list[bool] = []
    monkeypatch.setattr(Input, "set_game_focused", focused.append)

    class _PlayerEngine:
        def get_native_engine(self):
            return type("_NativeEngine", (), {"is_close_requested": lambda self: False})()

    player = PlayerGUI.__new__(PlayerGUI)
    player._engine = _PlayerEngine()
    player._control = type("_Control", (), {"poll": lambda self, engine: None})()

    player._tick(None)

    assert focused == [True]


def test_player_control_observation_is_token_authenticated(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    monkeypatch.setattr(
        player_control,
        "_observe_player",
        lambda _engine, names, probes=None, **kwargs: {
            "scene_name": "MainMenu",
            "names": names,
            "probes": probes or [],
            "discovery": kwargs,
        },
    )
    _write_request(request, "observe-1", "observe", object_names=["PlayerCar"])

    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["data"] == {
        "scene_name": "MainMenu",
        "names": ["PlayerCar"],
        "probes": [],
        "discovery": {
            "include_scene_objects": False,
            "discovery_component_types": [],
            "max_discovered_objects": 32,
        },
    }

    _write_request(request, "observe-2", "observe", object_names=[], token="wrong-token")
    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["error"] == "control token mismatch"


def test_player_control_waits_until_sdl_key_delivery(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    _write_request(request, "key-1", "key", scancode=26, pressed=True, repeat=False)

    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False)]
    assert not response.exists()

    engine.native.last_processed_synthetic_input_sequence = 12
    engine.native.pending_synthetic_input_count = 0
    input_manager = type("_InputManager", (), {
        "get_key": lambda self, scancode: scancode == 26,
        "get_key_down": lambda self, scancode: scancode == 26,
        "get_key_up": lambda self, scancode: False,
    })()
    monkeypatch.setattr("Infernux.lib.InputManager.instance", lambda: input_manager)
    monkeypatch.setattr(Input, "is_game_focused", lambda: True)
    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["data"] == {
        "sequence": 12,
        "delivered": True,
        "scancode": 26,
        "game_focused": True,
        "held": True,
        "down": True,
        "up": False,
        "pending_input_count": 0,
    }


def test_player_control_press_owns_hold_duration_inside_player(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    samples = iter((
        {"scene_name": "racetrack", "renderer_frame": 10, "objects": {"PlayerCar": {"position": [0, 0, 0]}}},
        {"scene_name": "racetrack", "renderer_frame": 13, "objects": {"PlayerCar": {"position": [0, 0, 3]}}},
        {"scene_name": "results", "renderer_frame": 14, "objects": {}},
    ))
    monkeypatch.setattr(player_control, "_observe_motion_state", lambda _engine, _names, _probes=None: next(samples))
    _write_request(
        request,
        "press-1",
        "press",
        scancode=26,
        duration_seconds=0.5,
        object_names=["PlayerCar"],
    )

    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False)]

    clock = iter((10.0, 10.25, 10.5))
    monkeypatch.setattr(player_control.time, "monotonic", lambda: next(clock))
    engine.native.last_processed_synthetic_input_sequence = 12
    assert channel.poll(engine) is None
    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False)]
    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False), (26, False, False)]

    engine.native.last_processed_synthetic_input_sequence = 13
    engine.native.pending_synthetic_input_count = 0
    input_manager = type("_InputManager", (), {
        "get_key": lambda self, scancode: False,
        "get_key_down": lambda self, scancode: False,
        "get_key_up": lambda self, scancode: scancode == 26,
    })()
    monkeypatch.setattr("Infernux.lib.InputManager.instance", lambda: input_manager)
    monkeypatch.setattr(Input, "is_game_focused", lambda: True)

    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["data"] == {
        "sequence": 13,
        "delivered": True,
        "scancode": 26,
        "game_focused": True,
        "held": False,
        "down": False,
        "up": True,
        "pending_input_count": 0,
        "down_sequence": 12,
        "requested_duration_seconds": 0.5,
        "actual_duration_seconds": 0.5,
        "initial_observation": {
            "scene_name": "racetrack",
            "renderer_frame": 10,
            "objects": {"PlayerCar": {"position": [0, 0, 0]}},
        },
        "final_observation": {"scene_name": "results", "renderer_frame": 14, "objects": {}},
        "last_same_scene_observation": {
            "scene_name": "racetrack",
            "renderer_frame": 13,
            "objects": {"PlayerCar": {"position": [0, 0, 3]}},
        },
    }

    _write_request(request, "press-nan", "press", scancode=26, duration_seconds=float("nan"))
    assert channel.poll(engine) is None
    rejected = json.loads(response.read_text(encoding="utf-8"))
    assert rejected["ok"] is False
    assert "duration_seconds" in rejected["error"]


def test_player_control_capture_starts_when_target_scene_objects_are_ready(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    now = [5.0]
    scene_name = ["MainMenu"]
    monkeypatch.setattr(player_control.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player_control, "_active_scene_name", lambda: scene_name[0])

    def observe_motion(_engine, names, probes):
        assert names == ["PlayerCar", "NpcRacer"]
        assert probes[0]["fields"] == ["current_speed_kph"]
        offset = max(0.0, now[0] - 6.0)
        return {
            "scene_name": scene_name[0],
            "renderer_frame": int(100 + offset * 100),
            "objects": {
                "PlayerCar": {"position": [0.0, 0.5, 0.0]},
                "NpcRacer": {"position": [2.0, 0.5, offset * 4.0]},
            },
        }

    monkeypatch.setattr(player_control, "_observe_motion_state", observe_motion)
    _write_request(
        request,
        "capture-arm",
        "motion_capture_arm",
        object_names=["PlayerCar", "NpcRacer"],
        component_probes=[{
            "object_name": "PlayerCar",
            "component_type": "RaceHUDController",
            "fields": ["current_speed_kph"],
        }],
        seconds=0.2,
        sample_interval=0.1,
        trigger_scene_name="racetrack",
        trigger_timeout=30.0,
    )

    assert channel.poll(engine) is None
    armed = json.loads(response.read_text(encoding="utf-8"))["data"]
    capture_id = armed["capture_id"]
    assert armed["status"] == "armed"
    assert armed["initial_scene_name"] == "MainMenu"
    response.unlink()

    now[0] = 5.5
    assert channel.poll(engine) is None
    assert not response.exists()

    scene_name[0] = "racetrack"
    now[0] = 6.0
    assert channel.poll(engine) is None
    now[0] = 6.11
    assert channel.poll(engine) is None
    now[0] = 6.22
    assert channel.poll(engine) is None

    _write_request(request, "capture-status", "motion_capture_status", capture_id=capture_id)
    assert channel.poll(engine) is None
    completed = json.loads(response.read_text(encoding="utf-8"))["data"]
    assert completed["status"] == "completed"
    assert completed["terminal"] is True
    assert completed["actual_scene_name"] == "racetrack"
    assert completed["missing_object_names"] == []
    assert completed["sample_count"] == 3
    assert [round(sample["time"], 2) for sample in completed["trajectory"]] == [0.0, 0.11, 0.2]
    assert completed["trajectory"][0]["objects"]["NpcRacer"]["position"] == [2.0, 0.5, 0.0]
    assert completed["trajectory"][-1]["objects"]["NpcRacer"]["position"][2] > 0.8


def test_player_control_capture_owns_frame_bounded_input_and_pauses(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    now = [5.0]
    scene_name = ["MainMenu"]
    frame = [0]
    paused = []
    monkeypatch.setattr(player_control.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player_control, "_active_scene_name", lambda: scene_name[0])
    monkeypatch.setattr(player_control, "_time_frame_count", lambda: frame[0])
    monkeypatch.setattr(player_control, "_pause_player_scene", lambda: paused.append(True) or True)
    monkeypatch.setattr(
        player_control,
        "_observe_motion_state",
        lambda _engine, _names, _probes: {
            "scene_name": scene_name[0],
            "renderer_frame": 100 + frame[0],
            "objects": {"PlayerCar": {"position": [0.0, 0.5, float(frame[0])]}}},
    )
    _write_request(
        request,
        "frame-capture-arm",
        "motion_capture_arm",
        object_names=["PlayerCar"],
        seconds=1.0,
        sample_interval=0.02,
        trigger_scene_name="racetrack",
        hold_scancodes=[26, 4],
        hold_frame_count=2,
        wait_frame_count=2,
        pause_on_complete=True,
    )

    assert channel.poll(engine) is None
    capture_id = json.loads(response.read_text(encoding="utf-8"))["data"]["capture_id"]
    response.unlink()

    scene_name[0] = "racetrack"
    frame[0] = 20
    now[0] = 6.0
    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False), (4, True, False)]

    frame[0] = 21
    now[0] = 6.03
    assert channel.poll(engine) is None
    frame[0] = 22
    now[0] = 6.06
    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False), (4, True, False), (4, False, False), (26, False, False)]

    engine.native.last_processed_synthetic_input_sequence = 15
    frame[0] = 24
    now[0] = 6.12
    assert channel.poll(engine) is None
    assert paused == [True]

    _write_request(request, "frame-capture-status", "motion_capture_status", capture_id=capture_id)
    assert channel.poll(engine) is None
    completed = json.loads(response.read_text(encoding="utf-8"))["data"]
    assert completed["status"] == "completed"
    assert completed["frame_count"] == 4
    assert completed["hold_frame_count"] == 2
    assert completed["wait_frame_count"] == 2
    assert completed["elapsed_frame_count"] == 4
    assert completed["input_released_after_hold_frame"] == 2
    assert completed["paused_on_complete"] is True
    assert [item["scancode"] for item in completed["input_releases"]] == [4, 26]


def test_player_control_capture_stops_on_sampled_public_condition(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    now = [5.0]
    scene_name = ["MainMenu"]
    frame = [0]
    paused = []
    samples = iter((
        {"scene_name": "racetrack", "renderer_frame": 20, "objects": {"PlayerCar": {
            "position": [0.0, 0.5, 0.0],
            "component_fields": {"RaceHUDController[0]": {"player_progress": 0.1}},
        }}},
        {"scene_name": "racetrack", "renderer_frame": 21, "objects": {"PlayerCar": {
            "position": [0.0, 0.5, 2.0],
            "component_fields": {"RaceHUDController[0]": {"player_progress": 0.8}},
        }}},
    ))
    monkeypatch.setattr(player_control.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player_control, "_active_scene_name", lambda: scene_name[0])
    monkeypatch.setattr(player_control, "_time_frame_count", lambda: frame[0])
    monkeypatch.setattr(player_control, "_pause_player_scene", lambda: paused.append(True) or True)
    monkeypatch.setattr(player_control, "_observe_motion_state", lambda *_args: next(samples))
    _write_request(
        request,
        "condition-capture-arm",
        "motion_capture_arm",
        object_names=["PlayerCar"],
        seconds=2.0,
        sample_interval=0.02,
        trigger_scene_name="racetrack",
        hold_scancodes=[26],
        hold_frame_count=120,
        wait_frame_count=2,
        wait_seconds=0.1,
        pause_on_complete=False,
        pause_on_condition=True,
        component_probes=[{
            "object_name": "PlayerCar",
            "component_type": "RaceHUDController",
            "fields": ["player_progress"],
        }],
        stop_assertions=[{
            "kind": "component_field",
            "object_name": "PlayerCar",
            "component_type": "RaceHUDController",
            "field": "player_progress",
            "operator": "greater_or_equal",
            "value": 0.75,
        }],
    )

    assert channel.poll(engine) is None
    capture_id = json.loads(response.read_text(encoding="utf-8"))["data"]["capture_id"]
    response.unlink()
    scene_name[0] = "racetrack"
    frame[0] = 20
    now[0] = 6.0
    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False)]

    frame[0] = 21
    now[0] = 6.03
    assert channel.poll(engine) is None
    assert engine.native.keys == [(26, True, False), (26, False, False)]

    engine.native.last_processed_synthetic_input_sequence = 13
    frame[0] = 23
    now[0] = 6.06
    assert channel.poll(engine) is None
    assert paused == []

    now[0] = 6.14
    assert channel.poll(engine) is None
    _write_request(request, "condition-capture-status", "motion_capture_status", capture_id=capture_id)
    assert channel.poll(engine) is None
    completed = json.loads(response.read_text(encoding="utf-8"))["data"]
    assert completed["status"] == "condition_met"
    assert completed["stop_condition"]["passed"] is True
    assert completed["condition_met_at_frame"] == 1
    assert completed["condition_settle_until_frame"] == 3
    assert completed["condition_settle_until_time"] == 6.13
    assert completed["input_released_after_hold_frame"] == 1
    assert completed["paused_on_complete"] is True
    assert paused == [True]


def test_player_control_capture_rejects_late_arm_in_target_scene(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    monkeypatch.setattr(player_control, "_active_scene_name", lambda: "racetrack")
    _write_request(
        request,
        "capture-late",
        "motion_capture_arm",
        object_names=["NpcRacer"],
        trigger_scene_name="RaceTrack",
    )

    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "must be armed before the target scene" in payload["error"]


def test_player_observation_reports_update_dispatch_diagnostics(monkeypatch):
    class _Proxy:
        overrides_update = True
        has_coroutine_scheduler = False
        update_dispatch_count = 41
        update_forward_count = 41

    class RaceHUDController:
        enabled = True
        _awake_called = True
        _has_started = True
        _load_requested = False
        destination_scene = "RaceTrack"
        trigger_key = "space"
        current_speed_kph = 42.5
        current_rank = 1
        _is_broken = False
        _cpp_component = _Proxy()

        def update(self, _delta_time):
            return None

    class _Transform:
        position = type("_V", (), {"x": 0.0, "y": 1.0, "z": 2.0})()
        euler_angles = type("_V", (), {"x": 3.0, "y": 4.0, "z": 5.0})()

    obj = type("_Object", (), {
        "id": 7,
        "active": True,
        "transform": _Transform(),
        "get_py_components": lambda self: [RaceHUDController()],
    })()
    scene = type("_Scene", (), {"name": "MainMenu", "is_playing": lambda self: True})()
    scene_manager = type("_SceneManager", (), {
        "get_active_scene": lambda self: scene,
        "is_playing": lambda self: True,
        "is_paused": lambda self: False,
    })()
    native = type("_NativeObserve", (), {
        "renderer_frame_snapshot": {},
        "gpu_residency_snapshot": {
            "material_descriptor_set_count": 3,
            "retired_material_descriptor_set_count": 0,
        },
        "msaa_state": {
            "active_samples": 4,
            "supported_samples": [1, 2, 4, 8],
            "scene_target_aligned": True,
            "game_target_aligned": True,
            "material_pipelines_aligned": True,
        },
        "last_processed_synthetic_input_sequence": 0,
        "pending_synthetic_input_count": 0,
    })()
    engine = type("_ObserveEngine", (), {
        "get_native_engine": lambda self: native,
        "get_play_mode_manager": lambda self: None,
    })()

    monkeypatch.setattr("Infernux.lib.SceneManager.instance", lambda: scene_manager)
    monkeypatch.setattr("Infernux.scene.GameObjectQuery.find", lambda name: obj if name == "Prompt" else None)
    monkeypatch.setattr(Input, "is_game_focused", lambda: True)

    data = player_control._observe_player(
        engine,
        ["Prompt"],
        [{
            "object_name": "Prompt",
            "component_type": "RaceHUDController",
            "fields": ["current_speed_kph", "current_rank"],
            "ordinal": 0,
        }],
    )

    assert data["scene_playing"] is True
    assert data["scene_manager_playing"] is True
    assert data["scene_manager_paused"] is False
    assert data["play_state"] == "playing"
    component = data["objects"]["Prompt"]["python_components"][0]
    assert component["update_overridden"] is True
    assert component["load_requested"] is False
    assert component["destination_scene"] == "RaceTrack"
    assert component["trigger_key"] == "space"
    assert component["broken_script"] is False
    assert component["broken_error"] == ""
    assert component["proxy"] == {
        "overrides_update": True,
        "has_coroutine_scheduler": False,
        "update_dispatch_count": 41,
        "update_forward_count": 41,
    }
    assert data["objects"]["Prompt"]["component_fields"] == {
        "RaceHUDController[0]": {"current_speed_kph": 42.5, "current_rank": 1}
    }
    assert data["gpu_residency"] == {
        "material_descriptor_set_count": 3,
        "retired_material_descriptor_set_count": 0,
    }
    assert data["msaa"] == {
        "active_samples": 4,
        "supported_samples": [1, 2, 4, 8],
        "scene_target_aligned": True,
        "game_target_aligned": True,
        "material_pipelines_aligned": True,
    }

    scene_manager.is_paused = lambda: True
    engine.get_play_mode_manager = lambda: type("_PlayManager", (), {
        "state": type("_State", (), {"name": "PLAYING"})(),
    })()
    paused_data = player_control._observe_player(engine, ["Prompt"])

    assert paused_data["play_state"] == "paused"


def test_player_observation_discovers_bounded_objects_by_public_component_type(monkeypatch):
    class SceneNavigationController:
        enabled = True

    class DecorativeComponent:
        enabled = True

    def make_object(object_id, name, component):
        return type("_Object", (), {
            "id": object_id,
            "name": name,
            "active": True,
            "get_components": lambda self: [],
            "get_py_components": lambda self: [component],
        })()

    prompt = make_object(7, "ResultsPrompt", SceneNavigationController())
    decoration = make_object(8, "Confetti", DecorativeComponent())
    scene = type("_Scene", (), {
        "name": "results",
        "is_playing": lambda self: True,
        "get_all_objects": lambda self: [prompt, decoration],
    })()
    scene_manager = type("_SceneManager", (), {
        "get_active_scene": lambda self: scene,
        "is_playing": lambda self: True,
        "is_paused": lambda self: False,
    })()
    native = type("_NativeObserve", (), {
        "renderer_frame_snapshot": {},
        "last_processed_synthetic_input_sequence": 0,
        "pending_synthetic_input_count": 0,
    })()
    engine = type("_ObserveEngine", (), {
        "get_native_engine": lambda self: native,
        "get_play_mode_manager": lambda self: None,
    })()

    monkeypatch.setattr("Infernux.lib.SceneManager.instance", lambda: scene_manager)
    monkeypatch.setattr(Input, "is_game_focused", lambda: True)

    data = player_control._observe_player(
        engine,
        [],
        include_scene_objects=False,
        discovery_component_types=["SceneNavigationController"],
        max_discovered_objects=1,
    )

    assert data["scene_objects"] == [{
        "id": 7,
        "name": "ResultsPrompt",
        "active": True,
        "component_types": ["SceneNavigationController"],
    }]
    assert data["scene_object_match_count"] == 1
    assert data["scene_objects_truncated"] is False


def test_player_control_rejects_private_or_unbounded_scene_discovery(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    _write_request(
        request,
        "observe-private",
        "observe",
        object_names=[],
        discovery_component_types=["_InternalController"],
    )

    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "public component type names" in payload["error"]

    _write_request(
        request,
        "observe-unbounded",
        "observe",
        object_names=[],
        include_scene_objects=True,
        max_discovered_objects=65,
    )
    assert channel.poll(engine) is None
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "between 1 and 64" in payload["error"]


def test_player_control_shutdown_returns_main_thread_action(tmp_path, monkeypatch):
    request, response, channel = _configure(tmp_path, monkeypatch)
    engine = _Engine()
    _write_request(request, "shutdown-1", "shutdown")

    assert channel.poll(engine) == "shutdown"
    payload = json.loads(response.read_text(encoding="utf-8"))
    assert payload["data"]["close_requested"] is True
