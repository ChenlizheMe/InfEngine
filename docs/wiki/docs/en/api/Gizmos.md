# Gizmos

<div class="class-info">
class in <b>Infernux.gizmos</b>
</div>

## Description

Draw visual debugging helpers in the Scene view.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| color | `Tuple[float, float, float]` | The color used for drawing gizmos. |
| matrix | `Optional[List[float]]` | The transformation matrix for gizmo drawing. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `Gizmos.draw_line(start: Vec3, end: Vec3) → None` | Draw a line from start to end in the Scene view. |
| `Gizmos.draw_ray(origin: Vec3, direction: Vec3) → None` | Draw a ray starting at origin in the given direction. |
| `Gizmos.draw_icon(position: Vec3, object_id: int, color: Optional[Tuple[float, float, float]] = ...) → None` | Draw an icon at the given world position. |
| `Gizmos.draw_wire_cube(center: Vec3, size: Vec3) → None` | Draw a wireframe cube in the Scene view. |
| `Gizmos.draw_wire_sphere(center: Vec3, radius: float, segments: int = ...) → None` | Draw a wireframe sphere in the Scene view. |
| `Gizmos.draw_frustum(position: Vec3, fov_deg: float, aspect: float, near: float, far: float, forward: Vec3 = ..., up: Vec3 = ..., right: Vec3 = ...) → None` | Draw a camera frustum wireframe in the Scene view. |
| `Gizmos.draw_wire_arc(center: Vec3, normal: Vec3, radius: float, start_angle_deg: float = ..., arc_deg: float = ..., segments: int = ...) → None` | Draw a wireframe arc in the Scene view. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
