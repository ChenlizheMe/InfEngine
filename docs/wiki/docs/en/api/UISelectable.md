# UISelectable

<div class="class-info">
class in <b>Infernux.ui</b>
</div>

**Inherits from:** `InxUIScreenComponent`

## Description

Base class for interactive UI elements with visual state feedback.

Provides Normal / Highlighted / Pressed / Disabled visual states
with ColorTint transitions.  ``UIButton`` inherits this and adds
an ``on_click`` event.

Attributes:
    interactable: Whether the user can interact with this element.
    transition: How visual states are displayed.
    normal_color: RGBA tint when idle.
    highlighted_color: RGBA tint when hovered.
    pressed_color: RGBA tint when pressed.
    disabled_color: RGBA tint when disabled.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| interactable | `bool` |  |
| transition | `UITransitionType` |  |
| normal_color | `list` |  |
| highlighted_color | `list` |  |
| pressed_color | `list` |  |
| disabled_color | `list` |  |
| current_selection_state | `int` | The current visual state index (see ``SelectionState``). *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `get_current_tint() → List[float]` | Return the ``[R, G, B, A]`` tint for the current visual state. |
| `on_pointer_enter(event_data: PointerEventData) → None` |  |
| `on_pointer_exit(event_data: PointerEventData) → None` |  |
| `on_pointer_down(event_data: PointerEventData) → None` |  |
| `on_pointer_up(event_data: PointerEventData) → None` |  |

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
> **Example status:** No curated example has been verified for this symbol in 0.3.6. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
