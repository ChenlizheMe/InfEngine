# Gizmos

<div class="class-info">
类位于 <b>Infernux.gizmos</b>
</div>

## 描述

在场景视图中绘制调试可视化图形的工具类。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| color | `Tuple[float, float, float]` | 下一次绘制操作使用的颜色。 |
| matrix | `Optional[List[float]]` | The transformation matrix for gizmo drawing. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `Gizmos.draw_line(start: Vec3, end: Vec3) → None` | 绘制一条线段。 |
| `Gizmos.draw_ray(origin: Vec3, direction: Vec3) → None` | 绘制一条射线。 |
| `Gizmos.draw_icon(position: Vec3, object_id: int, color: Optional[Tuple[float, float, float]] = ...) → None` | 在指定位置绘制图标。 |
| `Gizmos.draw_wire_cube(center: Vec3, size: Vec3) → None` | 绘制线框立方体。 |
| `Gizmos.draw_wire_sphere(center: Vec3, radius: float, segments: int = ...) → None` | 绘制线框球体。 |
| `Gizmos.draw_frustum(position: Vec3, fov_deg: float, aspect: float, near: float, far: float, forward: Vec3 = ..., up: Vec3 = ..., right: Vec3 = ...) → None` | 绘制视锥体。 |
| `Gizmos.draw_wire_arc(center: Vec3, normal: Vec3, radius: float, start_angle_deg: float = ..., arc_deg: float = ..., segments: int = ...) → None` | Draw a wireframe arc in the Scene view. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.6 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
