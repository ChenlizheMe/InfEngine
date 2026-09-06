"""
DefaultForwardPipeline — Standard 3-pass Forward rendering pipeline.

This is the default pipeline used when no custom pipeline is selected.
It defines a standard Forward rendering topology:

    OpaquePass → after_opaque → SkyboxPass → after_sky
    → TransparentPass → after_transparent

ScreenUI passes and post-process injection points are auto-generated
when the pipeline explicitly calls ``graph.screen_ui_section()``.

All injection points are exposed for user passes to hook into.

Usage::

    # Automatic — RenderStack stores the explicit default pipeline name
    stack = game_object.add_component(RenderStack)
    # stack.pipeline is DefaultForwardPipeline by default

    # Manual — can also be selected explicitly
    stack.set_pipeline("Default Forward")
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from Infernux.renderstack.render_pipeline import RenderPipeline
from Infernux.renderstack._platform_quality import (
    effective_msaa_samples,
    effective_shadow_resolution,
)
from Infernux.components.fields import serialized_field
from Infernux.renderstack._pipeline_common import (
    COLOR_TEXTURE,
    add_forward_opaque_pass,
    add_shadow_caster_pass,
    add_skybox_pass,
    add_standard_post_process_section,
    add_transparent_pass,
    create_main_scene_targets,
    opaque_queue_range,
    result_resources,
    transparent_queue_range,
)

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


class MSAASamples(IntEnum):
    """Anti-aliasing sample count."""
    OFF = 1
    X2 = 2
    X4 = 4
    X8 = 8


class DefaultForwardPipeline(RenderPipeline):
    """Standard Forward rendering pipeline.

    Defines 3 injection points:

    =============================  ==================================
    Injection Point                 Timing
    =============================  ==================================
    ``after_opaque``               After opaque objects, before skybox
    ``after_sky``                  After skybox, before transparent
    ``after_transparent``          After transparent objects
    =============================  ==================================

    ``before_post_process`` / ``after_post_process`` injection points and
    ScreenUI Camera / Overlay render passes are inserted explicitly by
    ``graph.screen_ui_section()``.
    """

    name: str = "Default Forward"
    material_pass = "forward"

    # ------------------------------------------------------------------
    # Exposed parameters (shown in RenderStack inspector)
    # ------------------------------------------------------------------
    shadow_resolution: int = serialized_field(
        default=4096,
        range=(256, 8192),
        slider=False,
        tooltip="Shadow map resolution (width & height)",
        header="Shadows",
    )

    msaa_samples: MSAASamples = serialized_field(
        default=MSAASamples.X4,
        enum_labels=["X1 (Off)", "X2", "X4", "X8"],
        tooltip="Anti-aliasing sample count (X1 disables multisample anti-aliasing)",
        header="Anti-Aliasing",
    )

    # ------------------------------------------------------------------
    # RenderPipeline interface
    # ------------------------------------------------------------------

    def define_topology(self, graph: "RenderGraph") -> None:
        """Define forward rendering topology skeleton.

        Topology::

            ShadowCasterPass → OpaquePass → after_opaque → SkyboxPass → after_sky
            → TransparentPass → after_transparent
        """
        # ---- MSAA configuration (from exposed parameter) ----
        msaa_samples = effective_msaa_samples(int(self.msaa_samples))
        graph.set_msaa_samples(msaa_samples)

        # ---- Shadow map configuration (from exposed parameters) ----
        # The serialized range is enforced by the descriptor. Keep this
        # boundary normalization as a final guard for pipeline values restored
        # from external/custom state before descriptor assignment.
        shadow_res = effective_shadow_resolution(self.shadow_resolution)

        # ---- Create resources ----
        create_main_scene_targets(
            graph,
            shadow_resolution=shadow_res,
            msaa_samples=msaa_samples,
        )

        # Pass 0: Shadow caster pass (depth-only, custom resolution)
        add_shadow_caster_pass(graph)

        # Pass 1: Opaque objects (front-to-back for early-z)
        add_forward_opaque_pass(graph, material_pass=self.material_pass)
        current = self.geometry_stage(
            graph,
            "opaque",
            buffers={
                "color": graph.get_texture("color"),
                "depth": graph.get_texture("depth"),
            },
            queue_range=opaque_queue_range(),
            msaa_samples=msaa_samples,
            clear=True,
        )
        with graph.pass_result(current):
            resources = result_resources(current)
            graph.injection_point("after_opaque", resources=resources)
            graph.effects(
                "after_opaque",
                scope="stage",
                display_name="After Opaque",
                inputs=resources,
                outputs={"color"},
                capabilities={"fullscreen"},
            )
            current = graph.current_pass_result

        # Pass 2: Skybox (renders after opaque, depth-tested)
        add_skybox_pass(graph)
        current = graph.derive_pass_result("sky", current, {"color": graph.get_texture("color")})
        with graph.pass_result(current):
            resources = result_resources(current)
            graph.injection_point("after_sky", resources=resources)
            graph.effects(
                "after_sky", scope="composite", display_name="After Sky",
                inputs=resources, outputs={"color"}, capabilities={"fullscreen"},
            )
            current = graph.current_pass_result

        # Pass 3: Transparent objects (back-to-front for blending)
        add_transparent_pass(graph, material_pass=self.material_pass)
        current = graph.derive_pass_result(
            "transparent", current, {"color": graph.get_texture("color")}
        )
        with graph.pass_result(current):
            resources = result_resources(current)
            graph.injection_point("after_transparent", resources=resources)
            graph.effects(
                "after_transparent", scope="composite",
                display_name="After Transparent", inputs=resources,
                outputs={"color"}, capabilities={"fullscreen"},
            )
            current = graph.current_pass_result

        # Camera UI, final post-processing, display encoding and Screen UI use
        # one canonical tail in every built-in pipeline.
        with graph.pass_result(current):
            add_standard_post_process_section(graph, current)

        graph.set_output(COLOR_TEXTURE)
