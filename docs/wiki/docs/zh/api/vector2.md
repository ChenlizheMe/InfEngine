# vector2

<div class="class-info">
类位于 <b>Infernux.math</b>
</div>

## 描述

二维向量，包含 x 和 y 分量。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| zero | `Vector2` | Vector2(0, 0)。 |
| one | `Vector2` | Vector2(1, 1)。 |
| up | `Vector2` | Vector2(0, 1)。 |
| down | `Vector2` | Vector2(0, -1)。 |
| left | `Vector2` | Vector2(-1, 0)。 |
| right | `Vector2` | Vector2(1, 0)。 |
| positive_infinity | `Vector2` | Shorthand for writing vector2(inf, inf). |
| negative_infinity | `Vector2` | Shorthand for writing vector2(-inf, -inf). |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static vector2.angle(a: Vector2, b: Vector2) → float` | 计算两个向量之间的角度。 |
| `static vector2.clamp_magnitude(v: Vector2, max_length: float) → Vector2` | Return a copy of the vector with its magnitude clamped. |
| `static vector2.cross(a: Vector2, b: Vector2) → float` | Return the 2D cross product (z-component of 3D cross). |
| `static vector2.distance(a: Vector2, b: Vector2) → float` | 计算两点之间的距离。 |
| `static vector2.dot(a: Vector2, b: Vector2) → float` | 计算两个向量的点积。 |
| `static vector2.lerp(a: Vector2, b: Vector2, t: float) → Vector2` | 在两个向量之间线性插值。 |
| `static vector2.lerp_unclamped(a: Vector2, b: Vector2, t: float) → Vector2` | Linearly interpolate between two vectors without clamping t. |
| `static vector2.max(a: Vector2, b: Vector2) → Vector2` | Return a vector made from the largest components of two vectors. |
| `static vector2.min(a: Vector2, b: Vector2) → Vector2` | Return a vector made from the smallest components of two vectors. |
| `static vector2.move_towards(current: Vector2, target: Vector2, max_delta: float) → Vector2` | Move current towards target by at most max_delta. |
| `static vector2.normalize(v: Vector2) → Vector2` | 将此向量单位化。 |
| `static vector2.perpendicular(v: Vector2) → Vector2` | Return the 2D vector perpendicular to this vector. |
| `static vector2.reflect(direction: Vector2, normal: Vector2) → Vector2` | Reflect a vector off the surface defined by a normal. |
| `static vector2.scale(a: Vector2, b: Vector2) → Vector2` | Multiply two vectors component-wise. |
| `static vector2.signed_angle(a: Vector2, b: Vector2) → float` | Return the signed angle in degrees between two vectors. |
| `static vector2.smooth_damp(current: Vector2, target: Vector2, current_velocity: Vector2, smooth_time: float, max_speed: float, delta_time: float) → Vector2` | Gradually change a vector towards a desired goal over time. |
| `static vector2.magnitude(v: Vector2) → float` | 向量的长度。 |
| `static vector2.sqr_magnitude(v: Vector2) → float` | 向量长度的平方。 |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.6 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
