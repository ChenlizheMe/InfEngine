"""UIText — a text label UI element (Figma-style properties).

Hierarchy:
    InxComponent → InxUIComponent → InxUIScreenComponent → UIText
"""

from Infernux.components import serialized_field, add_component_menu
from Infernux.components.serialized_field import FieldType
from .inx_ui_screen_component import InxUIScreenComponent
from .enums import TextAlignH, TextAlignV, TextOverflow, TextResizeMode


@add_component_menu("UI/Text")
class UIText(InxUIScreenComponent):
    """Figma-style text label rendered with ImGui draw primitives.

    Inherits x, y, width, height from InxUIScreenComponent.
    All fields carry ``group`` metadata so the generic inspector renderer
    displays them in collapsible sections automatically.
    """

    # ── Content ──
    text: str = serialized_field(
        default="New Text", tooltip="Display text",
        group="Content", multiline=True,
    )

    # ── Typography ──
    font_path: str = serialized_field(
        default="", tooltip="Optional font asset path (.ttf/.otf)",
        group="Typography",
    )
    font_size: float = serialized_field(
        default=16.0, tooltip="Font size in canvas pixels",
        group="Typography", range=(4.0, 256.0), slider=False, drag_speed=0.5,
    )
    line_height: float = serialized_field(
        default=1.2, tooltip="Line height multiplier",
        group="Typography", range=(0.5, 5.0), slider=False, drag_speed=0.01,
    )
    letter_spacing: float = serialized_field(
        default=0.0, tooltip="Extra letter spacing in px",
        group="Typography", range=(-20.0, 100.0), slider=False, drag_speed=0.1,
    )

    # ── Alignment ──
    text_align_h: TextAlignH = serialized_field(
        default=TextAlignH.Left, tooltip="Horizontal alignment",
        group="Alignment",
    )
    text_align_v: TextAlignV = serialized_field(
        default=TextAlignV.Top, tooltip="Vertical alignment",
        group="Alignment",
    )

    # ── Overflow ──
    overflow: TextOverflow = serialized_field(
        default=TextOverflow.Visible, tooltip="Text overflow mode",
        group="Overflow",
    )

    resize_mode: TextResizeMode = serialized_field(
        default=TextResizeMode.FixedSize,
        tooltip="How the text clipping box resizes with content",
        group="Layout",
    )

    # ── Fill ──
    color: list = serialized_field(
        default=[1.0, 1.0, 1.0, 1.0],
        field_type=FieldType.COLOR,
        hdr=True,
        tooltip="Text color (RGBA)",
        group="Fill",
    )

    def is_auto_width(self) -> bool:
        return self.resize_mode == TextResizeMode.AutoWidth

    def is_auto_height(self) -> bool:
        return self.resize_mode == TextResizeMode.AutoHeight

    def is_fixed_size(self) -> bool:
        return self.resize_mode == TextResizeMode.FixedSize

    def get_wrap_width(self) -> float:
        return 0.0 if self.is_auto_width() else max(1.0, float(self.width))

    def get_layout_tolerance(self) -> float:
        return max(4.0, float(getattr(self, "font_size", 24.0)) * 0.15)

    def get_editor_wrap_width(self) -> float:
        wrap_width = self.get_wrap_width()
        if wrap_width <= 0.0:
            return 0.0
        return wrap_width + self.get_layout_tolerance()

    def get_auto_size_padding(self) -> tuple[float, float]:
        tolerance = self.get_layout_tolerance()
        return tolerance, max(2.0, tolerance * 0.5)

    def is_width_editable(self) -> bool:
        return not self.is_auto_width()

    def is_height_editable(self) -> bool:
        return not self.is_auto_height()
