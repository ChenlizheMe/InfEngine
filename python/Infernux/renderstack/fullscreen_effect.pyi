from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Dict, List, Mapping, Set, TYPE_CHECKING

from Infernux.renderstack.render_pass import RenderPass

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus


class EffectColorComposition(str, Enum):
    REPLACE: EffectColorComposition
    ALPHA_OVER: EffectColorComposition

class FullScreenEffect(RenderPass):
    """Base class for fullscreen post-processing effects."""

    requires: ClassVar[Set[str]]
    modifies: ClassVar[Set[str]]
    menu_path: ClassVar[str]
    color_composition: ClassVar[EffectColorComposition]
    _serialized_fields_: ClassVar[Dict[str, Any]]

    def __init__(self, enabled: bool = ...) -> None: ...
    def setup_passes(self, graph: RenderGraph, bus: ResourceBus) -> None:
        """Override to add fullscreen passes to the render graph."""
        ...
    def get_shader_list(self) -> List[str]:
        """Return shader paths required by this effect."""
        ...
    def inject(self, graph: RenderGraph, bus: ResourceBus) -> None:
        """Inject this effect into the render graph."""
        ...
    def get_params_dict(self) -> Dict[str, Any]:
        """Get serializable parameters as a dictionary."""
        ...
    def set_params_dict(self, params: Dict[str, Any]) -> None:
        """Restore parameters from a dictionary."""
        ...
    @staticmethod
    def get_or_create_texture(
        graph: RenderGraph,
        name: str,
        *,
        format: Any = ...,
        camera_target: bool = ...,
        size: Any = ...,
        size_divisor: int = ...,
    ) -> Any:
        """Get or create a named texture handle in the render graph."""
        ...
    def apply_single_source_effect(
        self,
        graph: RenderGraph,
        bus: ResourceBus,
        *,
        output_name: str,
        pass_name: str,
        shader_name: str,
        format: Any,
        params: Mapping[str, object] | None = ...,
    ) -> bool:
        """Apply a one-pass fullscreen effect that rewrites the scene color."""
        ...
    def bind_buffers(
        self,
        render_pass: Any,
        bus: ResourceBus,
        *,
        extra_bindings: Mapping[str, object] | None = ...,
    ) -> None: ...
    def __repr__(self) -> str: ...
