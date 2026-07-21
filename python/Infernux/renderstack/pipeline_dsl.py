"""Typed authoring IR for the declarative RenderPipeline API.

This layer records user intent without allocating RenderGraph resources.  The
backend compiler can therefore validate queue ownership and effect scope before
it creates route targets or Vulkan work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from Infernux.renderstack.effect_stage import EffectScope, validate_effect_stage_id


MATERIAL_QUEUE_MIN = 0
MATERIAL_QUEUE_MAX = 9999
OPAQUE_QUEUE = (0, 2500)
TRANSPARENT_QUEUE = (2501, 5000)


class Path(str, Enum):
    FORWARD = "forward"
    FORWARD_PLUS = "forward_plus"
    DEFERRED = "deferred"


@dataclass(frozen=True, order=True, init=False)
class Queue:
    """Inclusive Material Render Queue interval."""

    minimum: int
    maximum: int

    def __init__(self, minimum: int, maximum: Optional[int] = None):
        low = _queue_value(minimum)
        high = low if maximum is None else _queue_value(maximum)
        if low > high:
            raise ValueError("queue minimum cannot be greater than queue maximum")
        object.__setattr__(self, "minimum", low)
        object.__setattr__(self, "maximum", high)

    @classmethod
    def exact(cls, value: int) -> "Queue":
        return cls(value)

    @classmethod
    def range(cls, minimum: int, maximum: int) -> "Queue":
        return cls(minimum, maximum)

    @classmethod
    def named(cls, name: str) -> "Queue":
        normalized = str(name or "").strip().lower().replace(" ", "_")
        presets = {
            "opaque": OPAQUE_QUEUE,
            "all_opaque": OPAQUE_QUEUE,
            "transparent": TRANSPARENT_QUEUE,
            "all_transparent": TRANSPARENT_QUEUE,
            "all": (MATERIAL_QUEUE_MIN, MATERIAL_QUEUE_MAX),
        }
        try:
            return cls(*presets[normalized])
        except KeyError as exc:
            raise ValueError(f"unknown render queue preset: {name!r}") from exc

    @classmethod
    def all_opaque(cls) -> "Queue":
        return cls(*OPAQUE_QUEUE)

    @classmethod
    def all_transparent(cls) -> "Queue":
        return cls(*TRANSPARENT_QUEUE)

    def contains(self, other: "Queue") -> bool:
        return self.minimum <= other.minimum and other.maximum <= self.maximum

    def overlaps(self, other: "Queue") -> bool:
        return self.minimum <= other.maximum and other.minimum <= self.maximum

    def as_tuple(self) -> tuple[int, int]:
        return self.minimum, self.maximum


# QueueSelector was the research name; Queue is the final public spelling.
QueueSelector = Queue


@dataclass(frozen=True)
class EffectStageDefinition:
    stable_id: str
    scope: EffectScope
    owner_id: str
    display_name: str = ""


@dataclass
class RouteDefinition:
    route_id: str
    domain: str
    selector: Optional[Queue]
    path: Path
    fallback: Optional[Path] = None
    layer_id: str = ""
    is_otherwise: bool = False
    effects: list[EffectStageDefinition] = field(default_factory=list)


@dataclass
class LayerDefinition:
    layer_id: str
    display_name: str
    routes: list[RouteDefinition] = field(default_factory=list)
    effects: list[EffectStageDefinition] = field(default_factory=list)
    operations: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DomainDefinition:
    domain_id: str
    queue: Queue
    routes: list[RouteDefinition] = field(default_factory=list)
    layers: list[LayerDefinition] = field(default_factory=list)
    effects: list[EffectStageDefinition] = field(default_factory=list)
    operations: list[tuple[str, str]] = field(default_factory=list)

    def all_routes(self) -> tuple[RouteDefinition, ...]:
        routes = list(self.routes)
        for layer in self.layers:
            routes.extend(layer.routes)
        return tuple(routes)


@dataclass(frozen=True)
class FrameDefinition:
    hdr: bool = True
    msaa: int = 1


@dataclass(frozen=True)
class ShadowDefinition:
    enabled: bool = True
    resolution: int = 4096


@dataclass(frozen=True)
class LightingDefinition:
    clustered: bool = False


@dataclass
class PipelineDefinition:
    frame: FrameDefinition = field(default_factory=FrameDefinition)
    shadows: Optional[ShadowDefinition] = None
    lighting: Optional[LightingDefinition] = None
    domains: list[DomainDefinition] = field(default_factory=list)
    effects: list[EffectStageDefinition] = field(default_factory=list)
    effect_order: list[EffectStageDefinition] = field(default_factory=list)
    operations: list[tuple[str, str]] = field(default_factory=list)
    has_sky: bool = False
    has_screen_ui: bool = False

    @property
    def effect_stages(self) -> tuple[EffectStageDefinition, ...]:
        return tuple(self.effect_order)

    def domain(self, domain_id: str) -> DomainDefinition:
        for domain in self.domains:
            if domain.domain_id == domain_id:
                return domain
        raise KeyError(domain_id)


@dataclass(frozen=True)
class QueueRoute:
    route_id: str
    selector: Queue


@dataclass(frozen=True)
class QueueSegment:
    selector: Queue
    route_id: Optional[str]
    is_otherwise: bool


def compile_queue_segments(
    base: Queue,
    routes: Iterable[QueueRoute | RouteDefinition],
    *,
    include_otherwise: bool,
) -> tuple[QueueSegment, ...]:
    """Split a domain into mutually exclusive explicit and remainder ranges."""

    claimed: list[tuple[str, Queue]] = []
    for route in routes:
        if getattr(route, "is_otherwise", False):
            continue
        selector = route.selector
        if selector is None:
            selector = base
        if not base.contains(selector):
            raise ValueError(
                f"route {route.route_id!r} queue {selector.as_tuple()} is outside "
                f"the {base.as_tuple()} domain"
            )
        claimed.append((route.route_id, selector))

    claimed.sort(key=lambda item: (item[1].minimum, item[1].maximum, item[0]))
    previous: Optional[tuple[str, Queue]] = None
    for current in claimed:
        if previous is not None and previous[1].overlaps(current[1]):
            raise ValueError(
                f"queue routes {previous[0]!r} {previous[1].as_tuple()} and "
                f"{current[0]!r} {current[1].as_tuple()} overlap"
            )
        previous = current

    segments: list[QueueSegment] = []
    cursor = base.minimum
    for route_id, selector in claimed:
        if include_otherwise and cursor < selector.minimum:
            segments.append(QueueSegment(Queue(cursor, selector.minimum - 1), None, True))
        segments.append(QueueSegment(selector, route_id, False))
        cursor = selector.maximum + 1
    if include_otherwise and cursor <= base.maximum:
        segments.append(QueueSegment(Queue(cursor, base.maximum), None, True))
    return tuple(segments)


class PipelineBuilder:
    """Record the public low-nesting Python syntax into :class:`PipelineDefinition`."""

    def __init__(self):
        self.definition = PipelineDefinition()
        self._domain_ids: set[str] = set()
        self._effect_ids: set[str] = set()
        self._route_counter = 0
        self._layer_counter = 0

    def frame(self, *, hdr: bool = True, msaa: int = 1) -> "PipelineBuilder":
        samples = int(msaa)
        if samples not in {1, 2, 4, 8}:
            raise ValueError("MSAA samples must be one of 1, 2, 4, or 8")
        self.definition.frame = FrameDefinition(bool(hdr), samples)
        self.definition.operations.append(("frame", ""))
        return self

    def shadows(self, *, resolution: int = 4096) -> "PipelineBuilder":
        size = int(resolution)
        if size <= 0:
            raise ValueError("shadow resolution must be positive")
        self.definition.shadows = ShadowDefinition(True, size)
        self.definition.operations.append(("shadows", ""))
        return self

    def lighting(self, *, clustered: bool = False) -> "PipelineBuilder":
        self.definition.lighting = LightingDefinition(bool(clustered))
        self.definition.operations.append(("lighting", ""))
        return self

    def opaque(self) -> "DomainBuilder":
        return self._domain("opaque", Queue.all_opaque())

    def transparent(self) -> "DomainBuilder":
        return self._domain("transparent", Queue.all_transparent())

    def sky(self) -> "PipelineBuilder":
        if self.definition.has_sky:
            raise ValueError("a pipeline can declare sky only once")
        self.definition.has_sky = True
        self.definition.operations.append(("sky", ""))
        return self

    def effects(self, stable_id: str, *, label: str = "") -> EffectStageDefinition:
        stage = self._effect(stable_id, EffectScope.COMPOSITE, "pipeline", label)
        self.definition.effects.append(stage)
        self.definition.operations.append(("effect", stage.stable_id))
        return stage

    def screen_ui(self) -> "PipelineBuilder":
        if self.definition.has_screen_ui:
            raise ValueError("a pipeline can declare screen UI only once")
        self.definition.has_screen_ui = True
        self.definition.operations.append(("screen_ui", ""))
        return self

    def build(self) -> PipelineDefinition:
        screen_ui_index = next(
            (
                index
                for index, operation in enumerate(self.definition.operations)
                if operation[0] == "screen_ui"
            ),
            -1,
        )
        if screen_ui_index >= 0 and screen_ui_index != len(self.definition.operations) - 1:
            raise ValueError("screen_ui must be the final pipeline operation")
        for domain in self.definition.domains:
            routes = domain.all_routes()
            otherwise = [route for route in routes if route.is_otherwise]
            if len(otherwise) > 1:
                raise ValueError(f"{domain.domain_id} declares more than one otherwise route")
            compile_queue_segments(
                domain.queue,
                routes,
                include_otherwise=bool(otherwise),
            )
        return self.definition

    def _domain(self, domain_id: str, queue: Queue) -> "DomainBuilder":
        if domain_id in self._domain_ids:
            raise ValueError(f"pipeline domain {domain_id!r} is already declared")
        self._domain_ids.add(domain_id)
        domain = DomainDefinition(domain_id, queue)
        self.definition.domains.append(domain)
        self.definition.operations.append(("domain", domain_id))
        return DomainBuilder(self, domain)

    def _effect(
        self,
        stable_id: str,
        scope: EffectScope,
        owner_id: str,
        label: str,
    ) -> EffectStageDefinition:
        normalized = validate_effect_stage_id(stable_id)
        if normalized in self._effect_ids:
            raise ValueError(f"pipeline EffectStage {normalized!r} is already declared")
        self._effect_ids.add(normalized)
        stage = EffectStageDefinition(normalized, scope, owner_id, str(label or ""))
        self.definition.effect_order.append(stage)
        return stage

    def _next_route_id(self, domain: str) -> str:
        self._route_counter += 1
        return f"{domain}.route_{self._route_counter}"

    def _next_layer_id(self, domain: str) -> str:
        self._layer_counter += 1
        return f"{domain}.layer_{self._layer_counter}"


class _RouteOwner:
    def __init__(self, pipeline: PipelineBuilder, domain: DomainDefinition):
        self._pipeline = pipeline
        self._domain = domain

    def _append_route(
        self,
        destination: list[RouteDefinition],
        path: Path,
        selector: Optional[Queue],
        *,
        fallback: Optional[Path] = None,
        layer_id: str = "",
        is_otherwise: bool = False,
        operation_sink: Optional[list[tuple[str, str]]] = None,
    ) -> "RouteBuilder":
        if selector is not None and not isinstance(selector, Queue):
            raise TypeError("route selector must be a Queue")
        route = RouteDefinition(
            route_id=self._pipeline._next_route_id(self._domain.domain_id),
            domain=self._domain.domain_id,
            selector=selector,
            path=Path(path),
            fallback=Path(fallback) if fallback is not None else None,
            layer_id=layer_id,
            is_otherwise=is_otherwise,
        )
        destination.append(route)
        if operation_sink is not None:
            operation_sink.append(("route", route.route_id))
        return RouteBuilder(self._pipeline, route)


class DomainBuilder(_RouteOwner):
    def __enter__(self) -> "DomainBuilder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def layer(self, name: str) -> "LayerBuilder":
        display_name = str(name or "").strip()
        if not display_name:
            raise ValueError("pipeline layer name cannot be empty")
        layer = LayerDefinition(
            self._pipeline._next_layer_id(self._domain.domain_id),
            display_name,
        )
        self._domain.layers.append(layer)
        self._domain.operations.append(("layer", layer.layer_id))
        return LayerBuilder(self._pipeline, self._domain, layer)

    def forward(self, selector: Optional[Queue] = None) -> "RouteBuilder":
        return self._append_route(
            self._domain.routes,
            Path.FORWARD,
            selector,
            operation_sink=self._domain.operations,
        )

    def forward_plus(self, selector: Optional[Queue] = None) -> "RouteBuilder":
        return self._append_route(
            self._domain.routes,
            Path.FORWARD_PLUS,
            selector,
            operation_sink=self._domain.operations,
        )

    def deferred(
        self,
        selector: Optional[Queue] = None,
        *,
        fallback: Optional[Path] = None,
    ) -> "RouteBuilder":
        return self._append_route(
            self._domain.routes,
            Path.DEFERRED,
            selector,
            fallback=fallback,
            operation_sink=self._domain.operations,
        )

    def otherwise(self) -> "OtherwiseBuilder":
        return OtherwiseBuilder(
            self._pipeline,
            self._domain,
            self._domain.routes,
            self._domain.operations,
        )

    def effects(self, stable_id: str, *, label: str = "") -> EffectStageDefinition:
        stage = self._pipeline._effect(
            stable_id, EffectScope.STAGE, self._domain.domain_id, label
        )
        self._domain.effects.append(stage)
        self._domain.operations.append(("effect", stage.stable_id))
        return stage


class LayerBuilder(_RouteOwner):
    def __init__(
        self,
        pipeline: PipelineBuilder,
        domain: DomainDefinition,
        layer: LayerDefinition,
    ):
        super().__init__(pipeline, domain)
        self._layer = layer

    def __enter__(self) -> "LayerBuilder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def forward(self, selector: Optional[Queue] = None) -> "RouteBuilder":
        return self._append_route(
            self._layer.routes,
            Path.FORWARD,
            selector,
            layer_id=self._layer.layer_id,
            operation_sink=self._layer.operations,
        )

    def forward_plus(self, selector: Optional[Queue] = None) -> "RouteBuilder":
        return self._append_route(
            self._layer.routes,
            Path.FORWARD_PLUS,
            selector,
            layer_id=self._layer.layer_id,
            operation_sink=self._layer.operations,
        )

    def deferred(
        self,
        selector: Optional[Queue] = None,
        *,
        fallback: Optional[Path] = None,
    ) -> "RouteBuilder":
        return self._append_route(
            self._layer.routes,
            Path.DEFERRED,
            selector,
            fallback=fallback,
            layer_id=self._layer.layer_id,
            operation_sink=self._layer.operations,
        )

    def otherwise(self) -> "OtherwiseBuilder":
        return OtherwiseBuilder(
            self._pipeline,
            self._domain,
            self._layer.routes,
            self._layer.operations,
            layer_id=self._layer.layer_id,
        )

    def effects(self, stable_id: str, *, label: str = "") -> EffectStageDefinition:
        stage = self._pipeline._effect(
            stable_id, EffectScope.LAYER, self._layer.layer_id, label
        )
        self._layer.effects.append(stage)
        self._layer.operations.append(("effect", stage.stable_id))
        return stage


class OtherwiseBuilder:
    def __init__(
        self,
        pipeline: PipelineBuilder,
        domain: DomainDefinition,
        destination: list[RouteDefinition],
        operation_sink: list[tuple[str, str]],
        *,
        layer_id: str = "",
    ):
        self._pipeline = pipeline
        self._domain = domain
        self._destination = destination
        self._operation_sink = operation_sink
        self._layer_id = layer_id

    def _route(self, path: Path, fallback: Optional[Path] = None) -> "RouteBuilder":
        owner = _RouteOwner(self._pipeline, self._domain)
        return owner._append_route(
            self._destination,
            path,
            None,
            fallback=fallback,
            layer_id=self._layer_id,
            is_otherwise=True,
            operation_sink=self._operation_sink,
        )

    def forward(self) -> "RouteBuilder":
        return self._route(Path.FORWARD)

    def forward_plus(self) -> "RouteBuilder":
        return self._route(Path.FORWARD_PLUS)

    def deferred(self, *, fallback: Optional[Path] = None) -> "RouteBuilder":
        return self._route(Path.DEFERRED, fallback)


class RouteBuilder:
    def __init__(self, pipeline: PipelineBuilder, route: RouteDefinition):
        self._pipeline = pipeline
        self.route = route

    def effects(self, stable_id: str, *, label: str = "") -> "RouteBuilder":
        stage = self._pipeline._effect(
            stable_id, EffectScope.ROUTE, self.route.route_id, label
        )
        self.route.effects.append(stage)
        return self


def _queue_value(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("render queue values must be integers")
    if not MATERIAL_QUEUE_MIN <= value <= MATERIAL_QUEUE_MAX:
        raise ValueError(
            f"render queue must be in [{MATERIAL_QUEUE_MIN}, {MATERIAL_QUEUE_MAX}]"
        )
    return value


__all__ = [
    "DomainDefinition",
    "EffectStageDefinition",
    "FrameDefinition",
    "LayerDefinition",
    "LightingDefinition",
    "Path",
    "PipelineBuilder",
    "PipelineDefinition",
    "Queue",
    "QueueRoute",
    "QueueSegment",
    "QueueSelector",
    "RouteDefinition",
    "ShadowDefinition",
    "compile_queue_segments",
]
