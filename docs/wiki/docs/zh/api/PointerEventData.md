# PointerEventData

<div class="class-info">
类位于 <b>Infernux.ui</b>
</div>

## 描述

指针事件数据。包含点击位置和来源信息。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `PointerEventData.__init__() → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| position | `Tuple[float, float]` | 当前指针屏幕坐标。 |
| delta | `Tuple[float, float]` |  |
| button | `PointerButton` | 触发事件的鼠标按钮。 |
| press_position | `Tuple[float, float]` |  |
| click_count | `int` | 点击次数。 |
| scroll_delta | `Tuple[float, float]` |  |
| canvas | `Optional[UICanvas]` |  |
| target | `Optional[InxUIScreenComponent]` |  |
| used | `bool` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `Use() → None` | Mark event as consumed (stops propagation to parent elements). |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
