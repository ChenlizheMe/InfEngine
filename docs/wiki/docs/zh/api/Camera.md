# Camera

<div class="class-info">
类位于 <b>Infernux.components.builtin</b>
</div>

**继承自:** [BuiltinComponent](Component.md)

## 描述

渲染场景视图的摄像机组件。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.6

普通 3D 深度使用透视 Camera，尺度稳定的 2D 构图使用正交 Camera。Near/Far 裁剪应与场景尺度匹配。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| projection_mode | `int` | 投影模式（0=透视，1=正交）。 |
| field_of_view | `float` | 垂直视野角度（度）。 |
| orthographic_size | `float` | 正交模式下摄像机的半尺寸。 |
| aspect_ratio | `float` | 摄像机宽高比（宽/高）。 *(只读)* |
| near_clip | `float` | 近裁剪面距离。 |
| far_clip | `float` | 远裁剪面距离。 |
| depth | `float` | 摄像机渲染顺序。 |
| culling_mask | `int` | 用于剔除对象的图层遮罩。 *(只读)* |
| clear_flags | `int` | 摄像机渲染前清除背景的方式。 |
| background_color | `List[float]` | 清除标志设为纯色时使用的背景颜色。 |
| stop_nans | `bool` | Whether invalid final-output pixels are replaced before display encoding. |
| dithering | `bool` | Whether display-space dithering is applied before output quantization. |
| pixel_width | `int` | 渲染目标宽度（像素）。 *(只读)* |
| pixel_height | `int` | 渲染目标高度（像素）。 *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `screen_to_world_point(x: float, y: float, depth: float = ...) → Optional[Tuple[float, float, float]]` | 将屏幕空间坐标转换为世界坐标。 |
| `world_to_screen_point(x: float, y: float, z: float) → Optional[Tuple[float, float]]` | 将世界空间坐标转换为屏幕坐标。 |
| `screen_point_to_ray(x: float, y: float) → Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]` | 从屏幕空间坐标向场景发出射线。 |
| `serialize() → str` | Serialize the component to a JSON string. |
| `deserialize(json_str: str) → bool` | Deserialize the component from a JSON string. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 生命周期方法

| 方法 | 描述 |
|------|------|
| `on_draw_gizmos_selected() → None` | 选中时绘制摄像机视锥 Gizmo。 |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
from Infernux import Camera, CameraProjection, GameObject

camera_object = GameObject.find("Main Camera")
if camera_object is not None:
    camera = camera_object.get_component(Camera)
    if camera is not None:
        camera.projection_mode = CameraProjection.Orthographic
        camera.orthographic_size = 5.0
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [CameraProjection](CameraProjection.md)
<!-- USER CONTENT END -->
