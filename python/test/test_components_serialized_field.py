"""Tests for Infernux.components.fields — field types, metadata, descriptors.

Merges tests from test_component_annotation_defaults.py.
"""

import weakref

from Infernux.components import InxComponent
from Infernux.components.fields import (
    FieldType,
    FieldMetadata,
    SerializedFieldDescriptor,
    clear_serialized_fields_cache,
    get_serialized_fields,
    serialized_field,
    int_field,
    list_field,
    HiddenField,
    hide_field,
)


def test_project_relative_asset_path_is_resolved_before_guid_lookup(monkeypatch, tmp_path):
    import importlib

    from Infernux.engine import project_context

    serialized_field_module = importlib.import_module("Infernux.components.fields")

    project = tmp_path / "Project"
    asset = project / "Assets" / "VFX" / "Ribbon.particlegraph"
    asset.parent.mkdir(parents=True)
    asset.write_text("{}", encoding="ascii")

    class AbsoluteOnlyDatabase:
        @staticmethod
        def get_guid_from_path(path):
            return "ribbon-guid" if str(path) == str(asset) else ""

    monkeypatch.setattr(serialized_field_module, "_get_asset_db", lambda: AbsoluteOnlyDatabase())
    monkeypatch.setattr(project_context, "_project_root", str(project))

    guid, path_hint = serialized_field_module._extract_guid_and_path(
        "Assets/VFX/Ribbon.particlegraph", ()
    )

    assert guid == "ribbon-guid"
    assert path_hint == "Assets/VFX/Ribbon.particlegraph"


# ══════════════════════════════════════════════════════════════════════
# FieldType enum completeness
# ══════════════════════════════════════════════════════════════════════

class TestFieldType:
    def test_core_members_exist(self):
        for name in ("INT", "FLOAT", "BOOL", "STRING",
                      "VEC2", "VEC3", "VEC4", "COLOR",
                      "GAME_OBJECT", "COMPONENT", "MATERIAL",
                      "TEXTURE", "SHADER", "ASSET",
                      "ENUM", "LIST", "SERIALIZABLE_OBJECT"):
            assert hasattr(FieldType, name), f"FieldType.{name} missing"

    def test_enum_values_are_int(self):
        assert isinstance(FieldType.INT.value, int)


# ══════════════════════════════════════════════════════════════════════
# FieldMetadata
# ══════════════════════════════════════════════════════════════════════

class TestFieldMetadata:
    def test_defaults(self):
        meta = FieldMetadata(name="x", field_type=FieldType.FLOAT, default=None)
        assert meta.name == "x"
        assert meta.field_type == FieldType.FLOAT
        assert meta.default is None
        assert meta.readonly is False
        assert meta.tooltip == ""

    def test_custom_values(self):
        meta = FieldMetadata(name="speed", field_type=FieldType.FLOAT,
                             default=5.0, readonly=True, tooltip="Move speed",
                             range=(0, 100))
        assert meta.default == 5.0
        assert meta.readonly is True
        assert meta.tooltip == "Move speed"
        assert meta.range == (0, 100)


# ══════════════════════════════════════════════════════════════════════
# serialized_field() factory
# ══════════════════════════════════════════════════════════════════════

class TestSerializedFieldFactory:
    def test_returns_descriptor(self):
        sf = serialized_field(default=0)
        assert isinstance(sf, SerializedFieldDescriptor)

    def test_int_field_shortcut(self):
        sf = int_field(default=7)
        assert isinstance(sf, SerializedFieldDescriptor)
        assert sf.metadata.default == 7

    def test_list_field_shortcut(self):
        sf = list_field(element_type=FieldType.INT)
        assert isinstance(sf, SerializedFieldDescriptor)
        assert sf.metadata.field_type == FieldType.LIST

    def test_stale_weakref_callback_cannot_remove_reused_instance_slot(self):
        descriptor = SerializedFieldDescriptor(
            FieldMetadata(name="value", field_type=FieldType.STRING, default="")
        )

        class Owner:
            pass

        stale_owner = Owner()
        current_owner = Owner()
        stale_ref = weakref.ref(stale_owner)
        current_ref = weakref.ref(current_owner)
        slot = 17
        descriptor._weak_refs[slot] = current_ref
        descriptor._values[slot] = "current"

        with descriptor._lock:
            descriptor._make_ref_callback(slot)(stale_ref)

        assert descriptor._weak_refs[slot] is current_ref
        assert descriptor._values[slot] == "current"


# ══════════════════════════════════════════════════════════════════════
# HiddenField
# ══════════════════════════════════════════════════════════════════════

class TestHiddenField:
    def test_hide_field_creates_hidden_field(self):
        hf = hide_field(42)
        assert isinstance(hf, HiddenField)
        assert hf.default == 42

    def test_hidden_field_not_serialized(self):
        class Comp(InxComponent):
            _internal = hide_field(10)
            visible: int = serialized_field(default=0)

        fields = get_serialized_fields(Comp)
        assert "visible" in fields
        assert "_internal" not in fields


# ══════════════════════════════════════════════════════════════════════
# Component field collection via __init_subclass__
# ══════════════════════════════════════════════════════════════════════

class TestComponentFieldCollection:
    def test_explicit_serialized_field(self):
        class Comp(InxComponent):
            speed: float = serialized_field(default=10.0)

        fields = get_serialized_fields(Comp)
        assert "speed" in fields
        assert fields["speed"].default == 10.0

    def test_plain_default_value(self):
        class Comp(InxComponent):
            count = 5

        fields = get_serialized_fields(Comp)
        assert "count" in fields
        assert fields["count"].default == 5

    def test_annotation_only_gets_zero_defaults(self):
        """Ported from test_component_annotation_defaults.py."""

        class Comp(InxComponent):
            c: int
            speed: float
            enabled_flag: bool
            label: str

        comp = Comp()
        assert comp.c == 0
        assert comp.speed == 0.0
        assert comp.enabled_flag is False
        assert comp.label == ""

        comp.c += 1
        comp.speed += 2.5
        comp.enabled_flag = True
        comp.label = "ok"

        assert comp.c == 1
        assert comp.speed == 2.5
        assert comp.enabled_flag is True
        assert comp.label == "ok"

    def test_private_fields_hidden_but_initialized(self):
        """Ported from test_component_annotation_defaults.py."""

        class Comp(InxComponent):
            _c: int
            _items: list[int]

        comp = Comp()
        assert comp._c == 0
        assert comp._items == []

        comp._c += 1
        comp._items.append("x")

        assert comp._c == 1
        assert comp._items == ["x"]
        assert "_c" not in get_serialized_fields(Comp)
        assert "_items" not in get_serialized_fields(Comp)

    def test_subclass_inherits_parent_fields(self):
        class Base(InxComponent):
            base_val: int = serialized_field(default=1)

        class Derived(Base):
            derived_val: float = serialized_field(default=2.0)

        fields = get_serialized_fields(Derived)
        assert "base_val" in fields
        assert "derived_val" in fields

    def test_field_set_and_get_round_trip(self):
        class Comp(InxComponent):
            x: float = serialized_field(default=1.0)

        comp = Comp()
        assert comp.x == 1.0
        comp.x = 42.0
        assert comp.x == 42.0

    def test_class_field_table_is_authoritative_after_cache_clear(self):
        class Comp(InxComponent):
            speed: float = serialized_field(default=10.0)

        assert "speed" in get_serialized_fields(Comp)

        clear_serialized_fields_cache(Comp)
        Comp._serialized_fields_ = {}

        assert get_serialized_fields(Comp) == {}
