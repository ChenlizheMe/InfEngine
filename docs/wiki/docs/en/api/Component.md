# Component

<div class="class-info">
class in <b>Infernux</b>
</div>

## Description

Base class for all components attached to GameObjects.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| type_name | `str` |  *(read-only)* |
| component_id | `int` |  *(read-only)* |
| handle | `ObjectHandle` |  *(read-only)* |
| enabled | `bool` |  |
| execution_order | `int` |  |
| game_object | `GameObject` |  *(read-only)* |
| required_component_types | `List[str]` |  *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `is_component_type(type_name: str) → bool` |  |
| `serialize() → str` |  |
| `serialize_document() → Dict[str, Any]` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
