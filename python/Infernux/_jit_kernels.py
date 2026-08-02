"""
Internal JIT bootstrap — DO NOT import directly.

Use the public API instead::

    from Infernux.jit import njit, warmup, JIT_AVAILABLE
"""

from __future__ import annotations

import ast
import copy
import functools
import importlib.util
import inspect
import os
import sys as _sys
import textwrap
from Infernux.engine.path_utils import path_key
import types
from time import perf_counter

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
_compiled_cache: dict = {}

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
    code = getattr(fn, "__code__", None)
    if code is None:
        return None
    import hashlib
    code_hash = hashlib.sha256(code.co_code).hexdigest()[:16]
    return (code.co_filename, fn.__name__, code_hash, kwargs_tag)


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


def _walk_loop_body(body):
    """Yield AST nodes from *body* without descending into nested function defs."""
    worklist = list(body)
    while worklist:
        node = worklist.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            worklist.extend(ast.iter_child_nodes(node))


def _body_has_supported_reduction(body) -> bool:
    """True when *body* contains ``acc op= expr`` with a Numba-supported operator.

    Supported: ``+=``, ``-=``, ``*=``, ``/=``  (but NOT ``//=``).
    """
    for node in _walk_loop_body(body):
        if (isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div))):
            return True
    return False


def _body_has_parallel_indexed_store(body, loop_var: str) -> bool:
    """True when *body* writes to ``arr[loop_var]`` — embarrassingly parallel."""
    for node in _walk_loop_body(body):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            idx = target.slice
            if isinstance(idx, ast.Name) and idx.id == loop_var:
                return True
            if isinstance(idx, ast.Tuple):
                for elt in idx.elts:
                    if isinstance(elt, ast.Name) and elt.id == loop_var:
                        return True
    return False


def _body_has_unsupported_control(body) -> bool:
    """True when *body* contains control flow unsupported in Numba prange.

    ``continue`` is intentionally allowed — Numba handles it correctly.
    """
    for node in _walk_loop_body(body):
        if isinstance(node, (ast.Return, ast.Break, ast.Yield, ast.YieldFrom, ast.Await, ast.Try)):
            return True
    return False


class _AutoParallelRangeTransformer(ast.NodeTransformer):
    def __init__(self):
        self.rewrote = False

    def visit_For(self, node):
        self.generic_visit(node)
        if not _is_range_call(node.iter):
            return node
        if _body_has_unsupported_control(node.body):
            return node

        loop_var = node.target.id if isinstance(node.target, ast.Name) else None
        has_reduction = _body_has_supported_reduction(node.body)
        has_indexed_store = loop_var and _body_has_parallel_indexed_store(
            node.body, loop_var
        )
        if not has_reduction and not has_indexed_store:
            return node

        node.iter.func.id = "prange"
        self.rewrote = True
        return node


def _rewrite_function_node_for_auto_parallel(function_node):
    rewritten_fn = copy.deepcopy(function_node)
    rewritten_fn.decorator_list = []

    transformer = _AutoParallelRangeTransformer()
    rewritten_fn = transformer.visit(rewritten_fn)
    if not transformer.rewrote:
        return None

    ast.fix_missing_locations(rewritten_fn)
    return rewritten_fn


def build_auto_parallel_sidecar_source(source: str) -> str | None:
    """Return module source containing rewritten auto-parallel functions only."""
    try:
        module_ast = ast.parse(textwrap.dedent(source))
    except SyntaxError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None

    rewritten_body = []
    for node in module_ast.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(_decorator_requests_auto_parallel(deco) for deco in node.decorator_list):
            continue
        rewritten_fn = _rewrite_function_node_for_auto_parallel(node)
        if rewritten_fn is not None:
            rewritten_body.append(rewritten_fn)

    if not rewritten_body:
        return None

    sidecar_ast = ast.Module(body=rewritten_body, type_ignores=[])
    ast.fix_missing_locations(sidecar_ast)
    return ast.unparse(sidecar_ast)


def _auto_parallel_sidecar_candidates(fn):
    seen = set()

    # Gather every plausible path the function could be associated with.
    # In packaged builds the module may not yet be in sys.modules during
    # decorator execution, so inspect.getmodule may return None.  We also
    # look up sys.modules[fn.__module__] directly as a fallback.
    module = inspect.getmodule(fn)
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        alt_mod = _sys.modules.get(getattr(fn, "__module__", ""))
        module_file = getattr(alt_mod, "__file__", None)

    raw_paths = [
        module_file,
        inspect.getsourcefile(fn),
    ]
    try:
        raw_paths.append(inspect.getfile(fn))
    except (TypeError, OSError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass
    raw_paths.append(
        getattr(getattr(fn, "__code__", None), "co_filename", None)
    )

    for path in raw_paths:
        if not path:
            continue
        norm = path_key(path)
        # Strip .pyc/.py so the same base path doesn't yield duplicate sidecars
        if norm.endswith(".pyc"):
            base_norm = norm[:-4]
        elif norm.endswith(".py"):
            base_norm = norm[:-3]
        else:
            continue
        if base_norm in seen:
            continue
        seen.add(base_norm)
        base_path = path[:-4] if norm.endswith(".pyc") else path[:-3]
        yield base_path + ".autop.pyc"
        yield base_path + ".autop.py"


def _load_prebuilt_auto_parallel_variant(fn):
    candidates = list(_auto_parallel_sidecar_candidates(fn))
    _log_jit(f"[JIT] auto_parallel sidecar candidates for {fn.__name__}: {candidates}")
    for sidecar_path in candidates:
        if not os.path.isfile(sidecar_path):
            continue

        _log_jit(f"[JIT] loading sidecar: {sidecar_path}")
        try:
            spec = importlib.util.spec_from_file_location(
                f"{fn.__module__}.__infernux_auto_parallel__.{fn.__name__}",
                sidecar_path,
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as _sidecar_exc:
            _log_jit(f"[JIT] sidecar load FAILED: {type(_sidecar_exc).__name__}: {_sidecar_exc}")
            continue

        sidecar_fn = getattr(module, fn.__name__, None)
        if not callable(sidecar_fn):
            continue

        globals_ns = dict(fn.__globals__)
        globals_ns["prange"] = prange
        rebound = types.FunctionType(
            sidecar_fn.__code__,
            globals_ns,
            fn.__name__,
            getattr(fn, "__defaults__", None),
            sidecar_fn.__closure__,
        )
        rebound.__kwdefaults__ = getattr(fn, "__kwdefaults__", None)
        rebound.__annotations__ = dict(getattr(fn, "__annotations__", {}))
        rebound.__dict__.update(getattr(fn, "__dict__", {}))
        rebound.__module__ = fn.__module__
        return rebound

    return None


def _try_build_auto_parallel_variant(fn):
    """Return a prange-rewritten clone of *fn* when safe enough.

    This intentionally handles only the simple case we care about for user
    scripts: ``for i in range(...): acc += expr``. If the source cannot be
    recovered or no loop matches the conservative pattern, return ``None``.
    """
    prebuilt = _load_prebuilt_auto_parallel_variant(fn)
    if prebuilt is not None:
        _log_jit(f"[JIT] {fn.__name__}: using prebuilt sidecar (prange)")
        return prebuilt

    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        _log_jit(
            f"[JIT] {fn.__name__}: no sidecar found and source "
            f"unavailable — parallel variant will NOT use prange. "
            f"Rebuild the project to generate .autop.pyc sidecars."
        )
        return None

    try:
        module_ast = ast.parse(textwrap.dedent(source))
    except SyntaxError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None

    function_node = None
    for node in module_ast.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_node = node
            break
    if function_node is None or not isinstance(function_node, ast.FunctionDef):
        return None

    rewritten_fn = _rewrite_function_node_for_auto_parallel(function_node)
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
    return rewritten_fn


def _benchmark_callable(fn, *args, **kwargs) -> float:
    started = perf_counter()
    fn(*args, **kwargs)
    return perf_counter() - started


def _build_auto_parallel_dispatcher(fn, serial_compiled, parallel_compiled):
    """Return a wrapper that prefers the parallel kernel but can self-heal.

    ``auto_parallel=True`` is intentionally conservative:

    - Both serial and ``parallel=True`` variants are compiled.
    - Calls default to the parallel variant.
    - ``warmup(...)`` benchmarks both variants once and pins the faster one.
    - If the parallel variant fails at runtime, execution falls back to the
      serial version automatically.
    """
    state = {"active": parallel_compiled, "mode": "parallel"}

    def _set_active(target, mode: str) -> None:
        state["active"] = target
        state["mode"] = mode
        dispatcher.selected_mode = mode

    @functools.wraps(fn)
    def dispatcher(*args, **kwargs):
        try:
            return state["active"](*args, **kwargs)
        except Exception:
            if state["mode"] == "parallel":
                _set_active(serial_compiled, "serial")
                return serial_compiled(*args, **kwargs)
            raise

    def _warmup(*args, **kwargs):
        # First pass pays compilation / thread-pool startup cost.
        _log_jit(f"[JIT] warmup {fn.__name__}: compiling serial…")
        serial_compiled(*args, **kwargs)

        _log_jit(f"[JIT] warmup {fn.__name__}: compiling parallel…")
        try:
            parallel_compiled(*args, **kwargs)
        except Exception as _exc:
            _log_jit(f"[JIT] warmup {fn.__name__}: parallel compilation FAILED ({_exc}), pinning serial")
            _set_active(serial_compiled, "serial")
            return

        # Second pass compares steady-state runtime instead of first-hit cost.
        serial_elapsed = _benchmark_callable(serial_compiled, *args, **kwargs)
        try:
            parallel_elapsed = _benchmark_callable(parallel_compiled, *args, **kwargs)
        except Exception:
            _log_jit(f"[JIT] warmup {fn.__name__}: parallel benchmark FAILED, pinning serial")
            _set_active(serial_compiled, "serial")
            return

        _log_jit(
            f"[JIT] warmup {fn.__name__}: serial={serial_elapsed*1000:.2f}ms  "
            f"parallel={parallel_elapsed*1000:.2f}ms  "
            f"→ {'parallel' if parallel_elapsed < serial_elapsed else 'serial'}"
        )
        if parallel_elapsed < serial_elapsed:
            _set_active(parallel_compiled, "parallel")
        else:
            _set_active(serial_compiled, "serial")

    dispatcher.py = fn
    dispatcher.serial = serial_compiled
    dispatcher.parallel = parallel_compiled
    dispatcher.auto_parallel = True
    dispatcher.selected_mode = "parallel"
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

    if not _HAS_NUMBA:
        # No-op fallback — attach .py for uniform API
        def _wrap(fn):
            fn.auto_parallel = auto_parallel
            fn.py = fn
            return fn
        if args and callable(args[0]):
            args[0].auto_parallel = auto_parallel
            args[0].py = args[0]
            return args[0]
        return _wrap

    if _NUITKA_COMPILED:
        kwargs.pop("cache", None)

    if auto_parallel:
        serial_kwargs = dict(kwargs)
        serial_kwargs.pop("parallel", None)

        parallel_kwargs = dict(kwargs)
        parallel_kwargs["parallel"] = True

        def _compile_auto_parallel(fn):
            cache_key = _njit_cache_key(fn, "auto_parallel")
            if cache_key and cache_key in _compiled_cache:
                _log_jit(f"[JIT] {fn.__name__}: reusing cached auto_parallel compilation")
                cached = _compiled_cache[cache_key]
                cached.py = fn
                return cached
            _log_jit(f"[JIT] compiling auto_parallel: {fn.__name__}")
            serial_compiled = _compile_njit(fn, serial_kwargs)
            parallel_source_fn = _try_build_auto_parallel_variant(fn)
            if parallel_source_fn is None:
                _log_jit(
                    f"[JIT] {fn.__name__}: prange rewrite unavailable "
                    f"— both serial and parallel variants use the original source."
                )
            else:
                _log_jit(f"[JIT] {fn.__name__}: prange rewrite OK")
            parallel_target = parallel_source_fn or fn
            parallel_compiled = _compile_njit(parallel_target, parallel_kwargs)
            _log_jit(f"[JIT] {fn.__name__}: auto_parallel compilation done")
            result = _build_auto_parallel_dispatcher(fn, serial_compiled, parallel_compiled)
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

    No-op when Numba is unavailable or inside a Nuitka standalone build.
    Exceptions during warmup are silently swallowed.

    Usage::

        @njit(cache=True, fastmath=True)
        def burn(n: int) -> float: ...

        warmup(burn, 1)
    """
    custom_warmup = getattr(fn, "_infernux_warmup", None)
    if callable(custom_warmup):
        try:
            custom_warmup(*args, **kwargs)
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass
        return

    if not _HAS_NUMBA or _NUITKA_COMPILED:
        return
    try:
        fn(*args, **kwargs)
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass


__all__ = [
    "njit",
    "warmup",
    "JIT_AVAILABLE",
]
