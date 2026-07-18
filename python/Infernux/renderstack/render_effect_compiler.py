"""Compile reusable RenderEffect sources into graph passes and parameter blocks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import (
    RenderEffectAsset,
    RenderEffectGroupAsset,
    parse_render_effect_document,
)


class RenderEffectCompileError(ValueError):
    """An effect source cannot satisfy its feature or graph contract."""


@dataclass(frozen=True)
class RenderEffectFeature:
    type_id: str
    effect_class: type
    topology_parameters: frozenset[str] = frozenset()
    required_inputs: frozenset[str] = frozenset({"color"})

    def instantiate(self, source: RenderEffect):
        from Infernux.components.serialized_field import get_serialized_fields

        instance = self.effect_class()
        fields = get_serialized_fields(self.effect_class)
        parameters = dict(source.to_asset().parameters)

        # Bloom assets may expose a compact color while the legacy effect
        # implementation still stores scalar shader channels.
        tint = parameters.pop("tint", None)
        if tint is not None and self.type_id == "infernux.post.bloom":
            if not isinstance(tint, (list, tuple)) or len(tint) < 3:
                raise RenderEffectCompileError("bloom tint must contain at least three numbers")
            parameters.update(tint_r=tint[0], tint_g=tint[1], tint_b=tint[2])

        unknown = sorted(set(parameters) - set(fields))
        if unknown:
            raise RenderEffectCompileError(
                f"effect feature {self.type_id!r} has unknown parameters: {unknown}"
            )
        instance.set_params_dict(parameters)
        return instance

    def topology_signature(self, source: RenderEffect) -> tuple:
        parameters = source.to_asset().parameters
        return tuple(
            (name, json.dumps(parameters.get(name), sort_keys=True, allow_nan=False))
            for name in sorted(self.topology_parameters)
        )


_FEATURES: dict[str, RenderEffectFeature] = {}
_BUILTINS_REGISTERED = False


def register_render_effect_feature(
    type_id: str,
    effect_class: type,
    *,
    topology_parameters=(),
    required_inputs=("color",),
) -> RenderEffectFeature:
    """Register one AOT feature implementation for `.effect` sources."""
    normalized = str(type_id or "").strip()
    if not normalized:
        raise ValueError("render effect feature type id cannot be empty")
    existing = _FEATURES.get(normalized)
    feature = RenderEffectFeature(
        normalized,
        effect_class,
        frozenset(str(name) for name in topology_parameters),
        frozenset(str(name) for name in required_inputs),
    )
    if existing is not None and existing != feature:
        raise ValueError(f"render effect feature {normalized!r} is already registered")
    _FEATURES[normalized] = feature
    return feature


def get_render_effect_feature(type_id: str) -> RenderEffectFeature:
    _register_builtin_features()
    feature = _FEATURES.get(str(type_id))
    if feature is None:
        raise RenderEffectCompileError(f"unknown render effect feature: {type_id!r}")
    return feature


def _register_builtin_features() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    from Infernux.renderstack.bloom_effect import BloomEffect
    from Infernux.renderstack.chromatic_aberration_effect import ChromaticAberrationEffect
    from Infernux.renderstack.color_adjustments_effect import ColorAdjustmentsEffect
    from Infernux.renderstack.film_grain_effect import FilmGrainEffect
    from Infernux.renderstack.sharpen_effect import SharpenEffect
    from Infernux.renderstack.tonemapping_effect import ToneMappingEffect
    from Infernux.renderstack.vignette_effect import VignetteEffect
    from Infernux.renderstack.white_balance_effect import WhiteBalanceEffect

    register_render_effect_feature(
        "infernux.post.bloom",
        BloomEffect,
        topology_parameters={"max_iterations"},
    )
    register_render_effect_feature("infernux.post.tonemapping", ToneMappingEffect)
    register_render_effect_feature("infernux.post.color_adjustments", ColorAdjustmentsEffect)
    register_render_effect_feature(
        "infernux.post.chromatic_aberration",
        ChromaticAberrationEffect,
    )
    register_render_effect_feature("infernux.post.film_grain", FilmGrainEffect)
    register_render_effect_feature("infernux.post.sharpen", SharpenEffect)
    register_render_effect_feature("infernux.post.vignette", VignetteEffect)
    register_render_effect_feature("infernux.post.white_balance", WhiteBalanceEffect)
    _BUILTINS_REGISTERED = True


@dataclass(frozen=True)
class _ParameterBlockSpec:
    block_id: str
    pass_index: int
    names: tuple[str, ...]


@dataclass
class CompiledEffectBinding:
    """A compiled effect instance whose values can update independently."""

    binding_id: str
    source: RenderEffect
    feature: RenderEffectFeature
    blocks: tuple[_ParameterBlockSpec, ...]
    topology_signature: tuple

    def collect_updates(self):
        """Return ``(requires_rebuild, native_updates)`` for current values."""
        if self.feature.topology_signature(self.source) != self.topology_signature:
            return True, []
        passes = _record_feature_passes(self.source, self.feature)
        updates = []
        from Infernux.lib import GraphParameterBlockUpdate

        for spec in self.blocks:
            if spec.pass_index >= len(passes):
                return True, []
            values = tuple(passes[spec.pass_index]._push_constants.items())
            if tuple(name for name, _ in values) != spec.names:
                return True, []
            update = GraphParameterBlockUpdate()
            update.id = spec.block_id
            update.revision = self.source.revision + 1
            update.values = values
            updates.append(update)
        return False, updates


def compile_effect_slots(stage, slots, graph, bus):
    """Expand and compile one ordered pipeline EffectStage slot list."""
    bindings: list[CompiledEffectBinding] = []
    errors: list[str] = []
    for slot in slots:
        if not slot.enabled or not slot.effect_ref:
            continue
        try:
            sources = expand_render_effect_reference(slot.effect_ref)
            for source_index, source in enumerate(sources):
                binding = _compile_effect(
                    source,
                    graph,
                    bus,
                    binding_id=f"{stage.stable_id}/{slot.slot_id}/{source_index}",
                    available_inputs=stage.contract.inputs,
                )
                if binding.blocks:
                    bindings.append(binding)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{stage.stable_id}/{slot.slot_id}: {exc}")
    return bindings, errors


def _compile_effect(
    source,
    graph,
    bus,
    *,
    binding_id: str,
    available_inputs,
) -> CompiledEffectBinding:
    feature = get_render_effect_feature(source.feature_type)
    missing = sorted(feature.required_inputs - frozenset(available_inputs))
    if missing:
        raise RenderEffectCompileError(
            f"effect feature {feature.type_id!r} requires unavailable stage inputs: {missing}"
        )
    instance = feature.instantiate(source)
    first_pass = len(graph._passes)
    first_texture = len(graph._textures)
    first_topology = len(graph._topology)
    bus_snapshot = bus.snapshot()
    try:
        with graph.name_scope(f"effects/{binding_id}"):
            instance.setup_passes(graph, bus)
    except Exception:
        del graph._passes[first_pass:]
        del graph._textures[first_texture:]
        del graph._topology[first_topology:]
        bus._resources = bus_snapshot
        raise
    generated = graph._passes[first_pass:]
    blocks = []
    for pass_index, render_pass in enumerate(generated):
        if not render_pass._push_constants:
            continue
        block_id = f"effect/{binding_id}/{pass_index}"
        values = tuple(render_pass._push_constants.items())
        render_pass.bind_parameter_block(block_id, dict(values))
        blocks.append(
            _ParameterBlockSpec(
                block_id=block_id,
                pass_index=pass_index,
                names=tuple(name for name, _ in values),
            )
        )
    return CompiledEffectBinding(
        binding_id=binding_id,
        source=source,
        feature=feature,
        blocks=tuple(blocks),
        topology_signature=feature.topology_signature(source),
    )


def _record_feature_passes(source: RenderEffect, feature: RenderEffectFeature):
    from Infernux.rendergraph.graph import RenderGraph
    from Infernux.renderstack.resource_bus import ResourceBus

    graph = RenderGraph("RenderEffectParameterProbe")
    color = graph.create_texture("color", camera_target=True)
    bus = ResourceBus()
    bus.set("color", color)
    feature.instantiate(source).setup_passes(graph, bus)
    return graph._passes


def expand_render_effect_reference(
    reference: RenderEffectRef,
    *,
    _parent: str = "",
    _trail: tuple[str, ...] = (),
) -> list[RenderEffect]:
    """Resolve one effect or recursively flatten an ordered effect group."""
    cached = reference.resolve()
    if isinstance(cached, RenderEffect):
        return [cached]

    path = _resolve_reference_path(reference, _parent)
    if not path:
        raise RenderEffectCompileError(
            f"effect reference cannot be resolved: {reference.path_hint or reference.guid!r}"
        )
    cycle_key = reference.guid or os.path.normcase(os.path.abspath(path))
    if cycle_key in _trail:
        raise RenderEffectCompileError(f"render effect group cycle detected at {path!r}")
    document = parse_render_effect_document(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, RenderEffectAsset):
        return [RenderEffect(document, file_path=path, guid=reference.guid)]
    if not isinstance(document, RenderEffectGroupAsset):
        raise RenderEffectCompileError(f"unsupported effect document: {path!r}")

    flattened: list[RenderEffect] = []
    for entry in document.entries:
        if not entry.enabled:
            continue
        child = RenderEffectRef(
            guid=entry.asset.guid,
            path_hint=entry.asset.path_hint,
        )
        children = expand_render_effect_reference(
            child,
            _parent=os.path.dirname(path),
            _trail=(*_trail, cycle_key),
        )
        if entry.overrides:
            children = _apply_group_overrides(children, entry.overrides, entry.entry_id)
        flattened.extend(children)
    return flattened


def _apply_group_overrides(sources, overrides: Mapping, entry_id: str):
    clones = [source.clone() for source in sources]
    for name, value in overrides.items():
        matched = False
        for source in clones:
            if source.has_parameter(name):
                source.set_param(name, value)
                matched = True
        if not matched:
            raise RenderEffectCompileError(
                f"effect group entry {entry_id!r} overrides unknown parameter {name!r}"
            )
    return clones


def _resolve_reference_path(reference: RenderEffectRef, parent: str) -> str:
    path = ""
    if reference.guid:
        from Infernux.core.assets import AssetManager

        path = AssetManager._get_path_from_guid(reference.guid) or ""
    if not path:
        path = reference.path_hint
    if path and parent and not os.path.isabs(path) and not os.path.isfile(path):
        path = os.path.join(parent, path)
    return os.path.normpath(path) if path else ""
