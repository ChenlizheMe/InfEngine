# UISelectable

<div class="class-info">
类位于 <b>Infernux.ui</b>
</div>

**继承自:** `InxUIScreenComponent`

## 描述

可选择的 UI 元素基类。UIButton 的老爸。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| interactable | `bool` | 是否可交互。 |
| transition | `UITransitionType` | 过渡类型。 |
| normal_color | `list` | 常态颜色。 |
| highlighted_color | `list` | 高亮颜色。 |
| pressed_color | `list` | 按下颜色。 |
| disabled_color | `list` | 禁用颜色。 |
| current_selection_state | `int` | The current visual state index (see ``SelectionState``). *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `get_current_tint() → List[float]` | Return the ``[R, G, B, A]`` tint for the current visual state. |
| `on_pointer_enter(event_data: PointerEventData) → None` |  |
| `on_pointer_exit(event_data: PointerEventData) → None` |  |
| `on_pointer_down(event_data: PointerEventData) → None` |  |
| `on_pointer_up(event_data: PointerEventData) → None` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 生命周期方法

| 方法 | 描述 |
|------|------|
| `awake() → None` |  |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
