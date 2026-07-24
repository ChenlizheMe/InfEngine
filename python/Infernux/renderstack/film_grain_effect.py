"""
FilmGrainEffect — Physically-inspired photographic grain.

Models the three properties that make film grain read as film rather than
digital static: grains have a physical size (soft value-noise clumps),
the grain is multiplicative so blacks stay clean, and the grain plate
advances at 24 fps like a real print. Runs after tone mapping so the grain
modulates the final displayed exposure, matching Unity URP ordering.

Parameters:
    intensity  — grain strength (0 = off, 1 = heavy)
    response   — luminance response (0 = uniform, 1 = midtones/shadows only)
    size       — grain size in pixels (1 = fine modern stock, 3+ = coarse)
    colored    — independent grain per channel like color negative stock
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.serialized_field import serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class FilmGrainEffect(FullScreenEffect):
    """URP-aligned Film Grain post-processing effect."""

    name = "Film Grain"
    injection_point = "after_post_process"
    # After tone mapping (900): grain modulates the final displayed image,
    # matching real film where grain lives in the print, not the scene light.
    default_order = 950
    menu_path = "Post-processing/Film Grain"

    intensity: float = serialized_field(default=0.2, range=(0.0, 1.0), slider=False)
    response: float = serialized_field(default=0.8, range=(0.0, 1.0), slider=False)
    size: float = serialized_field(
        default=1.6,
        range=(0.5, 4.0),
        slider=False,
        tooltip="Grain size in pixels (1 = fine modern stock, 3+ = coarse vintage)",
    )
    colored: bool = serialized_field(
        default=False,
        tooltip="Independent grain per RGB channel, like color negative film",
    )

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "Film Grain"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_filmgrain_out",
            pass_name="FilmGrain_Apply",
            shader_name="Film Grain",
            format=Format.RGBA16_SFLOAT,
            params={
                "intensity": self.intensity,
                "response": self.response,
                "size": self.size,
                "colored": 1.0 if self.colored else 0.0,
            },
        )
