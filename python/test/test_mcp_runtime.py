from __future__ import annotations

import threading

import pytest

from Infernux.mcp.tools import runtime
from Infernux.mcp.tools.runtime import _collect_input_state, _normalize_play_state


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("edit", "edit"),
        ("Edit Mode", "edit"),
        ("PLAY", "playing"),
        ("play-mode", "playing"),
        ("Paused", "paused"),
        ("pause_mode", "paused"),
    ],
)
def test_runtime_play_state_aliases_are_normalized(value, expected):
    assert _normalize_play_state(value) == expected


def test_runtime_input_probe_snapshot_reports_focus_keys_and_axes():
    class _Input:
        @staticmethod
        def is_game_focused():
            return True

        @staticmethod
        def is_cursor_locked():
            return False

        @staticmethod
        def get_key(key):
            return key == "w"

        @staticmethod
        def get_axis_raw(axis):
            return {"Horizontal": -1.0, "Vertical": 1.0}.get(axis, 0.0)

        @staticmethod
        def get_mouse_frame_state(button):
            assert button == 0
            return (16.0, 24.0, 0.0, -1.0, True, True, False)

    state = _collect_input_state(_Input, ["w", "d"], ["Horizontal", "Vertical"], [0])

    assert state == {
        "game_focused": True,
        "cursor_locked": False,
        "keys": {"w": True, "d": False},
        "axes": {"Horizontal": -1.0, "Vertical": 1.0},
        "mouse_buttons": {
            "0": {
                "position": [16.0, 24.0],
                "scroll": [0.0, -1.0],
                "held": True,
                "down": True,
                "up": False,
            },
        },
    }


def test_runtime_wait_can_wait_for_a_named_scene_after_a_transition(monkeypatch):
    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)
    states = iter([
        {"play_state": "playing", "scene_name": "racetrack"},
        {"play_state": "playing", "scene_name": "results"},
    ])
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, _fn: next(states))
    monkeypatch.setattr(runtime, "_is_runtime_idle", lambda _state: True)

    response = fake.tools["runtime_wait"](
        play_state="playing",
        scene_name="Results",
        timeout_seconds=0.1,
        poll_interval=0.01,
    )

    assert response["ok"] is True
    assert response["data"]["requested_scene_name"] == "Results"
    assert response["data"]["state"]["scene_name"] == "results"


def test_runtime_renderer_state_combines_frame_submission_and_gpu_residency(monkeypatch):
    class _Native:
        renderer_frame_snapshot = {
            "frame": 91,
            "game_camera_available": True,
            "game_target_ready": True,
            "game_draw_call_count": 7,
            "game_shadow_draw_call_count": 3,
            "game_render_graph_name": "Pipeline+Stack",
            "game_render_graph_execution_count": 42,
            "game_render_graph_current_executed": True,
            "game_render_graph_pass_names": ["OpaquePass", "Bloom_Composite"],
            "ui_panel_times_ms": {"project": 0.42, "console": 0.03},
            "ui_panel_sub_times_ms": {"project": {"folderTree": 0.19}},
        }
        gpu_residency_snapshot = {
            "tracked_bytes": 4096,
            "material_pipeline_count": 2,
        }
        msaa_state = {
            "active_samples": 4,
            "supported_samples": [1, 2, 4, 8],
            "scene_target_aligned": True,
            "game_target_aligned": True,
            "material_pipelines_aligned": True,
        }
        preview_task_snapshots = [
            {
                "kind": "material",
                "resource_key": "mat|Assets/Test.mat",
                "generation": 2,
                "ready_generation": 2,
                "texture_id": 123,
            }
        ]
        asset_runtime_records = [object(), object(), object()]

    class _Engine:
        @staticmethod
        def get_native_engine():
            return _Native()

    class _Bootstrap:
        engine = _Engine()

    from Infernux.engine.bootstrap import EditorBootstrap

    monkeypatch.setattr(EditorBootstrap, "instance", lambda: _Bootstrap())

    state = runtime._renderer_state()

    assert state["frame"]["frame"] == 91
    assert state["frame"]["game_draw_call_count"] == 7
    assert state["frame"]["game_render_graph_current_executed"] is True
    assert state["frame"]["game_render_graph_execution_count"] == 42
    assert "Bloom_Composite" in state["frame"]["game_render_graph_pass_names"]
    assert state["frame"]["ui_panel_times_ms"]["project"] == pytest.approx(0.42)
    assert state["frame"]["ui_panel_sub_times_ms"]["project"]["folderTree"] == pytest.approx(0.19)
    assert state["gpu_residency"]["tracked_bytes"] == 4096
    assert state["msaa"]["active_samples"] == 4
    assert state["msaa"]["supported_samples"] == [1, 2, 4, 8]
    assert state["msaa"]["material_pipelines_aligned"] is True
    assert state["preview_tasks"][0]["ready_generation"] == 2
    assert state["asset_runtime_record_count"] == 3
    assert state["submission_ready"] is True


def test_runtime_ui_performance_reads_native_rolling_snapshot(monkeypatch):
    class _Native:
        renderer_ui_performance_snapshot = {
            "first_frame": 10,
            "last_frame": 249,
            "sample_count": 240,
            "gui_build": {"median_ms": 0.5, "p95_ms": 0.7},
            "panel_times": {"project": {"median_ms": 0.04, "p95_ms": 0.05}},
            "panel_sub_times": {},
        }

    class _Engine:
        @staticmethod
        def get_native_engine():
            return _Native()

    class _Bootstrap:
        engine = _Engine()

    from Infernux.engine.bootstrap import EditorBootstrap

    monkeypatch.setattr(EditorBootstrap, "instance", lambda: _Bootstrap())

    state = runtime._ui_performance_state()

    assert state["sample_count"] == 240
    assert state["gui_build"]["p95_ms"] == pytest.approx(0.7)
    assert state["panel_times"]["project"]["median_ms"] == pytest.approx(0.04)


def test_runtime_physics_state_uses_public_world_and_frame_profile():
    class _Physics:
        body_count = 14

    class _SceneManager:
        @staticmethod
        def get_fixed_time_step():
            return 0.02

        @staticmethod
        def get_last_frame_profile():
            return {"fixed_steps": 1, "contact_events": 3, "rigidbody_sync_candidates": 4}

    state = runtime._physics_state(_Physics, _SceneManager())

    assert state["body_count"] == 14
    assert state["fixed_time_step"] == pytest.approx(0.02)
    assert state["fixed_step_ran"] is True
    assert state["contact_events_observed"] is True
    assert state["frame_profile"]["rigidbody_sync_candidates"] == 4


def test_runtime_physics_queries_are_bounded_and_serialize_public_hits():
    class _Object:
        id = 17
        name = "CheckpointGate_B"

    class _Collider:
        game_object = _Object()
        type_name = "BoxCollider"
        component_id = 51
        enabled = True
        is_trigger = False

    class _Vector:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class _Hit:
        game_object = _Object()
        collider = _Collider()
        distance = 4.5
        point = _Vector(0.0, 1.0, 2.0)
        normal = _Vector(0.0, 1.0, 0.0)

    class _Physics:
        @staticmethod
        def raycast_all(*_args):
            return [_Hit(), _Hit()]

        @staticmethod
        def overlap_box(*_args):
            return [_Collider(), _Collider()]

    arguments = runtime._physics_query_arguments(
        origin=[0, 1, 2], direction=[0, 0, 2], max_distance=20000, layer_mask=0xFFFFFFFF, limit=1
    )
    raycast = runtime._physics_raycast(**arguments, query_triggers=True, physics_api=_Physics)
    overlap = runtime._physics_overlap_box(
        center=(5.0, 0.0, 6.0),
        half_extents=(2.0, 2.0, 2.0),
        layer_mask=0xFFFFFFFF,
        query_triggers=True,
        limit=1,
        physics_api=_Physics,
    )

    assert arguments["max_distance"] == 10000.0
    assert raycast["hit_count"] == 2 and raycast["truncated"] is True
    assert raycast["hits"][0]["object_id"] == 17
    assert raycast["hits"][0]["point"] == [0.0, 1.0, 2.0]
    assert overlap["hit_count"] == 2 and overlap["truncated"] is True
    assert overlap["colliders"][0]["collider_type"] == "BoxCollider"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"origin": [0, 0], "direction": [0, 1, 0], "max_distance": 1, "layer_mask": 1, "limit": 1}, "origin"),
        ({"origin": [0, 0, 0], "direction": [0, 0, 0], "max_distance": 1, "layer_mask": 1, "limit": 1}, "direction"),
        ({"origin": [0, 0, 0], "direction": [0, 1, 0], "max_distance": 0, "layer_mask": 1, "limit": 1}, "max_distance"),
        ({"origin": [0, 0, 0], "direction": [0, 1, 0], "max_distance": 1, "layer_mask": -1, "limit": 1}, "layer_mask"),
    ],
)
def test_runtime_physics_query_validation_rejects_invalid_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        runtime._physics_query_arguments(**kwargs)


def test_runtime_object_prefab_linkage_exposes_exact_public_values():
    class _LinkedRoot:
        prefab_guid = "checkpoint-guid-123"
        prefab_root = True

    class _Unlinked:
        pass

    assert runtime._object_prefab_linkage(_LinkedRoot()) == {
        "prefab_guid": "checkpoint-guid-123",
        "prefab_root": True,
        "prefab_linked": True,
    }
    assert runtime._object_prefab_linkage(_Unlinked()) == {
        "prefab_guid": "",
        "prefab_root": False,
        "prefab_linked": False,
    }


@pytest.mark.parametrize(
    ("actual", "assertion", "expected"),
    [
        (12.0, {"operator": "greater_than", "value": 10.0}, True),
        (12.0, {"operator": "less_or_equal", "value": 10.0}, False),
        (1.001, {"equals": 1.0, "tolerance": 0.01}, True),
        ("Results", {"operator": "equals", "value": "Results"}, True),
        ("Results", {"operator": "not_equals", "value": "RaceTrack"}, True),
    ],
)
def test_runtime_comparisons_support_numeric_thresholds_and_exact_values(actual, assertion, expected):
    passed, _operator, _value, _detail = runtime._compare_value(actual, assertion)

    assert passed is expected


def test_runtime_idle_requires_scene_transition_completion():
    assert runtime._is_runtime_idle({"deferred_task_busy": False, "scene_loading": False}) is True
    assert runtime._is_runtime_idle({"deferred_task_busy": True, "scene_loading": False}) is False
    assert runtime._is_runtime_idle({"deferred_task_busy": False, "scene_loading": True}) is False


def test_runtime_object_state_returns_a_structured_not_found_result_after_scene_change(monkeypatch):
    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)

    def missing_object(*_args, **_kwargs):
        raise FileNotFoundError("GameObject 11 was not found.")

    monkeypatch.setattr(runtime, "_run_on_main", missing_object)

    response = fake.tools["runtime_get_object_state"](11)

    assert response["ok"] is False
    assert response["error"]["code"] == "error.not_found"
    assert response["data"] == {"object_id": 11, "object_exists": False}
    assert "active scene may have changed" in response["error"]["hint"]


def test_runtime_find_objects_exposes_name_to_id_resolution(monkeypatch):
    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(
        runtime,
        "_find_runtime_objects",
        lambda **kwargs: [{"id": 11, "name": kwargs["name"], "components": []}],
    )

    response = fake.tools["runtime_find_objects"](name="PlayerCar")

    assert response["ok"] is True
    assert response["data"]["objects"][0]["id"] == 11


def test_runtime_measure_motion_holds_input_and_returns_trajectory_summary(monkeypatch):
    from Infernux.mcp.tools import input as input_tools
    from Infernux.input import Input

    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)
    samples = iter(
        [
            {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0], "euler_angles": [0.0, 0.0, 0.0]}},
            {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 2.5], "euler_angles": [0.0, 0.0, 0.0]}},
        ]
    )
    states = iter(
        [
            {"scene_name": "racetrack", "scene_path": "Assets/racetrack.scene"},
            {"scene_name": "racetrack", "scene_path": "Assets/racetrack.scene"},
        ]
    )
    transitions = []
    monkeypatch.setattr(runtime, "_named_transform_snapshots", lambda _names, _probes=None: next(samples))
    monkeypatch.setattr(runtime, "_editor_state", lambda: next(states))
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        input_tools,
        "perform_key_transition",
        lambda key, pressed, **_: transitions.append(
            (key, pressed, Input._accepts_game_input(), Input.is_game_focused())
        ) or {"ok": True, "data": {"delivered": True}},
    )
    Input.set_game_focused(False)

    response = fake.tools["runtime_measure_motion"](
        ["PlayerCar"], seconds=0.2, hold_key="w", sample_interval=0.2
    )

    assert response["ok"] is True
    measurement = response["data"]["measurements"][0]
    assert measurement["delta"] == [0.0, 0.0, 2.5]
    assert measurement["position_range"] == [0.0, 0.0, 2.5]
    assert measurement["axis_path_length"] == [0.0, 0.0, 2.5]
    assert measurement["path_length"] == pytest.approx(2.5)
    assert response["data"]["sample_count"] == 2
    assert transitions == [
        ("w", True, True, False),
        ("w", False, True, False),
    ]
    assert Input._automation_game_input_depth == 0


def test_runtime_measure_motion_holds_multiple_keys_in_one_action_window(monkeypatch):
    from Infernux.mcp.tools import input as input_tools

    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)
    samples = iter(
        [
            {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0], "euler_angles": [0.0, 0.0, 0.0]}},
            {"PlayerCar": {"id": 11, "position": [1.0, 0.5, 2.0], "euler_angles": [0.0, 15.0, 0.0]}},
        ]
    )
    transitions = []
    monkeypatch.setattr(runtime, "_named_transform_snapshots", lambda _names, _probes=None: next(samples))
    monkeypatch.setattr(
        runtime,
        "_editor_state",
        lambda: {"scene_name": "racetrack", "scene_path": "Assets/racetrack.scene"},
    )
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        input_tools,
        "perform_key_transition",
        lambda key, pressed, **_: transitions.append((key, pressed)) or {
            "ok": True,
            "data": {"key": key, "pressed": pressed},
        },
    )
    response = fake.tools["runtime_measure_motion"](
        ["PlayerCar"], seconds=0.2, hold_keys=["w", "a"], sample_interval=0.2
    )

    assert response["ok"] is True
    assert response["data"]["hold_keys"] == ["w", "a"]
    assert response["data"]["input_presses"] == [
        {"key": "w", "pressed": True},
        {"key": "a", "pressed": True},
    ]
    assert response["data"]["input_releases"] == [
        {"key": "a", "pressed": False},
        {"key": "w", "pressed": False},
    ]
    assert transitions == [("w", True), ("a", True), ("a", False), ("w", False)]


def test_runtime_measure_motion_reveals_periodic_motion_with_small_endpoint_delta(monkeypatch):
    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)
    samples = iter(
        [
            {"Light": {"id": 3, "position": [0.0, 0.0, 0.0], "euler_angles": [0.0, 0.0, 0.0]}},
            {"Light": {"id": 3, "position": [0.0, 2.0, 0.0], "euler_angles": [0.0, 0.0, 0.0]}},
            {"Light": {"id": 3, "position": [0.0, 0.01, 0.0], "euler_angles": [0.0, 0.0, 0.0]}},
        ]
    )
    monkeypatch.setattr(runtime, "_named_transform_snapshots", lambda _names, _probes=None: next(samples))
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"scene_name": "results"})
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    response = fake.tools["runtime_measure_motion"](
        ["Light"], seconds=1.0, sample_interval=0.5
    )

    measurement = response["data"]["measurements"][0]
    assert measurement["delta"] == [0.0, 0.01, 0.0]
    assert measurement["position_range"] == [0.0, 2.0, 0.0]
    assert measurement["axis_path_length"] == pytest.approx([0.0, 3.99, 0.0])
    assert measurement["max_excursion_from_start"] == [0.0, 2.0, 0.0]


def test_runtime_measure_motion_samples_public_component_fields_in_action_window(monkeypatch):
    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)
    samples = iter(
        [
            {
                "PlayerCar": {
                    "id": 11,
                    "position": [0.0, 0.5, 0.0],
                    "euler_angles": [0.0, 0.0, 0.0],
                    "component_fields": {"AudioSource[0]": {"pitch": 0.8}},
                }
            },
            {
                "PlayerCar": {
                    "id": 11,
                    "position": [0.0, 0.5, 1.0],
                    "euler_angles": [0.0, 0.0, 0.0],
                    "component_fields": {"AudioSource[0]": {"pitch": 1.2}},
                }
            },
        ]
    )
    monkeypatch.setattr(runtime, "_named_transform_snapshots", lambda _names, _probes=None: next(samples))
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"scene_name": "racetrack", "scene_path": "Assets/racetrack.scene"})
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    response = fake.tools["runtime_measure_motion"](
        ["PlayerCar"],
        seconds=0.2,
        sample_interval=0.2,
        component_probes=[{
            "object_name": "PlayerCar",
            "component_type": "AudioSource",
            "fields": ["pitch"],
        }],
    )

    component = response["data"]["component_measurements"][0]
    assert component["first"] == pytest.approx(0.8)
    assert component["last"] == pytest.approx(1.2)
    assert component["maximum"] == pytest.approx(1.2)
    assert component["range"] == pytest.approx(0.4)


def test_armed_motion_capture_samples_across_a_later_real_play_transition(monkeypatch):
    from Infernux.mcp.tools import input as input_tools

    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    playing = threading.Event()
    play_samples = iter([5.0, 4.0, 1.5, 0.5])
    last_y = [0.5]

    def state():
        return {
            "play_state": "playing" if playing.is_set() else "edit",
            "scene_name": "PhysicsValidation",
            "scene_path": "Assets/PhysicsValidation.scene",
        }

    def objects(_names, probes=None):
        if not playing.is_set():
            y = 5.0
        else:
            try:
                y = next(play_samples)
                last_y[0] = y
            except StopIteration:
                y = last_y[0]
        value = {"id": 68, "position": [0.0, y, 0.0], "euler_angles": [0.0, 0.0, 0.0]}
        if probes:
            value["component_fields"] = {"Rigidbody[0]": {"velocity": [0.0, -y, 0.0]}}
        return {"FallingBall": value}

    transitions = []
    mouse_transitions = []
    monkeypatch.setattr(runtime, "_editor_state", state)
    monkeypatch.setattr(runtime, "_named_transform_snapshots", objects)
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())

    def register_trigger(capture_id):
        def wait_for_play():
            assert playing.wait(1.0)
            with runtime._MOTION_CAPTURE_LOCK:
                record = runtime._MOTION_CAPTURES[capture_id]
                record["trigger_state"] = {"play_state": "playing"}
                record["_trigger_event"].set()

        threading.Thread(target=wait_for_play, daemon=True).start()

    monkeypatch.setattr(runtime, "_register_motion_capture_trigger", register_trigger)
    monkeypatch.setattr(runtime, "_unregister_motion_capture_trigger", lambda _capture_id: None)
    monkeypatch.setattr(
        input_tools,
        "perform_key_transition",
        lambda key, pressed, **_: transitions.append((key, pressed)) or {
            "ok": True,
            "data": {"delivered": True, "pressed": pressed},
        },
    )
    monkeypatch.setattr(
        input_tools,
        "perform_mouse_button_transition",
        lambda button, pressed, **kwargs: mouse_transitions.append(
            (button, pressed, kwargs["x"], kwargs["y"])
        ) or {
            "ok": True,
            "data": {"delivered": True, "button": button, "pressed": pressed},
        },
    )

    armed = fake.tools["runtime_motion_capture_arm"](
        ["FallingBall"],
        seconds=0.06,
        sample_interval=0.02,
        trigger_timeout=1.0,
        hold_keys=["w", "a"],
        hold_mouse_buttons=[1],
        mouse_x=640.0,
        mouse_y=360.0,
        component_probes=[{
            "object_name": "FallingBall",
            "component_type": "Rigidbody",
            "fields": ["velocity"],
        }],
    )

    assert armed["ok"] is True
    assert armed["data"]["status"] == "armed"
    assert armed["data"]["armed_objects"]["FallingBall"]["position"][1] == 5.0
    playing.set()
    completed = fake.tools["runtime_motion_capture_status"](
        armed["data"]["capture_id"],
        wait_seconds=1.0,
    )

    assert completed["ok"] is True
    assert completed["data"]["status"] == "completed"
    assert completed["data"]["terminal"] is True
    assert completed["data"]["sample_count"] >= 3
    measurement = completed["data"]["measurements"][0]
    assert measurement["position_max"][1] == 5.0
    assert measurement["position_min"][1] <= 0.5
    assert measurement["axis_path_length"][1] >= 4.5
    assert completed["data"]["component_measurements"][0]["sample_count"] >= 3
    assert completed["data"]["input_press"] == {"delivered": True, "pressed": True}
    assert completed["data"]["input_release"] == {"delivered": True, "pressed": False}
    assert len(completed["data"]["input_presses"]) == 3
    assert len(completed["data"]["input_releases"]) == 3
    assert transitions == [("w", True), ("a", True), ("a", False), ("w", False)]
    assert mouse_transitions == [
        (1, True, 640.0, 360.0),
        (1, False, 640.0, 360.0),
    ]
    runtime._MOTION_CAPTURES.clear()


def test_motion_capture_refuses_to_arm_after_trigger_state_is_active(monkeypatch):
    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"play_state": "playing"})

    response = fake.tools["runtime_motion_capture_arm"](["FallingBall"])

    assert response["ok"] is False
    assert response["error"]["code"] == "error.invalid_state"
    assert response["data"]["trigger_play_state"] == "playing"


def test_motion_capture_exposes_bounded_frame_plan_without_per_frame_requests(monkeypatch):
    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(
        runtime,
        "_editor_state",
        lambda: {"play_state": "edit", "scene_name": "racetrack", "scene_path": "Assets/racetrack.scene"},
    )
    monkeypatch.setattr(
        runtime,
        "_named_transform_snapshots",
        lambda _names, _probes=None: {
            "PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0], "euler_angles": [0.0, 0.0, 0.0]}
        },
    )
    monkeypatch.setattr(runtime, "_register_motion_capture_trigger", lambda _capture_id: None)
    monkeypatch.setattr(runtime, "_unregister_motion_capture_trigger", lambda _capture_id: None)

    armed = fake.tools["runtime_motion_capture_arm"](
        ["PlayerCar"],
        seconds=3.0,
        sample_interval=0.1,
        trigger_timeout=1.0,
        hold_keys=["w", "a"],
        hold_mouse_buttons=[1],
        frame_count=180,
        pause_on_complete=True,
    )

    assert armed["ok"] is True
    assert armed["data"]["frame_count"] == 180
    assert armed["data"]["hold_frame_count"] == 180
    assert armed["data"]["wait_frame_count"] == 0
    assert armed["data"]["pause_on_complete"] is True
    assert armed["data"]["hold_keys"] == ["w", "a"]
    assert armed["data"]["hold_mouse_buttons"] == [1]
    cancelled = fake.tools["runtime_motion_capture_cancel"](armed["data"]["capture_id"])
    assert cancelled["ok"] is True
    completed = fake.tools["runtime_motion_capture_status"](
        armed["data"]["capture_id"], wait_seconds=1.0
    )
    assert completed["data"]["status"] == "cancelled"
    runtime._MOTION_CAPTURES.clear()


def test_motion_capture_accepts_a_two_stage_hold_and_wait_frame_plan(monkeypatch):
    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"play_state": "edit"})
    monkeypatch.setattr(
        runtime,
        "_named_transform_snapshots",
        lambda _names, _probes=None: {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0]}},
    )
    monkeypatch.setattr(runtime, "_register_motion_capture_trigger", lambda _capture_id: None)
    monkeypatch.setattr(runtime, "_unregister_motion_capture_trigger", lambda _capture_id: None)

    armed = fake.tools["runtime_motion_capture_arm"](
        ["PlayerCar"],
        seconds=2.0,
        hold_keys=["w"],
        hold_frame_count=90,
        wait_frame_count=30,
        pause_on_complete=True,
    )

    assert armed["ok"] is True
    assert armed["data"]["frame_count"] == 120
    assert armed["data"]["hold_frame_count"] == 90
    assert armed["data"]["wait_frame_count"] == 30
    assert armed["data"]["wait_seconds"] == 0.0
    fake.tools["runtime_motion_capture_cancel"](armed["data"]["capture_id"])
    runtime._MOTION_CAPTURES.clear()


def test_motion_capture_accepts_post_release_wall_clock_wait(monkeypatch):
    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"play_state": "edit"})
    monkeypatch.setattr(
        runtime,
        "_named_transform_snapshots",
        lambda _names, _probes=None: {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0]}},
    )
    monkeypatch.setattr(runtime, "_register_motion_capture_trigger", lambda _capture_id: None)
    monkeypatch.setattr(runtime, "_unregister_motion_capture_trigger", lambda _capture_id: None)

    armed = fake.tools["runtime_motion_capture_arm"](
        ["PlayerCar"],
        hold_key="w",
        hold_frame_count=60,
        wait_seconds=0.5,
        pause_on_complete=True,
    )

    assert armed["ok"] is True
    assert armed["data"]["frame_count"] == 60
    assert armed["data"]["wait_seconds"] == 0.5
    fake.tools["runtime_motion_capture_cancel"](armed["data"]["capture_id"])
    runtime._MOTION_CAPTURES.clear()


def test_motion_capture_status_waits_for_completion_event_without_polling(monkeypatch):
    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    capture_id = "motion-event-wait"
    terminal_event = threading.Event()
    with runtime._MOTION_CAPTURE_LOCK:
        runtime._MOTION_CAPTURES[capture_id] = {
            "capture_id": capture_id,
            "status": "capturing",
            "created_at": 0.0,
            "_terminal_event": terminal_event,
        }

    reads = 0
    original_public = runtime._public_motion_capture

    def read_capture(identifier):
        nonlocal reads
        reads += 1
        return original_public(identifier)

    monkeypatch.setattr(runtime, "_public_motion_capture", read_capture)
    response: dict = {}
    waiter = threading.Thread(
        target=lambda: response.update(
            fake.tools["runtime_motion_capture_status"](capture_id, wait_seconds=1.0)
        )
    )
    waiter.start()
    assert terminal_event.wait(0.1) is False
    runtime._finish_motion_capture(capture_id, status="completed")
    waiter.join(timeout=1.0)

    assert not waiter.is_alive()
    assert response["ok"] is True
    assert response["data"]["status"] == "completed"
    assert reads == 2
    runtime._MOTION_CAPTURES.clear()


def test_motion_capture_rejects_an_ambiguous_frame_plan():
    with pytest.raises(ValueError, match="frame_count as the total budget"):
        runtime._normalize_motion_capture_frame_plan(120, 90, 30, ["w"])


def test_motion_capture_pauses_when_a_declarative_stop_condition_is_sampled(monkeypatch):
    from Infernux.mcp.tools import input as input_tools

    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    transitions = []
    pauses = []
    snapshots = iter([
        {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0], "euler_angles": [0.0, 0.0, 0.0]}},
        {"PlayerCar": {"id": 11, "position": [0.0, 0.5, 1.2], "euler_angles": [0.0, 2.0, 0.0]}},
    ])
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"play_state": "edit"})
    monkeypatch.setattr(runtime, "_named_transform_snapshots", lambda _names, _probes=None: next(snapshots))
    monkeypatch.setattr(runtime, "_unregister_motion_capture_trigger", lambda _capture_id: None)
    monkeypatch.setattr(runtime, "_arm_debug_frame_pause", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_cancel_debug_frame_pause", lambda: None)
    monkeypatch.setattr(runtime, "_pause_active_play_mode", lambda: pauses.append(True) or True)
    monkeypatch.setattr(
        runtime,
        "_evaluate_motion_capture_stop_assertions",
        lambda assertions, mode: {"passed": True, "mode": mode, "results": [{"passed": True, "assertion": assertions[0]}]},
    )
    monkeypatch.setattr(
        input_tools,
        "perform_key_transition",
        lambda key, pressed, **_: transitions.append((key, pressed)) or {"ok": True, "data": {"delivered": True}},
    )

    def register_trigger(capture_id):
        with runtime._MOTION_CAPTURE_LOCK:
            record = runtime._MOTION_CAPTURES[capture_id]
            record["trigger_state"] = {"play_state": "playing"}
            record["_trigger_event"].set()

    monkeypatch.setattr(runtime, "_register_motion_capture_trigger", register_trigger)
    armed = fake.tools["runtime_motion_capture_arm"](
        ["PlayerCar"],
        seconds=1.0,
        sample_interval=0.02,
        frame_count=120,
        hold_keys=["w", "a"],
        stop_assertions=[{
            "kind": "transform_axis",
            "object_name": "PlayerCar",
            "field": "position",
            "axis": "z",
            "operator": "greater_or_equal",
            "value": 1.0,
        }],
    )
    completed = fake.tools["runtime_motion_capture_status"](
        armed["data"]["capture_id"], wait_seconds=1.0
    )

    assert completed["data"]["status"] == "condition_met"
    assert completed["data"]["stop_condition"]["passed"] is True
    assert pauses == [True]
    assert transitions == [("w", True), ("a", True), ("a", False), ("w", False)]
    runtime._MOTION_CAPTURES.clear()


def test_frame_bounded_capture_reports_timeout_without_masking_it_as_runtime_failure(monkeypatch):
    from Infernux.mcp.tools import input as input_tools

    fake = _FakeMcp()
    runtime._MOTION_CAPTURES.clear()
    runtime.register_runtime_tools(fake)
    playing = threading.Event()
    monkeypatch.setattr(
        runtime,
        "_editor_state",
        lambda: {
            "play_state": "playing" if playing.is_set() else "edit",
            "scene_name": "racetrack",
            "scene_path": "Assets/racetrack.scene",
        },
    )
    monkeypatch.setattr(
        runtime,
        "_named_transform_snapshots",
        lambda _names, _probes=None: {
            "PlayerCar": {"id": 11, "position": [0.0, 0.5, 0.0], "euler_angles": [0.0, 0.0, 0.0]}
        },
    )
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())
    monkeypatch.setattr(runtime, "_unregister_motion_capture_trigger", lambda _capture_id: None)
    monkeypatch.setattr(runtime, "_arm_debug_frame_pause", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        input_tools,
        "perform_key_transition",
        lambda _key, _pressed, **_: {"ok": True, "data": {"delivered": True}},
    )

    def register_trigger(capture_id):
        playing.set()
        with runtime._MOTION_CAPTURE_LOCK:
            runtime._MOTION_CAPTURES[capture_id]["_trigger_event"].set()

    monkeypatch.setattr(runtime, "_register_motion_capture_trigger", register_trigger)
    armed = fake.tools["runtime_motion_capture_arm"](
        ["PlayerCar"],
        seconds=0.03,
        sample_interval=0.02,
        frame_count=60,
        hold_key="w",
    )

    completed = fake.tools["runtime_motion_capture_status"](
        armed["data"]["capture_id"], wait_seconds=1.0
    )

    assert completed["data"]["status"] == "frame_timeout"
    assert "Frame budget did not complete" in completed["data"]["error"]
    assert completed["data"]["terminal"] is True
    runtime._MOTION_CAPTURES.clear()


def test_runtime_assertion_can_resolve_an_object_by_public_name(monkeypatch):
    from Infernux.scene import GameObjectQuery

    marker = object()
    monkeypatch.setattr(GameObjectQuery, "find", lambda name: marker if name == "PlayerCar" else None)

    assert runtime._resolve_assertion_object({"object_name": "PlayerCar"}) is marker


def test_runtime_assertion_schema_exposes_public_kinds_and_fields():
    schema = runtime.RuntimeAssertion.model_json_schema()

    assert set(schema["properties"]["kind"]["enum"]) == {
        "play_state",
        "audio_paused",
        "object_exists",
        "component_exists",
        "no_errors",
        "scene_name",
        "scene_path",
        "transform_axis",
        "input_axis",
        "component_field",
    }
    assert "object_name" in schema["properties"]
    assert "component_type" in schema["properties"]
    assert "operator" in schema["properties"]
    assert len(schema["examples"]) >= 8


def test_runtime_assert_rejects_an_empty_assertion_set():
    fake = _FakeMcp()
    runtime.register_runtime_tools(fake)

    with pytest.raises(ValueError, match="between 1 and 64"):
        fake.tools["runtime_assert"]([])


def test_runtime_assertion_model_preserves_equals_shorthand():
    assertion = runtime.RuntimeAssertion(kind="scene_name", equals="Results")

    assert runtime._assertion_mapping(assertion) == {"kind": "scene_name", "equals": "Results"}


def test_runtime_scene_and_transform_assertions_are_evaluated_from_observation(monkeypatch):
    class _Vector:
        x = 0.0
        y = 1.0
        z = 24.0

    class _Transform:
        position = _Vector()

    class _Object:
        transform = _Transform()

    monkeypatch.setattr(runtime, "_editor_state", lambda: {"scene_name": "Results", "scene_path": "Assets/Results.scene"})
    monkeypatch.setattr(runtime, "_try_find_object", lambda object_id: _Object() if object_id == 11 else None)

    scene = runtime._evaluate_assertion({"kind": "scene_name", "equals": "Results"})
    transform = runtime._evaluate_assertion({
        "kind": "transform_axis",
        "object_id": 11,
        "field": "position",
        "axis": "z",
        "operator": "greater_or_equal",
        "value": 20.0,
    })

    assert scene["passed"] is True
    assert transform["passed"] is True
    assert transform["actual"] == 24.0


def test_runtime_audio_paused_assertion_uses_global_audio_state(monkeypatch):
    monkeypatch.setattr(runtime, "_editor_state", lambda: {"audio_paused": True})

    result = runtime._evaluate_assertion({"kind": "audio_paused", "equals": True})

    assert result["passed"] is True
    assert result["actual"] is True


def test_runtime_component_field_assertion_supports_event_and_state_counters(monkeypatch):
    class _Counter:
        type_name = "RaceProgress"
        lap_count = 2

    monkeypatch.setattr(runtime, "_try_find_object", lambda object_id: object() if object_id == 11 else None)
    monkeypatch.setattr(runtime, "_find_component", lambda obj, component_type, ordinal: _Counter() if component_type == "RaceProgress" else None)

    result = runtime._evaluate_assertion({
        "kind": "component_field",
        "object_id": 11,
        "component_type": "RaceProgress",
        "field": "lap_count",
        "operator": "greater_or_equal",
        "value": 1,
    })

    assert result["passed"] is True
    assert result["actual"] == 2


def test_runtime_component_field_uses_public_builtin_wrapper(monkeypatch):
    from Infernux.components.builtin_component import BuiltinComponent

    class _NativeAudioSource:
        type_name = "AudioSource"
        component_id = 186

    class _PublicAudioSource:
        type_name = "AudioSource"
        component_id = 186
        is_playing = True

    public = _PublicAudioSource()

    class _AudioSourceWrapper:
        @classmethod
        def _get_or_create_wrapper(cls, cpp_component, game_object):
            assert cpp_component is native
            assert game_object is obj
            return public

    native = _NativeAudioSource()
    obj = type(
        "_Object",
        (),
        {
            "get_components": lambda self: [native],
            "get_py_components": lambda self: [],
        },
    )()
    monkeypatch.setitem(BuiltinComponent._builtin_registry, "AudioSource", _AudioSourceWrapper)
    monkeypatch.setattr(runtime, "_try_find_object", lambda object_id: obj if object_id == 11 else None)

    result = runtime._evaluate_assertion({
        "kind": "component_field",
        "object_id": 11,
        "component_type": "AudioSource",
        "field": "is_playing",
        "equals": True,
    })

    assert result["passed"] is True
    assert result["actual"] is True


def test_runtime_no_errors_assertion_includes_script_loader_failures(monkeypatch):
    monkeypatch.setattr(runtime, "_read_errors", lambda **_: {
        "errors": [],
        "script_errors": [{"path": "Assets/Racing/drive.py", "traceback": "SyntaxError"}],
    })

    result = runtime._evaluate_assertion({"kind": "no_errors"})

    assert result["passed"] is False
    assert result["actual"] == 1
