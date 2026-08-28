from __future__ import annotations

import pytest

from Infernux.engine.ui.curve_editor import _evaluate_keys, _view_bounds


def test_curve_editor_uses_cubic_hermite_tangents():
    keys = [
        {"time": 0.0, "value": 0.0, "in_tangent": 0.0, "out_tangent": 2.0},
        {"time": 1.0, "value": 1.0, "in_tangent": 0.0, "out_tangent": 0.0},
    ]

    assert _evaluate_keys(keys, 0.0) == pytest.approx(0.0)
    assert _evaluate_keys(keys, 0.5) == pytest.approx(0.75)
    assert _evaluate_keys(keys, 1.0) == pytest.approx(1.0)


def test_width_curve_editor_view_never_exposes_negative_value_space():
    keys = [
        {"time": 0.0, "value": 0.1, "in_tangent": 0.0, "out_tangent": 0.0},
        {"time": 1.0, "value": 0.2, "in_tangent": 0.0, "out_tangent": 0.0},
    ]

    _, _, value_min, value_max = _view_bounds(keys, non_negative=True)

    assert value_min == 0.0
    assert value_max > 0.2
