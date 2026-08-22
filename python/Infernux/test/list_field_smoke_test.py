"""Inspector smoke-test component for all currently supported list field types.

This component intentionally has no runtime behavior. It exists only to let
the editor exercise serialized list rendering, drag/drop, add/remove, and
scene serialization paths.
"""

from Infernux.components import (
    InxComponent,
    FieldType,
    add_component_menu,
    serialized_field,
)
from Infernux.math import Vector2, Vector3, vec4f


@add_component_menu("Tests/List Field Smoke Test")
class ListFieldSmokeTest(InxComponent):
    """Pure data component used to verify list field Inspector support."""

    int_values = serialized_field(
        default=[0, 1, 2],
        field_type=FieldType.LIST,
        element_type=FieldType.INT,
        header="Scalar Lists",
        tooltip="Integer list test",
    )

    float_values = serialized_field(
        default=[0.0, 0.5, 1.0],
        field_type=FieldType.LIST,
        element_type=FieldType.FLOAT,
        tooltip="Float list test",
    )

    bool_values = serialized_field(
        default=[True, False, True],
        field_type=FieldType.LIST,
        element_type=FieldType.BOOL,
        tooltip="Bool list test",
    )

    string_values = serialized_field(
        default=["Alpha", "Beta", "Gamma"],
        field_type=FieldType.LIST,
        element_type=FieldType.STRING,
        tooltip="String list test",
    )

    vec2_values = serialized_field(
        default=[Vector2(0.0, 0.0), Vector2(1.0, 2.0)],
        field_type=FieldType.LIST,
        element_type=FieldType.VEC2,
        header="Vector Lists",
        tooltip="Vector2 list test",
    )

    vec3_values = serialized_field(
        default=[Vector3(0.0, 0.0, 0.0), Vector3(1.0, 2.0, 3.0)],
        field_type=FieldType.LIST,
        element_type=FieldType.VEC3,
        tooltip="Vector3 list test",
    )

    vec4_values = serialized_field(
        default=[vec4f(0.0, 0.0, 0.0, 1.0), vec4f(1.0, 2.0, 3.0, 4.0)],
        field_type=FieldType.LIST,
        element_type=FieldType.VEC4,
        tooltip="Vector4 list test",
    )

    color_values = serialized_field(
        default=[
            [1.0, 0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0, 1.0],
            [0.0, 0.0, 1.0, 1.0],
        ],
        field_type=FieldType.LIST,
        element_type=FieldType.COLOR,
        header="Reference Lists",
        tooltip="Color list test",
    )

    game_objects = serialized_field(
        default=[],
        field_type=FieldType.LIST,
        element_type=FieldType.GAME_OBJECT,
        tooltip="Drag GameObjects from the hierarchy here",
    )

    materials = serialized_field(
        default=[],
        field_type=FieldType.LIST,
        element_type=FieldType.MATERIAL,
        tooltip="Drag materials from the project here",
    )

    textures = serialized_field(
        default=[],
        field_type=FieldType.LIST,
        element_type=FieldType.TEXTURE,
        tooltip="Drag textures from the project here",
    )

    shaders = serialized_field(
        default=[],
        field_type=FieldType.LIST,
        element_type=FieldType.SHADER,
        tooltip="Drag shaders from the project here",
    )

    audio_clips = serialized_field(
        default=[],
        field_type=FieldType.LIST,
        element_type=FieldType.ASSET,
        asset_type="AudioClip",
        tooltip="Drag WAV audio assets from the project here",
    )


__all__ = ["ListFieldSmokeTest"]
