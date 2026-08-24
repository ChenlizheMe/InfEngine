# PointerEventData

<div class="class-info">
class in <b>Infernux.ui</b>
</div>

## Description

Data container for a single pointer event.

Passed to ``on_pointer_enter``, ``on_pointer_click``, etc. on any
``InxUIScreenComponent`` subclass.

Attributes:
    position: Current pointer position in canvas design pixels.
    delta: Frame-to-frame delta in canvas design pixels.
    button: Which mouse button triggered this event.
    press_position: Canvas-space position where the button was pressed.
    click_count: Rapid click count (1 = single, 2 = double, ...).
    scroll_delta: ``(sx, sy)`` scroll delta this frame.
    canvas: The ``UICanvas`` owning the target element.
    target: The ``InxUIScreenComponent`` this event is addressed to.
    used: Set to ``True`` in a handler to stop further propagation.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `PointerEventData.__init__() → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| position | `Tuple[float, float]` |  |
| delta | `Tuple[float, float]` |  |
| button | `PointerButton` |  |
| press_position | `Tuple[float, float]` |  |
| click_count | `int` |  |
| scroll_delta | `Tuple[float, float]` |  |
| canvas | `Optional[UICanvas]` |  |
| target | `Optional[InxUIScreenComponent]` |  |
| used | `bool` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `Use() → None` | Mark event as consumed (stops propagation to parent elements). |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.6. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
