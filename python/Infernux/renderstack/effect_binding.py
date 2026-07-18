"""Versioned scene bindings from EffectStage IDs to ordered effect slots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from Infernux.renderstack.effect_stage import validate_effect_stage_id
from Infernux.renderstack.render_effect_asset import EffectAssetReference


EFFECT_BINDING_SCHEMA = "infernux.render_stack_effect_bindings"
EFFECT_BINDING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EffectSlotBinding:
    """One stable slot in a scene RenderStack EffectStage list."""

    slot_id: str
    asset: EffectAssetReference | None = None
    enabled: bool = True
    overrides: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        slot_id = str(self.slot_id or "").strip()
        if not slot_id:
            raise ValueError("effect slot_id cannot be empty")
        if self.asset is not None and not isinstance(self.asset, EffectAssetReference):
            raise TypeError("effect slot asset must be EffectAssetReference or None")
        if not isinstance(self.enabled, bool):
            raise TypeError("effect slot enabled must be a bool")
        if type(self.overrides) is not dict:
            raise TypeError("effect slot overrides must be an object")
        object.__setattr__(self, "slot_id", slot_id)
        object.__setattr__(self, "overrides", _json_clone(dict(self.overrides)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "asset": self.asset.to_dict() if self.asset is not None else None,
            "enabled": self.enabled,
            "overrides": _json_clone(dict(self.overrides)),
        }


@dataclass(frozen=True)
class EffectBindingDocument:
    """All ordered EffectStage slot lists serialized by one RenderStack."""

    stages: Mapping[str, tuple[EffectSlotBinding, ...]] = field(default_factory=dict)
    schema_version: int = EFFECT_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != EFFECT_BINDING_SCHEMA_VERSION:
            raise ValueError(f"effect binding $version must be {EFFECT_BINDING_SCHEMA_VERSION}")
        if type(self.stages) is not dict:
            raise TypeError("effect binding stages must be an object")

        normalized: dict[str, tuple[EffectSlotBinding, ...]] = {}
        all_slot_ids: set[str] = set()
        for stage_id, raw_slots in self.stages.items():
            normalized_id = validate_effect_stage_id(stage_id)
            slots = tuple(raw_slots)
            if not all(isinstance(slot, EffectSlotBinding) for slot in slots):
                raise TypeError(f"stage {normalized_id!r} must contain EffectSlotBinding values")
            for slot in slots:
                if slot.slot_id in all_slot_ids:
                    raise ValueError(f"duplicate effect slot_id: {slot.slot_id!r}")
                all_slot_ids.add(slot.slot_id)
            normalized[normalized_id] = slots
        object.__setattr__(self, "stages", normalized)

    def slots(self, stage_id: str) -> tuple[EffectSlotBinding, ...]:
        return self.stages.get(validate_effect_stage_id(stage_id), ())

    def with_stage(
        self,
        stage_id: str,
        slots: tuple[EffectSlotBinding, ...],
    ) -> "EffectBindingDocument":
        normalized_id = validate_effect_stage_id(stage_id)
        stages = dict(self.stages)
        stages[normalized_id] = tuple(slots)
        return EffectBindingDocument(stages=stages, schema_version=self.schema_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": EFFECT_BINDING_SCHEMA,
            "$version": self.schema_version,
            "stages": {
                stage_id: [slot.to_dict() for slot in slots]
                for stage_id, slots in sorted(self.stages.items())
            },
        }


def parse_effect_binding_document(value: str | bytes | Mapping[str, Any]) -> EffectBindingDocument:
    """Parse the strict RenderStack stage-slot document stored in a scene."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        root = json.loads(value)
    elif isinstance(value, Mapping):
        root = dict(value)
    else:
        raise TypeError("effect binding document must be JSON text or an object")
    if type(root) is not dict:
        raise TypeError("effect binding document root must be an object")
    _require_exact_keys(root, {"$schema", "$version", "stages"}, "effect binding")
    if root["$schema"] != EFFECT_BINDING_SCHEMA:
        raise ValueError(f"unsupported effect binding schema: {root['$schema']!r}")
    if type(root["stages"]) is not dict:
        raise TypeError("effect binding stages must be an object")

    stages: dict[str, tuple[EffectSlotBinding, ...]] = {}
    for stage_id, raw_slots in root["stages"].items():
        normalized_id = validate_effect_stage_id(stage_id)
        if type(raw_slots) is not list:
            raise TypeError(f"stage {normalized_id!r} slots must be an array")
        slots = []
        for index, raw_slot in enumerate(raw_slots):
            location = f"stages.{normalized_id}[{index}]"
            if type(raw_slot) is not dict:
                raise TypeError(f"{location} must be an object")
            _require_exact_keys(raw_slot, {"slot_id", "asset", "enabled", "overrides"}, location)
            raw_asset = raw_slot["asset"]
            asset = None if raw_asset is None else _parse_reference(raw_asset, f"{location}.asset")
            slots.append(
                EffectSlotBinding(
                    slot_id=raw_slot["slot_id"],
                    asset=asset,
                    enabled=raw_slot["enabled"],
                    overrides=raw_slot["overrides"],
                )
            )
        stages[normalized_id] = tuple(slots)
    return EffectBindingDocument(stages=stages, schema_version=root["$version"])


def dump_effect_binding_document(document: EffectBindingDocument, *, indent: int | None = None) -> str:
    if not isinstance(document, EffectBindingDocument):
        raise TypeError("document must be an EffectBindingDocument")
    separators = (",", ":") if indent is None else None
    return json.dumps(
        document.to_dict(),
        ensure_ascii=False,
        indent=indent,
        separators=separators,
        sort_keys=True,
    )


def _parse_reference(value: Any, location: str) -> EffectAssetReference:
    if type(value) is not dict:
        raise TypeError(f"{location} must be an object")
    _require_exact_keys(value, {"guid", "path_hint"}, location)
    if type(value["guid"]) is not str or type(value["path_hint"]) is not str:
        raise TypeError(f"{location}.guid and path_hint must be strings")
    return EffectAssetReference(guid=value["guid"], path_hint=value["path_hint"])


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{location} keys mismatch; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TypeError("effect slot overrides must be finite JSON data") from exc
