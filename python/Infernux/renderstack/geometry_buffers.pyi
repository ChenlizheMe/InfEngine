from __future__ import annotations
from enum import Enum
from typing import Callable, Iterable
from Infernux.renderstack.pass_result import BufferHandle, PassResult

BASE_COLOR: str
DEPTH: str
NORMAL: str
MOTION: str
DEFAULT_GEOMETRY_BUFFERS: frozenset[str]
class GeometryBufferTopologyError(RuntimeError): ...
class GeometryStagePhase(str, Enum):
    OPAQUE: str
    TRANSPARENT: str

def geometry_buffer(
    semantic: str,
    *,
    phase: GeometryStagePhase | str = ...,
    dependencies: Iterable[str] = ...,
) -> Callable: ...
