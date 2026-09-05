# InxComponent

<div class="class-info">
类位于 <b>Infernux.components</b>
</div>

## 描述

用户脚本组件的基类，类似于 Unity 的 MonoBehaviour。

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.7

项目玩法组件应继承此基类。不要只阅读生命周期表；请先完成第一个组件。
<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `InxComponent.__init__() → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| game_object | `GameObject` | 此组件附加到的 GameObject。 *(只读)* |
| transform | `Transform` | 附加到此 GameObject 的 Transform。 *(只读)* |
| is_valid | `bool` | Whether the underlying GameObject reference is still alive. *(只读)* |
| enabled | `bool` | 此组件是否已启用。 |
| type_name | `str` | Class name of this component. *(只读)* |
| execution_order | `int` | Execution order (lower value runs earlier). |
| component_id | `int` | Unique auto-incremented ID for this component instance. *(只读)* |
| tag | `str` | Tag of the attached GameObject. |
| game_object_layer | `int` | Layer index (0–31) of the attached GameObject. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `destroy() → None` | 销毁此组件或指定的 GameObject。 |
| `on_collision_enter(collision: Any) → None` | Called when this collider starts touching another collider. |
| `on_collision_stay(collision: Any) → None` | Called every fixed-update while two colliders remain in contact. |
| `on_collision_exit(collision: Any) → None` | Called when two colliders stop touching. |
| `on_trigger_enter(other: Any) → None` | Called when another collider enters this trigger volume. |
| `on_trigger_stay(other: Any) → None` | Called every fixed-update while another collider is inside this trigger. |
| `on_trigger_exit(other: Any) → None` | Called when another collider exits this trigger volume. |
| `start_coroutine(generator: Any) → Coroutine` | 启动一个协程。 |
| `stop_coroutine(coroutine: Coroutine) → None` | 停止一个协程。 |
| `stop_all_coroutines() → None` | 停止所有协程。 |
| `compare_tag(tag: str) → bool` | Returns True if the attached GameObject's tag matches. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `InxComponent.reserve_instances(count: int) → None` | Preallocate numeric-field storage for bulk component creation. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 生命周期方法

| 方法 | 描述 |
|------|------|
| `awake() → None` | 组件创建时调用一次。 |
| `start() → None` | 首次 Update 之前调用一次。 |
| `update(delta_time: float) → None` | 每帧调用一次。 |
| `fixed_update(fixed_delta_time: float) → None` | 以固定时间间隔调用。 |
| `late_update(delta_time: float) → None` | 在所有 Update 调用之后每帧调用。 |
| `on_destroy() → None` | 组件即将被销毁时调用。 |
| `on_enable() → None` | 组件启用时调用。 |
| `on_disable() → None` | 组件禁用时调用。 |
| `on_validate() → None` | 编辑器中属性变更时调用。 |
| `reset() → None` | Called when the component is reset to defaults (editor only). |
| `on_after_deserialize() → None` | Called after deserialization (scene load / undo). |
| `on_before_serialize() → None` | Called before serialization (scene save). |
| `on_draw_gizmos() → None` | 每帧绘制 Gizmos 时调用。 |
| `on_draw_gizmos_selected() → None` | 选中时绘制 Gizmos。 |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## 运算符

| 方法 | 返回值 |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
```python
import infernux as inx


class Patrol(inx.InxComponent):
    speed: float = inx.serialized_field(default=2.0, range=(0.0, 10.0))

    def update(self, delta_time: float) -> None:
        self.transform.translate(inx.Vector3(self.speed * delta_time, 0.0, 0.0))
```
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [GameObject](GameObject.md)
- [serialized_field](serialized_field.md)
- [Time](Time.md)
<!-- USER CONTENT END -->
