"""Annotation-driven Unity-style serialized field declarations.

Covers:
- bare typed defaults (``health: int = 100``)
- Annotated[] markers (Range/Tooltip/Header/HideInInspector/NonSerialized/...)
- Optional[X] and ``X | None`` unwrapping
- enum annotations
- Color fields
- asset/component reference annotations
- annotation + serialized_field() composition
- CDS backing for annotated numeric fields
"""
from __future__ import annotations

import enum
from typing import Annotated, Optional, List

import pytest

from Infernux.components import (
    InxComponent, serialized_field, FieldType, get_serialized_fields,
    Range, Tooltip, Header, Space, Group, InfoText, DragSpeed,
    Multiline, ReadOnly, HideInInspector, NonSerialized, HDR, Color,
    VALUE_CODECS,
)
from Infernux.components.fields import (
    build_field_from_annotation, coerce_serialized_field_input,
    _unwrap_annotation, _UNSET,
)
from Infernux.components.ref_wrappers import MaterialRef, GameObjectRef, ComponentRef, PrefabRef
from Infernux.core.asset_ref import AudioClipRef, ParticleGraphRef, TextureRef


# ── annotation unwrapping ────────────────────────────────────────────────

class TestUnwrapAnnotation:
    def test_plain_type_passthrough(self):
        base, markers = _unwrap_annotation(int)
        assert base is int and markers == []

    def test_annotated_collects_markers(self):
        base, markers = _unwrap_annotation(Annotated[float, Range(0, 1), Tooltip("t")])
        assert base is float
        assert any(isinstance(m, Range) for m in markers)
        assert any(isinstance(m, Tooltip) for m in markers)

    def test_optional_unwraps(self):
        base, _ = _unwrap_annotation(Optional[int])
        assert base is int

    def test_pep604_union_none_unwraps(self):
        base, _ = _unwrap_annotation(int | None)
        assert base is int

    def test_annotated_inside_optional(self):
        base, markers = _unwrap_annotation(Optional[Annotated[int, Range(1, 5)]])
        assert base is int
        assert any(isinstance(m, Range) for m in markers)


# ── build_field_from_annotation ──────────────────────────────────────────

class TestBuildField:
    def test_int_with_default(self):
        meta = build_field_from_annotation(int, default=42)
        assert meta.field_type == FieldType.INT and meta.default == 42

    def test_float_annotation_coerces_int_default(self):
        meta = build_field_from_annotation(float, default=3)
        assert meta.field_type == FieldType.FLOAT
        assert isinstance(meta.default, float) and meta.default == 3.0

    def test_bool_before_int(self):
        meta = build_field_from_annotation(bool, default=True)
        assert meta.field_type == FieldType.BOOL

    def test_range_marker(self):
        meta = build_field_from_annotation(Annotated[float, Range(0, 100, slider=False)], default=5.0)
        assert meta.range == (0, 100) and meta.slider is False

    def test_all_cosmetic_markers(self):
        ann = Annotated[str, Tooltip("tip"), Header("H"), Space(12.0),
                        Group("G"), InfoText("info"), Multiline, ReadOnly]
        meta = build_field_from_annotation(ann, default="x")
        assert meta.tooltip == "tip" and meta.header == "H"
        assert meta.space == 12.0 and meta.group == "G"
        assert meta.info_text == "info" and meta.multiline and meta.readonly

    def test_drag_speed_marker(self):
        meta = build_field_from_annotation(Annotated[int, DragSpeed(0.25)], default=1)
        assert meta.drag_speed == 0.25

    def test_hide_in_inspector_serialized_but_hidden(self):
        meta = build_field_from_annotation(Annotated[int, HideInInspector], default=7)
        assert meta is not None and meta.hidden is True

    def test_non_serialized_returns_sentinel(self):
        from Infernux.components.fields import NON_SERIALIZED_FIELD
        meta = build_field_from_annotation(Annotated[int, NonSerialized], default=7)
        assert meta is NON_SERIALIZED_FIELD

    def test_enum_annotation(self):
        class Mode(enum.Enum):
            A = 1
            B = 2
        meta = build_field_from_annotation(Mode, default=_UNSET)
        assert meta.field_type == FieldType.ENUM
        assert meta.enum_type is Mode and meta.default == Mode.A

    def test_enum_editor_input_accepts_member_name_and_integer(self):
        class Mode(enum.Enum):
            A = 1
            B = 2

        meta = build_field_from_annotation(Mode, default=Mode.A)

        assert coerce_serialized_field_input("B", meta, "Probe.mode") is Mode.B
        assert coerce_serialized_field_input("b", meta, "Probe.mode") is Mode.B
        assert coerce_serialized_field_input(1, meta, "Probe.mode") is Mode.A

    def test_color_annotation(self):
        meta = build_field_from_annotation(Color, default=_UNSET)
        assert meta.field_type == FieldType.COLOR
        assert tuple(meta.default) == (1.0, 1.0, 1.0, 1.0)
        assert meta.hdr is False

    def test_color_string_annotation(self):
        meta = build_field_from_annotation("Color", default=_UNSET)
        assert meta.field_type == FieldType.COLOR
        assert meta.default == [1.0, 1.0, 1.0, 1.0]

    def test_color_default_coercion(self):
        meta = build_field_from_annotation(Color, default=(0.5, 0.25, 0.0))
        assert meta.field_type == FieldType.COLOR
        assert meta.default == [0.5, 0.25, 0.0, 1.0]

    def test_hdr_marker(self):
        meta = build_field_from_annotation(Annotated[Color, HDR], default=_UNSET)
        assert meta.hdr is True

    def test_unsupported_annotation_falls_back_to_value(self):
        class Weird:
            pass
        meta = build_field_from_annotation(Weird, default=2.5)
        assert meta is not None and meta.field_type == FieldType.FLOAT

    def test_audio_asset_annotation_carries_explicit_contract(self):
        meta = build_field_from_annotation(AudioClipRef, default=_UNSET)

        assert meta.field_type == FieldType.ASSET
        assert meta.asset_type == "AudioClip"

    def test_registered_asset_ref_annotation_infers_contract(self):
        meta = build_field_from_annotation(ParticleGraphRef, default=_UNSET)

        assert meta.field_type == FieldType.ASSET
        assert meta.asset_type == "ParticleGraph"

    def test_prefab_ref_annotation_is_a_game_object_asset_field(self):
        meta = build_field_from_annotation(PrefabRef, default=_UNSET)

        assert meta.field_type == FieldType.GAME_OBJECT
        assert isinstance(meta.default, PrefabRef)

    def test_prefab_path_coerces_to_prefab_ref(self):
        meta = build_field_from_annotation(PrefabRef, default=_UNSET)

        value = coerce_serialized_field_input(
            "Assets/Prefabs/Coin.prefab",
            meta,
            "Spawner.coin_prefab",
        )

        assert isinstance(value, PrefabRef)
        assert value.path_hint == "Assets/Prefabs/Coin.prefab"

    def test_asset_list_annotation_propagates_element_contract(self):
        meta = build_field_from_annotation(list[AudioClipRef], default=_UNSET)

        assert meta.field_type == FieldType.LIST
        assert meta.element_type == FieldType.ASSET
        assert meta.asset_type == "AudioClip"

    def test_untyped_asset_field_is_rejected_at_schema_construction(self):
        with pytest.raises(ValueError, match="explicit asset_type"):
            serialized_field(default=None, field_type=FieldType.ASSET)

        with pytest.raises(ValueError, match="explicit asset_type"):
            serialized_field(
                default=[],
                field_type=FieldType.LIST,
                element_type=FieldType.ASSET,
            )


# ── full component declarations ──────────────────────────────────────────

class _Phase(enum.Enum):
    IDLE = 0
    RUN = 1


class V2Showcase(InxComponent):
    # Unity-style: annotation + plain default
    health: int = 100
    speed: Annotated[float, Range(0, 20), Tooltip("m/s")] = 5.0
    name_tag: Annotated[str, Multiline] = "hello"
    armor: float = 3            # int literal, float annotation → coerced
    phase: _Phase = _Phase.RUN
    tint: Color = Color(1, 0, 0)
    secret: Annotated[int, HideInInspector] = 9
    scratch: Annotated[int, NonSerialized] = 0
    opt_count: Optional[int] = 4
    # references
    mat: 'Material' = None  # noqa: F821  (string annotation; resolves via registry)
    target: Optional[GameObjectRef] = None
    # legacy API still composes with markers
    legacy: Annotated[float, Group("Legacy")] = serialized_field(default=1.5)


class TestV2Showcase:
    def setup_method(self):
        self.fields = get_serialized_fields(V2Showcase)

    def test_annotation_drives_type_over_value(self):
        assert self.fields['armor'].field_type == FieldType.FLOAT
        assert isinstance(self.fields['armor'].default, float)

    def test_basic_typed_default(self):
        assert self.fields['health'].field_type == FieldType.INT
        assert self.fields['health'].default == 100

    def test_markers_applied(self):
        assert self.fields['speed'].range == (0, 20)
        assert self.fields['speed'].tooltip == "m/s"
        assert self.fields['name_tag'].multiline is True

    def test_enum_field(self):
        meta = self.fields['phase']
        assert meta.field_type == FieldType.ENUM
        assert meta.enum_type is _Phase and meta.default == _Phase.RUN

    def test_color_field(self):
        meta = self.fields['tint']
        assert meta.field_type == FieldType.COLOR
        assert tuple(meta.default)[:3] == (1.0, 0.0, 0.0)
        assert meta.hdr is False

    def test_hidden_field_serialized(self):
        assert 'secret' in self.fields
        assert self.fields['secret'].hidden is True

    def test_non_serialized_excluded(self):
        assert 'scratch' not in self.fields

    def test_optional_unwrap(self):
        assert self.fields['opt_count'].field_type == FieldType.INT
        assert self.fields['opt_count'].default == 4

    def test_string_annotation_material(self):
        assert self.fields['mat'].field_type == FieldType.MATERIAL

    def test_descriptor_marker_composition(self):
        meta = self.fields['legacy']
        assert meta.group == "Legacy" and meta.default == 1.5

    def test_instance_roundtrip(self):
        comp = V2Showcase()
        assert comp.health == 100
        assert comp.speed == pytest.approx(5.0)
        comp.health = 55
        comp.speed = 9.5
        assert comp.health == 55
        assert comp.speed == pytest.approx(9.5)
        assert comp.phase == _Phase.RUN

    def test_cds_backing_for_annotated_numeric(self):
        # Annotated numeric fields should ride the same CDS fast path as
        # serialized_field() ones — descriptor carries CDS ids after register.
        desc = V2Showcase.__dict__['health']
        comp = V2Showcase()
        if comp._cds_slot is not None:
            assert desc._cds_class_id is not None

    def test_serialization_includes_hidden_excludes_nonserialized(self):
        import json
        comp = V2Showcase()
        comp.secret = 1234
        data = json.loads(comp._serialize_fields())
        assert data['secret'] == 1234          # HideInInspector → still serialized
        assert 'scratch' not in data           # NonSerialized → excluded
        assert data['health'] == 100


class TestAnnotationOnlyDeclarations:
    def test_bare_annotation_creates_field(self):
        class Bare(InxComponent):
            counter: int
            ratio: Annotated[float, Range(0, 1)]

        fields = get_serialized_fields(Bare)
        assert fields['counter'].field_type == FieldType.INT
        assert fields['counter'].default == 0
        assert fields['ratio'].range == (0, 1)

    def test_private_annotation_not_serialized(self):
        class Priv(InxComponent):
            _hidden_counter: int = 3
            shown: int = 1

        fields = get_serialized_fields(Priv)
        assert '_hidden_counter' not in fields and 'shown' in fields

    def test_explicit_private_hidden_field_is_serialized(self):
        class PrivateBacking(InxComponent):
            _state: str = serialized_field(default="{}", hidden=True)

        component = PrivateBacking()
        component._state = '{"value":2}'
        fields = get_serialized_fields(PrivateBacking)

        assert fields["_state"].hidden is True
        assert component._serialize_fields_document()["_state"] == '{"value":2}'

    def test_list_annotation(self):
        class WithList(InxComponent):
            tags: List[str] = serialized_field(default=[], field_type=FieldType.LIST,
                                               element_type=FieldType.STRING)
        fields = get_serialized_fields(WithList)
        assert fields['tags'].field_type == FieldType.LIST


class TestStrictSerializationFailures:
    def test_unsupported_field_value_raises_with_field_path(self):
        class UnsupportedField(InxComponent):
            payload = serialized_field(default=None, field_type=FieldType.UNKNOWN)

        component = UnsupportedField()
        component.payload = object()

        with pytest.raises(TypeError, match=r"UnsupportedField\.payload"):
            component._serialize_fields()

    def test_non_finite_vector_falls_back_to_field_default(self):
        from Infernux.math import Vector3

        class CameraRig(InxComponent):
            speed: float = serialized_field(default=2.4)
            sway: Vector3 = serialized_field(default=Vector3(0.1, 0.2, 0.3))

        component = CameraRig()
        component.speed = 5.0
        # C++ Vector3 rejects NaN at construction. Bypass CDS so encode sees
        # the leftover non-finite payload the way a stale Python store would.
        component._cds_slot = None
        type(component).__dict__["speed"]._values[id(component)] = 5.0
        type(component).__dict__["sway"]._values[id(component)] = [
            float("nan"),
            0.0,
            0.0,
        ]

        document = component._serialize_fields_document()
        assert document["speed"] == 5.0
        assert document["sway"] == pytest.approx([0.1, 0.2, 0.3])

    def test_repair_keeps_editor_defaults_for_stale_scene_data(self):
        class ShowcaseDirector(InxComponent):
            style_interval: float = serialized_field(default=3.5)
            camera_name: str = serialized_field(default="Main Camera")

        component = ShowcaseDirector()
        component._deserialize_fields_document(
            {
                "__type_name__": "ShowcaseDirector",
                "style_interval": "broken",
                "camera_name": "Hero Camera",
                "camera_position_sway": [0.1, 0.2, 0.3],
            },
            repair=True,
        )

        assert component.style_interval == 3.5
        assert component.camera_name == "Hero Camera"
        healed = component._serialize_fields_document()
        assert "camera_position_sway" not in healed
        assert healed["style_interval"] == 3.5
        assert healed["camera_name"] == "Hero Camera"

    @pytest.mark.parametrize(
        "mutate, error",
        [
            (lambda data: data.__setitem__("removed_field", 0), "fields mismatch"),
            (lambda data: data.__setitem__("__type_name__", "Other"), "type mismatch"),
            (lambda data: data.__setitem__("health", "bad"), "requires an integer"),
        ],
    )
    def test_invalid_field_document_is_rejected_without_partial_assignment(self, mutate, error):
        import json

        class StrictFields(InxComponent):
            health: int = 10
            title: str = "ready"

        source = StrictFields()
        document = json.loads(source._serialize_fields())
        mutate(document)

        target = StrictFields()
        target.health = 77
        target.title = "unchanged"
        with pytest.raises((TypeError, ValueError), match=error):
            target._deserialize_fields(json.dumps(document))
        assert target.health == 77
        assert target.title == "unchanged"

    def test_missing_new_component_field_uses_declared_default(self):
        import json

        class AdditiveFields(InxComponent):
            health: int = 10
            tags: list = serialized_field(
                default=[],
                field_type=FieldType.LIST,
                element_type=FieldType.STRING,
            )

        source = AdditiveFields()
        source.health = 42
        document = json.loads(source._serialize_fields())
        document.pop("tags")

        restored = AdditiveFields()
        restored._deserialize_fields(json.dumps(document))

        assert restored.health == 42
        assert restored.tags == []

    def test_removed_and_renamed_fields_are_rejected(self):
        import json

        class EvolvingFields(InxComponent):
            health: int = serialized_field(default=10)

        document = json.loads(EvolvingFields()._serialize_fields())
        document.pop("health")
        document["hp"] = 42
        document["removed_debug_value"] = True

        with pytest.raises(ValueError, match="serialized fields mismatch"):
            EvolvingFields()._deserialize_fields(json.dumps(document))

    def test_unknown_metadata_is_rejected(self):
        import json

        class CurrentFields(InxComponent):
            value: int = 3

        document = json.loads(CurrentFields()._serialize_fields())
        document["__unknown__"] = 1
        with pytest.raises(ValueError, match="serialized fields mismatch"):
            CurrentFields()._deserialize_fields(json.dumps(document))

    def test_all_fields_validate_before_any_decode(self, monkeypatch):
        class ValidateFirst(InxComponent):
            count: int = 1
            label: str = "ready"

        document = ValidateFirst()._serialize_fields_document()
        document["label"] = 42
        decode_calls = []
        original_decode = VALUE_CODECS.decode

        def track_decode(value, field_meta_or_type, path="value"):
            decode_calls.append(path)
            return original_decode(value, field_meta_or_type, path)

        monkeypatch.setattr(VALUE_CODECS, "decode", track_decode)

        with pytest.raises(TypeError, match="STRING field requires a string"):
            ValidateFirst()._deserialize_fields_document(document)

        assert decode_calls == []


class TestColorInference:
    def test_color_factory_infers_as_color(self):
        from Infernux.components.fields import infer_field_type_from_value
        assert infer_field_type_from_value(Color(1, 0, 0)) == FieldType.COLOR

    def test_color_value_without_annotation(self):
        class TintHolder(InxComponent):
            tint = Color(0.2, 0.4, 0.6)
        fields = get_serialized_fields(TintHolder)
        assert fields['tint'].field_type == FieldType.COLOR
        assert fields['tint'].default == [0.2, 0.4, 0.6, 1.0]

    def test_future_annotations_color(self):
        import textwrap
        import tempfile
        import os
        script = textwrap.dedent('''
            from __future__ import annotations
            from Infernux.components import InxComponent, Color
            class FutureColor(InxComponent):
                tint: Color
        ''')
        td = tempfile.mkdtemp()
        path = os.path.join(td, 'future_color.py')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(script)
        from Infernux.components.script_loader import load_component_from_file
        cls = load_component_from_file(path)
        fields = get_serialized_fields(cls)
        assert fields['tint'].field_type == FieldType.COLOR


class TestColorClass:
    def test_factory_returns_material_list(self):
        c = Color(0.1, 0.2, 0.3)
        assert isinstance(c, list)
        assert c == [0.1, 0.2, 0.3, 1.0]

    def test_construct_from_sequence(self):
        c = Color((0.5, 0.6, 0.7, 0.8))
        assert c == [0.5, 0.6, 0.7, 0.8]

    def test_undo_snapshot_rgba(self):
        from Infernux.engine.undo._base import _snapshot_value
        src = Color(0.2, 0.4, 0.6)
        snap = _snapshot_value(src)
        assert isinstance(snap, list)
        assert snap == [0.2, 0.4, 0.6, 1.0]
        assert snap is not src


class TestEffectColorFieldsAllowHdr:
    def test_mixin_color_annotation_keeps_hdr(self):
        from Infernux.renderstack._serialized_field_mixin import (
            SerializedFieldCollectorMixin,
        )

        class BloomTint(SerializedFieldCollectorMixin):
            tint: Annotated[Color, HDR] = Color(1.0, 1.0, 1.0, 1.0)

        meta = get_serialized_fields(BloomTint)["tint"]
        assert meta.field_type == FieldType.COLOR
        assert meta.hdr is True
