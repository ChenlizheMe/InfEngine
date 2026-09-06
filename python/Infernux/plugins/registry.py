"""Project-local catalog and GUID ownership ledger for InxPackages."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Iterable, Mapping

from packaging.utils import canonicalize_name

from Infernux.core.document_store import write_document_text
from Infernux.engine.path_utils import resolved_path

from .content import normalize_locale, normalize_page_descriptor
from .package import validate_reference


REGISTRY_RELATIVE_PATH = os.path.join("ProjectSettings", "InxPlugins.json")
LOCK_RELATIVE_PATH = os.path.join("ProjectSettings", "InxPackages.lock.json")
REGISTRY_SCHEMA = "infernux.plugin_registry"


class PluginRegistry:
    def __init__(self, project_root: str) -> None:
        self.project_root = resolved_path(project_root)
        if not self.project_root:
            raise ValueError("PluginRegistry requires a project root")
        self.path = os.path.join(self.project_root, REGISTRY_RELATIVE_PATH)
        self.lock_path = os.path.join(self.project_root, LOCK_RELATIVE_PATH)

    def load(self) -> dict[str, object]:
        if not os.path.isfile(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Plugin registry is unreadable: {self.path}") from exc
        if (
            not isinstance(value, dict)
            or value.get("$schema") != REGISTRY_SCHEMA
            or set(value) != {
                "$schema", "packages", "installed", "python_installs",
                "python_dependencies",
            }
        ):
            raise ValueError("Unsupported plugin registry schema")
        if (
            not isinstance(value.get("packages"), list)
            or not isinstance(value.get("installed"), list)
            or not isinstance(value.get("python_installs"), list)
            or not isinstance(value.get("python_dependencies"), list)
        ):
            raise ValueError("Plugin registry catalog, install, and Python fields must be lists")
        self._validate_installed(value["installed"])
        _validate_python_dependencies(value["python_dependencies"])
        return value

    def save(self, value: Mapping[str, object]) -> None:
        document = dict(value)
        document["$schema"] = REGISTRY_SCHEMA
        if set(document) != {
            "$schema", "packages", "installed", "python_installs",
            "python_dependencies",
        }:
            raise ValueError("Unsupported plugin registry schema")
        if (
            not isinstance(document["packages"], list)
            or not isinstance(document["installed"], list)
            or not isinstance(document["python_installs"], list)
            or not isinstance(document["python_dependencies"], list)
        ):
            raise ValueError("Plugin registry catalog, install, and Python fields must be lists")
        self._validate_installed(document["installed"])
        _validate_python_dependencies(document["python_dependencies"])
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Registry readers include the UI, script scanner and preload workers.
        # Use the shared IO service's durable publication and Windows sharing
        # semantics instead of a separate, immediate os.replace implementation.
        write_document_text(
            self.path, json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        )
        self._write_lock(document)

    def available(self) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(item) for item in self.load()["packages"] if isinstance(item, dict)
        )

    def installed(self) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(item) for item in self.load()["installed"] if isinstance(item, dict)
        )

    def find(self, reference: str) -> dict[str, object] | None:
        key = validate_reference(reference).casefold()
        return next(
            (
                item
                for item in self.available()
                if str(item.get("reference", "")).casefold() == key
            ),
            None,
        )

    def installed_record(self, reference: str) -> dict[str, object] | None:
        key = validate_reference(reference).casefold()
        return next(
            (
                item
                for item in self.installed()
                if str(item.get("reference", "")).casefold() == key
            ),
            None,
        )

    def owner_for_guid(self, guid: str) -> tuple[dict[str, object], dict[str, object]] | None:
        key = str(guid).casefold()
        for package in self.installed():
            for item in package.get("files", []):
                if isinstance(item, dict) and str(item.get("guid", "")).casefold() == key:
                    return package, dict(item)
            control = package.get("control")
            if isinstance(control, dict) and str(control.get("guid", "")).casefold() == key:
                return package, dict(control)
        return None

    def users_for_guid(
        self, guid: str, *, excluding: str = ""
    ) -> tuple[tuple[dict[str, object], dict[str, object]], ...]:
        """Return every installed package record that references ``guid``."""

        key = str(guid).casefold()
        excluded = str(excluding).casefold()
        result: list[tuple[dict[str, object], dict[str, object]]] = []
        for package in self.installed():
            if str(package.get("reference", "")).casefold() == excluded:
                continue
            for item in [*package.get("files", []), package.get("control")]:
                if isinstance(item, dict) and str(item.get("guid", "")).casefold() == key:
                    result.append((package, dict(item)))
        return tuple(result)

    @staticmethod
    def build_package_entry(
        reference: str,
        *,
        intro: str = "",
        intros: Mapping[str, object] | None = None,
        source: Mapping[str, object],
        version: str = "",
        engine: str = "",
        dependencies: Iterable[str] = (),
        name: str = "",
        pages: Iterable[Mapping[str, object] | str] = (),
        category: str = "Other",
        targets: Iterable[str] = (),
    ) -> dict[str, object]:
        """Normalize one catalog entry exactly as add_package() would store it."""

        reference = validate_reference(reference)
        source_value = dict(source)
        if not str(source_value.get("type", "")).strip() or not str(
            source_value.get("location", "")
        ).strip():
            raise ValueError("Registry package requires a typed source with a location")
        return {
            "reference": reference,
            "name": str(name or reference),
            "intro": str(intro),
            "intros": _normalize_intros(intros or {}),
            "version": str(version),
            "engine": str(engine),
            "dependencies": sorted({validate_reference(item) for item in dependencies}),
            "source": source_value,
            "pages": [normalize_page_descriptor(page) for page in pages],
            "category": str(category or "Other"),
            "targets": sorted({str(target) for target in targets if str(target)}),
        }

    def add_package(
        self,
        reference: str,
        *,
        intro: str = "",
        intros: Mapping[str, object] | None = None,
        source: Mapping[str, object],
        version: str = "",
        engine: str = "",
        dependencies: Iterable[str] = (),
        name: str = "",
        pages: Iterable[Mapping[str, object] | str] = (),
        category: str = "Other",
        targets: Iterable[str] = (),
    ) -> dict[str, object]:
        item = self.build_package_entry(
            reference,
            intro=intro,
            intros=intros,
            source=source,
            version=version,
            engine=engine,
            dependencies=dependencies,
            name=name,
            pages=pages,
            category=category,
            targets=targets,
        )
        reference = str(item["reference"])
        document = self.load()
        packages = [
            entry
            for entry in document["packages"]
            if str(entry.get("reference", "")).casefold() != reference.casefold()
        ]
        packages.append(item)
        document["packages"] = sorted(
            packages, key=lambda entry: str(entry.get("reference", "")).casefold()
        )
        self.save(document)
        return item

    def record_install(
        self,
        metadata: Mapping[str, object],
        *,
        files: Iterable[Mapping[str, object]],
        control: Mapping[str, object],
        package_path: str = "",
        source: Mapping[str, object] | None = None,
        dependencies: Iterable[str] = (),
        enabled: bool = True,
        transaction_id: str = "",
        python_requirements: Iterable[Mapping[str, object]] = (),
        python_changes: Iterable[Mapping[str, object]] = (),
        python_install: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        reference = validate_reference(str(metadata.get("reference", "")))
        normalized_python_requirements = _normalize_python_requirements(
            python_requirements
        )
        normalized_python_changes = _normalize_python_changes(python_changes)
        item = {
            "reference": reference,
            "name": str(metadata.get("name", reference)),
            "version": str(metadata.get("version", "")),
            "intro": str(metadata.get("intro", "")),
            "intros": _normalize_intros(metadata.get("intros", {})),
            "requirements": str(metadata.get("requirements", "requirements.txt")),
            "engine": str(metadata.get("engine", "")),
            "pages": [
                normalize_page_descriptor(page) for page in metadata.get("pages", [])
            ],
            "package_path": str(package_path),
            "source": dict(source or {}),
            "dependencies": sorted({validate_reference(item) for item in dependencies}),
            "transaction_id": str(transaction_id or uuid.uuid4().hex),
            "installed_at": time.time(),
            "enabled": bool(enabled),
            "python_requirements": normalized_python_requirements,
            "python_changes": normalized_python_changes,
            "files": [dict(file) for file in files],
            "control": dict(control),
        }
        document = self.load()
        names = {requirement["name"] for requirement in normalized_python_requirements}
        # The manager has released dropped requirements before replacing this
        # installed owner. Other packages and their baseline versions stay intact.
        for dependency in document["python_dependencies"]:
            if str(dependency["name"]) not in names:
                dependency["owners"] = [owner for owner in dependency.get("owners", [])
                                        if str(owner.get("reference", "")).casefold() != reference.casefold()]
        document["python_dependencies"] = [dependency for dependency in document["python_dependencies"]
                                           if dependency.get("owners")]
        _register_python_dependency_owner(
            document,
            reference,
            normalized_python_requirements,
            normalized_python_changes,
        )
        if python_install:
            evidence = dict(python_install)
            evidence["owner"] = reference
            evidence.setdefault("transaction_id", uuid.uuid4().hex)
            evidence.setdefault("installed_at", time.time())
            evidence["requirements"] = [
                item["requirement"] for item in normalized_python_requirements
            ]
            evidence["changes"] = normalized_python_changes
            document["python_installs"].append(evidence)
        if source:
            catalog_entry = {
                "reference": reference,
                "name": str(metadata.get("name", reference)),
                "version": str(metadata.get("version", "")),
                "intro": str(metadata.get("intro", "")),
                "intros": _normalize_intros(metadata.get("intros", {})),
                "source": dict(source),
                "pages": [
                    normalize_page_descriptor(page)
                    for page in metadata.get("pages", [])
                ],
            }
            if not any(
                str(entry.get("reference", "")).casefold() == reference.casefold()
                for entry in document["packages"]
            ):
                document["packages"].append(catalog_entry)
                document["packages"] = sorted(
                    document["packages"],
                    key=lambda entry: str(entry.get("reference", "")).casefold(),
                )
        installed = [
            entry
            for entry in document["installed"]
            if str(entry.get("reference", "")).casefold() != reference.casefold()
        ]
        previous = next((entry for entry in document["installed"]
                         if str(entry.get("reference", "")).casefold() == reference.casefold()), None)
        if previous:
            retained = {str(file["guid"]).casefold() for file in item["files"]}
            for old in previous["files"]:
                guid = str(old["guid"]).casefold()
                if not bool(old.get("owned", True)) or guid in retained:
                    continue
                replacement = next((file for package in installed for file in package["files"]
                                    if str(file["guid"]).casefold() == guid), None)
                if replacement is not None:
                    replacement["owned"] = True
        installed.append(item)
        document["installed"] = sorted(
            installed, key=lambda entry: str(entry.get("reference", "")).casefold()
        )
        self.save(document)
        return item

    def record_python_install(
        self,
        *,
        syntax: str,
        command: Iterable[str],
        output: str,
        requirements: Iterable[str] = (),
        dependency_requirements: Iterable[Mapping[str, object]] = (),
        changes: Iterable[Mapping[str, object]] = (),
        owner: str = "",
    ) -> dict[str, object]:
        document = self.load()
        normalized_requirements = _normalize_python_requirements(
            dependency_requirements
        )
        normalized_changes = _normalize_python_changes(changes)
        item = {
            "transaction_id": uuid.uuid4().hex,
            "installed_at": time.time(),
            "syntax": str(syntax),
            "command": [str(value) for value in command],
            "requirements": [str(value) for value in requirements],
            "output": str(output)[-4000:],
            "owner": str(owner),
            "changes": normalized_changes,
        }
        document["python_installs"].append(item)
        if owner and normalized_requirements:
            _register_python_dependency_owner(
                document,
                owner,
                normalized_requirements,
                normalized_changes,
            )
        self.save(document)
        return item

    def record_python_reconciliation(
        self,
        *,
        requirements: Iterable[str],
        changes: Iterable[Mapping[str, object]],
        owners: Mapping[str, Iterable[Mapping[str, object]]] | None = None,
    ) -> dict[str, object]:
        """Record dependency repair in the active project Python environment."""

        document = self.load()
        normalized_changes = _normalize_python_changes(changes)
        item = {
            "transaction_id": uuid.uuid4().hex,
            "installed_at": time.time(),
            "syntax": "startup dependency reconciliation",
            "command": [],
            "requirements": [str(value) for value in requirements],
            "output": "",
            "owner": "@environment",
            "changes": normalized_changes,
        }
        document["python_installs"].append(item)
        installed_by_reference = {
            str(record.get("reference", "")).casefold(): record
            for record in document.get("installed", [])
            if isinstance(record, dict)
        }
        for reference, raw_requirements in (owners or {}).items():
            normalized_requirements = _normalize_python_requirements(raw_requirements)
            if not normalized_requirements:
                continue
            _register_python_dependency_owner(
                document,
                reference,
                normalized_requirements,
                normalized_changes,
            )
            installed = installed_by_reference.get(str(reference).casefold())
            if installed is not None:
                installed["python_requirements"] = normalized_requirements
        changes_by_name = {change["name"]: change for change in normalized_changes}
        for raw in document.get("python_dependencies", []):
            if not isinstance(raw, dict):
                continue
            name = canonicalize_name(str(raw.get("name", "")))
            change = changes_by_name.get(name)
            if change is None:
                continue
            raw["managed"] = True
            raw["baseline_version"] = change["before"]
            raw["installed_version"] = change["after"]
        self.save(document)
        return item

    def python_release_plan(self, reference: str) -> tuple[dict[str, object], ...]:
        key = validate_reference(reference).casefold()
        result: list[dict[str, object]] = []
        for raw in self.load().get("python_dependencies", []):
            if not isinstance(raw, Mapping):
                continue
            owners = [
                dict(item)
                for item in raw.get("owners", [])
                if isinstance(item, Mapping)
            ]
            if not any(
                str(item.get("reference", "")).casefold() == key
                for item in owners
            ):
                continue
            result.append(
                {
                    **dict(raw),
                    "remaining_owners": [
                        item
                        for item in owners
                        if str(item.get("reference", "")).casefold() != key
                    ],
                }
            )
        return tuple(result)

    def remove_install(self, reference: str) -> dict[str, object]:
        key = validate_reference(reference).casefold()
        document = self.load()
        removed = next(
            (
                dict(item)
                for item in document["installed"]
                if str(item.get("reference", "")).casefold() == key
            ),
            None,
        )
        if removed is None:
            raise KeyError(f"Plugin is not installed: {reference}")
        remaining = [
            item
            for item in document["installed"]
            if str(item.get("reference", "")).casefold() != key
        ]
        python_dependencies = []
        for raw in document.get("python_dependencies", []):
            if not isinstance(raw, Mapping):
                continue
            dependency = dict(raw)
            owners = [
                dict(item)
                for item in dependency.get("owners", [])
                if isinstance(item, Mapping)
                and str(item.get("reference", "")).casefold() != key
            ]
            if owners:
                dependency["owners"] = owners
                python_dependencies.append(dependency)
        document["python_dependencies"] = python_dependencies
        transferred: list[str] = []
        for owned in [*removed.get("files", []), removed.get("control")]:
            if not isinstance(owned, Mapping) or not bool(owned.get("owned", True)):
                continue
            guid = str(owned.get("guid", "")).casefold()
            replacement = next(
                (
                    item
                    for package in remaining
                    for item in [*package.get("files", []), package.get("control")]
                    if isinstance(item, dict)
                    and str(item.get("guid", "")).casefold() == guid
                ),
                None,
            )
            if replacement is not None:
                replacement["owned"] = True
                transferred.append(guid)
        document["installed"] = remaining
        self.save(document)
        removed["transferred_guids"] = transferred
        return removed

    def set_enabled(self, reference: str, enabled: bool) -> dict[str, object]:
        key = validate_reference(reference).casefold()
        document = self.load()
        changed = None
        for item in document["installed"]:
            if str(item.get("reference", "")).casefold() == key:
                item["enabled"] = bool(enabled)
                changed = dict(item)
                break
        if changed is None:
            raise KeyError(f"Plugin is not installed: {reference}")
        self.save(document)
        return changed

    @staticmethod
    def _validate_installed(installed: object) -> None:
        if not isinstance(installed, list):
            raise ValueError("Plugin registry installed field must be a list")
        references: set[str] = set()
        owned_guids: set[str] = set()
        for package in installed:
            if not isinstance(package, Mapping):
                raise ValueError("Installed plugin record must be an object")
            reference = validate_reference(str(package.get("reference", "")))
            key = reference.casefold()
            if key in references:
                raise ValueError(f"Duplicate installed package reference: {reference}")
            references.add(key)
            files = package.get("files", [])
            if not isinstance(files, list):
                raise ValueError(f"Installed package files must be a list: {reference}")
            for item in [*files, package.get("control")]:
                if not isinstance(item, Mapping):
                    raise ValueError(f"Installed package ownership record is invalid: {reference}")
                guid = str(item.get("guid", "")).casefold()
                if not guid:
                    raise ValueError(f"Installed package ownership has no GUID: {reference}")
                if bool(item.get("owned", True)):
                    if guid in owned_guids:
                        raise ValueError(f"GUID has multiple package owners: {guid}")
                    owned_guids.add(guid)

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "$schema": REGISTRY_SCHEMA,
            "packages": [],
            "installed": [],
            "python_installs": [],
            "python_dependencies": [],
        }

    def _write_lock(self, registry: Mapping[str, object]) -> None:
        packages = []
        for raw in registry.get("installed", []):
            if not isinstance(raw, Mapping):
                continue
            packages.append(
                {
                    key: raw.get(key)
                    for key in (
                        "reference",
                        "version",
                        "engine",
                        "transaction_id",
                        "installed_at",
                        "source",
                        "dependencies",
                        "enabled",
                        "files",
                        "control",
                    )
                }
            )
        document = {
            "$schema": "infernux.package_lock",
            "packages": packages,
            "python": list(registry.get("python_installs", [])),
            "python_dependencies": list(
                registry.get("python_dependencies", [])
            ),
        }
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        write_document_text(
            self.lock_path, json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        )


def _normalize_python_requirements(
    values: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in values:
        if not isinstance(raw, Mapping):
            raise ValueError("Plugin Python requirement must be an object")
        name = canonicalize_name(str(raw.get("name", "")).strip())
        requirement = str(raw.get("requirement", "")).strip()
        if not name or not requirement:
            raise ValueError("Plugin Python requirement requires name and syntax")
        key = (name, requirement)
        if key not in seen:
            result.append({"name": name, "requirement": requirement})
            seen.add(key)
    return result


def _validate_python_dependencies(value: object) -> None:
    if not isinstance(value, list):
        raise ValueError("Plugin Python dependency ledger must be a list")
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("Plugin Python dependency must be an object")
        name = canonicalize_name(str(raw.get("name", "")).strip())
        if not name or name in names:
            raise ValueError("Plugin Python dependency names must be unique")
        names.add(name)
        if not isinstance(raw.get("managed", False), bool):
            raise ValueError("Plugin Python dependency managed flag must be boolean")
        owners = raw.get("owners", [])
        if not isinstance(owners, list):
            raise ValueError("Plugin Python dependency owners must be a list")
        owner_names: set[str] = set()
        for owner in owners:
            if not isinstance(owner, Mapping):
                raise ValueError("Plugin Python dependency owner must be an object")
            reference = str(owner.get("reference", "")).strip().casefold()
            requirements = owner.get("requirements", [])
            if (
                not reference
                or reference in owner_names
                or not isinstance(requirements, list)
                or any(not isinstance(item, str) for item in requirements)
            ):
                raise ValueError("Plugin Python dependency owner is invalid")
            owner_names.add(reference)


def _normalize_python_changes(
    values: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, Mapping):
            raise ValueError("Plugin Python environment change must be an object")
        name = canonicalize_name(str(raw.get("name", "")).strip())
        before = str(raw.get("before", "")).strip()
        after = str(raw.get("after", "")).strip()
        if not name or name in seen or before == after:
            continue
        result.append({"name": name, "before": before, "after": after})
        seen.add(name)
    return result


def _register_python_dependency_owner(
    document: dict[str, object],
    owner: str,
    requirements: list[dict[str, str]],
    changes: list[dict[str, str]],
) -> None:
    dependencies = document.setdefault("python_dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("Plugin Python dependency ledger must be a list")
    changes_by_name = {item["name"]: item for item in changes}
    requirements_by_name: dict[str, list[str]] = {}
    for requirement in requirements:
        requirements_by_name.setdefault(requirement["name"], []).append(
            requirement["requirement"]
        )
    for name, owner_requirements in requirements_by_name.items():
        entry = next(
            (
                item
                for item in dependencies
                if isinstance(item, dict)
                and canonicalize_name(str(item.get("name", ""))) == name
            ),
            None,
        )
        change = changes_by_name.get(name)
        if entry is None:
            entry = {
                "name": name,
                "managed": bool(change),
                "baseline_version": str(change.get("before", "")) if change else "",
                "installed_version": str(change.get("after", "")) if change else "",
                "owners": [],
            }
            dependencies.append(entry)
        elif change:
            if not bool(entry.get("managed", False)):
                entry["managed"] = True
                entry["baseline_version"] = str(change.get("before", ""))
            entry["installed_version"] = str(change.get("after", ""))
        owners = [
            dict(item)
            for item in entry.get("owners", [])
            if isinstance(item, Mapping)
            and str(item.get("reference", "")).casefold() != str(owner).casefold()
        ]
        owners.append(
            {
                "reference": str(owner),
                "requirements": list(dict.fromkeys(owner_requirements)),
            }
        )
        entry["owners"] = owners
    dependencies.sort(key=lambda item: str(item.get("name", "")))


def _normalize_intros(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Plugin localized intros must be an object")
    result: dict[str, str] = {}
    for raw_locale, raw_text in value.items():
        locale = normalize_locale(str(raw_locale))
        if not locale:
            raise ValueError("Plugin localized intro requires a locale")
        if not isinstance(raw_text, str):
            raise ValueError("Plugin localized intros must be strings")
        result[locale] = raw_text
    return result


__all__ = [
    "LOCK_RELATIVE_PATH",
    "PluginRegistry",
    "REGISTRY_RELATIVE_PATH",
]
