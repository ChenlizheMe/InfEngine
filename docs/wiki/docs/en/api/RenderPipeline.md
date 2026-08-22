# RenderPipeline

<div class="class-info">
class in <b>Infernux.renderstack</b>
</div>

## Description

Base class for scriptable render pipelines.

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

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Constructors

| Signature | Description |
|------|------|
| `RenderPipeline.__init__() → None` |  |

<!-- USER CONTENT START --> constructors

<!-- USER CONTENT END -->

## Properties

| Name | Type | Description |
|------|------|------|
| name | `str` | Display name for Editor UI and pipeline discovery. *(read-only)* |

<!-- USER CONTENT START --> properties

<!-- USER CONTENT END -->

## Public Methods

| Method | Description |
|------|------|
| `require_buffer(semantic: str) → BufferHandle` |  |
| `sample_buffer(result: PassResult, semantic: str | BufferHandle) → Any` |  |
| `publish_result(source: str, buffers: Dict[str, Any]) → PassResult` |  |
| `write_buffer(result: PassResult, semantic: str | BufferHandle, texture: Any, source: str) → PassResult` |  |
| `geometry_stage(graph: RenderGraph, source: str, phase: GeometryStagePhase | str = ..., buffers: Dict[str, Any], queue_range: tuple[int, int], msaa_samples: int = ..., sort_mode: str = ..., clear: bool = ...) → PassResult` |  |
| `render(context: Any, camera: Any) → None` | Render one camera through its dedicated context and RenderView. |
| `should_render_camera(camera: Any) → bool` | Decide whether *camera* should be rendered this frame. |
| `render_camera(context: Any, camera: Any, culling: Any) → None` | Per-camera render hook. |
| `define_topology(graph: RenderGraph) → None` | Define the rendering topology on *graph*. |
| `define(pipeline: Any) → None` | Declare a low-nesting pipeline topology. |
| `dispose() → None` | Override to release resources when the pipeline is replaced. |

<!-- USER CONTENT START --> public_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.4. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
