# GameObject

<div class="class-info">
类位于 <b>Infernux</b>
</div>

## 描述

场景层级中具有组件的游戏对象。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.6

GameObject 拥有 Transform 与一组组件。注意 `active_self` 与派生状态 `active_in_hierarchy` 的区别，并优先按类型查找组件。
<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| name | `str` | 此 GameObject 的名称。 |
| active | `bool` | 此 GameObject 是否处于活动状态。 |
| tag | `str` | 此 GameObject 的标签字符串。 |
| layer | `int` | 此 GameObject 的层级索引 (0-31)。 |
| is_static | `bool` | 静态标志。 |
| prefab_guid | `str` |  |
| prefab_root | `bool` |  |
| active_self | `bool` | 此对象自身是否处于活动状态。active 的别名。 *(只读)* |
| active_in_hierarchy | `bool` | 此对象在层级中是否处于活动状态。 *(只读)* |
| id | `int` | 唯一对象标识符。 *(只读)* |
| handle | `ObjectHandle` |  *(只读)* |
| is_prefab_instance | `bool` |  *(只读)* |
| game_object | `Optional[GameObject]` |  *(只读)* |
| transform | `Transform` | 获取 Transform 组件。 *(只读)* |
| scene | `Scene` | 此 GameObject 所属的场景。 *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `compare_tag(tag: str) → bool` | 此 GameObject 的标签是否与给定标签匹配。 |
| `get_transform() → Transform` | 获取 Transform 组件。 |
| `add_component(component_type: Any) → Optional[Any]` | 通过类型或类型名称添加 C++ 组件。 |
| `can_add_component(type_name: str) → bool` |  |
| `get_add_component_blockers(type_name: str) → List[str]` |  |
| `remove_component(component: Any) → bool` | 移除一个组件实例（无法移除 Transform）。 |
| `can_remove_component(component: Any) → bool` |  |
| `get_remove_component_blockers(component: Any) → List[str]` |  |
| `get_components(component_type: Any = ...) → List[Any]` | 获取所有组件（包括 Transform）。 |
| `get_component_order() → List[int]` |  |
| `set_component_order(component_ids: List[int]) → bool` |  |
| `get_component_default_document(component: Component) → Dict[str, Any]` |  |
| `get_component(component_type: Any) → Optional[Any]` |  |
| `get_cpp_component(type_name: str) → Optional[Component]` | 根据类型名称获取 C++ 组件。 |
| `get_cpp_components(type_name: str) → List[Component]` | 获取指定类型名称的所有 C++ 组件。 |
| `add_py_component(component_instance: Any) → Any` | 向此 GameObject 添加 Python InxComponent 实例。 |
| `get_py_component(component_type: Any) → Any` | 获取指定类型的 Python 组件。 |
| `get_py_components() → List[Any]` | 获取附加到此 GameObject 的所有 Python 组件。 |
| `remove_py_component(component: Any) → bool` | 移除一个 Python 组件实例。 |
| `replace_py_component(old_component: Any, new_component: Any) → Any` |  |
| `get_parent() → Optional[GameObject]` | 获取父级 GameObject。 |
| `set_parent(parent: Optional[GameObject], world_position_stays: bool = True) → None` | 设置父级 GameObject（None 表示根级）。 |
| `get_children() → List[GameObject]` | 获取子 GameObject 列表。 |
| `get_child_count() → int` | 获取子对象数量。 |
| `get_child(index: int) → GameObject` | 根据索引获取子对象。 |
| `find_child(name: str) → Optional[GameObject]` | 根据名称查找直接子对象（非递归）。 |
| `find_descendant(name: str) → Optional[GameObject]` | 根据名称查找后代对象（递归深度优先搜索）。 |
| `is_active_in_hierarchy() → bool` | 检查此对象及所有父对象是否处于活动状态。 |
| `get_component_in_children(component_type: Any, include_inactive: bool = False) → Any` |  |
| `get_component_in_parent(component_type: Any, include_inactive: bool = False) → Any` |  |
| `serialize() → str` | 将 GameObject 序列化为 JSON 字符串。 |
| `deserialize(json_str: str) → bool` | 从 JSON 字符串反序列化 GameObject。 |
| `serialize_document() → Dict[str, Any]` |  |
| `deserialize_document(document: Dict[str, Any]) → bool` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static GameObject.find(name: str) → Optional[GameObject]` |  |
| `static GameObject.find_with_tag(tag: str) → Optional[GameObject]` |  |
| `static GameObject.find_game_objects_with_tag(tag: str) → List[GameObject]` |  |
| `static GameObject.instantiate(original: Any) → Any` |  |
| `static GameObject.destroy(game_object: GameObject) → None` |  |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
from Infernux import GameObject, Rigidbody

player = GameObject.find("Player")
if player is not None and player.active_in_hierarchy:
    body = player.get_component(Rigidbody)
    if body is not None:
        body.use_gravity = True
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [Transform](Transform.md)
- [InxComponent](InxComponent.md)
<!-- USER CONTENT END -->
