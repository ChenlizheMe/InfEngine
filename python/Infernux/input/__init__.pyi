from __future__ import annotations

from enum import Enum
from typing import Tuple, Union
from .ime import ImeInputState
from .actions import InputAction, InputActionMap, InputActionPhase, InputActionType


class TouchPhase(str, Enum):
    BEGAN: TouchPhase
    MOVED: TouchPhase
    STATIONARY: TouchPhase
    ENDED: TouchPhase
    CANCELED: TouchPhase


class Touch:
    touch_id: int
    finger_id: int
    timestamp_ns: int
    window_id: int
    position: Tuple[float, float]
    delta_position: Tuple[float, float]
    pressure: float
    contact_size: Tuple[float, float]
    is_primary: bool
    cancel_reason: str
    phase: TouchPhase


class KeyCode:
    """Key code constants for keyboard input."""

    NONE: int
    BACKSPACE: int
    TAB: int
    RETURN: int
    ESCAPE: int
    SPACE: int
    DELETE: int

    ALPHA0: int
    ALPHA1: int
    ALPHA2: int
    ALPHA3: int
    ALPHA4: int
    ALPHA5: int
    ALPHA6: int
    ALPHA7: int
    ALPHA8: int
    ALPHA9: int

    A: int; B: int; C: int; D: int; E: int; F: int; G: int
    H: int; I: int; J: int; K: int; L: int; M: int; N: int
    O: int; P: int; Q: int; R: int; S: int; T: int; U: int
    V: int; W: int; X: int; Y: int; Z: int

    F1: int; F2: int; F3: int; F4: int; F5: int; F6: int
    F7: int; F8: int; F9: int; F10: int; F11: int; F12: int

    UP_ARROW: int
    DOWN_ARROW: int
    LEFT_ARROW: int
    RIGHT_ARROW: int

    LEFT_SHIFT: int
    RIGHT_SHIFT: int
    LEFT_CONTROL: int
    RIGHT_CONTROL: int
    LEFT_ALT: int
    RIGHT_ALT: int
    LEFT_COMMAND: int
    RIGHT_COMMAND: int

    KEYPAD0: int; KEYPAD1: int; KEYPAD2: int; KEYPAD3: int
    KEYPAD4: int; KEYPAD5: int; KEYPAD6: int; KEYPAD7: int
    KEYPAD8: int; KEYPAD9: int
    KEYPAD_PERIOD: int
    KEYPAD_DIVIDE: int
    KEYPAD_MULTIPLY: int
    KEYPAD_MINUS: int
    KEYPAD_PLUS: int
    KEYPAD_ENTER: int

    MINUS: int
    EQUALS: int
    LEFT_BRACKET: int
    RIGHT_BRACKET: int
    BACKSLASH: int
    SEMICOLON: int
    QUOTE: int
    BACKQUOTE: int
    COMMA: int
    PERIOD: int
    SLASH: int

    CAPS_LOCK: int
    INSERT: int
    HOME: int
    END: int
    PAGE_UP: int
    PAGE_DOWN: int
    PRINT_SCREEN: int
    SCROLL_LOCK: int
    PAUSE: int
    NUM_LOCK: int


class Input:
    """Interface for reading input from keyboard, mouse, and touch."""

    # Class-level properties (via _InputMeta metaclass)
    frame_index: int
    """Monotonic identity of the current native input frame."""
    mouse_position: Tuple[float, float]
    """The current mouse position in screen coordinates."""
    game_mouse_position: Tuple[float, float]
    """The current mouse position in game viewport coordinates."""
    mouse_scroll_delta: Tuple[float, float]
    """The mouse scroll delta for the current frame."""
    input_string: str
    """Characters typed by the user in the current frame."""
    any_key: bool
    """Returns True while any key or mouse button is held down."""
    any_key_down: bool
    """Returns True during the frame any key or mouse button is first pressed."""
    touch_count: int
    """Number of touch contacts in the current frame snapshot."""
    touches: Tuple[Touch, ...]
    """All touch contacts in stable first-contact order."""
    mouse_sensitivity: float
    """Mouse sensitivity multiplier (default 0.1)."""

    @staticmethod
    def set_game_focused(focused: bool) -> None:
        """Set whether the game viewport has input focus."""
        ...
    @staticmethod
    def set_game_viewport_origin(x: float, y: float) -> None:
        """Set the game viewport origin in screen coordinates."""
        ...
    @staticmethod
    def is_game_focused() -> bool:
        """Returns True if the game viewport has input focus."""
        ...

    @staticmethod
    def get_key(key: Union[str, int]) -> bool:
        """Returns True while the user holds down the specified key."""
        ...
    @staticmethod
    def get_key_down(key: Union[str, int]) -> bool:
        """Returns True during the frame the user starts pressing the key."""
        ...
    @staticmethod
    def get_key_up(key: Union[str, int]) -> bool:
        """Returns True during the frame the user releases the key."""
        ...

    @staticmethod
    def get_mouse_button(button: int) -> bool:
        """Returns True while the given mouse button is held down."""
        ...
    @staticmethod
    def get_mouse_button_down(button: int) -> bool:
        """Returns True during the frame the mouse button was pressed."""
        ...
    @staticmethod
    def get_mouse_button_up(button: int) -> bool:
        """Returns True during the frame the mouse button was released."""
        ...

    @staticmethod
    def get_touch(index: int) -> Touch:
        """Return one touch from the current frame snapshot."""
        ...

    @staticmethod
    def begin_text_input(initial_value: str = ..., input_type: str = ...) -> bool:
        """Begin platform text input and show a software keyboard when available."""
        ...
    @staticmethod
    def end_text_input() -> None:
        """End platform text input and dismiss its software keyboard."""
        ...
    @staticmethod
    def is_text_input_active() -> bool:
        """Return whether gameplay requested platform text input."""
        ...

    @staticmethod
    def get_mouse_frame_state(button: int = ...) -> Tuple[float, float, float, float, bool, bool, bool]:
        """Get comprehensive mouse state for the current frame."""
        ...
    @staticmethod
    def get_game_mouse_frame_state(button: int = ...) -> Tuple[float, float, float, float, bool, bool, bool]:
        """Get comprehensive game-viewport mouse state for the current frame."""
        ...
    @staticmethod
    def set_cursor_locked(locked: bool) -> None:
        """Lock or unlock the cursor."""
        ...
    @staticmethod
    def is_cursor_locked() -> bool:
        """Returns True if the cursor is currently locked."""
        ...

    @staticmethod
    def get_axis(axis_name: str) -> float:
        """Returns the value of the virtual axis identified by axis_name."""
        ...
    @staticmethod
    def get_axis_raw(axis_name: str) -> float:
        """Returns the raw value of the virtual axis with no smoothing."""
        ...

    @staticmethod
    def reset_input_axes() -> None:
        """Reset all input axes to zero."""
        ...


__all__ = [
    "Input",
    "InputAction",
    "InputActionMap",
    "InputActionPhase",
    "InputActionType",
    "KeyCode",
    "Touch",
    "TouchPhase",
]
