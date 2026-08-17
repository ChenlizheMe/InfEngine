from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from Infernux.components.component import InxComponent
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.effect_stage import EffectStage

if TYPE_CHECKING:
    from Infernux.renderstack.render_pipeline import RenderPipeline


class RenderStack(InxComponent):
    """Scene singleton that binds reusable Effect assets to pipeline stages."""

    pipeline_class_name: str
    pipeline_params_json: str
    effect_slots: List[EffectSlot]

    @classmethod
    def instance(cls, scene: Any = ...) -> Optional[RenderStack]:
        """Return the current active RenderStack, or None."""
        ...

    def awake(self) -> None:
        """Initialize the render stack on component awake."""
        ...
    def on_destroy(self) -> None:
        """Clean up the render stack when the component is destroyed."""
        ...
    @staticmethod
    def discover_pipelines() -> Dict[str, type]:
        """Discover all available render pipeline classes."""
        ...
    def set_pipeline(self, pipeline_class_name: str) -> None:
        """Set the active render pipeline by class name."""
        ...
    @property
    def effect_binding_error(self) -> str: ...
    @property
    def effect_compile_errors(self) -> tuple[str, ...]: ...
    @property
    def effect_stages(self) -> tuple[EffectStage, ...]: ...
    @property
    def orphan_effect_slots(self) -> tuple[EffectSlot, ...]: ...
    def get_effect_stage_slots(self, stage_id: str) -> tuple[EffectSlot, ...]: ...
    def set_effect_stage_slots(self, stage_id: str, slots: tuple[EffectSlot, ...]) -> None: ...
    def add_effect_slot(self, stage_id: str, effect: Any = ..., *, enabled: bool = ...) -> EffectSlot: ...
    def get_effect(self, stage_id: str, index: int = ...) -> Optional[RenderEffect]: ...
    def remap_orphan_effect_stage(self, old_stage_id: str, new_stage_id: str) -> int: ...

    @property
    def pipeline(self) -> RenderPipeline:
        """The currently active render pipeline."""
        ...
    def invalidate_graph(self) -> None:
        """Mark the render graph as dirty, triggering a rebuild."""
        ...
    def build_graph(self) -> Any:
        """Build and return the render graph description."""
        ...
    def render(self, context: Any, camera: Any) -> None:
        """Execute the render stack for a camera."""
        ...

    def on_enable(self) -> None:
        """Called when the component is enabled."""
        ...
    def on_disable(self) -> None:
        """Called when the component is disabled."""
        ...
    def on_before_serialize(self) -> None:
        """Serialize render stack state before saving."""
        ...
    def on_after_deserialize(self) -> None:
        """Restore render stack state after loading."""
        ...


__all__ = ["RenderStack"]
