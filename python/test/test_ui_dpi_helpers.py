from __future__ import annotations

import pytest

from Infernux.engine.ui.dpi import editor_dpi_scale, scaled_editor_metric


class _DpiContext:
    def __init__(self, scale: float) -> None:
        self.scale = scale

    def get_dpi_scale(self) -> float:
        return self.scale


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0, 2.5])
def test_editor_metrics_use_the_native_display_scale(scale: float):
    context = _DpiContext(scale)

    assert editor_dpi_scale(context) == scale
    assert scaled_editor_metric(context, 24.0) == pytest.approx(24.0 * scale)


@pytest.mark.parametrize("scale", [0.0, -1.0, float("inf"), float("nan")])
def test_editor_dpi_rejects_invalid_native_scale(scale: float):
    with pytest.raises(RuntimeError, match="invalid display scale"):
        editor_dpi_scale(_DpiContext(scale))
