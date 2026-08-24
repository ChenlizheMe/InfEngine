# quaternion

<div class="class-info">
class in <b>Infernux.math</b>
</div>

## Description

A representation of rotations using a quaternion.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| identity | `quatf` | The identity rotation (no rotation). |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static quaternion.euler(x: float, y: float, z: float) → quatf` | Create a rotation from Euler angles in degrees. |
| `static quaternion.angle_axis(angle: float, axis: Vector3) → quatf` | Create a rotation of angle degrees around axis. |
| `static quaternion.look_rotation(forward: Vector3, up: Vector3 = ...) → quatf` | Create a rotation looking in the forward direction. |
| `static quaternion.dot(a: quatf, b: quatf) → float` | Return the dot product of two quaternions. |
| `static quaternion.angle(a: quatf, b: quatf) → float` | Return the angle in degrees between two rotations. |
| `static quaternion.slerp(a: quatf, b: quatf, t: float) → quatf` | Spherically interpolate between two rotations. |
| `static quaternion.lerp(a: quatf, b: quatf, t: float) → quatf` | Linearly interpolate between two quaternions (normalized). |
| `static quaternion.inverse(q: quatf) → quatf` | Return the inverse of a rotation. |
| `static quaternion.rotate_towards(from_: quatf, to: quatf, max_degrees_delta: float) → quatf` | Rotate from towards to by at most max_degrees_delta degrees. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.6. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
