"""Separable Gaussian blur for isolated queue routes or full images."""

from __future__ import annotations

from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack.fullscreen_effect import FullScreenEffect


class GaussianBlurEffect(FullScreenEffect):
    name = "Gaussian Blur"
    injection_point = "before_post_process"
    default_order = 300
    menu_path = "Blur/Gaussian Blur"

    radius: int = serialized_field(
        default=12,
        range=(1, 128),
        slider=False,
        tooltip="Visible blur spread in full-resolution pixels",
    )
    sigma: float = serialized_field(
        default=3.0,
        range=(0.25, 64.0),
        slider=False,
        tooltip="Gaussian standard deviation",
    )

    def get_shader_list(self):
        return ["fullscreen_triangle", "gaussian_blur"]

    def setup_passes(self, graph, bus) -> None:
        from Infernux.rendergraph.graph import Format

        color_in = bus.get("color")
        if color_in is None:
            return
        horizontal = self.get_or_create_texture(
            graph,
            "_gaussian_horizontal",
            format=Format.RGBA16_SFLOAT,
        )
        color_out = self.get_or_create_texture(
            graph,
            "_gaussian_out",
            format=Format.RGBA16_SFLOAT,
        )
        parameters = {
            "radius": float(max(1, min(int(self.radius), 128))),
            "sigma": max(float(self.sigma), 0.25),
        }
        with graph.add_pass("GaussianBlur_Horizontal") as render_pass:
            render_pass.set_texture("_SourceTex", color_in)
            render_pass.write_color(horizontal)
            render_pass.set_param("directionX", 1.0)
            render_pass.set_param("directionY", 0.0)
            for name, value in parameters.items():
                render_pass.set_param(name, value)
            render_pass.fullscreen_quad("gaussian_blur")
        with graph.add_pass("GaussianBlur_Vertical") as render_pass:
            render_pass.set_texture("_SourceTex", horizontal)
            render_pass.write_color(color_out)
            render_pass.set_param("directionX", 0.0)
            render_pass.set_param("directionY", 1.0)
            for name, value in parameters.items():
                render_pass.set_param(name, value)
            render_pass.fullscreen_quad("gaussian_blur")
        bus.set("color", color_out)


__all__ = ["GaussianBlurEffect"]
