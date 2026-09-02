"""Pipeline-independent geometry-buffer contracts.

Geometry buffers describe the output of one geometry stage. They are not a
global scene singleton: every :class:`PassResult` names its source stage and
owns the semantic resources produced by that stage. ``color`` follows the same
source-scoped result model, while the geometry providers below own the default
base-color, depth, normal, and motion semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping

from Infernux.renderstack.pass_result import (
    PassResult,
    normalize_buffer_name,
)


BASE_COLOR = "base_color"
DEPTH = "depth"
NORMAL = "normal"
MOTION = "motion"
DEFAULT_GEOMETRY_BUFFERS = frozenset({BASE_COLOR, DEPTH, NORMAL, MOTION})


class GeometryBufferTopologyError(RuntimeError):
    """A geometry-buffer provider graph cannot be compiled."""


class GeometryStagePhase(str, Enum):
    """Stable points at which a pipeline may materialize geometry data."""

    OPAQUE = "opaque"
    TRANSPARENT = "transparent"


@dataclass(frozen=True)
class GeometryBufferProviderSpec:
    semantic: str
    phase: GeometryStagePhase
    dependencies: frozenset[str]
    method_name: str


def geometry_buffer(
    semantic: str,
    *,
    phase: GeometryStagePhase | str = GeometryStagePhase.OPAQUE,
    dependencies: Iterable[str] = (),
) -> Callable:
    """Declare a fallback or custom geometry-buffer producer.

    Derived pipelines override a built-in producer by declaring the same
    semantic and phase. New semantics use the identical mechanism::

        @geometry_buffer("object_index", dependencies={"depth"})
        def build_object_index(self, context):
            ...
            return index_texture
    """

    normalized = _semantic(semantic)
    normalized_dependencies = frozenset(_semantic(value) for value in dependencies)
    normalized_phase = GeometryStagePhase(phase)

    def decorate(function):
        function.__inx_geometry_buffer_provider__ = (
            normalized,
            normalized_phase,
            normalized_dependencies,
        )
        return function

    return decorate


class GeometryBufferProviderContext:
    """Mutable construction context used while producing one result."""

    def __init__(
        self,
        pipeline,
        graph,
        *,
        source: str,
        phase: GeometryStagePhase,
        seed: Mapping[str, object],
        queue_range,
        msaa_samples: int,
        sort_mode: str,
        clear: bool,
    ) -> None:
        self.pipeline = pipeline
        self.graph = graph
        self.source = str(source)
        self.phase = phase
        self.queue_range = tuple(queue_range)
        self.msaa_samples = int(msaa_samples)
        self.sort_mode = str(sort_mode)
        self.clear = bool(clear)
        self._buffers = {
            _semantic(name): texture
            for name, texture in dict(seed).items()
            if texture is not None
        }

    @property
    def requirements(self) -> frozenset[str]:
        return self.graph.geometry_buffer_requirements

    def has(self, semantic: str) -> bool:
        return _semantic(semantic) in self._buffers

    def sample(self, semantic: str):
        return PassResult(self.source, -1, self._buffers).sample(semantic)

    def publish(self, semantic: str, texture):
        if texture is None:
            raise ValueError("geometry buffer provider cannot publish None")
        self._buffers[_semantic(semantic)] = texture
        return texture

    def build(self) -> PassResult:
        return self.graph.publish_pass_result(self.source, self._buffers)


def provider_specs(
    pipeline_class: type,
) -> Mapping[tuple[str, GeometryStagePhase], GeometryBufferProviderSpec]:
    """Resolve provider overrides by semantic and phase across the MRO."""

    resolved: dict[tuple[str, GeometryStagePhase], GeometryBufferProviderSpec] = {}
    for owner in reversed(pipeline_class.__mro__):
        declared: dict[tuple[str, GeometryStagePhase], str] = {}
        for method_name, value in vars(owner).items():
            metadata = getattr(value, "__inx_geometry_buffer_provider__", None)
            if metadata is None:
                continue
            semantic, phase, dependencies = metadata
            key = (semantic, phase)
            previous = declared.get(key)
            if previous is not None:
                raise GeometryBufferTopologyError(
                    f"ambiguous geometry buffer provider for {semantic!r} at "
                    f"{phase.value!r} in {owner.__name__}: {previous!r} and "
                    f"{method_name!r}"
                )
            declared[key] = method_name
            resolved[(semantic, phase)] = GeometryBufferProviderSpec(
                semantic=semantic,
                phase=phase,
                dependencies=dependencies,
                method_name=method_name,
            )
    return resolved


def requirement_closure(
    requested: Iterable[str],
    providers: Mapping[tuple[str, GeometryStagePhase], GeometryBufferProviderSpec],
) -> frozenset[str]:
    result = {_semantic(value) for value in requested if str(value or "").strip()}
    pending = list(result)
    while pending:
        semantic = pending.pop()
        for (candidate, _phase), provider in providers.items():
            if candidate != semantic:
                continue
            for dependency in provider.dependencies:
                if dependency not in result:
                    result.add(dependency)
                    pending.append(dependency)
    return frozenset(result)


def topological_provider_order(
    requested: Iterable[str],
    *,
    available: Iterable[str],
    phase: GeometryStagePhase,
    providers: Mapping[tuple[str, GeometryStagePhase], GeometryBufferProviderSpec],
    source: str,
) -> tuple[GeometryBufferProviderSpec, ...]:
    """Return the provider order or raise a diagnostic topology error."""

    ready = {_semantic(value) for value in available}
    ordered: list[GeometryBufferProviderSpec] = []
    emitted: set[str] = set()
    visiting: list[str] = []

    def visit(semantic: str) -> None:
        if semantic in ready or semantic in emitted:
            return
        if semantic in visiting:
            cycle = visiting[visiting.index(semantic):] + [semantic]
            raise GeometryBufferTopologyError(
                f"geometry buffer dependency cycle for source {source!r}: "
                + " -> ".join(cycle)
            )
        spec = providers.get((semantic, phase))
        if spec is None:
            chain = " -> ".join((*visiting, semantic))
            raise GeometryBufferTopologyError(
                f"missing geometry buffer provider for {semantic!r} at "
                f"{phase.value!r} in source {source!r}; dependency chain: {chain}"
            )
        visiting.append(semantic)
        for dependency in sorted(spec.dependencies):
            visit(dependency)
        visiting.pop()
        ordered.append(spec)
        emitted.add(semantic)

    for semantic in sorted(_semantic(value) for value in requested):
        visit(semantic)
    return tuple(ordered)


def _semantic(value: str) -> str:
    return normalize_buffer_name(value)


__all__ = [
    "BASE_COLOR",
    "DEPTH",
    "NORMAL",
    "MOTION",
    "DEFAULT_GEOMETRY_BUFFERS",
    "GeometryBufferTopologyError",
    "GeometryStagePhase",
    "GeometryBufferProviderContext",
    "geometry_buffer",
    "topological_provider_order",
]
