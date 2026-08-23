# RenderStack

<div class="class-info">
类位于 <b>Infernux.renderstack</b>
</div>

**继承自:** [InxComponent](InxComponent.md)

## 描述

后处理效果栈。管理一系列后处理 Pass 的执行顺序。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| pipeline_class_name | `str` |  |
| pipeline_params_json | `str` |  |
| effect_slots | `List[EffectSlot]` |  |
| effect_binding_error | `str` |  *(只读)* |
| effect_compile_errors | `tuple[str, ...]` |  *(只读)* |
| effect_stages | `tuple[EffectStage, ...]` |  *(只读)* |
| orphan_effect_slots | `tuple[EffectSlot, ...]` |  *(只读)* |
| pipeline | `RenderPipeline` | The currently active render pipeline. *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
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

## 静态方法

| 方法 | 描述 |
|------|------|
| `RenderStack.instance(scene: Any = ...) → Optional[RenderStack]` | Return the current active RenderStack, or None. |
| `static RenderStack.discover_pipelines() → Dict[str, type]` | Discover all available render pipeline classes. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 生命周期方法

| 方法 | 描述 |
|------|------|
| `awake() → None` | Initialize the render stack on component awake. |
| `on_destroy() → None` | Clean up the render stack when the component is destroyed. |
| `on_enable() → None` | Called when the component is enabled. |
| `on_disable() → None` | Called when the component is disabled. |
| `on_before_serialize() → None` | Serialize render stack state before saving. |
| `on_after_deserialize() → None` | Restore render stack state after loading. |

<!-- USER CONTENT START --> lifecycle_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.5 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
