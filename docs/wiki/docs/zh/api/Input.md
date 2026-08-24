# Input

<div class="class-info">
类位于 <b>Infernux.input</b>
</div>

## 描述

用于读取键盘、鼠标和触摸输入的接口。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.6

连续动作使用按住状态，一帧边沿操作使用 down/up。游戏鼠标逻辑应使用 Game 视口坐标并尊重 Game 焦点。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| mouse_position | `Tuple[float, float]` | 当前鼠标在屏幕坐标中的位置。 |
| game_mouse_position | `Tuple[float, float]` | 当前鼠标在游戏视口坐标中的位置。 |
| mouse_scroll_delta | `Tuple[float, float]` | 当前帧的鼠标滚轮增量。 |
| input_string | `str` | 当前帧中用户输入的字符。 |
| any_key | `bool` | 当任意键或鼠标按钮被按住时返回 True。 |
| any_key_down | `bool` | 当任意键或鼠标按钮首次按下时返回 True。 |
| touch_count | `int` | 当前活动的触摸数量。 |
| mouse_sensitivity | `float` | Mouse sensitivity multiplier (default 0.1). |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static Input.set_game_focused(focused: bool) → None` | 设置游戏视口是否获得输入焦点。 |
| `static Input.set_game_viewport_origin(x: float, y: float) → None` | 设置游戏视口原点的屏幕坐标。 |
| `static Input.is_game_focused() → bool` | 游戏视口是否获得输入焦点。 |
| `static Input.get_key(key: Union[str, int]) → bool` | 当用户按住指定按键时返回 True。 |
| `static Input.get_key_down(key: Union[str, int]) → bool` | 当用户按下指定按键的那一帧返回 True。 |
| `static Input.get_key_up(key: Union[str, int]) → bool` | 当用户松开指定按键的那一帧返回 True。 |
| `static Input.get_mouse_button(button: int) → bool` | 当鼠标按钮被按住时返回 True。 |
| `static Input.get_mouse_button_down(button: int) → bool` | 当鼠标按钮按下的那一帧返回 True。 |
| `static Input.get_mouse_button_up(button: int) → bool` | 当鼠标按钮松开的那一帧返回 True。 |
| `static Input.get_mouse_frame_state(button: int = ...) → Tuple[float, float, float, float, bool, bool, bool]` | Get comprehensive mouse state for the current frame. |
| `static Input.get_game_mouse_frame_state(button: int = ...) → Tuple[float, float, float, float, bool, bool, bool]` | Get comprehensive game-viewport mouse state for the current frame. |
| `static Input.set_cursor_locked(locked: bool) → None` | Lock or unlock the cursor. |
| `static Input.is_cursor_locked() → bool` | Returns True if the cursor is currently locked. |
| `static Input.get_axis(axis_name: str) → float` | 返回经过平滑处理的虚拟轴的值。 |
| `static Input.get_axis_raw(axis_name: str) → float` | 返回未经平滑处理的虚拟轴原始值。 |
| `static Input.reset_input_axes() → None` | 将所有输入轴重置为零。 |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

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

## 另请参阅

<!-- USER CONTENT START --> see_also
- [KeyCode](KeyCode.md)
- [Time](Time.md)
- [Camera](Camera.md)
<!-- USER CONTENT END -->
