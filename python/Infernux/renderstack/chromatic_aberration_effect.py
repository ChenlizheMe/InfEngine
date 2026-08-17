"""
ChromaticAberrationEffect — RGB channel separation from screen center.

Aligned with Unity URP Chromatic Aberration. Simulates lens imperfection
where different wavelengths refract at different angles.

Parameters:
    intensity — channel separation strength (0 = off, 1 = strong)
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.fields import serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class ChromaticAberrationEffect(FullScreenEffect):
    """URP-aligned Chromatic Aberration post-processing effect."""

    name = "Chromatic Aberration"
    injection_point = "before_post_process"
    default_order = 600
    menu_path = "Post-processing/Chromatic Aberration"

    intensity: float = serialized_field(default=0.1, range=(0.0, 1.0), slider=False)

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "Chromatic Aberration"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_chromab_out",
            pass_name="ChromAb_Apply",
            shader_name="Chromatic Aberration",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": self.intensity},
        )
