# Mathf

<div class="class-info">
类位于 <b>Infernux.mathf</b>
</div>

## 描述

数学工具类。常用数学函数大全。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| PI | `float` | π（3.14159...）——数学界的摇滚巨星。 |
| TAU | `float` | The value of tau (2 * pi). |
| Infinity | `float` | 正无穷。 |
| NegativeInfinity | `float` | Negative infinity. |
| Epsilon | `float` | 极小正数。 |
| Deg2Rad | `float` | 度转弧度系数。 |
| Rad2Deg | `float` | 弧度转度系数。 |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static Mathf.clamp(value: float, min_val: float, max_val: float) → float` | 将值限制在 min 和 max 之间。 |
| `static Mathf.clamp01(value: float) → float` | 将值限制在 0 和 1 之间。 |
| `static Mathf.lerp(a: float, b: float, t: float) → float` | 线性插值。 |
| `static Mathf.lerp_unclamped(a: float, b: float, t: float) → float` | Linearly interpolate between two values without clamping t. |
| `static Mathf.inverse_lerp(a: float, b: float, value: float) → float` | 反向线性插值。 |
| `static Mathf.move_towards(current: float, target: float, max_delta: float) → float` | 向目标移动指定步长。 |
| `static Mathf.smooth_step(from_val: float, to_val: float, t: float) → float` | 平滑插值（Hermite 曲线）。 |
| `static Mathf.smooth_damp(current: float, target: float, current_velocity: float, smooth_time: float, max_speed: float = ..., delta_time: float = ...) → Tuple[float, float]` | Smoothly damp a value towards a target. |
| `static Mathf.delta_angle(current: float, target: float) → float` | Calculate the shortest difference between two angles in degrees. |
| `static Mathf.lerp_angle(a: float, b: float, t: float) → float` | Linearly interpolate between two angles in degrees. |
| `static Mathf.move_towards_angle(current: float, target: float, max_delta: float) → float` | Move current angle towards target angle by at most max_delta degrees. |
| `static Mathf.repeat(t: float, length: float) → float` | Loop a value t so that it is within 0 and length. |
| `static Mathf.ping_pong(t: float, length: float) → float` | Ping-pong a value t so that it bounces between 0 and length. |
| `static Mathf.approximately(a: float, b: float) → bool` | Returns True if two floats are approximately equal. |
| `static Mathf.sign(f: float) → float` | 返回值的符号（-1 / 0 / 1）。 |
| `static Mathf.sin(f: float) → float` | 正弦。 |
| `static Mathf.cos(f: float) → float` | 余弦。 |
| `static Mathf.tan(f: float) → float` | 正切。 |
| `static Mathf.asin(f: float) → float` | 反正弦。 |
| `static Mathf.acos(f: float) → float` | 反余弦。 |
| `static Mathf.atan(f: float) → float` | 反正切。 |
| `static Mathf.atan2(y: float, x: float) → float` | 双参数反正切。 |
| `static Mathf.sqrt(f: float) → float` | 平方根。 |
| `static Mathf.pow(f: float, p: float) → float` | 乘方。 |
| `static Mathf.exp(power: float) → float` | Return e raised to the given power. |
| `static Mathf.log(f: float, base: float = ...) → float` | Return the logarithm of f in the given base (default: natural log). |
| `static Mathf.log10(f: float) → float` | Return the base-10 logarithm of f. |
| `static Mathf.abs(f: float) → float` | 绝对值。 |
| `static Mathf.min() → float` | 返回较小值。 |
| `static Mathf.max() → float` | 返回较大值。 |
| `static Mathf.floor(f: float) → float` | 向下取整。 |
| `static Mathf.ceil(f: float) → float` | 向上取整。 |
| `static Mathf.round(f: float) → float` | 四舍五入。 |
| `static Mathf.floor_to_int(f: float) → int` |  |
| `static Mathf.ceil_to_int(f: float) → int` |  |
| `static Mathf.round_to_int(f: float) → int` |  |
| `static Mathf.is_power_of_two(value: int) → bool` |  |
| `static Mathf.next_power_of_two(value: int) → int` |  |
| `static Mathf.closest_power_of_two(value: int) → int` |  |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
