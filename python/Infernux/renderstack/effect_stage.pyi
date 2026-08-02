from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Iterable

class EffectScope(str, Enum):
    ROUTE: EffectScope
    LAYER: EffectScope
    STAGE: EffectScope
    COMPOSITE: EffectScope

class EffectResourceContract:
    inputs: FrozenSet[str]
    outputs: FrozenSet[str]
    capabilities: FrozenSet[str]
    def __init__(
        self,
        inputs: Iterable[str] = ...,
        outputs: Iterable[str] = ...,
        capabilities: Iterable[str] = ...,
    ) -> None: ...

class EffectStage:
    stable_id: str
    scope: EffectScope
    display_name: str
    contract: EffectResourceContract
    def __init__(
        self,
        stable_id: str,
        scope: EffectScope,
        display_name: str = ...,
        contract: EffectResourceContract = ...,
    ) -> None: ...

def validate_effect_stage_id(value: str) -> str: ...
