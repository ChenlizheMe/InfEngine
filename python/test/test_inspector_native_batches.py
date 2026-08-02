from pathlib import Path

from Infernux.engine.ui import inspector_material, inspector_renderstack


class _VirtualContext:
    def __init__(self, visible):
        self.visible = visible
        self.cursor_y = 10.0
        self.dummies = []

    def is_virtualized_region_visible(self, _height):
        return self.visible

    def get_cursor_pos_y(self):
        return self.cursor_y

    def dummy(self, width, height):
        self.dummies.append((width, height))
        self.cursor_y += height


def test_renderstack_has_no_bespoke_native_batch_or_virtualized_renderer():
    assert callable(inspector_renderstack.build_renderstack_inspector_model)
    assert not hasattr(inspector_renderstack, "_render_pipeline_params")
    assert not hasattr(inspector_renderstack, "_render_virtualized_stack_block")
    assert not hasattr(inspector_renderstack, "_PIPELINE_PARAM_BATCH_PLANS")


def test_material_virtual_block_remeasures_when_visible():
    state = type("State", (), {"extra": {}})()
    ctx = _VirtualContext(True)

    def render():
        ctx.cursor_y += 75.0
        return (True, "property.value", False)

    result = inspector_material._render_virtualized_material_block(
        ctx, state, "properties", render, (False, "", False),
    )

    assert result == (True, "property.value", False)
    assert state.extra["_material_virtual_block_heights"]["properties"] == 75.0


def test_constrained_numeric_inputs_clamp_typed_values_in_native_paths():
    source = Path(
        "cpp/infernux/function/renderer/gui/InxGUIContext.cpp"
    ).read_text(encoding="utf-8")

    assert source.count("ImGuiSliderFlags_AlwaysClamp") >= 4
    assert "ImGui::DragFloat(label.c_str(), value, speed, min, max, fmt, flags)" in source
    assert "ImGui::DragInt(label.c_str(), value, speed, min, max, fmt, flags)" in source
