from Infernux.engine.ui.inspector_declarative import (
    InspectorChoice,
    InspectorInlineAssets,
    InspectorList,
    InspectorModel,
    InspectorReadOnlyRow,
    InspectorSection,
    render_inspector_model,
)
from Infernux.engine.ui.inspector_renderstack import build_renderstack_inspector_model
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


def test_renderstack_topology_exposes_only_pass_rows_and_effect_stage_lists():
    controls = _topology_controls(build_renderstack_inspector_model(RenderStack()))

    assert any(isinstance(control, InspectorReadOnlyRow) for control in controls)
    lists = [control for control in controls if isinstance(control, InspectorList)]
    assert [control.key for control in lists] == [
        "stage_after_opaque",
        "stage_after_sky",
        "stage_after_transparent",
        "stage_final",
    ]
    assert all(control.accept_drop == "RENDER_EFFECT_FILE" for control in lists)
    assert len([control for control in controls if isinstance(control, InspectorInlineAssets)]) == 4


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
