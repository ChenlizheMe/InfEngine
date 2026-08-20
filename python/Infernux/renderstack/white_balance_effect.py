"""
WhiteBalanceEffect — Color temperature and tint adjustment.

Aligned with Unity URP White Balance. Operates in HDR space
(before_post_process) using Bradford chromatic adaptation.

Parameters:
    temperature — warm/cool shift (-100 to 100, 0 = neutral)
    tint        — green/magenta shift (-100 to 100, 0 = neutral)
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.fields import serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class WhiteBalanceEffect(FullScreenEffect):
    """URP-aligned White Balance post-processing effect."""

    name = "White Balance"
    injection_point = "before_post_process"
    default_order = 150
    menu_path = "Post-processing/White Balance"

    temperature: float = serialized_field(default=0.0, range=(-100.0, 100.0), slider=False)
    tint: float = serialized_field(default=0.0, range=(-100.0, 100.0), slider=False)

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "White Balance"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_whitebal_out",
            pass_name="WhiteBal_Apply",
            shader_name="White Balance",
            format=Format.RGBA16_SFLOAT,
            params={
                "temperature": self.temperature,
                "tint": self.tint,
            },
        )
