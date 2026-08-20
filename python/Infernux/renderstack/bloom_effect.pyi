"""Type stubs for Infernux.renderstack.bloom_effect — Unity-aligned Bloom post-processing."""

from __future__ import annotations

from typing import ClassVar, List, TYPE_CHECKING

from Infernux.renderstack.fullscreen_effect import FullScreenEffect

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class BloomEffect(FullScreenEffect):
    """Unity-aligned Bloom post-processing effect.

    Uses a progressive downsample/upsample chain with soft threshold
    and scatter-based diffusion, matching Unity URP's Bloom implementation.

    Attributes:
        threshold: Minimum brightness for bloom contribution (default 1.0).
        intensity: Final bloom intensity multiplier (default 0.8).
        scatter: Diffusion / spread factor (default 0.7).
        clamp: Maximum brightness to prevent fireflies (default 65472).
        tint_r: Red channel tint (0–1).
        tint_g: Green channel tint (0–1).
        tint_b: Blue channel tint (0–1).
        max_iterations: Maximum downsample/upsample iterations (1–8).
    """

    name: ClassVar[str]
    injection_point: ClassVar[str]
    default_order: ClassVar[int]
    menu_path: ClassVar[str]

    threshold: float
    intensity: float
    scatter: float
    clamp: float
    tint: list[float]
    max_iterations: int

    def get_shader_list(self) -> List[str]: ...
    def setup_passes(self, graph: RenderGraph, bus: ResourceBus) -> None: ...
