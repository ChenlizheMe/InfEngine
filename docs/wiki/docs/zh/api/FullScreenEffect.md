# FullScreenEffect

<div class="class-info">
类位于 <b>Infernux.renderstack</b>
</div>

**继承自:** [RenderPass](RenderPass.md)

## 描述

全屏后处理效果基类。自定义后处理从这里继承。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `FullScreenEffect.__init__(enabled: bool = ...) → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| requires | `Set[str]` |  *(只读)* |
| modifies | `Set[str]` |  *(只读)* |
| menu_path | `str` |  *(只读)* |
| color_composition | `EffectColorComposition` |  *(只读)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
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

## 静态方法

| 方法 | 描述 |
|------|------|
| `static FullScreenEffect.get_or_create_texture(graph: RenderGraph, name: str, format: Any = ..., camera_target: bool = ..., size: Any = ..., size_divisor: int = ...) → Any` | Get or create a named texture handle in the render graph. |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 运算符

| 方法 | 返回值 |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.4 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
