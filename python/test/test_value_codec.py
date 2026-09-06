from dataclasses import dataclass
from enum import Enum
import inspect

import pytest

from Infernux.components.fields import FieldMetadata, FieldType
from Infernux.components.value_codec import ValueCodecDescriptor, ValueCodecRegistry
from Infernux.components.value_document import (
    TYPE_KEY,
    GAME_OBJECT_REF,
    COMPONENT_REF,
    ASSET_REF,
)
from Infernux.core.asset_ref import (
    GenericAssetRef,
    create_asset_ref,
    get_all_asset_type_configs,
    register_asset_type,
)
from Infernux.graph.ramp import AnimationCurve, Gradient, GradientKey, Keyframe


def _meta(field_type: FieldType, name: str = "field") -> FieldMetadata:
    return FieldMetadata(name=name, field_type=field_type, default=None)


def test_nested_encode_error_contains_full_path():
    codec = ValueCodecRegistry()

    with pytest.raises(TypeError, match=r"Root\.items\[1\]\.payload"):
        codec.encode({"items": [1, {"payload": object()}]}, "Root")


@pytest.mark.parametrize("value", [["1", 2], [True, 2]])
def test_vectors_reject_implicit_numeric_conversion(value):
    codec = ValueCodecRegistry()

    with pytest.raises(TypeError, match="Position: vector values must be numbers"):
        codec.decode(value, _meta(FieldType.VEC2), "Position")


@pytest.mark.parametrize("value", [[0, 0, 0, "1"], [0, 0, 0, False]])
def test_colors_reject_implicit_numeric_conversion(value):
    codec = ValueCodecRegistry()

    with pytest.raises(TypeError, match="Tint: COLOR values must be numbers"):
        codec.decode(value, _meta(FieldType.COLOR), "Tint")


@pytest.mark.parametrize(
    ("value", "field_type"),
    [
        (
            AnimationCurve(
                (Keyframe(0.0, 0.25), Keyframe(1.0, 1.5)),
                "repeat",
                "ping_pong",
            ),
            FieldType.ANIMATION_CURVE,
        ),
        (
            Gradient(
                (
                    GradientKey(0.0, (1.0, 0.0, 0.0, 1.0)),
                    GradientKey(1.0, (0.0, 0.0, 1.0, 0.5)),
                ),
                "fixed",
            ),
            FieldType.GRADIENT,
        ),
    ],
)
def test_ramp_value_codecs_round_trip(value, field_type):
    codec = ValueCodecRegistry()

    document = codec.encode(value, "Ramp")
    restored = codec.decode(document, _meta(field_type), "Ramp")

    assert restored == value


def test_animation_curve_codec_rejects_malformed_editor_document():
    codec = ValueCodecRegistry()

    with pytest.raises(ValueError, match="Width: invalid AnimationCurve"):
        codec.decode(
            {"keys": [], "pre_wrap": "clamp", "post_wrap": "clamp"},
            _meta(FieldType.ANIMATION_CURVE),
            "Width",
        )


def test_duplicate_codec_registration_is_rejected():
    codec = ValueCodecRegistry()
    descriptor = ValueCodecDescriptor(
        name="point",
        can_encode=lambda value: False,
        can_decode=lambda field: False,
        encode=lambda value, path, registry: {},
        validate=lambda value, meta, path, registry: None,
        decode=lambda value, meta, path, registry: value,
    )
    codec.register_codec(descriptor)

    with pytest.raises(ValueError, match="already registered"):
        codec.register_codec(descriptor)
    assert not hasattr(codec, "register_encoder")
    assert not hasattr(codec, "register_decoder")


def test_codec_descriptor_requires_identity():
    with pytest.raises((TypeError, ValueError)):
        ValueCodecDescriptor(
            name="",
            can_encode=lambda value: False,
            can_decode=lambda field: False,
            encode=lambda value, path, registry: value,
            validate=lambda value, meta, path, registry: None,
            decode=lambda value, meta, path, registry: value,
        )


def test_custom_codecs_are_explicit_and_path_aware():
    @dataclass
    class Point:
        x: int

    custom_type = object()
    codec = ValueCodecRegistry()

    def validate_point(value, meta, path, registry):
        if not isinstance(value, dict) or type(value.get("point")) is not int:
            raise TypeError(f"{path}: point requires an integer")

    codec.register_codec(
        ValueCodecDescriptor(
            name="point",
            can_encode=lambda value: isinstance(value, Point),
            can_decode=lambda field: field is custom_type,
            encode=lambda value, path, registry: {"point": value.x, "path": path},
            validate=validate_point,
            decode=lambda value, meta, path, registry: Point(value["point"]),
        )
    )

    assert codec.encode(Point(3), "Owner.location") == {
        "point": 3,
        "path": "Owner.location",
    }
    assert codec.decode({"point": 4}, custom_type, "Owner.location") == Point(4)
    assert codec.descriptors[0].identity == "point"


def test_validate_does_not_invoke_custom_decoder():
    decoder_calls = []
    token = object()
    codec = ValueCodecRegistry()
    codec.register_codec(
        ValueCodecDescriptor(
            name="validated-only",
            can_encode=lambda value: False,
            can_decode=lambda field: field is token,
            encode=lambda value, path, registry: value,
            validate=lambda value, meta, path, registry: None,
            decode=lambda value, meta, path, registry: decoder_calls.append(value),
        )
    )

    codec.validate({"payload": 1}, token, "Owner.payload")

    assert decoder_calls == []


def test_custom_encoder_must_return_document_data():
    codec = ValueCodecRegistry()
    codec.register_codec(
        ValueCodecDescriptor(
            name="bad-output",
            can_encode=lambda value: value is Ellipsis,
            can_decode=lambda field: False,
            encode=lambda value, path, registry: object(),
            validate=lambda value, meta, path, registry: None,
            decode=lambda value, meta, path, registry: value,
        )
    )

    with pytest.raises(TypeError, match="codec produced non-document value"):
        codec.encode(Ellipsis, "Owner.payload")


@pytest.mark.parametrize(
    "document,error",
    [
        (
            {TYPE_KEY: GAME_OBJECT_REF, "object_id": "12"},
            "non-negative integer",
        ),
        (
            {TYPE_KEY: GAME_OBJECT_REF, "object_id": 12, "unknown": True},
            "unknown or missing fields",
        ),
        (
            {TYPE_KEY: COMPONENT_REF, "game_object_id": 1},
            "unknown or missing fields",
        ),
        (
            {
                TYPE_KEY: COMPONENT_REF,
                "game_object_id": -1,
                "component_type": "Mover",
            },
            "non-negative integer",
        ),
        (
            {
                TYPE_KEY: ASSET_REF,
                "asset_type": "Material",
                "guid": 42,
                "path_hint": "",
            },
            "values must be strings",
        ),
        (
            {
                TYPE_KEY: ASSET_REF,
                "asset_type": "Texture",
                "guid": "guid",
                "path_hint": 42,
            },
            "values must be strings",
        ),
    ],
)
def test_reference_documents_are_exact(document, error):
    codec = ValueCodecRegistry()

    with pytest.raises((TypeError, ValueError), match=error):
        codec.decode(document, FieldType.UNKNOWN, "Target")


def test_reserved_marker_documents_are_rejected():
    codec = ValueCodecRegistry()

    with pytest.raises(ValueError, match="reserved document fields are not supported"):
        codec.decode({"__reserved__": 12}, FieldType.UNKNOWN, "Target")


def test_enum_uses_exact_typed_document():
    class Mode(Enum):
        ACTIVE = 1

    codec = ValueCodecRegistry()
    meta = _meta(FieldType.ENUM)
    meta.enum_type = Mode

    document = codec.encode(Mode.ACTIVE, "Owner.mode")

    assert document == {
        TYPE_KEY: "enum",
        "enum_type": Mode.__qualname__,
        "name": "ACTIVE",
    }
    assert codec.decode(document, meta, "Owner.mode") is Mode.ACTIVE

    document["unknown"] = 1
    with pytest.raises(ValueError, match="unknown or missing fields"):
        codec.decode(document, meta, "Owner.mode")


def test_asset_registry_no_longer_owns_serialization_marker_keys():
    assert "dict_key" not in inspect.signature(register_asset_type).parameters
    assert all("dict_key" not in config for config in get_all_asset_type_configs().values())


def test_registered_asset_without_specialized_wrapper_roundtrips():
    codec = ValueCodecRegistry()
    metadata = FieldMetadata(
        name="mesh",
        field_type=FieldType.ASSET,
        default=None,
        asset_type="Mesh",
    )
    reference = create_asset_ref(
        "Mesh",
        guid="mesh-guid",
        path_hint="Assets/Models/Probe.fbx",
    )

    document = codec.encode(reference, "Probe.mesh")
    decoded = codec.decode(document, metadata, "Probe.mesh")

    assert document == {
        TYPE_KEY: ASSET_REF,
        "asset_type": "Mesh",
        "guid": "mesh-guid",
        "path_hint": "Assets/Models/Probe.fbx",
    }
    assert isinstance(decoded, GenericAssetRef)
    assert decoded.asset_type == "Mesh"
    assert decoded.guid == "mesh-guid"


def test_asset_list_codec_preserves_parent_element_contract():
    codec = ValueCodecRegistry()
    metadata = FieldMetadata(
        name="clips",
        field_type=FieldType.LIST,
        default=[],
        element_type=FieldType.ASSET,
        asset_type="AudioClip",
    )
    document = [
        {
            TYPE_KEY: ASSET_REF,
            "asset_type": "AudioClip",
            "guid": "audio-guid",
            "path_hint": "Assets/Audio/Probe.wav",
        }
    ]

    decoded = codec.decode(document, metadata, "Probe.clips")

    assert len(decoded) == 1
    assert decoded[0].guid == "audio-guid"
    assert decoded[0].path_hint == "Assets/Audio/Probe.wav"


def test_asset_list_codec_rejects_wrong_element_type():
    codec = ValueCodecRegistry()
    metadata = FieldMetadata(
        name="clips",
        field_type=FieldType.LIST,
        default=[],
        element_type=FieldType.ASSET,
        asset_type="AudioClip",
    )
    document = [
        {
            TYPE_KEY: ASSET_REF,
            "asset_type": "Mesh",
            "guid": "mesh-guid",
            "path_hint": "Assets/Models/Probe.fbx",
        }
    ]

    with pytest.raises(TypeError, match="requires AudioClip"):
        codec.decode(document, metadata, "Probe.clips")
