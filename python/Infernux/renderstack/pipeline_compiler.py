"""Lower declarative RenderPipeline definitions into executable RenderGraphs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from Infernux.renderstack.pipeline_dsl import (
    DomainDefinition,
    EffectStageDefinition,
    LayerDefinition,
    Path,
    PipelineDefinition,
    Queue,
    RouteDefinition,
    compile_queue_segments,
)


_CLEAR_TRANSPARENT = (0.0, 0.0, 0.0, 0.0)
_CLEAR_SCENE = (0.1, 0.1, 0.1, 1.0)


@dataclass
class _ImageAccumulator:
    """Ping-pong image used to combine isolated child image sets."""

    graph: object
    stable_id: str
    current: object
    alternate: object
    depth: object
    shadow_map: object | None
    composite_index: int = 0

    def composite(self, image, *, label: str) -> None:
        target = self.alternate
        with self.graph.name_scope(f"composite/{self.stable_id}"):
            with self.graph.add_pass(f"{self.composite_index:02d}_{label}") as render_pass:
                render_pass.set_texture("_BaseTex", self.current)
                render_pass.set_texture("_LayerTex", image)
                render_pass.write_color(target)
                render_pass.fullscreen_quad("route_alpha_composite")
        self.current, self.alternate = target, self.current
        self.composite_index += 1

    def effect_resources(self) -> dict:
        resources = {"color": self.current, "depth": self.depth}
        if self.shadow_map is not None:
            resources["shadow_map"] = self.shadow_map
        return resources


def compile_pipeline_definition(definition: PipelineDefinition, graph) -> None:
    """Compile one validated author definition into real graph resources.

    Route, layer, stage, and composite scopes are represented by distinct
    color images.  Geometry shares the frame depth texture, preserving
    occlusion while effects remain isolated to their declared image set.
    """
    from Infernux.rendergraph.graph import Format

    if not isinstance(definition, PipelineDefinition):
        raise TypeError("pipeline compiler requires a PipelineDefinition")
    if definition.lighting is not None and definition.lighting.clustered:
        raise NotImplementedError(
            "clustered lighting is not available until the true Forward+ "
            "lighting backend is implemented"
        )

    graph.set_msaa_samples(definition.frame.msaa)
    color_format = Format.RGBA16_SFLOAT if definition.frame.hdr else Format.RGBA8_UNORM
    camera_color = graph.create_texture("color", format=color_format, camera_target=True)
    scene_scratch = graph.create_texture("_scene_composite", format=color_format)
    depth = graph.create_texture("depth", format=Format.D32_SFLOAT)
    shadow_map = None
    if definition.shadows is not None and definition.shadows.enabled:
        shadow_map = graph.create_texture(
            "shadow_map",
            format=Format.D32_SFLOAT,
            size=(definition.shadows.resolution, definition.shadows.resolution),
        )

    with graph.add_pass("FrameClear") as render_pass:
        render_pass.write_color(camera_color)
        render_pass.write_depth(depth)
        render_pass.set_clear(color=_CLEAR_SCENE, depth=1.0)

    if shadow_map is not None:
        with graph.add_pass("ShadowCasterPass") as render_pass:
            render_pass.write_depth(shadow_map)
            render_pass.set_clear(depth=1.0)
            render_pass.draw_shadow_casters(queue_range=(0, 2999))

    scene = _ImageAccumulator(
        graph,
        "scene",
        camera_color,
        scene_scratch,
        depth,
        shadow_map,
    )
    domains = {domain.domain_id: domain for domain in definition.domains}
    stages = {stage.stable_id: stage for stage in definition.effect_stages}

    for operation, stable_id in definition.operations:
        if operation in {"frame", "shadows", "lighting"}:
            continue
        if operation == "domain":
            domain_image = _compile_domain(
                definition,
                domains[stable_id],
                graph,
                color_format,
                depth,
                shadow_map,
                stages,
            )
            scene.composite(domain_image, label=stable_id)
            continue
        if operation == "effect":
            _declare_effect(graph, stages[stable_id], scene.effect_resources())
            continue
        if operation == "sky":
            with graph.add_pass("SkyboxPass") as render_pass:
                render_pass.read(depth)
                render_pass.write_color(scene.current)
                render_pass.draw_skybox()
            continue
        if operation == "screen_ui":
            _commit_scene_to_camera(graph, scene, camera_color)
            graph.screen_ui_section(resources={"color"})
            continue
        raise ValueError(f"unknown pipeline operation: {operation!r}")

    _commit_scene_to_camera(graph, scene, camera_color)
    graph.set_output(camera_color)


def _compile_domain(
    definition: PipelineDefinition,
    domain: DomainDefinition,
    graph,
    color_format,
    depth,
    shadow_map,
    stages: dict[str, EffectStageDefinition],
):
    routes = {route.route_id: route for route in domain.all_routes()}
    otherwise = next((route for route in routes.values() if route.is_otherwise), None)
    segments = compile_queue_segments(
        domain.queue,
        routes.values(),
        include_otherwise=otherwise is not None,
    )
    selectors: dict[str, list[Queue]] = {route_id: [] for route_id in routes}
    for segment in segments:
        route_id = otherwise.route_id if segment.is_otherwise and otherwise else segment.route_id
        if route_id is not None:
            selectors[route_id].append(segment.selector)

    accumulator = _new_transparent_accumulator(
        graph,
        f"domain/{domain.domain_id}",
        color_format,
        depth,
        shadow_map,
    )
    layers = {layer.layer_id: layer for layer in domain.layers}

    for operation, stable_id in domain.operations:
        if operation == "route":
            route = routes[stable_id]
            image = _compile_route(
                route,
                selectors[stable_id],
                graph,
                color_format,
                depth,
                shadow_map,
                stages,
            )
            accumulator.composite(image, label=stable_id)
            continue
        if operation == "layer":
            image = _compile_layer(
                definition,
                domain,
                layers[stable_id],
                routes,
                selectors,
                graph,
                color_format,
                depth,
                shadow_map,
                stages,
            )
            accumulator.composite(image, label=stable_id)
            continue
        if operation == "effect":
            _declare_effect(graph, stages[stable_id], accumulator.effect_resources())
            continue
        raise ValueError(f"unknown {domain.domain_id} operation: {operation!r}")
    return accumulator.current


def _compile_layer(
    definition: PipelineDefinition,
    domain: DomainDefinition,
    layer: LayerDefinition,
    routes: dict[str, RouteDefinition],
    selectors: dict[str, list[Queue]],
    graph,
    color_format,
    depth,
    shadow_map,
    stages: dict[str, EffectStageDefinition],
):
    del definition, domain
    accumulator = _new_transparent_accumulator(
        graph,
        f"layer/{layer.layer_id}",
        color_format,
        depth,
        shadow_map,
    )
    for operation, stable_id in layer.operations:
        if operation == "route":
            image = _compile_route(
                routes[stable_id],
                selectors[stable_id],
                graph,
                color_format,
                depth,
                shadow_map,
                stages,
            )
            accumulator.composite(image, label=stable_id)
            continue
        if operation == "effect":
            _declare_effect(graph, stages[stable_id], accumulator.effect_resources())
            continue
        raise ValueError(f"unknown {layer.layer_id} operation: {operation!r}")
    return accumulator.current


def _compile_route(
    route: RouteDefinition,
    selectors: Iterable[Queue],
    graph,
    color_format,
    depth,
    shadow_map,
    stages: dict[str, EffectStageDefinition],
):
    if route.path is not Path.FORWARD:
        raise NotImplementedError(
            f"{route.path.value} route {route.route_id!r} is not available until its "
            "true lighting backend is implemented; ordinary Forward is not used as a silent fallback"
        )

    with graph.name_scope(f"route/{route.route_id}"):
        route_color = graph.create_texture("color", format=color_format)
        with graph.add_pass("Clear") as render_pass:
            render_pass.write_color(route_color)
            render_pass.set_clear(color=_CLEAR_TRANSPARENT)

        sort_mode = "back_to_front" if route.domain == "transparent" else "front_to_back"
        for index, selector in enumerate(selectors):
            with graph.add_pass(f"Draw_{index:02d}_{selector.minimum}_{selector.maximum}") as render_pass:
                render_pass.write_color(route_color)
                render_pass.write_depth(depth)
                if shadow_map is not None:
                    render_pass.set_texture("shadowMap", shadow_map)
                render_pass.draw_renderers(
                    queue_range=selector.as_tuple(),
                    sort_mode=sort_mode,
                    material_pass="forward",
                )

    resources = {"color": route_color, "depth": depth}
    if shadow_map is not None:
        resources["shadow_map"] = shadow_map
    for stage in route.effects:
        _declare_effect(graph, stages[stage.stable_id], resources)
    return route_color


def _new_transparent_accumulator(
    graph,
    stable_id: str,
    color_format,
    depth,
    shadow_map,
) -> _ImageAccumulator:
    with graph.name_scope(stable_id):
        current = graph.create_texture("composite_a", format=color_format)
        alternate = graph.create_texture("composite_b", format=color_format)
        with graph.add_pass("Clear") as render_pass:
            render_pass.write_color(current)
            render_pass.set_clear(color=_CLEAR_TRANSPARENT)
    return _ImageAccumulator(
        graph,
        stable_id,
        current,
        alternate,
        depth,
        shadow_map,
    )


def _declare_effect(graph, stage: EffectStageDefinition, resources: dict) -> None:
    with graph.effect_resources(resources):
        graph.effects(
            stage.stable_id,
            scope=stage.scope,
            display_name=stage.display_name,
            inputs=set(resources),
            outputs={"color"},
            capabilities={"fullscreen", "isolated_image_set"},
        )


def _commit_scene_to_camera(graph, scene: _ImageAccumulator, camera_color) -> None:
    if scene.current is camera_color:
        return
    previous_current = scene.current
    with graph.add_pass("CommitSceneColor") as render_pass:
        render_pass.set_texture("_SourceTex", previous_current)
        render_pass.write_color(camera_color)
        render_pass.fullscreen_quad("fullscreen_blit")
    scene.current = camera_color
    scene.alternate = previous_current


__all__ = ["compile_pipeline_definition"]
