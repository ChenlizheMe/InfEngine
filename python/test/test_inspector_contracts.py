"""Stable semantic contracts for scene and asset Inspector surfaces."""

import json
import os
from types import SimpleNamespace

import pytest

from Infernux.core.asset_types import SpriteFrame, TextureImportSettings, TextureType
from Infernux.engine.ui import asset_details_renderer as details
from Infernux.engine.ui import inspector_material
from Infernux.engine.ui import inspector_utils


class _FakeSemanticContext:
    def __init__(self):
        self.semantic_items = []

    def record_semantic_item(self, kind, label, enabled, semantic_id):
        self.semantic_items.append((kind, label, enabled, semantic_id))


class _FakeTextContext(_FakeSemanticContext):
    def input_text_multiline(self, _widget_id, value, *_args):
        return value

    def drag_float(self, _widget_id, value, *_args):
        return value


class _FakeThemeContext(_FakeSemanticContext):
    def get_content_region_avail_width(self):
        return 240.0

    def push_style_color(self, *_args):
        pass

    def pop_style_color(self, *_args):
        pass

    def button(self, _label, _callback, **_kwargs):
        return False

    def same_line(self, *_args):
        pass


class _FakeVectorContext(_FakeSemanticContext):
    def __init__(self):
        super().__init__()
        self.vector_semantics = []

    def vector2(self, _label, x, y, *_args, semantic_id=""):
        self.vector_semantics.append(semantic_id)
        return x, y


class _StableFieldIdContext:
    def __init__(self):
        self.id_stack = []
        self.vector_calls = []

    def push_id_str(self, value):
        self.id_stack.append(value)

    def pop_id(self):
        self.id_stack.pop()

    def vector2(self, label, x, y, *_args):
        self.vector_calls.append((tuple(self.id_stack), label))
        return x, y


def test_builtin_material_shader_sync_updates_readonly_cache_without_user_edit(monkeypatch):
    material_document = {"properties": {}}
    state = SimpleNamespace(extra={"cached_data": material_document})

    def _sync(document, _state):
        document["properties"]["BaseColor"] = {
            "type": 7,
            "value": [1.0, 1.0, 1.0, 1.0],
        }
        return True, True

    monkeypatch.setattr(inspector_material, "_sync_shader_annotations", _sync)

    changed, requires_deserialize = inspector_material._prepare_shader_annotations(
        material_document,
        state,
        read_only=True,
    )

    assert changed is False
    assert requires_deserialize is False
    assert json.loads(state.extra["cached_json"]) == material_document
    assert "_material_preview_pending" not in state.extra


def test_shader_annotation_reflection_tracks_structure_not_value_version(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    reflection_calls = []
    sync_calls = []
    monkeypatch.setattr(
        module.shader_utils,
        "shader_ref_id",
        lambda value: str(value or ""),
    )
    monkeypatch.setattr(
        module.shader_utils,
        "get_shader_property_generation",
        lambda: 4,
    )
    monkeypatch.setattr(
        module.shader_utils,
        "get_all_shader_property_names",
        lambda vertex, fragment: reflection_calls.append((vertex, fragment)) or ["roughness"],
    )
    monkeypatch.setattr(
        module.shader_utils,
        "sync_all_shader_properties",
        lambda *args, **kwargs: sync_calls.append((args, kwargs)),
    )

    document = {
        "shaders": {"vertex": "Vert", "fragment": "Frag"},
        "properties": {"roughness": {"type": 0, "value": 0.25}},
        "_shader_property_order": ["roughness"],
    }
    state = SimpleNamespace(extra={})

    # The first call establishes the annotation cache.
    module._sync_shader_annotations(document, state)
    assert len(reflection_calls) == 1
    initial_sync_count = len(sync_calls)

    # A value-only material edit must not reflect the shader again.
    document["properties"]["roughness"]["value"] = 0.75
    module._sync_shader_annotations(document, state)
    assert len(reflection_calls) == 1

    # Adding a property key or changing the shader generation invalidates it.
    document["properties"]["metallic"] = {"type": 0, "value": 0.0}
    module._sync_shader_annotations(document, state)
    assert len(reflection_calls) == 2
    module.shader_utils.get_shader_property_generation = lambda: 5
    module._sync_shader_annotations(document, state)
    assert len(reflection_calls) == 3
    assert len(sync_calls) == initial_sync_count + 1


def test_material_shader_ui_cache_replays_catalog_and_path_without_requery(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    catalog_calls = []
    path_calls = []
    monkeypatch.setattr(
        module.shader_utils,
        "get_shader_candidates",
        lambda ext, _cache: catalog_calls.append(ext) or [(ext, ext)],
    )
    monkeypatch.setattr(
        module.shader_utils,
        "shader_ref_id",
        lambda value: str(value or ""),
    )
    monkeypatch.setattr(
        module.shader_utils,
        "shader_display_from_value",
        lambda value, _items: str(value or ""),
    )
    monkeypatch.setattr(
        module,
        "_shader_reference_path",
        lambda value, ext: path_calls.append((value, ext)) or f"{value}{ext}",
    )

    state = SimpleNamespace(
        extra={
            "_shader_catalog_generation": 1,
            "shader_cache": {".vert": None, ".frag": None},
        }
    )
    module._get_material_shader_ui_cache(state, "Vert", "Frag")
    module._get_material_shader_ui_cache(state, "Vert", "Frag")

    assert catalog_calls == [".vert", ".frag"]
    assert path_calls == [("Vert", ".vert"), ("Frag", ".frag")]

    module._get_material_shader_ui_cache(state, "OtherVert", "Frag")
    assert catalog_calls == [".vert", ".frag", ".vert", ".frag"]


def test_material_property_layout_cache_ignores_value_edits_until_schema_revision(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    order_calls = []
    monkeypatch.setattr(module, "get_locale", lambda: "en")
    monkeypatch.setattr(
        module.shader_utils,
        "get_material_property_display_order",
        lambda _document: order_calls.append(True) or ["roughness", "tint"],
    )

    class Context:
        @staticmethod
        def calc_text_width(label):
            return float(len(label))

    document = {
        "properties": {
            "roughness": {"type": 0, "value": 0.25},
            "tint": {"type": 7, "value": [1.0, 1.0, 1.0, 1.0]},
        }
    }
    state = SimpleNamespace(extra={"_material_schema_revision": 0})
    context = Context()
    module._get_material_property_layout_cache(context, state, document)
    module._get_material_property_layout_cache(context, state, document)
    assert len(order_calls) == 1

    document["properties"]["roughness"]["value"] = 0.75
    module._get_material_property_layout_cache(context, state, document)
    assert len(order_calls) == 1

    other_context = Context()
    module._get_material_property_layout_cache(other_context, state, document)
    assert len(order_calls) == 2  # Context identity is part of the layout key.

    module._bump_material_schema_revision(state)
    module._get_material_property_layout_cache(other_context, state, document)
    assert len(order_calls) == 3


def test_inline_material_document_binding_is_not_repeated_for_stable_state(monkeypatch):
    import Infernux.engine.interaction as interaction
    import Infernux.engine.ui.inspector_material as module

    bind_calls = []

    class NativeMaterial:
        file_path = "C:/project/Assets/Test.mat"
        guid = "test-guid"
        is_builtin = False

        @staticmethod
        def get_version():
            return 1

        @staticmethod
        def serialize_document():
            return {"properties": {}, "shaders": {}}

    controller = SimpleNamespace(document_id="material-document")
    monkeypatch.setattr(
        interaction,
        "ensure_editable_resource_document",
        lambda **kwargs: bind_calls.append(kwargs) or controller,
    )

    panel = SimpleNamespace()
    native = NativeMaterial()
    first = module._build_inline_state(panel, native)
    second = module._build_inline_state(panel, native)

    assert first is second
    assert len(bind_calls) == 1


def test_serialized_bool_and_vector_fields_keep_stable_widget_ids(monkeypatch):
    from Infernux.components.fields import FieldMetadata, FieldType

    checkbox_calls = []
    monkeypatch.setattr(
        inspector_utils,
        "render_inspector_checkbox",
        lambda fake_ctx, label, value: checkbox_calls.append(
            (tuple(fake_ctx.id_stack), label)
        ) or value,
    )

    ctx = _StableFieldIdContext()
    bool_metadata = FieldMetadata(
        name="enabled",
        field_type=FieldType.BOOL,
        default=False,
    )
    inspector_utils.render_serialized_field(
        ctx,
        "##stable_enabled",
        "Enabled",
        bool_metadata,
        False,
        80.0,
    )

    vector_metadata = FieldMetadata(
        name="direction",
        field_type=FieldType.VEC2,
        default=None,
    )
    inspector_utils.render_serialized_field(
        ctx,
        "##stable_direction",
        "Direction",
        vector_metadata,
        SimpleNamespace(x=1.0, y=2.0),
        80.0,
    )

    assert checkbox_calls == [(('##stable_enabled',), "Enabled")]
    assert ctx.vector_calls == [(('##stable_direction',), "Direction")]
    assert ctx.id_stack == []


class _FakeObjectFieldContext(_FakeSemanticContext):
    def __init__(self, interaction=2):
        super().__init__()
        self.opened_popups = []
        self.interaction = int(interaction)

    def push_id_str(self, _value):
        pass

    def pop_id(self):
        pass

    def get_content_region_avail_width(self):
        return 240.0

    def push_style_var_vec2(self, *_args):
        pass

    def push_style_var_float(self, *_args):
        pass

    def pop_style_var(self, *_args):
        pass

    def push_style_color(self, *_args):
        pass

    def pop_style_color(self, *_args):
        pass

    def begin_group(self):
        pass

    def end_group(self):
        pass

    def set_next_item_allow_overlap(self):
        pass

    def selectable(self, *_args):
        return True

    def same_line(self, *_args):
        pass

    def get_cursor_pos_x(self):
        return 0.0

    def set_cursor_pos_x(self, _value):
        pass

    def open_popup(self, popup_id):
        self.opened_popups.append(popup_id)

    def render_object_field_chrome(
        self, _field_id, display_text, _type_hint, _selected, clickable,
        has_picker, _picker_texture_id, semantic_id, _fixed_width,
    ):
        if semantic_id:
            self.record_semantic_item(
                "object_field", display_text, clickable, semantic_id
            )
        return self.interaction


def _text_component():
    return SimpleNamespace(
        game_object=SimpleNamespace(id=55),
        component_id=186,
        text="Race complete",
        font_size=24.0,
        line_height=1.2,
        letter_spacing=0.0,
    )


def test_inspector_component_semantic_id_uses_object_and_component_identity():
    from Infernux.engine.ui.inspector_utils import inspector_component_semantic_id

    assert inspector_component_semantic_id(_text_component(), "text") == (
        "inspector.object.55.component.186.text"
    )
    assert inspector_component_semantic_id(SimpleNamespace(component_id=186), "text") == ""


def test_inspector_semantics_skip_component_identity_work_outside_snapshot():
    from Infernux.engine.ui.inspector_utils import record_inspector_component_item

    class _NoIdentityAccess:
        @property
        def game_object(self):
            raise AssertionError("ordinary frames must not resolve semantic identity")

    ctx = _FakeSemanticContext()
    ctx.semantic_capture_enabled = False

    assert record_inspector_component_item(
        ctx, _NoIdentityAccess(), "speed", "drag_float", "Speed"
    ) == ""
    assert ctx.semantic_items == []


def test_scalar_batch_descriptor_keeps_its_semantic_identity():
    from Infernux.components.fields import FieldType
    from Infernux.engine.ui.inspector_utils import build_scalar_desc

    metadata = SimpleNamespace(
        field_type=FieldType.FLOAT,
        range=None,
        drag_speed=None,
        slider=False,
        multiline=False,
        tooltip="",
    )
    desc = build_scalar_desc(
        "##speed",
        "Speed",
        metadata,
        4.0,
        semantic_id="inspector.object.7.component.11.speed",
    )

    assert desc is not None
    assert desc["w"] == "##speed"
    assert desc["sid"] == "inspector.object.7.component.11.speed"


def test_scalar_batch_descriptor_preserves_full_int32_range_as_integers():
    from Infernux.components.fields import FieldType
    from Infernux.engine.ui.inspector_utils import build_scalar_desc

    metadata = SimpleNamespace(
        field_type=FieldType.INT,
        range=(0, 2147483647),
        drag_speed=None,
        slider=True,
        multiline=False,
        tooltip="",
    )

    desc = build_scalar_desc("##seed", "Random Seed", metadata, 0)

    assert desc is not None
    assert type(desc["mn"]) is int
    assert type(desc["mx"]) is int
    assert desc["mn"] == 0
    assert desc["mx"] == 2147483647


def test_sprite_renderer_exposes_native_shadow_fields_to_inspector():
    from Infernux.components.builtin.sprite_renderer import SpriteRenderer
    from Infernux.engine.ui.inspector_components import _collect_cpp_properties

    properties = dict(_collect_cpp_properties(SpriteRenderer))

    assert properties["casts_shadows"].cpp_attr == "casts_shadows"
    assert properties["casts_shadows"].metadata.default is False
    assert properties["receives_shadows"].cpp_attr == "receives_shadows"
    assert properties["receives_shadows"].metadata.default is True


def test_text_inspector_exposes_stable_semantics_for_editable_fields(monkeypatch):
    import Infernux.engine.ui.inspector_ui_components as module

    monkeypatch.setattr(module, "render_compact_section_header", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 160.0)
    monkeypatch.setattr(module, "_render_font_picker", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "_render_text_alignment_row", lambda *_args, **_kwargs: None)

    ctx = _FakeTextContext()
    module._render_text_typography(ctx, _text_component())

    semantic_ids = {item[3] for item in ctx.semantic_items}
    assert {
        "inspector.object.55.component.186.text",
        "inspector.object.55.component.186.font_size",
        "inspector.object.55.component.186.line_height",
        "inspector.object.55.component.186.letter_spacing",
    } <= semantic_ids


def test_inline_button_rows_record_each_stable_action():
    from Infernux.engine.ui.theme import Theme

    ctx = _FakeThemeContext()
    Theme.render_inline_button_row(
        ctx,
        "alignment",
        [("left", "Left"), ("right", "Right")],
        semantic_base="inspector.object.55.component.186.alignment",
    )

    assert [item[3] for item in ctx.semantic_items] == [
        "inspector.object.55.component.186.alignment.left",
        "inspector.object.55.component.186.alignment.right",
    ]


def test_ui_layout_vector_exposes_stable_axis_semantic_base(monkeypatch):
    import Infernux.engine.ui.inspector_ui_components as module

    monkeypatch.setattr(module, "render_compact_section_header", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "render_compact_section_title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 160.0)
    monkeypatch.setattr(module.Theme, "render_inline_button_row", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_canvas_dims", lambda *_args, **_kwargs: (None, 0.0, 0.0))

    component = SimpleNamespace(
        game_object=SimpleNamespace(id=55),
        component_id=203,
        width=280.0,
        height=72.0,
        lock_aspect_ratio=False,
        texture_path="",
    )
    ctx = _FakeVectorContext()

    module._render_common_layout(ctx, component)

    assert ctx.vector_semantics == ["inspector.object.55.component.203.size"]


def test_ui_size_edit_keeps_position_set_after_rect_was_cached():
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.inspector_ui_components import (
        _apply_size_preserve_top_left,
        _apply_visual_position,
    )
    from Infernux.ui import UIButton
    from Infernux.ui.inx_ui_screen_component import clear_rect_cache
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    canvas = SimpleNamespace(reference_width=1920, reference_height=1080)
    button = UIButton()
    button._get_parent_world_rect = lambda width, height: (0.0, 0.0, float(width), float(height))
    clear_rect_cache(1)

    try:
        initial_rect = button.get_visual_rect(1920, 1080)
        assert initial_rect[:2] == (0.0, 0.0)
        _apply_visual_position(button, 820.0, 620.0, canvas)
        assert len(manager.action_journal.applied_entries()) == 1
        _apply_size_preserve_top_left(button, 280.0, 72.0, canvas)

        assert button.get_visual_rect(1920, 1080) == (820.0, 620.0, 280.0, 72.0)
        assert (button.x, button.y) == (820.0, 620.0)
        assert len(manager.action_journal.applied_entries()) == 2

        manager.undo()
        assert button.get_visual_rect(1920, 1080) == (
            820.0,
            620.0,
            initial_rect[2],
            initial_rect[3],
        )
        manager.undo()
        assert button.get_visual_rect(1920, 1080) == initial_rect
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_object_field_picker_button_opens_picker_and_records_semantic(monkeypatch):
    from Infernux.engine.ui.igui import IGUI

    monkeypatch.setattr(IGUI, "_mini_icon_button", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(IGUI, "_render_object_picker_popup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(IGUI, "_draw_item_outline", lambda *_args, **_kwargs: None)

    ctx = _FakeObjectFieldContext()
    clicked = IGUI.object_field(
        ctx,
        "engine_clip",
        "None",
        "AudioClip",
        picker_asset_items=lambda _filter: [],
        semantic_id="inspector.object.11.component.186.track_0.clip",
    )

    assert clicked is False
    assert ctx.opened_popups == ["##obj_picker"]
    assert ctx.semantic_items == [(
        "object_field",
        "None",
        True,
        "inspector.object.11.component.186.track_0.clip",
    )]


def test_object_field_body_locates_and_double_click_can_open(monkeypatch):
    from Infernux.engine.ui.igui import IGUI

    monkeypatch.setattr(IGUI, "_render_object_picker_popup", lambda *_args, **_kwargs: None)
    located = []
    opened = []

    clicked = IGUI.object_field(
        _FakeObjectFieldContext(interaction=1),
        "material",
        "Smoke",
        "Material",
        on_ping=lambda: located.append("material"),
    )
    assert clicked is True
    assert located == ["material"]

    IGUI.object_field(
        _FakeObjectFieldContext(interaction=5),
        "material",
        "Smoke",
        "Material",
        on_ping=lambda: located.append("again"),
        on_open=lambda: opened.append("material"),
    )
    assert located == ["material"]
    assert opened == ["material"]


def test_object_picker_executes_mutation_after_imgui_scopes_close(monkeypatch):
    from Infernux.engine.ui.igui import IGUI

    events = []

    class _PickerContext(_FakeObjectFieldContext):
        def __init__(self):
            super().__init__(interaction=0)

        def push_id_str(self, value):
            events.append(("push", value))

        def pop_id(self):
            events.append(("pop", None))

        def begin_popup(self, _popup_id):
            return True

        def end_popup(self):
            events.append(("end_popup", None))

        def input_text_with_hint(self, *_args):
            return ""

        def separator(self):
            pass

        def begin_child(self, *_args):
            return True

        def end_child(self):
            events.append(("end_child", None))

        def selectable_list_clipped(self, labels):
            assert len(labels) == 1
            assert labels[0] == "Smoke.png"
            return 0

        def close_current_popup(self):
            events.append(("close_popup", None))

    monkeypatch.setattr(IGUI, "_draw_item_outline", lambda *_args, **_kwargs: None)
    IGUI.object_field(
        _PickerContext(),
        "texture",
        "Smoke.png",
        "Texture",
        picker_asset_items=lambda _filter: [("Smoke.png", "Assets/Smoke.png")],
        on_pick=lambda value: events.append(("callback", value)),
    )

    assert events[-3:] == [
        ("end_popup", None),
        ("pop", None),
        ("callback", "Assets/Smoke.png"),
    ]


def test_inspector_property_edit_fails_closed_without_transaction_authority():
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui._inspector_undo import _record_property
    from Infernux.engine.undo import UndoManager

    previous = UndoManager._instance
    core = EditorInteractionCore()
    UndoManager._instance = None
    target = SimpleNamespace(speed=1.0)
    try:
        with pytest.raises(RuntimeError, match="active UndoManager"):
            _record_property(target, "speed", 1.0, 2.0, "Set Speed")
        assert target.speed == 1.0
    finally:
        core.shutdown()
        UndoManager._instance = previous


def test_multi_builtin_inspector_edit_is_one_global_action():
    from Infernux.components.fields import FieldType
    from Infernux.engine.ui.inspector_components import _apply_multi_builtin_change
    from Infernux.engine.undo import UndoManager

    first = SimpleNamespace(value=1.0)
    second = SimpleNamespace(value=2.0)
    metadata = SimpleNamespace(field_type=FieldType.FLOAT, readonly=False)
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        _apply_multi_builtin_change(
            (first, second), ("value", "value"), metadata, 5.0
        )

        assert (first.value, second.value) == (5.0, 5.0)
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert (first.value, second.value) == (1.0, 2.0)
    finally:
        UndoManager._instance = previous


def test_python_asset_reference_field_uses_component_semantic(monkeypatch):
    import Infernux.engine.ui._inspector_references as module
    from Infernux.components.fields import FieldType

    captured = {}
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "render_asset_reference_field",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    component = SimpleNamespace(
        game_object=SimpleNamespace(id=143),
        component_id=547,
        controller=None,
    )
    metadata = SimpleNamespace(asset_type="AnimFSM")

    module._render_asset_reference_field(
        SimpleNamespace(), component, "controller", metadata, None, FieldType.ASSET, 120.0,
    )

    assert captured["semantic_id"] == "inspector.object.143.component.547.controller"


def test_python_generic_asset_field_uses_unified_reference_widget(monkeypatch):
    import Infernux.engine.ui.inspector_components as module
    from Infernux.components.particle_system import ParticleSystem
    from Infernux.components.fields import FieldType, get_serialized_fields

    metadata = get_serialized_fields(ParticleSystem)["graph"]
    rendered = []
    flushed = []
    monkeypatch.setattr(
        module,
        "_render_asset_reference_field",
        lambda *args, **kwargs: rendered.append((args, kwargs)),
    )
    monkeypatch.setattr(module, "_tooltip_and_info", lambda *_args: None)

    handled = module._render_py_nonscalar_field(
        SimpleNamespace(),
        SimpleNamespace(graph=metadata.default),
        "graph",
        metadata,
        metadata.default,
        120.0,
        lambda: flushed.append(True),
    )

    assert metadata.field_type == FieldType.ASSET
    assert metadata.asset_type == "ParticleGraph"
    assert handled is True
    assert flushed == [True]
    assert len(rendered) == 1


def test_all_asset_reference_widgets_use_the_unified_typed_contract():
    import ast
    from pathlib import Path

    package_root = Path(__file__).resolve().parents[1] / "Infernux"
    issues = []
    for source_path in package_root.rglob("*.py"):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8-sig"),
            filename=str(source_path),
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            else:
                continue
            if function_name not in {
                "asset_reference_field",
                "render_asset_reference_field",
            }:
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
            if "asset_type" not in keywords:
                issues.append(f"{source_path}:{node.lineno}: missing asset_type")
            if "reference_value" not in keywords and "transaction" not in keywords:
                issues.append(
                    f"{source_path}:{node.lineno}: missing reference_value/transaction"
                )

    assert issues == []


def test_scene_reference_fields_use_hierarchy_ping_contract(monkeypatch):
    import Infernux.engine.ui._inspector_references as module
    from Infernux.components.ref_wrappers import ComponentRef, GameObjectRef

    captured = []
    pinged = []
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "render_object_field",
        lambda *_args, **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        module,
        "ping_scene_object_in_hierarchy",
        lambda object_id: pinged.append(int(object_id)),
    )

    component_ref = ComponentRef(go_id=41, component_type="Camera")
    monkeypatch.setattr(ComponentRef, "resolve", lambda _self: None)
    python_component = SimpleNamespace(
        game_object=SimpleNamespace(id=9),
        component_id=10,
        target=component_ref,
    )
    metadata = SimpleNamespace(component_type="Camera")
    module._render_component_ref_inline(
        SimpleNamespace(), python_component, "target", metadata, 120.0
    )
    captured[-1]["on_ping"]()

    game_object_ref = GameObjectRef(persistent_id=52)
    monkeypatch.setattr(GameObjectRef, "resolve", lambda _self: None)
    python_component.owner = game_object_ref
    metadata = SimpleNamespace(required_component="")
    module._render_gameobject_ref_inline(
        SimpleNamespace(), python_component, "owner", metadata, game_object_ref, 120.0
    )
    captured[-1]["on_ping"]()

    assert pinged == [41, 52]


def test_builtin_asset_reference_field_records_native_property(monkeypatch):
    import Infernux.engine.ui._inspector_references as module
    from Infernux.components.fields import FieldType
    from Infernux.core.asset_ref import PhysicMaterialRef

    captured = {}
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "render_asset_reference_field",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    component = SimpleNamespace(
        game_object=SimpleNamespace(id=21),
        component_id=34,
        physic_material=PhysicMaterialRef(),
    )
    metadata = SimpleNamespace(asset_type="PhysicMaterial")

    from Infernux.engine.undo import UndoManager

    previous = UndoManager._instance
    manager = UndoManager()
    try:
        module._render_asset_reference_field(
            SimpleNamespace(), component, "physic_material", metadata,
            component.physic_material, FieldType.ASSET, 120.0,
            builtin_attr="physic_material",
        )
        transaction = captured["transaction"]
        assert transaction.commit(
            "C:/project/Assets/Bouncy.physicMaterial"
        ).value == "applied"

        assert isinstance(component.physic_material, PhysicMaterialRef)
        assert component.physic_material.path_hint.endswith("Bouncy.physicMaterial")
        assert len(manager.action_journal.applied_entries()) == 1
    finally:
        UndoManager._instance = previous


def test_inline_material_state_and_preview_query_are_reused(monkeypatch):
    import Infernux.engine.ui.inspector_material as module
    from Infernux.engine.ui import asset_resource_preview

    serialize_calls = []

    class NativeMaterial:
        file_path = "C:/project/Assets/Test.mat"

        def __init__(self):
            self.version = 4

        def get_version(self):
            return self.version

        def serialize_document(self):
            serialize_calls.append(True)
            return {"properties": {}, "shaders": {}}

    panel = SimpleNamespace()
    native = NativeMaterial()
    first_state = module._build_inline_state(panel, native)
    calls_after_first = len(serialize_calls)
    second_state = module._build_inline_state(panel, native)

    assert second_state is first_state
    assert second_state.exec_layer is first_state.exec_layer
    assert calls_after_first > 0
    assert len(serialize_calls) == calls_after_first

    native.version = 5
    third_state = module._build_inline_state(panel, native)
    assert third_state is not first_state
    assert len(serialize_calls) > calls_after_first

    queries = []
    preview_native = SimpleNamespace(
        get_material_preview_texture_id=lambda key: 73 if key == "material-key" else 0
    )
    monkeypatch.setattr(
        asset_resource_preview,
        "_resolve_native_engine",
        lambda _panel: preview_native,
    )
    monkeypatch.setattr(
        module,
        "_query_material_preview_tex",
        lambda *_args: queries.append(True) or (73, "material-key"),
    )
    monkeypatch.setattr(module, "_is_material_preview_ready", lambda *_args: True)
    assert module._get_cached_material_preview_tex(
        panel, native, {}, first_state, "stable", native.file_path,
    ) == 73
    assert module._get_cached_material_preview_tex(
        panel, native, {}, first_state, "stable", native.file_path,
    ) == 73
    assert len(queries) == 1


def test_material_preview_does_not_cache_stale_generation(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    state = SimpleNamespace(extra={})
    queries = iter(((41, "material-key"), (42, "material-key"), (42, "material-key")))
    ready = iter((False, True, True))
    monkeypatch.setattr(module, "_query_material_preview_tex", lambda *_args: next(queries))
    monkeypatch.setattr(module, "_is_material_preview_ready", lambda *_args: next(ready))

    args = (SimpleNamespace(), None, {}, state, "new-json", "C:/project/Assets/Test.mat")
    assert module._get_cached_material_preview_tex(*args) == 41
    assert module._get_cached_material_preview_tex(*args) == 42
    assert module._get_cached_material_preview_tex(*args) == 42


def test_material_preview_prefers_new_live_document_over_stale_cache_tag(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    query_args = []
    state = SimpleNamespace(extra={"cached_json": '{"color":"B"}'})
    monkeypatch.setattr(
        module,
        "_query_material_preview_tex",
        lambda *args: query_args.append(args) or (77, "material-key"),
    )
    monkeypatch.setattr(module, "_is_material_preview_ready", lambda *_args: True)

    assert module._get_cached_material_preview_tex(
        SimpleNamespace(), None, {}, state, '{"color":"A"}',
        "C:/project/Assets/Test.mat",
    ) == 77
    assert query_args[0][4] == '{"color":"B"}'
    assert state.extra["_material_cache_tag"] == '{"color":"B"}'


def test_shader_reference_recovers_from_stale_path_hint(monkeypatch, tmp_path):
    import os

    from Infernux.engine.ui import inspector_shader_utils as shader_utils

    shader_path = tmp_path / "Recovered.frag"
    shader_path.write_text('ShaderInfo { Name "Recovered" }\n', encoding="utf-8")
    monkeypatch.setattr(
        shader_utils,
        "_get_shader_catalog",
        lambda _ext: {"items": [("Recovered", "Recovered")], "paths": {"Recovered": str(shader_path)}},
    )
    monkeypatch.setattr(
        shader_utils,
        "_read_compiled_shader_metadata",
        lambda path: {"shader_id": "Recovered", "guid": "recovered-guid"}
        if os.path.normpath(path) == os.path.normpath(str(shader_path)) else None,
    )

    reference = shader_utils.make_shader_reference(
        {
            "guid": "",
            "shader_id": "Recovered",
            "path_hint": str(tmp_path / "OldLocation.frag"),
        },
        ".frag",
    )

    assert reference == {
        "guid": "recovered-guid",
        "shader_id": "Recovered",
        "path_hint": os.path.normpath(str(shader_path)).replace("\\", "/"),
    }


def test_shader_picker_path_is_resolved_to_shader_info_name(monkeypatch, tmp_path):
    import os

    from Infernux.engine.ui import inspector_shader_utils as shader_utils

    shader_path = tmp_path / "lit.frag"
    shader_path.write_text('ShaderInfo { Name "Lit" }\n', encoding="utf-8")
    monkeypatch.setattr(shader_utils, "_read_compiled_shader_metadata", lambda _path: None)
    monkeypatch.setattr(shader_utils, "_get_shader_catalog", lambda _ext: {"items": [], "paths": {}})

    reference = shader_utils.make_shader_reference(str(shader_path), ".frag")

    assert reference["shader_id"] == "Lit"
    assert reference["shader_id"] != str(shader_path)
    assert reference["path_hint"] == os.path.normpath(str(shader_path)).replace("\\", "/")


def test_builtin_shader_picker_persists_only_the_shader_id(monkeypatch, tmp_path):
    from Infernux.engine import project_context
    from Infernux.engine.ui import inspector_shader_utils as shader_utils

    project_root = tmp_path / "Project"
    shader_path = project_root / "Library" / "Resources" / "shaders" / "lit.frag"
    shader_path.parent.mkdir(parents=True)
    shader_path.write_text('ShaderInfo { Name "Lit" }\n', encoding="utf-8")
    monkeypatch.setattr(project_context, "_project_root", str(project_root))
    monkeypatch.setattr(shader_utils, "_read_compiled_shader_metadata", lambda _path: None)
    monkeypatch.setattr(shader_utils, "_get_shader_catalog", lambda _ext: {"items": [], "paths": {}})

    reference = shader_utils.make_shader_reference(str(shader_path), ".frag")

    assert reference == {"guid": "", "shader_id": "Lit", "path_hint": ""}


def test_generated_library_shader_persists_only_id_before_project_context(
    monkeypatch, tmp_path
):
    from Infernux.engine import project_context
    from Infernux.engine.ui import inspector_shader_utils as shader_utils

    shader_path = tmp_path / "Project" / "Library" / "Resources" / "shaders" / "standard.vert"
    shader_path.parent.mkdir(parents=True)
    shader_path.write_text('ShaderInfo { Name "Standard" }\n', encoding="utf-8")
    monkeypatch.setattr(project_context, "_project_root", None)
    monkeypatch.setattr(shader_utils, "_read_compiled_shader_metadata", lambda _path: None)
    monkeypatch.setattr(shader_utils, "_get_shader_catalog", lambda _ext: {"items": [], "paths": {}})

    reference = shader_utils.make_shader_reference(str(shader_path), ".vert")

    assert reference == {"guid": "", "shader_id": "Standard", "path_hint": ""}


def test_initial_material_preview_uses_the_in_memory_document(monkeypatch, tmp_path):
    import Infernux.engine.ui.inspector_material as module
    from Infernux.engine.ui import asset_resource_preview

    path = tmp_path / "Fresh.mat"
    path.write_text('{"name":"Fresh"}', encoding="utf-8")
    calls = []
    native = object()
    monkeypatch.setattr(asset_resource_preview, "_resolve_native_engine", lambda _panel: native)
    monkeypatch.setattr(asset_resource_preview, "_try_get_cpp_material_preview_texture",
                        lambda native, preview_path, **kwargs:
                        calls.append((native, preview_path, kwargs)) or 17)

    document = {"name": "Fresh"}
    tex = module._query_material_preview_tex(
        SimpleNamespace(), None, document,
        SimpleNamespace(extra={"cached_json": json.dumps(document)}), "", str(path),
    )

    assert tex == (17, f"mat|{path}")
    assert calls == [(native, str(path), {
        "material_json": json.dumps(document),
        "file_mtime_hint": 0,
    })]


def test_texture_preview_uses_live_settings_generation(tmp_path):
    from Infernux.engine.ui import asset_resource_preview

    path = tmp_path / "Live.png"
    path.write_bytes(b"preview-source")

    class _Native:
        def __init__(self):
            self.calls = []

        def query_or_schedule_texture_preview(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return 7, 64, 64

    native = _Native()
    settings = TextureImportSettings()
    asset_resource_preview._try_get_cpp_texture_preview(native, str(path), settings)
    settings.generate_mipmaps = False
    asset_resource_preview._try_get_cpp_texture_preview(native, str(path), settings)

    assert all(call[0][0].startswith("tex|") for call in native.calls)
    assert native.calls[0][0][2] != native.calls[1][0][2]
    assert native.calls[0][1]["max_size"] == 2048
    assert native.calls[0][1]["texture_format"] == "auto"
    assert native.calls[0][1]["texture_type"] == "default"
    assert native.calls[0][1]["authoring"] is True


def test_texture_live_preview_can_be_released(monkeypatch, tmp_path):
    from Infernux.engine.ui import asset_resource_preview

    class _Native:
        def __init__(self):
            self.released = []

        def release_preview_authoring(self, key):
            self.released.append(key)

    native = _Native()
    monkeypatch.setattr(asset_resource_preview, "_resolve_native_engine", lambda _panel: native)
    path = tmp_path / "Released.png"
    asset_resource_preview.invalidate_live_texture_preview(str(path))

    assert native.released == [f"tex|{os.path.normpath(path)}"]


def test_material_undo_snapshot_is_decoded_only_when_edit_occurs():
    import Infernux.engine.ui.inspector_material as module

    state = SimpleNamespace(extra={"cached_json": '{"properties":{"roughness":0.25}}'})
    edited_document = {"properties": {"roughness": 0.75}}

    assert module._document_before_material_edit(state, edited_document) == {
        "properties": {"roughness": 0.25}
    }


def test_material_surface_controls_use_one_native_batch():
    import Infernux.engine.ui.inspector_material as module

    captured = []

    class Context:
        @staticmethod
        def create_property_batch_plan(descriptors):
            return descriptors

        @staticmethod
        def render_property_batch_plan_values(descriptors, values, label_width):
            captured.append((descriptors, label_width))
            return {0: 1}

    document = {
        "renderState": {
            "blendEnable": False,
            "depthWriteEnable": True,
            "depthTestEnable": True,
            "depthCompareOp": 1,
            "cullMode": 2,
            "renderQueue": 2000,
        },
        "renderStateOverrides": 0,
    }
    overrides, change_key = module._render_surface_options_batch(
        Context(), document["renderState"], document, 0, 144.0,
    )

    assert len(captured) == 1
    assert len(captured[0][0]) == 6
    assert captured[0][1] == 144.0
    assert document["renderState"]["blendEnable"] is True
    assert document["renderState"]["renderQueue"] == 3000
    assert overrides == document["renderStateOverrides"]
    assert change_key == "render_state.surface"


def test_material_surface_batch_reuses_structure_but_refreshes_live_values():
    import Infernux.engine.ui.inspector_material as module

    class Context:
        def __init__(self):
            self.plan_builds = 0
            self.values = []

        def create_property_batch_plan(self, descriptors):
            self.plan_builds += 1
            return tuple(descriptors)

        def render_property_batch_plan_values(self, plan, values, _label_width):
            self.values.append((plan, tuple(values)))
            return {}

    ctx = Context()
    cache = {}
    render_state = {
        "blendEnable": False,
        "depthWriteEnable": True,
        "depthTestEnable": True,
        "depthCompareOp": 1,
        "cullMode": 2,
        "renderQueue": 2000,
    }
    document = {"renderStateOverrides": 0}

    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )
    render_state["renderQueue"] = 1200
    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )

    assert ctx.plan_builds == 1
    assert ctx.values[0][0] is ctx.values[1][0]
    assert ctx.values[0][1] != ctx.values[1][1]

    render_state["alphaClipEnabled"] = True
    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )
    assert ctx.plan_builds == 2


def test_material_surface_batch_keeps_complete_native_enum_contract_in_cached_plan():
    import Infernux.engine.ui.inspector_material as module

    class Context:
        def __init__(self):
            self.plans = []

        def create_property_batch_plan(self, descriptors):
            required_by_type = {
                0: ("f",),
                1: ("i",),
                2: ("b",),
                7: ("ei", "en"),
            }
            for descriptor in descriptors:
                required = required_by_type[descriptor["t"]]
                assert all(key in descriptor for key in required)
            self.plans.append(tuple(descriptors))
            return self.plans[-1]

        @staticmethod
        def render_property_batch_plan_values(_plan, _values, _label_width):
            return {}

    ctx = Context()
    cache = {}
    render_state = {
        "blendEnable": True,
        "depthWriteEnable": False,
        "depthTestEnable": True,
        "depthCompareOp": 1,
        "cullMode": 2,
        "srcColorBlendFactor": 6,
        "dstColorBlendFactor": 7,
        "alphaClipEnabled": True,
        "alphaClipThreshold": 0.5,
        "renderQueue": 3000,
    }
    document = {"renderStateOverrides": 0}

    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )
    descriptors = ctx.plans[0]
    enum_descriptors = [desc for desc in descriptors if desc["t"] == 7]

    assert enum_descriptors
    assert all("ei" in desc and "en" in desc for desc in enum_descriptors)
    assert all(0 <= desc["ei"] < len(desc["en"]) for desc in enum_descriptors)
    assert any(desc["t"] == 0 and "f" in desc for desc in descriptors)
    assert any(desc["t"] == 1 and "i" in desc for desc in descriptors)
    assert all(desc["t"] != 2 or "b" in desc for desc in descriptors)

    # The cached plan must retain the same complete schema while live values
    # continue to come from the current render state.
    render_state["srcColorBlendFactor"] = 1
    render_state["dstColorBlendFactor"] = 1
    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )
    assert len(ctx.plans) == 1

    # A cache created before the enum descriptor contract was restored must be
    # discarded instead of reaching the native decoder with a missing ``ei``.
    cache["entries"][0][1].pop("ei")
    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )
    assert len(ctx.plans) == 2
    assert all(
        "ei" in desc
        for _, desc in cache["entries"]
        if desc["t"] == 7
    )


def test_material_surface_batch_rebuilds_for_locale_even_when_width_is_unchanged(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    locale = {"value": "zh"}
    monkeypatch.setattr(module, "get_locale", lambda: locale["value"])
    # Keep every translated label the same width so width-only invalidation
    # cannot make this test pass accidentally.
    monkeypatch.setattr(module, "t", lambda _key: "xxxx")

    class Context:
        def __init__(self):
            self.plan_builds = 0

        def create_property_batch_plan(self, descriptors):
            self.plan_builds += 1
            return tuple(descriptors)

        @staticmethod
        def render_property_batch_plan_values(_plan, _values, _label_width):
            return {}

    ctx = Context()
    cache = {}
    render_state = {
        "blendEnable": False,
        "depthWriteEnable": True,
        "depthTestEnable": True,
        "depthCompareOp": 1,
        "cullMode": 2,
        "renderQueue": 2000,
    }
    document = {"renderStateOverrides": 0}

    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )
    locale["value"] = "en"
    module._render_surface_options_batch(
        ctx, render_state, document, 0, 144.0, cache=cache,
    )

    assert ctx.plan_builds == 2


def test_audio_track_renderer_exposes_picker_callbacks_and_semantic(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module
    from Infernux.engine.play_mode import PlayModeManager

    captured = {}
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(
        module,
        "render_asset_reference_field",
        lambda *args, **kwargs: captured.update({"args": args, **kwargs}),
    )
    monkeypatch.setattr(PlayModeManager, "instance", staticmethod(lambda: None))

    comp = SimpleNamespace(
        game_object=SimpleNamespace(id=11),
        component_id=186,
        track_count=1,
        get_track_clip=lambda _index: None,
        get_track_volume=lambda _index: 1.0,
    )
    ctx = SimpleNamespace(
        separator=lambda: None,
        label=lambda _value: None,
        set_next_item_open=lambda _value: None,
        collapsing_header=lambda _value: True,
        float_slider=lambda _label, value, _min, _max: value,
    )

    module._render_audio_source_extra(ctx, comp)

    assert captured["args"][3] == "AudioClip"
    assert "picker_asset_items" not in captured
    assert captured["has_value"] is False
    assert callable(captured["on_assign"])
    assert "on_pick" not in captured
    assert "on_drop_callback" not in captured
    assert callable(captured["on_clear"])
    assert captured["semantic_id"] == "inspector.object.11.component.186.track_0.clip"


def test_particle_parameter_renderer_adapts_vector_storage(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module
    from Infernux.lib import Vector3

    captured = {}
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(module, "render_compact_section_header", lambda *_args, **_kwargs: True)

    def _render(_ctx, _wid, _label, _metadata, current, _label_width):
        captured["current"] = current
        return Vector3(4.0, 5.0, 6.0)

    monkeypatch.setattr(module, "render_serialized_field", _render)
    monkeypatch.setattr(
        module,
        "_record_python_component_document_edit",
        lambda _component, edit, *_args, **_kwargs: edit(),
    )
    updates = []
    comp = SimpleNamespace(
        exposed_parameter_schema=lambda: [{
            "stable_id": "wind-id",
            "name": "Wind",
            "type": "vec3",
            "default": [1.0, 2.0, 3.0],
            "value": [1.0, 2.0, 3.0],
            "category": "",
            "tooltip": "",
        }],
        set_parameter=lambda stable_id, value: updates.append((stable_id, value)),
    )
    ctx = SimpleNamespace(separator=lambda: None, is_item_hovered=lambda: False)

    module._render_particle_system_parameters(ctx, comp)

    assert (captured["current"].x, captured["current"].y, captured["current"].z) == (1.0, 2.0, 3.0)
    assert updates == [("wind-id", [4.0, 5.0, 6.0])]


def test_particle_texture_parameter_renders_in_the_parameter_section(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module
    import Infernux.lib as lib

    class _AssetDatabase:
        @staticmethod
        def get_guid_from_path(path):
            return "smoke-guid" if str(path).endswith("Smoke.png") else ""

    class _Registry:
        @staticmethod
        def get_asset_database():
            return _AssetDatabase()

    class _AssetRegistry:
        @staticmethod
        def instance():
            return _Registry()

    class _ParticleSystem:
        def __init__(self):
            self.path = "Assets/VFX/Default.png"

        def exposed_parameter_schema(self):
            return [{
                "stable_id": "smoke-texture",
                "name": "Smoke Texture",
                "type": "texture2d",
                "category": "Smoke",
                "tooltip": "",
                "default": {"guid": "default-guid", "path_hint": self.path},
                "value": {"guid": "default-guid", "path_hint": self.path},
            }]

        def set_parameter(self, stable_id, value):
            assert stable_id == "smoke-texture"
            assert value["guid"] == "smoke-guid"
            self.path = value["path_hint"]

        def reset_parameter(self, stable_id):
            assert stable_id == "smoke-texture"
            self.path = ""
            return True

    captured = {}
    monkeypatch.setattr(lib, "AssetRegistry", _AssetRegistry)
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module, "render_compact_section_header", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        module, "render_compact_section_title", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        module,
        "render_asset_reference_field",
        lambda _ctx, _field_id, _name, _label, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        module,
        "_record_python_component_document_edit",
        lambda _component, edit, *_args, **_kwargs: edit(),
    )
    monkeypatch.setattr(
        module, "_portable_asset_path_hint", lambda path: str(path).replace("\\", "/")
    )
    component = _ParticleSystem()
    ctx = SimpleNamespace(separator=lambda: None)

    module._render_particle_system_parameters(ctx, component)
    captured["on_assign"]("Assets/VFX/Smoke.png")
    captured["on_clear"]()

    assert captured["accept_drag_type"] == (
        "TEXTURE_GUID",
        "TEXTURE_FILE",
        "ASSET_FILE",
    )
    assert "picker_asset_items" not in captured
    assert captured["has_value"] is True
    assert component.path == ""


def test_particle_instance_sections_are_real_collapsible_groups(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module

    rendered = []
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(
        module,
        "render_compact_section_header",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        module,
        "render_serialized_field",
        lambda *_args, **_kwargs: rendered.append(True),
    )
    component = SimpleNamespace(
        component_id=9,
        emitter_instance_schema=lambda: [
            {
                "stable_id": "smoke",
                "name": "Smoke",
                "enabled": True,
                "play_on_start": True,
            }
        ],
        exposed_parameter_schema=lambda: [
            {
                "stable_id": "density",
                "name": "Density",
                "type": "f32",
                "default": 1.0,
                "value": 1.0,
                "category": "",
                "tooltip": "",
            }
        ],
    )

    module._render_particle_system_parameters(
        SimpleNamespace(separator=lambda: None), component
    )

    assert rendered == []


def test_particle_emitter_playback_controls_edit_the_component_instance(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module

    updates = []
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(
        module, "render_compact_section_header", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        module, "render_compact_section_title", lambda *_args, **_kwargs: None
    )

    def _render(_ctx, widget_id, _label, _metadata, current, _label_width):
        return not current if widget_id.endswith("_enabled") else current

    monkeypatch.setattr(module, "render_serialized_field", _render)
    monkeypatch.setattr(
        module,
        "_record_python_component_document_edit",
        lambda _component, edit, *_args, **_kwargs: edit(),
    )
    component = SimpleNamespace(
        component_id=12,
        emitter_instance_schema=lambda: [
            {
                "stable_id": "smoke",
                "name": "Smoke",
                "enabled": True,
                "play_on_start": True,
            }
        ],
        exposed_parameter_schema=lambda: [],
        set_emitter_options=lambda stable_id, **options: updates.append(
            (stable_id, options)
        ),
    )

    module._render_particle_system_parameters(
        SimpleNamespace(separator=lambda: None), component
    )

    assert updates == [("smoke", {"enabled": False})]


def test_particle_emitter_playback_controls_have_stable_unique_imgui_ids(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module

    labels = []
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(
        module, "render_compact_section_header", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        module, "render_compact_section_title", lambda *_args, **_kwargs: None
    )

    def _render(_ctx, _widget_id, label, _metadata, current, _label_width):
        labels.append(label)
        return current

    monkeypatch.setattr(module, "render_serialized_field", _render)
    component = SimpleNamespace(
        component_id=12,
        emitter_instance_schema=lambda: [
            {
                "stable_id": stable_id,
                "name": name,
                "enabled": True,
                "play_on_start": True,
            }
            for stable_id, name in (("smoke", "Smoke"), ("sparks", "Sparks"))
        ],
        exposed_parameter_schema=lambda: [],
        set_emitter_options=lambda *_args, **_kwargs: None,
    )

    module._render_particle_system_parameters(
        SimpleNamespace(separator=lambda: None), component
    )

    assert len(labels) == 4
    assert len(set(labels)) == 4
    visible_labels = [label.split("##", 1)[0] for label in labels]
    assert visible_labels[:2] == visible_labels[2:]
    assert visible_labels[0] != visible_labels[1]


def test_audio_track_picker_assigns_and_clears_registered_guid(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module
    import Infernux.lib as lib

    class _AssetDatabase:
        def get_guid_from_path(self, path):
            return "audio-guid" if str(path).endswith("engine_loop.wav") else ""

    class _Registry:
        def get_asset_database(self):
            return _AssetDatabase()

    class _AssetRegistry:
        @staticmethod
        def instance():
            return _Registry()

    class _AudioSource:
        def __init__(self):
            self.guid = ""

        def serialize_document(self):
            return {"track_guid": self.guid}

        def set_track_clip_by_guid(self, _index, guid):
            self.guid = guid

    changes = []
    monkeypatch.setattr(lib, "AssetRegistry", _AssetRegistry)
    monkeypatch.setattr(
        module,
        "_record_generic_component",
        lambda _comp, old, new: changes.append((old, new)),
    )

    comp = _AudioSource()
    module._apply_track_audio_clip_pick(comp, 0, "Assets/Audio/engine_loop.wav")
    module._clear_track_audio_clip(comp, 0)

    assert changes == [
        ({"track_guid": ""}, {"track_guid": "audio-guid"}),
        ({"track_guid": "audio-guid"}, {"track_guid": ""}),
    ]


class _ImportContext:
    def __init__(self):
        self.semantics = []

    def begin_disabled(self, _disabled):
        pass

    def end_disabled(self):
        pass

    def combo(self, _label, current, _items):
        return current

    def drag_float(self, _label, value, _speed, _minimum, _maximum):
        return value

    def record_semantic_item(self, *args):
        self.semantics.append(args)


class _SpriteContext(_ImportContext):
    def separator(self):
        pass

    def label(self, _text):
        pass

    def set_next_item_width(self, _width):
        pass

    def input_int(self, label, value, _step, _step_fast):
        return 3 if label == "##sprite_cols" else value

    def button(self, _label, callback=None):
        if callback is not None:
            callback()
        return callback is not None

    def dummy(self, _width, _height):
        pass


class _ApplyContext(_ImportContext):
    def __init__(self):
        super().__init__()
        self.buttons = []

    def separator(self):
        pass

    def push_style_color(self, *_args):
        pass

    def pop_style_color(self, _count):
        pass

    def same_line(self):
        pass

    def button(self, label, _callback):
        self.buttons.append(label)


def test_texture_import_fields_publish_stable_semantics(monkeypatch):
    details._ensure_categories()
    state = details._State()
    state.category = "texture"
    state.settings = TextureImportSettings()
    ctx = _ImportContext()

    monkeypatch.setattr(inspector_utils, "render_compact_section_header", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(inspector_utils, "render_inspector_checkbox", lambda _ctx, _label, value: value)
    monkeypatch.setattr(details, "max_label_w", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(details, "field_label", lambda *_args, **_kwargs: None)

    details._render_import_fields(ctx, details._categories["texture"], state)

    by_id = {entry[3]: entry for entry in ctx.semantics}
    assert set(by_id) == {
        "asset.texture.import.texture_type",
        "asset.texture.import.srgb",
        "asset.texture.import.filter_mode",
        "asset.texture.import.wrap_mode",
        "asset.texture.import.generate_mipmaps",
        "asset.texture.import.aniso_level",
        "asset.texture.import.format",
        "asset.texture.import.compression",
        "asset.texture.import.compression_quality",
        "asset.texture.import.max_size",
    }
    assert by_id["asset.texture.import.srgb"][4] is True
    assert ":" in by_id["asset.texture.import.texture_type"][1]


def test_sprite_slice_controls_are_distinct_and_report_values(monkeypatch):
    from Infernux.engine.undo import UndoManager

    settings = TextureImportSettings(
        texture_type=TextureType.SPRITE,
        sprite_frames=[
            SpriteFrame(name="frame_0", x=0, y=0, w=192, h=64),
        ],
    )
    ctx = _SpriteContext()

    details._sprite_state.reset()
    details._sprite_state.texture_id = 7
    details._sprite_state.tex_w = 192
    details._sprite_state.tex_h = 64

    monkeypatch.setattr(details, "_render_import_fields", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(details, "_ensure_sprite_texture", lambda _state: True)
    monkeypatch.setattr(details, "_render_sprite_preview", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(details, "max_label_w", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(details, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inspector_utils, "render_compact_section_header", lambda *_args, **_kwargs: True)

    state = details._State()
    state.file_path = "C:/Project/Assets/SpriteSheet.png"
    state.category = "texture"
    state.meta = {"guid": "sprite-sheet-test"}
    state.settings = settings
    state.disk_settings = settings.copy()
    applied = []
    save_requests = []
    state.exec_layer = SimpleNamespace(
        apply_import_settings=lambda value: applied.append(value.copy()) or True,
        refresh_binding=lambda _category, _path: None,
    )
    monkeypatch.setattr(
        details,
        "_auto_save_sprite",
        lambda current: save_requests.append(current.settings.copy()) or True,
    )
    previous_manager = UndoManager._instance
    UndoManager()
    try:
        details._bind_import_settings_document(
            state,
            details._categories["texture"],
        )
        details._render_sprite_body(ctx, None, state)
    finally:
        UndoManager._instance = previous_manager

    by_id = {entry[3]: entry for entry in ctx.semantics}
    assert by_id["asset.texture.sprite.rows"][1].endswith(": 1")
    assert by_id["asset.texture.sprite.columns"][1].endswith(": 3")
    assert "asset.texture.sprite.auto_slice" in by_id
    assert [(f.x, f.y, f.w, f.h) for f in state.settings.sprite_frames] == [
        (0, 0, 64, 64),
        (64, 0, 64, 64),
        (128, 0, 64, 64),
    ]
    assert applied == []
    assert len(save_requests) == 1
    assert save_requests[0].sprite_frames == state.settings.sprite_frames
    assert state.disk_settings != state.settings
    assert state.is_dirty()


def test_sprite_auto_slice_preserves_ids_for_unchanged_rectangles():
    stable = SpriteFrame(
        stable_id="a" * 32,
        name="left",
        x=0,
        y=0,
        w=64,
        h=64,
    )
    settings = TextureImportSettings(
        texture_type=TextureType.SPRITE,
        sprite_frames=[stable],
    )
    sprite_state = details._SpriteEditorState()
    sprite_state.tex_w = 128
    sprite_state.tex_h = 64
    sprite_state.slice_rows = 1
    sprite_state.slice_cols = 2

    details._auto_slice(settings, sprite_state)

    assert settings.sprite_frames[0].stable_id == stable.stable_id
    assert settings.sprite_frames[0].name == "left"
    assert len(settings.sprite_frames[1].stable_id) == 32
    assert settings.sprite_frames[1].stable_id != stable.stable_id


def test_apply_revert_bar_publishes_stable_enabled_state():
    ctx = _ApplyContext()

    inspector_utils.render_apply_revert(
        ctx,
        True,
        lambda: None,
        lambda: None,
        semantic_prefix="asset.texture.import",
    )

    assert ctx.buttons == ["Apply", "Revert"]
    assert ctx.semantics == [
        ("button", "Apply", True, "asset.texture.import.apply"),
        ("button", "Revert", True, "asset.texture.import.revert"),
    ]
def test_render_effect_asset_category_is_editable():
    details._ensure_categories()

    category = details._categories["render_effect"]

    assert category.access_mode == details.AssetAccessMode.READ_WRITE_RESOURCE
    assert category.custom_body_fn is details._render_render_effect_body


def test_only_categories_with_shared_document_contract_are_read_write():
    details._ensure_categories()

    read_write = {
        name
        for name, category in details._categories.items()
        if category.access_mode == details.AssetAccessMode.READ_WRITE_RESOURCE
    }

    assert read_write == {
        "material",
        "render_effect",
        "physic_material",
        "animclip",
        "animclip3d",
    }
    assert details._categories["prefab"].access_mode == details.AssetAccessMode.READ_ONLY_RESOURCE
    assert details._categories["animfsm"].access_mode == details.AssetAccessMode.READ_ONLY_RESOURCE
