"""Tests for Infernux.input — Input class, KeyCode constants, focus gating (real C++ backend)."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

import Infernux.input as input_module
from Infernux.input import AccelerationEvent, Input, KeyCode, Touch, TouchPhase
from Infernux.lib import InputManager
from Infernux.runtime_services import install_runtime_service, remove_runtime_service


# ═══════════════════════════════════════════════════════════════════════════
# KeyCode constants
# ═══════════════════════════════════════════════════════════════════════════

class TestKeyCode:
    def test_space_scancode(self):
        assert KeyCode.SPACE == 44

    def test_escape_scancode(self):
        assert KeyCode.ESCAPE == 41

    def test_letters_contiguous(self):
        assert KeyCode.A == 4
        assert KeyCode.Z == 29

    def test_arrow_keys(self):
        assert KeyCode.UP_ARROW == 82
        assert KeyCode.DOWN_ARROW == 81
        assert KeyCode.LEFT_ARROW == 80
        assert KeyCode.RIGHT_ARROW == 79

    def test_function_keys(self):
        assert KeyCode.F1 == 58
        assert KeyCode.F12 == 69

    def test_modifiers_exist(self):
        assert KeyCode.LEFT_SHIFT > 0
        assert KeyCode.LEFT_CONTROL > 0
        assert KeyCode.LEFT_ALT > 0


# ═══════════════════════════════════════════════════════════════════════════
# Focus gating
# ═══════════════════════════════════════════════════════════════════════════

class TestFocusGating:
    def test_set_game_focused(self):
        Input.set_game_focused(False)
        assert Input.is_game_focused() is False
        Input.set_game_focused(True)
        assert Input.is_game_focused() is True

    def test_set_game_viewport_origin(self):
        Input.set_game_viewport_origin(100.0, 200.0)
        assert Input._game_viewport_origin == (100.0, 200.0)

    def test_automation_scope_routes_game_input_without_claiming_editor_focus(self):
        Input.set_game_focused(False)

        Input._begin_automation_game_input()
        try:
            assert Input.is_game_focused() is False
            assert Input._accepts_game_input() is True
        finally:
            Input._end_automation_game_input()

        assert Input.is_game_focused() is False
        assert Input._accepts_game_input() is False

    def test_automation_scope_is_nestable_and_clamped(self):
        Input.set_game_focused(False)
        Input._begin_automation_game_input()
        Input._begin_automation_game_input()
        Input._end_automation_game_input()
        assert Input._accepts_game_input() is True
        Input._end_automation_game_input()
        Input._end_automation_game_input()
        assert Input._automation_game_input_depth == 0
        assert Input._accepts_game_input() is False


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_key (real InputManager.name_to_scancode)
# ═══════════════════════════════════════════════════════════════════════════

class TestResolveKey:
    def test_int_passthrough(self):
        assert Input._resolve_key(44) == 44

    def test_string_resolution_space(self):
        assert Input._resolve_key("space") == 44

    def test_string_resolution_a(self):
        assert Input._resolve_key("a") == 4

    def test_unknown_string_returns_negative(self):
        assert Input._resolve_key("nonexistent_key") == -1

    def test_name_to_scancode_consistency(self):
        """Verify real C++ name_to_scancode matches KeyCode constants."""
        assert InputManager.name_to_scancode("space") == KeyCode.SPACE
        assert InputManager.name_to_scancode("a") == KeyCode.A
        assert InputManager.name_to_scancode("d") == KeyCode.D
        assert InputManager.name_to_scancode("w") == KeyCode.W
        assert InputManager.name_to_scancode("left") == KeyCode.LEFT_ARROW
        assert InputManager.name_to_scancode("right") == KeyCode.RIGHT_ARROW
        assert InputManager.name_to_scancode("up") == KeyCode.UP_ARROW
        assert InputManager.name_to_scancode("down") == KeyCode.DOWN_ARROW


# ═══════════════════════════════════════════════════════════════════════════
# Keyboard queries (real C++ backend — idle state)
# ═══════════════════════════════════════════════════════════════════════════

class TestKeyboardQueries:
    def test_get_key_idle_returns_false(self):
        Input._game_focused = True
        assert Input.get_key(KeyCode.W) is False

    def test_get_key_unfocused_returns_false(self):
        Input._game_focused = False
        assert Input.get_key(KeyCode.W) is False

    def test_get_key_down_idle_returns_false(self):
        Input._game_focused = True
        assert Input.get_key_down(KeyCode.SPACE) is False

    def test_get_key_up_idle_returns_false(self):
        Input._game_focused = True
        assert Input.get_key_up(KeyCode.SPACE) is False

    def test_get_key_by_string(self):
        Input._game_focused = True
        assert Input.get_key("w") is False
        assert Input.get_key("space") is False


# ═══════════════════════════════════════════════════════════════════════════
# Mouse queries (real C++ backend — idle state)
# ═══════════════════════════════════════════════════════════════════════════

class TestMouseQueries:
    def test_get_mouse_button_idle(self):
        Input._game_focused = True
        assert Input.get_mouse_button(0) is False
        assert Input.get_mouse_button(1) is False
        assert Input.get_mouse_button(2) is False

    def test_get_mouse_button_unfocused(self):
        Input._game_focused = False
        assert Input.get_mouse_button(0) is False

    def test_get_mouse_frame_state_unfocused(self):
        Input._game_focused = False
        result = Input.get_mouse_frame_state(0)
        assert result == (0.0, 0.0, 0.0, 0.0, False, False, False)

    def test_get_game_mouse_frame_state_with_viewport_offset(self):
        Input._game_focused = True
        Input.set_game_viewport_origin(100.0, 200.0)
        gx, gy, _, _, _, _, _ = Input.get_game_mouse_frame_state(0)
        # Mouse at (0, 0), viewport origin at (100, 200) => game pos (-100, -200)
        assert gx == pytest.approx(-100.0)
        assert gy == pytest.approx(-200.0)

    def test_mouse_position_type(self):
        pos = Input.mouse_position
        assert isinstance(pos, tuple) and len(pos) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Axis queries (real C++ backend — idle state)
# ═══════════════════════════════════════════════════════════════════════════

class TestAxisQueries:
    def test_horizontal_axis_idle(self):
        Input._game_focused = True
        assert Input.get_axis("Horizontal") == pytest.approx(0.0)

    def test_vertical_axis_idle(self):
        Input._game_focused = True
        assert Input.get_axis("Vertical") == pytest.approx(0.0)

    def test_mouse_x_axis_idle(self):
        Input._game_focused = True
        assert Input.get_axis("Mouse X") == pytest.approx(0.0)

    def test_unknown_axis_returns_zero(self):
        Input._game_focused = True
        assert Input.get_axis("NonexistentAxis") == 0.0

    def test_axis_unfocused_returns_zero(self):
        Input._game_focused = False
        assert Input.get_axis("Horizontal") == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Metaclass properties (real C++ backend)
# ═══════════════════════════════════════════════════════════════════════════

class TestInputMetaclassProperties:
    def test_mouse_position_tuple(self):
        pos = Input.mouse_position
        assert isinstance(pos, tuple)
        assert len(pos) == 2

    def test_mouse_scroll_delta_unfocused(self):
        Input._game_focused = False
        assert Input.mouse_scroll_delta == (0.0, 0.0)

    def test_any_key_unfocused(self):
        Input._game_focused = False
        assert Input.any_key is False
        assert Input.any_key_down is False

    def test_any_key_idle(self):
        Input._game_focused = True
        assert Input.any_key is False

    def test_input_string_unfocused(self):
        Input._game_focused = False
        assert Input.input_string == ""

    def test_touch_count_unfocused(self):
        Input._game_focused = False
        assert Input.touch_count == 0
        assert Input.touches == ()

    def test_touch_public_types(self):
        touch = Touch(
            touch_id=1,
            finger_id=2,
            timestamp_ns=3,
            window_id=4,
            position=(480.0, 270.0),
            raw_position=(480.0, 270.0),
            delta_position=(12.0, -8.0),
            normalized_position=(0.25, 0.25),
            normalized_delta_position=(0.01, -0.01),
            delta_time=1.0 / 60.0,
            pressure=0.5,
            contact_size=(0.02, 0.03),
            is_primary=True,
            cancel_reason="",
            phase=TouchPhase.MOVED,
        )
        assert touch.phase is TouchPhase.MOVED
        assert touch.position == (480.0, 270.0)
        assert touch.normalized_position == (0.25, 0.25)
        assert touch.contact_size == (0.02, 0.03)
        assert touch.is_primary

    def test_motion_sensor_properties_publish_native_samples(self, monkeypatch):
        manager = SimpleNamespace(
            accelerometer_supported=True,
            gyroscope_supported=True,
            acceleration=(0.25, -0.5, 1.0),
            gyroscope_rotation_rate=(0.1, 0.2, -0.3),
            acceleration_events=(
                SimpleNamespace(acceleration=(0.0, 1.0, 0.0), delta_time=0.0),
                SimpleNamespace(acceleration=(0.1, 0.9, 0.0), delta_time=0.02),
            ),
        )
        monkeypatch.setattr(
            input_module,
            "_NativeInputManager",
            SimpleNamespace(instance=lambda: manager),
        )

        assert Input.accelerometer_supported
        assert Input.gyroscope_supported
        assert Input.acceleration == pytest.approx((0.25, -0.5, 1.0))
        assert Input.gyroscope_rotation_rate == pytest.approx((0.1, 0.2, -0.3))
        assert Input.acceleration_event_count == 2
        assert Input.acceleration_events == (
            AccelerationEvent((0.0, 1.0, 0.0), 0.0),
            AccelerationEvent((0.1, 0.9, 0.0), 0.02),
        )

    def test_native_touch_is_published_in_unity_screen_pixels(self, monkeypatch):
        native_touch = SimpleNamespace(
            touch_id=7,
            finger_id=11,
            timestamp_ns=2_000_000_000,
            window_id=3,
            x=0.25,
            y=0.75,
            delta_x=0.1,
            delta_y=-0.2,
            delta_time=0.016,
            pressure=0.6,
            contact_width=0.02,
            contact_height=0.03,
            is_primary=True,
            cancel_reason="",
            phase="moved",
        )
        manager = SimpleNamespace(
            screen_state=SimpleNamespace(logical_width=1920, logical_height=1080)
        )
        monkeypatch.setattr(
            input_module,
            "_NativeInputManager",
            SimpleNamespace(instance=lambda: manager),
        )

        touch = Input._from_native_touch(native_touch)

        assert touch.position == pytest.approx((480.0, 270.0))
        assert touch.raw_position == touch.position
        assert touch.delta_position == pytest.approx((192.0, 216.0))
        assert touch.normalized_position == pytest.approx((0.25, 0.25))
        assert touch.normalized_delta_position == pytest.approx((0.1, 0.2))
        assert touch.contact_size == pytest.approx((38.4, 32.4))
        assert touch.delta_time == pytest.approx(0.016)
        assert touch.phase is TouchPhase.MOVED

    def test_platform_text_input_runtime_service(self):
        class _TextInputService:
            def __init__(self):
                self.active = False
                self.request = None

            def begin_text_input(self, initial_value, input_type):
                self.request = (initial_value, input_type)
                self.active = True
                return True

            def end_text_input(self):
                self.active = False

            def is_text_input_active(self):
                return self.active

        service = _TextInputService()
        install_runtime_service("text-input", service)
        try:
            assert Input.begin_text_input("hello", "email")
            assert service.request == ("hello", "email")
            assert Input.is_text_input_active()
            Input.end_text_input()
            assert not Input.is_text_input_active()
        finally:
            remove_runtime_service("text-input", service)

    def test_input_string_idle(self):
        Input._game_focused = True
        assert isinstance(Input.input_string, str)
