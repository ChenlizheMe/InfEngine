# vector4

<div class="class-info">
类位于 <b>Infernux.math</b>
</div>

## 描述

四维向量，包含 x、y、z 和 w 分量。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| zero | `vec4f` | Vector4(0, 0, 0, 0)。 |
| one | `vec4f` | Vector4(1, 1, 1, 1)。 |
| positive_infinity | `vec4f` | A vector with all components set to positive infinity. |
| negative_infinity | `vec4f` | A vector with all components set to negative infinity. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static vector4.distance(a: vec4f, b: vec4f) → float` | 计算两点之间的距离。 |
| `static vector4.dot(a: vec4f, b: vec4f) → float` | 计算两个向量的点积。 |
| `static vector4.lerp(a: vec4f, b: vec4f, t: float) → vec4f` | 在两个向量之间线性插值。 |
| `static vector4.lerp_unclamped(a: vec4f, b: vec4f, t: float) → vec4f` | Linearly interpolate between two vectors without clamping t. |
| `static vector4.max(a: vec4f, b: vec4f) → vec4f` | Return a vector made from the largest components of two vectors. |
| `static vector4.min(a: vec4f, b: vec4f) → vec4f` | Return a vector made from the smallest components of two vectors. |
| `static vector4.move_towards(current: vec4f, target: vec4f, max_delta: float) → vec4f` | Move current towards target by at most max_delta. |
| `static vector4.normalize(v: vec4f) → vec4f` | 将此向量单位化。 |
| `static vector4.project(a: vec4f, b: vec4f) → vec4f` | Project vector a onto vector b. |
| `static vector4.scale(a: vec4f, b: vec4f) → vec4f` | Multiply two vectors component-wise. |
| `static vector4.smooth_damp(current: vec4f, target: vec4f, current_velocity: vec4f, smooth_time: float, max_speed: float, delta_time: float) → vec4f` | Gradually change a vector towards a desired goal over time. |
| `static vector4.magnitude(v: vec4f) → float` | 向量的长度。 |
| `static vector4.sqr_magnitude(v: vec4f) → float` | 向量长度的平方。 |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.6 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
