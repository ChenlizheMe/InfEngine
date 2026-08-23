# UIText

<div class="class-info">
类位于 <b>Infernux.ui</b>
</div>

**继承自:** `InxUIScreenComponent`

## 描述

UI 文本组件。在屏幕上显示文字。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| text | `str` | 显示的文本内容。 |
| font_path | `str` |  |
| font_size | `float` | 字体大小。 |
| line_height | `float` |  |
| letter_spacing | `float` |  |
| text_align_h | `TextAlignH` |  |
| text_align_v | `TextAlignV` |  |
| overflow | `TextOverflow` |  |
| resize_mode | `TextResizeMode` |  |
| color | `list` | 文本颜色。 |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `is_auto_width() → bool` | Return ``True`` if resize mode is ``AutoWidth``. |
| `is_auto_height() → bool` | Return ``True`` if resize mode is ``AutoHeight``. |
| `is_fixed_size() → bool` | Return ``True`` if resize mode is ``FixedSize``. |
| `get_wrap_width() → float` | Return the wrap width for text layout (0 = no wrap). |
| `get_layout_tolerance() → float` | Return the layout tolerance for auto-sizing decisions. |
| `get_editor_wrap_width() → float` | Return the wrap width used by the editor preview. |
| `get_auto_size_padding() → Tuple[float, float]` | Return ``(horizontal_padding, vertical_padding)`` for auto-sizing. |
| `is_width_editable() → bool` | Return ``True`` if width can be manually edited (not AutoWidth). |
| `is_height_editable() → bool` | Return ``True`` if height can be manually edited (not AutoHeight). |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.5 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
