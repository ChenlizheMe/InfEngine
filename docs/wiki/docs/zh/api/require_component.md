# require_component

<div class="class-info">
函数位于 <b>Infernux.components</b>
</div>

```python
require_component() → Callable
```

## 描述

Declare that a component requires other component types.

Example::

    @require_component(Rigidbody, Collider)
    class PhysicsController(InxComponent): ...

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->
