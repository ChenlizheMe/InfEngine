"""Tests for Infernux.components.serializable_object — SerializableObject base class."""

from Infernux.components.serializable_object import (
    SerializableObject,
    _SERIALIZABLE_REGISTRY,
    get_serializable_class,
    get_serializable_type_id,
)
from Infernux.components.serialized_field import serialized_field, FieldType
from Infernux.components.value_document import make_serializable_object
import pytest


# ── Test data classes ──

class Stats(SerializableObject):
    hp: int = serialized_field(default=100)
    mp: float = serialized_field(default=50.0)
    name: str = serialized_field(default="default")


class Nested(SerializableObject):
    inner: int = serialized_field(default=0)


# ══════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════

class TestRegistration:
    def test_subclass_auto_registered(self):
        assert get_serializable_type_id(Stats) in _SERIALIZABLE_REGISTRY

    def test_get_serializable_class(self):
        cls = get_serializable_class(get_serializable_type_id(Stats))
        assert cls is Stats

    def test_unknown_returns_none(self):
        assert get_serializable_class("NoSuchClass") is None


# ══════════════════════════════════════════════════════════════════════
# Field metadata collection
# ══════════════════════════════════════════════════════════════════════

class TestFieldCollection:
    def test_serialized_fields_collected(self):
        fields = Stats._serialized_fields_
        assert "hp" in fields
        assert "mp" in fields
        assert "name" in fields

    def test_field_defaults(self):
        fields = Stats._serialized_fields_
        assert fields["hp"].default == 100
        assert fields["mp"].default == 50.0
        assert fields["name"].default == "default"


# ══════════════════════════════════════════════════════════════════════
# Construction
# ══════════════════════════════════════════════════════════════════════

class TestConstruction:
    def test_defaults(self):
        s = Stats()
        assert s.hp == 100
        assert s.mp == 50.0
        assert s.name == "default"

    def test_kwargs(self):
        s = Stats(hp=999, name="hero")
        assert s.hp == 999
        assert s.name == "hero"
        assert s.mp == 50.0  # default

    def test_setattr(self):
        s = Stats()
        s.hp = 42
        assert s.hp == 42


# ══════════════════════════════════════════════════════════════════════
# Serialization round-trip
# ══════════════════════════════════════════════════════════════════════

class TestSerialization:
    def test_serialize_produces_dict_with_type_tag(self):
        s = Stats()
        data = s._serialize()
        assert data["$type"] == "serializable_object"
        assert data["$version"] == 1
        assert data["type_id"] == get_serializable_type_id(Stats)
        assert data["fields"]["hp"] == 100

    def test_deserialize_restores_values(self):
        s = Stats(hp=42, mp=7.5, name="test")
        data = s._serialize()
        restored = Stats._deserialize(data)
        assert restored.hp == 42
        assert restored.mp == 7.5
        assert restored.name == "test"

    def test_polymorphic_deserialize(self):
        s = Stats(hp=1)
        data = s._serialize()
        # Deserialize through base class resolves to Stats
        restored = SerializableObject._deserialize(data)
        assert type(restored) is Stats
        assert restored.hp == 1

    def test_validate_document_does_not_construct_instance(self):
        class ValidateOnly(SerializableObject):
            value: int = serialized_field(default=0)

            def __new__(cls, *args, **kwargs):
                raise AssertionError("validation must not construct SerializableObject")

        data = make_serializable_object(
            get_serializable_type_id(ValidateOnly),
            {"value": 7},
        )

        actual_cls, fields = SerializableObject._validate_document(data, "Root.stats")

        assert actual_cls is ValidateOnly
        assert set(fields) == {"value"}

        data["fields"]["value"] = "invalid"
        with pytest.raises(TypeError, match="INT field requires an integer"):
            SerializableObject._deserialize(data)

    def test_unsupported_nested_value_is_rejected(self):
        class Unsupported(SerializableObject):
            payload = serialized_field(default=None, field_type=FieldType.UNKNOWN)

        value = Unsupported(payload=object())
        with pytest.raises(TypeError, match=r"Unsupported\.payload.*unsupported serialized value"):
            value._serialize()

    @pytest.mark.parametrize(
        "mutate, error",
        [
            (lambda data: data.pop("$version"), "invalid"),
            (lambda data: data.__setitem__("type_id", "removed:Type"), "unknown"),
        ],
    )
    def test_document_identity_and_fields_are_strict(self, mutate, error):
        data = Stats()._serialize()
        mutate(data)
        with pytest.raises(ValueError, match=error):
            SerializableObject._deserialize(data)

    def test_removed_field_is_ignored_and_renamed_field_is_restored(self):
        class EvolvingStats(SerializableObject):
            health: int = serialized_field(default=100, formerly_serialized_as="hp")

        data = EvolvingStats()._serialize()
        data["fields"] = {"hp": 42, "removed_debug_value": True}

        restored = EvolvingStats._deserialize(data)

        assert restored.health == 42
        assert restored._serialize()["fields"] == {"health": 42}

    def test_missing_additive_field_uses_an_independent_declared_default(self):
        class AdditiveDefaults(SerializableObject):
            hp: int = serialized_field(default=100)
            tags: list = serialized_field(
                default=[],
                field_type=FieldType.LIST,
                element_type=FieldType.STRING,
            )

        data = AdditiveDefaults(hp=42, tags=["saved"])._serialize()
        data["fields"].pop("tags")

        restored = SerializableObject._deserialize(data)

        assert restored.hp == 42
        assert restored.tags == []
        restored.tags.append("local")
        assert AdditiveDefaults().tags == []


# ══════════════════════════════════════════════════════════════════════
# Equality and repr
# ══════════════════════════════════════════════════════════════════════

class TestDunder:
    def test_eq_same_values(self):
        a = Stats(hp=10, mp=20.0, name="x")
        b = Stats(hp=10, mp=20.0, name="x")
        assert a == b

    def test_neq_different_values(self):
        a = Stats(hp=10)
        b = Stats(hp=20)
        assert a != b

    def test_neq_different_types(self):
        s = Stats()
        n = Nested()
        assert s != n

    def test_repr(self):
        s = Stats()
        r = repr(s)
        assert "Stats" in r
        assert "hp=" in r

    def test_deepcopy(self):
        import copy
        s = Stats(hp=42)
        s2 = copy.deepcopy(s)
        assert s2.hp == 42
        s2.hp = 0
        assert s.hp == 42  # original unchanged
