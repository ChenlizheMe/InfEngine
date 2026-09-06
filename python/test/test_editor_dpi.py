from __future__ import annotations

import pytest

from Infernux.engine.nuitka_builder import NuitkaBuilder
from Infernux.engine.ui.curve_editor import render_curve_property
from Infernux.engine.ui.dpi import editor_dpi_scale
from Infernux.engine.ui.editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    render_editor_modal_actions,
)
from Infernux.engine.ui.editor_panel import EditorPanel
from Infernux.engine.ui.inspector_utils import (
    render_compact_section_header,
    render_inspector_checkbox,
)
from Infernux.engine.ui.theme import Theme
from Infernux.graph.ramp import AnimationCurve


class _ModalContext:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.window_size = None
        self.window_position = None
        self.buttons = []
        self.cursor_x = 0.0
        self.cursor_y = 0.0

    def get_dpi_scale(self) -> float:
        return self.scale

    def get_main_viewport_bounds(self):
        return (100.0, 50.0, 1600.0, 900.0)

    def set_next_window_pos(self, *args) -> None:
        self.window_position = args

    def set_next_window_size(self, *args) -> None:
        self.window_size = args

    def begin_popup_modal(self, _popup_id, _flags) -> bool:
        return True

    def record_semantic_window(self, *_args) -> None:
        pass

    def get_content_region_avail_height(self) -> float:
        return 180.0 * self.scale

    def get_cursor_pos_y(self) -> float:
        return self.cursor_y

    def set_cursor_pos_y(self, value: float) -> None:
        self.cursor_y = value

    def get_content_region_avail_width(self) -> float:
        return 520.0 * self.scale

    def get_cursor_pos_x(self) -> float:
        return self.cursor_x

    def set_cursor_pos_x(self, value: float) -> None:
        self.cursor_x = value

    def spacing(self) -> None:
        pass

    def separator(self) -> None:
        pass

    def same_line(self) -> None:
        pass

    def begin_disabled(self, _disabled: bool) -> None:
        pass

    def end_disabled(self) -> None:
        pass

    def button(self, label, callback, *, width, height) -> None:
        self.buttons.append((label, callback, width, height))

    def record_semantic_item(self, *_args) -> None:
        pass


class _CurvePreviewContext:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.buttons = []
        self.window_size = None
        self._rect = (0.0, 0.0, 0.0, 0.0)

    def get_dpi_scale(self) -> float:
        return self.scale

    def get_content_region_avail_width(self) -> float:
        return 100.0 * self.scale

    def invisible_button(self, widget_id, width, height) -> bool:
        self.buttons.append((widget_id, width, height))
        self._rect = (0.0, 0.0, width, height)
        return False

    def get_item_rect_min_x(self) -> float:
        return self._rect[0]

    def get_item_rect_min_y(self) -> float:
        return self._rect[1]

    def get_item_rect_max_x(self) -> float:
        return self._rect[2]

    def get_item_rect_max_y(self) -> float:
        return self._rect[3]

    def draw_filled_rect(self, *_args) -> None:
        pass

    def draw_line(self, *_args) -> None:
        pass

    def draw_filled_circle(self, *_args) -> None:
        pass

    def draw_rect(self, *_args) -> None:
        pass

    def is_item_hovered(self) -> bool:
        return False

    def record_semantic_rect(self, *_args) -> None:
        pass

    def set_next_window_size(self, *args) -> None:
        self.window_size = args

    def begin_popup(self, _popup_id) -> bool:
        return False


class _SizedPanel(EditorPanel):
    def __init__(self) -> None:
        super().__init__("DPI Panel", "dpi_panel")

    def _initial_size(self):
        return 800.0, 600.0


class _NativeInspectorContext:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.header_calls = []
        self.checkbox_calls = []

    def get_dpi_scale(self) -> float:
        return self.scale

    def render_compact_section_header(self, *args):
        self.header_calls.append(args)
        return True

    def checkbox_inspector(self, label: str, value: bool) -> bool:
        self.checkbox_calls.append((label, value))
        return not value


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0, 2.5])
def test_editor_modal_metrics_follow_per_monitor_scale(scale: float):
    ctx = _ModalContext(scale)

    assert begin_editor_modal(
        ctx,
        popup_id="##dpi_modal",
        title="DPI",
        semantic_id="dpi.modal",
    )
    assert ctx.window_size == (560.0 * scale, 220.0 * scale, Theme.COND_ALWAYS)
    assert ctx.window_position == (900.0, 500.0, Theme.COND_ALWAYS, 0.5, 0.5)

    render_editor_modal_actions(
        ctx,
        [
            EditorModalAction("Cancel", "dpi.cancel", lambda: None),
            EditorModalAction("Apply", "dpi.apply", lambda: None),
        ],
        semantic_prefix="dpi",
    )
    assert ctx.cursor_y == pytest.approx(122.0 * scale)
    assert ctx.cursor_x == pytest.approx(288.0 * scale)
    assert [(button[2], button[3]) for button in ctx.buttons] == [
        (112.0 * scale, 34.0 * scale),
        (112.0 * scale, 34.0 * scale),
    ]


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0, 2.5])
def test_curve_editor_preview_and_popup_follow_per_monitor_scale(scale: float):
    ctx = _CurvePreviewContext(scale)

    assert render_curve_property(
        ctx, "dpi_curve", AnimationCurve().to_dict()
    ) == AnimationCurve().to_dict()

    assert ctx.buttons == [
        ("##dpi_curve_curve_preview", 120.0 * scale, Theme.CURVE_EDITOR_PREVIEW_H * scale)
    ]
    assert ctx.window_size == (440.0 * scale, 440.0 * scale, Theme.COND_ALWAYS)


@pytest.mark.parametrize("scale", [0.0, -1.0, float("inf"), float("nan")])
def test_editor_dpi_rejects_invalid_native_scale(scale: float):
    with pytest.raises(RuntimeError, match="invalid display scale"):
        editor_dpi_scale(_ModalContext(scale))


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0, 2.5])
def test_editor_panel_initial_size_follows_per_monitor_scale(scale: float):
    assert _SizedPanel()._scaled_initial_size(_ModalContext(scale)) == (
        800.0 * scale,
        600.0 * scale,
    )


def test_windows_player_manifest_requires_per_monitor_v2_without_dpi_fallback():
    manifest = NuitkaBuilder._UTF8_MANIFEST.decode("utf-8")

    assert ">PerMonitorV2</dpiAwareness>" in manifest
    assert "PerMonitorV2," not in manifest
    assert "<dpiAware xmlns=" not in manifest


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0, 2.5])
def test_inspector_native_header_metrics_follow_per_monitor_scale(scale: float):
    ctx = _NativeInspectorContext(scale)

    assert render_compact_section_header(
        ctx,
        "Rendering",
        icon_id=17,
        level="secondary",
        allow_overlap=True,
    )

    call = ctx.header_calls[0]
    assert call[5] == pytest.approx(Theme.INSPECTOR_HEADER_SECONDARY_FRAME_PAD[0] * scale)
    assert call[6] == pytest.approx(Theme.INSPECTOR_HEADER_SECONDARY_FRAME_PAD[1] * scale)
    assert call[7] == pytest.approx(Theme.INSPECTOR_HEADER_ITEM_SPC[0] * scale)
    assert call[8] == pytest.approx(Theme.INSPECTOR_HEADER_ITEM_SPC[1] * scale)
    assert call[9] == pytest.approx(Theme.INSPECTOR_HEADER_BORDER_SIZE * scale)
    assert call[12] == pytest.approx(Theme.INSPECTOR_HEADER_RIGHT_MARGIN * scale)
    assert call[13] == pytest.approx(Theme.COMPONENT_ICON_SIZE * scale)


def test_inspector_widgets_require_current_native_bindings():
    with pytest.raises(RuntimeError, match="native checkbox_inspector"):
        render_inspector_checkbox(object(), "Enabled", True)

    with pytest.raises(RuntimeError, match="native render_compact_section_header"):
        render_compact_section_header(_ModalContext(1.0), "Rendering")


def test_inspector_checkbox_uses_native_contract():
    ctx = _NativeInspectorContext(2.0)

    assert render_inspector_checkbox(ctx, "Enabled", False)
    assert ctx.checkbox_calls == [("Enabled", False)]
