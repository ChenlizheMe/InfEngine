# vector4

<div class="class-info">
class in <b>Infernux.math</b>
</div>

## Description

A representation of 4D vectors.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| zero | `vec4f` | Shorthand for writing vector4(0, 0, 0, 0). |
| one | `vec4f` | Shorthand for writing vector4(1, 1, 1, 1). |
| positive_infinity | `vec4f` | A vector with all components set to positive infinity. |
| negative_infinity | `vec4f` | A vector with all components set to negative infinity. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static vector4.distance(a: vec4f, b: vec4f) → float` | Return the distance between two points. |
| `static vector4.dot(a: vec4f, b: vec4f) → float` | Return the dot product of two vectors. |
| `static vector4.lerp(a: vec4f, b: vec4f, t: float) → vec4f` | Linearly interpolate between two vectors. |
| `static vector4.lerp_unclamped(a: vec4f, b: vec4f, t: float) → vec4f` | Linearly interpolate between two vectors without clamping t. |
| `static vector4.max(a: vec4f, b: vec4f) → vec4f` | Return a vector made from the largest components of two vectors. |
| `static vector4.min(a: vec4f, b: vec4f) → vec4f` | Return a vector made from the smallest components of two vectors. |
| `static vector4.move_towards(current: vec4f, target: vec4f, max_delta: float) → vec4f` | Move current towards target by at most max_delta. |
| `static vector4.normalize(v: vec4f) → vec4f` | Return the vector with a magnitude of 1. |
| `static vector4.project(a: vec4f, b: vec4f) → vec4f` | Project vector a onto vector b. |
| `static vector4.scale(a: vec4f, b: vec4f) → vec4f` | Multiply two vectors component-wise. |
| `static vector4.smooth_damp(current: vec4f, target: vec4f, current_velocity: vec4f, smooth_time: float, max_speed: float, delta_time: float) → vec4f` | Gradually change a vector towards a desired goal over time. |
| `static vector4.magnitude(v: vec4f) → float` | Return the length of the vector. |
| `static vector4.sqr_magnitude(v: vec4f) → float` | Return the squared length of the vector. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
