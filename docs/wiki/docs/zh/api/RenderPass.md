# RenderPass

<div class="class-info">
类位于 <b>Infernux.renderstack</b>
</div>

## 描述

自定义渲染 Pass 的基类。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 构造函数

| 签名 | 描述 |
|------|------|
| `RenderPass.__init__(enabled: bool = ...) → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## 属性

| 名称 | 类型 | 描述 |
|------|------|------|
| name | `str` | The unique name of this render pass. *(只读)* |
| injection_point | `str` | The injection point where this pass is inserted. *(只读)* |
| default_order | `int` | Default execution order within the injection point. *(只读)* |
| requires | `Set[str]` | Resource names this pass reads from. *(只读)* |
| modifies | `Set[str]` | Resource names this pass writes to. *(只读)* |
| creates | `Set[str]` | Resource names this pass creates. *(只读)* |
| enabled | `bool` | Whether this pass is currently enabled. |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## 公共方法

| 方法 | 描述 |
|------|------|
| `inject(graph: RenderGraph, bus: ResourceBus) → None` | Inject render commands into the graph using the resource bus. |
| `validate(available_resources: Set[str]) → List[str]` | Validate that all required resources are available. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## 运算符

| 方法 | 返回值 |
|------|------|
| `__repr__() → str` | `str` |

<!-- USER CONTENT START --> operators

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.3.6 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
