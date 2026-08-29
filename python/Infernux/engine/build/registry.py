"""Process-local ownership registry for platform build exporters."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from .contracts import (
    BUILD_EXPORTER_CONTRACT_VERSION,
    BuildTarget,
    BuildTargetId,
    PlatformExporter,
)


@dataclass(frozen=True, slots=True)
class ExporterRegistration:
    token: str
    owner: str
    exporter_id: str
    target_ids: tuple[BuildTargetId, ...]


@dataclass(frozen=True, slots=True)
class _RegisteredExporter:
    registration: ExporterRegistration
    exporter: PlatformExporter
    targets: tuple[BuildTarget, ...]


class BuildExporterRegistry:
    """Thread-safe registry with atomic registration and owner cleanup."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._exporters: dict[str, _RegisteredExporter] = {}
        self._targets: dict[BuildTargetId, _RegisteredExporter] = {}
        self._tokens: dict[str, _RegisteredExporter] = {}

    def register(
        self,
        owner: str,
        exporter: PlatformExporter,
    ) -> ExporterRegistration:
        identifier = str(owner or "").strip()
        if not identifier:
            raise ValueError("Exporter registration requires an owner")
        if not isinstance(exporter, PlatformExporter):
            raise TypeError("exporter must implement PlatformExporter")
        if int(exporter.contract_version) != BUILD_EXPORTER_CONTRACT_VERSION:
            raise RuntimeError(
                "Unsupported build exporter contract version: "
                f"{exporter.contract_version}"
            )
        exporter_id = str(exporter.exporter_id or "").strip()
        if not exporter_id:
            raise ValueError("PlatformExporter.exporter_id is required")
        targets = tuple(exporter.targets())
        if not targets:
            raise ValueError("Platform exporters must contribute at least one target")
        target_ids = tuple(BuildTargetId(item.id) for item in targets)
        if len(set(target_ids)) != len(target_ids):
            raise ValueError(f"Exporter {exporter_id!r} contains duplicate targets")

        with self._lock:
            if exporter_id in self._exporters:
                raise RuntimeError(f"Build exporter is already registered: {exporter_id}")
            collisions = [target for target in target_ids if target in self._targets]
            if collisions:
                names = ", ".join(sorted(collisions))
                raise RuntimeError(f"Build targets are already registered: {names}")
            registration = ExporterRegistration(
                uuid.uuid4().hex,
                identifier,
                exporter_id,
                target_ids,
            )
            entry = _RegisteredExporter(registration, exporter, targets)
            self._exporters[exporter_id] = entry
            self._tokens[registration.token] = entry
            for target in target_ids:
                self._targets[target] = entry
            return registration

    def unregister(self, registration: ExporterRegistration | str) -> bool:
        token = (
            registration.token
            if isinstance(registration, ExporterRegistration)
            else str(registration or "")
        )
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return False
            self._remove(entry)
            return True

    def unregister_owner(self, owner: str) -> int:
        identifier = str(owner or "").strip()
        with self._lock:
            entries = tuple(
                entry
                for entry in self._exporters.values()
                if entry.registration.owner == identifier
            )
            for entry in entries:
                self._remove(entry)
            return len(entries)

    def targets(self) -> tuple[BuildTarget, ...]:
        with self._lock:
            result = [
                target
                for entry in self._exporters.values()
                for target in entry.targets
            ]
        return tuple(sorted(result, key=lambda item: item.id))

    def resolve(
        self, target: BuildTargetId | str
    ) -> tuple[PlatformExporter, BuildTarget]:
        target_id = BuildTargetId(target)
        with self._lock:
            entry = self._targets.get(target_id)
            if entry is None:
                raise KeyError(f"Unknown build target: {target_id}")
            descriptor = next(item for item in entry.targets if item.id == target_id)
            return entry.exporter, descriptor

    def clear(self) -> None:
        with self._lock:
            self._exporters.clear()
            self._targets.clear()
            self._tokens.clear()

    def _remove(self, entry: _RegisteredExporter) -> None:
        self._exporters.pop(entry.registration.exporter_id, None)
        self._tokens.pop(entry.registration.token, None)
        for target in entry.registration.target_ids:
            if self._targets.get(target) is entry:
                self._targets.pop(target, None)


exporter_registry = BuildExporterRegistry()


__all__ = [
    "BuildExporterRegistry",
    "ExporterRegistration",
    "exporter_registry",
]
