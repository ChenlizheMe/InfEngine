from __future__ import annotations

from enum import Enum
from os import PathLike
from typing import Any, Mapping, Optional

PLAYER_MANIFEST_SCHEMA: str

class RuntimeFlavor(str, Enum):
    EDITOR_DEVELOPMENT: RuntimeFlavor
    PLAYER_DEBUG: RuntimeFlavor
    PLAYER_RELEASE: RuntimeFlavor
    @property
    def is_player(self) -> bool: ...

class ValidationPolicy(str, Enum):
    AUTHORING: ValidationPolicy
    DIAGNOSTIC: ValidationPolicy
    RELEASE: ValidationPolicy

class ProfilingPolicy(str, Enum):
    AUTHORING: ProfilingPolicy
    AVAILABLE: ProfilingPolicy
    MINIMAL: ProfilingPolicy

class LoggingPolicy(str, Enum):
    DEBUG: LoggingPolicy
    INFO: LoggingPolicy

class RuntimeFeatureSet:
    jit: bool
    parallel: bool
    optional_subsystems: tuple[str, ...]
    def __init__(
        self,
        jit: bool = False,
        parallel: bool = False,
        optional_subsystems: tuple[str, ...] = (),
    ) -> None: ...
    @classmethod
    def from_manifest(cls, value: object) -> RuntimeFeatureSet: ...
    def to_manifest(self) -> dict[str, object]: ...

class RuntimePolicy:
    validation: ValidationPolicy
    profiling: ProfilingPolicy
    logging: LoggingPolicy
    player_control: str
    authoring_services: bool
    def to_manifest(self) -> dict[str, object]: ...

class RuntimeServiceSpec:
    service_id: str
    module: str
    dependencies: tuple[str, ...]
    feature: Optional[str]
    retention_reason: str
    authoring: bool
    def to_manifest(self) -> dict[str, object]: ...

class RuntimeServiceGraph:
    flavor: RuntimeFlavor
    features: RuntimeFeatureSet
    services: tuple[RuntimeServiceSpec, ...]
    @property
    def service_ids(self) -> tuple[str, ...]: ...
    def contains(self, service_id: str) -> bool: ...
    def require(self, service_id: str) -> RuntimeServiceSpec: ...
    def to_manifest_records(self) -> list[dict[str, object]]: ...
    def validate_manifest_records(self, value: object) -> None: ...

class RuntimeProductManifest:
    flavor: RuntimeFlavor
    features: RuntimeFeatureSet
    policy: RuntimePolicy
    service_graph: RuntimeServiceGraph
    document: Mapping[str, Any]
    @classmethod
    def from_document(cls, document: object) -> RuntimeProductManifest: ...
    def require_service(self, service_id: str) -> RuntimeServiceSpec: ...

class PlayerRuntimeAssetCatalog:
    project_root: str
    @classmethod
    def from_documents(
        cls,
        project_root: str,
        catalog: object,
        asset_records: object,
    ) -> PlayerRuntimeAssetCatalog: ...
    def artifact(self, runtime_artifact_id: str) -> Optional[Mapping[str, Any]]: ...
    def artifact_ids_for_guid(self, guid: str) -> tuple[str, ...]: ...
    def resolve_scene(self, reference: PathLike[str] | str) -> Optional[str]: ...

def runtime_policy_for(flavor: RuntimeFlavor) -> RuntimePolicy: ...
def runtime_service_graph_for(
    flavor: RuntimeFlavor,
    features: RuntimeFeatureSet = ...,
) -> RuntimeServiceGraph: ...
def player_manifest_service_section(
    flavor: RuntimeFlavor,
    features: RuntimeFeatureSet = ...,
) -> dict[str, object]: ...
def player_runtime_contract_sections(
    flavor: RuntimeFlavor,
    features: RuntimeFeatureSet = ...,
) -> dict[str, object]: ...
def forbidden_player_service_modules() -> frozenset[str]: ...
