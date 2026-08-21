# quaternion

<div class="class-info">
类位于 <b>Infernux.math</b>
</div>

## 描述

四元数，表示三维旋转。比欧拉角靠谱，不会万向锁。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| identity | `quatf` | 单位四元数（无旋转）。 |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static quaternion.euler(x: float, y: float, z: float) → quatf` | 从欧拉角创建四元数。 |
| `static quaternion.angle_axis(angle: float, axis: Vector3) → quatf` | 从轴角创建四元数。 |
| `static quaternion.look_rotation(forward: Vector3, up: Vector3 = ...) → quatf` | 创建朝向目标方向的旋转。 |
| `static quaternion.dot(a: quatf, b: quatf) → float` | Return the dot product of two quaternions. |
| `static quaternion.angle(a: quatf, b: quatf) → float` | Return the angle in degrees between two rotations. |
| `static quaternion.slerp(a: quatf, b: quatf, t: float) → quatf` | 球面插值。 |
| `static quaternion.lerp(a: quatf, b: quatf, t: float) → quatf` | Linearly interpolate between two quaternions (normalized). |
| `static quaternion.inverse(q: quatf) → quatf` | 求逆。 |
| `static quaternion.rotate_towards(from_: quatf, to: quatf, max_degrees_delta: float) → quatf` | Rotate from towards to by at most max_degrees_delta degrees. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.4 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
