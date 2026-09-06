# FullScreenEffect

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [RenderPass](RenderPass.md)

## Description

Base class for fullscreen post-processing effects.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `FullScreenEffect.__init__(enabled: bool = ...) → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| requires | `Set[str]` |  *(read-only)* |
| modifies | `Set[str]` |  *(read-only)* |
| menu_path | `str` |  *(read-only)* |
| color_composition | `EffectColorComposition` |  *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `setup_passes(graph: RenderGraph, bus: ResourceBus) → None` | Override to add fullscreen passes to the render graph. |
| `get_shader_list() → List[str]` | Return shader paths required by this effect. |
| `inject(graph: RenderGraph, bus: ResourceBus) → None` | Inject this effect into the render graph. |
| `get_params_dict() → Dict[str, Any]` | Get serializable parameters as a dictionary. |
| `set_params_dict(params: Dict[str, Any]) → None` | Restore parameters from a dictionary. |
| `apply_single_source_effect(graph: RenderGraph, bus: ResourceBus, output_name: str, pass_name: str, shader_name: str, format: Any, params: Mapping[str, object] | None = ...) → bool` | Apply a one-pass fullscreen effect that rewrites the scene color. |
| `bind_buffers(render_pass: Any, bus: ResourceBus, extra_bindings: Mapping[str, object] | None = ...) → None` |  |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static FullScreenEffect.get_or_create_texture(graph: RenderGraph, name: str, format: Any = ..., camera_target: bool = ..., size: Any = ..., size_divisor: int = ...) → Any` | Get or create a named texture handle in the render graph. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Operators

| Method | Returns |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.4.0. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
