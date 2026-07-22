"""Compile reusable RenderEffect sources into graph passes and parameter blocks."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.engine.path_utils import path_key, resolved_path
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import (
    RenderEffectAsset,
    RenderEffectGroupAsset,
    parse_render_effect_document,
)
from Infernux.renderstack.route_policy import RoutePolicy


class RenderEffectCompileError(ValueError):
    """An effect source cannot satisfy its feature or graph contract."""


_ARTIFACT_SCHEMA = "infernux.render_effect_artifact"
_EFFECT_GROUP_EXPANSIONS: dict[str, tuple[RenderEffect, ...]] = {}
_EFFECT_GROUP_EXPANSION_GENERATION = 0


def _clear_effect_group_expansions() -> None:
    global _EFFECT_GROUP_EXPANSION_GENERATION
    _EFFECT_GROUP_EXPANSIONS.clear()
    _EFFECT_GROUP_EXPANSION_GENERATION += 1


@dataclass(frozen=True)
class RenderEffectArtifact:
    """Immutable, successfully compiled source revision published to runtime."""

    source_key: str
    source_hash: str
    structural_hash: str
    kind: str
    revision: int
    artifact_path: str
    features: tuple[Mapping[str, Any], ...]


class RenderEffectArtifactRegistry:
    """Compile-then-publish registry with persistent last-known-good artifacts."""

    _artifacts: dict[str, RenderEffectArtifact] = {}
    _compiling: set[str] = set()
    _revision = 0
    _topology_generation = 0

    @classmethod
    def topology_generation(cls) -> int:
        return cls._topology_generation

    @classmethod
    def get(cls, path: str = "", guid: str = "") -> RenderEffectArtifact | None:
        return cls._artifacts.get(cls._source_key(path, guid))

    @classmethod
    def clear(cls) -> None:
        cls._artifacts.clear()
        cls._compiling.clear()
        cls._revision = 0
        cls._topology_generation = 0
        _clear_effect_group_expansions()

    @classmethod
    def compile_and_publish(cls, path: str, *, guid: str = ""):
        """Compile one source and atomically publish it after artifact IO succeeds."""
        source_path = resolved_path(path)
        key = cls._source_key(source_path, guid)
        if key in cls._compiling:
            raise RenderEffectCompileError(
                f"render effect compile cycle detected at {source_path!r}"
            )
        cls._compiling.add(key)
        expansion_snapshot = dict(_EFFECT_GROUP_EXPANSIONS)
        try:
            return cls._compile_and_publish_unchecked(source_path, guid=guid)
        except Exception:
            _EFFECT_GROUP_EXPANSIONS.clear()
            _EFFECT_GROUP_EXPANSIONS.update(expansion_snapshot)
            raise
        finally:
            cls._compiling.discard(key)

    @classmethod
    def _compile_and_publish_unchecked(cls, source_path: str, *, guid: str = ""):
        try:
            source_text = Path(source_path).read_text(encoding="utf-8")
            document = parse_render_effect_document(source_text)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RenderEffectCompileError(f"failed to read render effect source: {exc}") from exc

        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        key = cls._source_key(source_path, guid)
        existing = cls._artifacts.get(key)
        if existing is not None and existing.source_hash == source_hash:
            return existing, document

        group_sources = None
        if isinstance(document, RenderEffectGroupAsset):
            # Group membership is an asset-time concern. Publish a fresh,
            # flattened memory view when the source changes so RenderStack and
            # its Inspector never need to poll or parse source files per frame.
            _clear_effect_group_expansions()
            group_sources = _expand_render_effect_group_document(
                document,
                source_path,
                _trail=(path_key(source_path),),
            )

        artifact_path = cls._artifact_path(source_path, guid)
        if existing is None and artifact_path:
            persisted = cls._load_persisted(
                artifact_path,
                key=key,
                source_hash=source_hash,
            )
            if persisted is not None:
                cls._artifacts[key] = persisted
                cls._revision = max(cls._revision, persisted.revision)
                cls._topology_generation += 1
                if group_sources is not None:
                    _EFFECT_GROUP_EXPANSIONS[path_key(source_path)] = tuple(group_sources)
                return persisted, document

        if group_sources is None:
            features = cls._compile_document(document, source_path, guid)
        else:
            features = tuple(
                cls._compile_feature_record(source) for source in group_sources
            )
        structural_hash = cls._structural_hash(document, features)
        next_revision = cls._revision + 1
        payload = {
            "$schema": _ARTIFACT_SCHEMA,
            "source_key": key,
            "source_hash": source_hash,
            "structural_hash": structural_hash,
            "kind": "effect" if isinstance(document, RenderEffectAsset) else "group",
            "revision": next_revision,
            "source": document.to_dict(),
            "features": list(features),
        }
        if artifact_path:
            from Infernux.core.document_store import write_document_text

            os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
            write_document_text(
                artifact_path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

        artifact = RenderEffectArtifact(
            source_key=key,
            source_hash=source_hash,
            structural_hash=structural_hash,
            kind=payload["kind"],
            revision=next_revision,
            artifact_path=artifact_path,
            features=features,
        )
        cls._revision = next_revision
        cls._artifacts[key] = artifact
        if existing is None or existing.structural_hash != structural_hash:
            cls._topology_generation += 1
        if group_sources is not None:
            _EFFECT_GROUP_EXPANSIONS[path_key(source_path)] = tuple(group_sources)
        return artifact, document

    @staticmethod
    def _load_persisted(
        artifact_path: str,
        *,
        key: str,
        source_hash: str,
    ) -> RenderEffectArtifact | None:
        try:
            payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            if (
                type(payload) is not dict
                or set(payload) != {
                    "$schema", "source_key", "source_hash", "structural_hash",
                    "kind", "revision", "source", "features",
                }
                or payload.get("$schema") != _ARTIFACT_SCHEMA
                or payload.get("source_key") != key
                or payload.get("source_hash") != source_hash
                or payload.get("kind") not in {"effect", "group"}
                or type(payload.get("features")) is not list
                or not _is_current_feature_records(payload.get("features"))
            ):
                return None
            revision = payload.get("revision")
            structural_hash = payload.get("structural_hash")
            if type(revision) is not int or revision <= 0:
                return None
            if type(structural_hash) is not str or len(structural_hash) != 64:
                return None
            return RenderEffectArtifact(
                source_key=key,
                source_hash=source_hash,
                structural_hash=structural_hash,
                kind=payload["kind"],
                revision=revision,
                artifact_path=artifact_path,
                features=tuple(payload["features"]),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _compile_document(cls, document, source_path: str, guid: str):
        if isinstance(document, RenderEffectAsset):
            sources = [RenderEffect(document, file_path=source_path, guid=guid)]
        else:
            sources = _expand_render_effect_group_document(
                document,
                source_path,
                _trail=(path_key(source_path),),
            )
        return tuple(cls._compile_feature_record(source) for source in sources)

    @staticmethod
    def _compile_feature_record(source: RenderEffect) -> Mapping[str, Any]:
        feature = get_render_effect_feature(source.feature_type)
        passes = _record_feature_passes(source, feature)
        return {
            "feature_type": feature.type_id,
            "route_policy": feature.route_policy.value,
            "topology": list(feature.topology_signature(source)),
            "passes": [
                {
                    "name": render_pass.name,
                    "type": render_pass._pass_type,
                    "action": render_pass._action,
                    "shader": render_pass._shader_name,
                    "parameter_layout": list(render_pass._push_constants),
                }
                for render_pass in passes
            ],
        }

    @staticmethod
    def _structural_hash(document, features) -> str:
        if isinstance(document, RenderEffectAsset):
            structural = {
                "kind": "effect",
                "feature_type": document.feature_type,
                "features": features,
            }
        else:
            structural = {
                "kind": "group",
                "source": document.to_dict(),
                "features": features,
            }
        encoded = json.dumps(structural, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _source_key(path: str, guid: str) -> str:
        return str(guid or "").strip() or path_key(path)

    @staticmethod
    def _artifact_path(source_path: str, guid: str) -> str:
        from Infernux.engine.project_context import get_project_root

        project_root = get_project_root()
        if not project_root:
            return ""
        identity = str(guid or "").strip()
        if not identity:
            identity = hashlib.sha256(
                path_key(source_path).encode("utf-8")
            ).hexdigest()[:32]
        if not all(character.isalnum() or character in "-_" for character in identity):
            identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return os.path.join(
            project_root,
            "Library",
            "Artifacts",
            "RenderEffect",
            f"{identity}.inxeffect",
        )


def _is_current_feature_records(features) -> bool:
    if type(features) is not list:
        return False
    for feature in features:
        if type(feature) is not dict or set(feature) != {
            "feature_type",
            "route_policy",
            "topology",
            "passes",
        }:
            return False
        if type(feature["route_policy"]) is not str:
            return False
        if type(feature["topology"]) is not list or type(feature["passes"]) is not list:
            return False
        for render_pass in feature["passes"]:
            if type(render_pass) is not dict or set(render_pass) != {
                "name",
                "type",
                "action",
                "shader",
                "parameter_layout",
            }:
                return False
    return True


@dataclass(frozen=True)
class RenderEffectFeature:
    type_id: str
    effect_class: type
    topology_parameters: frozenset[str] = frozenset()
    route_policy: RoutePolicy = RoutePolicy.ISOLATE_AND_COMPOSITE

    def instantiate(self, source: RenderEffect):
        from Infernux.components.serialized_field import get_serialized_fields

        instance = self.effect_class()
        fields = get_serialized_fields(self.effect_class)
        parameters = dict(source.to_asset().parameters)

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
    route_policy=None,
) -> RenderEffectFeature:
    """Register one AOT feature implementation for `.effect` sources."""
    normalized = str(type_id or "").strip()
    if not normalized:
        raise ValueError("render effect feature type id cannot be empty")
    existing = _FEATURES.get(normalized)
    feature = RenderEffectFeature(
        type_id=normalized,
        effect_class=effect_class,
        topology_parameters=frozenset(str(name) for name in topology_parameters),
        route_policy=RoutePolicy(route_policy or RoutePolicy.ISOLATE_AND_COMPOSITE),
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
    from Infernux.renderstack.pixelation_effect import PixelationEffect
    from Infernux.renderstack.sharpen_effect import SharpenEffect
    from Infernux.renderstack.tonemapping_effect import ToneMappingEffect
    from Infernux.renderstack.vignette_effect import VignetteEffect
    from Infernux.renderstack.white_balance_effect import WhiteBalanceEffect

    register_render_effect_feature(
        "infernux.post.bloom",
        BloomEffect,
        topology_parameters={"max_iterations"},
        route_policy=RoutePolicy.ADDITIVE_EXTRACT,
    )
    register_render_effect_feature(
        "infernux.post.tonemapping",
        ToneMappingEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.post.color_adjustments",
        ColorAdjustmentsEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.post.chromatic_aberration",
        ChromaticAberrationEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.post.film_grain",
        FilmGrainEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.post.sharpen",
        SharpenEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.post.vignette",
        VignetteEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.post.white_balance",
        WhiteBalanceEffect,
        route_policy=RoutePolicy.MASK_AND_MODIFY,
    )
    register_render_effect_feature(
        "infernux.route.pixelation",
        PixelationEffect,
        route_policy=RoutePolicy.ISOLATE_AND_COMPOSITE,
    )
    _BUILTINS_REGISTERED = True


def resolve_effect_stage_route_policy(stage_ids, slot_lookup):
    """Resolve enabled assets mounted on stages into one route policy."""
    from Infernux.renderstack.route_policy import merge_route_policies

    policies = []
    for stage_id in stage_ids:
        for slot in slot_lookup(stage_id):
            if not slot.enabled or not slot.effect_ref:
                continue
            try:
                sources = expand_render_effect_reference(slot.effect_ref)
                policies.extend(
                    get_render_effect_feature(source.feature_type).route_policy
                    for source in sources
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # The normal EffectStage compiler records the actionable asset
                # error. A failed source must not force a costly route target.
                continue
    try:
        return merge_route_policies(policies)
    except ValueError as exc:
        joined = ", ".join(str(stage_id) for stage_id in stage_ids)
        raise RenderEffectCompileError(
            f"incompatible route effect policies for stages [{joined}]: {exc}"
        ) from exc


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
        if self.source.feature_type != self.feature.type_id:
            return True, []
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
) -> CompiledEffectBinding:
    feature = get_render_effect_feature(source.feature_type)
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
    cached = getattr(reference, "_cached", None)
    if isinstance(cached, RenderEffect):
        return [cached]

    local_group = getattr(reference, "_group_expansion_cache", None)
    local_identity = (reference.guid, reference.path_hint)
    if (
        isinstance(local_group, tuple)
        and len(local_group) == 3
        and local_group[0] == _EFFECT_GROUP_EXPANSION_GENERATION
        and local_group[1] == local_identity
    ):
        return list(local_group[2])

    path = _resolve_reference_path(reference, _parent)
    if not path:
        raise RenderEffectCompileError(
            f"effect reference cannot be resolved: {reference.path_hint or reference.guid!r}"
        )
    cycle_key = path_key(path)
    if cycle_key in _trail:
        raise RenderEffectCompileError(f"render effect group cycle detected at {path!r}")
    group_sources = _EFFECT_GROUP_EXPANSIONS.get(cycle_key)
    if group_sources is not None:
        reference._group_expansion_cache = (
            _EFFECT_GROUP_EXPANSION_GENERATION,
            local_identity,
            group_sources,
        )
        return list(group_sources)

    if path.lower().endswith(".effect"):
        cached = reference.resolve()
        if isinstance(cached, RenderEffect):
            return [cached]

    document = parse_render_effect_document(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, RenderEffectAsset):
        cached = reference.resolve()
        if isinstance(cached, RenderEffect):
            return [cached]
        return [RenderEffect(document, file_path=path, guid=reference.guid)]
    if not isinstance(document, RenderEffectGroupAsset):
        raise RenderEffectCompileError(f"unsupported effect document: {path!r}")

    flattened = _expand_render_effect_group_document(
        document,
        path,
        _trail=(*_trail, cycle_key),
    )
    _EFFECT_GROUP_EXPANSIONS[cycle_key] = tuple(flattened)
    reference._group_expansion_cache = (
        _EFFECT_GROUP_EXPANSION_GENERATION,
        local_identity,
        _EFFECT_GROUP_EXPANSIONS[cycle_key],
    )
    return list(flattened)


def _expand_render_effect_group_document(
    document: RenderEffectGroupAsset,
    path: str,
    *,
    _trail: tuple[str, ...] = (),
) -> list[RenderEffect]:
    """Flatten a parsed group into live effect objects for memory publication."""
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
            _trail=_trail,
        )
        if entry.overrides:
            children = _apply_group_overrides(children, entry.overrides, entry.entry_id)
        flattened.extend(children)
    return flattened


def _apply_group_overrides(sources, overrides: Mapping, entry_id: str):
    sources = list(sources)
    for name, value in overrides.items():
        if not any(source.has_parameter(name) for source in sources):
            raise RenderEffectCompileError(
                f"effect group entry {entry_id!r} overrides unknown parameter {name!r}"
            )
    return [
        _OverriddenRenderEffect(
            source,
            {name: value for name, value in overrides.items() if source.has_parameter(name)},
        )
        for source in sources
    ]


class _OverriddenRenderEffect(RenderEffect):
    """Live view that overlays group-local values on a shared source asset."""

    def __init__(self, source: RenderEffect, overrides: Mapping[str, Any]) -> None:
        self._source = source
        self._overrides = dict(overrides)
        source_asset = source.to_asset()
        parameters = dict(source_asset.parameters)
        parameters.update(self._overrides)
        super().__init__(
            RenderEffectAsset(
                feature_type=source_asset.feature_type,
                parameters=parameters,
                dependencies=source_asset.dependencies,
            )
        )

    @property
    def feature_type(self) -> str:
        return self._source.feature_type

    @property
    def revision(self) -> int:
        return self._source.revision

    @property
    def artifact_revision(self) -> int:
        return self._source.artifact_revision

    @property
    def file_path(self) -> str:
        return self._source.file_path

    @property
    def guid(self) -> str:
        return self._source.guid

    def to_asset(self) -> RenderEffectAsset:
        source = self._source.to_asset()
        parameters = dict(source.parameters)
        parameters.update(self._overrides)
        return RenderEffectAsset(
            feature_type=source.feature_type,
            parameters=parameters,
            dependencies=source.dependencies,
        )


def _resolve_reference_path(reference: RenderEffectRef, parent: str) -> str:
    path = ""
    if reference.guid:
        from Infernux.core.assets import AssetManager

        path = AssetManager._get_path_from_guid(reference.guid) or ""
    if not path:
        path = reference.path_hint
    if path and not os.path.isabs(path) and not os.path.isfile(path):
        from Infernux.engine.project_context import get_project_root

        project_root = get_project_root()
        project_path = os.path.join(project_root, path) if project_root else ""
        parent_path = os.path.join(parent, path) if parent else ""
        if project_path and os.path.isfile(project_path):
            path = project_path
        elif parent_path:
            path = parent_path
    return resolved_path(path) if path else ""
