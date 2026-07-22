"""Shared helpers for built-in RenderStack pipelines.

This module centralises the boilerplate and default conventions used by the
built-in forward/deferred pipelines so queue ranges, resource aliases, and
standard pass assembly stay in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from Infernux.lib import EngineConfig

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph


COLOR_TEXTURE = "color"
DEPTH_TEXTURE = "depth"
SHADOW_MAP_TEXTURE = "shadow_map"
BEFORE_POST_PROCESS_POINT = "before_post_process"
AFTER_POST_PROCESS_POINT = "after_post_process"

GBUFFER_ALBEDO_TEXTURE = "gbuffer_albedo"
GBUFFER_NORMAL_TEXTURE = "gbuffer_normal"
GBUFFER_MATERIAL_TEXTURE = "gbuffer_material"
GBUFFER_EMISSION_TEXTURE = "gbuffer_emission"

SCENE_RESOURCES = {COLOR_TEXTURE, DEPTH_TEXTURE}
POST_PROCESS_RESOURCES = {COLOR_TEXTURE}
GBUFFER_RESOURCES = {
    GBUFFER_ALBEDO_TEXTURE,
    GBUFFER_NORMAL_TEXTURE,
    GBUFFER_MATERIAL_TEXTURE,
    GBUFFER_EMISSION_TEXTURE,
    DEPTH_TEXTURE,
}

FORWARD_CLEAR_COLOR = (0.1, 0.1, 0.1, 1.0)
DEFERRED_GBUFFER_CLEAR_COLOR = (0.0, 0.0, 0.0, 0.0)
DEFERRED_LIGHTING_CLEAR_COLOR = (0.0, 0.0, 0.0, 1.0)

DEFERRED_LIGHTING_SHADER = "deferred_lighting"


def _config() -> EngineConfig:
    return EngineConfig.get()


def opaque_queue_range() -> tuple[int, int]:
    config = _config()
    return (config.opaque_queue_min, config.opaque_queue_max)


def transparent_queue_range() -> tuple[int, int]:
    config = _config()
    return (config.transparent_queue_min, config.transparent_queue_max)


def shadow_caster_queue_range() -> tuple[int, int]:
    config = _config()
    return (config.shadow_caster_queue_min, config.shadow_caster_queue_max)


def create_main_scene_targets(graph: "RenderGraph", *, shadow_resolution: int) -> None:
    from Infernux.rendergraph.graph import Format

    graph.create_texture(COLOR_TEXTURE, camera_target=True)
    graph.create_texture(DEPTH_TEXTURE, format=Format.D32_SFLOAT)
    graph.create_texture(
        SHADOW_MAP_TEXTURE,
        format=Format.D32_SFLOAT,
        size=(shadow_resolution, shadow_resolution),
    )


def create_deferred_gbuffer(graph: "RenderGraph") -> None:
    from Infernux.rendergraph.graph import Format

    graph.create_texture(GBUFFER_ALBEDO_TEXTURE, format=Format.RGBA16_SFLOAT)
    graph.create_texture(GBUFFER_NORMAL_TEXTURE, format=Format.RGBA16_SFLOAT)
    graph.create_texture(GBUFFER_MATERIAL_TEXTURE, format=Format.RGBA8_UNORM)
    graph.create_texture(GBUFFER_EMISSION_TEXTURE, format=Format.RGBA16_SFLOAT)


def add_shadow_caster_pass(
    graph: "RenderGraph",
    *,
    name: str = "ShadowCasterPass",
    queue_range: tuple[int, int] | None = None,
    light_index: int = 0,
) -> None:
    with graph.add_pass(name) as p:
        p.write_depth(SHADOW_MAP_TEXTURE)
        p.set_clear(depth=1.0)
        p.draw_shadow_casters(
            queue_range=queue_range or shadow_caster_queue_range(),
            light_index=light_index,
        )


def add_forward_opaque_pass(
    graph: "RenderGraph",
    *,
    name: str = "OpaquePass",
    clear_color: tuple[float, float, float, float] = FORWARD_CLEAR_COLOR,
    queue_range: tuple[int, int] | None = None,
    material_pass: str = "forward",
) -> None:
    with graph.add_pass(name) as p:
        p.write_color(COLOR_TEXTURE)
        p.write_depth(DEPTH_TEXTURE)
        p.set_clear(color=clear_color, depth=1.0)
        p.set_texture("shadowMap", SHADOW_MAP_TEXTURE)
        p.draw_renderers(
            queue_range=queue_range or opaque_queue_range(),
            sort_mode="front_to_back",
            material_pass=material_pass,
        )


def add_skybox_pass(
    graph: "RenderGraph",
    *,
    name: str = "SkyboxPass",
) -> None:
    with graph.add_pass(name) as p:
        p.read(DEPTH_TEXTURE)
        p.write_color(COLOR_TEXTURE)
        p.draw_skybox()


def add_transparent_pass(
    graph: "RenderGraph",
    *,
    name: str = "TransparentPass",
    queue_range: tuple[int, int] | None = None,
    material_pass: str = "forward",
) -> None:
    with graph.add_pass(name) as p:
        p.read(DEPTH_TEXTURE)
        p.write_color(COLOR_TEXTURE)
        p.set_texture("shadowMap", SHADOW_MAP_TEXTURE)
        p.draw_renderers(
            queue_range=queue_range or transparent_queue_range(),
            sort_mode="back_to_front",
            material_pass=material_pass,
        )


def add_standard_post_process_section(
    graph: "RenderGraph",
    *,
    enable_screen_ui: bool,
) -> None:
    if enable_screen_ui:
        graph.screen_ui_section(resources=POST_PROCESS_RESOURCES)
        return

    ensure_standard_post_process_points(graph)


def ensure_standard_post_process_points(graph: "RenderGraph") -> None:
    if not graph.has_injection_point(BEFORE_POST_PROCESS_POINT):
        graph.injection_point(BEFORE_POST_PROCESS_POINT, resources=POST_PROCESS_RESOURCES)
    if not graph.has_injection_point(AFTER_POST_PROCESS_POINT):
        graph.injection_point(AFTER_POST_PROCESS_POINT, resources=POST_PROCESS_RESOURCES)
