# serialized_field

<div class="class-info">
函数位于 <b>Infernux.components</b>
</div>

```python
serialized_field(default: _SerializedValue = ..., field_type: Optional[FieldType] = ..., element_type: Optional[FieldType] = ..., element_class: Optional[Type] = ..., serializable_class: Optional[Type] = ..., component_type: Optional[str] = ..., asset_type: Optional[str] = ..., range: Optional[Tuple[float, float]] = ..., tooltip: str = ..., display_name_key: str = ..., enum_labels: Optional[List[str]] = ..., readonly: bool = ..., header: str = ..., space: float = ..., group: str = ..., info_text: str = ..., multiline: bool = ..., slider: bool = ..., drag_speed: Optional[float] = ..., required_component: Optional[str] = ..., visible_when: Optional[Callable] = ..., hdr: bool = ..., hidden: bool = ...) → _SerializedValue
```

## 描述

<!-- USER CONTENT START --> description
**状态：** Preview · **验证版本：** 0.3.7

每个序列化字段都应保留明确类型标注。元数据用于 Inspector 展示与校验，不能替代对缺失对象或资源引用的运行时检查。
<!-- USER CONTENT END -->

## 参数

| 名称 | 类型 | 描述 |
|------|------|------|
| default | `_SerializedValue` |  (default: `...`) |
| field_type | `Optional[FieldType]` |  (default: `...`) |
| element_type | `Optional[FieldType]` |  (default: `...`) |
| element_class | `Optional[Type]` |  (default: `...`) |
| serializable_class | `Optional[Type]` |  (default: `...`) |
| component_type | `Optional[str]` |  (default: `...`) |
| asset_type | `Optional[str]` |  (default: `...`) |
| range | `Optional[Tuple[float, float]]` |  (default: `...`) |
| tooltip | `str` |  (default: `...`) |
| display_name_key | `str` |  (default: `...`) |
| enum_labels | `Optional[List[str]]` |  (default: `...`) |
| readonly | `bool` |  (default: `...`) |
| header | `str` |  (default: `...`) |
| space | `float` |  (default: `...`) |
| group | `str` |  (default: `...`) |
| info_text | `str` |  (default: `...`) |
| multiline | `bool` |  (default: `...`) |
| slider | `bool` |  (default: `...`) |
| drag_speed | `Optional[float]` |  (default: `...`) |
| required_component | `Optional[str]` |  (default: `...`) |
| visible_when | `Optional[Callable]` |  (default: `...`) |
| hdr | `bool` |  (default: `...`) |
| hidden | `bool` |  (default: `...`) |

## 示例

<!-- USER CONTENT START --> example
```python
from Infernux import InxComponent, serialized_field


class ProjectileSettings(InxComponent):
    speed: float = serialized_field(
        default=20.0,
        range=(0.0, 100.0),
        tooltip="World units per second",
        slider=True,
    )
    notes: str = serialized_field(default="", multiline=True)
```
<!-- USER CONTENT END -->
