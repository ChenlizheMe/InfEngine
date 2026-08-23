# RenderStack

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

**Inherits from:** [InxComponent](InxComponent.md)

## Description

Scene singleton that binds reusable Effect assets to pipeline stages.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| pipeline_class_name | `str` |  |
| pipeline_params_json | `str` |  |
| effect_slots | `List[EffectSlot]` |  |
| effect_binding_error | `str` |  *(read-only)* |
| effect_compile_errors | `tuple[str, ...]` |  *(read-only)* |
| effect_stages | `tuple[EffectStage, ...]` |  *(read-only)* |
| orphan_effect_slots | `tuple[EffectSlot, ...]` |  *(read-only)* |
| pipeline | `RenderPipeline` | The currently active render pipeline. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `set_pipeline(pipeline_class_name: str) → None` | Set the active render pipeline by class name. |
| `get_effect_stage_slots(stage_id: str) → tuple[EffectSlot, ...]` |  |
| `set_effect_stage_slots(stage_id: str, slots: tuple[EffectSlot, ...]) → None` |  |
| `add_effect_slot(stage_id: str, effect: Any = ..., enabled: bool = ...) → EffectSlot` |  |
| `get_effect(stage_id: str, index: int = ...) → Optional[RenderEffect]` |  |
| `remap_orphan_effect_stage(old_stage_id: str, new_stage_id: str) → int` |  |
| `invalidate_graph() → None` | Mark the render graph as dirty, triggering a rebuild. |
| `build_graph() → Any` | Build and return the render graph description. |
| `render(context: Any, camera: Any) → None` | Execute the render stack for a camera. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `RenderStack.instance(scene: Any = ...) → Optional[RenderStack]` | Return the current active RenderStack, or None. |
| `static RenderStack.discover_pipelines() → Dict[str, type]` | Discover all available render pipeline classes. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Lifecycle Methods

| Method | Description |
|------|------|
| `awake() → None` | Initialize the render stack on component awake. |
| `on_destroy() → None` | Clean up the render stack when the component is destroyed. |
| `on_enable() → None` | Called when the component is enabled. |
| `on_disable() → None` | Called when the component is disabled. |
| `on_before_serialize() → None` | Serialize render stack state before saving. |
| `on_after_deserialize() → None` | Restore render stack state after loading. |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.5. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
