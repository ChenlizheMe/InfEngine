from __future__ import annotations

from typing import Any, Callable, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

JIT_AVAILABLE: bool

def ensure_jit_runtime(*, auto_install: bool = True) -> bool:
    """Ensure the current runtime can import Numba-backed JIT helpers."""
    ...

def njit(*args: Any, **kwargs: Any) -> Any:
    """Numba ``njit`` decorator, or a no-op fallback when Numba is unavailable.

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
    """
    ...

def warmup(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Pre-compile a ``@njit`` function by calling it with representative args."""
    ...

prange: Any

__all__: list[str]
