"""Stage versioned InxPackage assets for one GitHub release.

The official package build remains the only package authoring path.  This
script only verifies those completed packages, selects the packages owned by
the requested repository, and writes the small release manifests consumed by
the Editor's GitHub release resolver.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Release assets are produced from a checked-out engine tag. Resolve the
# matching Python implementation explicitly before importing Infernux so an
# older package installed in the build interpreter cannot define the protocol.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PYTHON = _REPOSITORY_ROOT / "python"
if not _SOURCE_PYTHON.is_dir():
    raise RuntimeError(f"Infernux source package is missing: {_SOURCE_PYTHON}")
if str(_SOURCE_PYTHON) not in sys.path:
    sys.path.insert(0, str(_SOURCE_PYTHON))

from Infernux.plugins import InxPackage
from Infernux.plugins.github_releases import (
    RELEASE_MANIFEST_SCHEMA,
    release_manifest_name,
)
from Infernux.plugins.package import validate_reference


_RELEASE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _write_json(path: Path, document: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _repository_identity(value: str) -> tuple[str, str]:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {
        "github.com",
        "www.github.com",
    }:
        raise ValueError("Release repository must be an https://github.com URL")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Release repository must identify one GitHub repository")
    owner, repository = parts
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ValueError("Release repository must identify one GitHub repository")
    return owner.casefold(), repository.casefold()


def _read_object(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return document


def build(
    package_root: Path,
    output_root: Path,
    catalog_path: Path,
    *,
    repository: str,
    release_tag: str,
) -> tuple[Path, ...]:
    """Stage repository-owned packages and their release protocol manifests."""

    if not _RELEASE_TAG.fullmatch(release_tag):
        raise ValueError(f"Invalid release tag: {release_tag!r}")
    repository_id = _repository_identity(repository)
    catalog = _read_object(catalog_path)
    if (
        catalog.get("$schema") != "infernux.official_plugin_sources"
        or set(catalog) != {"$schema", "plugins"}
        or not isinstance(catalog.get("plugins"), list)
    ):
        raise RuntimeError(f"Invalid official plugin source catalog: {catalog_path}")

    selected: list[tuple[dict[str, object], dict[str, object]]] = []
    source_root = catalog_path.parent
    for raw in catalog["plugins"]:
        if not isinstance(raw, dict):
            raise RuntimeError("Official plugin source entry must be an object")
        source_repository = str(raw.get("repository", "")).strip()
        if not source_repository:
            continue
        if _repository_identity(source_repository) != repository_id:
            continue
        relative = str(raw.get("path", "")).strip()
        plugin_root = (source_root / relative).resolve()
        if plugin_root.parent != source_root.resolve() or not plugin_root.is_dir():
            raise RuntimeError(f"Official plugin source is missing or unsafe: {relative}")
        selected.append((raw, _read_object(plugin_root / "InxPackage.json")))

    if not selected:
        raise RuntimeError(f"No official plugins are owned by {repository}")

    output_root.mkdir(parents=True, exist_ok=True)
    expected_names: set[str] = set()
    records: list[tuple[Path, Path, dict[str, object]]] = []
    for _source, source_manifest in selected:
        reference = validate_reference(str(source_manifest.get("reference", "")))
        artifact_name = f"{reference.replace('/', '.')}.inxpkg"
        manifest_name = release_manifest_name(reference)
        if artifact_name in expected_names or manifest_name in expected_names:
            raise RuntimeError(f"Duplicate official plugin release asset: {reference}")
        expected_names.update((artifact_name, manifest_name))

        package_path = package_root / artifact_name
        if not package_path.is_file():
            raise RuntimeError(f"Official plugin package has not been built: {package_path}")
        preview = InxPackage.inspect(str(package_path))
        metadata = preview.metadata
        for field in ("reference", "version", "engine"):
            expected = str(source_manifest.get(field, ""))
            actual = str(metadata.get(field, ""))
            if actual != expected:
                raise RuntimeError(
                    f"Official plugin package {field} mismatch for {reference}: "
                    f"expected {expected!r}, found {actual!r}"
                )
        release_document: dict[str, object] = {
            "$schema": RELEASE_MANIFEST_SCHEMA,
            "reference": reference,
            "version": str(metadata["version"]),
            "engine": str(metadata.get("engine", "")),
            "artifact": {"name": artifact_name},
            "generator": "Infernux official plugin release assets",
            "release_tag": release_tag,
        }
        records.append(
            (
                package_path,
                output_root / manifest_name,
                release_document,
            )
        )

    unexpected = sorted(
        path.name
        for path in output_root.iterdir()
        if path.is_file() and path.name not in expected_names
    )
    if unexpected:
        raise RuntimeError(
            "Plugin release output contains files not owned by this build: "
            + ", ".join(unexpected)
        )

    outputs: list[Path] = []
    for package_path, manifest_path, release_document in records:
        artifact_path = output_root / package_path.name
        shutil.copy2(package_path, artifact_path)
        _write_json(manifest_path, release_document)
        outputs.extend((artifact_path, manifest_path))
    return tuple(outputs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    arguments = parser.parse_args()
    outputs = build(
        arguments.package_root,
        arguments.output_root,
        arguments.catalog,
        repository=arguments.repository,
        release_tag=arguments.release_tag,
    )
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
