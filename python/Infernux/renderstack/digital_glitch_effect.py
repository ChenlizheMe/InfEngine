"""Digital glitch effect for isolated render-queue routes or full images."""

from __future__ import annotations

from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack.fullscreen_effect import FullScreenEffect


class DigitalGlitchEffect(FullScreenEffect):
    """Block displacement, RGB separation, and scanlines in one route-safe pass."""

    name = "Digital Glitch"
    injection_point = "before_post_process"
    default_order = 320
    menu_path = "Stylized/Digital Glitch"

    intensity: float = serialized_field(default=0.65, range=(0.0, 1.0), slider=False)
    block_size: float = serialized_field(default=18.0, range=(4.0, 64.0), slider=False)
    color_shift: float = serialized_field(default=0.75, range=(0.0, 1.0), slider=False)
    scanline_strength: float = serialized_field(default=0.22, range=(0.0, 1.0), slider=False)

    def get_shader_list(self):
        return ["fullscreen_triangle", "digital_glitch"]

    def setup_passes(self, graph, bus) -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_digital_glitch_out",
            pass_name="DigitalGlitch_Apply",
            shader_name="digital_glitch",
            format=Format.RGBA16_SFLOAT,
            params={
                "intensity": min(max(float(self.intensity), 0.0), 1.0),
                "blockSize": min(max(float(self.block_size), 4.0), 64.0),
                "colorShift": min(max(float(self.color_shift), 0.0), 1.0),
                "scanlineStrength": min(max(float(self.scanline_strength), 0.0), 1.0),
            },
        )


__all__ = ["DigitalGlitchEffect"]
