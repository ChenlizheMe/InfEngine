"""Type stubs for Infernux.renderstack.render_pipeline — base classes for scriptable render pipelines."""

from __future__ import annotations

from typing import Any, ClassVar, Dict, TYPE_CHECKING
from Infernux.renderstack.pass_result import BufferHandle, PassResult
from Infernux.renderstack.geometry_buffers import GeometryStagePhase

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


class RenderPipelineAsset:
    """Factory for creating ``RenderPipeline`` instances.

    Override ``create_pipeline()`` to return your custom pipeline.

    Example::

        class MyPipelineAsset(RenderPipelineAsset):
            def create_pipeline(self):
                return MyPipeline()

        engine.set_render_pipeline(MyPipelineAsset())
    """

    def create_pipeline(self) -> RenderPipeline:
        """Create and return a new pipeline instance."""
        ...


class RenderPipeline:
    """Base class for scriptable render pipelines.

    The minimal subclass only needs ``define_topology()`` and optionally
    ``render_camera()`` for per-camera custom logic.

    Exposable parameters:
        Use class-level attributes (plain values or ``serialized_field()``)
        just like ``InxComponent``::

            class MyPipeline(RenderPipeline):
                shadow_resolution: int = serialized_field(default=2048, range=(256, 8192))
                enable_ssao: bool = True

    Example::

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
    """

    name: ClassVar[str]
    """Display name for Editor UI and pipeline discovery."""
    _serialized_fields_: Dict[str, Any]

    def __init__(self) -> None: ...
    def require_buffer(self, semantic: str) -> BufferHandle: ...
    def sample_buffer(self, result: PassResult, semantic: str | BufferHandle) -> Any: ...
    def publish_result(self, source: str, buffers: Dict[str, Any]) -> PassResult: ...
    def write_buffer(
        self, result: PassResult, semantic: str | BufferHandle, texture: Any,
        *, source: str,
    ) -> PassResult: ...
    def geometry_stage(
        self, graph: RenderGraph, source: str, *,
        phase: GeometryStagePhase | str = ..., buffers: Dict[str, Any],
        queue_range: tuple[int, int], msaa_samples: int = ...,
        sort_mode: str = ..., clear: bool = ...,
    ) -> PassResult: ...

    def render(self, context: Any, camera: Any) -> None:
        """Render one camera through its dedicated context and RenderView.

        The base implementation builds the graph from ``define_topology()``,
        filters the camera via ``should_render_camera()``, then calls
        ``render_camera()``.

        Args:
            context: The render context provided by the engine.
            camera: The camera owned by this render context.
        """
        ...

    def should_render_camera(self, camera: Any) -> bool:
        """Decide whether *camera* should be rendered this frame.

        Override to filter out specific cameras.  Returns ``True`` by default.
        """
        ...

    def render_camera(self, context: Any, camera: Any, culling: Any) -> None:
        """Per-camera render hook.

        Args:
            context: The render context provided by the engine.
            camera: The current camera being rendered.
            culling: Culling results from ``context.cull(camera)``.
        """
        ...

    def define_topology(self, graph: RenderGraph) -> None:
        """Define the rendering topology on *graph*.

        Subclass implementation should:

        1. Create textures via ``graph.create_texture(...)``
        2. Add passes via ``graph.add_pass(...)``
        3. Declare stable stages via ``graph.effects(...)``
        4. Call ``graph.set_output(...)``

        Args:
            graph: The ``RenderGraph`` builder to populate.
        """
        ...

    def define(self, pipeline: Any) -> None:
        """Declare a low-nesting pipeline topology."""
        ...

    def dispose(self) -> None:
        """Override to release resources when the pipeline is replaced."""
        ...
