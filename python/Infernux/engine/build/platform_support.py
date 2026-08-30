"""Read-only discovery of platform packages that can provide build targets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from Infernux.engine.path_utils import portable_path, resolved_path
from Infernux.plugins.cache import PackageBlobCache
from Infernux.plugins.manager import PluginManager
from Infernux.plugins.registry import PluginRegistry

from .registry import exporter_registry


@dataclass(frozen=True, slots=True)
class PlatformSupport:
    target_id: str
    package_reference: str
    package_name: str
    installed: bool
    enabled: bool
    registered: bool
    cached: bool
    source: Mapping[str, object]


def _cached_source_exists(source: Mapping[str, object]) -> bool:
    location = portable_path(str(source.get("cache_location", ""))).strip("/")
    if not location:
        return False
    if str(source.get("cache_scope", "")).casefold() != "hub":
        return False
    path = resolved_path(
        os.path.join(PackageBlobCache().root, *location.split("/"))
    )
    return os.path.isfile(path)


def platform_support_catalog(
    project_root: str = "", *, manager: PluginManager | None = None
) -> tuple[PlatformSupport, ...]:
    """Return known platform targets without registering imaginary exporters."""

    active = manager or PluginManager.instance()
    root = resolved_path(project_root)
    if active is not None and (not root or active.project_root == root):
        registry = active.registry
        states = active.states
    elif root:
        registry = PluginRegistry(root)
        states = {}
    else:
        return ()

    installed = {
        str(item.get("reference", "")).casefold(): item
        for item in registry.installed()
    }
    registered = {str(item.id) for item in exporter_registry.targets()}
    result: list[PlatformSupport] = []
    for item in registry.available():
        if str(item.get("category", "")).casefold() != "platform":
            continue
        reference = str(item.get("reference", "")).strip()
        if not reference:
            continue
        record = installed.get(reference.casefold())
        state = states.get(reference.casefold())
        enabled = bool(record is not None and record.get("enabled", True))
        if state is not None:
            enabled = bool(state.enabled)
        source = item.get("source")
        source = dict(source) if isinstance(source, Mapping) else {}
        for target_id in item.get("targets", ()):
            identifier = str(target_id).strip()
            if not identifier:
                continue
            result.append(
                PlatformSupport(
                    target_id=identifier,
                    package_reference=reference,
                    package_name=str(item.get("name", reference)),
                    installed=record is not None,
                    enabled=enabled,
                    registered=identifier in registered,
                    cached=_cached_source_exists(source),
                    source=source,
                )
            )
    return tuple(sorted(result, key=lambda item: item.target_id.casefold()))


def required_platform_plugin(
    target_id: str, project_root: str = "", *, manager: PluginManager | None = None
) -> PlatformSupport | None:
    identifier = str(target_id).strip().casefold()
    return next(
        (
            item
            for item in platform_support_catalog(project_root, manager=manager)
            if item.target_id.casefold() == identifier and not item.registered
        ),
        None,
    )


__all__ = [
    "PlatformSupport",
    "platform_support_catalog",
    "required_platform_plugin",
]
