# UICanvas

<div class="class-info">
类位于 <b>Infernux.ui</b>
</div>

**继承自:** [InxUIComponent](InxUIComponent.md)

## 描述

UI 画布组件。所有 UI 元素的根容器——UI 的舞台。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.4.0

使用参考分辨率像素设计，然后为支持的宽高比选择缩放与屏幕匹配策略。装饰元素不应参与 Raycast。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| render_mode | `RenderMode` | 渲染模式。 |
| sort_order | `int` | 排序顺序。 |
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

## 公共方法

| 方法 | 描述 |
|------|------|
| `compute_scale(screen_w: float, screen_h: float) → Tuple[float, float, float]` | Compute ``(scale_x, scale_y, text_scale)`` for a viewport size. |
| `compute_logical_size(screen_w: float, screen_h: float) → Tuple[float, float]` | Return the live logical Canvas extent for the viewport. |
| `invalidate_element_cache() → None` | Mark the cached element list as stale. |
| `iter_ui_elements() → Iterator[InxUIScreenComponent]` | Yield all screen-space UI components on child GameObjects (depth-first). |
| `raycast(canvas_x: float, canvas_y: float, tolerance: float = ...) → Optional[InxUIScreenComponent]` | Return the front-most element hit at ``(canvas_x, canvas_y)``, or ``None``. |
| `raycast_all(canvas_x: float, canvas_y: float, tolerance: float = ...) → List[InxUIScreenComponent]` | Return all elements hit at the given point, front-to-back order. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
import infernux as inx

ui_root = inx.GameObject.find("UI")
if ui_root is not None:
    canvas = ui_root.get_component(inx.ui.UICanvas)
    if canvas is not None:
        canvas.ui_scale_mode = inx.ui.UIScaleMode.ScaleWithScreenSize
        scale_x, scale_y, text_scale = canvas.compute_scale(1280, 720)
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [UIText](UIText.md)
- [UIImage](UIImage.md)
- [UIButton](UIButton.md)
<!-- USER CONTENT END -->
