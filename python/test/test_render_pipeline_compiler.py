import pytest

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.rendergraph.graph import RenderGraph
from Infernux.renderstack.effect_stage import EffectScope
from Infernux.renderstack.pipeline_dsl import Path, PipelineBuilder, Queue
from Infernux.renderstack.pipeline_compiler import compile_pipeline_definition
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import RenderEffectAsset
from Infernux.renderstack.render_pipeline import RenderPipeline
from Infernux.renderstack.render_stack import RenderStack


def _forward_mixed_definition():
    pipeline = PipelineBuilder()
    pipeline.frame(hdr=True)
    pipeline.shadows(resolution=1024)

    with pipeline.opaque() as opaque:
        with opaque.layer("Stylized") as layer:
            layer.forward(Queue(1, 100)).effects("low_queue")
            layer.forward(Queue(101, 200)).effects("middle_queue")
            layer.effects("stylized_combined")
        opaque.otherwise().forward()
        opaque.effects("opaque_only")

    pipeline.effects("after_opaque")
    pipeline.sky()
    pipeline.effects("after_sky")
    with pipeline.transparent() as transparent:
        transparent.otherwise().forward()
        transparent.effects("transparent_only")
    pipeline.effects("final")
    pipeline.screen_ui()
    return pipeline.build()


def test_compiler_creates_distinct_route_layer_stage_and_scene_images():
    graph = RenderGraph("Mixed")
    resources = {}

    def capture(stage):
        resources[stage.stable_id] = {
            name: handle.name
            for name, handle in graph.current_effect_resources.items()
        }

    graph._effect_stage_callback = capture
    compile_pipeline_definition(_forward_mixed_definition(), graph)

    assert resources["low_queue"]["color"] != resources["middle_queue"]["color"]
    assert resources["stylized_combined"]["color"].startswith("layer/")
    assert resources["opaque_only"]["color"].startswith("domain/opaque/")
    assert resources["after_opaque"]["color"] in {"color", "_scene_composite"}
    assert resources["transparent_only"]["color"].startswith("domain/transparent/")
    assert resources["final"]["color"] in {"color", "_scene_composite"}
    assert resources["low_queue"]["depth"] == "depth"

    stages = {stage.stable_id: stage for stage in graph.effect_stages}
    assert stages["low_queue"].scope is EffectScope.ROUTE
    assert stages["stylized_combined"].scope is EffectScope.LAYER
    assert stages["opaque_only"].scope is EffectScope.STAGE
    assert stages["after_opaque"].scope is EffectScope.COMPOSITE

    description = graph.build()
    assert description.output_texture == "color"
    assert len(description.passes) == graph.pass_count


def test_compiler_partitions_otherwise_without_drawing_explicit_queues_twice():
    graph = RenderGraph("QueuePartition")
    compile_pipeline_definition(_forward_mixed_definition(), graph)

    queue_ranges = [
        (render_pass._queue_min, render_pass._queue_max)
        for render_pass in graph._passes
        if render_pass._action == "draw_renderers"
        and "route/opaque" in render_pass.name
    ]
    assert sorted(queue_ranges) == [(0, 0), (1, 100), (101, 200), (201, 2500)]


@pytest.mark.parametrize("path", [Path.FORWARD_PLUS, Path.DEFERRED])
def test_compiler_never_silently_substitutes_forward_for_unfinished_paths(path):
    pipeline = PipelineBuilder()
    opaque = pipeline.opaque()
    if path is Path.FORWARD_PLUS:
        opaque.forward_plus()
    else:
        opaque.deferred(fallback=Path.FORWARD_PLUS)

    with pytest.raises(NotImplementedError, match="not available"):
        compile_pipeline_definition(pipeline.build(), RenderGraph("Unsupported"))


def test_compiler_never_silently_ignores_clustered_lighting_request():
    pipeline = PipelineBuilder()
    pipeline.lighting(clustered=True)
    pipeline.opaque().forward()

    with pytest.raises(NotImplementedError, match="clustered lighting"):
        compile_pipeline_definition(pipeline.build(), RenderGraph("Clustered"))


def test_screen_ui_must_be_the_final_author_operation():
    pipeline = PipelineBuilder()
    pipeline.opaque().forward()
    pipeline.screen_ui()
    pipeline.effects("too_late")
    with pytest.raises(ValueError, match="final pipeline operation"):
        pipeline.build()


def test_render_pipeline_define_method_uses_declarative_compiler():
    class ForwardPipeline(RenderPipeline):
        name = "Forward DSL Test"

        def define(self, pipeline):
            pipeline.frame(hdr=False)
            pipeline.opaque().forward()
            pipeline.effects("final")

    graph = RenderGraph("DefineBridge")
    ForwardPipeline().define_topology(graph)

    assert graph.has_effect_stage("final")
    assert any(
        render_pass._action == "draw_renderers"
        for render_pass in graph._passes
    )


def test_render_stack_mounts_route_effect_against_isolated_route_color():
    class RouteEffectPipeline(RenderPipeline):
        name = "Route Effect Test"

        def define(self, pipeline):
            with pipeline.opaque() as opaque:
                opaque.forward(Queue(1, 100)).effects("route_fx")
                opaque.otherwise().forward()

    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.tonemapping",
            parameters={"exposure": 1.0},
        )
    )
    stack = RenderStack()
    stack._pipeline = RouteEffectPipeline()
    stack._pipeline._render_stack = stack
    stack.add_effect_slot("route_fx", RenderEffectRef(effect=effect))

    description = stack.build_graph()
    tone_pass = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("ToneMap_Apply")
    )
    bindings = dict(tone_pass.commands[0].input_bindings)

    assert bindings["_SourceTex"].startswith("route/opaque.")
    assert bindings["_SourceTex"].endswith("/color")
    assert stack.effect_compile_errors == ()
