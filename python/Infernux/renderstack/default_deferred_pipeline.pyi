"""Type stubs for Infernux.renderstack.default_deferred_pipeline."""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from Infernux.renderstack.render_pipeline import RenderPipeline

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


class DeferredMSAA(IntEnum):
    """Deferred pipeline only supports OFF."""
    OFF = 1


class DefaultDeferredPipeline(RenderPipeline):
    """Standard deferred rendering pipeline.

    Uses GBuffer multi-target rendering to separate geometry and lighting,
    suitable for scenes with many light sources.

    GBuffer layout::

        Slot 0 — Base Color         (RGBA16_SFLOAT)
        Slot 1 — World Normals      (RGBA16_SFLOAT)
        Slot 2 — Material Params    (RGBA8_UNORM)
        Slot 3 — Emission           (RGBA16_SFLOAT)
        Slot 4 — Object Metadata    (RG32_UINT)
        Depth  — Scene depth        (D32_SFLOAT)

    Injection points:

    =============================  ==================================
    Injection Point                 Timing
    =============================  ==================================
    ``after_gbuffer``              After GBuffer, before lighting
    ``after_opaque``               After deferred lighting, before skybox
    ``after_sky``                  After skybox, before transparent
    ``after_transparent``          After transparent objects
    =============================  ==================================

    Attributes:
        shadow_resolution: Shadow map resolution (default 4096).
        msaa_samples: Always OFF for deferred.
    """

    name: str
    shadow_resolution: int

    def define_topology(self, graph: RenderGraph) -> None: ...
