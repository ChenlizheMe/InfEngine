# UICanvas

<div class="class-info">
class in <b>Infernux.ui</b>
</div>

**Inherits from:** [InxUIComponent](InxUIComponent.md)

## Description

Screen-space UI canvas — root container for all UI elements.

Defines a *design* reference resolution (default 1920x1080).  At runtime
the Game View scales from design resolution to actual viewport size so
that all positions, sizes and font sizes adapt proportionally.

Attributes:
    render_mode: ``ScreenOverlay`` or ``CameraOverlay``.
    sort_order: Rendering order (lower draws first).
    target_camera_id: Camera GameObject ID (CameraOverlay mode only).
    reference_width: Design reference width in pixels (default 1920).
    reference_height: Design reference height in pixels (default 1080).
    ui_scale_mode: Canvas scaler mode.
    screen_match_mode: Width/height match behavior for ScaleWithScreenSize.
    match_width_or_height: 0 = match width, 1 = match height.
    pixel_perfect: Round scale to integer pixels when possible.
    reference_pixels_per_unit: Sprite pixel density hint.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.7

Design in reference-resolution pixels, then select a scale and screen-match policy for the supported aspect ratios. Decorative elements should not be raycast targets.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| render_mode | `RenderMode` |  |
| sort_order | `int` |  |
| target_camera_id | `int` |  |
| reference_width | `int` |  |
| reference_height | `int` |  |
| ui_scale_mode | `UIScaleMode` |  |
| screen_match_mode | `ScreenMatchMode` |  |
| match_width_or_height | `float` |  |
| pixel_perfect | `bool` |  |
| reference_pixels_per_unit | `float` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `compute_scale(screen_w: float, screen_h: float) → Tuple[float, float, float]` | Compute ``(scale_x, scale_y, text_scale)`` for a viewport size. |
| `invalidate_element_cache() → None` | Mark the cached element list as stale. |
| `iter_ui_elements() → Iterator[InxUIScreenComponent]` | Yield all screen-space UI components on child GameObjects (depth-first). |
| `raycast(canvas_x: float, canvas_y: float, tolerance: float = ...) → Optional[InxUIScreenComponent]` | Return the front-most element hit at ``(canvas_x, canvas_y)``, or ``None``. |
| `raycast_all(canvas_x: float, canvas_y: float, tolerance: float = ...) → List[InxUIScreenComponent]` | Return all elements hit at the given point, front-to-back order. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import GameObject
from Infernux.ui import UICanvas, UIScaleMode

ui_root = GameObject.find("UI")
if ui_root is not None:
    canvas = ui_root.get_component(UICanvas)
    if canvas is not None:
        canvas.ui_scale_mode = UIScaleMode.ScaleWithScreenSize
        scale_x, scale_y, text_scale = canvas.compute_scale(1280, 720)
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [UIText](UIText.md)
- [UIImage](UIImage.md)
- [UIButton](UIButton.md)
<!-- USER CONTENT END -->
