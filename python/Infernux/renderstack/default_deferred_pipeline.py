"""
DefaultDeferredPipeline — Standard deferred rendering pipeline.

Implements a classic deferred shading pipeline with a GBuffer pass
that writes multiple render targets (albedo, normals, material props),
followed by a fullscreen lighting pass, then transparent forward pass.

GBuffer layout (MRT)::

    Slot 0 — Base Color         (RGBA16_SFLOAT)
    Slot 1 — World Normals      (RGBA16_SFLOAT)
    Slot 2 — Material Params    (RGBA8_UNORM)
    Slot 3 — Emission           (RGBA16_SFLOAT)
    Slot 4 — Object Metadata    (RG32_UINT)
    Depth  — Scene depth        (D32_SFLOAT)

Topology::

    ShadowCasterPass → GBufferPass → after_gbuffer
    → DeferredLightingPass → DeferredForwardFallbackPass → after_opaque
    → SkyboxPass → after_sky
    → TransparentPass (Forward+) → after_transparent
    → [post-process injection points]

Usage::

    stack = game_object.add_component(RenderStack)
    stack.set_pipeline("Default Deferred")

.. note::
    This pipeline requires the engine to support:
    - MRT (multiple render targets) — already supported
    - Depth buffer as shader input — now supported
    - Deferred lighting shader (``deferred_lighting.frag``)

    Deferred lighting uses the same camera-local light, shadow, layer-mask,
    and Forward+ tile data as the built-in Forward+ path.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from Infernux.renderstack.render_pipeline import RenderPipeline
from Infernux.renderstack._platform_quality import effective_shadow_resolution
from Infernux.components.fields import serialized_field
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
    GBUFFER_OBJECT_TEXTURE,
    GBUFFER_RESOURCES,
    SHADOW_MAP_TEXTURE,
    add_shadow_caster_pass,
    add_skybox_pass,
    add_standard_post_process_section,
    add_transparent_pass,
    create_deferred_gbuffer,
    create_main_scene_targets,
    opaque_queue_range,
    result_resources,
    transparent_queue_range,
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

    # ------------------------------------------------------------------
    # RenderPipeline interface
    # ------------------------------------------------------------------

    def define_topology(self, graph: "RenderGraph") -> None:
        """Define the built-in deferred rendering topology.

        Topology::

            ShadowCaster → GBuffer (MRT) → after_gbuffer
            → DeferredLighting → Deferred Forward+ Fallback → after_opaque
            → Skybox → after_sky
            → Transparent (forward) → after_transparent
        """
        # Deferred pipeline does not support MSAA on GBuffer
        graph.set_msaa_samples(1)

        shadow_res = effective_shadow_resolution(self.shadow_resolution)

        # ---- GBuffer textures (MRT) ----
        create_main_scene_targets(
            graph,
            shadow_resolution=shadow_res,
            msaa_samples=1,
        )
        create_deferred_gbuffer(graph)

        # ---- Pass 0: Shadow casters ----
        add_shadow_caster_pass(graph)

        # ---- Pass 1: GBuffer (opaque geometry → MRT) ----
        with graph.add_pass("GBufferPass") as p:
            p.write_color(GBUFFER_ALBEDO_TEXTURE, slot=0)
            p.write_color(GBUFFER_NORMAL_TEXTURE, slot=1)
            p.write_color(GBUFFER_MATERIAL_TEXTURE, slot=2)
            p.write_color(GBUFFER_EMISSION_TEXTURE, slot=3)
            p.write_color(GBUFFER_OBJECT_TEXTURE, slot=4)
            p.write_depth(DEPTH_TEXTURE)
            p.set_clear(
                color=DEFERRED_GBUFFER_CLEAR_COLOR,
                depth=1.0,
            )
            p.draw_renderers(
                queue_range=opaque_queue_range(),
                sort_mode="front_to_back",
                material_pass="gbuffer",
                material_filter="deferred_compatible",
            )

        current = self.geometry_stage(
            graph,
            "gbuffer",
            buffers={
                "color": graph.get_texture(COLOR_TEXTURE),
                "base_color": graph.get_texture(GBUFFER_ALBEDO_TEXTURE),
                "normal": graph.get_texture(GBUFFER_NORMAL_TEXTURE),
                "depth": graph.get_texture(DEPTH_TEXTURE),
                "material": graph.get_texture(GBUFFER_MATERIAL_TEXTURE),
                "emission": graph.get_texture(GBUFFER_EMISSION_TEXTURE),
                "object": graph.get_texture(GBUFFER_OBJECT_TEXTURE),
            },
            queue_range=opaque_queue_range(),
            clear=True,
        )

        with graph.pass_result(current):
            resources = result_resources(current)
            graph.injection_point("after_gbuffer", resources=resources)
            graph.effects(
                "after_gbuffer", scope="stage", display_name="After GBuffer",
                inputs=resources, outputs=resources,
                capabilities={"fullscreen", "multiple_render_targets"},
            )
            current = graph.current_pass_result

        # ---- Pass 2: Deferred lighting (fullscreen) ----
        with graph.add_pass("DeferredLightingPass") as p:
            p.set_textures(
                {
                    "gAlbedo": GBUFFER_ALBEDO_TEXTURE,
                    "gNormal": GBUFFER_NORMAL_TEXTURE,
                    "gMaterial": GBUFFER_MATERIAL_TEXTURE,
                    "gEmission": GBUFFER_EMISSION_TEXTURE,
                    "gObject": GBUFFER_OBJECT_TEXTURE,
                    "sceneDepth": DEPTH_TEXTURE,
                }
            )
            p.write_color(COLOR_TEXTURE)
            p.set_clear(color=DEFERRED_LIGHTING_CLEAR_COLOR)
            p.fullscreen_quad(DEFERRED_LIGHTING_SHADER)

        # Opaque shading models are Deferred-compatible by default. Models
        # that explicitly declare Unsupported [Deferred] skip the GBuffer and
        # render here with the camera's Forward+ light list while sharing the
        # same scene color/depth. This is a real topology fallback, not an
        # error-material substitution inside GBufferPass.
        with graph.add_pass("DeferredForwardFallbackPass") as p:
            p.write_color(COLOR_TEXTURE)
            p.write_depth(DEPTH_TEXTURE)
            p.draw_renderers(
                queue_range=opaque_queue_range(),
                sort_mode="front_to_back",
                material_pass="forward_plus",
                material_filter="deferred_unsupported",
            )

        current = graph.derive_pass_result(
            "opaque_lighting", current, {"color": graph.get_texture(COLOR_TEXTURE)}
        )
        with graph.pass_result(current):
            resources = result_resources(current)
            graph.injection_point("after_opaque", resources=resources)
            graph.effects(
                "after_opaque", scope="stage",
                display_name="After Opaque Lighting", inputs=resources,
                outputs={"color"}, capabilities={"fullscreen"},
            )
            current = graph.current_pass_result

        # ---- Pass 3: Skybox ----
        add_skybox_pass(graph)
        current = graph.derive_pass_result("sky", current, {"color": graph.get_texture(COLOR_TEXTURE)})
        with graph.pass_result(current):
            resources = result_resources(current)
            graph.injection_point("after_sky", resources=resources)
            graph.effects(
                "after_sky", scope="composite", display_name="After Sky",
                inputs=resources, outputs={"color"}, capabilities={"fullscreen"},
            )
            current = graph.current_pass_result

        # ---- Pass 4: Transparent objects (Forward+ rendering) ----
        add_transparent_pass(graph, material_pass="forward_plus")
        current = graph.derive_pass_result(
            "transparent", current, {"color": graph.get_texture(COLOR_TEXTURE)}
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

        # ---- Camera UI + post-process + Screen UI tail ----
        with graph.pass_result(current):
            add_standard_post_process_section(graph, current)

        graph.set_output(COLOR_TEXTURE)
