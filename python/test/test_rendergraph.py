"""Tests for Infernux.rendergraph.graph — RenderGraph, Format, TextureHandle (real C++ backend)."""

from __future__ import annotations

import pytest
import Infernux.lib as native

from Infernux.lib import (
    CommandBuffer,
    RenderGraphDescription, GraphPassDesc, GraphTextureDesc,
    GraphBufferAccessType, GraphBufferUsage, GraphCommandType,
    GraphPassActionType, GraphPassType,
    MaterialPassType, PixelFormat, SampleCount,
)
from Infernux.rendergraph.graph import BufferHandle, RenderGraph, Format, TextureHandle
from Infernux.renderstack.effect_stage import EffectScope


# ── Helpers ──

def _make_graph():
    graph = RenderGraph("TestGraph")
    graph.create_texture("color", camera_target=True)
    graph.create_texture("depth", format=Format.D32_SFLOAT)
    return graph


# ══════════════════════════════════════════════════════════════════════
# Format enum
# ══════════════════════════════════════════════════════════════════════

class TestFormat:
    def test_color_formats(self):
        assert Format is PixelFormat
        assert Format.RGBA8_UNORM.name == "RGBA8_UNORM"
        assert Format.RGBA16_SFLOAT.name == "RGBA16_SFLOAT"

    def test_depth_formats(self):
        assert Format.D32_SFLOAT.is_depth
        assert Format.D24_UNORM_S8_UINT.is_depth
        assert not Format.RGBA8_UNORM.is_depth

    def test_native_rendering_surface_has_no_vulkan_format_types(self):
        assert not hasattr(native, "VkFormat")
        assert not hasattr(native, "VkSampleCount")

    def test_native_description_and_command_buffer_use_rhi_types(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as render_pass:
            render_pass.write_color("color")
            render_pass.write_depth("depth")
            render_pass.draw_renderers()
        graph.set_output("color")
        description = graph.build()
        assert description.textures[0].format == PixelFormat.RGBA8_UNORM
        assert description.textures[1].format == PixelFormat.D32_SFLOAT

        commands = CommandBuffer("RHI contract")
        handle = commands.get_temporary_rt(
            64, 64, PixelFormat.RGBA16_SFLOAT, SampleCount.COUNT_4
        )
        assert handle.is_valid()
        assert commands.command_count == 1


# ══════════════════════════════════════════════════════════════════════
# TextureHandle
# ══════════════════════════════════════════════════════════════════════

class TestTextureHandle:
    def test_default_properties(self):
        h = TextureHandle("color", Format.RGBA8_UNORM, is_camera_target=True)
        assert h.name == "color"
        assert h.is_camera_target
        assert not h.is_depth

    def test_depth_handle(self):
        h = TextureHandle("depth", Format.D32_SFLOAT)
        assert h.is_depth

    def test_eq_by_name(self):
        a = TextureHandle("x", Format.RGBA8_UNORM)
        b = TextureHandle("x", Format.RGBA16_SFLOAT)
        assert a == b

    def test_hash_by_name(self):
        a = TextureHandle("x", Format.RGBA8_UNORM)
        b = TextureHandle("x", Format.RGBA16_SFLOAT)
        assert hash(a) == hash(b)

    def test_repr(self):
        h = TextureHandle("color", Format.RGBA8_UNORM, is_camera_target=True)
        r = repr(h)
        assert "color" in r
        assert "camera_target" in r


# ══════════════════════════════════════════════════════════════════════
# Buffer and typed non-raster IR
# ══════════════════════════════════════════════════════════════════════

class TestTypedResourcePasses:
    @staticmethod
    def _add_camera_output(graph):
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        graph.set_output("color")

    def test_buffer_handle_and_usage(self):
        graph = _make_graph()
        handle = graph.create_buffer(
            "particles",
            4096,
            indirect=True,
            transfer_source=True,
        )
        assert isinstance(handle, BufferHandle)
        assert graph.buffer_count == 1
        assert handle.usage & int(GraphBufferUsage.STORAGE)
        assert handle.usage & int(GraphBufferUsage.INDIRECT)
        assert handle.usage & int(GraphBufferUsage.TRANSFER_SOURCE)

    def test_compute_pass_serializes_typed_buffer_accesses(self):
        graph = _make_graph()
        graph.create_buffer("particles", 4096)
        self._add_camera_output(graph)
        with graph.add_compute_pass("Simulate") as p:
            p.read_buffer("particles")
            p.write_buffer("particles")
            p.set_side_effect()

        description = graph.build()
        compute = next(p for p in description.passes if p.name == "Simulate")
        assert compute.type == GraphPassType.COMPUTE
        assert compute.commands == []
        assert compute.side_effect
        assert [a.type for a in compute.buffer_accesses] == [
            GraphBufferAccessType.STORAGE_READ,
            GraphBufferAccessType.STORAGE_WRITE,
        ]

    def test_buffer_copy_serializes_and_adds_transfer_usage(self):
        graph = _make_graph()
        graph.create_buffer("source", 1024)
        graph.create_buffer("destination", 2048)
        self._add_camera_output(graph)
        with graph.add_copy_pass("CopyParticles") as p:
            p.copy_buffer("source", "destination", byte_count=512)
            p.set_side_effect()

        description = graph.build()
        copy_pass = next(p for p in description.passes if p.name == "CopyParticles")
        command = copy_pass.commands[0]
        buffers = {buffer.name: buffer for buffer in description.buffers}
        assert copy_pass.type == GraphPassType.COPY
        assert command.type == GraphCommandType.COPY_BUFFER
        assert command.source_resource == "source"
        assert command.destination_resource == "destination"
        assert command.copy_bytes == 512
        assert buffers["source"].usage & int(GraphBufferUsage.TRANSFER_SOURCE)
        assert buffers["destination"].usage & int(GraphBufferUsage.TRANSFER_DESTINATION)

    def test_texture_copy_serializes(self):
        graph = _make_graph()
        graph.create_texture("source", format=Format.RGBA16_SFLOAT)
        graph.create_texture("destination", format=Format.RGBA16_SFLOAT)
        with graph.add_pass("Produce") as p:
            p.write_color("source")
            p.draw_renderers()
        with graph.add_copy_pass("CopyColor") as p:
            p.copy_texture("source", "destination")
        graph.set_output("destination")

        description = graph.build()
        copy_pass = next(p for p in description.passes if p.name == "CopyColor")
        command = copy_pass.commands[0]
        assert copy_pass.type == GraphPassType.COPY
        assert command.type == GraphCommandType.COPY_TEXTURE
        assert command.source_resource == "source"
        assert command.destination_resource == "destination"

    def test_present_pass_sets_graph_output(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        with graph.add_present_pass("Present") as p:
            p.present("color")

        description = graph.build()
        present = description.passes[-1]
        assert description.output_texture == "color"
        assert present.type == GraphPassType.PRESENT
        assert present.commands[0].type == GraphCommandType.PRESENT
        assert present.commands[0].source_resource == "color"

    def test_compute_access_requires_declared_usage(self):
        graph = _make_graph()
        graph.create_buffer("particles", 1024, indirect=False)
        self._add_camera_output(graph)
        with graph.add_compute_pass("BadIndirectRead") as p:
            p.read_buffer("particles", usage="indirect")
        with pytest.raises(ValueError, match="does not declare"):
            graph.build()

    def test_copy_rejects_same_resource(self):
        graph = _make_graph()
        graph.create_buffer("particles", 1024)
        self._add_camera_output(graph)
        with graph.add_copy_pass("BadCopy") as p:
            p.copy_buffer("particles", "particles")
        with pytest.raises(ValueError, match="distinct buffers"):
            graph.build()

    def test_texture_copy_rejects_camera_target(self):
        graph = _make_graph()
        graph.create_texture("destination", format=Format.RGBA8_UNORM)
        with graph.add_copy_pass("BadCameraCopy") as p:
            p.copy_texture("color", "destination")
        graph.set_output("destination")
        with pytest.raises(ValueError, match="requires transient textures"):
            graph.build()

    def test_present_rejects_depth_texture(self):
        graph = RenderGraph("BadPresent")
        graph.create_texture("depth", format=Format.D32_SFLOAT)
        with graph.add_present_pass("PresentDepth") as p:
            p.present("depth")
        with pytest.raises(ValueError, match="cannot export a depth"):
            graph.build()


# ══════════════════════════════════════════════════════════════════════
# RenderPassBuilder
# ══════════════════════════════════════════════════════════════════════

class TestRenderPassBuilder:
    def test_context_manager(self):
        graph = _make_graph()
        with graph.add_pass("Test") as p:
            p.write_color("color")
            p.draw_renderers()
        assert p._action == "draw_renderers"

    def test_draw_renderers_selects_linked_material_pass(self):
        graph = _make_graph()
        graph.create_texture("g0", format=Format.RGBA8_UNORM)
        with graph.add_pass("GBuffer") as p:
            p.write_color("g0")
            p.write_depth("depth")
            p.draw_renderers(material_pass="gbuffer")
        graph.set_output("g0")
        description = graph.build()
        assert description.passes[0].action == GraphPassActionType.NONE
        assert description.passes[0].commands[0].type == GraphCommandType.DRAW_RENDERERS
        assert description.passes[0].commands[0].material_pass == MaterialPassType.GBUFFER

    def test_draw_renderers_rejects_unknown_material_pass(self):
        graph = _make_graph()
        with graph.add_pass("Bad") as p:
            p.write_color("color")
            with pytest.raises(ValueError, match="Unknown material pass"):
                p.draw_renderers(material_pass="magic")

    def test_draw_skybox(self):
        graph = _make_graph()
        with graph.add_pass("Sky") as p:
            p.write_color("color")
            p.draw_skybox()
        assert p._action == "draw_skybox"

    def test_draw_shadow_casters(self):
        graph = _make_graph()
        graph.create_texture("shadow", format=Format.D32_SFLOAT, size=(2048, 2048))
        with graph.add_pass("Shadow") as p:
            p.write_depth("shadow")
            p.set_clear(depth=1.0)
            p.draw_shadow_casters(light_index=0)
        assert p._action == "draw_shadow_casters"
        assert p._light_index == 0

    def test_fullscreen_quad_with_params(self):
        graph = _make_graph()
        graph.create_texture("fx", format=Format.RGBA16_SFLOAT)
        with graph.add_pass("FX") as p:
            p.set_texture("_Src", "color")
            p.write_color("fx")
            p.set_param("intensity", 0.5)
            p.fullscreen_quad("my_shader")
        assert p._action == "fullscreen_quad"
        assert p._shader_name == "my_shader"
        assert p._push_constants["intensity"] == 0.5

    def test_fullscreen_quad_can_bind_dynamic_parameter_block(self):
        graph = _make_graph()
        graph.create_texture("fx", format=Format.RGBA16_SFLOAT)
        with graph.add_pass("FX") as p:
            p.set_texture("_Src", "color")
            p.write_color("fx")
            p.bind_parameter_block(
                "slot-1/composite",
                {"intensity": 0.5, "threshold": 1.0},
            )
            p.fullscreen_quad("my_shader")

        assert p._parameter_block == "slot-1/composite"
        assert list(p._push_constants) == ["intensity", "threshold"]

    def test_draw_screen_ui_camera(self):
        graph = _make_graph()
        with graph.add_pass("UI") as p:
            p.write_color("color")
            p.draw_screen_ui(list="camera")
        assert p._screen_ui_list == 0

    def test_draw_screen_ui_overlay(self):
        graph = _make_graph()
        with graph.add_pass("UI") as p:
            p.write_color("color")
            p.draw_screen_ui(list="overlay")
        assert p._screen_ui_list == 1

    def test_draw_screen_ui_invalid_raises(self):
        graph = _make_graph()
        with graph.add_pass("UI") as p:
            with pytest.raises(ValueError):
                p.draw_screen_ui(list="nonsense")

    def test_repr(self):
        graph = _make_graph()
        with graph.add_pass("Test") as p:
            p.draw_renderers()
        assert "Test" in repr(p)


# ══════════════════════════════════════════════════════════════════════
# RenderGraph — texture management
# ══════════════════════════════════════════════════════════════════════

class TestGraphTextures:
    def test_build_assigns_a_new_nonzero_source_revision(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as render_pass:
            render_pass.write_color("color")
            render_pass.write_depth("depth")
            render_pass.draw_renderers()
        graph.set_output("color")

        first = graph.build()
        second = graph.build()

        assert first.source_revision > 0
        assert second.source_revision > first.source_revision

    def test_create_and_get(self):
        g = RenderGraph("G")
        h = g.create_texture("t", format=Format.RGBA8_UNORM)
        assert g.get_texture("t") is h
        assert g.texture_count == 1

    def test_duplicate_name_raises(self):
        g = _make_graph()
        with pytest.raises(ValueError, match="already exists"):
            g.create_texture("color")

    def test_camera_target_depth_raises(self):
        g = RenderGraph("G")
        with pytest.raises(ValueError):
            g.create_texture("d", format=Format.D32_SFLOAT, camera_target=True)

    def test_size_and_divisor_mutually_exclusive(self):
        g = RenderGraph("G")
        with pytest.raises(ValueError):
            g.create_texture("t", size=(100, 100), size_divisor=2)

    def test_invalid_size_raises(self):
        g = RenderGraph("G")
        with pytest.raises(ValueError):
            g.create_texture("t", size=(0, 100))

    def test_divisor_one_raises(self):
        g = RenderGraph("G")
        with pytest.raises(ValueError):
            g.create_texture("t", size_divisor=1)

    def test_get_nonexistent_returns_none(self):
        g = RenderGraph("G")
        assert g.get_texture("nope") is None

    def test_name_scope_reuses_readable_local_resource_and_pass_names(self):
        graph = _make_graph()
        for scope in ("first", "second"):
            with graph.name_scope(scope):
                output = graph.create_texture("result", format=Format.RGBA16_SFLOAT)
                with graph.add_pass("Apply") as render_pass:
                    render_pass.set_texture("_SourceTex", "color")
                    render_pass.write_color(output)
                    render_pass.fullscreen_quad("effect")

        assert graph.get_texture("first/result") is not None
        assert graph.get_texture("second/result") is not None
        assert [render_pass.name for render_pass in graph._passes] == [
            "first/Apply",
            "second/Apply",
        ]

    def test_msaa_valid_values(self):
        g = RenderGraph("G")
        for v in (0, 1, 2, 4, 8):
            g.set_msaa_samples(v)
        with pytest.raises(ValueError):
            g.set_msaa_samples(3)


# ══════════════════════════════════════════════════════════════════════
# RenderGraph — pass management
# ══════════════════════════════════════════════════════════════════════

class TestPassManagement:
    def test_remove_pass_returns_builder(self):
        graph = _make_graph()
        with graph.add_pass("A") as p:
            p.write_color("color")
            p.draw_renderers()
        removed = graph.remove_pass("A")
        assert removed is not None
        assert removed._name == "A"
        assert graph.pass_count == 0

    def test_remove_nonexistent_returns_none(self):
        graph = _make_graph()
        assert graph.remove_pass("DoesNotExist") is None

    def test_remove_clears_topology(self):
        graph = _make_graph()
        with graph.add_pass("A") as p:
            p.write_color("color")
            p.draw_renderers()
        graph.remove_pass("A")
        assert not any(label == "A" for _, label in graph.topology_sequence)

    def test_append_pass_adds_to_end(self):
        graph = _make_graph()
        with graph.add_pass("A") as p:
            p.write_color("color")
            p.draw_renderers()
        with graph.add_pass("B") as p:
            p.write_color("color")
            p.draw_renderers()
        removed = graph.remove_pass("A")
        graph.append_pass(removed)
        names = [label for kind, label in graph.topology_sequence if kind == "pass"]
        assert names == ["B", "A"]

    def test_has_pass(self):
        graph = _make_graph()
        graph.add_pass("X")
        assert graph.has_pass("X")
        assert not graph.has_pass("Y")


# ══════════════════════════════════════════════════════════════════════
# Injection point callbacks
# ══════════════════════════════════════════════════════════════════════

class TestInjectionPointCallback:
    def test_callback_fires_for_explicit_ip(self):
        graph = _make_graph()
        fired = []
        graph._injection_callback = lambda name: fired.append(name)
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        graph.injection_point("after_opaque", resources={"color", "depth"})
        assert "after_opaque" in fired

    def test_callback_fires_for_screen_ui_section(self):
        graph = _make_graph()
        fired = []
        graph._injection_callback = lambda name: fired.append(name)
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        graph.screen_ui_section()
        assert "before_post_process" in fired
        assert "after_post_process" in fired

    def test_auto_inject_does_not_fire_callback(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        fired = []
        graph._injection_callback = lambda name: fired.append(name)
        graph._injection_callback = None
        graph.set_output("color")
        graph.build()
        assert "before_post_process" not in fired

    def test_manual_inject_before_build(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        fired = []
        graph._injection_callback = lambda name: fired.append(name)
        if not graph.has_injection_point("before_post_process"):
            graph.injection_point("before_post_process", resources={"color"})
        if not graph.has_injection_point("after_post_process"):
            graph.injection_point("after_post_process", resources={"color"})
        assert "before_post_process" in fired
        assert "after_post_process" in fired
        graph._injection_callback = None
        graph.set_output("color")
        desc = graph.build()
        ip_names = [ip.name for ip in graph.injection_points]
        assert ip_names.count("before_post_process") == 1
        assert ip_names.count("after_post_process") == 1


class TestEffectStageDeclaration:
    def test_effect_stage_records_stable_identity_and_contract(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as render_pass:
            render_pass.write_color("color")
            render_pass.draw_renderers()

        fired = []
        graph._effect_stage_callback = fired.append
        stage = graph.effects(
            "final",
            scope="composite",
            display_name="Final Post Processing",
            inputs={"color"},
            outputs={"color"},
            capabilities={"fullscreen"},
            aliases=("post_process",),
        )

        assert stage.scope is EffectScope.COMPOSITE
        assert stage.contract.inputs == frozenset({"color"})
        assert graph.effect_stages == [stage]
        assert graph.has_effect_stage("final")
        assert graph.has_effect_stage("post_process")
        assert ("effect_stage", "final") in graph.topology_sequence
        assert fired == [stage]

    def test_effect_stage_rejects_duplicate_ids_and_aliases(self):
        graph = _make_graph()
        graph.effects("final", aliases=("post_process",))

        with pytest.raises(ValueError, match="must be unique"):
            graph.effects("post_process")
        with pytest.raises(ValueError, match="must be unique"):
            graph.effects("other", aliases=("final",))

    def test_effect_stage_before_first_pass_is_rejected(self):
        graph = _make_graph()
        graph.effects("final")
        with graph.add_pass("Opaque") as render_pass:
            render_pass.write_color("color")
            render_pass.draw_renderers()
        graph.set_output("color")

        with pytest.raises(ValueError, match="requires an upstream result"):
            graph.build()


# ══════════════════════════════════════════════════════════════════════
# Overlay reordering
# ══════════════════════════════════════════════════════════════════════

class TestOverlayReordering:
    def test_overlay_moved_after_blit(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        with graph.add_pass("_ScreenUI_Overlay") as p:
            p.write_color("color")
            p.draw_screen_ui(list="overlay")
        overlay = graph.remove_pass("_ScreenUI_Overlay")
        assert overlay is not None
        with graph.add_pass("_FinalCompositeBlit") as p:
            p.set_texture("_SourceTex", "color")
            p.write_color("color")
            p.fullscreen_quad("fullscreen_blit")
        graph.append_pass(overlay)
        names = [label for kind, label in graph.topology_sequence if kind == "pass"]
        assert names == ["Opaque", "_FinalCompositeBlit", "_ScreenUI_Overlay"]


# ══════════════════════════════════════════════════════════════════════
# Build & validation
# ══════════════════════════════════════════════════════════════════════

class TestBuild:
    def test_basic_build_succeeds(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.write_depth("depth")
            p.draw_renderers()
        graph.set_output("color")
        desc = graph.build()
        assert desc.name == "TestGraph"
        assert desc.output_texture == "color"
        assert graph.has_injection_point("before_post_process")
        assert graph.has_injection_point("after_post_process")

    def test_shadow_pass_preserves_light_index(self):
        graph = _make_graph()
        graph.create_texture("shadow_map", format=Format.D32_SFLOAT, size=(4096, 4096))
        with graph.add_pass("ShadowCaster") as p:
            p.write_depth("shadow_map")
            p.set_clear(depth=1.0)
            p.draw_shadow_casters(light_index=0)
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.write_depth("depth")
            p.draw_renderers()
        graph.set_output("color")
        desc = graph.build()
        shadow_pass = next(p for p in desc.passes if p.name == "ShadowCaster")
        assert shadow_pass.commands[0].type == GraphCommandType.DRAW_SHADOW_CASTERS
        assert shadow_pass.commands[0].light_index == 0
        # hard/soft shadow selection lives on the Light component, not the
        # graph pass (the former shadow_type parameter was a dead end and
        # has been removed from the API).

    def test_fullscreen_quad_push_constants(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        graph.create_texture("_fx_out", format=Format.RGBA16_SFLOAT)
        with graph.add_pass("FX") as p:
            p.set_texture("_SourceTex", "color")
            p.write_color("_fx_out")
            p.set_param("intensity", 0.5)
            p.set_param("threshold", 1.0)
            p.fullscreen_quad("my_effect")
        graph.set_output("_fx_out")
        desc = graph.build()
        fx_pass = next(p for p in desc.passes if p.name == "FX")
        assert fx_pass.commands[0].type == GraphCommandType.FULLSCREEN_QUAD
        assert fx_pass.commands[0].shader_name == "my_effect"
        pc_dict = dict(fx_pass.commands[0].push_constants)
        assert pc_dict["intensity"] == 0.5
        assert pc_dict["threshold"] == 1.0

    def test_dynamic_parameter_block_is_emitted_in_command_ir(self):
        graph = _make_graph()
        with graph.add_pass("Opaque") as p:
            p.write_color("color")
            p.draw_renderers()
        graph.create_texture("_fx_out", format=Format.RGBA16_SFLOAT)
        with graph.add_pass("FX") as p:
            p.set_texture("_SourceTex", "color")
            p.write_color("_fx_out")
            p.bind_parameter_block("slot-1/fx", {"intensity": 0.5})
            p.fullscreen_quad("my_effect")
        graph.set_output("_fx_out")

        command = graph.build().passes[1].commands[0]

        assert command.parameter_block == "slot-1/fx"
        assert command.push_constants == [("intensity", 0.5)]

    def test_empty_graph_raises(self):
        g = RenderGraph("Empty")
        with pytest.raises(ValueError, match="no passes"):
            g.build()

    def test_output_unknown_raises(self):
        graph = _make_graph()
        with graph.add_pass("A") as p:
            p.write_color("color")
            p.draw_renderers()
        with pytest.raises(ValueError, match="not found"):
            graph.set_output("nope")

    def test_auto_output_from_camera_target(self):
        graph = _make_graph()
        with graph.add_pass("A") as p:
            p.write_color("color")
            p.draw_renderers()
        desc = graph.build()
        assert desc.output_texture == "color"


# ══════════════════════════════════════════════════════════════════════
# Validation error cases
# ══════════════════════════════════════════════════════════════════════

class TestValidation:
    def test_shadow_caster_with_color_raises(self):
        graph = _make_graph()
        graph.create_texture("sm", format=Format.D32_SFLOAT, size=(1024, 1024))
        with graph.add_pass("Bad") as p:
            p.write_color("color")
            p.write_depth("sm")
            p.draw_shadow_casters()
        graph.set_output("color")
        with pytest.raises(ValueError, match="depth-only"):
            graph.build()

    def test_clear_depth_without_depth_output_raises(self):
        graph = _make_graph()
        with graph.add_pass("Bad") as p:
            p.write_color("color")
            p.set_clear(depth=1.0)
            p.draw_renderers()
        graph.set_output("color")
        with pytest.raises(ValueError, match="clears depth"):
            graph.build()

    def test_read_unknown_texture_raises(self):
        graph = _make_graph()
        with graph.add_pass("Bad") as p:
            p._reads.append("nonexistent")
            p.write_color("color")
            p.draw_renderers()
        graph.set_output("color")
        with pytest.raises(ValueError, match="unknown texture"):
            graph.build()

    def test_write_depth_on_color_texture_raises(self):
        graph = _make_graph()
        with graph.add_pass("Bad") as p:
            p._write_depth = "color"
            p.write_color("color")
            p.draw_renderers()
        graph.set_output("color")
        with pytest.raises(ValueError, match="color texture"):
            graph.build()
