from __future__ import annotations

from typing import Any, Mapping, TypeAlias

RENDER_EFFECT_EXTENSION: str
RENDER_EFFECT_GROUP_EXTENSION: str
RENDER_EFFECT_SCHEMA: str
RENDER_EFFECT_GROUP_SCHEMA: str

class EffectAssetReference:
    guid: str
    path_hint: str
    def __init__(self, guid: str = ..., path_hint: str = ...) -> None: ...
    def to_dict(self) -> dict[str, str]: ...

class RenderEffectAsset:
    feature_type: str
    parameters: Mapping[str, Any]
    dependencies: tuple[EffectAssetReference, ...]
    def __init__(
        self,
        feature_type: str,
        parameters: Mapping[str, Any] = ...,
        dependencies: tuple[EffectAssetReference, ...] = ...,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...

class RenderEffectGroupEntry:
    entry_id: str
    asset: EffectAssetReference
    enabled: bool
    overrides: Mapping[str, Any]
    def __init__(
        self,
        entry_id: str,
        asset: EffectAssetReference,
        enabled: bool = ...,
        overrides: Mapping[str, Any] = ...,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...

class RenderEffectGroupAsset:
    entries: tuple[RenderEffectGroupEntry, ...]
    def __init__(
        self,
        entries: tuple[RenderEffectGroupEntry, ...] = ...,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...

RenderEffectDocument: TypeAlias = RenderEffectAsset | RenderEffectGroupAsset

def parse_render_effect_document(value: str | bytes | Mapping[str, Any]) -> RenderEffectDocument: ...
def dump_render_effect_document(document: RenderEffectDocument, *, indent: int = ...) -> str: ...
def direct_effect_dependencies(document: RenderEffectDocument) -> tuple[EffectAssetReference, ...]: ...
