"""Public build exporter API for core adapters and InxPackage plugins."""

from .contracts import *
from .contracts import __all__ as _contract_exports
from .registry import (
    BuildExporterRegistry,
    ExporterRegistration,
    exporter_registry,
)
from .service import BuildService, BuildUnavailableError, build_service
from .host_player_export import current_host_player_target
from .platform_support import (
    PlatformSupport,
    platform_support_catalog,
    required_platform_plugin,
)
from .progress import build_progress_fraction

__all__ = [
    *_contract_exports,
    "BuildExporterRegistry",
    "BuildService",
    "BuildUnavailableError",
    "ExporterRegistration",
    "build_service",
    "build_progress_fraction",
    "current_host_player_target",
    "exporter_registry",
    "PlatformSupport",
    "platform_support_catalog",
    "required_platform_plugin",
]
