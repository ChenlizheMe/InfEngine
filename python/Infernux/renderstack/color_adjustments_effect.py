"""
ColorAdjustmentsEffect — Post-exposure, contrast, saturation, hue shift.

Aligned with Unity URP Color Adjustments. Operates in HDR space
(before_post_process) so adjustments apply before tone mapping.

Parameters:
    post_exposure — exposure offset in EV stops
    contrast      — contrast adjustment (-100 to 100)
    saturation    — saturation adjustment (-100 to 100)
    hue_shift     — hue rotation in degrees (-180 to 180)
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.fields import serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class ColorAdjustmentsEffect(FullScreenEffect):
    """URP-aligned Color Adjustments post-processing effect."""

    name = "Color Adjustments"
    injection_point = "before_post_process"
    default_order = 200
    menu_path = "Post-processing/Color Adjustments"

    post_exposure: float = serialized_field(default=0.0, range=(-5.0, 5.0), drag_speed=0.05, slider=False)
    contrast: float = serialized_field(default=0.0, range=(-100.0, 100.0), slider=False)
    saturation: float = serialized_field(default=0.0, range=(-100.0, 100.0), slider=False)
    hue_shift: float = serialized_field(default=0.0, range=(-180.0, 180.0), slider=False)

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "Color Adjustments"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_coloradj_out",
            pass_name="ColorAdj_Apply",
            shader_name="Color Adjustments",
            format=Format.RGBA16_SFLOAT,
            params={
                "postExposure": self.post_exposure,
                "contrast": self.contrast,
                "saturation": self.saturation,
                "hueShift": self.hue_shift,
            },
        )
