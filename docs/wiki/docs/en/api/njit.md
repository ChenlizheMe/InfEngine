# njit

<div class="class-info">
function in <b>Infernux.jit</b>
</div>

```python
njit() → Any
```

## Description

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

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.7. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->
