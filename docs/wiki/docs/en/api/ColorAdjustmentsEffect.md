# ColorAdjustmentsEffect

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [FullScreenEffect](FullScreenEffect.md)

## Description

URP-aligned Color Adjustments post-processing effect.

Post-exposure, contrast, saturation, hue shift — operates in HDR space.

Attributes:
    post_exposure: Exposure offset in EV stops (default 0.0).
    contrast: Contrast adjustment (-100 to 100).
    saturation: Saturation adjustment (-100 to 100).
    hue_shift: Hue rotation in degrees (-180 to 180).

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| name | `str` |  *(read-only)* |
| injection_point | `str` |  *(read-only)* |
| default_order | `int` |  *(read-only)* |
| menu_path | `str` |  *(read-only)* |
| post_exposure | `float` |  |
| contrast | `float` |  |
| saturation | `float` |  |
| hue_shift | `float` |  |

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
