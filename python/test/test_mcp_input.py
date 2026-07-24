from __future__ import annotations

import threading

import pytest

from Infernux import lib as infernux_lib
from Infernux.mcp import session
from Infernux.mcp.threading import MainThreadCommandQueue
from Infernux.mcp.tools import input as input_tools


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


class _FakeNativeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._sequence = 0
        self.last_processed_synthetic_input_sequence = 0
        self.pending_synthetic_input_count = 0

    def request_full_speed_frame(self) -> None:
        self.calls.append(("full_speed",))

    def queue_synthetic_key_input(self, scancode: int, pressed: bool, repeat: bool) -> int:
        self.calls.append(("key", scancode, pressed, repeat))
        self._sequence += 1
        self.last_processed_synthetic_input_sequence = self._sequence
        return self._sequence

    def queue_synthetic_mouse_motion_input(self, x: float, y: float, delta_x: float, delta_y: float) -> int:
        self.calls.append(("motion", x, y, delta_x, delta_y))
        self._sequence += 1
        self.last_processed_synthetic_input_sequence = self._sequence
        return self._sequence

    def queue_synthetic_mouse_button_input(self, button: int, pressed: bool, x: float, y: float) -> int:
        self.calls.append(("button", button, pressed, x, y))
        self._sequence += 1
        self.last_processed_synthetic_input_sequence = self._sequence
        return self._sequence

    def queue_synthetic_text_input(self, text: str) -> int:
        self.calls.append(("text", text))
        self._sequence += 1
        self.last_processed_synthetic_input_sequence = self._sequence
        return self._sequence

    def queue_synthetic_close_request(self) -> int:
        self.calls.append(("close",))
        self._sequence += 1
        self.last_processed_synthetic_input_sequence = self._sequence
        return self._sequence


def test_validation_input_tool_waits_for_native_delivery(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    native = _FakeNativeEngine()
    monkeypatch.setattr(input_tools, "_native_engine", lambda: native)

    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)

    fake_mcp = _FakeMcp()
    input_tools.register_input_tools(fake_mcp)
    result = fake_mcp.tools["input_key"](26, True)

    assert result["ok"] is True
    assert result["data"] == {
        "sequence": 1,
        "last_processed_sequence": 1,
        "pending_event_count": 0,
        "delivered": True,
    }
    assert native.calls == [("full_speed",), ("key", 26, True, False)]
    assert (tmp_path / "Logs" / "mcp_session.jsonl").is_file()


def test_semantic_click_helper_delivers_move_press_release(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    native = _FakeNativeEngine()
    monkeypatch.setattr(input_tools, "_native_engine", lambda: native)

    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)
    monkeypatch.setattr(input_tools, "_current_rendered_gui_frame", lambda _timeout: 41)
    monkeypatch.setattr(
        input_tools,
        "_wait_for_rendered_gui_frame",
        lambda previous_frame, **kwargs: {
            "frame": previous_frame + 1,
            "target_active": True if kwargs.get("expected_target_id") else None,
        },
    )

    result = input_tools.perform_pointer_click(12.5, 8.0, expected_target_id="button:probe:activate:1")

    assert result["ok"] is True
    assert result["data"] == {
        "x": 12.5,
        "y": 8.0,
        "button": 0,
        "move_sequence": 1,
        "press_sequence": 2,
        "release_sequence": 3,
        "move_render_frame": 42,
        "press_render_frame": 43,
            "release_render_frame": 44,
            "press_target_active": True,
            "press_frame": {"frame": 43, "target_active": True},
            "delivered": True,
    }
    assert native.calls == [
        ("full_speed",),
        ("motion", 12.5, 8.0, 0.0, 0.0),
        ("full_speed",),
        ("button", 0, True, 12.5, 8.0),
        ("full_speed",),
        ("button", 0, False, 12.5, 8.0),
    ]

    monkeypatch.setattr(
        infernux_lib,
        "get_gui_semantic_snapshot",
        lambda: {
            "capture_enabled": True,
            "frame": 45,
            "mouse": [20.0, 11.0],
            "targets": [
                {
                    "id": "button:rebuilt_window:activate:9",
                    "semantic_id": "activate",
                    "active": True,
                    "visible": True,
                    "enabled": True,
                    "rect": [10.0, 5.0, 20.0, 20.0],
                }
            ],
        },
    )
    frame_status = input_tools._native_gui_frame_status("button:probe:activate:1")
    assert frame_status["target_match"] == "semantic_id"
    assert frame_status["target_id_matched"] is False
    assert frame_status["eligible_semantic_match_count"] == 1
    assert frame_status["matched_target_id"] == "button:rebuilt_window:activate:9"
    assert frame_status["matched_semantic_id"] == "activate"
    assert frame_status["rendered_target_count"] == 1
    assert frame_status["active_targets"][0]["semantic_id"] == "activate"
    assert frame_status["target_active"] is True
    exact_status = input_tools._native_gui_frame_status("button:rebuilt_window:activate:9")
    assert exact_status["target_match"] == "target_id"
    assert exact_status["target_id_matched"] is True
    assert exact_status["matched_target_id"] == "button:rebuilt_window:activate:9"
    monkeypatch.setattr(
        infernux_lib,
        "get_gui_semantic_snapshot",
        lambda: {
            "capture_enabled": True,
            "frame": 46,
            "mouse": [20.0, 11.0],
            "targets": [{
                "id": "menu:Menu_00:menu.project:2529322466",
                "semantic_id": "menu.project",
                "active": False,
                "visible": False,
                "enabled": False,
                "rect": [3.0, 2.0, 34.0, 19.0],
            }],
        },
    )

    hidden_status = input_tools._native_gui_frame_status("menu:##MainMenuBar:menu.project:2529322466")

    assert hidden_status["semantic_match_count"] == 1
    assert hidden_status["eligible_semantic_match_count"] == 0
    assert hidden_status["target_found"] is False
    assert hidden_status["target_match"] == "none"
    assert input_tools._press_target_accepted(hidden_status) is False

    monkeypatch.setattr(
        infernux_lib,
        "get_gui_semantic_snapshot",
        lambda: {
            "capture_enabled": True,
            "frame": 47,
            "mouse": [20.0, 11.0],
            "targets": [{
                "id": "menu_item:Menu_00:menu.project.new_scene:77",
                "semantic_id": "menu.project.new_scene",
                "kind": "menu_item",
                "active": False,
                "visible": True,
                "enabled": True,
                "rect": [3.0, 2.0, 80.0, 19.0],
            }],
        },
    )
    menu_item_status = input_tools._native_gui_frame_status(
        "menu_item:Menu_00:menu.project.new_scene:77"
    )
    assert menu_item_status["target_under_pointer"] is True
    assert input_tools._press_target_accepted(menu_item_status) is True

    game_button_status = {
        "matched_target_kind": "game_ui_button",
        "target_found": True,
        "target_visible": True,
        "target_enabled": True,
        "target_under_pointer": True,
        "target_active": False,
    }
    assert input_tools._press_target_accepted(game_button_status) is True


def test_semantic_click_releases_button_when_rendered_frame_barrier_fails(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    native = _FakeNativeEngine()
    monkeypatch.setattr(input_tools, "_native_engine", lambda: native)

    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)
    monkeypatch.setattr(input_tools, "_current_rendered_gui_frame", lambda _timeout: 41)

    barriers = iter(({"frame": 42}, None))

    def _no_rendered_press(previous_frame, **_kwargs):
        result = next(barriers)
        if result is None:
            assert previous_frame == 42
            assert native.calls[-1] == ("button", 0, True, 12.5, 8.0)
        return result

    monkeypatch.setattr(input_tools, "_wait_for_rendered_gui_frame", _no_rendered_press)

    result = input_tools.perform_pointer_click(12.5, 8.0)

    assert result["ok"] is False
    assert result["error"]["code"] == "error.click_frame_barrier"
    assert native.calls[-2:] == [
        ("full_speed",),
        ("button", 0, False, 12.5, 8.0),
    ]


def test_semantic_drag_delivers_each_phase_across_rendered_frames(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    native = _FakeNativeEngine()
    monkeypatch.setattr(input_tools, "_native_engine", lambda: native)
    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)
    monkeypatch.setattr(input_tools, "_current_rendered_gui_frame", lambda _timeout: 50)
    monkeypatch.setattr(
        input_tools,
        "_wait_for_rendered_gui_frame",
        lambda previous_frame, **kwargs: {
            "frame": previous_frame + 1,
            "target_found": bool(kwargs.get("expected_target_id")),
            "target_visible": bool(kwargs.get("expected_target_id")),
            "target_enabled": bool(kwargs.get("expected_target_id")),
            "target_under_pointer": bool(kwargs.get("expected_target_id")),
            "target_active": False,
            "matched_target_kind": "node_graph_node_drag_handle",
        },
    )

    result = input_tools.perform_pointer_drag(
        10.0, 20.0, 20.0, 40.0, button=2, steps=2, expected_target_id="canvas:graph"
    )

    assert result["ok"] is True
    assert result["data"]["press_render_frame"] == 52
    assert result["data"]["motion_render_frames"] == [53, 54]
    assert result["data"]["release_render_frame"] == 55
    assert result["data"]["button"] == 2
    assert result["data"]["press_target_reachable"] is True
    assert native.calls == [
        ("full_speed",),
        ("motion", 10.0, 20.0, 0.0, 0.0),
        ("full_speed",),
        ("button", 2, True, 10.0, 20.0),
        ("full_speed",),
        ("motion", 15.0, 30.0, 5.0, 10.0),
        ("full_speed",),
        ("motion", 20.0, 40.0, 5.0, 10.0),
        ("full_speed",),
        ("button", 2, False, 20.0, 40.0),
    ]


def test_key_chord_presses_in_order_and_releases_in_reverse(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    native = _FakeNativeEngine()
    monkeypatch.setattr(input_tools, "_native_engine", lambda: native)

    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)

    result = input_tools.perform_key_chord([224, 4])

    assert result["ok"] is True
    assert result["data"]["press_sequences"] == [1, 2]
    assert result["data"]["release_sequences"] == [3, 4]
    assert native.calls == [
        ("full_speed",),
        ("key", 224, True, False),
        ("full_speed",),
        ("key", 4, True, False),
        ("full_speed",),
        ("key", 4, False, False),
        ("full_speed",),
        ("key", 224, False, False),
    ]


@pytest.mark.parametrize(
    ("key_name", "expected_scancode"),
    [
        ("CTRL", 224),
        ("control", 224),
        ("shift", 225),
        ("alt", 226),
        ("cmd", 227),
        ("ESC", 41),
    ],
)
def test_key_chord_resolves_common_key_aliases(key_name, expected_scancode):
    assert input_tools._resolve_scancode(key_name) == expected_scancode


def test_window_close_request_uses_the_native_close_event_path(tmp_path, monkeypatch):
    session.configure(
        str(tmp_path),
        {"profile": "global_validation", "session": {"build_profile": "debug_feedback"}},
    )
    native = _FakeNativeEngine()
    monkeypatch.setattr(input_tools, "_native_engine", lambda: native)

    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    monkeypatch.setattr(MainThreadCommandQueue, "_instance", queue)

    result = input_tools.perform_window_close_request()

    assert result["ok"] is True
    assert result["data"] == {
        "sequence": 1,
        "last_processed_sequence": 1,
        "pending_event_count": 0,
        "delivered": True,
    }
    assert native.calls == [("full_speed",), ("close",)]


def test_combo_press_is_accepted_when_popup_activation_clears_item_active_state():
    assert input_tools._press_target_accepted(
        {
            "matched_target_kind": "combo",
            "target_found": True,
            "target_visible": True,
            "target_enabled": True,
            "target_under_pointer": True,
            "target_active": False,
        }
    )
