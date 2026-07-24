"""
VignetteEffect — Darkens screen edges for cinematic framing.

Aligned with Unity URP Vignette. Runs in HDR space (before_post_process)
so that tone mapping applies to the vignetted result.

Parameters:
    intensity   — vignette strength (0 = off, 1 = full black edges)
    smoothness  — falloff softness
    roundness   — shape control (1 = circular, lower = squared)
    rounded     — force perfectly circular regardless of aspect ratio
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.serialized_field import Color, serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class VignetteEffect(FullScreenEffect):
    """URP-aligned Vignette post-processing effect."""

    name = "Vignette"
    injection_point = "before_post_process"
    default_order = 500
    menu_path = "Post-processing/Vignette"

    intensity: float = serialized_field(default=0.35, range=(0.0, 1.0), slider=False)
    smoothness: float = serialized_field(default=0.3, range=(0.01, 1.0), slider=False)
    roundness: float = serialized_field(default=1.0, range=(0.0, 1.0), slider=False)
    rounded: bool = serialized_field(default=False)
    color: Color = Color(0.0, 0.0, 0.0, 1.0)

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "Vignette"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_vignette_out",
            pass_name="Vignette_Apply",
            shader_name="Vignette",
            format=Format.RGBA16_SFLOAT,
            params={
                "intensity": self.intensity,
                "smoothness": self.smoothness,
                "roundness": self.roundness,
                "rounded": 1.0 if self.rounded else 0.0,
                "colorR": self.color[0],
                "colorG": self.color[1],
                "colorB": self.color[2],
            },
        )
