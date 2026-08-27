# Component

<div class="class-info">
类位于 <b>Infernux</b>
</div>

## 描述

附加到 GameObject 的所有组件的基类。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| type_name | `str` |  *(只读)* |
| component_id | `int` |  *(只读)* |
| handle | `ObjectHandle` |  *(只读)* |
| enabled | `bool` | 此组件是否已启用。 |
| execution_order | `int` |  |
| game_object | `GameObject` | 此组件附加到的 GameObject。 *(只读)* |
| required_component_types | `List[str]` |  *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `is_component_type(type_name: str) → bool` |  |
| `serialize() → str` |  |
| `serialize_document() → Dict[str, Any]` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.7 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
