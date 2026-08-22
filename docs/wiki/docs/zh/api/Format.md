# Format

<div class="class-info">
枚举位于 <b>Infernux.rendergraph</b>
</div>

## 描述

渲染目标使用的纹理格式。这个公共别名映射到原生 `PixelFormat` 枚举。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 枚举值

| 名称 | 描述 |
|------|------|
| RGBA8_UNORM |  |
| RGBA8_SRGB |  |
| BGRA8_UNORM |  |
| RGBA16_SFLOAT |  |
| RGBA32_SFLOAT |  |
| R32_SFLOAT |  |
| R8_UNORM |  |
| R8G8_UNORM |  |
| RG16_SFLOAT |  |
| A2R10G10B10_UNORM |  |
| R16_SFLOAT |  |
| D32_SFLOAT |  |
| D24_UNORM_S8_UINT |  |

<!-- USER CONTENT START --> enum_values

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| is_depth | `bool` | 如果该格式为深度格式则返回 True。*（只读）* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.4 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also
- [RenderGraph](RenderGraph.md)
<!-- USER CONTENT END -->
