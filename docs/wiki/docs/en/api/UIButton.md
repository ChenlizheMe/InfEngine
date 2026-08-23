# UIButton

<div class="class-info">
class in <b>Infernux.ui</b>
</div>

**Inherits from:** [UISelectable](UISelectable.md)

## Description

A clickable button with visual state feedback.

Combines **Image** (background) and **Text** (label) capabilities.
Fires ``on_click`` when the user performs a full click (down + up).

Attributes:
    label: Button label text.
    font_size: Label font size in canvas pixels.
    font_path: Optional font asset path.
    label_color: Label text colour as ``[R, G, B, A]``.
    text_align_h: Horizontal text alignment.
    text_align_v: Vertical text alignment.
    line_height: Line height multiplier.
    letter_spacing: Extra letter spacing in pixels.
    texture_path: Background image texture path.
    background_color: Background fill colour as ``[R, G, B, A]``.
    on_click_entries: Persistent click handlers (serialized).

Example::

    class MyUI(InxComponent):
        def start(self):
            start_btn = GameObject.find("StartBtn")
            if start_btn is None:
                return
            btn = start_btn.get_component(UIButton)
            btn.on_click.add_listener(self.on_start)

        def on_start(self):
            print("Start clicked!")

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| label | `str` |  |
| font_size | `float` |  |
| font_path | `str` |  |
| label_color | `list` |  |
| text_align_h | `TextAlignH` |  |
| text_align_v | `TextAlignV` |  |
| line_height | `float` |  |
| letter_spacing | `float` |  |
| texture_path | `str` |  |
| background_color | `list` |  |
| on_click_entries | `List[UIEventEntry]` |  |
| on_click | `UIEvent` | The click event — call ``add_listener()`` to subscribe. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `on_pointer_click(event_data: PointerEventData) → None` | Internal — fires ``on_click`` and persistent entries on click. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Lifecycle Methods

| Method | Description |
|------|------|
| `awake() → None` |  |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.5. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
