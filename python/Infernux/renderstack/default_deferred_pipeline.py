"""
DefaultDeferredPipeline — Standard deferred rendering pipeline.

Implements a classic deferred shading pipeline with a GBuffer pass
that writes multiple render targets (albedo, normals, material props),
followed by a fullscreen lighting pass, then transparent forward pass.

GBuffer layout (MRT)::

    Slot 0 — Lit Scene Color    (RGBA8_UNORM)
    Slot 1 — World Normals      (RGBA16_SFLOAT)
    Slot 2 — Material Params    (RGBA8_UNORM)
    Slot 3 — Emission           (RGBA16_SFLOAT)
    Depth  — Scene depth        (D32_SFLOAT)

Topology::

    ShadowCasterPass → GBufferPass → after_gbuffer
    → DeferredLightingPass → after_opaque
    → SkyboxPass → after_sky
    → TransparentPass (forward) → after_transparent
    → [post-process injection points]

Usage::

    stack = game_object.add_component(RenderStack)
    stack.set_pipeline("Default Deferred")

.. note::
    This pipeline requires the engine to support:
    - MRT (multiple render targets) — already supported
    - Depth buffer as shader input — now supported
    - Deferred lighting shader (``deferred_lighting.frag``)

    The deferred lighting shader is NOT yet shipped with the engine.
    Users must provide their own ``deferred_lighting`` shader, or this
    pipeline will fall back to a placeholder that outputs albedo only.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from Infernux.renderstack.render_pipeline import RenderPipeline
from Infernux.components.serialized_field import serialized_field
from Infernux.renderstack._pipeline_common import (
    COLOR_TEXTURE,
    DEPTH_TEXTURE,
    DEFERRED_GBUFFER_CLEAR_COLOR,
    DEFERRED_LIGHTING_CLEAR_COLOR,
    DEFERRED_LIGHTING_SHADER,
    GBUFFER_ALBEDO_TEXTURE,
    GBUFFER_EMISSION_TEXTURE,
    GBUFFER_MATERIAL_TEXTURE,
    GBUFFER_NORMAL_TEXTURE,
    GBUFFER_RESOURCES,
    SCENE_RESOURCES,
    SHADOW_MAP_TEXTURE,
    add_shadow_caster_pass,
    add_skybox_pass,
    add_standard_post_process_section,
    add_transparent_pass,
    create_deferred_gbuffer,
    create_main_scene_targets,
    opaque_queue_range,
)

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


class DeferredMSAA(IntEnum):
    """Deferred pipeline only supports OFF or resolve-after-lighting."""
    OFF = 1


class DefaultDeferredPipeline(RenderPipeline):
    """Standard deferred rendering pipeline.

    Uses GBuffer multi-target rendering to separate geometry and lighting,
    suitable for scenes with many light sources.

    Injection points:

    =============================  ==================================
    Injection Point                 Timing
    =============================  ==================================
    ``after_gbuffer``              After GBuffer, before lighting
    ``after_opaque``               After deferred lighting, before skybox
    ``after_sky``                  After skybox, before transparent
    ``after_transparent``          After transparent objects
    =============================  ==================================

    ``before_post_process`` / ``after_post_process`` are automatically
    inserted by ``graph.screen_ui_section()``.
    """

    name: str = "Default Deferred"

    # ------------------------------------------------------------------
    # Exposed parameters
    # ------------------------------------------------------------------
    shadow_resolution: int = serialized_field(
        default=4096,
        range=(256, 8192),
        slider=False,
        tooltip="Shadow map resolution (width & height)",
        header="Shadows",
    )

    enable_screen_ui: bool = serialized_field(
        default=True,
        tooltip="Enable screen-space UI rendering",
        header="Screen UI",
    )

    # ------------------------------------------------------------------
    # RenderPipeline interface
    # ------------------------------------------------------------------

    def define_topology(self, graph: "RenderGraph") -> None:
        """Define deferred rendering topology skeleton.

        Topology::

            ShadowCaster → GBuffer (MRT) → after_gbuffer
            → DeferredLighting → after_opaque → Skybox → after_sky
            → Transparent (forward) → after_transparent
        """
        # Deferred pipeline does not support MSAA on GBuffer
        graph.set_msaa_samples(1)

        shadow_res = self.shadow_resolution

        # ---- GBuffer textures (MRT) ----
        create_main_scene_targets(graph, shadow_resolution=shadow_res)
        create_deferred_gbuffer(graph)

        # ---- Pass 0: Shadow casters ----
        add_shadow_caster_pass(graph)

        # ---- Pass 1: GBuffer (opaque geometry → MRT) ----
        with graph.add_pass("GBufferPass") as p:
            p.write_color(GBUFFER_ALBEDO_TEXTURE, slot=0)
            p.write_color(GBUFFER_NORMAL_TEXTURE, slot=1)
            p.write_color(GBUFFER_MATERIAL_TEXTURE, slot=2)
            p.write_color(GBUFFER_EMISSION_TEXTURE, slot=3)
            p.write_depth(DEPTH_TEXTURE)
            p.set_clear(
                color=DEFERRED_GBUFFER_CLEAR_COLOR,
                depth=1.0,
            )
            p.draw_renderers(
                queue_range=opaque_queue_range(),
                sort_mode="front_to_back",
                material_pass="gbuffer",
            )

        graph.injection_point("after_gbuffer", resources=GBUFFER_RESOURCES)

        # ---- Pass 2: Deferred lighting (fullscreen) ----
        with graph.add_pass("DeferredLightingPass") as p:
            p.set_textures(
                {
                    "gAlbedo": GBUFFER_ALBEDO_TEXTURE,
                    "gNormal": GBUFFER_NORMAL_TEXTURE,
                    "gMaterial": GBUFFER_MATERIAL_TEXTURE,
                    "gEmission": GBUFFER_EMISSION_TEXTURE,
                    "sceneDepth": DEPTH_TEXTURE,
                    "shadowMap": SHADOW_MAP_TEXTURE,
                }
            )
            p.write_color(COLOR_TEXTURE)
            p.set_clear(color=DEFERRED_LIGHTING_CLEAR_COLOR)
            p.fullscreen_quad(DEFERRED_LIGHTING_SHADER)

        graph.injection_point("after_opaque", resources=SCENE_RESOURCES)

        # ---- Pass 3: Skybox ----
        add_skybox_pass(graph)
        graph.injection_point("after_sky", resources=SCENE_RESOURCES)

        # ---- Pass 4: Transparent objects (forward rendering) ----
        add_transparent_pass(graph)
        graph.injection_point("after_transparent", resources=SCENE_RESOURCES)

        # ---- Post-process + ScreenUI injection points ----
        add_standard_post_process_section(graph, enable_screen_ui=self.enable_screen_ui)

        graph.set_output(COLOR_TEXTURE)
