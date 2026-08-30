"""Official package catalog synchronization and default library installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Mapping

from Infernux.engine.path_utils import resolved_path

from .content import normalize_page_descriptor
from .manager import PluginManager, PluginState
from .package import (
    InxPackage,
    InxPackagePreview,
    PACKAGE_EXTENSION,
    validate_reference,
)
from .registry import PluginRegistry


OFFICIAL_REGISTRY_FILENAME = "official-registry.json"
DEFAULT_LIBRARIES_FILENAME = "default-libraries.json"
OFFICIAL_REGISTRY_SCHEMA = "infernux.official_plugin_registry"
DEFAULT_LIBRARIES_SCHEMA = "infernux.default_libraries"
CATALOG_VERSION = 1
_REMOTE_SOURCE_TYPES = {"git", "github", "url"}


class OfficialCatalogError(RuntimeError):
    """The optional engine-provided package catalog cannot be trusted."""


def _official_packages_root(resources_root: str | None = None) -> str:
    if resources_root:
        return os.path.join(resolved_path(resources_root), "official_packages")
    from Infernux.resources import get_package_resources_path

    return os.path.join(get_package_resources_path(), "official_packages")


def _package_resources_root(resources_root: str | None = None) -> str:
    if resources_root:
        return resolved_path(resources_root)
    from Infernux.resources import get_package_resources_path

    return get_package_resources_path()


def _read_document(path: str, schema: str) -> dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise OfficialCatalogError(
            f"Official plugin catalog is unavailable: {path}"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("$schema") != schema
        or document.get("catalog_version") != CATALOG_VERSION
    ):
        raise OfficialCatalogError(f"Unsupported official plugin catalog: {path}")
    return document


def _remote_source(raw: object, *, reference: str) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise OfficialCatalogError(
            f"Official plugin has no downloadable source: {reference}"
        )
    source = dict(raw)
    source_type = str(source.get("type", "")).strip().casefold()
    location = str(source.get("location", "")).strip()
    if source_type not in _REMOTE_SOURCE_TYPES or not location:
        raise OfficialCatalogError(
            f"Official plugin source is invalid: {reference}"
        )
    source["type"] = source_type
    source["location"] = location
    source["official"] = True
    return source


def sync_official_registry(
    project_root: str,
    *,
    resources_root: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Merge the engine's official catalog into one project registry."""

    project = resolved_path(project_root)
    official_packages = _official_packages_root(resources_root)
    document = _read_document(
        os.path.join(official_packages, OFFICIAL_REGISTRY_FILENAME),
        OFFICIAL_REGISTRY_SCHEMA,
    )
    packages = document.get("packages")
    if not isinstance(packages, list):
        raise OfficialCatalogError(
            "Official plugin registry packages must be a list"
        )
    validated: list[dict[str, object]] = []
    for raw in packages:
        if not isinstance(raw, Mapping):
            raise OfficialCatalogError(
                "Official plugin registry entry must be an object"
            )
        try:
            reference = validate_reference(str(raw.get("reference", "")).strip())
        except ValueError as exc:
            raise OfficialCatalogError(
                "Official plugin registry entry has an invalid reference"
            ) from exc
        artifact = str(raw.get("artifact", "")).strip()
        if not artifact or os.path.basename(artifact) != artifact:
            raise OfficialCatalogError("Official plugin registry entry is invalid")
        package_path = os.path.join(official_packages, artifact)
        expected_sha256 = str(raw.get("artifact_sha256", "")).strip().casefold()
        if not expected_sha256:
            raise OfficialCatalogError(
                f"Official plugin artifact hash is missing: {reference}"
            )
        if os.path.isfile(package_path):
            try:
                actual_sha256 = _file_sha256(package_path)
            except OSError as exc:
                raise OfficialCatalogError(
                    f"Official plugin artifact is unreadable: {package_path}"
                ) from exc
            if actual_sha256 != expected_sha256:
                raise OfficialCatalogError(
                    f"Official plugin artifact hash mismatch: {reference}: {package_path}"
                )
            # Development catalogs and release assembly may keep the artifact
            # beside the catalog. Prefer that exact verified blob when present.
            source = {
                "type": "local",
                "location": f"Library/Resources/official_packages/{artifact}",
                "official": True,
                "sha256": expected_sha256,
            }
        else:
            # Host wheels intentionally carry only their direct built-in
            # packages. Other official entries remain discoverable and are
            # acquired on demand from their catalog-owned source.
            source = _remote_source(raw.get("source"), reference=reference)
            source["release_sha256"] = expected_sha256
            source["reference"] = reference
        repository = str(raw.get("repository", "")).strip()
        if repository:
            source["repository"] = repository
        dependencies = raw.get("dependencies", [])
        pages = raw.get("pages", [])
        intros = raw.get("intros", {})
        targets = raw.get("targets", [])
        if (
            not isinstance(dependencies, list)
            or not isinstance(pages, list)
            or not isinstance(intros, Mapping)
            or not isinstance(targets, list)
        ):
            raise OfficialCatalogError(
                f"Official plugin registry entry has invalid content metadata: {reference}"
            )
        try:
            normalized_dependencies = [
                validate_reference(str(item)) for item in dependencies
            ]
        except ValueError as exc:
            raise OfficialCatalogError(
                f"Official plugin dependencies are invalid: {reference}"
            ) from exc
        try:
            normalized_pages = [normalize_page_descriptor(item) for item in pages]
        except ValueError as exc:
            raise OfficialCatalogError(
                f"Official plugin pages are invalid: {reference}"
            ) from exc
        validated.append(
            {
                "reference": reference,
                "name": str(raw.get("name", reference)),
                "version": str(raw.get("version", "")),
                "engine": str(raw.get("engine", "")),
                "dependencies": normalized_dependencies,
                "intro": str(raw.get("intro", "")),
                "intros": dict(intros),
                "pages": normalized_pages,
                "category": str(raw.get("category", "Other")),
                "targets": [str(value) for value in targets],
                "source": source,
            }
        )

    # Do not alter the project registry until every official artifact and
    # catalog entry has passed validation.
    registry = PluginRegistry(project)
    current = registry.load()
    current["packages"] = [
        item
        for item in current["packages"]
        if not (
            isinstance(item, Mapping)
            and isinstance(item.get("source"), Mapping)
            and bool(item["source"].get("official", False))
        )
    ]
    registry.save(current)
    added: list[dict[str, object]] = []
    for item in validated:
        added.append(
            registry.add_package(
                str(item["reference"]),
                name=str(item["name"]),
                version=str(item["version"]),
                engine=str(item["engine"]),
                dependencies=item["dependencies"],
                intro=str(item["intro"]),
                intros=item["intros"],
            pages=item["pages"],
            category=str(item["category"]),
            targets=item["targets"],
            source=item["source"],
            )
        )
    return tuple(added)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_bundled_packages(
    project_root: str,
    *,
    resources_root: str | None = None,
    manager: PluginManager | None = None,
) -> tuple[PluginState, ...]:
    """Install every wheel-mandatory InxPackage from the resources root.

    A direct child named ``*.inxpkg`` is part of the host wheel's built-in
    package set.  The set is authoritative and local: startup never replaces
    a missing or incompatible artifact with a network download.
    """

    project = resolved_path(project_root)
    root = _package_resources_root(resources_root)
    try:
        candidates = sorted(
            (
                os.path.join(root, name)
                for name in os.listdir(root)
                if name.casefold().endswith(PACKAGE_EXTENSION)
                and os.path.isfile(os.path.join(root, name))
            ),
            key=lambda path: os.path.basename(path).casefold(),
        )
    except OSError as exc:
        raise OfficialCatalogError(
            f"Built-in package resources are unavailable: {root}"
        ) from exc

    previews: list[tuple[str, InxPackagePreview, str]] = []
    references: dict[str, str] = {}
    for package_path in candidates:
        try:
            preview = InxPackage.inspect(package_path)
            reference = validate_reference(str(preview.metadata.get("reference", "")))
            package_sha256 = _file_sha256(package_path)
        except Exception as exc:
            raise OfficialCatalogError(
                f"Built-in InxPackage is invalid: {package_path}"
            ) from exc
        key = reference.casefold()
        if key in references:
            raise OfficialCatalogError(
                "Built-in package reference is duplicated: "
                f"{reference}: {references[key]}, {package_path}"
            )
        references[key] = package_path
        previews.append((package_path, preview, package_sha256))

    active = PluginManager.instance()
    if (
        manager is None
        and active is not None
        and active.project_root == project
        and not active.runtime
    ):
        manager = active
    owned_manager = manager is None
    manager = manager or PluginManager(project, runtime=False)
    if manager.project_root != project:
        raise ValueError("Built-in package manager belongs to another project")

    try:
        # Publish the complete local set before installation so one built-in
        # package can resolve another regardless of filename ordering.
        for package_path, preview, package_sha256 in previews:
            metadata = preview.metadata
            manager.registry.add_package(
                str(metadata["reference"]),
                name=str(metadata.get("name", "")),
                version=str(metadata.get("version", "")),
                engine=str(metadata.get("engine", "")),
                dependencies=metadata.get("dependencies", ()),
                intro=str(metadata.get("intro", "")),
                intros=metadata.get("intros", {}),
                pages=metadata.get("pages", ()),
                source={
                    "type": "local",
                    "location": package_path,
                    "sha256": package_sha256,
                    "builtin": True,
                },
            )
        return tuple(
            manager.install_reference(str(preview.metadata["reference"]))
            for _package_path, preview, _package_sha256 in previews
        )
    finally:
        if owned_manager:
            manager.shutdown()


def install_default_libraries(
    project_root: str,
    *,
    resources_root: str | None = None,
    manager: PluginManager | None = None,
) -> tuple[PluginState, ...]:
    """Install the ordered default plugin list into a newly created project."""

    project = resolved_path(project_root)
    sync_official_registry(project, resources_root=resources_root)
    defaults = _read_document(
        os.path.join(
            _official_packages_root(resources_root), DEFAULT_LIBRARIES_FILENAME
        ),
        DEFAULT_LIBRARIES_SCHEMA,
    )
    references = defaults.get("libraries")
    if not isinstance(references, list) or any(not isinstance(item, str) for item in references):
        raise OfficialCatalogError("Default library list must contain references")
    active = PluginManager.instance()
    if (
        manager is None
        and active is not None
        and active.project_root == project
        and not active.runtime
    ):
        manager = active
    owned_manager = manager is None
    manager = manager or PluginManager(project, runtime=False)
    if manager.project_root != project:
        raise ValueError("Default library manager belongs to another project")
    installed = {
        str(item.get("reference", "")).casefold()
        for item in manager.registry.installed()
    }
    states: list[PluginState] = []
    try:
        for reference in references:
            key = reference.strip().casefold()
            if not key:
                raise OfficialCatalogError("Default library reference cannot be empty")
            if key in installed:
                states.append(manager.reload(reference))
            else:
                states.append(manager.install_reference(reference))
                installed.add(key)
        return tuple(states)
    finally:
        if owned_manager:
            manager.shutdown()


def bootstrap_new_project(project_root: str) -> tuple[PluginState, ...]:
    """Mirror this engine's resources, then install its default core plugins."""

    from Infernux.engine.library_sync import sync_resources

    sync_resources(project_root)
    manager = PluginManager(project_root, runtime=False)
    try:
        install_bundled_packages(project_root, manager=manager)
        return install_default_libraries(project_root, manager=manager)
    finally:
        manager.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    states = bootstrap_new_project(args.project)
    failed = [state for state in states if not state.loaded]
    if failed:
        details = ", ".join(f"{state.reference}: {state.error}" for state in failed)
        raise RuntimeError(f"Default plugin preload failed: {details}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "bootstrap_new_project",
    "install_bundled_packages",
    "install_default_libraries",
    "OfficialCatalogError",
    "sync_official_registry",
]
