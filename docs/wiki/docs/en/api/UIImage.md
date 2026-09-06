# UIImage

<div class="class-info">
class in <b>Infernux.ui</b>
</div>

**Inherits from:** `InxUIScreenComponent`

## Description

Screen-space image element rendered from a texture asset.

Inherits ``x``, ``y``, ``width``, ``height``, ``opacity``,
``corner_radius``, ``rotation``, ``mirror_x``, ``mirror_y``
from ``InxUIScreenComponent``.

Attributes:
    texture_path: Path to texture asset (drag from Project panel).
    color: Tint color as ``[R, G, B, A]`` (0–1 each).

Example::

    img = game_object.add_component(UIImage)
    img.texture_path = "Assets/Textures/logo.png"
    img.color = [1.0, 1.0, 1.0, 0.8]

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| texture_path | `str` |  |
| color | `list` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.4.0. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
