# Scene

<div class="class-info">
class in <b>Infernux</b>
</div>

## Description

A single scene containing GameObjects.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| name | `str` |  |
| structure_version | `int` |  *(read-only)* |
| temporal_discontinuity_revision | `int` |  *(read-only)* |
| world_id | `int` |  *(read-only)* |
| main_camera | `Optional[Camera]` |  |
| effective_game_camera | `Optional[Camera]` |  *(read-only)* |
| active_game_cameras | `List[Camera]` |  *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `get_environment() → Dict[str, Any]` |  |
| `set_environment(settings: Dict[str, Any]) → None` |  |
| `resolve_skybox_material() → Optional[InxMaterial]` |  |
| `set_playing(playing: bool) → None` |  |
| `create_game_object(name: str = 'GameObject') → GameObject` |  |
| `create_primitive(type: PrimitiveType, name: str = '') → GameObject` |  |
| `create_primitives_batch(type: PrimitiveType, count: int, name_prefix: str = '', with_colliders: bool = True) → List[GameObject]` |  |
| `create_from_model(guid: str, name: str = '') → Optional[GameObject]` |  |
| `get_root_objects() → List[GameObject]` |  |
| `get_all_objects() → List[GameObject]` |  |
| `find_objects_with_component(type_name: str) → List[GameObject]` |  |
| `find(name: str) → Optional[GameObject]` |  |
| `find_by_id(id: int) → Optional[GameObject]` |  |
| `resolve_game_object(handle: ObjectHandle) → Optional[GameObject]` |  |
| `resolve_component(handle: ObjectHandle) → Optional[Component]` |  |
| `find_object_by_id(id: int) → Optional[GameObject]` |  |
| `find_with_tag(tag: str) → Optional[GameObject]` |  |
| `find_game_objects_with_tag(tag: str) → List[GameObject]` |  |
| `find_game_objects_in_layer(layer: int) → List[GameObject]` |  |
| `destroy_game_object(game_object: GameObject) → None` |  |
| `process_pending_destroys() → None` |  |
| `is_playing() → bool` |  |
| `awake_object(game_object: GameObject) → None` |  |
| `serialize() → str` |  |
| `serialize_document() → Dict[str, Any]` |  |
| `save_to_file(path: str) → None` |  |
| `has_pending_py_components() → bool` |  |
| `get_pending_py_components() → List[PendingPyComponent]` |  |
| `take_pending_py_components() → List[PendingPyComponent]` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Lifecycle Methods

| Method | Description |
|------|------|
| `start() → None` |  |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.4.0. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
