# Rigidbody

<div class="class-info">
类位于 <b>Infernux.components.builtin</b>
</div>

**继承自:** [BuiltinComponent](Component.md)

## 描述

刚体组件。让物体受物理引擎控制——牛顿看了都点头。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.4.0

动态刚体应使用力或速度，不要每帧写 Transform。运动学刚体使用移动操作，仿真命令从固定更新发出。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| mass | `float` | 刚体质量（千克）。 |
| drag | `float` | 线性阻力。 |
| angular_drag | `float` | 角阻力。 |
| use_gravity | `bool` | 是否受重力影响。 |
| is_kinematic | `bool` | 是否为运动学模式（不受力影响，但能推动别人）。 |
| constraints | `int` | 冻结哪些轴的位置或旋转。 |
| collision_detection_mode | `CollisionDetectionMode` | 碰撞检测模式。 |
| interpolation | `RigidbodyInterpolation` | 插值模式。 |
| max_angular_velocity | `float` | The maximum angular velocity in radians per second. |
| max_linear_velocity | `float` | The maximum linear velocity of the rigidbody. |
| freeze_position_x | `bool` | Whether X-axis position is frozen. |
| freeze_position_y | `bool` | Whether Y-axis position is frozen. |
| freeze_position_z | `bool` | Whether Z-axis position is frozen. |
| freeze_rotation_x | `bool` | Whether X-axis rotation is frozen. |
| freeze_rotation_y | `bool` | Whether Y-axis rotation is frozen. |
| freeze_rotation_z | `bool` | Whether Z-axis rotation is frozen. |
| freeze_rotation | `bool` | Shortcut to freeze or unfreeze all rotation axes. |
| constraints_flags | `RigidbodyConstraints` | The constraint flags as a RigidbodyConstraints enum. |
| velocity | `Any` | 线速度。 |
| angular_velocity | `Any` | 角速度。 |
| world_center_of_mass | `Any` | The center of mass in world space. *(只读)* |
| position | `Any` | 刚体位置。 |
| rotation | `Tuple[float, float, float, float]` | 刚体旋转。 |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `has_constraint(constraint: RigidbodyConstraints) → bool` | Return whether the specified constraint flag is set. |
| `add_constraint(constraint: RigidbodyConstraints) → None` | Add a constraint flag to the rigidbody. |
| `remove_constraint(constraint: RigidbodyConstraints) → None` | Remove a constraint flag from the rigidbody. |
| `add_force(force: Any, mode: Any = ...) → None` | 施加力。 |
| `add_torque(torque: Any, mode: Any = ...) → None` | 施加扭矩。 |
| `add_force_at_position(force: Any, position: Any, mode: Any = ...) → None` | 在指定位置施加力。 |
| `move_position(position: Any) → None` | 移动刚体到目标位置。 |
| `move_rotation(rotation: Any) → None` | 旋转刚体到目标朝向。 |
| `is_sleeping() → bool` | 刚体是否正在休眠。 |
| `wake_up() → None` | 唤醒刚体。 |
| `sleep() → None` | 强制刚体进入休眠。 |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
import infernux as inx


class Thruster(inx.InxComponent):
    def start(self) -> None:
        self.body = self.game_object.get_component(inx.Rigidbody)

    def fixed_update(self, fixed_delta_time: float) -> None:
        if self.body is not None:
            self.body.add_force(inx.Vector3(0.0, 12.0, 0.0))
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [Physics](Physics.md)
- [Collider](Collider.md)
- [InxComponent](InxComponent.md)
<!-- USER CONTENT END -->
