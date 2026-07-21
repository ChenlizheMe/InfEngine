"""Reusable luminance conversion effect for queue routes and full images."""

from __future__ import annotations

from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack.fullscreen_effect import FullScreenEffect


class GrayscaleEffect(FullScreenEffect):
    name = "Grayscale"
    injection_point = "before_post_process"
    default_order = 250
    menu_path = "Color/Grayscale"

    intensity: float = serialized_field(
        default=1.0,
        range=(0.0, 1.0),
        slider=False,
        tooltip="Blend between the original color and luminance",
    )

    def get_shader_list(self):
        return ["fullscreen_triangle", "grayscale"]

    def setup_passes(self, graph, bus) -> None:
        from Infernux.rendergraph.graph import Format

        self.apply_single_source_effect(
            graph,
            bus,
            output_name="_grayscale_out",
            pass_name="Grayscale_Apply",
            shader_name="grayscale",
            format=Format.RGBA16_SFLOAT,
            params={"intensity": self.intensity},
        )


__all__ = ["GrayscaleEffect"]
