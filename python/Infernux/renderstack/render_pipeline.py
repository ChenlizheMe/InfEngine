"""
Base classes for the Scriptable Render Pipeline.

Users extend RenderPipeline to define custom rendering logic.
RenderPipelineAsset acts as a factory for pipeline instances.

RenderStack integration:
    Subclasses implement ``define_topology(graph)`` to declare passes
    and stable EffectStages inline on the ``RenderGraph``. Legacy injection
    points remain available during migration. The system
    auto-records the topology sequence. ScreenUI/post-process section is
    inserted explicitly by calling ``graph.screen_ui_section()``.

Exposing parameters:
    RenderPipeline supports the same ``serialized_field`` mechanism as
    ``InxComponent``.  Declare class-level attributes and they will be
    collected automatically, shown in the RenderStack inspector, and
    available to ``define_topology()``::

        class MyPipeline(RenderPipeline):
            name = "My Pipeline"
            shadow_resolution: int = serialized_field(default=4096, range=(512, 8192))
            enable_bloom: bool = True
"""

from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

from Infernux.lib import RenderPipelineCallback
from Infernux.renderstack._serialized_field_mixin import SerializedFieldCollectorMixin

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


class RenderPipelineAsset:
    """
    Factory for creating RenderPipeline instances.

    Override ``create_pipeline()`` to return your custom RenderPipeline.

    Usage::

        class MyPipelineAsset(RenderPipelineAsset):
            def create_pipeline(self):
                return MyPipeline()

        engine.set_render_pipeline(MyPipelineAsset())
    """

    def create_pipeline(self) -> "RenderPipeline":
        raise NotImplementedError("Subclass must implement create_pipeline()")


# Reserved attribute names that should never be treated as serialized fields.
_RESERVED_ATTRS = frozenset({
    "name",
})


class RenderPipeline(SerializedFieldCollectorMixin, RenderPipelineCallback):
    """
    Base class for scriptable render pipelines.

    The minimal subclass only needs ``define_topology()`` and optionally
    ``render_camera()`` for per-camera custom logic::

        class MyPipeline(RenderPipeline):
            name = "My Pipeline"

            def define_topology(self, graph):
                graph.create_texture("color", camera_target=True)
                graph.create_texture("depth", format=Format.D32_SFLOAT)
                with graph.add_pass("OpaquePass") as p:
                    p.write_color("color")
                    p.write_depth("depth")
                    p.draw_renderers(queue_range=(0, 2500))
                graph.set_output("color")

    Exposable parameters:
        Use class-level attributes (plain values or ``serialized_field()``)
        just like ``InxComponent``::

            class MyPipeline(RenderPipeline):
                shadow_resolution: int = serialized_field(default=2048, range=(256, 8192))
                enable_ssao: bool = True

        These are collected into ``_serialized_fields_`` and rendered by
        the RenderStack inspector.

    RenderStack integration:
        Subclasses implement ``define_topology(graph)`` to declare passes
        and ``graph.effects(stable_id, ...)`` attachment stages inline. The
        ``RenderGraph`` auto-records the topology sequence; scene RenderStacks
        may only bind effects to those declarations.
    """

    # Display name for Editor UI and discovery. Subclasses should override.
    name: str = "Unnamed Pipeline"

    # Class-level storage for serialized field metadata (same pattern as InxComponent)
    _serialized_fields_: Dict[str, Any] = {}

    _reserved_attrs_ = frozenset({"name"})

    # Optional back-reference to owning RenderStack (set when pipeline is
    # created by RenderStack, used to invalidate_graph on param change).
    _render_stack: Any = None

    # ------------------------------------------------------------------
    # Instance init: set field defaults
    # ------------------------------------------------------------------

    def __init__(self):
        super().__init__()
        self._render_stack = None
        # Initialize serialized fields with defaults
        from Infernux.components.serialized_field import get_serialized_fields
        for field_name, meta in get_serialized_fields(self.__class__).items():
            # If descriptor already provides the default via __get__, skip.
            # But we need to ensure instance storage is primed.
            if not hasattr(self, f"_sf_{field_name}"):
                # Trigger descriptor __set__ so instance value is stored
                try:
                    setattr(self, field_name, meta.default)
                except Exception as e:
                    from Infernux.debug import Debug
                    Debug.log_warning(f"RenderPipeline: failed to set default for '{field_name}': {e}")

    # ==================================================================
    # Standalone render entry point (without RenderStack)
    # ==================================================================

    def render(self, context, cameras):
        """Render all cameras.

        The base implementation builds the graph once from
        ``define_topology()``, filters cameras via ``should_render_camera()``,
        then calls ``render_camera()`` for each accepted camera.

        Override this method entirely if you need fully custom loop logic
        (e.g. multi-pass techniques that interleave multiple cameras).
        For simple camera filtering, override ``should_render_camera()``
        instead.
        """
        from Infernux.rendergraph.graph import RenderGraph

        if not hasattr(self, '_standalone_desc') or self._standalone_desc is None:
            g = RenderGraph(self.name)
            self.define_topology(g)
            self._standalone_desc = g.build()

        for camera in cameras:
            if not self.should_render_camera(camera):
                continue
            context.setup_camera_properties(camera)
            culling = context.cull(camera)
            self.render_camera(context, camera, culling)

    def should_render_camera(self, camera) -> bool:
        """Decide whether *camera* should be rendered this frame.

        Override to filter out specific cameras, e.g. skip editor cameras
        or only render cameras on a certain layer::

            def should_render_camera(self, camera):
                return not camera.is_editor_camera

        Returns ``True`` by default (render all cameras).
        """
        return True

    def render_camera(self, context, camera, culling):
        """Per-camera render hook.

        The default implementation applies the compiled graph and submits
        culling results.  Override to inject custom per-camera logic
        (e.g. per-camera shadow passes, camera-specific post-process).

        Args:
            context: The render context provided by the engine.
            camera: The current camera being rendered.
            culling: Culling results from ``context.cull(camera)``.
        """
        if not context.is_graph_revision_current(self._standalone_desc.source_revision):
            context.apply_graph(self._standalone_desc)
        context.submit_culling(culling)

    def dispose(self):
        """Override to release resources when the pipeline is replaced."""
        if hasattr(self, '_standalone_desc'):
            self._standalone_desc = None

    # ==================================================================
    # RenderStack integration
    # ==================================================================

    def define_topology(self, graph: "RenderGraph") -> None:
        """Define the rendering topology on *graph*.

        Subclass implementation should:
        1. Create textures via ``graph.create_texture(...)``
        2. Add passes via ``graph.add_pass(...)``
        3. Declare user stages via ``graph.effects(...)``
        4. Call ``graph.set_output(...)``

        **Rules**:
        - No injection point before the first pass (validated by system).

        Args:
            graph: The ``RenderGraph`` builder to populate.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement define_topology()"
        )
