"""Source-scoped named-buffer results for render pipeline authoring.

A result is a versioned view of the buffers available after one explicit
pipeline stage or pass.  There are no process-global ``scene color`` or
``scene depth`` singletons in this model: consumers retain the result they
want and sample a semantic from that result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping


def normalize_buffer_name(value: str) -> str:
    name = str(value or "").strip().lower()
    if not name:
        raise ValueError("render buffer name cannot be empty")
    return name


@dataclass(frozen=True)
class BufferHandle:
    """A symbolic named-buffer request used by pipeline authoring APIs."""

    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_buffer_name(self.name))


@dataclass
class PassResult:
    """Versioned named buffers produced by one explicit pipeline source.

    ``source`` is a stable author-facing stage/pass name.  Missing buffers may
    be materialized lazily by the owning pipeline.  A write never mutates an
    earlier result; :class:`RenderGraph` derives a new result revision instead.
    """

    source: str
    revision: int
    buffers: Mapping[str, object]
    _materialize: Callable[["PassResult", str], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        if not source:
            raise ValueError("pass result source cannot be empty")
        self.source = source
        self.revision = int(self.revision)
        self.buffers = {
            normalize_buffer_name(name): texture
            for name, texture in dict(self.buffers).items()
            if texture is not None
        }

    def has(self, name: str | BufferHandle) -> bool:
        return _buffer_name(name) in self.buffers

    def sample(self, name: str | BufferHandle):
        semantic = _buffer_name(name)
        if semantic not in self.buffers and self._materialize is not None:
            self._materialize(self, semantic)
        try:
            return self.buffers[semantic]
        except KeyError as exc:
            available = ", ".join(sorted(self.buffers)) or "none"
            raise RuntimeError(
                f"pass result {self.source!r} does not provide {semantic!r}; "
                f"available buffers: {available}"
            ) from exc

    def _publish_materialized(self, name: str, texture) -> None:
        """Publish a lazy provider result without changing source identity."""
        if texture is None:
            raise ValueError("pass result cannot publish a null buffer")
        self.buffers[normalize_buffer_name(name)] = texture

    @property
    def snapshot(self) -> Mapping[str, object]:
        return MappingProxyType(dict(self.buffers))


def _buffer_name(value: str | BufferHandle) -> str:
    if isinstance(value, BufferHandle):
        return value.name
    return normalize_buffer_name(value)


__all__ = ["BufferHandle", "PassResult", "normalize_buffer_name"]
