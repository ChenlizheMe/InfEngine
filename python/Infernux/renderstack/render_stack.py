"""
RenderStack — Scene-level rendering configuration component.

RenderStack is a scene-singleton InxComponent that manages:
- The active RenderPipeline (topology skeleton + injection points)
- All mounted RenderPass instances (user effects + built-in passes)
- Graph construction: combines pipeline topology with injected passes

Architecture::

    RenderStack (InxComponent, scene singleton)
      ├── selected_pipeline: RenderPipeline  (defines topology skeleton)
      └── pass_entries: List[PassEntry]      (user-mounted passes)

    Each frame:
      1. RenderStack.render(context, camera)
      2. Lazy-build graph if invalidated
      3. context.apply_graph(desc) + context.submit_culling(culling)

Build flow (Section 7.1)::

    graph = RenderGraph("Pipeline+Stack")
    bus = ResourceBus()
    pipeline.define_topology(graph, bus, callback)
      └── callback triggers _inject_passes_at for each injection point
    graph.set_output(bus.get("color"))
    graph.build() → RenderGraphDescription

Usage::

    # In a scene setup script
    stack = game_object.add_component(RenderStack)
    stack.set_pipeline("Default Forward")
    stack.add_pass(BloomPass())
"""

from __future__ import annotations

import json as _json
import sys
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from Infernux.components.component import InxComponent
from Infernux.components.serialized_field import FieldType, list_field, serialized_field
from Infernux.components.decorators import disallow_multiple, add_component_menu
from Infernux.renderstack._pipeline_common import (
    COLOR_TEXTURE,
    ensure_standard_post_process_points,
)
from Infernux.renderstack.injection_point import InjectionPoint
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.resource_bus import ResourceBus

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.render_pass import RenderPass


@dataclass
class PassEntry:
    """Persistent data for a mounted RenderStack pass slot."""

    render_pass: "RenderPass"
    enabled: bool = True
    order: int = 0
    # injection_point is read from render_pass.injection_point.


from ._render_pass_mgmt import RenderPassManagementMixin
from ._render_pipeline_reload import PipelineReloadMixin

@disallow_multiple
@add_component_menu("Rendering/RenderStack")
class RenderStack(RenderPassManagementMixin, PipelineReloadMixin, InxComponent):
    """Scene-level rendering configuration component.

    Manages the active RenderPipeline and all mounted RenderPass instances for
    the current scene. At most one RenderStack can be active at a time.

    Attributes:
        pipeline_class_name: Selected pipeline class name. Empty means the
            default pipeline.
        pass_entries: Mounted pass list.
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
    mounted_passes_json: str = ""   # Persisted pass configuration.
    pipeline_params_json: str = ""  # Persisted pipeline parameter snapshot.
    effect_slots: list = list_field(
        element_type=FieldType.SERIALIZABLE_OBJECT,
        element_class=EffectSlot,
        default=[],
        tooltip="Ordered Effect assets mounted into pipeline stages.",
    )
    # Read-only migration source for scenes saved by the short-lived JSON
    # binding prototype. New scenes always persist ``effect_slots``.
    effect_stage_bindings_json: str = serialized_field(default="", hidden=True)

    # ---- Runtime state (not serialized) ----
    _pipeline = None  # Optional[RenderPipeline]
    _graph_desc = None  # cached RenderGraphDescription
    _resource_bus: Optional[ResourceBus] = None
    _build_failed: bool = False  # True after a build error; cleared by invalidate_graph()
    _pipeline_module = None  # module object for watchdog hot-reload subscription
    _pass_entries: List[PassEntry] = None  # initialized properly in awake()
    _pipeline_param_store: Dict[str, Dict[str, object]] = None
    _pipeline_catalog_signature: tuple = ()
    _topology_probe_cache = None
    _effect_binding_error: str = ""
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
        if self._pass_entries is None:
            self._pass_entries = []
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
    # Custom inspector
    # ------------------------------------------------------------------

    def on_inspector_gui(self, ctx) -> None:
        """Render the RenderStack custom inspector panel."""
        from Infernux.engine.ui.inspector_renderstack import render_renderstack_inspector
        render_renderstack_inspector(ctx, self)

    # ------------------------------------------------------------------
    # Serialization hooks
    # ------------------------------------------------------------------

    def on_before_serialize(self) -> None:
        """Save pass_entries into mounted_passes_json."""
        self._canonicalize_effect_stage_aliases()
        self._save_current_pipeline_params()
        entries = []
        for e in self._pass_entries:
            entry_data = {
                "class": type(e.render_pass).__name__,
                "enabled": e.enabled,
                "order": e.order,
            }
            # FullScreenEffect: also persist tuneable parameters
            from Infernux.renderstack.fullscreen_effect import FullScreenEffect
            if isinstance(e.render_pass, FullScreenEffect):
                entry_data["params"] = e.render_pass.get_params_dict()
            entries.append(entry_data)
        # Only overwrite when we actually have runtime entries; when
        # _pass_entries is empty (e.g. discover_passes() failed) preserve the
        # existing serialised data so the play-mode snapshot keeps the values.
        if entries:
            self.mounted_passes_json = _json.dumps(entries)
        self.pipeline_params_json = _json.dumps(self._pipeline_param_store) if self._pipeline_param_store else ""
        if self.effect_slots and not self._effect_binding_error:
            self.effect_stage_bindings_json = ""

    def on_after_deserialize(self) -> None:
        """Recreate pass_entries from mounted_passes_json."""
        # Register as the active instance so that the fast-path in
        # RenderStackPipeline._find_render_stack works even in edit mode
        # (where awake() is not called).
        if RenderStack.instance() is None and RenderStack._is_effectively_active(self):
            RenderStack._active_instance = self

        # Ensure _pass_entries is initialized (may be called before awake())
        if self._pass_entries is None:
            self._pass_entries = []
        else:
            # Scene / project reopen can invoke deserialization multiple times
            # during object reconstruction. Always rebuild from JSON instead of
            # appending onto previously restored runtime state.
            self._pass_entries.clear()
        if self._pipeline_param_store is None:
            self._pipeline_param_store = {}

        self._effect_binding_error = ""
        if not self.effect_slots and self.effect_stage_bindings_json:
            try:
                self._migrate_legacy_effect_bindings()
            except (TypeError, ValueError, _json.JSONDecodeError) as exc:
                self._effect_binding_error = str(exc)
        self._normalize_effect_slots()

        if self.pipeline_params_json:
            try:
                data = _json.loads(self.pipeline_params_json)
                if isinstance(data, dict):
                    self._pipeline_param_store = data
            except (ValueError, _json.JSONDecodeError):
                self._pipeline_param_store = {}

        # Deserialization may be repeated on an existing editor component.
        # Recreate the selected pipeline only after its parameter store exists.
        self._pipeline = None
        self._topology_probe_cache = None
        self._cached_ips = None
        self._canonicalize_effect_stage_aliases()

        if not self.mounted_passes_json:
            return
        from Infernux.renderstack.discovery import discover_passes

        all_passes = discover_passes()
        items = _json.loads(self.mounted_passes_json)
        restored_keys = set()
        for item in items:
            cls_name = item.get("class", "")
            cls = all_passes.get(cls_name)
            if cls is None:
                # Also try name→class mapping by class __name__
                for pcls in all_passes.values():
                    if pcls.__name__ == cls_name:
                        cls = pcls
                        break
            if cls is None:
                print(
                    f"[RenderStack] Cannot restore pass '{cls_name}' "
                    f"— class not found.",
                    file=sys.stderr,
                )
                continue
            inst = cls()
            # FullScreenEffect: restore tuneable parameters
            from Infernux.renderstack.fullscreen_effect import FullScreenEffect
            if isinstance(inst, FullScreenEffect) and "params" in item:
                inst.set_params_dict(item["params"])
            entry = PassEntry(
                render_pass=inst,
                enabled=item.get("enabled", True),
                order=item.get("order", 0),
            )
            inst.enabled = entry.enabled
            key = (inst.injection_point, inst.name)
            if key in restored_keys:
                continue
            restored_keys.add(key)
            self._pass_entries.append(entry)

        # Validate injection points (warn only — don't drop entries, because
        # the pipeline might not be loaded yet or may change later).
        if self._pass_entries:
            try:
                valid_points = {p.name for p in self.injection_points}
                for entry in self._pass_entries:
                    ip = entry.render_pass.injection_point
                    if ip not in valid_points:
                        print(
                            f"[RenderStack] Restored pass '{entry.render_pass.name}' "
                            f"has unknown injection_point '{ip}'. "
                            f"Valid: {sorted(valid_points)}",
                            file=sys.stderr,
                        )
            except (RuntimeError, AttributeError) as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass  # pipeline not available yet — skip validation
            self.invalidate_graph()

    # ==================================================================
    # Pipeline management
    # ==================================================================

    @property
    def effect_binding_error(self) -> str:
        """Return a persistent parse diagnostic without discarding source data."""
        return self._effect_binding_error

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
            if not any(stage.accepts_id(slot.stage_id) for stage in stages)
        )

    def _resolve_effect_stage(self, stage_id: str):
        from Infernux.renderstack.effect_stage import validate_effect_stage_id

        normalized_id = validate_effect_stage_id(stage_id)
        for stage in self.effect_stages:
            if stage.accepts_id(normalized_id):
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
            slot for slot in (self.effect_slots or ()) if stage.accepts_id(slot.stage_id)
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
            slot for slot in self.effect_slots if not stage.accepts_id(slot.stage_id)
        ] + replacement
        self._normalize_effect_slots()
        self._effect_binding_error = ""
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
        if any(stage.accepts_id(old_id) for stage in self.effect_stages):
            raise ValueError(f"EffectStage {old_id!r} is declared and is not orphaned")
        target = self._resolve_effect_stage(new_stage_id)
        remapped = 0
        for slot in self.effect_slots or ():
            if slot.stage_id == old_id:
                slot.stage_id = target.stable_id
                remapped += 1
        if remapped:
            self._normalize_effect_slots()
            self._effect_binding_error = ""
            self.invalidate_graph()
        return remapped

    def _migrate_legacy_effect_bindings(self) -> None:
        from Infernux.core.asset_ref import RenderEffectRef
        from Infernux.renderstack.effect_binding import parse_effect_binding_document

        document = parse_effect_binding_document(self.effect_stage_bindings_json)
        migrated = []
        for stage_id, slots in document.stages.items():
            for old_slot in slots:
                reference = old_slot.asset
                effect_ref = RenderEffectRef(
                    guid=reference.guid if reference is not None else "",
                    path_hint=reference.path_hint if reference is not None else "",
                )
                migrated.append(
                    EffectSlot(
                        slot_id=old_slot.slot_id,
                        stage_id=stage_id,
                        effect=effect_ref,
                        enabled=old_slot.enabled,
                    )
                )
        self.effect_slots = migrated
        self.effect_stage_bindings_json = ""

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

    def _canonicalize_effect_stage_aliases(self) -> None:
        """Rewrite known migration aliases while preserving true orphans."""
        try:
            stages = self.effect_stages
        except (AttributeError, ImportError, RuntimeError, ValueError):
            return
        for slot in self.effect_slots or ():
            for stage in stages:
                if stage.accepts_id(slot.stage_id):
                    slot.stage_id = stage.stable_id
                    break

    # ==================================================================

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
    # Pass management
    # ==================================================================

    # ==================================================================
    # Graph construction
    # ==================================================================

    def _build_full_topology_probe(self):
        """Return a RenderGraph with the pipeline-defined topology.

        Used by ``injection_points`` and the inspector renderer to display
        the same sequence the pipeline explicitly defines.
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

        This is called automatically after pass changes and pipeline switches.
        """
        self._graph_desc = None
        self._build_failed = False  # allow retry after explicit invalidation
        self._topology_probe_cache = None
        self._compiled_effect_bindings = []
        self._effect_upload_revisions = {}

    def build_graph(self):  # -> RenderGraphDescription
        """Build the complete RenderGraph.

        Steps:
            1. Create ``RenderGraph("Pipeline+Stack")``
            2. Install the injection callback for mounted passes
            3. Call ``pipeline.define_topology(graph)``
            4. Validate that no injection point appears before the first pass
            5. Set the final graph output
            6. Build the graph description

        Returns:
            Compiled ``RenderGraphDescription`` ready for
            ``context.apply_graph()``.
        """
        from Infernux.rendergraph.graph import RenderGraph

        # Guard: ensure pass_entries is initialized even if awake() hasn't run yet
        if self._pass_entries is None:
            self._pass_entries = []

        graph = RenderGraph("Pipeline+Stack")
        bus = ResourceBus()
        self._resource_bus = bus
        compiled_effects = []
        effect_errors = []

        # Callback: invoked every time pipeline calls graph.injection_point()
        def on_injection_point(point_name: str) -> None:
            # Sync bus with all graph textures (add any new ones)
            for tex in graph._textures:
                if not bus.has(tex.name):
                    bus.set(tex.name, tex)
            self._inject_passes_at(point_name, graph, bus)

        graph._injection_callback = on_injection_point

        def on_effect_stage(stage) -> None:
            from Infernux.renderstack.render_effect_compiler import compile_effect_slots

            for resource_name in stage.contract.inputs:
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
        # Guard: ensure pass_entries is initialized even if awake() hasn't run yet
        if self._pass_entries is None:
            self._pass_entries = []

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

    def _inject_passes_at(
        self,
        point_name: str,
        graph: "RenderGraph",
        bus: ResourceBus,
    ) -> None:
        """Inject all enabled passes for a given injection point in order.

        Responsibilities:
            1. Gather pass entries for the injection point in order
            2. Validate resource requirements for each pass
            3. Call ``pass.inject(graph, bus)``

        Args:
            point_name: Injection point name.
            graph: RenderGraph currently being built.
            bus: Resource bus.
        """
        entries = self.get_passes_at(point_name)
        enabled = [e for e in entries if e.enabled]

        if not enabled:
            return

        for entry in enabled:
            rp = entry.render_pass

            # Validate resource requirements before injection
            errors = rp.validate(bus.available_resources)
            if errors:
                for err in errors:
                    print(f"[RenderStack] {err}", file=sys.stderr)
                print(
                    f"[RenderStack] Skipping pass '{rp.name}' at "
                    f"'{point_name}' due to validation errors.",
                    file=sys.stderr,
                )
                continue

            # Warn on creates collision
            for res_name in rp.creates:
                if bus.has(res_name):
                    warnings.warn(
                        f"[RenderStack] Pass '{rp.name}' creates "
                        f"resource '{res_name}' which already exists "
                        f"in bus. It will be overwritten.",
                        stacklevel=2,
                    )

            rp.inject(graph, bus)


