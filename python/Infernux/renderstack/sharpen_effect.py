"""
SharpenEffect — Contrast Adaptive Sharpening (CAS) post-processing effect.

Implements AMD FidelityFX CAS-inspired sharpening. Enhances local contrast
without introducing visible halos. Operates in LDR space (after_post_process)
after tone mapping, matching the typical game sharpening pipeline placement.

Parameters:
    intensity — sharpening strength (0 = off, 1 = maximum sharpening)
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.fields import serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class SharpenEffect(FullScreenEffect):
    """Contrast Adaptive Sharpening (CAS) post-processing effect.

    Placed after tone mapping to sharpen the final LDR image.
    Based on AMD FidelityFX CAS algorithm.
    """

    name = "Sharpen"
    injection_point = "after_post_process"
    default_order = 920  # after tone mapping (900), before film grain (950)
    menu_path = "Post-processing/Sharpen"

    # ---- Serialized parameters ----
    intensity: float = serialized_field(
        default=0.5,
        range=(0.0, 1.0),
        drag_speed=0.01,
        slider=False,
        tooltip="Sharpening strength (0 = off, 1 = maximum)",
    )

    # ------------------------------------------------------------------
    # FullScreenEffect interface
    # ------------------------------------------------------------------

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "Sharpen CAS"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_sharpen_out",
            pass_name="Sharpen_CAS",
            shader_name="Sharpen CAS",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": self.intensity},
        )
