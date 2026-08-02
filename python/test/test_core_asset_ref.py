"""Tests for Infernux.core.asset_ref — AssetRefBase, TextureRef, ShaderRef, etc."""

from __future__ import annotations

import pytest

from Infernux.core.asset_ref import (
    AssetRefBase,
    AudioClipRef,
    MaterialRef,
    ShaderRef,
    TextureRef,
)


# ═══════════════════════════════════════════════════════════════════════════
# AssetRefBase
# ═══════════════════════════════════════════════════════════════════════════

class TestAssetRefBase:
    def test_empty_ref(self):
        ref = AssetRefBase()
        assert ref.guid == ""
        assert ref.path_hint == ""
        assert bool(ref) is False

    def test_guid_set_invalidates_cache(self):
        ref = AssetRefBase(guid="abc")
        ref._cached = "something"
        ref.guid = "def"
        assert ref._cached is None
        assert ref.guid == "def"

    def test_same_guid_no_invalidate(self):
        ref = AssetRefBase(guid="abc")
        ref._cached = "something"
        ref.guid = "abc"
        assert ref._cached == "something"

    def test_resolve_empty_guid_returns_none(self):
        ref = AssetRefBase()
        assert ref.resolve() is None

    def test_resolve_caches(self):
        ref = AssetRefBase(guid="abc")
        ref._cached = "cached_value"
        assert ref.resolve() == "cached_value"

    def test_invalidate_clears_cache(self):
        ref = AssetRefBase(guid="abc")
        ref._cached = "cached"
        ref.invalidate()
        assert ref._cached is None

    def test_to_dict(self):
        ref = AssetRefBase(guid="abc", path_hint="textures/foo.png")
        d = ref.to_dict()
        assert d == {"guid": "abc", "path_hint": "textures/foo.png"}

    def test_from_dict(self):
        ref = AssetRefBase.from_dict({"guid": "xyz", "path_hint": "bar.mat"})
        assert ref.guid == "xyz"
        assert ref.path_hint == "bar.mat"

    @pytest.mark.parametrize(
        "document",
        [None, {}, {"guid": "xyz"}, {"guid": "xyz", "path_hint": "", "path": "old"}],
    )
    def test_from_dict_rejects_noncanonical_documents(self, document):
        with pytest.raises(ValueError, match="complete current field set"):
            AssetRefBase.from_dict(document)

    def test_from_dict_rejects_non_string_values(self):
        with pytest.raises(TypeError, match="must be strings"):
            AssetRefBase.from_dict({"guid": 7, "path_hint": "bar.mat"})

    def test_display_name_with_path_hint(self):
        ref = AssetRefBase(guid="abc", path_hint="textures/foo.png")
        assert ref.display_name == "foo.png"

    def test_display_name_guid_only(self):
        ref = AssetRefBase(guid="abcdefgh1234")
        assert "GUID:" in ref.display_name

    def test_display_name_empty(self):
        ref = AssetRefBase()
        assert ref.display_name == "None"

    def test_is_missing_no_guid(self):
        ref = AssetRefBase()
        assert ref.is_missing is False

    def test_equality_same_guid(self):
        a = AssetRefBase(guid="abc")
        b = AssetRefBase(guid="abc")
        assert a == b

    def test_equality_different_guid(self):
        a = AssetRefBase(guid="abc")
        b = AssetRefBase(guid="def")
        assert a != b

    def test_hash(self):
        a = AssetRefBase(guid="abc")
        b = AssetRefBase(guid="abc")
        assert hash(a) == hash(b)

    def test_repr(self):
        ref = AssetRefBase(guid="abc", path_hint="test.png")
        r = repr(ref)
        assert "AssetRefBase" in r
        assert "abc" in r


# ═══════════════════════════════════════════════════════════════════════════
# Subclass ref types
# ═══════════════════════════════════════════════════════════════════════════

class TestTextureRef:
    def test_inherits_base(self):
        ref = TextureRef(guid="tex123")
        assert isinstance(ref, AssetRefBase)
        assert ref.guid == "tex123"

    def test_repr(self):
        ref = TextureRef(guid="abc")
        assert "TextureRef" in repr(ref)


class TestShaderRef:
    def test_inherits_base(self):
        ref = ShaderRef(guid="shd456")
        assert isinstance(ref, AssetRefBase)


class TestAudioClipRef:
    def test_inherits_base(self):
        ref = AudioClipRef(guid="aud789")
        assert isinstance(ref, AssetRefBase)


class TestMaterialRef:
    def test_from_guid(self):
        ref = MaterialRef(guid="mat123", path_hint="test.mat")
        assert ref.guid == "mat123"
        assert ref.path_hint == "test.mat"

    def test_repr(self):
        ref = MaterialRef(guid="abc")
        assert "MaterialRef" in repr(ref)

    def test_empty(self):
        ref = MaterialRef()
        assert ref.guid == ""
        assert bool(ref) is False
