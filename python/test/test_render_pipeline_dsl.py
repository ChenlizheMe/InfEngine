import pytest

from Infernux.renderstack.effect_stage import EffectScope
from Infernux.renderstack.pipeline_dsl import (
    Path,
    PipelineBuilder,
    Queue,
    QueueRoute,
    compile_queue_segments,
)


def _mixed_pipeline_definition():
    pipeline = PipelineBuilder()
    pipeline.frame(hdr=True)
    pipeline.shadows()
    pipeline.lighting(clustered=True)

    with pipeline.opaque() as opaque:
        with opaque.layer("Stylized Objects") as layer:
            layer.forward(Queue(1, 100)).effects("low_queue")
            layer.deferred(
                Queue(101, 200), fallback=Path.FORWARD_PLUS
            ).effects("middle_queue")
            layer.effects("stylized_combined")

        opaque.otherwise().forward_plus()
        opaque.effects("opaque_only")

    pipeline.effects("after_opaque")
    pipeline.sky()
    pipeline.effects("after_sky")

    with pipeline.transparent() as transparent:
        transparent.otherwise().forward_plus()
        transparent.effects("transparent_only")

    pipeline.effects("final")
    pipeline.screen_ui()
    return pipeline.build()


def test_queue_is_one_strict_public_material_range_type():
    assert Queue(1001).as_tuple() == (1001, 1001)
    assert Queue.range(10, 20).as_tuple() == (10, 20)
    assert Queue.named("Opaque") == Queue(0, 2500)
    assert Queue.all_transparent() == Queue(2501, 5000)

    with pytest.raises(TypeError, match="integers"):
        Queue(True)
    with pytest.raises(ValueError, match=r"\[0, 9999\]"):
        Queue(10000)
    with pytest.raises(ValueError, match="minimum"):
        Queue(20, 10)


def test_queue_router_splits_routes_and_remaining_intervals_without_duplicates():
    segments = compile_queue_segments(
        Queue.all_opaque(),
        (
            QueueRoute("gray", Queue(1001)),
            QueueRoute("blur", Queue(1002)),
        ),
        include_otherwise=True,
    )

    assert [segment.selector.as_tuple() for segment in segments] == [
        (0, 1000),
        (1001, 1001),
        (1002, 1002),
        (1003, 2500),
    ]
    assert [segment.route_id for segment in segments] == [None, "gray", "blur", None]
    assert [segment.is_otherwise for segment in segments] == [True, False, False, True]


def test_queue_router_rejects_overlap_and_out_of_domain_routes():
    with pytest.raises(ValueError, match="overlap"):
        compile_queue_segments(
            Queue.all_opaque(),
            (
                QueueRoute("a", Queue(1, 100)),
                QueueRoute("b", Queue(100, 200)),
            ),
            include_otherwise=True,
        )
    with pytest.raises(ValueError, match="outside"):
        compile_queue_segments(
            Queue.all_opaque(),
            (QueueRoute("transparent", Queue(3000)),),
            include_otherwise=True,
        )


def test_mixed_pipeline_fixture_preserves_scope_path_and_operation_semantics():
    definition = _mixed_pipeline_definition()
    opaque = definition.domain("opaque")
    transparent = definition.domain("transparent")
    layer = opaque.layers[0]

    assert definition.frame.hdr is True
    assert definition.lighting.clustered is True
    assert [route.selector.as_tuple() for route in layer.routes] == [(1, 100), (101, 200)]
    assert layer.routes[0].path is Path.FORWARD
    assert layer.routes[1].path is Path.DEFERRED
    assert layer.routes[1].fallback is Path.FORWARD_PLUS
    assert opaque.routes[0].is_otherwise is True
    assert transparent.routes[0].is_otherwise is True

    stages = {stage.stable_id: stage for stage in definition.effect_stages}
    assert stages["low_queue"].scope is EffectScope.ROUTE
    assert stages["middle_queue"].scope is EffectScope.ROUTE
    assert stages["stylized_combined"].scope is EffectScope.LAYER
    assert stages["opaque_only"].scope is EffectScope.STAGE
    assert stages["after_opaque"].scope is EffectScope.COMPOSITE
    assert stages["transparent_only"].scope is EffectScope.STAGE
    assert stages["final"].scope is EffectScope.COMPOSITE
    assert definition.operations[-1] == ("screen_ui", "")
    assert definition.has_screen_ui is True

    opaque_segments = compile_queue_segments(
        opaque.queue, opaque.all_routes(), include_otherwise=True
    )
    explicit = [segment for segment in opaque_segments if not segment.is_otherwise]
    assert [segment.selector.as_tuple() for segment in explicit] == [(1, 100), (101, 200)]
    assert all(
        not segment.selector.overlaps(Queue(1, 200))
        for segment in opaque_segments
        if segment.is_otherwise
    )


def test_pipeline_rejects_duplicate_effect_ids_and_domain_declarations():
    pipeline = PipelineBuilder()
    opaque = pipeline.opaque()
    opaque.forward(Queue(1, 100)).effects("shared")
    with pytest.raises(ValueError, match="already declared"):
        pipeline.effects("shared")
    with pytest.raises(ValueError, match="already declared"):
        pipeline.opaque()


def test_pipeline_rejects_overlapping_routes_at_build_boundary():
    pipeline = PipelineBuilder()
    opaque = pipeline.opaque()
    opaque.forward(Queue(1, 100))
    opaque.forward_plus(Queue(50, 150))
    with pytest.raises(ValueError, match="overlap"):
        pipeline.build()


def test_pipeline_rejects_more_than_one_otherwise_route():
    pipeline = PipelineBuilder()
    opaque = pipeline.opaque()
    opaque.otherwise().forward()
    opaque.otherwise().forward_plus()
    with pytest.raises(ValueError, match="more than one otherwise"):
        pipeline.build()
