# Mathf

<div class="class-info">
class in <b>Infernux.mathf</b>
</div>

## Description

A collection of common math functions and constants.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| PI | `float` | The value of pi (3.14159...). |
| TAU | `float` | The value of tau (2 * pi). |
| Infinity | `float` | Positive infinity. |
| NegativeInfinity | `float` | Negative infinity. |
| Epsilon | `float` | The smallest float value greater than zero. |
| Deg2Rad | `float` | Degrees-to-radians conversion constant. |
| Rad2Deg | `float` | Radians-to-degrees conversion constant. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static Mathf.clamp(value: float, min_val: float, max_val: float) → float` | Clamp a value between a minimum and a maximum. |
| `static Mathf.clamp01(value: float) → float` | Clamp a value between 0 and 1. |
| `static Mathf.lerp(a: float, b: float, t: float) → float` | Linearly interpolate between two values. |
| `static Mathf.lerp_unclamped(a: float, b: float, t: float) → float` | Linearly interpolate between two values without clamping t. |
| `static Mathf.inverse_lerp(a: float, b: float, value: float) → float` | Calculate the t parameter that produces value between a and b. |
| `static Mathf.move_towards(current: float, target: float, max_delta: float) → float` | Move current towards target by at most max_delta. |
| `static Mathf.smooth_step(from_val: float, to_val: float, t: float) → float` | Hermite interpolation between two values. |
| `static Mathf.smooth_damp(current: float, target: float, current_velocity: float, smooth_time: float, max_speed: float = ..., delta_time: float = ...) → Tuple[float, float]` | Smoothly damp a value towards a target. |
| `static Mathf.delta_angle(current: float, target: float) → float` | Calculate the shortest difference between two angles in degrees. |
| `static Mathf.lerp_angle(a: float, b: float, t: float) → float` | Linearly interpolate between two angles in degrees. |
| `static Mathf.move_towards_angle(current: float, target: float, max_delta: float) → float` | Move current angle towards target angle by at most max_delta degrees. |
| `static Mathf.repeat(t: float, length: float) → float` | Loop a value t so that it is within 0 and length. |
| `static Mathf.ping_pong(t: float, length: float) → float` | Ping-pong a value t so that it bounces between 0 and length. |
| `static Mathf.approximately(a: float, b: float) → bool` | Returns True if two floats are approximately equal. |
| `static Mathf.sign(f: float) → float` | Return the sign of f: -1, 0, or 1. |
| `static Mathf.sin(f: float) → float` | Return the sine of angle f in radians. |
| `static Mathf.cos(f: float) → float` | Return the cosine of angle f in radians. |
| `static Mathf.tan(f: float) → float` | Return the tangent of angle f in radians. |
| `static Mathf.asin(f: float) → float` | Return the arc-sine of f in radians. |
| `static Mathf.acos(f: float) → float` | Return the arc-cosine of f in radians. |
| `static Mathf.atan(f: float) → float` | Return the arc-tangent of f in radians. |
| `static Mathf.atan2(y: float, x: float) → float` | Return the angle in radians whose tangent is y/x. |
| `static Mathf.sqrt(f: float) → float` | Return the square root of f. |
| `static Mathf.pow(f: float, p: float) → float` | Return f raised to the power p. |
| `static Mathf.exp(power: float) → float` | Return e raised to the given power. |
| `static Mathf.log(f: float, base: float = ...) → float` | Return the logarithm of f in the given base (default: natural log). |
| `static Mathf.log10(f: float) → float` | Return the base-10 logarithm of f. |
| `static Mathf.abs(f: float) → float` | Return the absolute value of f. |
| `static Mathf.min() → float` | Return the smallest of the given values. |
| `static Mathf.max() → float` | Return the largest of the given values. |
| `static Mathf.floor(f: float) → float` | Return the largest integer less than or equal to f. |
| `static Mathf.ceil(f: float) → float` | Return the smallest integer greater than or equal to f. |
| `static Mathf.round(f: float) → float` | Round f to the nearest integer. |
| `static Mathf.floor_to_int(f: float) → int` |  |
| `static Mathf.ceil_to_int(f: float) → int` |  |
| `static Mathf.round_to_int(f: float) → int` |  |
| `static Mathf.is_power_of_two(value: int) → bool` |  |
| `static Mathf.next_power_of_two(value: int) → int` |  |
| `static Mathf.closest_power_of_two(value: int) → int` |  |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.6. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
