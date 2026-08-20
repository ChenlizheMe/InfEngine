"""Authoritative runtime product and Player service-graph contracts.

This module is deliberately runtime-neutral.  Build tooling may use it to
emit ``Player.inxmanifest`` and the Player bootstrap uses the same model to
validate the packaged product before importing the engine runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .path_utils import (
    is_path_within,
    portable_relative_path,
    relative_path,
    resolved_path,
)


PLAYER_MANIFEST_SCHEMA = "infernux.player_runtime_manifest"
PLAYER_MANIFEST_VERSION = 1
_KNOWN_OPTIONAL_SUBSYSTEMS = frozenset({"splash"})


def _freeze_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_document(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_document(item) for item in value)
    return value


class RuntimeFlavor(str, Enum):
    EDITOR_DEVELOPMENT = "EditorDevelopment"
    PLAYER_DEBUG = "PlayerDebug"
    PLAYER_RELEASE = "PlayerRelease"

    @property
    def is_player(self) -> bool:
        return self is not RuntimeFlavor.EDITOR_DEVELOPMENT


class ValidationPolicy(str, Enum):
    AUTHORING = "authoring"
    DIAGNOSTIC = "diagnostic"
    RELEASE = "release"


class ProfilingPolicy(str, Enum):
    AUTHORING = "authoring"
    AVAILABLE = "available"
    MINIMAL = "minimal"


class LoggingPolicy(str, Enum):
    DEBUG = "debug"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class RuntimeFeatureSet:
    """Build-time feature switches that may retain optional runtime services."""

    jit: bool = False
    parallel: bool = False
    optional_subsystems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.jit) is not bool or type(self.parallel) is not bool:
            raise TypeError("runtime JIT and parallel features must be booleans")
        normalized = tuple(sorted({str(item).strip() for item in self.optional_subsystems}))
        if any(not item for item in normalized):
            raise ValueError("runtime subsystem names must be non-empty")
        unknown = set(normalized) - _KNOWN_OPTIONAL_SUBSYSTEMS
        if unknown:
            raise ValueError(
                "unknown runtime subsystems: " + ", ".join(sorted(unknown))
            )
        if self.parallel and not self.jit:
            raise ValueError("parallel runtime requires the JIT runtime")
        object.__setattr__(self, "optional_subsystems", normalized)

    @classmethod
    def from_manifest(cls, value: object) -> "RuntimeFeatureSet":
        if not isinstance(value, Mapping):
            raise RuntimeError("Player runtime feature declaration is missing")
        allowed = {"jit", "parallel", "optional_subsystems"}
        unknown = set(value) - allowed
        if unknown:
            raise RuntimeError(
                "Player runtime feature declaration contains unknown fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        jit = value.get("jit")
        parallel = value.get("parallel")
        subsystems = value.get("optional_subsystems")
        if not isinstance(jit, bool) or not isinstance(parallel, bool):
            raise RuntimeError("Player JIT and parallel features must be explicit booleans")
        if not isinstance(subsystems, list) or any(
            not isinstance(item, str) or not item.strip() for item in subsystems
        ):
            raise RuntimeError("Player optional_subsystems must be a string array")
        try:
            return cls(jit=jit, parallel=parallel, optional_subsystems=tuple(subsystems))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    def to_manifest(self) -> dict[str, object]:
        return {
            "jit": self.jit,
            "parallel": self.parallel,
            "optional_subsystems": list(self.optional_subsystems),
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    validation: ValidationPolicy
    profiling: ProfilingPolicy
    logging: LoggingPolicy
    player_control: str
    authoring_services: bool

    def to_manifest(self) -> dict[str, object]:
        return {
            "validation": self.validation.value,
            "profiling": self.profiling.value,
            "logging": self.logging.value,
            "player_control": self.player_control,
            "authoring_services": self.authoring_services,
        }


@dataclass(frozen=True, slots=True)
class RuntimeServiceSpec:
    service_id: str
    module: str
    dependencies: tuple[str, ...] = ()
    flavors: frozenset[RuntimeFlavor] = frozenset(RuntimeFlavor)
    feature: Optional[str] = None
    retention_reason: str = "runtime contract"
    authoring: bool = False

    def enabled_for(
        self,
        flavor: RuntimeFlavor,
        features: RuntimeFeatureSet,
    ) -> bool:
        if flavor not in self.flavors:
            return False
        if self.feature is None:
            return True
        if self.feature == "jit":
            return features.jit
        if self.feature == "parallel":
            return features.parallel
        return self.feature in features.optional_subsystems

    def to_manifest(self) -> dict[str, object]:
        return {
            "id": self.service_id,
            "module": self.module,
            "dependencies": list(self.dependencies),
            "retention_reason": self.retention_reason,
            "present": True,
        }


@dataclass(frozen=True, slots=True)
class RuntimeServiceGraph:
    flavor: RuntimeFlavor
    features: RuntimeFeatureSet
    services: tuple[RuntimeServiceSpec, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for service in self.services:
            if service.service_id in seen:
                raise ValueError(f"duplicate runtime service: {service.service_id}")
            missing = [dependency for dependency in service.dependencies if dependency not in seen]
            if missing:
                raise ValueError(
                    f"runtime service {service.service_id} precedes dependencies: "
                    + ", ".join(missing)
                )
            if self.flavor.is_player and service.authoring:
                raise ValueError(
                    f"Player service graph contains authoring service: {service.service_id}"
                )
            seen.add(service.service_id)

    @property
    def service_ids(self) -> tuple[str, ...]:
        return tuple(service.service_id for service in self.services)

    def contains(self, service_id: str) -> bool:
        return any(service.service_id == service_id for service in self.services)

    def require(self, service_id: str) -> RuntimeServiceSpec:
        for service in self.services:
            if service.service_id == service_id:
                return service
        raise RuntimeError(
            f"Runtime product {self.flavor.value} does not declare service {service_id!r}"
        )

    def to_manifest_records(self) -> list[dict[str, object]]:
        return [service.to_manifest() for service in self.services]

    def validate_manifest_records(self, value: object) -> None:
        if not isinstance(value, list):
            raise RuntimeError("Player service graph must be an array")
        expected = self.to_manifest_records()
        if value != expected:
            raise RuntimeError(
                "Player service graph does not match the authoritative runtime product graph"
            )


_ALL_FLAVORS = frozenset(RuntimeFlavor)
_PLAYER_FLAVORS = frozenset(
    {RuntimeFlavor.PLAYER_DEBUG, RuntimeFlavor.PLAYER_RELEASE}
)


_SERVICE_SPECS = (
    RuntimeServiceSpec(
        "application_context",
        "Infernux/application.pyc",
        flavors=_ALL_FLAVORS,
        retention_reason="runtime role and application state",
    ),
    RuntimeServiceSpec(
        "runtime_product_manifest",
        "Infernux/engine/player_service_graph.pyc",
        flavors=_ALL_FLAVORS,
        retention_reason="authoritative runtime product and service graph",
    ),
    RuntimeServiceSpec(
        "engine",
        "Infernux/engine/engine.pyc",
        dependencies=("application_context",),
        flavors=_ALL_FLAVORS,
        retention_reason="native engine host",
    ),
    RuntimeServiceSpec(
        "runtime_execution_scheduler",
        "Infernux/components/_component_lifecycle.pyc",
        dependencies=("application_context",),
        flavors=_ALL_FLAVORS,
        retention_reason="static lifecycle execution plans",
    ),
    RuntimeServiceSpec(
        "runtime_asset_catalog",
        "Infernux/engine/runtime_artifact_catalog.pyc",
        flavors=_PLAYER_FLAVORS,
        retention_reason="read-only GUID and RuntimeArtifact identity",
    ),
    RuntimeServiceSpec(
        "runtime_type_registry",
        "Infernux/engine/runtime_type_registry.pyc",
        flavors=_PLAYER_FLAVORS,
        retention_reason="static component type and lifecycle registry",
    ),
    RuntimeServiceSpec(
        "runtime_scene_transaction",
        "Infernux/engine/runtime_scene_transaction.pyc",
        dependencies=("runtime_asset_catalog", "runtime_type_registry"),
        flavors=_PLAYER_FLAVORS,
        retention_reason="owner-thread scene publication transaction",
    ),
    RuntimeServiceSpec(
        "player_scene_service",
        "Infernux/engine/player_scene.pyc",
        dependencies=("runtime_scene_transaction",),
        flavors=_PLAYER_FLAVORS,
        retention_reason="Player-owned scene lifecycle",
    ),
    RuntimeServiceSpec(
        "player_runtime_session",
        "Infernux/engine/player_runtime.pyc",
        dependencies=(
            "engine",
            "runtime_execution_scheduler",
            "player_scene_service",
        ),
        flavors=_PLAYER_FLAVORS,
        retention_reason="standalone gameplay lifecycle",
    ),
    RuntimeServiceSpec(
        "scene_api",
        "Infernux/scene/__init__.pyc",
        dependencies=("player_scene_service",),
        flavors=_PLAYER_FLAVORS,
        retention_reason="runtime scene-load API",
    ),
    RuntimeServiceSpec(
        "player_gui",
        "Infernux/engine/player_gui.pyc",
        dependencies=("engine", "player_runtime_session"),
        flavors=_PLAYER_FLAVORS,
        retention_reason="game viewport and runtime UI",
    ),
    RuntimeServiceSpec(
        "splash_player",
        "Infernux/engine/splash_player.pyc",
        dependencies=("player_gui",),
        flavors=_PLAYER_FLAVORS,
        feature="splash",
        retention_reason="configured startup media",
    ),
    RuntimeServiceSpec(
        "player_control_debug",
        "Infernux/engine/player_control.pyc",
        dependencies=("engine", "player_runtime_session"),
        flavors=frozenset({RuntimeFlavor.PLAYER_DEBUG}),
        retention_reason="token-authenticated PlayerDebug control",
    ),
    RuntimeServiceSpec(
        "jit_runtime_support",
        "Infernux/jit_runtime.pyc",
        dependencies=("runtime_execution_scheduler",),
        flavors=_PLAYER_FLAVORS,
        feature="jit",
        retention_reason="bounded JIT cache and adaptive dispatch",
    ),
    RuntimeServiceSpec(
        "jit_hir",
        "Infernux/jit_hir.pyc",
        dependencies=("jit_runtime_support",),
        flavors=_PLAYER_FLAVORS,
        feature="jit",
        retention_reason="typed JIT legality and cost model",
    ),
    RuntimeServiceSpec(
        "jit_kernels",
        "Infernux/_jit_kernels.pyc",
        dependencies=("jit_runtime_support", "jit_hir"),
        flavors=_PLAYER_FLAVORS,
        feature="jit",
        retention_reason="explicit JIT-enabled kernel dispatch",
    ),
    RuntimeServiceSpec(
        "jit_public_api",
        "Infernux/jit.pyc",
        dependencies=("jit_kernels",),
        flavors=_PLAYER_FLAVORS,
        feature="jit",
        retention_reason="public JIT API used by cooked scripts",
    ),
    RuntimeServiceSpec(
        "parallel_module",
        "Modules/Parallel.inxmod",
        dependencies=("jit_public_api",),
        flavors=_PLAYER_FLAVORS,
        feature="parallel",
        retention_reason="explicit Numba/LLVM parallel payload",
    ),
    RuntimeServiceSpec(
        "player_bootstrap",
        "Infernux/engine/player_bootstrap.pyc",
        dependencies=(
            "runtime_product_manifest",
            "engine",
            "player_runtime_session",
            "player_gui",
        ),
        flavors=_PLAYER_FLAVORS,
        retention_reason="manifest-first Player assembly",
    ),
    RuntimeServiceSpec(
        "editor_play_mode",
        "Infernux/engine/play_mode.pyc",
        dependencies=("engine", "runtime_execution_scheduler"),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="Editor Play snapshot and restore",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_resources",
        "Infernux/engine/resources_manager.pyc",
        dependencies=("engine",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring import and watcher service",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_script_compiler",
        "Infernux/engine/script_compiler.pyc",
        dependencies=("editor_resources",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring script revision compiler",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_selection",
        "Infernux/engine/interaction/selection.pyc",
        dependencies=("engine",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring selection state",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_undo",
        "Infernux/engine/undo/_manager.pyc",
        dependencies=("editor_selection",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring command history",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_inspector_schema",
        "Infernux/engine/ui/inspector_snapshot.pyc",
        dependencies=("editor_selection",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring Inspector schema and snapshots",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_preview",
        "Infernux/engine/ui/asset_resource_preview.pyc",
        dependencies=("editor_resources",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring asset preview publication",
        authoring=True,
    ),
    RuntimeServiceSpec(
        "editor_importer",
        "Infernux/engine/import_coordinator.pyc",
        dependencies=("editor_resources",),
        flavors=frozenset({RuntimeFlavor.EDITOR_DEVELOPMENT}),
        retention_reason="authoring asset import coordination",
        authoring=True,
    ),
)


def runtime_policy_for(flavor: RuntimeFlavor) -> RuntimePolicy:
    if not isinstance(flavor, RuntimeFlavor):
        raise TypeError("runtime flavor must be a RuntimeFlavor")
    if flavor is RuntimeFlavor.EDITOR_DEVELOPMENT:
        return RuntimePolicy(
            validation=ValidationPolicy.AUTHORING,
            profiling=ProfilingPolicy.AUTHORING,
            logging=LoggingPolicy.DEBUG,
            player_control="disabled",
            authoring_services=True,
        )
    if flavor is RuntimeFlavor.PLAYER_DEBUG:
        return RuntimePolicy(
            validation=ValidationPolicy.DIAGNOSTIC,
            profiling=ProfilingPolicy.AVAILABLE,
            logging=LoggingPolicy.DEBUG,
            player_control="token_authenticated",
            authoring_services=False,
        )
    return RuntimePolicy(
        validation=ValidationPolicy.RELEASE,
        profiling=ProfilingPolicy.MINIMAL,
        logging=LoggingPolicy.INFO,
        player_control="disabled",
        authoring_services=False,
    )


def runtime_service_graph_for(
    flavor: RuntimeFlavor,
    features: RuntimeFeatureSet = RuntimeFeatureSet(),
) -> RuntimeServiceGraph:
    if not isinstance(flavor, RuntimeFlavor):
        raise TypeError("runtime flavor must be a RuntimeFlavor")
    if not isinstance(features, RuntimeFeatureSet):
        raise TypeError("runtime features must be a RuntimeFeatureSet")
    services = tuple(
        service
        for service in _SERVICE_SPECS
        if service.enabled_for(flavor, features)
    )
    return RuntimeServiceGraph(flavor=flavor, features=features, services=services)


@dataclass(frozen=True, slots=True)
class RuntimeProductManifest:
    flavor: RuntimeFlavor
    features: RuntimeFeatureSet
    policy: RuntimePolicy
    service_graph: RuntimeServiceGraph
    document: Mapping[str, Any]

    @classmethod
    def from_document(cls, document: object) -> "RuntimeProductManifest":
        if not isinstance(document, Mapping):
            raise RuntimeError("Player runtime manifest must be an object")
        if document.get("$schema") != PLAYER_MANIFEST_SCHEMA:
            raise RuntimeError("Unsupported Player runtime manifest schema")
        if document.get("manifest_version") != PLAYER_MANIFEST_VERSION:
            raise RuntimeError("Unsupported Player runtime manifest version")

        product = document.get("product")
        if not isinstance(product, Mapping):
            raise RuntimeError("Player runtime product declaration is missing")
        try:
            flavor = RuntimeFlavor(str(product.get("flavor", "")))
        except ValueError as exc:
            raise RuntimeError("Player runtime flavor is invalid") from exc
        if not flavor.is_player:
            raise RuntimeError("EditorDevelopment cannot boot as a standalone Player")

        features = RuntimeFeatureSet.from_manifest(document.get("features"))
        expected_policy = runtime_policy_for(flavor)
        if document.get("runtime_policy") != expected_policy.to_manifest():
            raise RuntimeError(
                f"{flavor.value} runtime policy does not match the product service graph"
            )

        graph = runtime_service_graph_for(flavor, features)
        services = document.get("services")
        if not isinstance(services, Mapping) or services.get("kind") != "player":
            raise RuntimeError("Player service declaration is missing")
        if services.get("flavor") != flavor.value:
            raise RuntimeError("Player service graph flavor disagrees with the product")
        if services.get("editor_services") != []:
            raise RuntimeError("Player service graph contains Editor services")
        if services.get("declared") != list(graph.service_ids):
            raise RuntimeError("Player declared-service list is not authoritative")
        graph.validate_manifest_records(services.get("graph"))

        return cls(
            flavor=flavor,
            features=features,
            policy=expected_policy,
            service_graph=graph,
            document=_freeze_document(document),
        )

    def require_service(self, service_id: str) -> RuntimeServiceSpec:
        return self.service_graph.require(service_id)


@dataclass(frozen=True, slots=True)
class PlayerRuntimeAssetCatalog:
    """Immutable Player view of build-authored runtime asset identities.

    The catalog never scans ``Assets`` or invents an identity from a source
    path. A runtime scene is loadable only when it is present in the catalog
    and its cooked document exists under the packaged project root.
    """

    project_root: str
    _artifacts_by_id: Mapping[str, Mapping[str, Any]]
    _artifact_ids_by_guid: Mapping[str, tuple[str, ...]]
    _asset_paths: Mapping[str, str]
    _scene_paths: Mapping[str, str]

    @classmethod
    def from_documents(
        cls,
        project_root: str,
        catalog: object,
        asset_records: object,
    ) -> "PlayerRuntimeAssetCatalog":
        if not isinstance(catalog, Mapping):
            raise RuntimeError("Player runtime asset catalog must be an object")
        artifacts = catalog.get("artifacts")
        if not isinstance(artifacts, list):
            raise RuntimeError("Player runtime asset catalog has no artifact list")

        artifacts_by_id: dict[str, Mapping[str, Any]] = {}
        artifact_ids_by_guid: dict[str, set[str]] = {}
        scene_artifact_paths: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise RuntimeError("Player runtime asset catalog contains a malformed artifact")
            artifact_id = artifact.get("runtime_artifact_id")
            runtime_path = artifact.get("runtime_path")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise RuntimeError("Player runtime artifact has no stable identity")
            if artifact_id in artifacts_by_id:
                raise RuntimeError(f"Duplicate Player runtime artifact: {artifact_id}")
            normalized_path = cls._normalized_runtime_path(runtime_path)
            artifacts_by_id[artifact_id] = dict(
                artifact,
                runtime_path=normalized_path,
            )
            guid = artifact.get("asset_guid")
            if guid is not None:
                if not isinstance(guid, str) or not guid:
                    raise RuntimeError("Player runtime artifact has an invalid asset GUID")
                artifact_ids_by_guid.setdefault(guid, set()).add(artifact_id)
            package_name = Path(
                str(artifact.get("package", "")).replace("\\", "/")
            ).name
            if (
                artifact.get("logical_type") in {"scene", "scene_artifact"}
                and package_name == "Content.inxpkg"
            ):
                scene_artifact_paths.add(normalized_path)

        for artifact_id, artifact in artifacts_by_id.items():
            dependencies = artifact.get("dependencies", [])
            if not isinstance(dependencies, list) or any(
                not isinstance(dependency, str) or dependency not in artifacts_by_id
                for dependency in dependencies
            ):
                raise RuntimeError(
                    f"Player runtime artifact has invalid dependencies: {artifact_id}"
                )

        if not isinstance(asset_records, Mapping) or not isinstance(
            asset_records.get("entries"), list
        ):
            raise RuntimeError("Player runtime asset records have no entry list")
        seen_guids: set[str] = set()
        asset_paths: dict[str, str] = {
            path.casefold(): path
            for path in (
                str(artifact["runtime_path"])
                for artifact in artifacts_by_id.values()
            )
        }
        scene_paths: dict[str, str] = {
            path.casefold(): path for path in scene_artifact_paths
        }
        for record in asset_records["entries"]:
            if not isinstance(record, Mapping):
                raise RuntimeError("Player runtime asset record is malformed")
            guid = record.get("guid")
            primary = record.get("primary_runtime_artifact_id")
            runtime_ids = record.get("runtime_artifact_ids")
            if (
                not isinstance(guid, str)
                or not guid
                or guid in seen_guids
                or not isinstance(primary, str)
                or not isinstance(runtime_ids, list)
                or primary not in runtime_ids
                or any(not isinstance(item, str) for item in runtime_ids)
            ):
                raise RuntimeError("Player runtime asset record has incomplete identity")
            if set(runtime_ids) != artifact_ids_by_guid.get(guid, set()):
                raise RuntimeError(
                    f"Player runtime asset artifacts disagree for GUID {guid}"
                )
            dependencies = record.get("dependencies", [])
            if not isinstance(dependencies, list) or any(
                dependency not in artifacts_by_id for dependency in dependencies
            ):
                raise RuntimeError(
                    f"Player runtime asset dependencies are invalid for GUID {guid}"
                )
            primary_artifact = artifacts_by_id.get(primary)
            if primary_artifact is None:
                raise RuntimeError(
                    f"Player runtime asset primary artifact is missing for GUID {guid}"
                )
            primary_path = str(primary_artifact["runtime_path"])
            source_alias = record.get("runtime_path")
            if source_alias:
                normalized_alias = cls._normalized_runtime_path(source_alias)
                alias_key = normalized_alias.casefold()
                existing = asset_paths.get(alias_key)
                if existing is not None and existing != primary_path:
                    raise RuntimeError(
                        f"Player runtime asset path is ambiguous: {normalized_alias}"
                    )
                asset_paths[alias_key] = primary_path
            if primary_path in scene_artifact_paths:
                scene_paths[primary_path.casefold()] = primary_path
                if source_alias:
                    scene_paths[normalized_alias.casefold()] = primary_path
            seen_guids.add(guid)

        return cls(
            project_root=resolved_path(project_root),
            _artifacts_by_id=MappingProxyType(
                {
                    artifact_id: _freeze_document(artifact)
                    for artifact_id, artifact in artifacts_by_id.items()
                }
            ),
            _artifact_ids_by_guid=MappingProxyType(
                {
                    guid: tuple(sorted(artifact_ids))
                    for guid, artifact_ids in artifact_ids_by_guid.items()
                }
            ),
            _asset_paths=MappingProxyType(dict(asset_paths)),
            _scene_paths=MappingProxyType(dict(scene_paths)),
        )

    @staticmethod
    def _normalized_runtime_path(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise RuntimeError("Player runtime artifact path is missing")
        try:
            normalized = portable_relative_path(value)
        except ValueError:
            raise RuntimeError(f"Player runtime artifact path is unsafe: {value}")
        return normalized

    def artifact(self, runtime_artifact_id: str) -> Optional[Mapping[str, Any]]:
        return self._artifacts_by_id.get(str(runtime_artifact_id))

    def artifact_ids_for_guid(self, guid: str) -> tuple[str, ...]:
        return self._artifact_ids_by_guid.get(str(guid), ())

    def resolve_asset(self, reference: os.PathLike[str] | str) -> Optional[str]:
        """Resolve a cataloged source alias or cooked path to its payload."""
        raw = os.fspath(reference)
        requested = resolved_path(
            raw if os.path.isabs(raw) else os.path.join(self.project_root, raw)
        )
        if not is_path_within(requested, self.project_root, allow_root=False):
            return None
        runtime_path = relative_path(requested, self.project_root).replace("\\", "/")
        cooked_runtime_path = self._asset_paths.get(runtime_path.casefold())
        if cooked_runtime_path is None:
            return None
        candidate = resolved_path(
            os.path.join(self.project_root, *cooked_runtime_path.split("/"))
        )
        if (
            not is_path_within(candidate, self.project_root, allow_root=False)
            or not os.path.isfile(candidate)
        ):
            return None
        return candidate

    def resolve_scene(self, reference: os.PathLike[str] | str) -> Optional[str]:
        raw = os.fspath(reference)
        requested = resolved_path(
            raw if os.path.isabs(raw) else os.path.join(self.project_root, raw)
        )
        if not is_path_within(requested, self.project_root, allow_root=False):
            return None
        runtime_path = relative_path(requested, self.project_root)
        cooked_runtime_path = self._scene_paths.get(
            runtime_path.replace("\\", "/").casefold()
        )
        if cooked_runtime_path is None:
            return None
        candidate = resolved_path(
            os.path.join(self.project_root, cooked_runtime_path)
        )
        if (
            not is_path_within(candidate, self.project_root, allow_root=False)
            or not os.path.isfile(candidate)
        ):
            return None
        return candidate


def player_manifest_service_section(
    flavor: RuntimeFlavor,
    features: RuntimeFeatureSet = RuntimeFeatureSet(),
) -> dict[str, object]:
    if not flavor.is_player:
        raise ValueError("EditorDevelopment does not produce a Player service section")
    graph = runtime_service_graph_for(flavor, features)
    return {
        "kind": "player",
        "flavor": flavor.value,
        "declared": list(graph.service_ids),
        "graph": graph.to_manifest_records(),
        "editor_services": [],
    }


def player_runtime_contract_sections(
    flavor: RuntimeFlavor,
    features: RuntimeFeatureSet = RuntimeFeatureSet(),
) -> dict[str, object]:
    if not flavor.is_player:
        raise ValueError("EditorDevelopment does not produce a Player manifest")
    return {
        "product": {"flavor": flavor.value},
        "features": features.to_manifest(),
        "runtime_policy": runtime_policy_for(flavor).to_manifest(),
        "services": player_manifest_service_section(flavor, features),
    }


def forbidden_player_service_modules() -> frozenset[str]:
    return frozenset(
        service.module
        for service in _SERVICE_SPECS
        if service.authoring
    )


__all__ = [
    "LoggingPolicy",
    "PLAYER_MANIFEST_SCHEMA",
    "PLAYER_MANIFEST_VERSION",
    "PlayerRuntimeAssetCatalog",
    "ProfilingPolicy",
    "RuntimeFeatureSet",
    "RuntimeFlavor",
    "RuntimePolicy",
    "RuntimeProductManifest",
    "RuntimeServiceGraph",
    "RuntimeServiceSpec",
    "ValidationPolicy",
    "forbidden_player_service_modules",
    "player_manifest_service_section",
    "player_runtime_contract_sections",
    "runtime_policy_for",
    "runtime_service_graph_for",
]
