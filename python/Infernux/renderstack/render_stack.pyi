from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from Infernux.components.component import InxComponent
from Infernux.renderstack.injection_point import InjectionPoint
from Infernux.renderstack.resource_bus import ResourceBus
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.effect_stage import EffectStage

if TYPE_CHECKING:
    from Infernux.renderstack.render_pass import RenderPass
    from Infernux.renderstack.render_pipeline import RenderPipeline


@dataclass
class PassEntry:
    """Entry associating a render pass with its enabled state and order."""

    render_pass: RenderPass
    enabled: bool = ...
    order: int = ...


class RenderStack(InxComponent):
    """Scene singleton component that manages render pipelines and injected passes.

    Runtime class also mixes in render-pass management and pipeline reload
    behavior; those public methods are mirrored in this stub.
    """

    pipeline_class_name: str
    mounted_passes_json: str
    pipeline_params_json: str
    effect_stage_bindings_json: str
    effect_slots: List[EffectSlot]

    @classmethod
    def instance(cls) -> Optional[RenderStack]:
        """Return the current active RenderStack, or None."""
        ...

    def awake(self) -> None:
        """Initialize the render stack on component awake."""
        ...
    def on_destroy(self) -> None:
        """Clean up the render stack when the component is destroyed."""
        ...
    def on_inspector_gui(self, ctx: object) -> None:
        """Render the custom editor inspector for this RenderStack."""
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
    def effect_stages(self) -> tuple[EffectStage, ...]: ...
    @property
    def orphan_effect_slots(self) -> tuple[EffectSlot, ...]: ...
    def get_effect_stage_slots(self, stage_id: str) -> tuple[EffectSlot, ...]: ...
    def set_effect_stage_slots(self, stage_id: str, slots: tuple[EffectSlot, ...]) -> None: ...
    def add_effect_slot(self, stage_id: str, effect: Any = ..., *, enabled: bool = ...) -> EffectSlot: ...
    def get_effect(self, stage_id: str, index: int = ...) -> Optional[RenderEffect]: ...

    @property
    def pipeline(self) -> RenderPipeline:
        """The currently active render pipeline."""
        ...
    @property
    def injection_points(self) -> List[InjectionPoint]:
        """List of injection points defined by the pipeline."""
        ...
    @property
    def pass_entries(self) -> List[PassEntry]:
        """All mounted render pass entries."""
        ...

    def add_pass(self, render_pass: RenderPass) -> bool:
        """Add a render pass to the stack. Returns True on success."""
        ...
    def remove_pass(self, pass_name: str) -> bool:
        """Remove a render pass by name. Returns True if found."""
        ...
    def set_pass_enabled(self, pass_name: str, enabled: bool) -> None:
        """Enable or disable a render pass by name."""
        ...
    def reorder_pass(self, pass_name: str, new_order: int) -> None:
        """Change the execution order of a render pass."""
        ...
    def move_pass_before(self, dragged_name: str, target_name: str) -> None:
        """Move a render pass to execute before another pass."""
        ...
    def get_passes_at(self, injection_point: str) -> List[PassEntry]:
        """Get all pass entries at a specific injection point."""
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


__all__ = [
    "PassEntry",
    "RenderStack",
]
