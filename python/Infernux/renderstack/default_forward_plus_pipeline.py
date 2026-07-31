"""Built-in tiled Forward+ rendering pipeline.

Forward and Forward+ intentionally share the same topology and user-facing
effect mount points.  The only semantic difference is the material pass used
for opaque and transparent geometry: this class selects the tiled-light
Forward+ variants while :class:`DefaultForwardPipeline` remains the lightweight
default for ordinary scenes.
"""

from __future__ import annotations

from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline


class DefaultForwardPlusPipeline(DefaultForwardPipeline):
    """Standard Forward+ pipeline using camera-local tiled light lists."""

    name: str = "Default Forward+"
    material_pass = "forward_plus"
