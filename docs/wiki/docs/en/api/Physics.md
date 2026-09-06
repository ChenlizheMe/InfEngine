# Physics

<div class="class-info">
class in <b>Infernux.physics</b>
</div>

## Description

Global physics system for raycasting and spatial queries.

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.4.0

Spatial queries accept layer masks and explicit trigger handling. Keep query volumes and distances bounded, and use the simplest query that answers the gameplay question.
<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| body_count | `int` | Number of native physics bodies currently owned by the world. *(read-only)* |
| gravity | `Any` | The global gravity vector applied to all rigidbodies. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static Physics.get_gravity() → Any` | Get the global gravity vector. |
| `static Physics.set_gravity(value: Any) → None` | Set the global gravity vector. |
| `static Physics.raycast(origin: Any, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` | Cast a ray and return the first hit, or None. |
| `static Physics.raycast_all(origin: Any, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` | Cast a ray and return all hits. |
| `static Physics.overlap_sphere(center: Any, radius: float, layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` | Find all colliders within a sphere. |
| `static Physics.overlap_box(center: Any, half_extents: Any, orientation: Any = ..., layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` | Find all colliders within an oriented box. |
| `static Physics.overlap_capsule(point0: Any, point1: Any, radius: float, layer_mask: int = ..., query_triggers: bool = ...) → List[Any]` |  |
| `static Physics.sphere_cast(origin: Any, radius: float, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` | Cast a sphere along a direction and return the first hit, or None. |
| `static Physics.box_cast(center: Any, half_extents: Any, direction: Any, orientation: Any = ..., max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` | Cast a box along a direction and return the first hit, or None. |
| `static Physics.capsule_cast(point0: Any, point1: Any, radius: float, direction: Any, max_distance: float = ..., layer_mask: int = ..., query_triggers: bool = ...) → Optional[Any]` |  |
| `static Physics.ignore_layer_collision(layer1: int, layer2: int, ignore: bool = ...) → None` | Set whether collisions between two layers are ignored. |
| `static Physics.get_ignore_layer_collision(layer1: int, layer2: int) → bool` | Check if collisions between two layers are ignored. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
import infernux as inx


class GroundProbe(inx.InxComponent):
    def is_grounded(self) -> bool:
        hit = inx.physics.Physics.raycast(
            self.transform.position,
            inx.Vector3(0.0, -1.0, 0.0),
            max_distance=1.1,
        )
        return hit is not None
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [Rigidbody](Rigidbody.md)
- [Collider](Collider.md)
- [BoxCollider](BoxCollider.md)
<!-- USER CONTENT END -->
