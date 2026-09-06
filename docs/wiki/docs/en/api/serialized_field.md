# serialized_field

<div class="class-info">
function in <b>Infernux.components</b>
</div>

```python
serialized_field(default: _SerializedValue = ..., field_type: Optional[FieldType] = ..., element_type: Optional[FieldType] = ..., element_class: Optional[Type] = ..., serializable_class: Optional[Type] = ..., component_type: Optional[str] = ..., asset_type: Optional[str] = ..., range: Optional[Tuple[float, float]] = ..., tooltip: str = ..., display_name_key: str = ..., enum_labels: Optional[List[str]] = ..., readonly: bool = ..., header: str = ..., space: float = ..., group: str = ..., info_text: str = ..., multiline: bool = ..., slider: bool = ..., drag_speed: Optional[float] = ..., required_component: Optional[str] = ..., visible_when: Optional[Callable] = ..., hdr: bool = ..., curve_non_negative: bool = ..., hidden: bool = ...) → _SerializedValue
```

## Description

<!-- USER CONTENT START --> description
**Status:** Preview · **Verified with:** 0.4.0

Keep an explicit type annotation beside each serialized field. Metadata controls Inspector presentation and validation; it does not replace runtime checks for missing object or asset references.
<!-- USER CONTENT END -->

## Parameters

| Name | Type | Description |
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
| curve_non_negative | `bool` |  (default: `...`) |
| hidden | `bool` |  (default: `...`) |

## Example

<!-- USER CONTENT START --> example
```python
import infernux as inx


class ProjectileSettings(inx.InxComponent):
    speed: float = inx.serialized_field(
        default=20.0,
        range=(0.0, 100.0),
        tooltip="World units per second",
        slider=True,
    )
    notes: str = inx.serialized_field(default="", multiline=True)
```
<!-- USER CONTENT END -->
