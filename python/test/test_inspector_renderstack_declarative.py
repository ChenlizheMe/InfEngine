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
from Infernux.components.serialized_field import get_serialized_fields
from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline
from Infernux.renderstack.render_stack import RenderStack


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


def test_pipeline_parameter_change_is_mirrored_into_serialized_stack_state():
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
    pipeline.msaa_samples = MSAASamples.X2
    control.on_change(pipeline, "msaa_samples", old_value, pipeline.msaa_samples)

    assert '"msaa_samples": {"__enum_name__": "X2"}' in stack.pipeline_params_json


def test_renderstack_inspector_exposes_only_effect_mount_points_with_inline_slots():
    controls = _topology_controls(build_renderstack_inspector_model(RenderStack()))

    assert all(isinstance(control, (InspectorList, InspectorMessages)) for control in controls)
    lists = [control for control in controls if isinstance(control, InspectorList)]
    assert [control.key for control in lists] == [
        "stage_after_opaque",
        "stage_after_sky",
        "stage_after_transparent",
        "stage_final",
        "stage_after_screen_ui",
    ]
    assert all(control.accept_drop == "RENDER_EFFECT_FILE" for control in lists)
    assert all(control.item_label is not None for control in lists)
    assert all(control.item_renderer is not None for control in lists)


def test_switching_pipeline_rebuilds_stage_model_without_stale_stage_access():
    stack = RenderStack()
    forward_model = build_renderstack_inspector_model(stack)
    choice = forward_model.sections[0].controls[0]
    pipelines = tuple(choice.options())
    choice.on_change(pipelines.index("Default Deferred"))

    deferred_controls = _topology_controls(build_renderstack_inspector_model(stack))
    deferred_stages = [
        control.key for control in deferred_controls if isinstance(control, InspectorList)
    ]
    assert "stage_after_gbuffer" in deferred_stages

    deferred_choice = build_renderstack_inspector_model(stack).sections[0].controls[0]
    deferred_choice.on_change(tuple(deferred_choice.options()).index("Default Forward"))
    forward_controls = _topology_controls(build_renderstack_inspector_model(stack))
    assert "stage_after_gbuffer" not in {
        control.key for control in forward_controls if isinstance(control, InspectorList)
    }


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
