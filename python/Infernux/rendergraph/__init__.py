"""
Infernux RenderGraph module.

Provides the Python API used to define render-graph topology.
Python describes pass layout, resource connections, and per-pass actions.
C++ compiles the graph, inserts barriers, and manages transient resources.

Usage::

    from Infernux.rendergraph import RenderGraph, Format

    graph = RenderGraph("ForwardPipeline")

        graph.create_texture("color", camera_target=True)
    graph.create_texture("depth", format=Format.D32_SFLOAT)

    with graph.add_pass("OpaquePass") as p:
        p.write_color("color")
        p.write_depth("depth")
        p.set_clear(color=(0.1, 0.1, 0.1, 1.0), depth=1.0)
        p.draw_renderers(queue_range=(0, 2500), sort_mode="front_to_back")

    with graph.add_pass("SkyboxPass") as p:
        p.read("depth")
        p.write_color("color")
        p.draw_skybox()

    with graph.add_pass("TransparentPass") as p:
        p.read("depth")
        p.write_color("color")
        p.draw_renderers(queue_range=(2501, 5000), sort_mode="back_to_front")

    graph.set_output("color")

    # Apply to the engine's scene render graph
    scene_graph = engine.get_scene_render_graph()
    scene_graph.apply_python_graph(graph.build())
"""

from .graph import BufferHandle, RenderGraph, RenderPassBuilder, TextureHandle, Format
from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline

__all__ = [
    "RenderGraph",
    "RenderPassBuilder",
    "TextureHandle",
    "BufferHandle",
    "Format",
    "DefaultForwardPipeline",
]
