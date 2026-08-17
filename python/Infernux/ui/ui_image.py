"""UIImage — a rectangular image UI element.

Hierarchy:
    InxComponent → InxUIComponent → InxUIScreenComponent → UIImage
"""

from Infernux.components import serialized_field, add_component_menu
from Infernux.components.fields import FieldType
from .inx_ui_screen_component import InxUIScreenComponent


@add_component_menu("UI/Image")
class UIImage(InxUIScreenComponent):
    """Screen-space image element rendered from a texture asset.

    Inherits x, y, width, height, opacity, corner_radius, rotation,
    mirror_x, mirror_y from InxUIScreenComponent.
    """

    # ── Fill ──
    texture_path: str = serialized_field(
        default="", tooltip="Path to texture asset (drag from Project panel)",
        group="Fill",
    )
    color: list = serialized_field(
        default=[1.0, 1.0, 1.0, 1.0],
        field_type=FieldType.COLOR,
        hdr=True,
        tooltip="Tint color (RGBA)",
        group="Fill",
    )
