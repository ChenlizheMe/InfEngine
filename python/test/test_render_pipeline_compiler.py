import pytest

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.rendergraph.graph import RenderGraph
from Infernux.renderstack.effect_stage import EffectScope
from Infernux.renderstack.pipeline_dsl import Path, PipelineBuilder, Queue
from Infernux.renderstack.pipeline_compiler import compile_pipeline_definition
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import RenderEffectAsset
from Infernux.renderstack.render_effect_compiler import (
    RenderEffectCompileError,
    get_render_effect_feature,
)
from Infernux.renderstack.render_pipeline import RenderPipeline
from Infernux.renderstack.render_stack import RenderStack
from Infernux.renderstack.route_policy import RoutePolicy, merge_route_policies


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


def test_msaa_pipeline_resolves_isolated_routes_and_sky_before_effects():
    pipeline = PipelineBuilder()
    pipeline.frame(hdr=True, msaa=4)
    with pipeline.opaque() as opaque:
        opaque.forward(Queue(1, 100)).effects("route_fx")
        opaque.otherwise().forward()
    pipeline.sky()

    graph = RenderGraph("MSAA Routes")
    compile_pipeline_definition(pipeline.build(), graph)
    description = graph.build()
    textures = {texture.name: texture for texture in description.textures}
    route_draws = [
        render_pass
        for render_pass in description.passes
        if render_pass.name.startswith("route/opaque.")
        and render_pass.name.split("/")[-1].startswith("Draw_")
    ]
    sky = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name == "sky/SkyboxPass"
    )

    assert route_draws
    assert all(render_pass.resolve_color.endswith("/color") for render_pass in route_draws)
    assert all(
        textures[render_pass.write_colors[0][1]].samples == 4
        for render_pass in route_draws
    )
    assert all(textures[render_pass.resolve_color].samples == 1 for render_pass in route_draws)
    assert sky.resolve_color == "sky/color"
    assert textures[sky.write_colors[0][1]].samples == 4

    frame_clear = next(
        render_pass for render_pass in description.passes
        if render_pass.name == "FrameClear"
    )
    sky_under = next(
        render_pass for render_pass in description.passes
        if render_pass.name.startswith("under/scene/")
    )
    assert frame_clear.clear_color_a == 0.0
    assert sky.clear_color_a == 1.0
    assert sky_under.commands[0].shader_name == "route_alpha_composite"
    assert sky_under.commands[0].input_bindings[0] == ("_BaseTex", "sky/color")


def test_opaque_route_effect_overflow_is_composited_after_ordinary_background():
    pipeline = PipelineBuilder()
    with pipeline.opaque() as opaque:
        with opaque.layer("Effects") as layer:
            layer.forward(Queue(1, 100)).effects("blurred")
        opaque.otherwise().forward()
    pipeline.effects("empty_after_opaque")
    pipeline.sky()

    graph = RenderGraph("Deferred Route Overflow")
    graph._effect_route_policy_resolver = lambda stage_ids: (
        RoutePolicy.ISOLATE_AND_COMPOSITE
        if "blurred" in stage_ids
        else RoutePolicy.INLINE
    )
    compile_pipeline_definition(pipeline.build(), graph)
    description = graph.build()
    names = [render_pass.name for render_pass in description.passes]

    ordinary_draw = next(
        index
        for index, name in enumerate(names)
        if name.startswith("route/opaque.route_2/") and "/Draw_" in name
    )
    overflow_composite = next(
        index
        for index, render_pass in enumerate(description.passes)
        if any(
            command.shader_name == "route_alpha_composite"
            and dict(command.input_bindings).get("_LayerTex", "").endswith(
                "route/opaque.route_1/color"
            )
            for command in render_pass.commands
        )
    )
    sky_under = next(
        index
        for index, name in enumerate(names)
        if name.startswith("under/scene/")
    )

    assert ordinary_draw < overflow_composite
    assert sky_under < overflow_composite


def test_msaa_inline_background_is_not_deferred_over_effect_overflow():
    class MsaaOverflowPipeline(RenderPipeline):
        name = "MSAA Overflow Ordering"

        def define(self, pipeline):
            pipeline.frame(hdr=True, msaa=4)
            with pipeline.opaque() as opaque:
                with opaque.layer("Effects") as layer:
                    layer.forward(Queue(1, 100)).effects("blurred")
                opaque.otherwise().forward()
            pipeline.sky()

    stack = RenderStack()
    stack._pipeline = MsaaOverflowPipeline()
    stack._pipeline._render_stack = stack
    stack.add_effect_slot(
        "blurred",
        RenderEffectRef(
            effect=_effect("infernux.route.pixelation", pixel_size=18)
        ),
    )

    description = stack.build_graph()
    passes = list(description.passes)
    otherwise_composite = next(
        index
        for index, render_pass in enumerate(passes)
        if any(
            command.shader_name == "route_alpha_composite"
            and dict(command.input_bindings).get("_LayerTex", "").endswith(
                "route/opaque.route_2/color"
            )
            for command in render_pass.commands
        )
    )
    overflow_composite = next(
        index
        for index, render_pass in enumerate(passes)
        if any(
            command.shader_name == "route_alpha_composite"
            and dict(command.input_bindings).get("_LayerTex", "").endswith(
                "route/opaque.route_1/color"
            )
            for command in render_pass.commands
        )
    )
    sky_under = next(
        index
        for index, render_pass in enumerate(passes)
        if render_pass.name.startswith("under/scene/")
    )

    assert otherwise_composite < sky_under < overflow_composite


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


class _RouteEffectPipeline(RenderPipeline):
    name = "Route Policy Test"

    def define(self, pipeline):
        with pipeline.opaque() as opaque:
            opaque.forward(Queue(1, 100)).effects("route_fx")
            opaque.otherwise().forward()


def _route_effect_stack(*effects: RenderEffect) -> RenderStack:
    stack = RenderStack()
    stack._pipeline = _RouteEffectPipeline()
    stack._pipeline._render_stack = stack
    for effect in effects:
        stack.add_effect_slot("route_fx", RenderEffectRef(effect=effect))
    return stack


def _effect(feature_type: str, **parameters) -> RenderEffect:
    return RenderEffect(
        RenderEffectAsset(feature_type=feature_type, parameters=parameters)
    )


def test_builtin_route_effects_declare_their_image_ownership_policy():
    assert get_render_effect_feature("infernux.post.bloom").route_policy is RoutePolicy.ADDITIVE_EXTRACT
    assert get_render_effect_feature("infernux.route.pixelation").route_policy is RoutePolicy.ISOLATE_AND_COMPOSITE


def test_route_policy_merge_rejects_additive_and_replacement_effects():
    assert merge_route_policies([]) is RoutePolicy.INLINE
    assert merge_route_policies(
        [RoutePolicy.MASK_AND_MODIFY, RoutePolicy.ISOLATE_AND_COMPOSITE]
    ) is RoutePolicy.ISOLATE_AND_COMPOSITE
    with pytest.raises(ValueError, match="cannot be mixed"):
        merge_route_policies(
            [RoutePolicy.ADDITIVE_EXTRACT, RoutePolicy.MASK_AND_MODIFY]
        )


def test_composite_stage_activity_does_not_merge_route_policies():
    graph = RenderGraph("Composite Activity")
    graph._effect_stage_active_resolver = lambda stage_id: stage_id == "final"
    graph._effect_route_policy_resolver = lambda stage_ids: (_ for _ in ()).throw(
        AssertionError("composite activity must not query route policy")
    )

    assert graph.is_effect_stage_active("final") is True
    assert graph.is_effect_stage_active("empty") is False


def test_empty_route_effect_stage_draws_inline_without_an_isolation_target():
    description = _route_effect_stack().build_graph()
    names = [render_pass.name for render_pass in description.passes]

    assert "route/opaque.route_1/Clear" not in names
    assert not any(
        command.shader_name == "route_alpha_composite"
        and dict(command.input_bindings).get("_LayerTex", "").endswith(
            "route/opaque.route_1/color"
        )
        for render_pass in description.passes
        for command in render_pass.commands
    )


def test_pixelation_route_uses_isolation_and_alpha_composite():
    description = _route_effect_stack(
        _effect(
            "infernux.route.pixelation",
            pixel_size=24,
            pixel_aspect=1.5,
            sampling=2,
            preserve_alpha_coverage=True,
        )
    ).build_graph()
    pixelation = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("Pixelation_Apply")
    )
    command = pixelation.commands[0]
    bindings = dict(command.input_bindings)
    constants = dict(command.push_constants)

    assert bindings["_SourceTex"].startswith("route/opaque.route_1/")
    assert command.shader_name == "pixelation"
    assert constants["pixelSize"] == 24.0
    assert constants["pixelAspect"] == 1.5
    assert constants["samplingMode"] == 2.0
    assert constants["preserveAlphaCoverage"] == 1.0
    assert any(
        command.shader_name == "route_alpha_composite"
        for render_pass in description.passes
        for command in render_pass.commands
    )
    assert description.output_texture == "color"


def test_pixelation_clamps_runtime_parameters_to_production_limits():
    stack = _route_effect_stack(
        _effect(
            "infernux.route.pixelation",
            pixel_size=999,
            pixel_aspect=0.01,
            grid_offset_x=4.0,
            grid_offset_y=-4.0,
            intensity=2.0,
            sampling=2,
        )
    )
    description = stack.build_graph()
    pixelation = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("Pixelation_Apply")
    )
    constants = dict(pixelation.commands[0].push_constants)

    assert constants["pixelSize"] == 256.0
    assert constants["pixelAspect"] == 0.25
    assert constants["gridOffsetX"] == 1.0
    assert constants["gridOffsetY"] == -1.0
    assert constants["intensity"] == 1.0
    assert constants["samplingMode"] == 2.0


def test_bloom_route_extracts_only_additive_energy_and_handles_one_mip():
    description = _route_effect_stack(
        _effect("infernux.post.bloom", max_iterations=1)
    ).build_graph()
    names = [render_pass.name for render_pass in description.passes]
    composite = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("Bloom_Composite")
    )
    bindings = dict(composite.commands[0].input_bindings)

    assert "route/opaque.route_1/PreserveOriginal" in names
    assert "route/opaque.route_1/ExtractAdditiveDelta" in names
    assert any(
        command.shader_name == "route_additive_composite"
        for render_pass in description.passes
        for command in render_pass.commands
    )
    assert bindings["_BloomTex"].endswith("/_bloom_mip0")


def test_mixed_additive_and_replacement_route_effects_fail_actionably():
    stack = _route_effect_stack(
        _effect("infernux.post.bloom", max_iterations=2),
        _effect("infernux.route.pixelation", pixel_size=16),
    )

    with pytest.raises(RenderEffectCompileError, match="incompatible route effect policies"):
        stack.build_graph()
