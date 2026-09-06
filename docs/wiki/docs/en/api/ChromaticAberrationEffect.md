# ChromaticAberrationEffect

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [FullScreenEffect](FullScreenEffect.md)

## Description

URP-aligned Chromatic Aberration post-processing effect.

Simulates lens imperfection where different wavelengths refract
at different angles, producing RGB channel separation from screen center.

Attributes:
    intensity: Channel separation strength (0 = off, 1 = strong).

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| name | `str` |  *(read-only)* |
| injection_point | `str` |  *(read-only)* |
| default_order | `int` |  *(read-only)* |
| menu_path | `str` |  *(read-only)* |
| intensity | `float` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `get_shader_list() → List[str]` |  |
| `setup_passes(graph: RenderGraph, bus: ResourceBus) → None` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.4.0. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
