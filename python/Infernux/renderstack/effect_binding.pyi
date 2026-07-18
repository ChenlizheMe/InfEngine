from __future__ import annotations

from typing import Any, Mapping

from Infernux.renderstack.render_effect_asset import EffectAssetReference

EFFECT_BINDING_SCHEMA: str
EFFECT_BINDING_SCHEMA_VERSION: int

class EffectSlotBinding:
    slot_id: str
    asset: EffectAssetReference | None
    enabled: bool
    overrides: Mapping[str, Any]
    def __init__(
        self,
        slot_id: str,
        asset: EffectAssetReference | None = ...,
        enabled: bool = ...,
        overrides: Mapping[str, Any] = ...,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...

class EffectBindingDocument:
    stages: Mapping[str, tuple[EffectSlotBinding, ...]]
    schema_version: int
    def __init__(
        self,
        stages: Mapping[str, tuple[EffectSlotBinding, ...]] = ...,
        schema_version: int = ...,
    ) -> None: ...
    def slots(self, stage_id: str) -> tuple[EffectSlotBinding, ...]: ...
    def with_stage(
        self,
        stage_id: str,
        slots: tuple[EffectSlotBinding, ...],
    ) -> EffectBindingDocument: ...
    def to_dict(self) -> dict[str, Any]: ...

def parse_effect_binding_document(value: str | bytes | Mapping[str, Any]) -> EffectBindingDocument: ...
def dump_effect_binding_document(document: EffectBindingDocument, *, indent: int | None = ...) -> str: ...
