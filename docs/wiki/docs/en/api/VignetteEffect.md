# VignetteEffect

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [FullScreenEffect](FullScreenEffect.md)

## Description

URP-aligned Vignette post-processing effect.

Darkens screen edges for cinematic framing.

Attributes:
    intensity: Vignette strength (0 = off, 1 = full black edges).
    smoothness: Falloff softness.
    roundness: Shape control (1 = circular, lower = squared).
    rounded: Force perfectly circular regardless of aspect ratio.

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
| smoothness | `float` |  |
| roundness | `float` |  |
| rounded | `bool` |  |

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
> **Example status:** No curated example has been verified for this symbol in 0.3.5. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
