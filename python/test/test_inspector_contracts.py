"""Stable semantic contracts for scene and asset Inspector surfaces."""

import json
import os
from types import SimpleNamespace

from Infernux.core.asset_types import TextureImportSettings, TextureType
from Infernux.engine.ui import asset_details_renderer as details
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


class _FakeObjectFieldContext(_FakeSemanticContext):
    def __init__(self):
        super().__init__()
        self.opened_popups = []

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
        has_picker, _picker_texture_id, semantic_id,
    ):
        if has_picker:
            self.open_popup("##obj_picker")
        if semantic_id:
            self.record_semantic_item(
                "object_field", display_text, clickable, semantic_id
            )
        return 1


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
    from Infernux.components.serialized_field import FieldType
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


def test_renderstack_field_semantic_uses_component_and_pass_identity():
    from Infernux.components.serialized_field import FieldType
    from Infernux.engine.ui.inspector_renderstack import _record_renderstack_field_semantic

    class _Context:
        semantic_capture_enabled = True

        def __init__(self):
            self.items = []

        def record_semantic_item(self, *args):
            self.items.append(args)

    ctx = _Context()
    stack = SimpleNamespace(game_object=SimpleNamespace(id=69), component_id=232)
    metadata = SimpleNamespace(
        field_type=FieldType.FLOAT,
        slider=False,
        readonly=False,
        multiline=False,
    )

    _record_renderstack_field_semantic(
        ctx,
        stack,
        "pass.BloomEffect.parameter.threshold",
        "Threshold",
        metadata,
        1.1,
    )

    assert ctx.items == [(
        "drag_float",
        "Threshold",
        True,
        "inspector.object.69.component.232.renderstack.pass.BloomEffect.parameter.threshold",
        None,
        1.1,
        None,
    )]


def test_renderstack_field_semantic_skips_identity_work_outside_snapshot():
    from Infernux.components.serialized_field import FieldType
    from Infernux.engine.ui.inspector_renderstack import _record_renderstack_field_semantic

    class _NoIdentityAccess:
        @property
        def game_object(self):
            raise AssertionError("ordinary frames must not resolve RenderStack semantic identity")

    ctx = SimpleNamespace(semantic_capture_enabled=False)
    metadata = SimpleNamespace(field_type=FieldType.FLOAT)

    _record_renderstack_field_semantic(
        ctx,
        _NoIdentityAccess(),
        "pass.BloomEffect.parameter.threshold",
        "Threshold",
        metadata,
        1.0,
    )


def test_renderstack_header_and_order_use_distinct_semantic_roles(monkeypatch):
    import Infernux.engine.ui.inspector_renderstack as module

    class BloomEffect:
        name = "Bloom"

    class _Context:
        semantic_capture_enabled = True

        def __init__(self):
            self.items = []

        def record_semantic_item(self, *args):
            self.items.append(args)

        def push_id_str(self, _value):
            pass

        def pop_id(self):
            pass

        def same_line(self, *_args):
            pass

        def begin_drag_drop_source(self, _flags):
            return False

        def begin_popup_context_item(self, _popup_id):
            return False

    monkeypatch.setattr(module, "render_inspector_checkbox", lambda *_args: True)
    monkeypatch.setattr(module, "render_compact_section_header", lambda *_args, **_kwargs: False)

    ctx = _Context()
    stack = SimpleNamespace(game_object=SimpleNamespace(id=69), component_id=232)
    entry = SimpleNamespace(render_pass=BloomEffect(), enabled=True, order=100)

    module._render_mounted_effect(ctx, stack, entry)

    by_semantic_id = {item[3]: item for item in ctx.items}
    header_id = "inspector.object.69.component.232.renderstack.pass.BloomEffect.header"
    order_id = "inspector.object.69.component.232.renderstack.pass.BloomEffect.order"
    assert by_semantic_id[header_id][0] == "renderstack_pass_header"
    assert by_semantic_id[order_id][0] == "status"
    assert by_semantic_id[order_id][2] is False
    assert by_semantic_id[order_id][5] == 100.0


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
    from Infernux.engine.ui.inspector_ui_components import (
        _apply_size_preserve_top_left,
        _apply_visual_position,
    )
    from Infernux.ui import UIButton
    from Infernux.ui.inx_ui_screen_component import clear_rect_cache

    canvas = SimpleNamespace(reference_width=1920, reference_height=1080)
    button = UIButton()
    button._get_parent_world_rect = lambda width, height: (0.0, 0.0, float(width), float(height))
    clear_rect_cache(1)

    assert button.get_visual_rect(1920, 1080)[:2] == (0.0, 0.0)
    _apply_visual_position(button, 820.0, 620.0, canvas)
    _apply_size_preserve_top_left(button, 280.0, 72.0, canvas)

    assert button.get_visual_rect(1920, 1080) == (820.0, 620.0, 280.0, 72.0)
    assert (button.x, button.y) == (820.0, 620.0)


def test_object_field_click_opens_picker_and_records_semantic(monkeypatch):
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

    assert clicked is True
    assert ctx.opened_popups == ["##obj_picker"]
    assert ctx.semantic_items == [(
        "object_field",
        "None",
        True,
        "inspector.object.11.component.186.track_0.clip",
    )]


def test_python_asset_reference_field_uses_component_semantic(monkeypatch):
    import Infernux.engine.ui._inspector_references as module
    from Infernux.components.serialized_field import FieldType

    captured = {}
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "render_object_field",
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


def test_builtin_asset_reference_field_records_native_property(monkeypatch):
    import Infernux.engine.ui._inspector_references as module
    from Infernux.components.serialized_field import FieldType
    from Infernux.core.asset_ref import PhysicMaterialRef

    captured = {}
    recorded = []
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "render_object_field",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        module,
        "_record_builtin_property",
        lambda comp, attr, old, new, description: recorded.append(
            (comp, attr, old, new, description)
        ),
    )
    component = SimpleNamespace(
        game_object=SimpleNamespace(id=21),
        component_id=34,
        physic_material=PhysicMaterialRef(),
    )
    metadata = SimpleNamespace(asset_type="PhysicMaterial")

    module._render_asset_reference_field(
        SimpleNamespace(), component, "physic_material", metadata,
        component.physic_material, FieldType.ASSET, 120.0,
        builtin_attr="physic_material",
    )
    captured["on_pick"]("C:/project/Assets/Bouncy.physicMaterial")

    assert recorded[0][0] is component
    assert recorded[0][1] == "physic_material"
    assert isinstance(recorded[0][3], PhysicMaterialRef)
    assert recorded[0][3].path_hint.endswith("Bouncy.physicMaterial")


def test_inline_material_state_and_preview_query_are_reused(monkeypatch):
    import Infernux.engine.ui.inspector_material as module

    class NativeMaterial:
        file_path = "C:/project/Assets/Test.mat"

        @staticmethod
        def get_version():
            return 4

        @staticmethod
        def serialize_document():
            return {"properties": {}, "shaders": {}}

    panel = SimpleNamespace()
    native = NativeMaterial()
    first_state = module._build_inline_state(panel, native)
    second_state = module._build_inline_state(panel, native)

    assert second_state is first_state
    assert second_state.exec_layer is first_state.exec_layer

    queries = []
    monkeypatch.setattr(
        module,
        "_query_material_preview_tex",
        lambda *_args: queries.append(True) or 73,
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
    queries = iter((41, 42, 42))
    ready = iter((False, True))
    monkeypatch.setattr(module, "_query_material_preview_tex", lambda *_args: next(queries))
    monkeypatch.setattr(module, "_is_material_preview_ready", lambda *_args: next(ready))

    args = (SimpleNamespace(), None, {}, state, "new-json", "C:/project/Assets/Test.mat")
    assert module._get_cached_material_preview_tex(*args) == 41
    assert module._get_cached_material_preview_tex(*args) == 42
    assert module._get_cached_material_preview_tex(*args) == 42


def test_shader_reference_recovers_from_stale_path_hint(monkeypatch, tmp_path):
    import os

    from Infernux.engine.ui import inspector_shader_utils as shader_utils

    shader_path = tmp_path / "Recovered.frag"
    shader_path.write_text("@shader_id: Recovered\n", encoding="utf-8")
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

    assert tex == 17
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

    assert all(call[0][0].startswith("texedit|") for call in native.calls)
    assert native.calls[0][0][2] != native.calls[1][0][2]
    assert native.calls[0][1]["max_size"] == 2048
    assert native.calls[0][1]["texture_format"] == "auto"


def test_texture_live_preview_can_be_released(monkeypatch, tmp_path):
    from Infernux.engine.ui import asset_resource_preview

    class _Native:
        def __init__(self):
            self.invalidated = []

        def invalidate_texture_preview_task(self, key):
            self.invalidated.append(key)

    native = _Native()
    monkeypatch.setattr(asset_resource_preview, "_resolve_native_engine", lambda _panel: native)
    path = tmp_path / "Released.png"
    asset_resource_preview.invalidate_live_texture_preview(str(path))

    assert native.invalidated == [f"texedit|{os.path.normpath(path)}"]


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


def test_audio_track_renderer_exposes_picker_callbacks_and_semantic(monkeypatch):
    import Infernux.engine.ui._inspector_extra_renderers as module
    from Infernux.engine.play_mode import PlayModeManager

    captured = {}
    monkeypatch.setattr(module, "field_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "max_label_w", lambda *_args, **_kwargs: 120.0)
    monkeypatch.setattr(module, "render_object_field", lambda *_args, **kwargs: captured.update(kwargs))
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

    assert callable(captured["picker_asset_items"])
    assert callable(captured["on_pick"])
    assert callable(captured["on_clear"])
    assert captured["semantic_id"] == "inspector.object.11.component.186.track_0.clip"


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
        "asset.texture.import.format",
        "asset.texture.import.compression",
        "asset.texture.import.compression_quality",
        "asset.texture.import.max_size",
    }
    assert by_id["asset.texture.import.srgb"][4] is True
    assert ":" in by_id["asset.texture.import.texture_type"][1]


def test_sprite_slice_controls_are_distinct_and_report_values(monkeypatch):
    settings = TextureImportSettings(texture_type=TextureType.SPRITE)
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
    state.settings = settings
    details._render_sprite_body(ctx, None, state)

    by_id = {entry[3]: entry for entry in ctx.semantics}
    assert by_id["asset.texture.sprite.rows"][1].endswith(": 1")
    assert by_id["asset.texture.sprite.columns"][1].endswith(": 3")
    assert "asset.texture.sprite.auto_slice" in by_id
    assert [(f.x, f.y, f.w, f.h) for f in settings.sprite_frames] == [
        (0, 0, 64, 64),
        (64, 0, 64, 64),
        (128, 0, 64, 64),
    ]


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
