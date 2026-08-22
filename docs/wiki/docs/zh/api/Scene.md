# Scene

<div class="class-info">
类位于 <b>Infernux</b>
</div>

## 描述

运行时场景，包含 GameObject 层级。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| name | `str` | 场景名称。 |
| structure_version | `int` |  *(只读)* |
| temporal_discontinuity_revision | `int` |  *(只读)* |
| world_id | `int` |  *(只读)* |
| main_camera | `Optional[Camera]` |  |
| effective_game_camera | `Optional[Camera]` |  *(只读)* |
| active_game_cameras | `List[Camera]` |  *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
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

## 生命周期方法

| 方法 | 描述 |
|------|------|
| `start() → None` |  |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.4 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
