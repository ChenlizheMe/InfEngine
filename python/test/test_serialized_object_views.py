from __future__ import annotations

from Infernux.components import InxComponent, SerializableObject, serialized_field, FieldType
from Infernux.components.fields import get_raw_field_value
from Infernux.components.ref_wrappers import MaterialRef
from Infernux.core import Material, Texture
from Infernux.core.asset_ref import TextureRef
from Infernux.lib import InxMaterial


class RefViewData(SerializableObject):
    mat: Material = None
    tex: Texture = None


class RefViewComponent(InxComponent):
    mat: Material = None
    tex: Texture = None
    materials = serialized_field(default=[], field_type=FieldType.LIST, element_type=FieldType.MATERIAL)
    textures = serialized_field(default=[], field_type=FieldType.LIST, element_type=FieldType.TEXTURE)
    data: RefViewData = serialized_field(default=RefViewData())


def _make_material() -> Material:
    return Material.from_native(InxMaterial.create_default_lit())


def _make_texture() -> Texture:
    tex = Texture.solid_color(4, 4, 255, 0, 0, 255)
    assert tex is not None
    return tex


def test_component_material_and_texture_fields_store_refs_but_return_objects():
    comp = RefViewComponent()
    mat = _make_material()
    tex = _make_texture()

    comp.mat = mat
    comp.tex = tex

    raw_mat = get_raw_field_value(comp, "mat")
    raw_tex = get_raw_field_value(comp, "tex")

    assert isinstance(raw_mat, MaterialRef)
    assert isinstance(raw_tex, TextureRef)
    assert comp.mat is mat
    assert comp.tex is tex


def test_component_list_reference_fields_return_object_views():
    comp = RefViewComponent()
    mat = _make_material()
    tex = _make_texture()

    comp.materials = [mat]
    comp.textures = [tex]

    raw_mats = get_raw_field_value(comp, "materials")
    raw_texs = get_raw_field_value(comp, "textures")

    assert len(raw_mats) == 1 and isinstance(raw_mats[0], MaterialRef)
    assert len(raw_texs) == 1 and isinstance(raw_texs[0], TextureRef)
    assert comp.materials == [mat]
    assert comp.textures == [tex]


def test_serializable_object_reference_fields_store_refs_but_return_objects():
    data = RefViewData()
    mat = _make_material()
    tex = _make_texture()

    data.mat = mat
    data.tex = tex

    assert isinstance(get_raw_field_value(data, "mat"), MaterialRef)
    assert isinstance(get_raw_field_value(data, "tex"), TextureRef)
    assert data.mat is mat
    assert data.tex is tex


def test_component_can_expose_nested_serializable_object_with_object_view():
    comp = RefViewComponent()
    mat = _make_material()
    tex = _make_texture()

    comp.data.mat = mat
    comp.data.tex = tex

    assert isinstance(get_raw_field_value(comp.data, "mat"), MaterialRef)
    assert isinstance(get_raw_field_value(comp.data, "tex"), TextureRef)
    assert comp.data.mat is mat
    assert comp.data.tex is tex
