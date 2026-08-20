from __future__ import annotations
from typing import Mapping
from Infernux.rendergraph.graph import TextureHandle

class BufferHandle:
    name: str
    def __init__(self, name: str) -> None: ...

class PassResult:
    source: str
    revision: int
    buffers: Mapping[str, TextureHandle]
    def has(self, name: str | BufferHandle) -> bool: ...
    def sample(self, name: str | BufferHandle) -> TextureHandle: ...
    @property
    def snapshot(self) -> Mapping[str, TextureHandle]: ...

def normalize_buffer_name(value: str) -> str: ...
