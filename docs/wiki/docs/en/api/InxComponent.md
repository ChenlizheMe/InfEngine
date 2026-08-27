# InxComponent

<div class="class-info">
class in <b>Infernux.components</b>
</div>

## Description

Base class for Python-scripted game components (Unity-style lifecycle).

Subclass this to create game logic scripts.  Use ``serialized_field()``
class variables for Inspector-editable properties.

Example::

    class PlayerController(InxComponent):
        speed: float = serialized_field(default=5.0, range=(0, 100))

        def start(self):
            Debug.log("PlayerController started")

        def update(self, delta_time: float):
            pos = self.transform.position
            self.transform.position = Vector3(
                pos.x + self.speed * delta_time, pos.y, pos.z
            )

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.3.7

Use this base for project gameplay components. Read Your First Component before relying on the lifecycle table alone.
<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `InxComponent.__init__() → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| game_object | `GameObject` | The GameObject this component is attached to during normal lifecycle use. *(read-only)* |
| transform | `Transform` | Shortcut to ``self.game_object.transform`` during normal lifecycle use. *(read-only)* |
| is_valid | `bool` | Whether the underlying GameObject reference is still alive. *(read-only)* |
| enabled | `bool` | Whether the component is enabled. |
| type_name | `str` | Class name of this component. *(read-only)* |
| execution_order | `int` | Execution order (lower value runs earlier). |
| component_id | `int` | Unique auto-incremented ID for this component instance. *(read-only)* |
| tag | `str` | Tag of the attached GameObject. |
| game_object_layer | `int` | Layer index (0–31) of the attached GameObject. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `destroy() → None` | Remove this component from its owning GameObject (Unity-style). |
| `on_collision_enter(collision: Any) → None` | Called when this collider starts touching another collider. |
| `on_collision_stay(collision: Any) → None` | Called every fixed-update while two colliders remain in contact. |
| `on_collision_exit(collision: Any) → None` | Called when two colliders stop touching. |
| `on_trigger_enter(other: Any) → None` | Called when another collider enters this trigger volume. |
| `on_trigger_stay(other: Any) → None` | Called every fixed-update while another collider is inside this trigger. |
| `on_trigger_exit(other: Any) → None` | Called when another collider exits this trigger volume. |
| `start_coroutine(generator: Any) → Coroutine` | Start a coroutine on this component. |
| `stop_coroutine(coroutine: Coroutine) → None` | Stop a specific coroutine previously started with ``start_coroutine()``. |
| `stop_all_coroutines() → None` | Stop all coroutines running on this component. |
| `compare_tag(tag: str) → bool` | Returns True if the attached GameObject's tag matches. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `InxComponent.reserve_instances(count: int) → None` | Preallocate numeric-field storage for bulk component creation. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Lifecycle Methods

| Method | Description |
|------|------|
| `awake() → None` | Called once when the component is first created. |
| `start() → None` | Called before the first Update after the component is enabled. |
| `update(delta_time: float) → None` | Called every frame. |
| `fixed_update(fixed_delta_time: float) → None` | Called at a fixed time step (default 50 Hz). |
| `late_update(delta_time: float) → None` | Called every frame after all Update calls. |
| `on_destroy() → None` | Called when the component or its GameObject is destroyed. |
| `on_enable() → None` | Called when the component is enabled. |
| `on_disable() → None` | Called when the component is disabled. |
| `on_validate() → None` | Called when a serialized field is changed in the Inspector (editor only). |
| `reset() → None` | Called when the component is reset to defaults (editor only). |
| `on_after_deserialize() → None` | Called after deserialization (scene load / undo). |
| `on_before_serialize() → None` | Called before serialization (scene save). |
| `on_draw_gizmos() → None` | Called every frame in the editor to draw gizmos for this component. |
| `on_draw_gizmos_selected() → None` | Called every frame in the editor ONLY when this object is selected. |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## Operators

| Method | Returns |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
```python
from Infernux import InxComponent, Vector3, serialized_field


class Patrol(InxComponent):
    speed: float = serialized_field(default=2.0, range=(0.0, 10.0))

    def update(self, delta_time: float) -> None:
        self.transform.translate(Vector3(self.speed * delta_time, 0.0, 0.0))
```
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also
- [GameObject](GameObject.md)
- [serialized_field](serialized_field.md)
- [Time](Time.md)
<!-- USER CONTENT END -->
