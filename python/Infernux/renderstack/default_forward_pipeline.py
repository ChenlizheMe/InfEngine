"""
DefaultForwardPipeline — Standard 3-pass Forward+ rendering pipeline.

This is the default pipeline used when no custom pipeline is selected.
It defines a standard Forward+ rendering topology:

    OpaquePass → after_opaque → SkyboxPass → after_sky
    → TransparentPass → after_transparent

ScreenUI passes and post-process injection points are auto-generated
when the pipeline explicitly calls ``graph.screen_ui_section()``.

All injection points are exposed for user passes to hook into.

Usage::

    # Automatic — RenderStack uses this when pipeline_class_name is empty
    stack = game_object.add_component(RenderStack)
    # stack.pipeline is DefaultForwardPipeline by default

    # Manual — can also be selected explicitly
    stack.set_pipeline("Default Forward+")
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from Infernux.renderstack.render_pipeline import RenderPipeline
from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack._pipeline_common import (
    COLOR_TEXTURE,
    SCENE_RESOURCES,
    add_forward_opaque_pass,
    add_shadow_caster_pass,
    add_skybox_pass,
    add_standard_post_process_section,
    add_transparent_pass,
    create_main_scene_targets,
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
    """Standard Forward+ rendering pipeline.

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

    name: str = "Default Forward+"

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

    enable_screen_ui: bool = serialized_field(
        default=True,
        tooltip="Enable screen-space UI rendering (Canvas Overlay / Camera)",
        header="Screen UI",
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
        graph.set_msaa_samples(int(self.msaa_samples))

        # ---- Shadow map configuration (from exposed parameters) ----
        shadow_res = self.shadow_resolution

        # ---- Create resources ----
        create_main_scene_targets(graph, shadow_resolution=shadow_res)

        # Pass 0: Shadow caster pass (depth-only, custom resolution)
        add_shadow_caster_pass(graph)

        # Pass 1: Opaque objects (front-to-back for early-z)
        add_forward_opaque_pass(graph, material_pass="forward_plus")
        graph.injection_point("after_opaque", resources=SCENE_RESOURCES)
        graph.effects(
            "after_opaque",
            scope="stage",
            display_name="After Opaque",
            inputs=SCENE_RESOURCES,
            outputs={"color"},
            capabilities={"fullscreen"},
        )

        # Pass 2: Skybox (renders after opaque, depth-tested)
        add_skybox_pass(graph)
        graph.injection_point("after_sky", resources=SCENE_RESOURCES)
        graph.effects(
            "after_sky",
            scope="composite",
            display_name="After Sky",
            inputs=SCENE_RESOURCES,
            outputs={"color"},
            capabilities={"fullscreen"},
        )

        # Pass 3: Transparent objects (back-to-front for blending)
        add_transparent_pass(graph, material_pass="forward_plus")
        graph.injection_point("after_transparent", resources=SCENE_RESOURCES)
        graph.effects(
            "after_transparent",
            scope="composite",
            display_name="After Transparent",
            inputs=SCENE_RESOURCES,
            outputs={"color"},
            capabilities={"fullscreen"},
        )

        graph.effects(
            "final",
            scope="composite",
            display_name="Final Post Processing",
            inputs={"color"},
            outputs={"color"},
            capabilities={"fullscreen", "hdr_to_display"},
        )

        # Post-process + ScreenUI injection points
        add_standard_post_process_section(graph, enable_screen_ui=self.enable_screen_ui)

        graph.set_output(COLOR_TEXTURE)
