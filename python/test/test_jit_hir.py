"""Corpus tests for the standalone Typed HIR legality front-end."""

from __future__ import annotations

import pytest

from Infernux.jit_hir import (
    AliasRiskKind,
    BasicBlockKind,
    BufferAccessKind,
    DiagnosticCode,
    EffectKind,
    HIRParseError,
    ValueType,
    analyze_function,
    build_hir,
    hir_fingerprint,
)


def _loop(source: str):
    hir = build_hir(source)
    assert len(hir.loops) == 1
    return hir, hir.loops[0]


def _codes(loop):
    return {diagnostic.code for diagnostic in loop.diagnostics}


class TestSafeCorpus:
    def test_direct_elementwise_write_and_pure_math(self):
        hir, loop = _loop(
            """
            import math

            def scale(out: array, values: array, n: int):
                for i in range(n):
                    out[i] = math.sqrt(values[i]) * 2.0
            """
        )

        assert hir.parallel_eligible
        assert loop.parallel_eligible
        assert loop.index_name == "i"
        assert loop.range_spec is not None and loop.range_spec.is_affine
        assert loop.buffer_writes[0].kind == BufferAccessKind.WRITE
        assert loop.buffer_writes[0].unique
        assert loop.buffer_reads[0].same_iteration
        assert any(effect.kind == EffectKind.PURE_CALL for effect in loop.effects)
        assert not loop.diagnostics

    def test_shape_query_is_a_valid_range_bound(self):
        _, loop = _loop(
            """
            def copy(out: array, values: array):
                for i in range(values.shape[0]):
                    out[i] = values[i]
            """
        )

        assert loop.parallel_eligible
        assert loop.range_spec is not None
        assert loop.range_spec.stop_affine is not None
        assert loop.range_spec.stop_affine.variables == ("values.shape[0]",)

    def test_len_and_numpy_scalar_allowlist(self):
        hir, loop = _loop(
            """
            import numpy as np

            def transform(out: array, values: array):
                for i in range(len(values)):
                    out[i] = np.maximum(np.sin(values[i]), 0.0)
            """
        )

        assert hir.parallel_eligible
        assert loop.parallel_eligible
        assert not _codes(loop)
        assert sum(effect.kind == EffectKind.PURE_CALL for effect in loop.effects) >= 2

    def test_strided_affine_range_and_unique_affine_write(self):
        _, loop = _loop(
            """
            def fill(out: array, n: int):
                for i in range(1, n, 2):
                    out[2 * i + 1] = 1.0
            """
        )

        assert loop.parallel_eligible
        assert loop.range_spec is not None and loop.range_spec.step_value == 2
        assert loop.buffer_writes[0].index is not None
        assert loop.buffer_writes[0].index.coefficient("i") == 2

    def test_scalar_sum_reduction(self):
        hir, loop = _loop(
            """
            def sum_values(values: array, n: int):
                total = 0.0
                for i in range(n):
                    total += values[i]
                return total
            """
        )

        assert hir.parallel_eligible
        assert loop.parallel_eligible
        assert len(loop.reductions) == 1
        assert loop.reductions[0].target == "total"
        assert loop.reductions[0].operator == "+"
        assert any(effect.kind == EffectKind.REDUCTION for effect in loop.effects)

    def test_elementwise_alias_risk_is_reported_without_rejecting_same_index_work(self):
        _, loop = _loop(
            """
            def copy(out: array, values: array, n: int):
                for i in range(n):
                    out[i] = values[i]
            """
        )

        assert loop.parallel_eligible
        assert loop.alias_risks[0].kind == AliasRiskKind.POSSIBLE
        assert "out" in loop.alias_risks[0].buffers

    def test_source_position_and_stable_id_are_transformer_facing(self):
        source = """
        def kernel(out: array, n: int):
            for i in range(n):
                out[i] = i
        """
        first = build_hir(source).loops[0]
        second = build_hir(source).loops[0]

        assert first.stable_id == second.stable_id
        assert first.source_location.line == 3
        assert first.source_location.end_line == 4
        assert "out[i] = i" in first.source

    def test_stable_id_survives_source_line_shift(self):
        first = build_hir(
            """
            def kernel(out: array, n: int):
                for i in range(n):
                    out[i] = i
            """
        ).loops[0]
        shifted = build_hir(
            """


            def kernel(out: array, n: int):
                for i in range(n):
                    out[i] = i
            """
        ).loops[0]

        assert first.stable_id == shifted.stable_id
        assert shifted.source_location.line == 5

    def test_function_object_entry_point_does_not_execute_function(self):
        calls = []

        def kernel(out: list, n: int):
            calls.append("must not run")
            for i in range(n):
                out[i] = i

        hir = analyze_function(kernel)
        assert hir.parallel_eligible
        assert calls == []

    def test_structured_cfg_contains_loop_back_edge_and_exit_edge(self):
        hir = build_hir(
            """
            def kernel(out: array, n: int):
                scale = 2
                for i in range(n):
                    out[i] = i * scale
                return out
            """
        )

        kinds = [block.kind for block in hir.blocks]
        assert kinds == [
            BasicBlockKind.ENTRY,
            BasicBlockKind.LINEAR,
            BasicBlockKind.LOOP_HEADER,
            BasicBlockKind.LOOP_BODY,
            BasicBlockKind.LINEAR,
            BasicBlockKind.EXIT,
        ]
        header = next(block for block in hir.blocks if block.kind == BasicBlockKind.LOOP_HEADER)
        body = next(block for block in hir.blocks if block.kind == BasicBlockKind.LOOP_BODY)
        tail = hir.blocks[-2]
        assert header.successors == (body.stable_id, tail.stable_id)
        assert body.successors == (header.stable_id,)
        assert tail.successors == (hir.exit_block.stable_id,)

    def test_hir_fingerprint_changes_with_semantics_not_whitespace(self):
        first = build_hir(
            """
            def kernel(out: array, n: int):
                for i in range(n):
                    out[i] = i * 2
            """
        )
        spaced = build_hir(
            """

            def kernel(out: array, n: int):
                for i in range(n):
                    out[i] = i * 2
            """
        )
        changed = build_hir(
            """
            def kernel(out: array, n: int):
                for i in range(n):
                    out[i] = i * 3
            """
        )

        assert hir_fingerprint(first) == hir_fingerprint(spaced)
        assert hir_fingerprint(first) != hir_fingerprint(changed)


class TestUnsafeCorpus:
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ("out[i] = out[i - 1] + 1", DiagnosticCode.LOOP_CARRIED_READ),
            ("out[index[i]] = values[i]", DiagnosticCode.INDIRECT_WRITE),
            ("out[i] = make_value(values[i])", DiagnosticCode.UNKNOWN_CALL),
            ("values.append(i)", DiagnosticCode.CONTAINER_MUTATION),
            ("break", DiagnosticCode.UNSUPPORTED_CONTROL_FLOW),
            ("return", DiagnosticCode.UNSUPPORTED_CONTROL_FLOW),
            ("yield i", DiagnosticCode.UNSUPPORTED_CONTROL_FLOW),
            ("await values[i]", DiagnosticCode.UNSUPPORTED_CONTROL_FLOW),
        ],
    )
    def test_unsafe_constructs_are_rejected_with_specific_diagnostic(self, body, code):
        source = f"""
        def kernel(out: array, values: array, index: array, n: int):
            for i in range(n):
                {body}
        """

        hir, loop = _loop(source)
        assert not loop.parallel_eligible
        assert code in _codes(loop)
        assert any(diagnostic.message for diagnostic in loop.diagnostics)

    def test_try_is_rejected_even_when_body_looks_pure(self):
        hir, loop = _loop(
            """
            def kernel(out: array, values: array, n: int):
                for i in range(n):
                    try:
                        out[i] = values[i]
                    except ValueError:
                        out[i] = 0.0
            """
        )

        assert not hir.parallel_eligible
        assert DiagnosticCode.UNSUPPORTED_CONTROL_FLOW in _codes(loop)

    def test_unknown_range_and_zero_step_are_rejected(self):
        _, dynamic = _loop(
            """
            def kernel(out: array, n: int):
                for i in range(start(), n):
                    out[i] = 1.0
            """
        )
        _, zero = _loop(
            """
            def kernel(out: array, n: int):
                for i in range(n, 0, 0):
                    out[i] = 1.0
            """
        )

        assert DiagnosticCode.UNSUPPORTED_RANGE in _codes(dynamic)
        assert DiagnosticCode.UNSUPPORTED_RANGE in _codes(zero)

    def test_non_affine_read_is_rejected_conservatively(self):
        _, loop = _loop(
            """
            def kernel(out: array, values: array, index: int, n: int):
                for i in range(n):
                    out[i] = values[index]
            """
        )

        assert not loop.parallel_eligible
        assert DiagnosticCode.NON_AFFINE_INDEX in _codes(loop)

    def test_offset_access_between_distinct_buffers_is_rejected_for_alias_safety(self):
        _, loop = _loop(
            """
            def kernel(out: array, values: array, n: int):
                for i in range(n):
                    out[i] = values[i - 1]
            """
        )

        assert not loop.parallel_eligible
        assert DiagnosticCode.ALIAS_RISK in _codes(loop)

    def test_uninitialized_scalar_update_is_not_a_reduction(self):
        _, loop = _loop(
            """
            def kernel(values: array, n: int):
                for i in range(n):
                    total += values[i]
            """
        )

        assert not loop.parallel_eligible
        assert DiagnosticCode.INVALID_REDUCTION in _codes(loop)

    @pytest.mark.parametrize("operator", ["-=", "/="])
    def test_non_associative_augmented_scalar_updates_are_not_reductions(self, operator):
        _, loop = _loop(
            f"""
            def kernel(values: array, n: int):
                total = 1.0
                for i in range(n):
                    total {operator} values[i]
            """
        )

        assert not loop.parallel_eligible
        assert DiagnosticCode.INVALID_REDUCTION in _codes(loop)

    def test_nested_loop_is_rejected(self):
        hir = build_hir(
            """
            def kernel(out: array, values: array, n: int, m: int):
                for i in range(n):
                    for j in range(m):
                        out[i] = values[j]
            """
        )
        loop = hir.loops[0]

        assert not loop.parallel_eligible
        assert DiagnosticCode.UNSUPPORTED_NESTED_LOOP in _codes(loop)

    def test_unknown_attribute_call_is_rejected(self):
        _, loop = _loop(
            """
            def kernel(out: array, values: array, n: int, service):
                for i in range(n):
                    out[i] = service.sample(values[i])
            """
        )

        assert not loop.parallel_eligible
        assert DiagnosticCode.UNKNOWN_CALL in _codes(loop)

    def test_normal_function_return_after_loop_does_not_block_candidate(self):
        hir, loop = _loop(
            """
            def kernel(out: array, n: int):
                for i in range(n):
                    out[i] = i
                return out
            """
        )

        assert loop.parallel_eligible
        assert hir.parallel_eligible

    def test_no_output_loop_is_not_a_candidate(self):
        _, loop = _loop(
            """
            import math

            def kernel(values: array, n: int):
                for i in range(n):
                    math.sin(values[i])
            """
        )

        assert not loop.parallel_eligible
        assert "no analyzable output" in loop.reason

    def test_parse_error_is_explicit(self):
        with pytest.raises(HIRParseError) as error:
            build_hir("def broken(:\n    pass\n")
        assert "parse_error" in str(error.value)
