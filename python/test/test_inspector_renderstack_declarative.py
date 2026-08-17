import pytest
from types import SimpleNamespace

from Infernux.engine.ui.inspector_declarative import (
    InspectorChoice,
    InspectorList,
    InspectorMessages,
    InspectorModel,
    InspectorSection,
    InspectorSerializedTarget,
    render_inspector_model,
)
from Infernux.engine.ui.inspector_renderstack import build_renderstack_inspector_model
from Infernux.components.fields import get_serialized_fields
from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline
from Infernux.renderstack.default_forward_plus_pipeline import DefaultForwardPlusPipeline
from Infernux.renderstack.default_deferred_pipeline import DefaultDeferredPipeline
from Infernux.renderstack.render_stack import RenderStack


@pytest.fixture(autouse=True)
def action_journal():
    from Infernux.engine.interaction import (
        EditorCommandRegistry,
        EditorInteractionCore,
        FocusService,
        RenderStackCommandService,
        SelectionService,
    )
    from Infernux.engine.undo import UndoManager

    previous = UndoManager.instance()
    previous_service = RenderStackCommandService.instance()
    previous_core = EditorInteractionCore._instance
    previous_registry = EditorCommandRegistry._instance
    manager = UndoManager()
    service = RenderStackCommandService()
    registry = EditorCommandRegistry(
        focus=FocusService(),
        selection=SelectionService(),
    )
    service.register_commands(registry)
    EditorInteractionCore._instance = SimpleNamespace(commands=registry)
    try:
        yield manager
    finally:
        service.shutdown()
        EditorCommandRegistry._instance = previous_registry
        EditorInteractionCore._instance = previous_core
        RenderStackCommandService._instance = previous_service
        UndoManager._instance = previous


def _topology_controls(model):
    return next(section.controls for section in model.sections if section.key == "topology")


def test_renderstack_uses_common_declarative_inspector_without_mounted_pass_api():
    stack = RenderStack()
    model = build_renderstack_inspector_model(stack)

    assert "on_inspector_gui" not in RenderStack.__dict__
    assert not hasattr(stack, "pass_entries")
    assert not hasattr(stack, "add_pass")
    assert not hasattr(stack, "remove_pass")
    assert [section.key for section in model.sections] == ["pipeline", "topology"]
    assert isinstance(model.sections[0].controls[0], InspectorChoice)


def test_forward_pipeline_labels_single_sample_msaa_explicitly():
    metadata = get_serialized_fields(DefaultForwardPipeline)["msaa_samples"]

    assert metadata.enum_labels == ["X1 (Off)", "X2", "X4", "X8"]


def test_shadow_resolution_range_is_enforced_outside_the_inspector():
    pipeline = DefaultForwardPipeline()

    pipeline.shadow_resolution = 0
    assert pipeline.shadow_resolution == 256

    pipeline.shadow_resolution = 99999
    assert pipeline.shadow_resolution == 8192


def test_default_forward_pipeline_uses_forward_for_opaque_and_transparent():
    from Infernux.rendergraph.graph import RenderGraph

    graph = RenderGraph("Default Forward")
    DefaultForwardPipeline().define_topology(graph)
    draws = {
        render_pass.name: render_pass
        for render_pass in graph._passes
        if render_pass._action == "draw_renderers"
    }

    assert draws["OpaquePass"]._material_pass == "forward"
    assert draws["OpaquePass"]._sort_mode == "front_to_back"
    assert draws["TransparentPass"]._material_pass == "forward"
    assert draws["TransparentPass"]._sort_mode == "back_to_front"


def test_builtin_pipeline_route_selector_is_not_an_exposed_parameter():
    from Infernux.components.fields import get_serialized_fields

    assert "material_pass" not in get_serialized_fields(DefaultForwardPipeline)
    assert "material_pass" not in get_serialized_fields(DefaultForwardPlusPipeline)


def test_default_forward_plus_pipeline_uses_tiled_variants_for_all_geometry():
    from Infernux.rendergraph.graph import RenderGraph

    graph = RenderGraph("Default Forward+")
    DefaultForwardPlusPipeline().define_topology(graph)
    draws = {
        render_pass.name: render_pass
        for render_pass in graph._passes
        if render_pass._action == "draw_renderers"
    }

    assert draws["OpaquePass"]._material_pass == "forward_plus"
    assert draws["TransparentPass"]._material_pass == "forward_plus"


def test_default_deferred_pipeline_uses_forward_plus_for_transparent():
    from Infernux.rendergraph.graph import Format, RenderGraph

    graph = RenderGraph("Default Deferred")
    DefaultDeferredPipeline().define_topology(graph)
    transparent = next(
        render_pass
        for render_pass in graph._passes
        if render_pass.name == "TransparentPass"
    )

    assert transparent._material_pass == "forward_plus"
    assert transparent._sort_mode == "back_to_front"
    gbuffer = next(
        render_pass for render_pass in graph._passes if render_pass.name == "GBufferPass"
    )
    lighting = next(
        render_pass
        for render_pass in graph._passes
        if render_pass.name == "DeferredLightingPass"
    )
    assert len(gbuffer._write_colors) == 5
    assert gbuffer._material_filter == "deferred_compatible"
    fallback = next(
        render_pass
        for render_pass in graph._passes
        if render_pass.name == "DeferredForwardFallbackPass"
    )
    assert fallback._material_pass == "forward_plus"
    assert fallback._material_filter == "deferred_unsupported"
    assert fallback._write_depth == "depth"
    assert graph.get_texture("gbuffer_object").format == Format.RG32_UINT
    assert lighting._shader_name == "Deferred Lighting"
    assert list(lighting._input_bindings) == [
        "gAlbedo",
        "gNormal",
        "gMaterial",
        "gEmission",
        "gObject",
        "sceneDepth",
    ]


def test_default_pipelines_publish_depth_tested_motion_for_both_queue_domains():
    from Infernux.rendergraph.graph import Format, RenderGraph

    for pipeline in (
        DefaultForwardPipeline(),
        DefaultForwardPlusPipeline(),
        DefaultDeferredPipeline(),
    ):
        graph = RenderGraph(type(pipeline).__name__)
        graph.set_geometry_buffer_requirements({"motion"})
        pipeline.define_topology(graph)

        result = graph.get_pass_result(
            "gbuffer" if isinstance(pipeline, DefaultDeferredPipeline) else "opaque"
        )
        motion = result.sample("motion")
        assert motion is not None
        assert motion.format == Format.RG16_SFLOAT
        assert motion.samples == 1
        multisampled = isinstance(pipeline, DefaultForwardPipeline)
        source = "gbuffer" if isinstance(pipeline, DefaultDeferredPipeline) else "opaque"
        motion_target = (
            f"_result/{source}/motion_msaa"
            if multisampled
            else f"_result/{source}/motion"
        )
        if multisampled:
            assert graph.get_texture(motion_target).samples == 4

        passes = {
            render_pass.name: render_pass
            for render_pass in graph._passes
            if render_pass._material_pass == "motion"
        }
        assert list(passes) == [f"{source}/Motion"]
        assert passes[f"{source}/Motion"]._reads == ["depth"]
        assert passes[f"{source}/Motion"]._write_colors == {0: motion_target}
        assert passes[f"{source}/Motion"]._resolve_color == (
            f"_result/{source}/motion" if multisampled else None
        )
        assert passes[f"{source}/Motion"]._write_depth is None
        assert passes[f"{source}/Motion"]._clear_color == (0.0, 0.0, 0.0, 0.0)

        stages = {stage.stable_id: stage for stage in graph.effect_stages}
        assert "motion" in stages["after_transparent"].contract.inputs
        assert "motion" in stages["final"].contract.inputs
        graph.build()


def test_forward_motion_targets_follow_every_supported_msaa_value():
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.default_forward_pipeline import MSAASamples

    revisions = set()
    for samples in MSAASamples:
        pipeline = DefaultForwardPipeline()
        pipeline.msaa_samples = samples
        graph = RenderGraph(f"Forward Motion X{int(samples)}")
        graph.set_geometry_buffer_requirements({"motion"})
        pipeline.define_topology(graph)
        description = graph.build()
        textures = {texture.name: texture for texture in description.textures}
        motion_passes = [
            render_pass
            for render_pass in description.passes
            if render_pass.commands
            and render_pass.commands[0].material_pass == "motion"
        ]

        assert description.msaa_samples == int(samples)
        assert textures["_result/opaque/motion"].samples == 1
        if int(samples) == 1:
            assert "_result/opaque/motion_msaa" not in textures
            assert all(
                p.write_colors == [(0, "_result/opaque/motion")]
                for p in motion_passes
            )
            assert all(not p.resolve_color for p in motion_passes)
        else:
            assert textures["_result/opaque/motion_msaa"].samples == int(samples)
            assert all(
                p.write_colors == [(0, "_result/opaque/motion_msaa")]
                for p in motion_passes
            )
            assert all(
                p.resolve_color == "_result/opaque/motion"
                for p in motion_passes
            )
        revisions.add(description.source_revision)

    assert len(revisions) == len(MSAASamples)


def test_default_pipelines_omit_motion_without_a_consumer():
    from Infernux.rendergraph.graph import RenderGraph

    for pipeline in (
        DefaultForwardPipeline(),
        DefaultForwardPlusPipeline(),
        DefaultDeferredPipeline(),
    ):
        graph = RenderGraph(type(pipeline).__name__)
        pipeline.define_topology(graph)

        assert graph.get_texture("motion") is None
        assert all(
            render_pass._material_pass != "motion"
            for render_pass in graph._passes
        )


def test_pipeline_parameter_change_is_mirrored_into_serialized_stack_state(action_journal):
    from Infernux.renderstack.default_forward_pipeline import MSAASamples

    stack = RenderStack()
    model = build_renderstack_inspector_model(stack)
    control = next(
        item
        for item in model.sections[0].controls
        if isinstance(item, InspectorSerializedTarget)
    )
    pipeline = control.target()

    old_value = pipeline.msaa_samples
    stack._graph_desc = object()
    control.on_change(pipeline, "msaa_samples", old_value, MSAASamples.X2)

    assert '"msaa_samples": {"__enum_name__": "X2"}' in stack.pipeline_params_json
    assert stack._graph_desc is None
    assert stack.build_graph().msaa_samples == 2

    stack._graph_desc = object()
    old_value = pipeline.msaa_samples
    control.on_change(pipeline, "msaa_samples", old_value, MSAASamples.X8)

    assert stack._graph_desc is None
    assert stack.build_graph().msaa_samples == 8


def test_pipeline_parameter_undo_keeps_serialized_document_and_rebuild_in_sync():
    from Infernux.engine.undo import UndoManager
    from Infernux.renderstack.default_forward_pipeline import MSAASamples

    previous_manager = UndoManager.instance()
    manager = UndoManager()
    stack = RenderStack()
    model = build_renderstack_inspector_model(stack)
    control = next(
        item
        for item in model.sections[0].controls
        if isinstance(item, InspectorSerializedTarget)
    )
    pipeline = control.target()

    try:
        old_value = pipeline.msaa_samples
        pipeline.msaa_samples = MSAASamples.X8
        control.on_change(pipeline, "msaa_samples", old_value, pipeline.msaa_samples)
        assert stack.build_graph().msaa_samples == 8

        manager.undo()
        assert '"msaa_samples": {"__enum_name__": "X4"}' in stack.pipeline_params_json

        # Recreate the runtime projection from the serialized parameter
        # document. A stale mirror used to restore X8 at this point.
        stack._pipeline = None
        stack.invalidate_graph()
        assert stack.build_graph().msaa_samples == 4

        manager.redo()
        assert '"msaa_samples": {"__enum_name__": "X8"}' in stack.pipeline_params_json
        stack._pipeline = None
        stack.invalidate_graph()
        assert stack.build_graph().msaa_samples == 8
    finally:
        manager.clear()
        UndoManager._instance = previous_manager


def test_renderstack_inspector_exposes_only_effect_mount_points_with_inline_slots():
    controls = _topology_controls(build_renderstack_inspector_model(RenderStack()))

    assert all(isinstance(control, (InspectorList, InspectorMessages)) for control in controls)
    lists = [control for control in controls if isinstance(control, InspectorList)]
    assert [control.key for control in lists] == [
        "stage_after_opaque",
        "stage_after_sky",
        "stage_after_transparent",
        "stage_after_camera_ui",
        "stage_final",
        "stage_after_screen_ui",
    ]
    assert all(control.accept_drop == "RENDER_EFFECT_FILE" for control in lists)
    assert all(control.item_label is not None for control in lists)
    assert all(control.item_renderer is not None for control in lists)


def test_screen_ui_is_not_an_optional_pipeline_parameter():
    assert "enable_screen_ui" not in get_serialized_fields(DefaultForwardPipeline)
    assert "enable_screen_ui" not in get_serialized_fields(DefaultDeferredPipeline)


def test_default_pipeline_ui_and_effect_tail_has_canonical_order():
    from Infernux.rendergraph.graph import RenderGraph

    graph = RenderGraph("Default Forward")
    DefaultForwardPipeline().define_topology(graph)
    sequence = list(graph.topology_sequence)

    def position(kind, name):
        return sequence.index((kind, name))

    assert position("pass", "_ScreenUI_Camera") < position(
        "effect_stage", "after_camera_ui"
    )
    assert position("effect_stage", "after_camera_ui") < position(
        "effect_stage", "final"
    )
    assert position("effect_stage", "final") < position(
        "pass", "_DisplayEncode"
    )
    assert position("pass", "_DisplayEncode_Commit") < position(
        "pass", "_ScreenUI_Overlay"
    )
    assert position("pass", "_ScreenUI_Overlay") < position(
        "effect_stage", "after_screen_ui"
    )


def test_switching_pipeline_rebuilds_stage_model_without_stale_stage_access(action_journal):
    stack = RenderStack()
    assert stack.pipeline_class_name == "Default Forward"
    forward_model = build_renderstack_inspector_model(stack)
    choice = forward_model.sections[0].controls[0]
    pipelines = tuple(choice.options())
    assert choice.current_index() == pipelines.index("Default Forward")
    assert {"Default Forward", "Default Forward+", "Default Deferred"} <= set(pipelines)
    choice.on_change(pipelines.index("Default Deferred"))

    deferred_controls = _topology_controls(build_renderstack_inspector_model(stack))
    deferred_stages = [
        control.key for control in deferred_controls if isinstance(control, InspectorList)
    ]
    assert "stage_after_gbuffer" in deferred_stages

    deferred_choice = build_renderstack_inspector_model(stack).sections[0].controls[0]
    deferred_choice.on_change(tuple(deferred_choice.options()).index("Default Forward+"))
    forward_controls = _topology_controls(build_renderstack_inspector_model(stack))
    assert "stage_after_gbuffer" not in {
        control.key for control in forward_controls if isinstance(control, InspectorList)
    }

    forward_choice = build_renderstack_inspector_model(stack).sections[0].controls[0]
    forward_choice.on_change(tuple(forward_choice.options()).index("Default Forward"))
    assert stack.pipeline_class_name == "Default Forward"


def test_broken_topology_probe_keeps_the_last_valid_inspector_model(monkeypatch):
    stack = RenderStack()
    valid_probe = stack._build_full_topology_probe()
    stack.invalidate_graph()
    monkeypatch.setattr(
        stack.pipeline,
        "define_topology",
        lambda _graph: (_ for _ in ()).throw(ValueError("bad topology")),
    )

    assert stack._build_full_topology_probe() is valid_probe
    assert "bad topology" in stack.effect_compile_errors[-1]


def test_rejected_graph_rebuild_keeps_rendering_the_last_valid_graph(monkeypatch):
    stack = RenderStack()
    previous_graph = object()
    stack._last_valid_graph_desc = previous_graph
    stack._graph_desc = None
    monkeypatch.setattr(
        stack,
        "build_graph",
        lambda: (_ for _ in ()).throw(ValueError("invalid graph edit")),
    )

    class Context:
        def __init__(self):
            self.applied = []
            self.submitted = []

        def setup_camera_properties(self, _camera):
            pass

        def cull(self, _camera):
            return "culling"

        def apply_graph(self, graph):
            self.applied.append(graph)

        def submit_culling(self, culling):
            self.submitted.append(culling)

    context = Context()
    stack.render(context, object())

    assert stack._graph_desc is previous_graph
    assert stack._build_failed is True
    assert context.applied == [previous_graph]
    assert context.submitted == ["culling"]


def test_declarative_lists_scope_nested_widget_ids_by_control_key(monkeypatch):
    rendered_scopes = []

    class Context:
        def __init__(self):
            self.ids = []

        def push_style_var_vec2(self, *_args):
            pass

        def pop_style_var(self, _count):
            pass

        def push_id_str(self, value):
            self.ids.append(value)

        def pop_id(self):
            self.ids.pop()

    def render_list(_ctx, *_args, **_kwargs):
        rendered_scopes.append(tuple(_ctx.ids))

    monkeypatch.setattr(
        "Infernux.engine.ui._inspector_list_field._render_list_field",
        render_list,
    )
    target = object()
    metadata = object()
    controls = tuple(
        InspectorList(
            key=key,
            label=key,
            target=target,
            field_name="slots",
            metadata=metadata,
            value=lambda: [],
        )
        for key in ("stage_after_opaque", "stage_final")
    )
    model = InspectorModel(
        key="stack",
        sections=(InspectorSection(key="topology", controls=controls),),
    )

    render_inspector_model(Context(), target, model)

    assert rendered_scopes == [
        ("stage_after_opaque",),
        ("stage_final",),
    ]


def test_renderstack_effect_slots_use_shared_command_service(action_journal):
    from Infernux.engine.interaction import ActionOrigin, RenderStackCommandService
    from Infernux.renderstack.effect_slot import EffectSlot

    stack = RenderStack()
    stage_id = stack.effect_stages[0].stable_id
    service = RenderStackCommandService.instance()
    assert service is not None

    assert service.set_effect_stage_slots(
        stack,
        stage_id,
        [EffectSlot(stage_id=stage_id, enabled=False)],
        origin=ActionOrigin.AUTOMATION,
    )
    assert len(stack.get_effect_stage_slots(stage_id)) == 1
    assert not stack.get_effect_stage_slots(stage_id)[0].enabled
    assert action_journal.action_journal.applied_entries()[-1].origin is ActionOrigin.AUTOMATION

    action_journal.undo()
    assert stack.get_effect_stage_slots(stage_id) == ()


def test_renderstack_inspector_stage_list_uses_global_command_registry(action_journal):
    from Infernux.renderstack.effect_slot import EffectSlot

    stack = RenderStack()
    control = next(
        item
        for item in _topology_controls(build_renderstack_inspector_model(stack))
        if isinstance(item, InspectorList)
    )
    stage_id = control.key.removeprefix("stage_")
    slots = [EffectSlot(stage_id=stage_id, enabled=False)]

    assert control.target is stack
    assert control.on_change is not None
    control.on_change(stack, control.field_name, [], slots)

    assert len(stack.get_effect_stage_slots(stage_id)) == 1
    assert not stack.get_effect_stage_slots(stage_id)[0].enabled
    assert not hasattr(stack, "_inspector_effect_stage_adapters")

    action_journal.undo()
    assert stack.get_effect_stage_slots(stage_id) == ()


def test_renderstack_mcp_mutations_do_not_write_stack_or_scene_dirty_directly():
    import os

    source_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "Infernux",
        "mcp",
        "tools",
        "renderstack.py",
    )
    with open(source_path, "r", encoding="utf-8") as stream:
        source = stream.read()

    mutation_slice = source[
        source.index('    @mcp.tool(name="renderstack_set_pipeline")') :
        source.index("def _find_stack")
    ]
    assert "_mark_scene_dirty" not in mutation_slice
    assert "stack.set_pipeline(" not in mutation_slice
    assert "stack.set_effect_stage_slots(" not in mutation_slice
    assert "stack.add_effect_slot(" not in mutation_slice
    assert "RenderStackCommandService" not in mutation_slice
    assert "submit_renderstack_command" in mutation_slice
    assert '"renderstack.set_pipeline"' in mutation_slice
    assert '"renderstack.set_parameter"' in mutation_slice
    assert '"renderstack.set_effect_slots"' in mutation_slice
