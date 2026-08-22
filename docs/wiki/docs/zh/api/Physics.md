# Physics

<div class="class-info">
类位于 <b>Infernux.physics</b>
</div>

## 描述

物理系统的静态工具类。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.4

空间查询接受 Layer Mask 和明确的 Trigger 处理。应限制查询体积与距离，并选择能回答玩法问题的最简单查询。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| body_count | `int` | Number of native physics bodies currently owned by the world. *(只读)* |
| gravity | `Any` | 全局重力加速度。 |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static Physics.get_gravity() → Any` | Get the global gravity vector. |
| `static Physics.set_gravity(value: Any) → None` | Set the global gravity vector. |
| `static Physics.raycast(origin: Any, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` | 从原点沿方向发射射线检测碰撞。 |
| `static Physics.raycast_all(origin: Any, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` | 射线检测所有碰撞体。 |
| `static Physics.overlap_sphere(center: Any, radius: float, layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` | 检测球形区域内的所有碰撞体。 |
| `static Physics.overlap_box(center: Any, half_extents: Any, orientation: Any = ..., layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` | Find all colliders within an oriented box. |
| `static Physics.overlap_capsule(point0: Any, point1: Any, radius: float, layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` |  |
| `static Physics.sphere_cast(origin: Any, radius: float, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` | Cast a sphere along a direction and return the first hit, or None. |
| `static Physics.box_cast(center: Any, half_extents: Any, direction: Any, orientation: Any = ..., max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` | Cast a box along a direction and return the first hit, or None. |
| `static Physics.capsule_cast(point0: Any, point1: Any, radius: float, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` |  |
| `static Physics.ignore_layer_collision(layer1: int, layer2: int, ignore: bool = ...) → None` | Set whether collisions between two layers are ignored. |
| `static Physics.get_ignore_layer_collision(layer1: int, layer2: int) → bool` | Check if collisions between two layers are ignored. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
from Infernux import InxComponent, Vector3
from Infernux.physics import Physics


class GroundProbe(InxComponent):
    def is_grounded(self) -> bool:
        hit = Physics.raycast(
            self.transform.position,
            Vector3(0.0, -1.0, 0.0),
            max_distance=1.1,
        )
        return hit is not None
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [Rigidbody](Rigidbody.md)
- [Collider](Collider.md)
- [BoxCollider](BoxCollider.md)
<!-- USER CONTENT END -->
