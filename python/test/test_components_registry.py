"""Tests for Infernux.components.registry — get_type, get_all_types, T accessor."""

from Infernux.components.component import InxComponent
from Infernux.components.registry import get_type, get_all_types, T


# ── Test components ──

class _TestCompA(InxComponent):
    pass

class _TestCompB(_TestCompA):
    pass


# ══════════════════════════════════════════════════════════════════════
# get_type
# ══════════════════════════════════════════════════════════════════════

class TestGetType:
    def test_finds_direct_subclass(self):
        cls = get_type("_TestCompA")
        assert cls is _TestCompA

    def test_finds_nested_subclass(self):
        cls = get_type("_TestCompB")
        assert cls is _TestCompB

    def test_returns_none_for_unknown(self):
        assert get_type("NonExistentType12345") is None


# ══════════════════════════════════════════════════════════════════════
# get_all_types
# ══════════════════════════════════════════════════════════════════════

class TestGetAllTypes:
    def test_includes_test_types(self):
        all_types = get_all_types()
        assert "_TestCompA" in all_types
        assert "_TestCompB" in all_types

    def test_values_are_classes(self):
        all_types = get_all_types()
        for cls in all_types.values():
            assert isinstance(cls, type)


# ══════════════════════════════════════════════════════════════════════
# T accessor
# ══════════════════════════════════════════════════════════════════════

class TestTypeAccessor:
    def test_attribute_access(self):
        assert T._TestCompA is _TestCompA

    def test_returns_none_for_unknown(self):
        assert T.NonExistentComp99999 is None

    def test_repr(self):
        r = repr(T)
        assert "<ComponentTypes:" in r


class TestInxComponentSurface:
    def test_component_manipulation_apis_live_on_game_object(self):
        comp = _TestCompA()
        for name in (
            "get_component",
            "get_components",
            "add_component",
            "get_component_in_children",
            "get_component_in_parent",
            "try_get_component",
            "get_mesh_renderer",
        ):
            assert not hasattr(comp, name)


class TestComponentsRootExports:
    def test_common_component_types_are_exported(self):
        from Infernux import components

        assert components.Transform is not None
        assert components.Rigidbody is not None
        assert components.BoxCollider is not None
        assert components.CylinderCollider is not None
        assert components.Camera is not None
