"""Type stubs for Infernux.renderstack.default_forward_pipeline."""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from Infernux.renderstack.render_pipeline import RenderPipeline

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


class MSAASamples(IntEnum):
    """Anti-aliasing sample count."""
    OFF = 1
    X2 = 2
    X4 = 4
    X8 = 8


class DefaultForwardPipeline(RenderPipeline):
    """Standard forward rendering pipeline (default pipeline).

    Defines a standard forward rendering topology::

        ShadowCasterPass -> OpaquePass -> after_opaque -> SkyboxPass -> after_sky
        -> TransparentPass -> after_transparent

    Injection points:

    =============================  ==================================
    Injection Point                 Timing
    =============================  ==================================
    ``after_opaque``               After opaque objects, before skybox
    ``after_sky``                  After skybox, before transparent
    ``after_transparent``          After transparent objects
    =============================  ==================================

    Attributes:
        shadow_resolution: Shadow map resolution (default 4096).
        msaa_samples: Anti-aliasing sample count (X1/Off, X2, X4, or X8; default X4).
    """

    name: str
    shadow_resolution: int
    msaa_samples: MSAASamples

    def define_topology(self, graph: RenderGraph) -> None: ...
