"""
Infernux Input — Unity-style static input query API (snake_case).

Provides a static ``Input`` class whose API mirrors ``UnityEngine.Input``
but uses Python-idiomatic snake_case naming.

Quick reference (Unity → Infernux):
    ``Input.GetKey("w")``            → ``Input.get_key("w")``  *(held this frame)*
    ``Input.GetKeyDown(KeyCode.Space)`` → ``Input.get_key_down(KeyCode.SPACE)``  *(first frame only)*
    ``Input.GetMouseButton(0)``      → ``Input.get_mouse_button(0)``
    ``Input.mousePosition``          → ``Input.mouse_position``
    ``Input.GetAxis("Horizontal")``  → ``Input.get_axis("Horizontal")``

Example::

    from Infernux.input import Input, KeyCode

    class PlayerController(InxComponent):
        def update(self):
            if Input.get_key("w"):
                # move forward
                ...
            if Input.get_mouse_button_down(0):
                # fire
                ...
            h = Input.get_axis("Horizontal")
            v = Input.get_axis("Vertical")
"""

from __future__ import annotations

from typing import Tuple, Union

from Infernux.lib import InputManager as _NativeInputManager


# =============================================================================
# _InputMeta — metaclass for class-level properties (Input.mouse_position etc.)
# =============================================================================

class _InputMeta(type):
    """Metaclass that enables ``Input.mouse_position`` (no parentheses)."""

    @property
    def mouse_position(cls) -> Tuple[float, float]:
        """Current mouse position as ``(x, y)`` in window-space pixels."""
        mgr = _NativeInputManager.instance()
        return (mgr.mouse_position_x, mgr.mouse_position_y)

    @property
    def game_mouse_position(cls) -> Tuple[float, float]:
        """Mouse position relative to the Game View viewport.

        Returns ``(x, y)`` in viewport pixels where ``(0, 0)`` is the
        top-left corner of the game image.  Coordinates may be negative
        or exceed viewport size when the cursor is outside.
        """
        mgr = _NativeInputManager.instance()
        abs_x, abs_y = mgr.mouse_position_x, mgr.mouse_position_y
        vx, vy = cls._game_viewport_origin
        return (abs_x - vx, abs_y - vy)

    @property
    def mouse_scroll_delta(cls) -> Tuple[float, float]:
        """Scroll delta as ``(x, y)`` this frame (positive y = scroll up)."""
        if not cls._accepts_game_input():
            return (0.0, 0.0)
        mgr = _NativeInputManager.instance()
        return (mgr.mouse_scroll_delta_x, mgr.mouse_scroll_delta_y)

    @property
    def input_string(cls) -> str:
        """Characters typed this frame (UTF-8)."""
        if not cls._accepts_game_input():
            return ""
        return _NativeInputManager.instance().input_string

    @property
    def any_key(cls) -> bool:
        """``True`` if any key or mouse button is currently held."""
        if not cls._accepts_game_input():
            return False
        return _NativeInputManager.instance().any_key()

    @property
    def any_key_down(cls) -> bool:
        """``True`` during the frame any key was first pressed."""
        if not cls._accepts_game_input():
            return False
        return _NativeInputManager.instance().any_key_down()

    @property
    def touch_count(cls) -> int:
        """Number of active touch contacts this frame."""
        if not cls._accepts_game_input():
            return 0
        return _NativeInputManager.instance().touch_count


# =============================================================================
# KeyCode — Unity-compatible key code constants
# =============================================================================

class KeyCode:
    """
    Unity-compatible key code constants mapped to SDL scancodes.

    Each constant holds the SDL scancode integer that can be passed directly
    to ``Input.get_key()``, ``Input.get_key_down()``, ``Input.get_key_up()``.

    You can also pass string names (e.g. ``"space"``, ``"a"``) — the ``Input``
    class resolves them automatically via ``InputManager.name_to_scancode()``.
    """

    # --- Special ---
    NONE = 0
    BACKSPACE = 42   # SDL_SCANCODE_BACKSPACE
    TAB = 43         # SDL_SCANCODE_TAB
    RETURN = 40      # SDL_SCANCODE_RETURN
    ESCAPE = 41      # SDL_SCANCODE_ESCAPE
    SPACE = 44       # SDL_SCANCODE_SPACE
    DELETE = 76       # SDL_SCANCODE_DELETE

    # --- Digits (top row) ---
    ALPHA0 = 39      # SDL_SCANCODE_0
    ALPHA1 = 30
    ALPHA2 = 31
    ALPHA3 = 32
    ALPHA4 = 33
    ALPHA5 = 34
    ALPHA6 = 35
    ALPHA7 = 36
    ALPHA8 = 37
    ALPHA9 = 38

    # --- Letters ---
    A = 4;  B = 5;  C = 6;  D = 7;  E = 8;  F = 9;  G = 10
    H = 11; I = 12; J = 13; K = 14; L = 15; M = 16; N = 17
    O = 18; P = 19; Q = 20; R = 21; S = 22; T = 23; U = 24
    V = 25; W = 26; X = 27; Y = 28; Z = 29

    # --- Function keys ---
    F1 = 58;  F2 = 59;  F3 = 60;  F4 = 61;  F5 = 62;  F6 = 63
    F7 = 64;  F8 = 65;  F9 = 66;  F10 = 67; F11 = 68; F12 = 69

    # --- Arrow keys ---
    UP_ARROW = 82      # SDL_SCANCODE_UP
    DOWN_ARROW = 81    # SDL_SCANCODE_DOWN
    LEFT_ARROW = 80    # SDL_SCANCODE_LEFT
    RIGHT_ARROW = 79   # SDL_SCANCODE_RIGHT

    # --- Modifiers ---
    LEFT_SHIFT = 225
    RIGHT_SHIFT = 229
    LEFT_CONTROL = 224
    RIGHT_CONTROL = 228
    LEFT_ALT = 226
    RIGHT_ALT = 230
    LEFT_COMMAND = 227   # Left GUI / Super / Windows
    RIGHT_COMMAND = 231

    # --- Numpad ---
    KEYPAD0 = 98;  KEYPAD1 = 89; KEYPAD2 = 90; KEYPAD3 = 91
    KEYPAD4 = 92;  KEYPAD5 = 93; KEYPAD6 = 94; KEYPAD7 = 95
    KEYPAD8 = 96;  KEYPAD9 = 97
    KEYPAD_PERIOD = 99
    KEYPAD_DIVIDE = 84
    KEYPAD_MULTIPLY = 85
    KEYPAD_MINUS = 86
    KEYPAD_PLUS = 87
    KEYPAD_ENTER = 88

    # --- Punctuation / symbols ---
    MINUS = 45          # -
    EQUALS = 46         # =
    LEFT_BRACKET = 47   # [
    RIGHT_BRACKET = 48  # ]
    BACKSLASH = 49
    SEMICOLON = 51
    QUOTE = 52          # '
    BACKQUOTE = 53      # `
    COMMA = 54
    PERIOD = 55
    SLASH = 56

    # --- Misc ---
    CAPS_LOCK = 57
    INSERT = 73
    HOME = 74
    END = 77
    PAGE_UP = 75
    PAGE_DOWN = 78
    PRINT_SCREEN = 70
    SCROLL_LOCK = 71
    PAUSE = 72
    NUM_LOCK = 83


# =============================================================================
# Input — static query class (Unity Input drop-in)
# =============================================================================

class Input(metaclass=_InputMeta):
    """
    Static input query class — mirrors ``UnityEngine.Input`` in snake_case.

    All methods are class-level (``@staticmethod``).  No instantiation needed.
    Properties like ``Input.mouse_position`` are accessed without parentheses
    (powered by the ``_InputMeta`` metaclass).

    The class holds an internal ``_game_focused`` flag that, when ``False``,
    causes all query methods to return idle values.  This lets the editor
    suppress game-object input when the Game View is not focused.
    """

    _game_focused: bool = True
    """When False, all queries return idle / zero values."""

    _automation_game_input_depth: int = 0
    """Internal scoped bypass used by background gameplay validation."""

    _game_viewport_origin: Tuple[float, float] = (0.0, 0.0)
    """Top-left corner of the game image in absolute window pixels."""

    mouse_sensitivity: float = 0.1
    """Sensitivity multiplier applied to Mouse X / Mouse Y axes.

    Raw pixel deltas from the OS are multiplied by this value before
    being returned by ``get_axis("Mouse X")`` / ``get_axis("Mouse Y")``.
    Unity uses 0.1 by default.
    """

    # ------------------------------------------------------------------
    # Focus gating (called by the editor's GameViewPanel)
    # ------------------------------------------------------------------

    @staticmethod
    def set_game_focused(focused: bool) -> None:
        """Enable or disable game-input queries.

        Called by the editor when the Game View gains / loses focus.
        When *focused* is ``False`` every query returns its "idle" value
        (``False`` for booleans, ``0.0`` for floats, ``""`` for strings).
        """
        Input._game_focused = focused

    @staticmethod
    def set_game_viewport_origin(x: float, y: float) -> None:
        """Store the absolute pixel position of the game image top-left.

        Called each frame by GameViewPanel so that
        ``Input.game_mouse_position`` can convert absolute mouse
        coordinates to viewport-relative ones.
        """
        Input._game_viewport_origin = (x, y)

    @staticmethod
    def is_game_focused() -> bool:
        """Return whether the Game View is currently focused."""
        return Input._game_focused

    @staticmethod
    def _accepts_game_input() -> bool:
        return Input._game_focused or Input._automation_game_input_depth > 0

    @staticmethod
    def _begin_automation_game_input() -> None:
        """Route gameplay automation without changing visible Editor focus."""
        Input._automation_game_input_depth += 1

    @staticmethod
    def _end_automation_game_input() -> None:
        Input._automation_game_input_depth = max(0, Input._automation_game_input_depth - 1)

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_key(key: Union[str, int]) -> int:
        """Resolve a key argument to an SDL scancode integer.

        Accepts either an ``int`` scancode (e.g. ``KeyCode.SPACE``) or a
        ``str`` name (e.g. ``"space"``).  Returns ``-1`` for unknown names.
        """
        if isinstance(key, int):
            return key
        return _NativeInputManager.name_to_scancode(key)

    # ------------------------------------------------------------------
    # Keyboard — Unity: Input.GetKey / GetKeyDown / GetKeyUp
    # ------------------------------------------------------------------

    @staticmethod
    def get_key(key: Union[str, int]) -> bool:
        """``True`` while *key* is held down.

        *key* can be a ``KeyCode`` constant or a string name (``"w"``,
        ``"space"``, ``"left shift"`` …).
        """
        if not Input._accepts_game_input():
            return False
        sc = Input._resolve_key(key)
        return sc >= 0 and _NativeInputManager.instance().get_key(sc)

    @staticmethod
    def get_key_down(key: Union[str, int]) -> bool:
        """``True`` only on the frame *key* transitions to pressed (Unity ``GetKeyDown``).

        For "while held" semantics, use :meth:`get_key` (Unity ``GetKey``).
        """
        if not Input._accepts_game_input():
            return False
        sc = Input._resolve_key(key)
        return sc >= 0 and _NativeInputManager.instance().get_key_down(sc)

    @staticmethod
    def get_key_up(key: Union[str, int]) -> bool:
        """``True`` during the frame *key* was released."""
        if not Input._accepts_game_input():
            return False
        sc = Input._resolve_key(key)
        return sc >= 0 and _NativeInputManager.instance().get_key_up(sc)

    # ------------------------------------------------------------------
    # Mouse buttons — Unity: Input.GetMouseButton / Down / Up
    # ------------------------------------------------------------------

    @staticmethod
    def get_mouse_button(button: int) -> bool:
        """``True`` while *button* is held (0=left, 1=right, 2=middle)."""
        if not Input._accepts_game_input():
            return False
        return _NativeInputManager.instance().get_mouse_button(button)

    @staticmethod
    def get_mouse_button_down(button: int) -> bool:
        """``True`` during the frame *button* was pressed."""
        if not Input._accepts_game_input():
            return False
        return _NativeInputManager.instance().get_mouse_button_down(button)

    @staticmethod
    def get_mouse_button_up(button: int) -> bool:
        """``True`` during the frame *button* was released."""
        if not Input._accepts_game_input():
            return False
        return _NativeInputManager.instance().get_mouse_button_up(button)

    @staticmethod
    def get_mouse_frame_state(button: int = 0):
        """Return ``(abs_x, abs_y, scroll_x, scroll_y, held, down, up)`` for one button."""
        if not Input._accepts_game_input():
            return (0.0, 0.0, 0.0, 0.0, False, False, False)
        return _NativeInputManager.instance().get_mouse_frame_state(button)

    @staticmethod
    def get_game_mouse_frame_state(button: int = 0):
        """Return ``(game_x, game_y, scroll_x, scroll_y, held, down, up)`` for one button."""
        abs_x, abs_y, scroll_x, scroll_y, held, down, up = Input.get_mouse_frame_state(button)
        vx, vy = Input._game_viewport_origin
        return (abs_x - vx, abs_y - vy, scroll_x, scroll_y, held, down, up)

    # ------------------------------------------------------------------
    # Mouse position & delta — Unity: Input.mousePosition / mouseDelta
    # ------------------------------------------------------------------
    # Property-style access: Input.mouse_position → (x, y)
    # (implemented in _InputMeta metaclass)

    # Property-style access: Input.mouse_scroll_delta → (x, y)
    # (implemented in _InputMeta metaclass)

    # ------------------------------------------------------------------
    # Axis helpers — Unity: Input.GetAxis / GetAxisRaw
    # ------------------------------------------------------------------

    @staticmethod
    def get_axis(axis_name: str) -> float:
        """Simple virtual axis query (no smoothing — equivalent to ``GetAxisRaw``).

        Built-in axes:
            ``"Horizontal"``  — A/D or Left/Right arrows → -1 / 0 / +1
            ``"Vertical"``    — S/W or Down/Up arrows    → -1 / 0 / +1
            ``"Mouse X"``     — horizontal mouse delta this frame
            ``"Mouse Y"``     — vertical mouse delta this frame
            ``"Mouse ScrollWheel"`` — vertical scroll delta

        Returns ``0.0`` for unknown axis names.
        """
        if not Input._accepts_game_input():
            return 0.0
        mgr = _NativeInputManager.instance()
        name = axis_name.lower()
        if name == "horizontal":
            val = 0.0
            if mgr.get_key(_NativeInputManager.name_to_scancode("d")) or \
               mgr.get_key(_NativeInputManager.name_to_scancode("right")):
                val += 1.0
            if mgr.get_key(_NativeInputManager.name_to_scancode("a")) or \
               mgr.get_key(_NativeInputManager.name_to_scancode("left")):
                val -= 1.0
            return val
        elif name == "vertical":
            val = 0.0
            if mgr.get_key(_NativeInputManager.name_to_scancode("w")) or \
               mgr.get_key(_NativeInputManager.name_to_scancode("up")):
                val += 1.0
            if mgr.get_key(_NativeInputManager.name_to_scancode("s")) or \
               mgr.get_key(_NativeInputManager.name_to_scancode("down")):
                val -= 1.0
            return val
        elif name == "mouse x":
            return mgr.mouse_delta_x * Input.mouse_sensitivity
        elif name == "mouse y":
            return mgr.mouse_delta_y * Input.mouse_sensitivity
        elif name in ("mouse scrollwheel", "mouse scroll wheel"):
            return mgr.mouse_scroll_delta_y
        return 0.0

    @staticmethod
    def get_axis_raw(axis_name: str) -> float:
        """Alias for ``get_axis`` — no smoothing is applied."""
        return Input.get_axis(axis_name)

    # ------------------------------------------------------------------
    # Text input — Unity: Input.inputString
    # ------------------------------------------------------------------
    # Property-style access: Input.input_string → str
    # (implemented in _InputMeta metaclass)

    # ------------------------------------------------------------------
    # Any key — Unity: Input.anyKey / anyKeyDown
    # ------------------------------------------------------------------
    # Property-style access: Input.any_key, Input.any_key_down → bool
    # (implemented in _InputMeta metaclass)

    # ------------------------------------------------------------------
    # Touch (placeholder) — Unity: Input.touchCount
    # ------------------------------------------------------------------
    # Property-style access: Input.touch_count → int
    # (implemented in _InputMeta metaclass)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def set_cursor_locked(locked: bool) -> None:
        """Lock or unlock the cursor.

        When locked the cursor is hidden and captured — mouse deltas
        continue to work but the cursor stays invisible and confined.
        Equivalent to Unity ``Cursor.lockState = CursorLockMode.Locked``.
        """
        _NativeInputManager.instance().set_cursor_locked(locked)

    @staticmethod
    def is_cursor_locked() -> bool:
        """Return ``True`` when the cursor is locked."""
        return _NativeInputManager.instance().is_cursor_locked

    @staticmethod
    def reset_input_axes() -> None:
        """Reset all input state. Unity: ``Input.ResetInputAxes()``."""
        _NativeInputManager.instance().reset_all()
