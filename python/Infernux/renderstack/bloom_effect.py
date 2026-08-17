"""
BloomEffect — Unity-aligned Bloom post-processing effect.

Implements a multi-pass bloom pipeline aligned with Unity URP's Bloom:
  1. Prefilter: soft knee threshold to extract bright pixels
  2. Downsample chain: 13-tap progressive downsamples (up to N iterations)
  3. Upsample chain: 9-tap tent filter with scatter blending
  4. Composite: additive blend bloom result onto scene color

Shader pipeline::

    bloom_prefilter   → threshold + knee
    bloom_downsample  → 13-tap Jimenez downsample (per mip)
    bloom_upsample    → 9-tap tent upsample with scatter (per mip)
    bloom_composite   → additive blend onto scene

Parameters (aligned with Unity URP Bloom):
    threshold   — minimum brightness for bloom contribution
    intensity   — final bloom intensity multiplier
    scatter     — diffusion / spread (maps to upsample blend factor)
    clamp       — maximum brightness to prevent fireflies
    tint        — (r, g, b) color tint applied during composite
    max_iterations — maximum number of downsample/upsample iterations
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect
from Infernux.components.fields import Color, serialized_field

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class BloomEffect(FullScreenEffect):
    """Unity-aligned Bloom post-processing effect.

    Uses a progressive downsample/upsample chain with soft threshold
    and scatter-based diffusion, matching Unity URP's Bloom implementation.
    """

    name = "Bloom"
    injection_point = "before_post_process"
    default_order = 100
    menu_path = "Post-processing/Bloom"

    # ---- Serialized parameters (shown in Inspector) ----
    threshold: float = serialized_field(default=1.0, range=(0.0, 10.0), slider=False)
    intensity: float = serialized_field(default=0.8, range=(0.0, 5.0), slider=False)
    scatter: float = serialized_field(default=0.7, range=(0.0, 1.0), slider=False)
    clamp: float = serialized_field(default=65472.0, range=(0.0, 65472.0), slider=False)
    tint: Color = Color(1.0, 1.0, 1.0, 1.0)
    max_iterations: int = serialized_field(default=5, range=(1, 8), slider=False)

    # ------------------------------------------------------------------
    # FullScreenEffect interface
    # ------------------------------------------------------------------

    def get_shader_list(self) -> List[str]:
        return [
            "Fullscreen Triangle",
            "Fullscreen Blit",
            "Bloom Prefilter",
            "Bloom Downsample",
            "Bloom Upsample",
            "Bloom Composite",
        ]

    def setup_passes(self, graph: "RenderGraph", bus: "ResourceBus") -> None:
        """Inject all bloom passes into the render graph.

        Pipeline overview::

            scene_color → [blit] → scene_copy
            scene_copy  → [prefilter] → mip0
            mip0 → [downsample] → mip1 → ... → mipN
            mipN + mip(N-1) → [upsample] → up(N-2) → ... → up0
            up0 + scene_copy → [composite] → scene_color

        Each intermediate mip/up texture is a transient texture with
        progressive half-resolution via size_divisor.
        The blit pass copies scene_color so the composite can read the
        original scene without a read-write hazard on the backbuffer.
        """
        from Infernux.rendergraph.graph import Format

        color_handle = bus.get("color")
        if color_handle is None:
            return

        iterations = max(1, min(self.max_iterations, 8))

        _tex = self.get_or_create_texture

        # ---- Create intermediate textures ----

        # Scene copy (full-res) — needed because composite reads original scene
        scene_copy = _tex(
            graph,
            "_bloom_scene_copy",
            format=Format.RGBA16_SFLOAT,
        )

        # Final output (separate from input to enable proper effect chaining)
        color_out = _tex(graph, "_bloom_out", format=Format.RGBA16_SFLOAT)

        # Downsample mip chain (half-res each step)
        mip_textures = []
        for i in range(iterations):
            divisor = 2 ** (i + 1)  # 2, 4, 8, 16, ...
            tex = _tex(
                graph,
                f"_bloom_mip{i}",
                format=Format.RGBA16_SFLOAT,
                size_divisor=divisor,
            )
            mip_textures.append(tex)

        # Upsample output textures (separate from mip textures to
        # avoid read+write hazard during the upsample chain)
        up_textures = []
        for i in range(iterations):
            divisor = 2 ** (i + 1)
            tex = _tex(
                graph,
                f"_bloom_up{i}",
                format=Format.RGBA16_SFLOAT,
                size_divisor=divisor,
            )
            up_textures.append(tex)

        # ---- Pass 0: Blit scene_color → scene_copy ----
        with graph.add_pass("Bloom_SceneCopy") as p:
            p.set_texture("_SourceTex", color_handle)
            p.write_color(scene_copy)
            p.fullscreen_quad("Fullscreen Blit")

        # ---- Pass 1: Prefilter (scene_copy → mip0) ----
        with graph.add_pass("Bloom_Prefilter") as p:
            p.set_texture("_SourceTex", scene_copy)
            p.set_param("threshold", self.threshold)
            p.set_param("knee", 0.5)
            p.set_param("clampMax", self.clamp)
            p.write_color(mip_textures[0])
            p.fullscreen_quad("Bloom Prefilter")

        # ---- Passes 2..N: Downsample chain (mip[i-1] → mip[i]) ----
        for i in range(1, iterations):
            src = mip_textures[i - 1]
            dst = mip_textures[i]
            with graph.add_pass(f"Bloom_Down{i}") as p:
                p.set_texture("_SourceTex", src)
                p.write_color(dst)
                p.fullscreen_quad("Bloom Downsample")

        # ---- Passes N..2: Upsample chain ----
        # Each step reads (lower-res mip OR previous up result) + higher-res mip,
        # writes to a SEPARATE up texture to avoid read+write hazard.
        for i in range(iterations - 1, 0, -1):
            # Source: lowest mip starts the chain, subsequent steps use
            # the previous upsample result
            if i == iterations - 1:
                src = mip_textures[i]  # bottom of the chain
            else:
                src = up_textures[i]   # previous upsample output
            higher = mip_textures[i - 1]  # skip-connection from downsample

            with graph.add_pass(f"Bloom_Up{i}") as p:
                p.set_texture("_SourceTex", src)
                p.set_texture("_DestTex", higher)
                p.write_color(up_textures[i - 1])
                p.set_param("scatter", self.scatter)
                p.fullscreen_quad("Bloom Upsample")

        # A one-level pyramid has no upsample pass; its prefiltered mip is the
        # final bloom source. Larger pyramids finish in up_textures[0].
        bloom_source = (
            mip_textures[0]
            if iterations == 1
            else up_textures[0]
        )

        # ---- Final pass: Composite (bloom + scene_copy → color_out) ----
        with graph.add_pass("Bloom_Composite") as p:
            p.set_texture("_BloomTex", bloom_source)
            p.set_texture("_SceneColor", scene_copy)
            p.write_color(color_out)
            p.set_param("intensity", self.intensity)
            p.set_param("tintR", self.tint[0])
            p.set_param("tintG", self.tint[1])
            p.set_param("tintB", self.tint[2])
            p.fullscreen_quad("Bloom Composite")

        # Update bus so subsequent effects read the bloom result
        bus.set("color", color_out)
