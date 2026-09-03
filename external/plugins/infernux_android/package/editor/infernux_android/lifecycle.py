"""Register Android targets while the InxPackage is enabled."""

from __future__ import annotations

from Infernux.engine.build import ExporterRegistration, exporter_registry
from Infernux.lifecycle import InxPreload, PreloadContext

from .exporter import AndroidPlatformExporter


class InfernuxAndroidPreload(InxPreload):
    def __init__(self) -> None:
        self._registration: ExporterRegistration | None = None

    def preload(self, context: PreloadContext) -> None:
        if context.runtime:
            return
        owner = context.package_reference or f"preload:{context.script_guid}"
        self._registration = exporter_registry.register(
            owner,
            AndroidPlatformExporter(),
        )

    def unload(self) -> None:
        if self._registration is None:
            return
        exporter_registry.unregister(self._registration)
        self._registration = None


__all__ = ["InfernuxAndroidPreload"]
