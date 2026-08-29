"""Platform-neutral contracts shared by build frontends and exporters."""

from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from Infernux.engine.build_cancellation import BuildCancelled


BUILD_EXPORTER_CONTRACT_VERSION = 1
_TARGET_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BuildTargetId(str):
    """Stable, portable identifier for one Player build target."""

    def __new__(cls, value: str) -> "BuildTargetId":
        normalized = str(value or "").strip()
        if not _TARGET_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Build target IDs must be lowercase dash-separated tokens: "
                f"{value!r}"
            )
        return str.__new__(cls, normalized)


class BuildConfiguration(str, Enum):
    DEVELOPMENT = "development"
    RELEASE = "release"


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    graphics_api: str
    threads: bool = True
    dynamic_loading: bool = True
    filesystem: bool = True
    network: bool = True
    audio: bool = True
    pointer_input: bool = True
    text_input: bool = True
    gamepad_input: bool = True
    python_native_modules: bool = True
    numba: bool = True
    persistent_storage: bool = True
    features: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        graphics_api = str(self.graphics_api or "").strip().casefold()
        if graphics_api not in {"vulkan", "webgpu"}:
            raise ValueError(
                "Infernux platform targets support only Vulkan or WebGPU"
            )
        object.__setattr__(self, "graphics_api", graphics_api)
        object.__setattr__(
            self,
            "features",
            frozenset(str(item).strip() for item in self.features if str(item).strip()),
        )


@dataclass(frozen=True, slots=True)
class BuildTarget:
    id: BuildTargetId
    display_name: str
    platform: str
    architecture: str
    capabilities: PlatformCapabilities

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", BuildTargetId(self.id))
        for field_name in ("display_name", "platform", "architecture"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"BuildTarget.{field_name} is required")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class BuildProfile:
    configuration: BuildConfiguration = BuildConfiguration.DEVELOPMENT
    debug_symbols: bool = True
    compress_resources: bool = False
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "configuration", BuildConfiguration(self.configuration)
        )
        object.__setattr__(self, "options", _frozen_mapping(self.options))


class BuildCancellationToken:
    """Thread-safe cancellation signal shared by every build frontend."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise BuildCancelled("Build cancelled")


@dataclass(frozen=True, slots=True)
class BuildProgress:
    phase: str
    completed: int
    total: int
    message: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        phase = str(self.phase or "").strip()
        if not phase:
            raise ValueError("BuildProgress.phase is required")
        completed = int(self.completed)
        total = int(self.total)
        if total < 0 or completed < 0 or (total and completed > total):
            raise ValueError("Build progress counters are invalid")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "completed", completed)
        object.__setattr__(self, "total", total)
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "detail", _frozen_mapping(self.detail))


ProgressCallback = Callable[[BuildProgress], None]


@dataclass(frozen=True, slots=True)
class BuildRequest:
    project_root: str
    target: BuildTargetId
    output_dir: str
    profile: BuildProfile = field(default_factory=BuildProfile)
    cancellation: BuildCancellationToken = field(
        default_factory=BuildCancellationToken,
        compare=False,
        repr=False,
    )
    progress: ProgressCallback | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        project_root = str(self.project_root or "").strip()
        output_dir = str(self.output_dir or "").strip()
        if not project_root:
            raise ValueError("BuildRequest.project_root is required")
        if not output_dir:
            raise ValueError("BuildRequest.output_dir is required")
        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "output_dir", output_dir)
        object.__setattr__(self, "target", BuildTargetId(self.target))

    def report(
        self,
        phase: str,
        completed: int,
        total: int,
        message: str,
        **detail: object,
    ) -> None:
        self.cancellation.raise_if_cancelled()
        if self.progress is not None:
            self.progress(
                BuildProgress(phase, completed, total, message, detail)
            )


@dataclass(frozen=True, slots=True)
class BuildDiagnostic:
    severity: DiagnosticSeverity
    code: str
    message: str
    source: str = ""
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", DiagnosticSeverity(self.severity))
        object.__setattr__(self, "code", str(self.code or "").strip())
        object.__setattr__(self, "message", str(self.message or "").strip())
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "detail", _frozen_mapping(self.detail))
        if not self.code or not self.message:
            raise ValueError("Build diagnostics require a code and message")


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    available: bool
    diagnostics: tuple[BuildDiagnostic, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        diagnostics = tuple(self.diagnostics)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "details", _frozen_mapping(self.details))
        if self.available and any(
            item.severity is DiagnosticSeverity.ERROR for item in diagnostics
        ):
            raise ValueError("An available capability report cannot contain errors")


@dataclass(frozen=True, slots=True)
class BuildStep:
    id: str
    title: str
    phase: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("id", "title", "phase"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"BuildStep.{field_name} is required")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "detail", _frozen_mapping(self.detail))


@dataclass(frozen=True, slots=True)
class BuildPlan:
    target: BuildTargetId
    steps: tuple[BuildStep, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", BuildTargetId(self.target))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    path: str
    kind: str
    sha256: str = ""
    size: int = 0

    def __post_init__(self) -> None:
        path = str(self.path or "").strip()
        kind = str(self.kind or "").strip()
        if not path or not kind or int(self.size) < 0:
            raise ValueError("Build artifact path, kind, and size are invalid")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "sha256", str(self.sha256 or "").strip())
        object.__setattr__(self, "size", int(self.size))


@dataclass(frozen=True, slots=True)
class BuildResult:
    target: BuildTargetId
    success: bool
    artifacts: tuple[BuildArtifact, ...] = ()
    diagnostics: tuple[BuildDiagnostic, ...] = ()
    manifest: Mapping[str, object] = field(default_factory=dict)
    logs: tuple[str, ...] = ()
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", BuildTargetId(self.target))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "manifest", _frozen_mapping(self.manifest))
        object.__setattr__(self, "logs", tuple(str(item) for item in self.logs))
        object.__setattr__(self, "elapsed_seconds", float(self.elapsed_seconds))
        if self.elapsed_seconds < 0:
            raise ValueError("BuildResult.elapsed_seconds cannot be negative")


class PlatformExporter(ABC):
    """Versioned interface implemented by core and InxPackage exporters."""

    contract_version = BUILD_EXPORTER_CONTRACT_VERSION

    @property
    @abstractmethod
    def exporter_id(self) -> str:
        """Return a stable reverse-DNS or package-style exporter identity."""

    @abstractmethod
    def targets(self) -> Sequence[BuildTarget]:
        """Return all targets contributed by this exporter."""

    @abstractmethod
    def doctor(self, request: BuildRequest) -> CapabilityReport:
        """Inspect the host toolchain without mutating the project."""

    @abstractmethod
    def create_plan(self, request: BuildRequest) -> BuildPlan:
        """Create an inspectable plan before performing build mutations."""

    @abstractmethod
    def execute(self, request: BuildRequest, plan: BuildPlan) -> BuildResult:
        """Execute an accepted plan and atomically publish its artifacts."""

    def audit(self, request: BuildRequest, result: BuildResult) -> BuildResult:
        """Validate the produced package; exporters may return richer results."""

        return result

    def smoke(self, request: BuildRequest, result: BuildResult) -> BuildResult:
        """Optionally run a target-specific launch or installation smoke test."""

        return result


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


__all__ = [
    "BUILD_EXPORTER_CONTRACT_VERSION",
    "BuildArtifact",
    "BuildCancellationToken",
    "BuildConfiguration",
    "BuildDiagnostic",
    "BuildPlan",
    "BuildProfile",
    "BuildProgress",
    "BuildRequest",
    "BuildResult",
    "BuildStep",
    "BuildTarget",
    "BuildTargetId",
    "CapabilityReport",
    "DiagnosticSeverity",
    "PlatformCapabilities",
    "PlatformExporter",
    "ProgressCallback",
]
