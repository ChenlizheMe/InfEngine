from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Infernux.rendergraph.graph import RenderGraph

COLOR_TEXTURE: str
DEPTH_TEXTURE: str
SHADOW_MAP_TEXTURE: str
MOTION_TEXTURE: str
MOTION_MSAA_TEXTURE: str
NORMAL_TEXTURE: str
NORMAL_MSAA_TEXTURE: str
BEFORE_POST_PROCESS_POINT: str
AFTER_POST_PROCESS_POINT: str

GBUFFER_ALBEDO_TEXTURE: str
GBUFFER_NORMAL_TEXTURE: str
GBUFFER_MATERIAL_TEXTURE: str
GBUFFER_EMISSION_TEXTURE: str

POST_PROCESS_RESOURCES: set[str]
GBUFFER_RESOURCES: set[str]

FORWARD_CLEAR_COLOR: tuple[float, float, float, float]
DEFERRED_GBUFFER_CLEAR_COLOR: tuple[float, float, float, float]
DEFERRED_LIGHTING_CLEAR_COLOR: tuple[float, float, float, float]

DEFERRED_LIGHTING_SHADER: str

def opaque_queue_range() -> tuple[int, int]: ...
def transparent_queue_range() -> tuple[int, int]: ...
def result_resources(result) -> set[str]: ...
def shadow_caster_queue_range() -> tuple[int, int]: ...
def create_main_scene_targets(
    graph: RenderGraph,
    *,
    shadow_resolution: int,
    msaa_samples: int = ...,
) -> None: ...
def create_deferred_gbuffer(graph: RenderGraph) -> None: ...
def add_shadow_caster_pass(
    graph: RenderGraph,
    *,
    name: str = ...,
    queue_range: tuple[int, int] | None = ...,
    light_index: int = ...,
) -> None: ...
def add_forward_opaque_pass(
    graph: RenderGraph,
    *,
    name: str = ...,
    clear_color: tuple[float, float, float, float] = ...,
    queue_range: tuple[int, int] | None = ...,
    material_pass: str = ...,
) -> None: ...
def add_skybox_pass(graph: RenderGraph, *, name: str = ...) -> None: ...
def add_transparent_pass(
    graph: RenderGraph,
    *,
    name: str = ...,
    queue_range: tuple[int, int] | None = ...,
    material_pass: str = ...,
) -> None: ...
def add_motion_vector_pass(
    graph: RenderGraph,
    *,
    source: str,
    depth,
    queue_range: tuple[int, int],
    clear: bool = ...,
    sort_mode: str = ...,
    msaa_samples: int = ...,
) -> object | None: ...
def add_normal_buffer_pass(
    graph: RenderGraph,
    *,
    source: str,
    depth,
    queue_range: tuple[int, int] | None = ...,
    msaa_samples: int = ...,
) -> object | None: ...
def add_base_color_buffer_pass(
    graph: RenderGraph,
    *,
    source: str,
    depth,
    queue_range: tuple[int, int],
    msaa_samples: int = ...,
) -> object: ...
def add_standard_post_process_section(graph: RenderGraph, result=...) -> None: ...
def ensure_standard_post_process_points(graph: RenderGraph) -> None: ...
def ensure_standard_screen_ui_tail(graph: RenderGraph) -> None: ...
