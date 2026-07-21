"""
RenderStack — Scene-level rendering configuration component.

RenderStack is a scene-singleton InxComponent that manages:
- The active RenderPipeline (topology skeleton + EffectStages)
- Ordered reusable Effect assets mounted into those stages
- Graph construction and parameter-only hot updates

Architecture::

    RenderStack (InxComponent, scene singleton)
      ├── selected_pipeline: RenderPipeline  (defines topology skeleton)
      └── effect_slots: List[EffectSlot]      (stage-owned reusable assets)

    Each frame:
      1. RenderStack.render(context, camera)
      2. Lazy-build graph if invalidated
      3. context.apply_graph(desc) + context.submit_culling(culling)

Usage::

    # In a scene setup script
    stack = game_object.add_component(RenderStack)
    stack.set_pipeline("Default Forward")
    stack.add_effect_slot("final", bloom_effect_ref)
"""

from __future__ import annotations

import json as _json
import warnings
from typing import Dict, Optional, TYPE_CHECKING

from Infernux.components.component import InxComponent
from Infernux.components.serialized_field import FieldType, list_field
from Infernux.components.decorators import disallow_multiple, add_component_menu
from Infernux.renderstack._pipeline_common import (
    COLOR_TEXTURE,
    ensure_standard_post_process_points,
)
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.resource_bus import ResourceBus

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


from ._render_pipeline_reload import PipelineReloadMixin

@disallow_multiple
@add_component_menu("Rendering/RenderStack")
class RenderStack(PipelineReloadMixin, InxComponent):
    """Scene-level rendering configuration component.

    Manages the active RenderPipeline and its reusable EffectStage assets for
    the current scene. At most one RenderStack can be active at a time.

    Attributes:
        pipeline_class_name: Selected pipeline class name. Empty means the
            default pipeline.
        effect_slots: Ordered Effect assets grouped by pipeline stage.
    """

    _component_category_ = "Rendering"

    # ---- Class-level singleton (scene-global) ----
    _active_instance: Optional["RenderStack"] = None

    @classmethod
    def instance(cls) -> Optional["RenderStack"]:
        """Return the active scene-scoped RenderStack, or None."""
        inst = cls._active_instance
        if inst is not None and not cls._is_effectively_active(inst):
            cls._active_instance = None
            return None
        return inst

    @classmethod
    def _is_effectively_active(cls, stack: Optional["RenderStack"]) -> bool:
        if stack is None or not stack.is_valid or not stack.enabled:
            return False
        go = stack.game_object
        return bool(go is not None and go.is_active_in_hierarchy())

    # ---- Serialized fields ----
    pipeline_class_name: str = ""
    pipeline_params_json: str = ""  # Persisted pipeline parameter snapshot.
    effect_slots: list = list_field(
        element_type=FieldType.SERIALIZABLE_OBJECT,
        element_class=EffectSlot,
        default=[],
        tooltip="Ordered Effect assets mounted into pipeline stages.",
    )
    # ---- Runtime state (not serialized) ----
    _pipeline = None  # Optional[RenderPipeline]
    _graph_desc = None  # cached RenderGraphDescription
    _resource_bus: Optional[ResourceBus] = None
    _build_failed: bool = False  # True after a build error; cleared by invalidate_graph()
    _pipeline_module = None  # module object for watchdog hot-reload subscription
    _pipeline_param_store: Dict[str, Dict[str, object]] = None
    _pipeline_catalog_signature: tuple = ()
    _topology_probe_cache = None
    _compiled_effect_bindings = None
    _effect_upload_revisions = None
    _effect_compile_errors: tuple[str, ...] = ()
    _effect_artifact_topology_generation = 0

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def awake(self) -> None:
        """Initialize the component.

        Multiple RenderStacks may exist in a scene, but only one can be active
        at a time. Activation is managed through ``on_enable`` and
        ``on_disable``.
        """
        # Initialize instance-level fields (not serialized), but do NOT
        # stomp values already restored by on_after_deserialize().
        if self._pipeline_param_store is None:
            self._pipeline_param_store = {}
        if self.effect_slots is None:
            self.effect_slots = []
        if self._compiled_effect_bindings is None:
            self._compiled_effect_bindings = []
        if self._effect_upload_revisions is None:
            self._effect_upload_revisions = {}
        self._pipeline_catalog_signature = ()
        self._register_pipeline_catalog_reload()
        self._sync_pipeline_catalog()

        # If no active instance (or existing one is stale), self-promote
        # provided this component is enabled.
        existing = RenderStack.instance()
        if existing is not None and existing is not self:
            if RenderStack._is_effectively_active(existing):
                # Another valid RenderStack is active; stay dormant.
                # on_enable() will take over when this one is enabled.
                return
            # Stale — evict it
            RenderStack._active_instance = None
        if RenderStack._is_effectively_active(self):
            RenderStack._active_instance = self

    def on_destroy(self) -> None:
        """Dispose pipeline resources and promote another active stack if needed."""
        self._unregister_pipeline_catalog_reload()
        was_active = (RenderStack._active_instance is self)
        if was_active:
            RenderStack._active_instance = None
        if self._pipeline is not None and hasattr(self._pipeline, "dispose"):
            self._pipeline.dispose()
        self._pipeline = None
        self._graph_desc = None
        self._resource_bus = None
        self._compiled_effect_bindings = []
        self._effect_upload_revisions = {}
        self._effect_artifact_topology_generation = 0
        if was_active:
            self._promote_next_stack()

    def on_enable(self) -> None:
        """Become the active RenderStack when enabled."""
        if RenderStack._is_effectively_active(self):
            RenderStack._active_instance = self
            self.invalidate_graph()

    def on_disable(self) -> None:
        """Release active ownership and promote another enabled RenderStack."""
        if RenderStack._active_instance is self:
            RenderStack._active_instance = None
            self._promote_next_stack()
        self._graph_desc = None

    # ------------------------------------------------------------------
    # Singleton promotion
    # ------------------------------------------------------------------

    def _promote_next_stack(self) -> None:
        """Scan the scene for another enabled RenderStack and promote it."""
        try:
            from Infernux.lib import SceneManager as _NativeSceneManager
            scene = _NativeSceneManager.instance().get_active_scene()
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return
        if scene is None:
            return
        for obj in scene.get_all_objects():
            if not obj.is_active_in_hierarchy():
                continue
            for comp in obj.get_py_components():
                if (isinstance(comp, RenderStack)
                        and comp is not self
                        and RenderStack._is_effectively_active(comp)):
                    RenderStack._active_instance = comp
                    comp.invalidate_graph()
                    return

    # ------------------------------------------------------------------
    # Serialization hooks
    # ------------------------------------------------------------------

    def on_before_serialize(self) -> None:
        """Persist the current pipeline parameter snapshot."""
        self._save_current_pipeline_params()
        self.pipeline_params_json = _json.dumps(self._pipeline_param_store)

    def _deserialize_fields_document(
        self, data: dict, *, _skip_on_after_deserialize: bool = False
    ) -> None:
        if not isinstance(data, dict):
            raise TypeError("RenderStack fields document must be an object")
        obsolete = {"effect_stage_bindings_json", "mounted_passes_json"}.intersection(data)
        if obsolete:
            raise ValueError(
                "RenderStack contains removed fields: " + ", ".join(sorted(obsolete))
            )
        super()._deserialize_fields_document(
            data,
            _skip_on_after_deserialize=_skip_on_after_deserialize,
        )

    def on_after_deserialize(self) -> None:
        """Restore the canonical pipeline and EffectStage state."""
        # Register as the active instance so that the fast-path in
        # RenderStackPipeline._find_render_stack works even in edit mode
        # (where awake() is not called).
        if RenderStack.instance() is None and RenderStack._is_effectively_active(self):
            RenderStack._active_instance = self

        if self._pipeline_param_store is None:
            self._pipeline_param_store = {}

        self._normalize_effect_slots()

        if self.pipeline_params_json:
            data = _json.loads(self.pipeline_params_json)
            if type(data) is not dict:
                raise TypeError("RenderStack pipeline parameters must be an object")
            self._pipeline_param_store = data

        # Deserialization may be repeated on an existing editor component.
        # Recreate the selected pipeline only after its parameter store exists.
        self._pipeline = None
        self._topology_probe_cache = None
        self._cached_ips = None

    # ==================================================================
    # Pipeline management
    # ==================================================================

    @property
    def effect_compile_errors(self) -> tuple[str, ...]:
        """Current non-destructive diagnostics for mounted Effect assets."""
        return self._effect_compile_errors

    @property
    def effect_stages(self):
        """Pipeline-declared EffectStages in topology order."""
        return tuple(self._build_full_topology_probe().effect_stages)

    @property
    def orphan_effect_slots(self):
        """Saved slots whose stage was removed from the current pipeline."""
        stages = self.effect_stages
        return tuple(
            slot
            for slot in (self.effect_slots or ())
            if not any(stage.stable_id == slot.stage_id for stage in stages)
        )

    def _resolve_effect_stage(self, stage_id: str):
        from Infernux.renderstack.effect_stage import validate_effect_stage_id

        normalized_id = validate_effect_stage_id(stage_id)
        for stage in self.effect_stages:
            if stage.stable_id == normalized_id:
                return stage
        valid = [stage.stable_id for stage in self.effect_stages]
        raise ValueError(
            f"pipeline '{self.pipeline.name}' does not declare EffectStage "
            f"{normalized_id!r}; valid stages: {valid}"
        )

    def get_effect_stage_slots(self, stage_id: str):
        """Return the ordered structured slots for one stable EffectStage."""
        stage = self._resolve_effect_stage(stage_id)
        return tuple(
            slot for slot in (self.effect_slots or ()) if stage.stable_id == slot.stage_id
        )

    def set_effect_stage_slots(self, stage_id: str, slots) -> None:
        """Replace one stage list while preserving all other stage bindings."""
        stage = self._resolve_effect_stage(stage_id)
        replacement = []
        for slot in slots:
            if not isinstance(slot, EffectSlot):
                raise TypeError("RenderStack stage slots must be EffectSlot values")
            slot.stage_id = stage.stable_id
            replacement.append(slot)
        self.effect_slots = [
            slot for slot in self.effect_slots if stage.stable_id != slot.stage_id
        ] + replacement
        self._normalize_effect_slots()
        self.invalidate_graph()

    def add_effect_slot(self, stage_id: str, effect=None, *, enabled: bool = True) -> EffectSlot:
        """Append a serializable Effect asset slot to one pipeline stage."""
        stage = self._resolve_effect_stage(stage_id)
        slot = EffectSlot(
            stage_id=stage.stable_id,
            effect=effect,
            enabled=enabled,
        )
        self.effect_slots = [*self.effect_slots, slot]
        self.invalidate_graph()
        return slot

    def get_effect(self, stage_id: str, index: int = 0):
        """Resolve one mounted Effect for ordinary runtime scripts."""
        slots = self.get_effect_stage_slots(stage_id)
        if index < 0 or index >= len(slots):
            return None
        return slots[index].effect

    def remap_orphan_effect_stage(self, old_stage_id: str, new_stage_id: str) -> int:
        """Move preserved orphan slots onto one currently declared stage."""
        from Infernux.renderstack.effect_stage import validate_effect_stage_id

        old_id = validate_effect_stage_id(old_stage_id)
        if any(stage.stable_id == old_id for stage in self.effect_stages):
            raise ValueError(f"EffectStage {old_id!r} is declared and is not orphaned")
        target = self._resolve_effect_stage(new_stage_id)
        remapped = 0
        for slot in self.effect_slots or ():
            if slot.stage_id == old_id:
                slot.stage_id = target.stable_id
                remapped += 1
        if remapped:
            self._normalize_effect_slots()
            self.invalidate_graph()
        return remapped

    def _normalize_effect_slots(self) -> None:
        import uuid
        from Infernux.renderstack.effect_stage import validate_effect_stage_id

        slot_ids = set()
        for slot in self.effect_slots or []:
            if not isinstance(slot, EffectSlot):
                raise TypeError("RenderStack.effect_slots must contain EffectSlot values")
            slot.stage_id = validate_effect_stage_id(slot.stage_id)
            if not slot.slot_id:
                slot.slot_id = uuid.uuid4().hex
            if slot.slot_id in slot_ids:
                raise ValueError(f"duplicate effect slot_id: {slot.slot_id!r}")
            slot_ids.add(slot.slot_id)

    @staticmethod
    def discover_pipelines() -> Dict[str, type]:
        """Discover all RenderPipeline subclasses in the project.

        Returns:
            A mapping of ``{display_name: class}``.
        """
        from Infernux.renderstack.discovery import discover_pipelines

        return discover_pipelines()

    def set_pipeline(self, pipeline_class_name: str) -> None:
        """Switch the active render pipeline.

        Pass an empty string to use the default pipeline.
        """
        if self.pipeline_class_name == pipeline_class_name:
            return
        self._save_current_pipeline_params()
        self.pipeline_class_name = pipeline_class_name
        self._pipeline = None
        self._cached_ips = None
        self.invalidate_graph()

    @property
    def pipeline(self):  # -> RenderPipeline
        """Current pipeline instance, created lazily."""
        if self._pipeline is None:
            self._pipeline = self._create_pipeline()
            self._restore_pipeline_params(self._pipeline)
            # Wire back-reference so pipeline param changes can
            # invalidate the graph via self._render_stack.
            if hasattr(self._pipeline, '_render_stack'):
                self._pipeline._render_stack = self
        return self._pipeline

    # ==================================================================
    # Graph construction
    # ==================================================================

    def _build_full_topology_probe(self):
        """Return a RenderGraph with the pipeline-defined topology.

        Used by the common Inspector model to display the same sequence the
        pipeline explicitly defines.
        """
        if self._topology_probe_cache is not None:
            return self._topology_probe_cache

        from Infernux.rendergraph.graph import RenderGraph
        g = RenderGraph("_FullTopologyProbe")
        self.pipeline.define_topology(g)
        # Keep the inspector probe consistent with build(): post-process
        # injection points are guaranteed to exist even when Screen UI is off.
        ensure_standard_post_process_points(g)
        self._topology_probe_cache = g
        return g

    def invalidate_graph(self) -> None:
        """Mark the graph as needing a rebuild.

        This is called automatically after effect or pipeline changes.
        """
        self._graph_desc = None
        self._build_failed = False  # allow retry after explicit invalidation
        self._topology_probe_cache = None
        self._compiled_effect_bindings = []
        self._effect_upload_revisions = {}

    def build_graph(self):  # -> RenderGraphDescription
        """Build the complete RenderGraph.

        The pipeline defines topology and EffectStages. Structured EffectSlot
        assets are compiled only at those stages before the graph is built.

        Returns:
            Compiled ``RenderGraphDescription`` ready for
            ``context.apply_graph()``.
        """
        from Infernux.rendergraph.graph import RenderGraph

        graph = RenderGraph("Pipeline+Stack")
        bus = ResourceBus()
        self._resource_bus = bus
        compiled_effects = []
        effect_errors = []

        def on_effect_stage(stage) -> None:
            from Infernux.renderstack.render_effect_compiler import compile_effect_slots

            semantic_resources = graph.current_effect_resources
            for resource_name in stage.contract.inputs:
                resource = semantic_resources.get(resource_name)
                if resource is None:
                    resource = graph.get_texture(resource_name)
                if resource is not None:
                    bus.set(resource_name, resource)

            stage_color = bus.get(COLOR_TEXTURE)
            bindings, errors = compile_effect_slots(
                stage,
                self.get_effect_stage_slots(stage.stable_id),
                graph,
                bus,
            )
            compiled_effects.extend(bindings)
            effect_errors.extend(errors)

            effect_color = bus.get(COLOR_TEXTURE)
            if (stage_color is not None
                    and effect_color is not None
                    and effect_color is not stage_color):
                with graph.name_scope(f"effects/{stage.stable_id}"):
                    with graph.add_pass("Commit") as render_pass:
                        render_pass.set_texture("_SourceTex", effect_color)
                        render_pass.write_color(stage_color)
                        render_pass.fullscreen_quad("fullscreen_blit")
                bus.set(COLOR_TEXTURE, stage_color)

        graph._effect_stage_callback = on_effect_stage

        # Pipeline populates graph with passes + injection points
        self.pipeline.define_topology(graph)
        from Infernux.renderstack.render_effect_compiler import (
            RenderEffectArtifactRegistry,
        )

        self._effect_artifact_topology_generation = (
            RenderEffectArtifactRegistry.topology_generation()
        )
        self._compiled_effect_bindings = compiled_effects
        self._effect_compile_errors = tuple(effect_errors)
        self._effect_upload_revisions = {}

        # Ensure before/after_post_process injection points exist WHILE the
        # callback is still active. graph.build() also auto-injects these,
        # but that happens after the callback is detached — effects targeting
        # these points would never be injected.  Calling injection_point()
        # here triggers the callback so mounted effects are properly inserted.
        ensure_standard_post_process_points(graph)

        # Validate: no injection point before first pass
        graph.validate_no_ip_before_first_pass()

        # If post-processing effects redirected "color" to a different
        # texture, blit the result back to the original camera target
        # (backbuffer) so it gets presented to the screen.
        original_color = graph.get_texture(COLOR_TEXTURE)
        final_color = bus.get(COLOR_TEXTURE)
        if (final_color is not None
                and original_color is not None
                and final_color is not original_color):
            # Move _ScreenUI_Overlay (if present) so it renders AFTER the
            # final blit — otherwise the blit overwrites the overlay UI.
            overlay_pass = graph.remove_pass("_ScreenUI_Overlay")

            with graph.add_pass("_FinalCompositeBlit") as p:
                p.set_texture("_SourceTex", final_color)
                p.write_color(original_color)
                p.fullscreen_quad("fullscreen_blit")

            # Re-append overlay after the blit
            if overlay_pass is not None:
                graph.append_pass(overlay_pass)

            graph.set_output(original_color)
        elif final_color is not None:
            graph.set_output(final_color)
        elif graph._output is None:
            # Only override if the pipeline didn't call set_output() itself.
            # Pipelines that use non-standard output names (e.g. "final")
            # will have already set _output inside define_topology().
            graph.set_output(COLOR_TEXTURE)
        # else: pipeline already called graph.set_output() — respect it.

        return graph.build()

    def render(self, context, camera) -> None:
        """Per-frame render entry point invoked by RenderStackPipeline.

        Lazy-builds the graph on first call or after invalidation,
        then applies the compiled graph and submits culling results.

        Args:
            context: The render context provided by the engine.
            camera: The camera to render from.
        """
        if self._graph_desc is not None:
            from Infernux.renderstack.render_effect_compiler import (
                RenderEffectArtifactRegistry,
            )

            if (
                RenderEffectArtifactRegistry.topology_generation()
                != self._effect_artifact_topology_generation
            ):
                self.invalidate_graph()

        if self._graph_desc is not None:
            requires_rebuild, updates = self._collect_effect_parameter_updates(context)
            if requires_rebuild:
                self.invalidate_graph()
            elif updates:
                context.update_parameter_blocks(updates)

        # Lazy build graph topology (skip if last build failed)
        if self._graph_desc is None and not self._build_failed:
            context.setup_camera_properties(camera)
            culling = context.cull(camera)
            try:
                self._graph_desc = self.build_graph()
            except Exception as exc:
                self._graph_desc = self._fallback_on_build_failure(exc)

            if self._graph_desc is None:
                # Build failed and fallback also failed; skip rendering
                # until hot-reload fixes it.
                self._build_failed = True
                context.submit_culling(culling)
                return

            try:
                context.apply_graph(self._graph_desc)
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_error(
                    f"[RenderStack] apply_graph failed: {exc}. "
                    f"Attempting fallback pipeline."
                )
                self._graph_desc = self._fallback_on_build_failure(exc)
                if self._graph_desc is None:
                    self._build_failed = True
                    context.submit_culling(culling)
                    return
                try:
                    context.apply_graph(self._graph_desc)
                except Exception as exc2:
                    from Infernux.debug import Debug
                    Debug.log_error(
                        f"[RenderStack] Fallback apply_graph also failed: "
                        f"{exc2}. Rendering disabled until hot-reload."
                    )
                    self._graph_desc = None
                    self._build_failed = True
                    context.submit_culling(culling)
                    return

            context.submit_culling(culling)
        elif self._graph_desc is not None:
            # Steady state sends only a revision integer. A second camera or a
            # rebuilt native graph falls back to the full description once.
            if not context.render_compiled(camera, self._graph_desc.source_revision):
                context.render_with_graph(camera, self._graph_desc)

    def _collect_effect_parameter_updates(self, context):
        bindings = self._compiled_effect_bindings or ()
        if not bindings:
            return False, []
        graph_id = int(getattr(context, "graph_instance_id", 0) or 0)
        if self._effect_upload_revisions is None:
            self._effect_upload_revisions = {}
        updates = []
        for binding in bindings:
            revision_key = (graph_id, binding.binding_id)
            revision = binding.source.revision
            if self._effect_upload_revisions.get(revision_key) == revision:
                continue
            try:
                requires_rebuild, binding_updates = binding.collect_updates()
            except (TypeError, ValueError) as exc:
                diagnostic = f"{binding.binding_id}: {exc}"
                self._effect_compile_errors = tuple(
                    dict.fromkeys((*self._effect_compile_errors, diagnostic))
                )
                self._effect_upload_revisions[revision_key] = revision
                continue
            if requires_rebuild:
                return True, []
            updates.extend(binding_updates)
            self._effect_upload_revisions[revision_key] = revision
            diagnostic_prefix = f"{binding.binding_id}: "
            self._effect_compile_errors = tuple(
                error
                for error in self._effect_compile_errors
                if not error.startswith(diagnostic_prefix)
            )
        return False, updates

    # ==================================================================
    # Private helpers
    # ==================================================================

    def _fallback_on_build_failure(self, exc: Exception):
        """Log the error and attempt to fall back to DefaultForwardPipeline.

        Returns:
            A ``RenderGraphDescription`` built from the default pipeline,
            or ``None`` if the fallback also fails.
        """
        from Infernux.debug import Debug
        pipeline_name = getattr(self._pipeline, 'name', '?')
        Debug.log_error(
            f"[RenderStack] Pipeline '{pipeline_name}' build failed: {exc}. "
            f"Falling back to DefaultForwardPipeline."
        )

        # If already on the default pipeline, nothing left to try.
        from Infernux.renderstack.default_forward_pipeline import (
            DefaultForwardPipeline,
        )
        if isinstance(self._pipeline, DefaultForwardPipeline):
            Debug.log_error(
                "[RenderStack] DefaultForwardPipeline itself failed — "
                "cannot recover."
            )
            return None

        # Switch to default pipeline and retry once.
        self._pipeline = DefaultForwardPipeline()
        self._pipeline._render_stack = self
        self._cached_ips = None
        try:
            return self.build_graph()
        except Exception as fallback_exc:
            Debug.log_error(
                f"[RenderStack] Fallback pipeline also failed: {fallback_exc}"
            )
            return None

    def _create_pipeline(self):  # -> RenderPipeline
        """Instantiate the pipeline selected by ``pipeline_class_name``.

        Empty or unknown names fall back to ``DefaultForwardPipeline``. The
        pipeline source file is also registered for hot-reload callbacks.
        """
        import inspect, os
        from Infernux.renderstack.default_forward_pipeline import (
            DefaultForwardPipeline,
        )

        if not self.pipeline_class_name:
            self._unregister_pipeline_reload()
            return DefaultForwardPipeline()

        pipelines = self.discover_pipelines()
        cls = pipelines.get(self.pipeline_class_name)
        if cls is None:
            warnings.warn(
                f"[RenderStack] Pipeline '{self.pipeline_class_name}' "
                f"not found. Available: {list(pipelines.keys())}. "
                f"Falling back to DefaultForwardPipeline.",
                stacklevel=2,
            )
            self.pipeline_class_name = ""
            self._unregister_pipeline_reload()
            return DefaultForwardPipeline()

        pipeline = cls()
        # Register watchdog callback for hot-reload
        self._register_pipeline_reload(cls)
        return pipeline

    def _pipeline_key(self, pipeline_name: str) -> str:
        return pipeline_name if pipeline_name else "__default__"

    def _save_current_pipeline_params(self) -> None:
        if self._pipeline_param_store is None:
            self._pipeline_param_store = {}
        if self._pipeline is None:
            return
        try:
            from Infernux.components.serialized_field import get_serialized_fields
            from enum import Enum

            key = self._pipeline_key(self.pipeline_class_name)
            fields = get_serialized_fields(self._pipeline.__class__)
            params = {}
            for field_name in fields.keys():
                value = getattr(self._pipeline, field_name, None)
                if isinstance(value, Enum):
                    params[field_name] = {"__enum_name__": value.name}
                else:
                    params[field_name] = value
            self._pipeline_param_store[key] = params
        except (ImportError, RuntimeError, AttributeError) as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return

    def _restore_pipeline_params(self, pipeline) -> None:
        if self._pipeline_param_store is None:
            self._pipeline_param_store = {}
        try:
            from Infernux.components.serialized_field import get_serialized_fields, FieldType
        except ImportError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return

        key = self._pipeline_key(self.pipeline_class_name)
        saved = self._pipeline_param_store.get(key)
        if not isinstance(saved, dict):
            return

        fields = get_serialized_fields(pipeline.__class__)
        pipeline._inf_deserializing = True
        try:
            for field_name, meta in fields.items():
                if field_name not in saved:
                    continue
                value = saved[field_name]
                try:
                    if meta.field_type == FieldType.ENUM and isinstance(value, dict) and "__enum_name__" in value:
                        enum_name = value.get("__enum_name__", "")
                        enum_cls = meta.enum_type
                        if enum_cls is not None and enum_name in enum_cls.__members__:
                            setattr(pipeline, field_name, enum_cls[enum_name])
                            continue
                    setattr(pipeline, field_name, value)
                except (AttributeError, TypeError, ValueError) as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    continue
        finally:
            pipeline._inf_deserializing = False

