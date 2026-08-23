# Transform

<div class="class-info">
类位于 <b>Infernux</b>
</div>

**继承自:** [Component](Component.md)

## 描述

对象在场景中的位置、旋转和缩放。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| position | `Vector3` | 世界空间中的位置。 |
| euler_angles | `Vector3` | 世界空间中以欧拉角表示的旋转（度）。 |
| rotation | `quatf` | 世界空间中的旋转（四元数）。 |
| local_position | `Vector3` | 相对于父变换的位置。 |
| local_euler_angles | `Vector3` | 相对于父变换的欧拉角旋转（度）。 |
| local_scale | `Vector3` | 相对于父变换的缩放。 |
| local_rotation | `quatf` | 相对于父变换的旋转。 |
| lossy_scale | `Vector3` | 对象的全局缩放（只读）。 *(只读)* |
| forward | `Vector3` | 世界空间中的前方向向量（蓝轴）。 *(只读)* |
| right | `Vector3` | 世界空间中的右方向向量（红轴）。 *(只读)* |
| up | `Vector3` | 世界空间中的上方向向量（绿轴）。 *(只读)* |
| local_forward | `Vector3` |  *(只读)* |
| local_right | `Vector3` |  *(只读)* |
| local_up | `Vector3` |  *(只读)* |
| parent | `Optional[Transform]` | 父变换。 |
| root | `Transform` | 层级视图中最顶层的变换。 *(只读)* |
| child_count | `int` | 子变换数量。 *(只读)* |
| has_changed | `bool` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `set_parent(parent: Optional[Transform], world_position_stays: bool = True) → None` | 设置变换的父级。 |
| `get_child(index: int) → Transform` | 按索引获取子变换。 |
| `find(name: str) → Optional[Transform]` | 按名称查找子变换。 |
| `detach_children() → None` | 清除所有子项的父级。 |
| `is_child_of(parent: Transform) → bool` |  |
| `get_sibling_index() → int` | 获取同级索引。 |
| `set_sibling_index(index: int) → None` | 设置同级索引。 |
| `set_as_first_sibling() → None` | 将变换移动到兄弟列表的开头。 |
| `set_as_last_sibling() → None` | 将变换移动到兄弟列表的末尾。 |
| `set_local_trs(px: float, py: float, pz: float, rx: float, ry: float, rz: float, sx: float, sy: float, sz: float) → None` |  |
| `look_at(target: Vector3) → None` | 旋转变换使前方向指向目标位置。 |
| `translate(delta: Vector3, space: int = ...) → None` | 按指定方向和距离移动变换。 |
| `translate_local(delta: Vector3) → None` |  |
| `rotate(euler: Vector3, space: int = ...) → None` | 按欧拉角旋转变换。 |
| `rotate_around(point: Vector3, axis: Vector3, angle: float) → None` | 围绕指定轴和点旋转。 |
| `transform_point(point: Vector3) → Vector3` | 将点从本地空间变换到世界空间。 |
| `inverse_transform_point(point: Vector3) → Vector3` | 将点从世界空间变换到本地空间。 |
| `transform_direction(direction: Vector3) → Vector3` | 将方向从本地空间变换到世界空间。 |
| `inverse_transform_direction(direction: Vector3) → Vector3` | 将方向从世界空间变换到本地空间。 |
| `transform_vector(vector: Vector3) → Vector3` |  |
| `inverse_transform_vector(vector: Vector3) → Vector3` |  |
| `local_to_world_matrix() → List[float]` |  |
| `world_to_local_matrix() → List[float]` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.5 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
