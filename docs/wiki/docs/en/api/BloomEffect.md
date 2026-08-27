# BloomEffect

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [FullScreenEffect](FullScreenEffect.md)

## Description

Unity-aligned Bloom post-processing effect.

Uses a progressive downsample/upsample chain with soft threshold
and scatter-based diffusion, matching Unity URP's Bloom implementation.

Attributes:
    threshold: Minimum brightness for bloom contribution (default 1.0).
    intensity: Final bloom intensity multiplier (default 0.8).
    scatter: Diffusion / spread factor (default 0.7).
    clamp: Maximum brightness to prevent fireflies (default 65472).
    tint_r: Red channel tint (0–1).
    tint_g: Green channel tint (0–1).
    tint_b: Blue channel tint (0–1).
    max_iterations: Maximum downsample/upsample iterations (1–8).

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| name | `str` |  *(read-only)* |
| injection_point | `str` |  *(read-only)* |
| default_order | `int` |  *(read-only)* |
| menu_path | `str` |  *(read-only)* |
| threshold | `float` |  |
| intensity | `float` |  |
| scatter | `float` |  |
| clamp | `float` |  |
| tint | `list[float]` |  |
| max_iterations | `int` |  |

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
> **Example status:** No curated example has been verified for this symbol in 0.3.7. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
