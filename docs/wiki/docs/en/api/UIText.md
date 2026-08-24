# UIText

<div class="class-info">
class in <b>Infernux.ui</b>
</div>

**Inherits from:** `InxUIScreenComponent`

## Description

Figma-style text label rendered with ImGui draw primitives.

Inherits ``x``, ``y``, ``width``, ``height`` from ``InxUIScreenComponent``.

Attributes:
    text: Display string.
    font_path: Optional font asset path (``.ttf`` / ``.otf``).
    font_size: Font size in canvas pixels.
    line_height: Line height multiplier.
    letter_spacing: Extra letter spacing in pixels.
    text_align_h: Horizontal text alignment.
    text_align_v: Vertical text alignment.
    overflow: Text overflow mode.
    resize_mode: How the clipping box resizes with content.
    color: Text color as ``[R, G, B, A]`` (0–1 each).

Example::

    text = game_object.add_component(UIText)
    text.text = "Hello World"
    text.font_size = 32.0
    text.color = [1.0, 0.8, 0.0, 1.0]

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| text | `str` |  |
| font_path | `str` |  |
| font_size | `float` |  |
| line_height | `float` |  |
| letter_spacing | `float` |  |
| text_align_h | `TextAlignH` |  |
| text_align_v | `TextAlignV` |  |
| overflow | `TextOverflow` |  |
| resize_mode | `TextResizeMode` |  |
| color | `list` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
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

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.6. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
