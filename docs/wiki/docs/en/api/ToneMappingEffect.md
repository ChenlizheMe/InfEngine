# ToneMappingEffect

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [FullScreenEffect](FullScreenEffect.md)

## Description

HDR-to-LDR tone mapping post-processing effect.

Should be the last effect in the post-process chain so that bloom
and other HDR effects can operate on the full dynamic range.

Attributes:
    mode: Tone mapping operator (ACES is recommended).
    exposure: Pre-tonemap exposure multiplier (default 1.0).
    gamma: Gamma correction exponent (default 2.2 = standard sRGB).

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| name | `str` |  *(read-only)* |
| injection_point | `str` |  *(read-only)* |
| default_order | `int` |  *(read-only)* |
| menu_path | `str` |  *(read-only)* |
| mode | `ToneMappingMode` |  |
| exposure | `float` |  |
| gamma | `float` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `get_shader_list() → List[str]` |  |
| `setup_passes(graph: RenderGraph, bus: ResourceBus) → None` |  |
| `set_params_dict(params: Dict[str, Any]) → None` | Restore parameters from a dictionary, normalizing the mode value. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
