"""
Internal JIT bootstrap — DO NOT import directly.

Use the public API instead::

    from Infernux.jit import njit, warmup, JIT_AVAILABLE
"""

from __future__ import annotations

import ast
import copy
import functools
import inspect
import os
from statistics import median
import sys as _sys
import textwrap
from time import perf_counter

from Infernux.jit_hir import FunctionHIR, analyze_source, hir_fingerprint
from Infernux.jit_runtime import (
    BoundedLRU,
    DispatchDecision,
    calls_equivalent,
    clone_call_arguments,
    compiler_fingerprint,
    runtime_signature,
    static_cost_decision,
)

_HAS_NUMBA = False
_real_njit = None
try:
    from numba import njit as _numba_njit  # type: ignore[import-untyped]
    _real_njit = _numba_njit
    _HAS_NUMBA = True
except Exception as _exc:
    if hasattr(_sys, '_INFERNUX_DEBUG'):
        print(f"[_jit_kernels] numba unavailable: {type(_exc).__name__}: {_exc}",
              flush=True)


JIT_AVAILABLE = _HAS_NUMBA

# Debug flag: set ``sys._INFERNUX_DEBUG = True`` to see verbose auto_parallel
# diagnostic messages.
_DEBUG = hasattr(_sys, "_INFERNUX_DEBUG")


def _log_jit(msg: str) -> None:
    """Log a JIT diagnostic through the engine Debug system AND stdout.

    In packaged debug builds the boot script redirects stdout to the debug
    log file, so ``print()`` always reaches a log.  We also forward to the
    engine's ``Debug`` system for the in-editor Console panel.
    """
    print(msg, flush=True)
    try:
        from Infernux.debug import Debug  # late import to avoid circular deps
        Debug.log_internal(msg)
    except Exception:
        pass


# In Nuitka standalone builds user scripts are compiled to .pyc and the
# originals removed.  Numba's cache locator requires the source .py to
# exist, so ``cache=True`` would raise RuntimeError.
_NUITKA_COMPILED = "__compiled__" in globals()

# ── Compilation cache ─────────────────────────────────────────────────
# Prevents re-compiling the same @njit function when a user script module
# is re-imported (e.g. scene loading calls load_all_components_from_file
# multiple times for the same file).  Keyed by (co_filename, func_name, code_hash).
_compiled_cache = BoundedLRU(128)

try:
    from numba import prange as _numba_prange  # type: ignore[import-untyped]
except Exception:
    _numba_prange = range

prange = _numba_prange


def _njit_cache_key(fn, kwargs_tag: str = "") -> tuple:
    """Build a hashable cache key for a @njit function.

    Uses (co_filename, func_name, bytecode_hash, kwargs_tag) so that
    re-importing the same module reuses the previous compilation as long
    as the function source hasn't changed.
    """
    if getattr(fn, "__code__", None) is None:
        return None
    return compiler_fingerprint(fn, {"numba_options": kwargs_tag})


def _compile_njit(fn, kwargs):
    """Compile *fn* with the current numba njit factory and attach ``.py``.

    Automatically drops ``cache=True`` when the source ``.py`` file is
    missing (e.g. in packaged builds where only ``.pyc`` remains), because
    Numba's cache locator requires the source file.
    """
    if kwargs.get("cache"):
        co_file = getattr(getattr(fn, "__code__", None), "co_filename", "")
        if co_file and not os.path.isfile(co_file):
            kwargs = dict(kwargs)
            kwargs.pop("cache", None)
    if kwargs:
        compiled = _real_njit(**kwargs)(fn)
    else:
        compiled = _real_njit(fn)
    compiled.py = fn
    return compiled


def _compile_njit_cached(fn, kwargs):
    """Like _compile_njit but reuses a previous result if the bytecode matches."""
    kwargs_tag = ",".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
    cache_key = _njit_cache_key(fn, kwargs_tag)
    if cache_key and cache_key in _compiled_cache:
        _log_jit(f"[JIT] {fn.__name__}: reusing cached compilation")
        cached = _compiled_cache[cache_key]
        cached.py = fn
        return cached
    compiled = _compile_njit(fn, kwargs)
    if cache_key:
        _compiled_cache[cache_key] = compiled
    return compiled


def _is_range_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
    )


def _is_true_constant(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_njit_decorator(node) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "njit"
    if isinstance(node, ast.Attribute):
        return node.attr == "njit"
    return False


def _decorator_requests_auto_parallel(node) -> bool:
    if not isinstance(node, ast.Call) or not _is_njit_decorator(node.func):
        return False
    for keyword in node.keywords:
        if keyword.arg == "auto_parallel" and _is_true_constant(keyword.value):
            return True
    return False


class _AutoParallelRangeTransformer(ast.NodeTransformer):
    def __init__(self, eligible_locations, *, range_name: str = "prange"):
        self._eligible_locations = frozenset(eligible_locations)
        self._range_name = range_name
        self.rewrote = False

    def visit_For(self, node):
        self.generic_visit(node)
        location = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if location not in self._eligible_locations or not _is_range_call(node.iter):
            return node
        node.iter.func.id = self._range_name
        self.rewrote = True
        return node


def _clear_function_annotations(function_node) -> None:
    function_node.returns = None
    for argument in (
        *function_node.args.posonlyargs,
        *function_node.args.args,
        *function_node.args.kwonlyargs,
    ):
        argument.annotation = None
    if function_node.args.vararg is not None:
        function_node.args.vararg.annotation = None
    if function_node.args.kwarg is not None:
        function_node.args.kwarg.annotation = None


def _rewrite_function_node_for_auto_parallel(
    function_node,
    hir: FunctionHIR,
    *,
    range_name: str = "prange",
):
    rewritten_fn = copy.deepcopy(function_node)
    rewritten_fn.decorator_list = []
    _clear_function_annotations(rewritten_fn)

    eligible_locations = {
        (loop.source_location.line, loop.source_location.column)
        for loop in hir.eligible_loops
        if loop.range_spec is not None and loop.range_spec.step_value == 1
    }
    transformer = _AutoParallelRangeTransformer(
        eligible_locations,
        range_name=range_name,
    )
    rewritten_fn = transformer.visit(rewritten_fn)
    if not transformer.rewrote:
        return None

    ast.fix_missing_locations(rewritten_fn)
    return rewritten_fn


def _parallel_policy_from_decorator(node) -> str:
    if not isinstance(node, ast.Call):
        return "auto"
    for keyword in node.keywords:
        if keyword.arg == "parallel_policy" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return "auto"


def auto_parallel_declarations(source: str) -> tuple[tuple[str, str], ...]:
    """Return ``(function_name, policy)`` declarations without executing code."""

    try:
        module_ast = ast.parse(source)
    except SyntaxError:
        return ()
    declarations: list[tuple[str, str]] = []
    for node in module_ast.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decorator = next(
            (item for item in node.decorator_list if _decorator_requests_auto_parallel(item)),
            None,
        )
        if decorator is not None:
            declarations.append((node.name, _parallel_policy_from_decorator(decorator)))
    return tuple(declarations)


def _hir_diagnostic(hir: FunctionHIR) -> str:
    numba_eligible = tuple(
        loop
        for loop in hir.eligible_loops
        if loop.range_spec is not None and loop.range_spec.step_value == 1
    )
    if numba_eligible:
        return "; ".join(loop.reason for loop in numba_eligible)
    if hir.eligible_loops:
        return "rejected: the current Numba prange backend requires a constant step size of 1"
    diagnostics = [item.format() for item in hir.diagnostics if item.is_error]
    if diagnostics:
        return diagnostics[0]
    loop_reasons = [loop.reason for loop in hir.loops]
    return loop_reasons[0] if loop_reasons else "function has no top-level affine loop"


def build_auto_parallel_embedded_source(source: str) -> str | None:
    """Embed verified parallel clones in the original module source.

    Player builds compile this transformed module into the normal script
    bytecode. No user-visible ``.autop.py[c]`` sidecar is emitted.
    """
    try:
        module_ast = ast.parse(source)
    except SyntaxError:
        return None

    rewritten_body = []
    changed = False
    manifest: dict[str, dict[str, object]] = {}
    for node in module_ast.body:
        if isinstance(node, ast.FunctionDef):
            decorator = next(
                (
                    item
                    for item in node.decorator_list
                    if _decorator_requests_auto_parallel(item)
                ),
                None,
            )
            if decorator is not None:
                hir = analyze_source(source, node.name)
                rewritten_fn = _rewrite_function_node_for_auto_parallel(
                    node,
                    hir,
                    range_name="__infernux_prange",
                )
                policy = _parallel_policy_from_decorator(decorator)
                if rewritten_fn is None:
                    if policy == "required":
                        raise ValueError(
                            f"{node.name}: parallel_policy='required' rejected: {_hir_diagnostic(hir)}"
                        )
                else:
                    suffix = hir.eligible_loops[0].stable_id.rsplit(":", 1)[-1]
                    rewritten_fn.name = f"__infernux_parallel_{node.name}_{suffix}"
                    fingerprint = hir_fingerprint(hir)
                    rewritten_body.append(rewritten_fn)
                    assert isinstance(decorator, ast.Call)
                    decorator.keywords.append(
                        ast.keyword(
                            arg="_parallel_impl",
                            value=ast.Name(id=rewritten_fn.name, ctx=ast.Load()),
                        )
                    )
                    decorator.keywords.append(
                        ast.keyword(
                            arg="_parallel_fingerprint",
                            value=ast.Constant(fingerprint),
                        )
                    )
                    decorator.keywords.append(
                        ast.keyword(
                            arg="_parallel_static_cost",
                            value=ast.Constant(hir.operation_cost),
                        )
                    )
                    manifest[node.name] = {
                        "compiler_revision": 1,
                        "hir_fingerprint": fingerprint,
                        "parallel_impl": rewritten_fn.name,
                        "policy": policy,
                        "loop_ids": [loop.stable_id for loop in hir.eligible_loops],
                        "diagnostic": _hir_diagnostic(hir),
                        "operation_cost": hir.operation_cost,
                    }
                    changed = True
        rewritten_body.append(node)

    if not changed:
        return None

    import_node = ast.ImportFrom(
        module="Infernux.jit",
        names=[ast.alias(name="prange", asname="__infernux_prange")],
        level=0,
    )
    insert_at = 0
    if (
        rewritten_body
        and isinstance(rewritten_body[0], ast.Expr)
        and isinstance(rewritten_body[0].value, ast.Constant)
        and isinstance(rewritten_body[0].value.value, str)
    ):
        insert_at = 1
    while (
        insert_at < len(rewritten_body)
        and isinstance(rewritten_body[insert_at], ast.ImportFrom)
        and rewritten_body[insert_at].module == "__future__"
    ):
        insert_at += 1
    rewritten_body.insert(insert_at, import_node)
    manifest_assignment = ast.Assign(
        targets=[ast.Name(id="__infernux_jit_manifest__", ctx=ast.Store())],
        value=ast.parse(repr(manifest), mode="eval").body,
    )
    rewritten_body.insert(insert_at + 1, manifest_assignment)
    module_ast.body = rewritten_body
    ast.fix_missing_locations(module_ast)
    return ast.unparse(module_ast)


def _try_build_auto_parallel_variant(fn, parallel_impl=None):
    """Return a HIR-verified prange clone, or ``None`` when not provable."""
    if callable(parallel_impl):
        parallel_impl.__defaults__ = getattr(fn, "__defaults__", None)
        parallel_impl.__kwdefaults__ = getattr(fn, "__kwdefaults__", None)
        fn._infernux_parallel_diagnostic = "eligible: embedded build-time Typed HIR implementation"
        return parallel_impl

    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return None

    try:
        module_ast = ast.parse(textwrap.dedent(source))
    except SyntaxError as exc:
        fn._infernux_parallel_diagnostic = f"rejected: source parse failed ({exc})"
        return None

    function_node = None
    for node in module_ast.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_node = node
            break
    if function_node is None or not isinstance(function_node, ast.FunctionDef):
        fn._infernux_parallel_diagnostic = "rejected: no synchronous function definition"
        return None

    hir = analyze_source(source, function_node.name)
    fn._infernux_parallel_operation_cost = hir.operation_cost
    fn._infernux_parallel_diagnostic = _hir_diagnostic(hir)
    rewritten_fn = _rewrite_function_node_for_auto_parallel(function_node, hir)
    if rewritten_fn is None:
        return None

    rewritten = ast.Module(body=[rewritten_fn], type_ignores=[])
    ast.fix_missing_locations(rewritten)

    namespace = dict(fn.__globals__)
    namespace["prange"] = prange

    try:
        closure_vars = inspect.getclosurevars(fn)
    except TypeError:
        closure_vars = None
    if closure_vars is not None:
        namespace.update(closure_vars.globals)
        namespace.update(closure_vars.nonlocals)
        namespace.update(closure_vars.builtins)

    compiled_code = compile(
        rewritten,
        inspect.getsourcefile(fn) or inspect.getfile(fn) or "<auto_parallel>",
        "exec",
    )
    exec(compiled_code, namespace)
    rewritten_fn = namespace.get(fn.__name__)
    if not callable(rewritten_fn):
        return None

    rewritten_fn.__defaults__ = getattr(fn, "__defaults__", None)
    rewritten_fn.__kwdefaults__ = getattr(fn, "__kwdefaults__", None)
    rewritten_fn.__dict__.update(getattr(fn, "__dict__", {}))
    rewritten_fn._infernux_hir = hir
    rewritten_fn._infernux_parallel_diagnostic = _hir_diagnostic(hir)
    return rewritten_fn


def _benchmark_callable(fn, *args, **kwargs) -> float:
    started = perf_counter()
    fn(*args, **kwargs)
    return perf_counter() - started


def _numba_thread_count() -> int:
    try:
        from numba import get_num_threads  # type: ignore[import-untyped]

        return max(1, int(get_num_threads()))
    except Exception:
        return max(1, int(os.cpu_count() or 1))


def _build_auto_parallel_dispatcher(
    fn,
    serial_compiled,
    parallel_compiled,
    *,
    parallel_policy: str = "auto",
    diagnostic: str = "",
    operation_cost: int = 1,
):
    """Build a signature-aware dispatcher without speculative re-execution.

    Unknown signatures run serially. ``warmup`` validates and benchmarks a
    single runtime signature using isolated argument copies, then stores a
    bounded decision for that dtype/rank/layout/shape/thread-count bucket.
    A selected parallel call is executed exactly once and user exceptions are
    never caught and replayed through the serial kernel.
    """
    decisions = BoundedLRU(64)

    def _signature(args, kwargs):
        return runtime_signature(args, kwargs, thread_count=_numba_thread_count())

    def _static(args, kwargs):
        return static_cost_decision(
            args,
            kwargs,
            operation_cost=operation_cost,
            thread_count=_numba_thread_count(),
        )

    def _record(key, decision: DispatchDecision) -> None:
        decisions[key] = decision
        dispatcher.selected_mode = decision.mode
        dispatcher.last_diagnostic = decision.reason

    @functools.wraps(fn)
    def dispatcher(*args, **kwargs):
        key = _signature(args, kwargs)
        decision = decisions.get(key)
        if decision is None:
            if parallel_policy == "required":
                decision = DispatchDecision("parallel", "parallel required by policy")
            else:
                static = _static(args, kwargs)
                decision = DispatchDecision(static.mode, static.reason)
                if static.confidence == "high":
                    decisions[key] = decision
        dispatcher.selected_mode = decision.mode
        dispatcher.last_diagnostic = decision.reason
        target = parallel_compiled if decision.mode == "parallel" else serial_compiled
        return target(*args, **kwargs)

    def _warmup(*args, **kwargs):
        key = _signature(args, kwargs)
        if parallel_compiled is serial_compiled:
            reason = diagnostic or "serial retained: no validated parallel variant is available"
            _record(key, DispatchDecision("serial", reason))
            _log_jit(f"[JIT] {fn.__name__}: {reason}")
            return
        if parallel_policy != "required":
            static = _static(args, kwargs)
            if static.confidence == "high":
                _record(key, DispatchDecision(static.mode, static.reason))
                _log_jit(f"[JIT] {fn.__name__}: {static.reason}")
                return
        try:
            serial_args, serial_kwargs = clone_call_arguments(args, kwargs)
            parallel_args, parallel_kwargs = clone_call_arguments(args, kwargs)
        except TypeError as exc:
            reason = f"serial retained: inputs cannot be isolated for validation ({exc})"
            _record(key, DispatchDecision("serial", reason))
            _log_jit(f"[JIT] {fn.__name__}: {reason}")
            return

        _log_jit(f"[JIT] warmup {fn.__name__}: compiling serial")
        serial_result = serial_compiled(*serial_args, **serial_kwargs)

        _log_jit(f"[JIT] warmup {fn.__name__}: compiling parallel")
        try:
            parallel_result = parallel_compiled(*parallel_args, **parallel_kwargs)
        except Exception as exc:
            if parallel_policy == "required":
                raise RuntimeError(
                    f"auto_parallel required kernel {fn.__name__!r} failed during validation"
                ) from exc
            reason = f"serial retained: parallel validation failed ({type(exc).__name__}: {exc})"
            _record(key, DispatchDecision("serial", reason))
            _log_jit(f"[JIT] {fn.__name__}: {reason}")
            return

        if not calls_equivalent(
            serial_result,
            serial_args,
            serial_kwargs,
            parallel_result,
            parallel_args,
            parallel_kwargs,
        ):
            reason = "serial retained: serial and parallel results or mutations differ"
            if parallel_policy == "required":
                raise RuntimeError(f"auto_parallel required kernel {fn.__name__!r}: {reason}")
            _record(key, DispatchDecision("serial", reason))
            _log_jit(f"[JIT] {fn.__name__}: {reason}")
            return

        serial_samples: list[float] = []
        parallel_samples: list[float] = []
        try:
            sample_target = 5
            while len(serial_samples) < sample_target:
                sample_args, sample_kwargs = clone_call_arguments(args, kwargs)
                serial_samples.append(
                    _benchmark_callable(serial_compiled, *sample_args, **sample_kwargs)
                )
                sample_args, sample_kwargs = clone_call_arguments(args, kwargs)
                parallel_samples.append(
                    _benchmark_callable(parallel_compiled, *sample_args, **sample_kwargs)
                )
                # Long kernels already provide a strong signal per sample.
                # Keep a median, but avoid turning a 1.5 s serial baseline
                # into a 30 s editor warmup through excessive cloning/runs.
                if len(serial_samples) == 1 and max(
                    serial_samples[0], parallel_samples[0]
                ) >= 0.050:
                    sample_target = 3
        except Exception as exc:
            if parallel_policy == "required":
                raise
            reason = f"serial retained: benchmark failed ({type(exc).__name__}: {exc})"
            _record(key, DispatchDecision("serial", reason))
            _log_jit(f"[JIT] {fn.__name__}: {reason}")
            return

        serial_elapsed = median(serial_samples)
        parallel_elapsed = median(parallel_samples)
        choose_parallel = parallel_policy == "required" or parallel_elapsed <= serial_elapsed * 0.90
        mode = "parallel" if choose_parallel else "serial"
        reason = (
            f"{mode} selected for runtime signature; median serial={serial_elapsed * 1000:.3f}ms, "
            f"parallel={parallel_elapsed * 1000:.3f}ms, samples={sample_target}"
        )
        _record(
            key,
            DispatchDecision(
                mode,
                reason,
                serial_elapsed,
                parallel_elapsed,
                sample_target,
            ),
        )
        _log_jit(
            f"[JIT] warmup {fn.__name__}: {reason}"
        )

    dispatcher.py = fn
    dispatcher.serial = serial_compiled
    dispatcher.parallel = parallel_compiled
    dispatcher.auto_parallel = True
    dispatcher.parallel_policy = parallel_policy
    dispatcher.static_operation_cost = operation_cost
    dispatcher.selected_mode = "parallel" if parallel_policy == "required" else "serial"
    dispatcher.last_diagnostic = diagnostic or "runtime signature has not been validated"
    dispatcher.decisions = decisions
    dispatcher._infernux_warmup = _warmup
    return dispatcher


# ── njit wrapper ──────────────────────────────────────────────────────

def njit(*args, **kwargs):
    """``numba.njit`` wrapper — safe for both editor and standalone builds.

    The returned callable always has a ``.py`` attribute pointing to the
    original pure-Python function, so callers can force the fallback::

        @njit(cache=True, fastmath=True)
        def burn(n: int) -> float: ...

        burn(100)       # JIT-accelerated (or fallback if no Numba)
        burn.py(100)    # always pure Python
    """
    auto_parallel = bool(kwargs.pop("auto_parallel", False))
    parallel_policy = str(kwargs.pop("parallel_policy", "auto"))
    parallel_impl = kwargs.pop("_parallel_impl", None)
    parallel_fingerprint = str(kwargs.pop("_parallel_fingerprint", ""))
    parallel_static_cost = max(1, int(kwargs.pop("_parallel_static_cost", 1)))
    if parallel_policy not in {"auto", "required"}:
        raise ValueError("parallel_policy must be 'auto' or 'required'")

    if not _HAS_NUMBA:
        # No-op fallback — attach .py for uniform API
        def _attach_fallback(fn):
            fn.auto_parallel = auto_parallel
            fn.parallel_policy = parallel_policy
            fn.compiler_fingerprint = parallel_fingerprint
            fn.static_operation_cost = parallel_static_cost
            fn.selected_mode = "serial"
            fn.last_diagnostic = "serial selected: JIT runtime is unavailable in this build"
            fn.serial = fn
            fn.parallel = fn
            fn.decisions = BoundedLRU(1)
            fn.py = fn
            return fn

        def _wrap(fn):
            if auto_parallel and parallel_policy == "required":
                raise RuntimeError("parallel_policy='required' needs the Numba JIT runtime")
            return _attach_fallback(fn)
        if args and callable(args[0]):
            if auto_parallel and parallel_policy == "required":
                raise RuntimeError("parallel_policy='required' needs the Numba JIT runtime")
            return _attach_fallback(args[0])
        return _wrap

    if _NUITKA_COMPILED:
        kwargs.pop("cache", None)

    if auto_parallel:
        serial_kwargs = dict(kwargs)
        serial_kwargs.pop("parallel", None)

        parallel_kwargs = dict(kwargs)
        parallel_kwargs["parallel"] = True

        def _compile_auto_parallel(fn):
            cache_key = _njit_cache_key(
                fn,
                f"auto_parallel:{parallel_policy}:{parallel_fingerprint}:{sorted(serial_kwargs.items())}",
            )
            if cache_key and cache_key in _compiled_cache:
                _log_jit(f"[JIT] {fn.__name__}: reusing cached auto_parallel compilation")
                cached = _compiled_cache[cache_key]
                cached.py = fn
                return cached
            _log_jit(f"[JIT] compiling auto_parallel: {fn.__name__}")
            serial_compiled = _compile_njit(fn, serial_kwargs)
            parallel_source_fn = _try_build_auto_parallel_variant(fn, parallel_impl)
            operation_cost = max(
                parallel_static_cost,
                int(getattr(fn, "_infernux_parallel_operation_cost", 1)),
            )
            diagnostic = getattr(
                fn,
                "_infernux_parallel_diagnostic",
                "rejected: source is unavailable and no embedded HIR implementation exists",
            )
            if parallel_source_fn is None:
                if parallel_policy == "required":
                    raise ValueError(
                        f"{fn.__name__}: parallel_policy='required' rejected: {diagnostic}"
                    )
                _log_jit(
                    f"[JIT] {fn.__name__}: serial retained — {diagnostic}"
                )
                parallel_compiled = serial_compiled
            else:
                _log_jit(f"[JIT] {fn.__name__}: Typed HIR lowering accepted")
                parallel_compiled = _compile_njit(parallel_source_fn, parallel_kwargs)
            _log_jit(f"[JIT] {fn.__name__}: auto_parallel compilation done")
            result = _build_auto_parallel_dispatcher(
                fn,
                serial_compiled,
                parallel_compiled,
                parallel_policy=parallel_policy,
                diagnostic=diagnostic,
                operation_cost=operation_cost,
            )
            result.compiler_fingerprint = parallel_fingerprint or compiler_fingerprint(
                fn,
                {"parallel_policy": parallel_policy, **serial_kwargs},
            )
            if cache_key:
                _compiled_cache[cache_key] = result
            return result

        if args and callable(args[0]):
            return _compile_auto_parallel(args[0])

        return _compile_auto_parallel

    # @njit  (bare decorator, no parentheses)
    if args and callable(args[0]):
        return _compile_njit_cached(args[0], {})

    # @njit(cache=True, ...)  (decorator factory)
    def _decorator(fn):
        return _compile_njit_cached(fn, kwargs)
    return _decorator


# ── warmup helper ─────────────────────────────────────────────────────

def warmup(fn, *args, **kwargs):
    """Pre-compile a ``@njit`` function by calling it once.

    No-op when Numba is unavailable. Auto-parallel validation uses isolated
    arguments; ``parallel_policy='required'`` propagates validation failures.

    Usage::

        @njit(cache=True, fastmath=True)
        def burn(n: int) -> float: ...

        warmup(burn, 1)
    """
    custom_warmup = getattr(fn, "_infernux_warmup", None)
    if callable(custom_warmup):
        try:
            custom_warmup(*args, **kwargs)
        except Exception as exc:
            if getattr(fn, "parallel_policy", "auto") == "required":
                raise
            _log_jit(f"[JIT] warmup {getattr(fn, '__name__', '<kernel>')} failed: {exc}")
        return

    if not _HAS_NUMBA or _NUITKA_COMPILED:
        return
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        _log_jit(f"[JIT] warmup {getattr(fn, '__name__', '<kernel>')} failed: {exc}")


__all__ = [
    "njit",
    "warmup",
    "JIT_AVAILABLE",
]
