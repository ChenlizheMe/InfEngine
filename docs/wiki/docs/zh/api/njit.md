# njit

<div class="class-info">
函数位于 <b>Infernux.jit</b>
</div>

```python
njit() → Any
```

## 描述

Numba ``njit`` decorator, or a no-op fallback when Numba is unavailable.

The decorated function gains a ``.py`` attribute pointing to the
original pure-Python source.

Supports ``auto_parallel=True`` as an Infernux extension. A conservative
Typed HIR pass proves one-dimensional affine loops before creating a
``prange`` variant. A static HIR cost model decides clear small/large
one-shot workloads before timing; gray-zone signatures use :func:`warmup`
for isolated equivalence and multi-sample measurement. Decisions remain
bounded per dtype/rank/layout/shape/thread-count bucket. Use
``parallel_policy="required"`` to reject kernels which cannot be proven
parallel instead of retaining serial execution.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->
