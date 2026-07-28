"""Camera and object motion blur backed by the shared motion-vector target."""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack._pipeline_common import COLOR_TEXTURE, DEPTH_TEXTURE, MOTION_TEXTURE
from Infernux.renderstack.fullscreen_effect import FullScreenEffect

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class MotionBlurEffect(FullScreenEffect):
    """Depth-aware screen-space blur driven by per-pixel UV motion vectors."""

    name = "Motion Blur"
    injection_point = "before_post_process"
    default_order = 700
    menu_path = "Post-processing/Motion Blur"

    requires = {COLOR_TEXTURE, DEPTH_TEXTURE, MOTION_TEXTURE}
    modifies = {COLOR_TEXTURE}

    intensity: float = serialized_field(
        default=1.0,
        range=(0.0, 2.0),
        drag_speed=0.01,
        slider=False,
        tooltip="Shutter strength applied to camera and object motion",
    )
    max_blur_pixels: float = serialized_field(
        default=32.0,
        range=(0.0, 128.0),
        drag_speed=0.5,
        slider=False,
        tooltip="Maximum blur radius in output pixels",
    )
    depth_rejection: float = serialized_field(
        default=1.0,
        range=(0.0, 8.0),
        drag_speed=0.05,
        slider=False,
        tooltip="Suppresses samples crossing depth discontinuities",
    )

    def get_shader_list(self) -> List[str]:
        return ["Fullscreen Triangle", "Motion Blur"]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        from Infernux.rendergraph.graph import Format

        color_in = bus.get(COLOR_TEXTURE)
        depth = bus.get(DEPTH_TEXTURE)
        motion = bus.get(MOTION_TEXTURE)
        if color_in is None or depth is None or motion is None:
            missing = [
                name
                for name, handle in (
                    (COLOR_TEXTURE, color_in),
                    (DEPTH_TEXTURE, depth),
                    (MOTION_TEXTURE, motion),
                )
                if handle is None
            ]
            raise ValueError(
                "Motion Blur requires effect-stage resources: " + ", ".join(missing)
            )

        color_out = self.get_or_create_texture(
            graph,
            "_motion_blur_out",
            format=Format.RGBA16_SFLOAT,
        )
        with graph.add_pass("MotionBlur_Apply") as render_pass:
            render_pass.set_textures(
                {
                    "_SourceTex": color_in,
                    "_MotionTex": motion,
                    "_DepthTex": depth,
                }
            )
            render_pass.write_color(color_out)
            render_pass.set_param("intensity", min(max(float(self.intensity), 0.0), 2.0))
            render_pass.set_param(
                "maxBlurPixels",
                min(max(float(self.max_blur_pixels), 0.0), 128.0),
            )
            render_pass.set_param(
                "depthRejection",
                min(max(float(self.depth_rejection), 0.0), 8.0),
            )
            render_pass.set_param("_pad0", 0.0)
            render_pass.fullscreen_quad("Motion Blur")

        bus.set(COLOR_TEXTURE, color_out)
