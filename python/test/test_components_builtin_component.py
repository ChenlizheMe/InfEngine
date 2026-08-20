"""Tests for Infernux.components.builtin_component — CppProperty, BuiltinComponent (real C++ backend)."""

from __future__ import annotations

from enum import IntEnum

import pytest

from Infernux.components.builtin_component import BuiltinComponent, CppProperty
from Infernux.components.fields import FieldType, get_serialized_fields
import Infernux.lib as lib


# ── Test helpers ──

class DemoEnum(IntEnum):
    A = 1
    B = 2


class DemoCpp:
    """Minimal stand-in for a C++ component (needed for CppProperty __set_name__)."""
    def __init__(self):
        self.component_id = 42
        self.mode = 2
        self.raw = 11
        self.locked = 5
        self.intensity = 1.0


class DemoBuiltin(BuiltinComponent):
    _cpp_type_name = "DemoBuiltin"

    mode = CppProperty("mode", FieldType.ENUM, default=DemoEnum.A, enum_type=DemoEnum)
    raw = CppProperty("raw", FieldType.INT, default=7)
    locked = CppProperty("locked", FieldType.INT, default=3, readonly=True)
    intensity = CppProperty("intensity", FieldType.FLOAT, default=1.0)


class LazyEnumBuiltin(BuiltinComponent):
    _cpp_type_name = "LazyEnumBuiltin"

    mode = CppProperty("mode", FieldType.ENUM, default=DemoEnum.A, enum_type="DemoEnum")


# ══════════════════════════════════════════════════════════════════════
# CppProperty descriptor
# ══════════════════════════════════════════════════════════════════════

class TestCppPropertyBinding:
    def test_unbound_access_is_rejected(self):
        demo = DemoBuiltin()
        with pytest.raises(ReferenceError):
            _ = demo.mode
        with pytest.raises(ReferenceError):
            demo.raw = 7

    def test_set_name_assigns_metadata_name(self):
        desc = DemoBuiltin.__dict__["raw"]
        assert desc.metadata.name == "raw"

    def test_display_name_key_is_preserved_in_metadata(self):
        class LocalizedBuiltin(BuiltinComponent):
            _cpp_type_name = "LocalizedBuiltin"

            value = CppProperty(
                "value",
                FieldType.FLOAT,
                default=0.0,
                display_name_key="component.localized.value",
            )

        assert (
            LocalizedBuiltin.__dict__["value"].metadata.display_name_key
            == "component.localized.value"
        )


class TestCppPropertyReadWrite:
    def test_reads_from_cpp_and_casts_enum(self):
        demo = DemoBuiltin()
        demo._cpp_component = DemoCpp()
        assert demo.mode is DemoEnum.B   # cpp.mode = 2, DemoEnum(2) == B
        assert demo.raw == 11

    def test_writes_to_cpp(self):
        cpp = DemoCpp()
        demo = DemoBuiltin()
        demo._cpp_component = cpp

        demo.mode = DemoEnum.A
        demo.raw = 42

        assert cpp.mode == 1
        assert cpp.raw == 42

    def test_readonly_rejects_set(self):
        demo = DemoBuiltin()
        demo._cpp_component = DemoCpp()
        with pytest.raises(AttributeError):
            demo.locked = 10

    def test_lazy_enum_type_resolved_from_lib(self):
        """Test lazy enum resolution using the real Infernux.lib."""
        lib.DemoEnum = DemoEnum
        try:
            demo = LazyEnumBuiltin()
            demo._cpp_component = DemoCpp()
            assert demo.mode is DemoEnum.B
        finally:
            del lib.DemoEnum


class TestCppPropertyEdgeCases:
    def test_class_access_returns_descriptor(self):
        desc = DemoBuiltin.mode
        assert isinstance(desc, CppProperty)

    def test_runtime_error_invalidates_binding_and_raises(self):
        class BadCpp:
            component_id = 42

            @property
            def raw(self):
                raise RuntimeError("dead")
        demo = DemoBuiltin()
        demo._cpp_component = BadCpp()
        with pytest.raises(ReferenceError):
            _ = demo.raw
        assert demo._cpp_component is None


# ══════════════════════════════════════════════════════════════════════
# BuiltinComponent subclass
# ══════════════════════════════════════════════════════════════════════

class TestBuiltinComponent:
    def test_registered_in_builtin_registry(self):
        assert "DemoBuiltin" in BuiltinComponent._builtin_registry
        assert BuiltinComponent._builtin_registry["DemoBuiltin"] is DemoBuiltin

    def test_isinstance_inf_component(self):
        from Infernux.components.component import InxComponent
        demo = DemoBuiltin()
        assert isinstance(demo, InxComponent)

    def test_serialized_fields_contain_cpp_properties(self):
        fields = get_serialized_fields(DemoBuiltin)
        assert "mode" in fields
        assert "raw" in fields
        assert "locked" in fields

    def test_raw_field_value_reads_live_cpp_not_metadata_default(self):
        from Infernux.components.fields import get_raw_field_value

        cpp = DemoCpp()
        cpp.intensity = 4.5
        demo = DemoBuiltin()
        demo._cpp_component = cpp
        assert "intensity" not in demo.__dict__
        assert get_raw_field_value(demo, "intensity") == 4.5

    def test_scene_rebuild_marks_wrapper_stale_without_clearing_pointer(self):
        class _Scene:
            structure_version = 4

            def resolve_component(self, _handle):
                return None

        class _GameObject:
            id = 9
            handle = None

            def __init__(self, scene):
                self.scene = scene

        scene = _Scene()
        cpp = DemoCpp()
        demo = DemoBuiltin()
        demo._bind_cpp(cpp, _GameObject(scene))
        assert demo._get_bound_native_component() is cpp
        assert not demo._is_native_binding_stale()

        scene.structure_version += 1
        assert demo._is_native_binding_stale()
        assert demo._get_bound_native_component() is cpp
        assert demo._require_cpp_component() is cpp
        assert demo._cpp_component is cpp
        assert not demo._is_destroyed

    def test_structure_bump_does_not_unbind_cached_rigidbody_wrapper(self):
        class _Scene:
            structure_version = 4

            def resolve_component(self, _handle):
                return cpp

        class _GameObject:
            id = 9
            handle = 3

            def __init__(self, scene):
                self.scene = scene

        scene = _Scene()
        cpp = DemoCpp()
        cpp.handle = 17
        demo = DemoBuiltin()
        demo._bind_cpp(cpp, _GameObject(scene))
        scene.structure_version += 1

        assert demo._require_cpp_component() is cpp
        assert not demo._is_destroyed
        assert not demo._is_native_binding_stale()

    def test_dead_handle_still_invalidates_builtin_wrapper(self):
        class _Scene:
            structure_version = 4

            def resolve_component(self, _handle):
                return None

        class _GameObject:
            id = 9
            handle = 3

            def __init__(self, scene):
                self.scene = scene

        scene = _Scene()
        cpp = DemoCpp()
        cpp.handle = 17
        demo = DemoBuiltin()
        demo._bind_cpp(cpp, _GameObject(scene))

        assert demo._get_bound_native_component() is None
        assert demo._is_destroyed

    def test_repr_unbound(self):
        demo = DemoBuiltin()
        r = repr(demo)
        assert "DemoBuiltin" in r
        assert "bound=False" in r

    def test_repr_bound(self):
        cpp = DemoCpp()
        cpp.component_id = 42
        cpp.enabled = True
        demo = DemoBuiltin()
        demo._bind_cpp(cpp, type("FakeGO", (), {"id": 1})())
        r = repr(demo)
        assert "bound=True" in r

    def test_native_wrapper_does_not_enter_script_instance_registry(self):
        from Infernux.components.component import InxComponent

        previous = {
            game_object_id: list(components)
            for game_object_id, components in InxComponent._active_instances.items()
        }
        InxComponent._active_instances.clear()
        try:
            demo = DemoBuiltin()
            demo._bind_cpp(DemoCpp(), type("FakeGO", (), {"id": 1})())
            assert InxComponent._active_instances == {}
            assert demo._cds_slot is None
            assert demo._cds_class_id is None
        finally:
            InxComponent._active_instances.clear()
            InxComponent._active_instances.update(previous)

    def test_mcp_serialization_reports_wrapper_as_native(self):
        from Infernux.mcp.tools.common import serialize_component

        cpp = DemoCpp()
        cpp.enabled = True
        cpp.execution_order = 0
        wrapper = DemoBuiltin()
        wrapper._bind_cpp(cpp, type("FakeGO", (), {"id": 1})())

        assert serialize_component(wrapper) == {
            "type": "DemoBuiltin",
            "python": False,
            "component_id": 42,
        }

    def test_clear_cache(self):
        BuiltinComponent._wrapper_cache.clear()
        BuiltinComponent._clear_cache()
        assert len(BuiltinComponent._wrapper_cache) == 0

    def test_clear_cache_does_not_import_inspector_in_player(self, monkeypatch):
        import builtins

        from Infernux.application import Application

        BuiltinComponent._wrapper_cache.clear()
        monkeypatch.setattr(Application, "is_editor", staticmethod(lambda: False))
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "Infernux.engine.ui.inspector_components":
                raise AssertionError("Player cache cleanup imported editor Inspector state")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        BuiltinComponent._clear_cache()

    def test_wrapper_rebinds_when_native_instance_is_replaced(self):
        BuiltinComponent._wrapper_cache.clear()
        game_object = type("FakeGO", (), {"id": 1})()
        first_cpp = DemoCpp()
        first_cpp.component_id = 42
        first_cpp.handle = 1001
        first_cpp.intensity = 1.0
        wrapper = DemoBuiltin._get_or_create_wrapper(first_cpp, game_object)

        replacement = DemoCpp()
        replacement.component_id = 42
        replacement.handle = 2002
        replacement.intensity = 1.0
        rebound = DemoBuiltin._get_or_create_wrapper(replacement, game_object)

        assert rebound is wrapper
        assert rebound._get_bound_native_component() is replacement
        rebound.intensity = 8.0
        assert replacement.intensity == 8.0
        assert first_cpp.intensity == 1.0

    def test_wrapper_keeps_identity_for_same_native_handle(self):
        BuiltinComponent._wrapper_cache.clear()
        game_object = type("FakeGO", (), {"id": 1})()
        first_cpp = DemoCpp()
        first_cpp.component_id = 42
        first_cpp.handle = 1001
        wrapper = DemoBuiltin._get_or_create_wrapper(first_cpp, game_object)

        same_native = DemoCpp()
        same_native.component_id = 42
        same_native.handle = 1001
        same_native.intensity = 3.0
        again = DemoBuiltin._get_or_create_wrapper(same_native, game_object)

        assert again is wrapper
        assert again._get_bound_native_component() is first_cpp

    def test_sprite_renderer_invalidation_releases_event_and_material_refs(self):
        import gc
        import weakref

        from Infernux.components.builtin.sprite_renderer import SpriteRenderer
        from Infernux.engine.interaction import (
            AssetMutationService,
            DocumentRegistry,
            SelectionService,
        )

        previous = AssetMutationService.instance()
        if previous is not None:
            previous.shutdown()
        bus = AssetMutationService(DocumentRegistry(), SelectionService())
        baseline = bus.listener_diagnostics["component_callbacks"]
        wrapper = SpriteRenderer()
        wrapper._sprite_material = object()
        wrapper._material_ready = True
        wrapper._sprite_frames = [{"name": "frame"}]
        wrapper._sprite_frames_by_id = {"frame": wrapper._sprite_frames[0]}
        wrapper._subscribe_asset_events()
        wrapper_ref = weakref.ref(wrapper)

        assert bus.listener_diagnostics["component_callbacks"] == baseline + 1

        wrapper._invalidate_native_binding()
        assert bus.listener_diagnostics["component_callbacks"] == baseline
        assert wrapper._sprite_material is None
        assert wrapper._material_ready is False
        assert wrapper._sprite_frames == []
        assert wrapper._sprite_frames_by_id == {}

        del wrapper
        gc.collect()
        assert wrapper_ref() is None
        bus.shutdown()

    def test_sprite_renderer_missing_frame_id_does_not_fall_back_by_index(self):
        from types import SimpleNamespace

        from Infernux.components.builtin.sprite_renderer import SpriteRenderer
        from Infernux.core.asset_types import SpriteFrame

        class _Material:
            def __init__(self):
                self.values = {}

            def set_vector4(self, name, *value):
                self.values[name] = value

        existing = SpriteFrame(
            stable_id="1" * 32,
            name="existing",
            w=32,
            h=32,
        )
        wrapper = SpriteRenderer()
        wrapper._cpp_component = SimpleNamespace(
            sprite_guid="texture-guid",
            frame_id="2" * 32,
            flip_x=False,
            flip_y=True,
        )
        wrapper._sprite_frames = [existing]
        wrapper._sprite_frames_by_id = {existing.stable_id: existing}
        wrapper._tex_w = 32
        wrapper._tex_h = 32
        material = _Material()
        wrapper._get_material = lambda: material

        wrapper._apply_uv_rect()

        assert material.values["uvRect"] == (0.0, 0.0, 0.0, 0.0)
        assert material.values["displayScale"] == (0.0, 0.0, 0.0, 0.0)

    def test_sprite_renderer_uses_published_metadata_without_meta_sidecar(
        self,
        monkeypatch,
    ):
        from types import SimpleNamespace

        from Infernux.components.builtin import sprite_renderer as sprite_module
        from Infernux.components.builtin.sprite_renderer import SpriteRenderer

        frame_id = "1" * 32
        metadata_document = {
            "metadata": {
                "width": {"type": "int", "value": 128},
                "height": {"type": "int", "value": 64},
                "texture_type": {"type": "string", "value": "sprite"},
                "sprite_frames": {
                    "type": "json_array",
                    "value": [{
                        "stable_id": frame_id,
                        "name": "runtime-frame",
                        "x": 32,
                        "y": 16,
                        "w": 32,
                        "h": 16,
                        "pivot_x": 0.5,
                        "pivot_y": 0.5,
                    }],
                },
            }
        }
        native_meta = SimpleNamespace(
            serialize_document=lambda: metadata_document,
        )
        database = SimpleNamespace(
            get_path_from_guid=lambda guid: (
                "Assets/runtime-sheet.png" if guid == "texture-guid" else ""
            ),
            get_meta_by_guid=lambda guid: (
                native_meta if guid == "texture-guid" else None
            ),
        )
        monkeypatch.setattr(sprite_module, "_get_asset_database", lambda: database)
        from Infernux.core import asset_types
        monkeypatch.setattr(asset_types, "_published_asset_database", lambda: database)

        class _Material:
            def __init__(self):
                self.values = {}

            def set_texture(self, name, value):
                self.values[name] = value

            def set_vector4(self, name, *value):
                self.values[name] = value

        wrapper = SpriteRenderer()
        wrapper._cpp_component = SimpleNamespace(
            sprite_guid="texture-guid",
            frame_id=frame_id,
            flip_x=False,
            flip_y=True,
        )
        material = _Material()
        wrapper._get_material = lambda: material

        wrapper._load_sprite_data()
        wrapper._apply_uv_rect()

        assert wrapper._tex_w == 128
        assert wrapper._tex_h == 64
        assert [frame.stable_id for frame in wrapper._sprite_frames] == [frame_id]
        assert material.values["texSampler"] == "texture-guid"
        assert material.values["uvRect"] == (0.25, 0.25, 0.25, 0.25)
        assert material.values["displayScale"] == (1.0, 0.5, 0.0, 0.0)

    def test_native_sprite_renderer_scene_document_uses_stable_frame_id(self):
        from Infernux.lib import SpriteRenderer as NativeSpriteRenderer

        renderer = NativeSpriteRenderer()
        renderer.sprite_guid = "texture-guid"
        renderer.frame_id = "0123456789abcdef0123456789abcdef"

        document = renderer.serialize_document()

        assert document["frameId"] == renderer.frame_id
        assert "frameIndex" not in document
        restored = NativeSpriteRenderer()
        assert restored.deserialize_document(document)
        assert restored.frame_id == renderer.frame_id

    def test_native_sprite_renderer_rejects_invalid_frame_identity(self):
        from Infernux.lib import SpriteRenderer as NativeSpriteRenderer

        renderer = NativeSpriteRenderer()
        with pytest.raises(ValueError, match="32-character lowercase UUID"):
            renderer.frame_id = "legacy-index-3"

        document = renderer.serialize_document()
        document["frameId"] = "ABCDEF0123456789ABCDEF0123456789"
        assert renderer.deserialize_document(document) is False

    def test_sprite_assignment_binds_texture_and_persisted_frame_atomically(
        self,
        monkeypatch,
    ):
        from types import SimpleNamespace

        from Infernux.components.builtin import sprite_renderer as sprite_module
        from Infernux.components.builtin.sprite_renderer import SpriteRenderer
        from Infernux.core import asset_types

        first = asset_types.SpriteFrame(
            stable_id="1" * 32,
            name="first",
            w=32,
            h=32,
        )
        second = asset_types.SpriteFrame(
            stable_id="2" * 32,
            name="second",
            x=32,
            w=32,
            h=32,
        )
        settings = asset_types.TextureImportSettings(
            texture_type=asset_types.TextureType.SPRITE,
            sprite_frames=[first, second],
        )
        monkeypatch.setattr(
            sprite_module,
            "_get_asset_database",
            lambda: SimpleNamespace(
                get_path_from_guid=lambda guid: (
                    "C:/Project/Assets/sheet.png" if guid == "texture-guid" else ""
                )
            ),
        )
        monkeypatch.setattr(
            asset_types,
            "read_texture_import_settings",
            lambda _path: settings,
        )

        wrapper = SpriteRenderer()
        wrapper._cpp_component = SimpleNamespace(
            component_id=1,
            sprite_guid="",
            frame_id="",
        )
        monkeypatch.setattr(wrapper, "_load_sprite_data", lambda: None)
        monkeypatch.setattr(wrapper, "_apply_uv_rect", lambda: None)

        wrapper.sprite = {"guid": "texture-guid"}
        assert wrapper._cpp_component.sprite_guid == "texture-guid"
        assert wrapper._cpp_component.frame_id == first.stable_id

        wrapper._cpp_component.frame_id = second.stable_id
        wrapper.sprite = {"guid": "texture-guid"}
        assert wrapper._cpp_component.frame_id == second.stable_id

        requested = wrapper._sprite_assignment_snapshot(
            {"guid": "texture-guid", "frame_id": first.stable_id}
        )
        assert requested == {
            "sprite_guid": "texture-guid",
            "frame_id": first.stable_id,
        }

        with pytest.raises(ValueError, match="does not belong"):
            wrapper._sprite_assignment_snapshot(
                {"guid": "texture-guid", "frame_id": "f" * 32}
            )

        wrapper.sprite = None
        assert wrapper._cpp_component.sprite_guid == ""
        assert wrapper._cpp_component.frame_id == ""


class TestInxComponentSceneMutation:
    def test_prefab_instantiate_structure_bump_keeps_script_game_object(self, monkeypatch):
        """Ordinary scene mutations must not unbind user scripts before start().

        Prefab instantiate / activate bumps Scene.structure_version.  That
        signal is only for BuiltinComponent inspector wrappers after a Play
        rebuild, not for live InxComponents such as CoinCollisionReporter.
        """
        from Infernux.components.component import InxComponent

        class CoinStartProbe(InxComponent):
            _uses_component_data_store = False

            def start(self):
                self.seen_id = int(self.game_object.id)

        class _Scene:
            structure_version = 4

            def resolve_component(self, _handle):
                return cpp

        class _GameObject:
            id = 88
            handle = 9

            def __init__(self, scene):
                self.scene = scene

        scene = _Scene()
        game_object = _GameObject(scene)
        cpp = DemoCpp()
        cpp.handle = 17
        cpp.game_object = game_object
        cpp.enabled = True
        cpp.execution_order = 0

        monkeypatch.setattr(
            InxComponent,
            "_is_native_game_object_alive",
            staticmethod(lambda go: go is not None and int(getattr(go, "id", 0)) > 0),
        )

        probe = CoinStartProbe()
        probe._bind_native_component(cpp, game_object)
        scene.structure_version += 1

        assert not probe._is_native_binding_stale()
        assert probe._get_bound_native_component() is cpp
        assert not probe._is_destroyed

        probe.start()
        assert probe.seen_id == 88
        assert probe.game_object is game_object
