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
MOTION_TEXTURE = "motion"
MOTION_MSAA_TEXTURE = "_motion_msaa"
NORMAL_TEXTURE = "normal"
NORMAL_MSAA_TEXTURE = "_normal_msaa"
BEFORE_POST_PROCESS_POINT = "before_post_process"
AFTER_POST_PROCESS_POINT = "after_post_process"

GBUFFER_ALBEDO_TEXTURE = "gbuffer_albedo"
GBUFFER_NORMAL_TEXTURE = NORMAL_TEXTURE
GBUFFER_MATERIAL_TEXTURE = "gbuffer_material"
GBUFFER_EMISSION_TEXTURE = "gbuffer_emission"
GBUFFER_OBJECT_TEXTURE = "gbuffer_object"

POST_PROCESS_RESOURCES = {COLOR_TEXTURE, DEPTH_TEXTURE, MOTION_TEXTURE, NORMAL_TEXTURE}
GBUFFER_RESOURCES = {
    GBUFFER_ALBEDO_TEXTURE,
    GBUFFER_NORMAL_TEXTURE,
    GBUFFER_MATERIAL_TEXTURE,
    GBUFFER_EMISSION_TEXTURE,
    GBUFFER_OBJECT_TEXTURE,
    DEPTH_TEXTURE,
    MOTION_TEXTURE,
}

FORWARD_CLEAR_COLOR = (0.1, 0.1, 0.1, 1.0)
DEFERRED_GBUFFER_CLEAR_COLOR = (0.0, 0.0, 0.0, 0.0)
DEFERRED_LIGHTING_CLEAR_COLOR = (0.0, 0.0, 0.0, 1.0)

DEFERRED_LIGHTING_SHADER = "Deferred Lighting"


def _config() -> EngineConfig:
    return EngineConfig.get()


def opaque_queue_range() -> tuple[int, int]:
    config = _config()
    return (config.opaque_queue_min, config.opaque_queue_max)


def transparent_queue_range() -> tuple[int, int]:
    config = _config()
    return (config.transparent_queue_min, config.transparent_queue_max)


def result_resources(result) -> set[str]:
    """Return the named buffers exposed by one explicit pass result."""
    return set(result.snapshot) if result is not None else set()


def _result_texture_name(source: str, semantic: str, suffix: str = "") -> str:
    safe_source = str(source or "geometry").strip().replace(" ", "_")
    tail = f"_{suffix}" if suffix else ""
    return f"_result/{safe_source}/{semantic}{tail}"


def shadow_caster_queue_range() -> tuple[int, int]:
    config = _config()
    return (config.shadow_caster_queue_min, config.shadow_caster_queue_max)


def create_main_scene_targets(
    graph: "RenderGraph",
    *,
    shadow_resolution: int,
    msaa_samples: int = 1,
) -> None:
    from Infernux.rendergraph.graph import Format

    shadow_resolution = int(shadow_resolution)
    if shadow_resolution <= 0:
        raise ValueError(
            f"shadow resolution must be positive, got {shadow_resolution}"
        )

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
    graph.create_texture(GBUFFER_OBJECT_TEXTURE, format=Format.RG32_UINT)


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


def add_motion_vector_pass(
    graph: "RenderGraph",
    *,
    source: str,
    depth,
    queue_range: tuple[int, int],
    clear: bool = False,
    sort_mode: str = "front_to_back",
    msaa_samples: int = 1,
) -> object | None:
    """Rasterize motion only when this graph revision demands it."""
    from Infernux.rendergraph.graph import Format

    target = graph.create_texture(
        _result_texture_name(source, MOTION_TEXTURE),
        format=Format.RG16_SFLOAT,
        samples=1,
    )
    multisampled = int(msaa_samples) > 1
    draw_target = target
    if multisampled:
        draw_target = graph.create_texture(
            _result_texture_name(source, MOTION_TEXTURE, "msaa"),
            format=Format.RG16_SFLOAT,
            samples=int(msaa_samples),
        )
    with graph.add_pass(f"{source}/Motion") as render_pass:
        render_pass.read(depth)
        render_pass.write_color(draw_target)
        if multisampled:
            render_pass.write_resolve(target)
        if clear:
            render_pass.set_clear(color=(0.0, 0.0, 0.0, 0.0))
        render_pass.draw_renderers(
            queue_range=queue_range,
            sort_mode=sort_mode,
            material_pass="motion",
        )
    return target


def add_normal_buffer_pass(
    graph: "RenderGraph",
    *,
    source: str,
    depth,
    queue_range: tuple[int, int] | None = None,
    msaa_samples: int = 1,
) -> object | None:
    """Write nearest opaque normals against the existing scene depth."""
    from Infernux.rendergraph.graph import Format

    target = graph.create_texture(
        _result_texture_name(source, NORMAL_TEXTURE),
        format=Format.RGBA16_SFLOAT,
        samples=1,
    )
    multisampled = int(msaa_samples) > 1
    draw_target = target
    if multisampled:
        draw_target = graph.create_texture(
            _result_texture_name(source, NORMAL_TEXTURE, "msaa"),
            format=Format.RGBA16_SFLOAT,
            samples=int(msaa_samples),
        )
    with graph.add_pass(f"{source}/Normal") as render_pass:
        render_pass.read(depth)
        render_pass.write_color(draw_target)
        if multisampled:
            render_pass.write_resolve(target)
        render_pass.set_clear(color=(0.5, 0.5, 1.0, 0.0))
        render_pass.draw_renderers(
            queue_range=queue_range or opaque_queue_range(),
            sort_mode="front_to_back",
            material_pass="normal",
        )
    return target


def add_base_color_buffer_pass(
    graph: "RenderGraph",
    *,
    source: str,
    depth,
    queue_range: tuple[int, int],
    msaa_samples: int = 1,
) -> object:
    """Rasterize material base color for a forward geometry result."""
    from Infernux.rendergraph.graph import Format

    target = graph.create_texture(
        _result_texture_name(source, "base_color"),
        format=Format.RGBA16_SFLOAT,
        samples=1,
    )
    multisampled = int(msaa_samples) > 1
    draw_target = target
    if multisampled:
        draw_target = graph.create_texture(
            _result_texture_name(source, "base_color", "msaa"),
            format=Format.RGBA16_SFLOAT,
            samples=int(msaa_samples),
        )
    with graph.add_pass(f"{source}/BaseColor") as render_pass:
        render_pass.read(depth)
        render_pass.write_color(draw_target)
        if multisampled:
            render_pass.write_resolve(target)
        render_pass.set_clear(color=(0.0, 0.0, 0.0, 0.0))
        render_pass.draw_renderers(
            queue_range=queue_range,
            sort_mode="front_to_back",
            material_pass="base_color",
        )
    return target


def add_standard_post_process_section(graph: "RenderGraph", result=None) -> None:
    """Append the canonical camera-UI, post-process, and screen-UI tail.

    Screen UI is part of the standard camera output contract, not an optional
    RenderStack feature.  Keeping this tail unconditional also guarantees that
    a scene without RenderStack and an empty default RenderStack compile the
    same visible pipeline.
    """
    graph.screen_ui_section(resources=result_resources(result))


def ensure_standard_post_process_points(graph: "RenderGraph") -> None:
    resources = result_resources(graph.current_pass_result)
    if not graph.has_injection_point(BEFORE_POST_PROCESS_POINT):
        graph.injection_point(BEFORE_POST_PROCESS_POINT, resources=resources)
    if not graph.has_injection_point(AFTER_POST_PROCESS_POINT):
        graph.injection_point(AFTER_POST_PROCESS_POINT, resources=resources)
