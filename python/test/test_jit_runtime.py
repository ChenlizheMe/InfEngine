from __future__ import annotations

import numpy as np

from Infernux.jit_runtime import (
    BoundedLRU,
    calls_equivalent,
    clone_call_arguments,
    compiler_fingerprint,
    runtime_signature,
    static_cost_decision,
)


def test_bounded_lru_evicts_oldest_and_promotes_reads():
    cache = BoundedLRU[str, int](2)
    cache["a"] = 1
    cache["b"] = 2
    assert cache.get("a") == 1
    cache["c"] = 3
    assert "a" in cache
    assert "b" not in cache
    assert cache.get("c") == 3


def test_compiler_fingerprint_tracks_constants_defaults_and_dependencies():
    def helper(value):
        return value + 1

    def kernel(value=3):
        return helper(value) + 7

    before = compiler_fingerprint(kernel, {"fastmath": False})

    def replacement(value):
        return value + 2

    helper.__code__ = replacement.__code__
    after_dependency = compiler_fingerprint(kernel, {"fastmath": False})
    after_option = compiler_fingerprint(kernel, {"fastmath": True})

    assert before != after_dependency
    assert after_dependency != after_option


def test_compiler_fingerprint_tracks_cpu_feature_configuration(monkeypatch):
    def kernel(value):
        return value + 1

    monkeypatch.setenv("NUMBA_CPU_FEATURES", "+sse2")
    first = compiler_fingerprint(kernel)
    monkeypatch.setenv("NUMBA_CPU_FEATURES", "+avx2")
    second = compiler_fingerprint(kernel)

    assert first != second


def test_runtime_signature_separates_shape_dtype_layout_and_threads():
    small = np.zeros((16, 2), dtype=np.float32)
    large = np.zeros((100_000, 2), dtype=np.float32)
    f_order = np.asfortranarray(small)

    small_key = runtime_signature((small,), {}, thread_count=4)
    assert small_key != runtime_signature((large,), {}, thread_count=4)
    assert small_key != runtime_signature((small.astype(np.float64),), {}, thread_count=4)
    assert small_key != runtime_signature((f_order,), {}, thread_count=4)
    assert small_key != runtime_signature((small,), {}, thread_count=8)


def test_static_cost_model_prefers_ast_work_before_timing():
    small = np.zeros((32, 3), dtype=np.float32)
    large = np.zeros((10_000_000, 3), dtype=np.float32)

    small_decision = static_cost_decision(
        (small,), {}, operation_cost=12, thread_count=8
    )
    large_decision = static_cost_decision(
        (large,), {}, operation_cost=12, thread_count=8
    )

    assert (small_decision.mode, small_decision.confidence) == ("serial", "high")
    assert (large_decision.mode, large_decision.confidence) == ("parallel", "high")
    assert large_decision.work_units > small_decision.work_units


def test_static_cost_model_leaves_unknown_trip_count_for_measurement():
    decision = static_cost_decision(([0] * 16,), {}, operation_cost=12, thread_count=8)
    assert (decision.mode, decision.confidence) == ("serial", "gray")


def test_clone_and_equivalence_include_mutated_array_arguments():
    values = np.arange(8, dtype=np.float32)
    left_args, left_kwargs = clone_call_arguments((values,), {"scale": 2.0})
    right_args, right_kwargs = clone_call_arguments((values,), {"scale": 2.0})

    left_args[0][:] *= 2
    right_args[0][:] *= 2
    assert calls_equivalent(None, left_args, left_kwargs, None, right_args, right_kwargs)
    assert np.array_equal(values, np.arange(8, dtype=np.float32))

    right_args[0][3] = -1
    assert not calls_equivalent(None, left_args, left_kwargs, None, right_args, right_kwargs)


def test_clone_rejects_unknown_mutable_object():
    class Unknown:
        pass

    try:
        clone_call_arguments((Unknown(),), {})
    except TypeError as exc:
        assert "cannot isolate" in str(exc)
    else:
        raise AssertionError("unknown mutable object must not be benchmarked")
