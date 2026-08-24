# ToneMappingEffect

<div class="class-info">
类位于 <b>Infernux.renderstack</b>
</div>

**继承自:** [FullScreenEffect](FullScreenEffect.md)

## 描述

色调映射效果。把 HDR 颜色压到屏幕可显示范围。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| name | `str` |  *(只读)* |
| injection_point | `str` |  *(只读)* |
| default_order | `int` |  *(只读)* |
| menu_path | `str` |  *(只读)* |
| mode | `ToneMappingMode` | 映射模式（ACES / Reinhard / Neutral 等）。 |
| exposure | `float` |  |
| gamma | `float` |  |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `get_shader_list() → List[str]` |  |
| `setup_passes(graph: RenderGraph, bus: ResourceBus) → None` |  |
| `set_params_dict(params: Dict[str, Any]) → None` | Restore parameters from a dictionary, normalizing the mode value. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.6 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
