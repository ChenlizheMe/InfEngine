"""Lower declarative RenderPipeline definitions into executable RenderGraphs."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
from Infernux.renderstack.route_policy import RoutePolicy
from Infernux.renderstack._pipeline_common import DEFERRED_LIGHTING_SHADER


_CLEAR_TRANSPARENT = (0.0, 0.0, 0.0, 0.0)
_CLEAR_SCENE = (0.0, 0.0, 0.0, 0.0)
_CLEAR_SKY_FALLBACK = (0.1, 0.1, 0.1, 1.0)


@dataclass
class _ImageAccumulator:
    """Ping-pong image used to combine isolated child image sets."""

    graph: object
    stable_id: str
    current: object
    alternate: object
    depth: object
    motion: object
    shadow_map: object | None
    composite_index: int = 0

    def composite(self, image, *, label: str) -> None:
        target = self.alternate
        with self.graph.name_scope(f"composite/{self.stable_id}"):
            with self.graph.add_pass(f"{self.composite_index:02d}_{label}") as render_pass:
                render_pass.set_texture("_BaseTex", self.current)
                render_pass.set_texture("_LayerTex", image)
                render_pass.write_color(target)
                render_pass.fullscreen_quad("Route Alpha Composite")
        self.current, self.alternate = target, self.current
        self.composite_index += 1

    def add(self, image, *, label: str) -> None:
        target = self.alternate
        with self.graph.name_scope(f"additive/{self.stable_id}"):
            with self.graph.add_pass(f"{self.composite_index:02d}_{label}") as render_pass:
                render_pass.set_texture("_BaseTex", self.current)
                render_pass.set_texture("_AdditiveTex", image)
                render_pass.write_color(target)
                render_pass.fullscreen_quad("Route Additive Composite")
        self.current, self.alternate = target, self.current
        self.composite_index += 1

    def under(self, image, *, label: str) -> None:
        """Place an image behind the accumulated premultiplied foreground."""
        target = self.alternate
        with self.graph.name_scope(f"under/{self.stable_id}"):
            with self.graph.add_pass(f"{self.composite_index:02d}_{label}") as render_pass:
                render_pass.set_texture("_BaseTex", image)
                render_pass.set_texture("_LayerTex", self.current)
                render_pass.write_color(target)
                render_pass.fullscreen_quad("Route Alpha Composite")
        self.current, self.alternate = target, self.current
        self.composite_index += 1

    def effect_resources(self) -> dict:
        resources = {"color": self.current, "depth": self.depth, "motion": self.motion}
        if self.shadow_map is not None:
            resources["shadow_map"] = self.shadow_map
        return resources


@dataclass(frozen=True)
class _RouteContribution:
    color: object | None = None
    additive: object | None = None
    deferred_overlay: bool = True


@dataclass(frozen=True)
class _ScopeImage:
    """A scope's ordinary image plus effect pixels that must stay on top.

    Isolated route effects can create color outside their source geometry.  A
    later opaque draw has no depth at those pixels and would overwrite that
    overflow if it were committed immediately.  Keeping those contributions
    separate until the scope's ordinary geometry is complete gives them the
    same useful semantics as a local post-process layer.
    """

    base: object
    overlays: tuple[_RouteContribution, ...] = ()


def _consume_route_contribution(
    accumulator: _ImageAccumulator,
    contribution: _RouteContribution | None,
    *,
    label: str,
) -> None:
    if contribution is None:
        return
    if contribution.color is not None:
        accumulator.composite(contribution.color, label=label)
    if contribution.additive is not None:
        accumulator.add(contribution.additive, label=f"{label}_additive")


def _flush_route_contributions(
    accumulator: _ImageAccumulator,
    contributions: list[tuple[str, _RouteContribution]],
) -> None:
    for label, contribution in contributions:
        _consume_route_contribution(accumulator, contribution, label=label)
    contributions.clear()


def compile_pipeline_definition(definition: PipelineDefinition, graph) -> None:
    """Compile one validated author definition into real graph resources.

    Route, layer, stage, and composite scopes are represented by distinct
    color images.  Geometry shares the frame depth texture, preserving
    occlusion while effects remain isolated to their declared image set.
    """
    from Infernux.rendergraph.graph import Format

    if not isinstance(definition, PipelineDefinition):
        raise TypeError("pipeline compiler requires a PipelineDefinition")
    graph.set_msaa_samples(definition.frame.msaa)
    color_format = Format.RGBA16_SFLOAT if definition.frame.hdr else Format.RGBA8_UNORM
    camera_color = graph.create_texture("color", format=color_format, camera_target=True)
    scene_scratch = graph.create_texture("_scene_composite", format=color_format)
    depth = graph.create_texture("depth", format=Format.D32_SFLOAT)
    motion = graph.create_texture("motion", format=Format.RG16_SFLOAT, samples=1)
    motion_draw = motion
    if definition.frame.msaa > 1:
        motion_draw = graph.create_texture(
            "_motion_msaa",
            format=Format.RG16_SFLOAT,
            samples=definition.frame.msaa,
        )
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

    with graph.add_pass("MotionClear") as render_pass:
        render_pass.write_color(motion_draw)
        if motion_draw is not motion:
            render_pass.write_resolve(motion)
        render_pass.set_clear(color=(0.0, 0.0, 0.0, 0.0))

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
        motion,
        shadow_map,
    )
    pending_scene_overlays: list[tuple[str, _RouteContribution]] = []
    domains = {domain.domain_id: domain for domain in definition.domains}
    stages = {stage.stable_id: stage for stage in definition.effect_stages}

    for operation, stable_id in definition.operations:
        if operation in {"frame", "shadows", "lighting"}:
            continue
        if operation == "domain":
            domain = domains[stable_id]
            # Transparent geometry is authored after opaque scene effects and
            # must blend over them, so finish any deferred opaque overflow
            # before entering the transparent domain.
            if domain.domain_id == "transparent":
                _flush_route_contributions(scene, pending_scene_overlays)
            domain_image = _compile_domain(
                definition,
                domain,
                graph,
                color_format,
                depth,
                motion,
                motion_draw,
                shadow_map,
                stages,
                definition.frame.msaa,
            )
            scene.composite(domain_image.base, label=stable_id)
            pending_scene_overlays.extend(
                (f"{stable_id}_{index:02d}", contribution)
                for index, contribution in enumerate(domain_image.overlays)
            )
            continue
        if operation == "effect":
            stage = stages[stable_id]
            if _effect_stage_is_active(graph, stage):
                _flush_route_contributions(scene, pending_scene_overlays)
            _declare_effect(graph, stage, scene.effect_resources())
            continue
        if operation == "sky":
            _compile_sky(
                graph,
                scene,
                color_format,
                definition.frame.msaa,
            )
            _flush_route_contributions(scene, pending_scene_overlays)
            continue
        if operation == "screen_ui":
            _flush_route_contributions(scene, pending_scene_overlays)
            _commit_scene_to_camera(graph, scene, camera_color)
            graph.screen_ui_section(resources={"color", "depth", "motion"})
            continue
        raise ValueError(f"unknown pipeline operation: {operation!r}")

    _flush_route_contributions(scene, pending_scene_overlays)
    _commit_scene_to_camera(graph, scene, camera_color)
    graph.set_output(camera_color)


def _compile_domain(
    definition: PipelineDefinition,
    domain: DomainDefinition,
    graph,
    color_format,
    depth,
    motion,
    motion_draw,
    shadow_map,
    stages: dict[str, EffectStageDefinition],
    msaa_samples: int,
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
        motion,
        shadow_map,
    )
    pending_overlays: list[tuple[str, _RouteContribution]] = []
    layers = {layer.layer_id: layer for layer in domain.layers}

    for operation, stable_id in domain.operations:
        if operation == "route":
            route = routes[stable_id]
            contribution = _compile_route(
                route,
                selectors[stable_id],
                graph,
                color_format,
                depth,
                motion,
                motion_draw,
                shadow_map,
                stages,
                inline_target=accumulator.current,
                msaa_samples=msaa_samples,
            )
            if contribution is not None:
                if contribution.deferred_overlay:
                    pending_overlays.append((stable_id, contribution))
                else:
                    _consume_route_contribution(
                        accumulator,
                        contribution,
                        label=stable_id,
                    )
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
                motion,
                motion_draw,
                shadow_map,
                stages,
                msaa_samples,
            )
            accumulator.composite(image.base, label=stable_id)
            pending_overlays.extend(
                (f"{stable_id}_{index:02d}", contribution)
                for index, contribution in enumerate(image.overlays)
            )
            continue
        if operation == "effect":
            stage = stages[stable_id]
            if _effect_stage_is_active(graph, stage):
                _flush_route_contributions(accumulator, pending_overlays)
            _declare_effect(graph, stage, accumulator.effect_resources())
            continue
        raise ValueError(f"unknown {domain.domain_id} operation: {operation!r}")
    return _ScopeImage(
        base=accumulator.current,
        overlays=tuple(contribution for _, contribution in pending_overlays),
    )


def _compile_layer(
    definition: PipelineDefinition,
    domain: DomainDefinition,
    layer: LayerDefinition,
    routes: dict[str, RouteDefinition],
    selectors: dict[str, list[Queue]],
    graph,
    color_format,
    depth,
    motion,
    motion_draw,
    shadow_map,
    stages: dict[str, EffectStageDefinition],
    msaa_samples: int,
):
    del definition, domain
    accumulator = _new_transparent_accumulator(
        graph,
        f"layer/{layer.layer_id}",
        color_format,
        depth,
        motion,
        shadow_map,
    )
    pending_overlays: list[tuple[str, _RouteContribution]] = []
    for operation, stable_id in layer.operations:
        if operation == "route":
            contribution = _compile_route(
                routes[stable_id],
                selectors[stable_id],
                graph,
                color_format,
                depth,
                motion,
                motion_draw,
                shadow_map,
                stages,
                inline_target=accumulator.current,
                msaa_samples=msaa_samples,
            )
            if contribution is not None:
                if contribution.deferred_overlay:
                    pending_overlays.append((stable_id, contribution))
                else:
                    _consume_route_contribution(
                        accumulator,
                        contribution,
                        label=stable_id,
                    )
            continue
        if operation == "effect":
            stage = stages[stable_id]
            if _effect_stage_is_active(graph, stage):
                _flush_route_contributions(accumulator, pending_overlays)
            _declare_effect(graph, stage, accumulator.effect_resources())
            continue
        raise ValueError(f"unknown {layer.layer_id} operation: {operation!r}")
    return _ScopeImage(
        base=accumulator.current,
        overlays=tuple(contribution for _, contribution in pending_overlays),
    )


def _compile_route(
    route: RouteDefinition,
    selectors: Iterable[Queue],
    graph,
    color_format,
    depth,
    motion,
    motion_draw,
    shadow_map,
    stages: dict[str, EffectStageDefinition],
    *,
    inline_target,
    msaa_samples: int,
):
    if route.path is Path.DEFERRED and msaa_samples > 1:
        if route.fallback not in {Path.FORWARD, Path.FORWARD_PLUS}:
            raise ValueError(
                f"deferred route {route.route_id!r} cannot use {msaa_samples}x MSAA; "
                "declare an explicit Forward or Forward+ fallback"
            )
        return _compile_route(
            replace(route, path=route.fallback, fallback=None),
            selectors,
            graph,
            color_format,
            depth,
            motion,
            motion_draw,
            shadow_map,
            stages,
            inline_target=inline_target,
            msaa_samples=msaa_samples,
        )
    if route.path not in {Path.FORWARD, Path.FORWARD_PLUS, Path.DEFERRED}:
        raise ValueError(f"unsupported render path {route.path.value!r}")

    route_stages = tuple(stages[stage.stable_id] for stage in route.effects)
    policy = graph.resolve_effect_route_policy(route_stages)
    if policy is RoutePolicy.CUSTOM_FEATURE:
        raise NotImplementedError(
            f"custom route policy for {route.route_id!r} requires a registered compiler"
        )

    if route.path is Path.DEFERRED:
        route_color = _draw_deferred_route(
            route,
            selectors,
            graph,
            color_format,
            depth,
            motion,
            motion_draw,
        )
        original_color = None
        if policy is RoutePolicy.ADDITIVE_EXTRACT:
            with graph.name_scope(f"route/{route.route_id}"):
                original_color = graph.create_texture("original", format=color_format)
                with graph.add_pass("PreserveOriginal") as render_pass:
                    render_pass.set_texture("_SourceTex", route_color)
                    render_pass.write_color(original_color)
                    render_pass.fullscreen_quad("Fullscreen Blit")

        resources = _effect_resources(route_color, depth, motion, shadow_map)
        for stage in route_stages:
            _declare_effect(graph, stage, resources, policy=policy)

        if policy is not RoutePolicy.ADDITIVE_EXTRACT:
            return _RouteContribution(
                color=route_color,
                deferred_overlay=policy is not RoutePolicy.INLINE,
            )

        with graph.name_scope(f"route/{route.route_id}"):
            additive = graph.create_texture("additive_delta", format=color_format)
            with graph.add_pass("ExtractAdditiveDelta") as render_pass:
                render_pass.set_texture("_OriginalTex", original_color)
                render_pass.set_texture("_ProcessedTex", route_color)
                render_pass.write_color(additive)
                render_pass.fullscreen_quad("Route Additive Delta")
        return _RouteContribution(color=original_color, additive=additive)

    if policy is RoutePolicy.INLINE and msaa_samples == 1:
        _draw_route(
            route,
            selectors,
            graph,
            inline_target,
            depth,
            motion,
            motion_draw,
            shadow_map,
        )
        resources = _effect_resources(inline_target, depth, motion, shadow_map)
        for stage in route_stages:
            _declare_effect(graph, stage, resources, policy=policy)
        return None

    with graph.name_scope(f"route/{route.route_id}"):
        route_color = graph.create_texture("color", format=color_format, samples=1)
        draw_color = route_color
        if msaa_samples > 1:
            draw_color = graph.create_texture(
                "color_msaa",
                format=color_format,
                samples=msaa_samples,
            )
        with graph.add_pass("Clear") as render_pass:
            render_pass.write_color(draw_color)
            if draw_color is not route_color:
                render_pass.write_resolve(route_color)
            render_pass.set_clear(color=_CLEAR_TRANSPARENT)

    _draw_route(
        route,
        selectors,
        graph,
        draw_color,
        depth,
        motion,
        motion_draw,
        shadow_map,
        resolve=route_color if draw_color is not route_color else None,
    )

    original_color = None
    if policy is RoutePolicy.ADDITIVE_EXTRACT:
        with graph.name_scope(f"route/{route.route_id}"):
            original_color = graph.create_texture("original", format=color_format)
            with graph.add_pass("PreserveOriginal") as render_pass:
                render_pass.set_texture("_SourceTex", route_color)
                render_pass.write_color(original_color)
                render_pass.fullscreen_quad("Fullscreen Blit")

    resources = _effect_resources(route_color, depth, motion, shadow_map)
    for stage in route_stages:
        _declare_effect(graph, stage, resources, policy=policy)

    if policy is not RoutePolicy.ADDITIVE_EXTRACT:
        # MSAA forces even an ordinary inline route through an isolated resolve
        # target. That is only a storage detail: it must join the scope's base
        # image immediately, otherwise it is deferred alongside real effect
        # overflow and can cover blur/glitch pixels composited later.
        return _RouteContribution(
            color=route_color,
            deferred_overlay=policy is not RoutePolicy.INLINE,
        )

    with graph.name_scope(f"route/{route.route_id}"):
        additive = graph.create_texture("additive_delta", format=color_format)
        with graph.add_pass("ExtractAdditiveDelta") as render_pass:
            render_pass.set_texture("_OriginalTex", original_color)
            render_pass.set_texture("_ProcessedTex", route_color)
            render_pass.write_color(additive)
            render_pass.fullscreen_quad("Route Additive Delta")
    return _RouteContribution(color=original_color, additive=additive)


def _draw_deferred_route(
    route: RouteDefinition,
    selectors: Iterable[Queue],
    graph,
    color_format,
    depth,
    motion,
    motion_draw,
):
    """Rasterize one opaque route into the canonical GBuffer and light it."""
    from Infernux.rendergraph.graph import Format

    if route.domain != "opaque":
        raise ValueError(
            f"deferred route {route.route_id!r} belongs to {route.domain!r}; "
            "transparent geometry must use Forward or Forward+"
        )

    with graph.name_scope(f"route/{route.route_id}/deferred"):
        albedo = graph.create_texture("albedo", format=Format.RGBA16_SFLOAT)
        normal = graph.create_texture("normal", format=Format.RGBA16_SFLOAT)
        material = graph.create_texture("material", format=Format.RGBA8_UNORM)
        emission = graph.create_texture("emission", format=Format.RGBA16_SFLOAT)
        object_data = graph.create_texture("object", format=Format.RG32_UINT)
        lit = graph.create_texture("lit", format=color_format)

        with graph.add_pass("Clear") as clear_pass:
            clear_pass.write_color(albedo, slot=0)
            clear_pass.write_color(normal, slot=1)
            clear_pass.write_color(material, slot=2)
            clear_pass.write_color(emission, slot=3)
            clear_pass.write_color(object_data, slot=4)
            clear_pass.set_clear(color=_CLEAR_TRANSPARENT)

        for index, selector in enumerate(selectors):
            with graph.add_pass(
                f"Geometry_{index:02d}_{selector.minimum}_{selector.maximum}"
            ) as geometry_pass:
                geometry_pass.write_color(albedo, slot=0)
                geometry_pass.write_color(normal, slot=1)
                geometry_pass.write_color(material, slot=2)
                geometry_pass.write_color(emission, slot=3)
                geometry_pass.write_color(object_data, slot=4)
                geometry_pass.write_depth(depth)
                geometry_pass.draw_renderers(
                    queue_range=selector.as_tuple(),
                    sort_mode="front_to_back",
                    material_pass="gbuffer",
                )

            with graph.add_pass(
                f"Motion_{index:02d}_{selector.minimum}_{selector.maximum}"
            ) as motion_pass:
                motion_pass.read(depth)
                motion_pass.write_color(motion_draw)
                if motion_draw is not motion:
                    motion_pass.write_resolve(motion)
                motion_pass.draw_renderers(
                    queue_range=selector.as_tuple(),
                    sort_mode="front_to_back",
                    material_pass="motion",
                )

        with graph.add_pass("Lighting") as lighting_pass:
            lighting_pass.set_textures(
                {
                    "gAlbedo": albedo,
                    "gNormal": normal,
                    "gMaterial": material,
                    "gEmission": emission,
                    "gObject": object_data,
                    "sceneDepth": depth,
                }
            )
            lighting_pass.write_color(lit)
            lighting_pass.set_clear(color=_CLEAR_TRANSPARENT)
            lighting_pass.fullscreen_quad(DEFERRED_LIGHTING_SHADER)

    return lit


def _draw_route(
    route: RouteDefinition,
    selectors: Iterable[Queue],
    graph,
    color,
    depth,
    motion,
    motion_draw,
    shadow_map,
    *,
    resolve=None,
) -> None:
    sort_mode = "back_to_front" if route.domain == "transparent" else "front_to_back"
    with graph.name_scope(f"route/{route.route_id}"):
        for index, selector in enumerate(selectors):
            with graph.add_pass(
                f"Draw_{index:02d}_{selector.minimum}_{selector.maximum}"
            ) as render_pass:
                render_pass.write_color(color)
                if route.domain == "transparent":
                    # Transparent geometry depth-tests against the opaque scene
                    # without mutating it.  Keeping the attachment read-only also
                    # makes the same depth image available to soft-particle
                    # shaders through the RenderGraph sampled-depth contract.
                    render_pass.read(depth)
                else:
                    render_pass.write_depth(depth)
                if resolve is not None:
                    render_pass.write_resolve(resolve)
                if shadow_map is not None:
                    render_pass.set_texture("shadowMap", shadow_map)
                render_pass.draw_renderers(
                    queue_range=selector.as_tuple(),
                    sort_mode=sort_mode,
                    material_pass=route.path.value,
                )
            with graph.add_pass(
                f"Motion_{index:02d}_{selector.minimum}_{selector.maximum}"
            ) as motion_pass:
                motion_pass.read(depth)
                motion_pass.write_color(motion_draw)
                if motion_draw is not motion:
                    motion_pass.write_resolve(motion)
                motion_pass.draw_renderers(
                    queue_range=selector.as_tuple(),
                    sort_mode=sort_mode,
                    material_pass="motion",
                )


def _compile_sky(
    graph,
    scene: _ImageAccumulator,
    color_format,
    msaa_samples: int,
) -> None:
    with graph.name_scope("sky"):
        sky_resolved = graph.create_texture("color", format=color_format, samples=1)
        sky_target = sky_resolved
        if msaa_samples > 1:
            sky_target = graph.create_texture(
                "color_msaa",
                format=color_format,
                samples=msaa_samples,
            )
        with graph.add_pass("SkyboxPass") as render_pass:
            render_pass.read(scene.depth)
            render_pass.write_color(sky_target)
            if sky_target is not sky_resolved:
                render_pass.write_resolve(sky_resolved)
            render_pass.set_clear(color=_CLEAR_SKY_FALLBACK)
            render_pass.draw_skybox()
    # Sky is a background. Compositing it underneath preserves route effects
    # whose alpha coverage expands beyond geometry depth, such as blur/bloom.
    scene.under(sky_resolved, label="sky")


def _effect_resources(color, depth, motion, shadow_map) -> dict:
    resources = {"color": color, "depth": depth, "motion": motion}
    if shadow_map is not None:
        resources["shadow_map"] = shadow_map
    return resources


def _new_transparent_accumulator(
    graph,
    stable_id: str,
    color_format,
    depth,
    motion,
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
        motion,
        shadow_map,
    )


def _declare_effect(
    graph,
    stage: EffectStageDefinition,
    resources: dict,
    *,
    policy: RoutePolicy | None = None,
) -> None:
    capabilities = {"fullscreen", "isolated_image_set"}
    if policy is not None:
        capabilities.add(f"route_policy:{policy.value}")
    with graph.effect_resources(resources):
        graph.effects(
            stage.stable_id,
            scope=stage.scope,
            display_name=stage.display_name,
            inputs=set(resources),
            outputs={"color"},
            capabilities=capabilities,
        )


def _effect_stage_is_active(graph, stage: EffectStageDefinition) -> bool:
    return graph.is_effect_stage_active(stage)


def _commit_scene_to_camera(graph, scene: _ImageAccumulator, camera_color) -> None:
    if scene.current is camera_color:
        return
    previous_current = scene.current
    with graph.add_pass("CommitSceneColor") as render_pass:
        render_pass.set_texture("_SourceTex", previous_current)
        render_pass.write_color(camera_color)
        render_pass.fullscreen_quad("Fullscreen Blit")
    scene.current = camera_color
    scene.alternate = previous_current


__all__ = ["compile_pipeline_definition"]
