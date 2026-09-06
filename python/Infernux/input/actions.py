"""Device-independent gameplay actions.

Actions combine physical bindings and named virtual sources.  A mobile stick,
for example, writes a vector to ``Move`` without synthesizing keyboard events;
the same game script can keep reading the action on desktop, Android, and Web.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

from . import Input, KeyCode


Vector2: TypeAlias = tuple[float, float]
ActionValue: TypeAlias = float | Vector2


class InputActionType(str, Enum):
    BUTTON = "button"
    AXIS = "axis"
    VECTOR2 = "vector2"


class InputActionPhase(str, Enum):
    WAITING = "waiting"
    PERFORMED = "performed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class _KeyBinding:
    key: str | int
    scale: float


@dataclass(frozen=True, slots=True)
class _Composite2DBinding:
    up: str | int
    down: str | int
    left: str | int
    right: str | int
    scale: float


@dataclass(frozen=True, slots=True)
class _PointerDeltaBinding:
    scale: float
    invert_y: bool


@dataclass(slots=True)
class _VirtualSource:
    value: ActionValue
    previous: ActionValue
    changed_frame: int
    pressed_frame: int = -1
    released_frame: int = -1


@dataclass(frozen=True, slots=True)
class _ActionSample:
    current: ActionValue
    previous: ActionValue
    pressed: bool
    released: bool


def _zero(action_type: InputActionType) -> ActionValue:
    return (0.0, 0.0) if action_type is InputActionType.VECTOR2 else 0.0


def _as_vector(value: ActionValue) -> Vector2:
    if isinstance(value, tuple) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    raise TypeError("Vector2 actions require a two-number tuple")


def _magnitude(value: ActionValue) -> float:
    if isinstance(value, tuple):
        return math.hypot(float(value[0]), float(value[1]))
    return abs(float(value))


def _clamp_vector(value: Vector2) -> Vector2:
    length = math.hypot(value[0], value[1])
    if length <= 1.0 or length == 0.0:
        return value
    return (value[0] / length, value[1] / length)


class InputAction:
    """One named gameplay intent with physical and virtual bindings."""

    def __init__(
        self,
        name: str,
        action_type: InputActionType | str = InputActionType.BUTTON,
        *,
        press_point: float = 0.5,
    ) -> None:
        cleaned = str(name).strip()
        if not cleaned:
            raise ValueError("InputAction.name is required")
        self.name = cleaned
        self.action_type = InputActionType(action_type)
        self.press_point = max(1e-6, min(1.0, float(press_point)))
        self._enabled = True
        self._key_bindings: list[_KeyBinding] = []
        self._composite_bindings: list[_Composite2DBinding] = []
        self._pointer_bindings: list[_PointerDeltaBinding] = []
        self._virtual_sources: dict[str, _VirtualSource] = {}
        self._sample_frame = -1
        zero = _zero(self.action_type)
        self._sample_cache = _ActionSample(zero, zero, False, False)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        self._sample_frame = -1

    def bind_key(self, key: str | int, *, scale: float = 1.0) -> InputAction:
        """Add a key to a button or one-dimensional axis."""
        if self.action_type is InputActionType.VECTOR2:
            raise TypeError("Use bind_2d_composite for a Vector2 action")
        self._key_bindings.append(_KeyBinding(key, float(scale)))
        self._sample_frame = -1
        return self

    def bind_2d_composite(
        self,
        *,
        up: str | int,
        down: str | int,
        left: str | int,
        right: str | int,
        scale: float = 1.0,
    ) -> InputAction:
        """Add a digital four-direction binding to a Vector2 action."""
        if self.action_type is not InputActionType.VECTOR2:
            raise TypeError("2D composites require a Vector2 action")
        self._composite_bindings.append(
            _Composite2DBinding(up, down, left, right, float(scale))
        )
        self._sample_frame = -1
        return self

    def bind_pointer_delta(
        self, *, scale: float = 1.0, invert_y: bool = False
    ) -> InputAction:
        """Add per-frame pointer motion to a Vector2 action."""
        if self.action_type is not InputActionType.VECTOR2:
            raise TypeError("Pointer delta requires a Vector2 action")
        self._pointer_bindings.append(_PointerDeltaBinding(float(scale), bool(invert_y)))
        self._sample_frame = -1
        return self

    def set_virtual_value(self, source: str, value: ActionValue) -> None:
        """Publish one virtual control contribution for the current frame."""
        source_id = str(source).strip()
        if not source_id:
            raise ValueError("Virtual input source is required")
        normalized: ActionValue
        if self.action_type is InputActionType.VECTOR2:
            normalized = _clamp_vector(_as_vector(value))
        else:
            normalized = max(-1.0, min(1.0, float(value)))
            if self.action_type is InputActionType.BUTTON:
                normalized = max(0.0, normalized)

        frame = Input.frame_index
        source_state = self._virtual_sources.get(source_id)
        if source_state is None:
            zero = _zero(self.action_type)
            source_state = _VirtualSource(normalized, zero, frame)
            self._virtual_sources[source_id] = source_state
            previous_value = zero
        else:
            previous_value = source_state.value
            if source_state.changed_frame != frame:
                source_state.previous = source_state.value
                source_state.changed_frame = frame
            source_state.value = normalized
        was_pressed = _magnitude(previous_value) >= self.press_point
        is_pressed = _magnitude(normalized) >= self.press_point
        if is_pressed and not was_pressed:
            source_state.pressed_frame = frame
        elif was_pressed and not is_pressed:
            source_state.released_frame = frame
        self._sample_frame = -1

    def clear_virtual_value(self, source: str) -> None:
        """Release one virtual control without affecting other contributors."""
        if str(source).strip() in self._virtual_sources:
            self.set_virtual_value(str(source).strip(), _zero(self.action_type))

    def remove_virtual_source(self, source: str) -> None:
        self._virtual_sources.pop(str(source).strip(), None)
        self._sample_frame = -1

    def read_value(self) -> ActionValue:
        return self._sample().current

    @property
    def is_pressed(self) -> bool:
        return _magnitude(self._sample().current) >= self.press_point

    @property
    def was_pressed_this_frame(self) -> bool:
        return self._sample().pressed

    @property
    def was_released_this_frame(self) -> bool:
        return self._sample().released

    @property
    def phase(self) -> InputActionPhase:
        sample = self._sample()
        if sample.released and _magnitude(sample.current) < self.press_point:
            return InputActionPhase.CANCELED
        if _magnitude(sample.current) >= self.press_point:
            return InputActionPhase.PERFORMED
        return InputActionPhase.WAITING

    def _sample(self) -> _ActionSample:
        frame = Input.frame_index
        if self._sample_frame == frame:
            return self._sample_cache
        self._sample_frame = frame
        if not self.enabled:
            zero = _zero(self.action_type)
            self._sample_cache = _ActionSample(zero, zero, False, False)
            return self._sample_cache

        current = _zero(self.action_type)
        previous = _zero(self.action_type)
        direct_pressed = False
        direct_released = False

        if self.action_type is not InputActionType.VECTOR2:
            current_scalar = 0.0
            previous_scalar = 0.0
            for binding in self._key_bindings:
                held = Input.get_key(binding.key)
                down = Input.get_key_down(binding.key)
                up = Input.get_key_up(binding.key)
                current_scalar += binding.scale if held else 0.0
                previous_held = (not down and held) or up
                previous_scalar += binding.scale if previous_held else 0.0
                direct_pressed = direct_pressed or down
                direct_released = direct_released or up
            current = max(-1.0, min(1.0, current_scalar))
            previous = max(-1.0, min(1.0, previous_scalar))
        else:
            current_x = current_y = previous_x = previous_y = 0.0
            for binding in self._composite_bindings:
                keys = (binding.up, binding.down, binding.left, binding.right)
                held = tuple(Input.get_key(key) for key in keys)
                down = tuple(Input.get_key_down(key) for key in keys)
                up = tuple(Input.get_key_up(key) for key in keys)
                previous_held = tuple(
                    (not down[index] and held[index]) or up[index]
                    for index in range(4)
                )
                current_x += (float(held[3]) - float(held[2])) * binding.scale
                current_y += (float(held[0]) - float(held[1])) * binding.scale
                previous_x += (
                    float(previous_held[3]) - float(previous_held[2])
                ) * binding.scale
                previous_y += (
                    float(previous_held[0]) - float(previous_held[1])
                ) * binding.scale
                direct_pressed = direct_pressed or any(down)
                direct_released = direct_released or any(up)
            for binding in self._pointer_bindings:
                current_x += Input.get_axis_raw("Mouse X") * binding.scale
                pointer_y = Input.get_axis_raw("Mouse Y") * binding.scale
                current_y += -pointer_y if binding.invert_y else pointer_y
            current = _clamp_vector((current_x, current_y))
            previous = _clamp_vector((previous_x, previous_y))

        for source in self._virtual_sources.values():
            source_previous = (
                source.previous if source.changed_frame == frame else source.value
            )
            direct_pressed = direct_pressed or source.pressed_frame == frame
            direct_released = direct_released or source.released_frame == frame
            if self.action_type is InputActionType.VECTOR2:
                cx, cy = _as_vector(current)
                vx, vy = _as_vector(source.value)
                px, py = _as_vector(previous)
                pvx, pvy = _as_vector(source_previous)
                current = _clamp_vector((cx + vx, cy + vy))
                previous = _clamp_vector((px + pvx, py + pvy))
            else:
                current = max(-1.0, min(1.0, float(current) + float(source.value)))
                previous = max(
                    -1.0, min(1.0, float(previous) + float(source_previous))
                )

        current_pressed = _magnitude(current) >= self.press_point
        previous_pressed = _magnitude(previous) >= self.press_point
        same_frame_tap = direct_pressed and direct_released and not current_pressed
        self._sample_cache = _ActionSample(
            current,
            previous,
            (not previous_pressed and current_pressed) or same_frame_tap,
            (previous_pressed and not current_pressed) or same_frame_tap,
        )
        return self._sample_cache


class InputActionMap:
    """Named collection of actions that can be enabled as one unit."""

    def __init__(self, name: str = "Gameplay") -> None:
        self.name = str(name).strip() or "Gameplay"
        self.enabled = True
        self._actions: dict[str, InputAction] = {}

    def add_action(
        self,
        name: str,
        action_type: InputActionType | str = InputActionType.BUTTON,
        *,
        press_point: float = 0.5,
    ) -> InputAction:
        key = str(name).strip().casefold()
        if not key:
            raise ValueError("Input action name is required")
        if key in self._actions:
            raise ValueError(f"Input action already exists: {name}")
        action = InputAction(name, action_type, press_point=press_point)
        action.enabled = self.enabled
        self._actions[key] = action
        return action

    def find_action(self, name: str) -> InputAction | None:
        return self._actions.get(str(name).strip().casefold())

    def __getitem__(self, name: str) -> InputAction:
        action = self.find_action(name)
        if action is None:
            raise KeyError(name)
        return action

    @property
    def actions(self) -> tuple[InputAction, ...]:
        return tuple(self._actions.values())

    def enable(self) -> None:
        self.enabled = True
        for action in self._actions.values():
            action.enabled = True

    def disable(self) -> None:
        self.enabled = False
        for action in self._actions.values():
            action.enabled = False

    def remove_virtual_source(self, source: str) -> None:
        for action in self._actions.values():
            action.remove_virtual_source(source)

    @classmethod
    def standard_gameplay(cls) -> InputActionMap:
        """Create the engine's portable Move/Look/Submit/Cancel/Pause map."""
        result = cls("Gameplay")
        result.add_action("Move", InputActionType.VECTOR2).bind_2d_composite(
            up=KeyCode.W,
            down=KeyCode.S,
            left=KeyCode.A,
            right=KeyCode.D,
        ).bind_2d_composite(
            up=KeyCode.UP_ARROW,
            down=KeyCode.DOWN_ARROW,
            left=KeyCode.LEFT_ARROW,
            right=KeyCode.RIGHT_ARROW,
        )
        result.add_action("Look", InputActionType.VECTOR2).bind_pointer_delta()
        result.add_action("Submit").bind_key(KeyCode.RETURN).bind_key(KeyCode.SPACE)
        result.add_action("Cancel").bind_key(KeyCode.ESCAPE).bind_key(KeyCode.AC_BACK)
        result.add_action("Pause").bind_key(KeyCode.P)
        return result


__all__ = [
    "ActionValue",
    "InputAction",
    "InputActionMap",
    "InputActionPhase",
    "InputActionType",
    "Vector2",
]
