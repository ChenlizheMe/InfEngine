# RenderGraph

<div class="class-info">
类位于 <b>Infernux.rendergraph</b>
</div>

## 描述

声明式渲染图。用 Pass 描述你想怎么画，引擎帮你调度。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `RenderGraph.__init__(name: str = ...) → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| name | `str` | The name of this render graph. *(只读)* |
| pass_count | `int` | Number of render passes in the graph. *(只读)* |
| texture_count | `int` | Number of texture resources in the graph. *(只读)* |
| buffer_count | `int` | Number of buffer resources in the graph. *(只读)* |
| topology_sequence | `List[Tuple[str, str]]` | Ordered list of (pass_name, type) entries defining the execution order. *(只读)* |
| injection_points | `list` | List of injection points for pass extension. *(只读)* |
| effect_stages | `List[EffectStage]` | Pipeline-declared user attachment stages in topology order. *(只读)* |
| current_effect_resources | `Mapping[str, TextureHandle]` |  *(只读)* |
| current_pass_result | `PassResult | None` |  *(只读)* |
| geometry_buffer_requirements | `` |  *(只读)* |
| pass_results | `Mapping[str, PassResult]` |  *(只读)* |
| latest_pass_result | `PassResult | None` |  *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `set_msaa_samples(samples: int) → None` | Set the MSAA sample count for all render targets. |
| `create_texture(name: str, format: Format = ..., camera_target: bool = ..., size: Optional[Tuple[int, int]] = ..., size_divisor: int = ..., samples: Optional[int] = ...) → TextureHandle` | 创建临时纹理。 |
| `get_texture(name: str) → Optional[TextureHandle]` | Get a texture handle by name, or None if not found. |
| `name_scope(prefix: str) → AbstractContextManager[RenderGraph]` |  |
| `effect_resources(resources: Mapping[str, TextureHandle]) → AbstractContextManager[RenderGraph]` |  |
| `pass_result(result: PassResult) → AbstractContextManager[RenderGraph]` |  |
| `replace_current_pass_result(result: PassResult) → None` |  |
| `resolve_effect_route_policy(stages)` |  |
| `create_buffer(name: str, byte_size: int, storage: bool = ..., indirect: bool = ..., transfer_source: bool = ..., transfer_destination: bool = ...) → BufferHandle` | Declare a transient buffer resource in the render graph. |
| `get_buffer(name: str) → Optional[BufferHandle]` | Get a buffer handle by name, or None if not found. |
| `has_pass(name: str) → bool` | Check if a render pass with the given name exists. |
| `has_injection_point(name: str) → bool` | Check if an injection point with the given name exists. |
| `has_effect_stage(stable_id: str) → bool` | Check for an exact declared stage ID. |
| `injection_point(name: str, display_name: str = ..., resources: Optional[set] = ...) → None` | Declare an injection point where external passes can be inserted. |
| `effect_stage(stable_id: str, scope: EffectScope | str = ..., display_name: str = ..., inputs: Optional[set[str]] = ..., outputs: Optional[set[str]] = ..., capabilities: Optional[set[str]] = ...) → EffectStage` | Declare a stable user-facing RenderEffect attachment stage. |
| `effects(stable_id: str) → EffectStage` | Pipeline-author shorthand for ``effect_stage``. |
| `screen_ui_section(resources: set | None = ...) → None` | Declare a screen UI section in the graph topology. |
| `set_geometry_buffer_requirements(requirements) → None` |  |
| `require_geometry_buffers(requirements) → None` |  |
| `needs_geometry_buffer(semantic: str) → bool` |  |
| `publish_pass_result(source: str, buffers, materialize = ...) → PassResult` |  |
| `derive_pass_result(source: str, parent: PassResult, overrides) → PassResult` |  |
| `write_buffer(source: str, parent: PassResult, name: str, texture: TextureHandle) → PassResult` |  |
| `get_pass_result(source: str) → PassResult | None` |  |
| `camera_ui_section(resources: set | None = ...) → None` | Draw Camera UI and declare the after-camera-UI effect stage. |
| `screen_ui_overlay_section(resources: set | None = ...) → None` | Encode for display, draw Screen UI, and declare its effect stage. |
| `add_pass(name: str) → RenderPassBuilder` | 添加一个渲染 Pass。 |
| `add_copy_pass(name: str) → RenderPassBuilder` | Add a transfer-domain texture or buffer copy pass. |
| `add_present_pass(name: str) → RenderPassBuilder` | Add a final graph export pass. |
| `remove_pass(name: str) → RenderPassBuilder | None` | Remove a render pass by name. |
| `append_pass(builder: RenderPassBuilder) → None` | Append an existing RenderPassBuilder to the graph. |
| `set_output(texture: str | TextureHandle) → None` | Set the final output texture of the render graph. |
| `validate_no_ip_before_first_pass() → None` | Validate that no user extension point appears before the first pass. |
| `get_debug_string() → str` | Return a human-readable summary of the graph for debugging. |
| `build() → RenderGraphDescription` | Compile the graph into a RenderGraphDescription for the backend. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.4 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
