# Input

<div class="class-info">
class in <b>Infernux.input</b>
</div>

## Description

Interface for reading input from keyboard, mouse, and touch.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.5

Use held-state queries for continuous actions and down/up queries for one-frame edges. Gameplay mouse work should use Game viewport coordinates and respect Game focus.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| mouse_position | `Tuple[float, float]` | The current mouse position in screen coordinates. |
| game_mouse_position | `Tuple[float, float]` | The current mouse position in game viewport coordinates. |
| mouse_scroll_delta | `Tuple[float, float]` | The mouse scroll delta for the current frame. |
| input_string | `str` | Characters typed by the user in the current frame. |
| any_key | `bool` | Returns True while any key or mouse button is held down. |
| any_key_down | `bool` | Returns True during the frame any key or mouse button is first pressed. |
| touch_count | `int` | Number of active touch contacts. |
| mouse_sensitivity | `float` | Mouse sensitivity multiplier (default 0.1). |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static Input.set_game_focused(focused: bool) → None` | Set whether the game viewport has input focus. |
| `static Input.set_game_viewport_origin(x: float, y: float) → None` | Set the game viewport origin in screen coordinates. |
| `static Input.is_game_focused() → bool` | Returns True if the game viewport has input focus. |
| `static Input.get_key(key: Union[str, int]) → bool` | Returns True while the user holds down the specified key. |
| `static Input.get_key_down(key: Union[str, int]) → bool` | Returns True during the frame the user starts pressing the key. |
| `static Input.get_key_up(key: Union[str, int]) → bool` | Returns True during the frame the user releases the key. |
| `static Input.get_mouse_button(button: int) → bool` | Returns True while the given mouse button is held down. |
| `static Input.get_mouse_button_down(button: int) → bool` | Returns True during the frame the mouse button was pressed. |
| `static Input.get_mouse_button_up(button: int) → bool` | Returns True during the frame the mouse button was released. |
| `static Input.get_mouse_frame_state(button: int = ...) → Tuple[float, float, float, float, bool, bool, bool]` | Get comprehensive mouse state for the current frame. |
| `static Input.get_game_mouse_frame_state(button: int = ...) → Tuple[float, float, float, float, bool, bool, bool]` | Get comprehensive game-viewport mouse state for the current frame. |
| `static Input.set_cursor_locked(locked: bool) → None` | Lock or unlock the cursor. |
| `static Input.is_cursor_locked() → bool` | Returns True if the cursor is currently locked. |
| `static Input.get_axis(axis_name: str) → float` | Returns the value of the virtual axis identified by axis_name. |
| `static Input.get_axis_raw(axis_name: str) → float` | Returns the raw value of the virtual axis with no smoothing. |
| `static Input.reset_input_axes() → None` | Reset all input axes to zero. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import InxComponent, Vector3
from Infernux.input import Input, KeyCode


class KeyboardMover(InxComponent):
    speed: float = 4.0

    def update(self, delta_time: float) -> None:
        axis = float(Input.get_key(KeyCode.D)) - float(Input.get_key(KeyCode.A))
        self.transform.translate(Vector3(axis * self.speed * delta_time, 0.0, 0.0))
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [KeyCode](KeyCode.md)
- [Time](Time.md)
- [Camera](Camera.md)
<!-- USER CONTENT END -->
