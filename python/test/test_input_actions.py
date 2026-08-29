from __future__ import annotations

import math

import Infernux.input.actions as action_module
from Infernux.input import (
    InputAction,
    InputActionMap,
    InputActionPhase,
    InputActionType,
    KeyCode,
)


class _FakeInput:
    frame_index = 1
    held: set[str | int] = set()
    down: set[str | int] = set()
    up: set[str | int] = set()
    axes: dict[str, float] = {}

    @classmethod
    def get_key(cls, key):
        return key in cls.held

    @classmethod
    def get_key_down(cls, key):
        return key in cls.down

    @classmethod
    def get_key_up(cls, key):
        return key in cls.up

    @classmethod
    def get_axis_raw(cls, name):
        return cls.axes.get(name, 0.0)


def _reset_fake(monkeypatch):
    _FakeInput.frame_index = 1
    _FakeInput.held = set()
    _FakeInput.down = set()
    _FakeInput.up = set()
    _FakeInput.axes = {}
    monkeypatch.setattr(action_module, "Input", _FakeInput)


def test_standard_gameplay_map_combines_keyboard_move(monkeypatch):
    _reset_fake(monkeypatch)
    actions = InputActionMap.standard_gameplay()
    _FakeInput.held = {KeyCode.W, KeyCode.D}
    _FakeInput.down = {KeyCode.W, KeyCode.D}

    x, y = actions["move"].read_value()
    assert math.isclose(x, math.sqrt(0.5), rel_tol=1e-6)
    assert math.isclose(y, math.sqrt(0.5), rel_tol=1e-6)
    assert actions["Move"].was_pressed_this_frame
    assert actions["Move"].phase is InputActionPhase.PERFORMED


def test_standard_cancel_accepts_android_back(monkeypatch):
    _reset_fake(monkeypatch)
    actions = InputActionMap.standard_gameplay()
    _FakeInput.held = {KeyCode.AC_BACK}
    _FakeInput.down = {KeyCode.AC_BACK}

    assert actions["Cancel"].is_pressed
    assert actions["Cancel"].was_pressed_this_frame


def test_virtual_vector_source_has_frame_stable_edges(monkeypatch):
    _reset_fake(monkeypatch)
    move = InputAction("Move", InputActionType.VECTOR2)
    move.set_virtual_value("touch-stick", (0.75, 0.0))
    assert move.read_value() == (0.75, 0.0)
    assert move.was_pressed_this_frame
    assert move.was_pressed_this_frame

    _FakeInput.frame_index = 2
    assert not move.was_pressed_this_frame
    move.clear_virtual_value("touch-stick")
    assert move.read_value() == (0.0, 0.0)
    assert move.was_released_this_frame
    assert move.phase is InputActionPhase.CANCELED


def test_virtual_sources_compose_without_fake_keys(monkeypatch):
    _reset_fake(monkeypatch)
    move = InputAction("Move", InputActionType.VECTOR2)
    move.set_virtual_value("left-stick", (0.8, 0.0))
    move.set_virtual_value("accessibility", (0.0, 0.8))
    x, y = move.read_value()
    assert math.isclose(math.hypot(x, y), 1.0, rel_tol=1e-6)

    move.remove_virtual_source("accessibility")
    assert move.read_value() == (0.8, 0.0)


def test_virtual_button_tap_in_one_frame_preserves_both_edges(monkeypatch):
    _reset_fake(monkeypatch)
    submit = InputAction("Submit")
    submit.set_virtual_value("touch-button", 1.0)
    submit.clear_virtual_value("touch-button")

    assert not submit.is_pressed
    assert submit.was_pressed_this_frame
    assert submit.was_released_this_frame


def test_zero_press_point_does_not_make_idle_action_pressed(monkeypatch):
    _reset_fake(monkeypatch)
    action = InputAction("Idle", press_point=0.0)

    assert not action.is_pressed
    assert action.phase is InputActionPhase.WAITING


def test_button_and_axis_bindings_publish_edges_and_values(monkeypatch):
    _reset_fake(monkeypatch)
    submit = InputAction("Submit").bind_key(KeyCode.RETURN).bind_key(KeyCode.SPACE)
    throttle = InputAction("Throttle", InputActionType.AXIS).bind_key(
        KeyCode.W, scale=1.0
    ).bind_key(KeyCode.S, scale=-1.0)

    _FakeInput.held = {KeyCode.RETURN, KeyCode.W}
    _FakeInput.down = {KeyCode.RETURN, KeyCode.W}
    assert submit.is_pressed
    assert submit.was_pressed_this_frame
    assert throttle.read_value() == 1.0

    _FakeInput.frame_index = 2
    _FakeInput.held = set()
    _FakeInput.down = set()
    _FakeInput.up = {KeyCode.RETURN, KeyCode.W}
    assert submit.was_released_this_frame
    assert throttle.was_released_this_frame


def test_releasing_one_binding_does_not_cancel_an_action_still_held(monkeypatch):
    _reset_fake(monkeypatch)
    submit = InputAction("Submit").bind_key(KeyCode.RETURN).bind_key(KeyCode.SPACE)
    _FakeInput.frame_index = 2
    _FakeInput.held = {KeyCode.SPACE}
    _FakeInput.up = {KeyCode.RETURN}

    assert submit.is_pressed
    assert not submit.was_released_this_frame


def test_pointer_look_and_map_disable(monkeypatch):
    _reset_fake(monkeypatch)
    actions = InputActionMap.standard_gameplay()
    _FakeInput.axes = {"Mouse X": 0.25, "Mouse Y": -0.5}
    assert actions["Look"].read_value() == (0.25, -0.5)

    actions.disable()
    assert actions["Look"].read_value() == (0.0, 0.0)
    actions.enable()
    assert actions["Look"].read_value() == (0.25, -0.5)


def test_action_binding_types_are_strict(monkeypatch):
    _reset_fake(monkeypatch)
    vector = InputAction("Move", InputActionType.VECTOR2)
    button = InputAction("Submit")

    try:
        vector.bind_key(KeyCode.W)
    except TypeError:
        pass
    else:
        raise AssertionError("Vector2 action accepted a scalar key binding")

    try:
        button.bind_pointer_delta()
    except TypeError:
        pass
    else:
        raise AssertionError("Button action accepted pointer delta")
