# vector3

<div class="class-info">
类位于 <b>Infernux.math</b>
</div>

## 描述

三维向量，包含 x、y 和 z 分量。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| zero | `Vector3` | Vector3(0, 0, 0)。 |
| one | `Vector3` | Vector3(1, 1, 1)。 |
| up | `Vector3` | Vector3(0, 1, 0)。 |
| down | `Vector3` | Vector3(0, -1, 0)。 |
| left | `Vector3` | Vector3(-1, 0, 0)。 |
| right | `Vector3` | Vector3(1, 0, 0)。 |
| forward | `Vector3` | Vector3(0, 0, 1)。 |
| back | `Vector3` | Vector3(0, 0, -1)。 |
| positive_infinity | `Vector3` | Shorthand for writing vector3(inf, inf, inf). |
| negative_infinity | `Vector3` | Shorthand for writing vector3(-inf, -inf, -inf). |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static vector3.angle(a: Vector3, b: Vector3) → float` | 计算两个向量之间的角度。 |
| `static vector3.clamp_magnitude(v: Vector3, max_length: float) → Vector3` | Return a copy of the vector with its magnitude clamped. |
| `static vector3.cross(a: Vector3, b: Vector3) → Vector3` | 计算两个向量的叉积。 |
| `static vector3.distance(a: Vector3, b: Vector3) → float` | 计算两点之间的距离。 |
| `static vector3.dot(a: Vector3, b: Vector3) → float` | 计算两个向量的点积。 |
| `static vector3.lerp(a: Vector3, b: Vector3, t: float) → Vector3` | 在两个向量之间线性插值。 |
| `static vector3.lerp_unclamped(a: Vector3, b: Vector3, t: float) → Vector3` | Linearly interpolate between two vectors without clamping t. |
| `static vector3.max(a: Vector3, b: Vector3) → Vector3` | Return a vector made from the largest components of two vectors. |
| `static vector3.min(a: Vector3, b: Vector3) → Vector3` | Return a vector made from the smallest components of two vectors. |
| `static vector3.move_towards(current: Vector3, target: Vector3, max_delta: float) → Vector3` | Move current towards target by at most max_delta. |
| `static vector3.normalize(v: Vector3) → Vector3` | 将此向量单位化。 |
| `static vector3.ortho_normalize(v1: Vector3, v2: Vector3, v3: Vector3) → Vector3` | Make vectors normalized and orthogonal to each other. |
| `static vector3.project(v: Vector3, on_normal: Vector3) → Vector3` | Project a vector onto another vector. |
| `static vector3.project_on_plane(v: Vector3, plane_normal: Vector3) → Vector3` | Project a vector onto a plane defined by its normal. |
| `static vector3.reflect(in_dir: Vector3, normal: Vector3) → Vector3` | Reflect a vector off the plane defined by a normal. |
| `static vector3.rotate_towards(current: Vector3, target: Vector3, max_radians: float, max_mag: float) → Vector3` | Rotate current towards target, limited by max angle and magnitude. |
| `static vector3.scale(a: Vector3, b: Vector3) → Vector3` | Multiply two vectors component-wise. |
| `static vector3.signed_angle(from_v: Vector3, to_v: Vector3, axis: Vector3) → float` | Return the signed angle in degrees between two vectors around an axis. |
| `static vector3.slerp(a: Vector3, b: Vector3, t: float) → Vector3` | Spherically interpolate between two vectors. |
| `static vector3.slerp_unclamped(a: Vector3, b: Vector3, t: float) → Vector3` | Spherically interpolate between two vectors without clamping t. |
| `static vector3.smooth_damp(current: Vector3, target: Vector3, current_velocity: Vector3, smooth_time: float, max_speed: float, delta_time: float) → Vector3` | Gradually change a vector towards a desired goal over time. |
| `static vector3.magnitude(v: Vector3) → float` | 向量的长度。 |
| `static vector3.sqr_magnitude(v: Vector3) → float` | 向量长度的平方。 |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.7 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
