"""Build official source repositories into wheel-distributed InxPackages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# The official catalog is built from the repository checkout before Infernux
# itself is necessarily installed.  Make that source dependency explicit so
# the documented root-level command is reproducible in a clean environment.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PYTHON = _REPOSITORY_ROOT / "python"
if _SOURCE_PYTHON.is_dir() and str(_SOURCE_PYTHON) not in sys.path:
    sys.path.insert(0, str(_SOURCE_PYTHON))

from Infernux.plugins import InxPackage


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build(source_root: Path, output_root: Path, catalog_path: Path) -> None:
    source_document = json.loads(catalog_path.read_text(encoding="utf-8"))
    if (
        not isinstance(source_document, dict)
        or source_document.get("$schema") != "infernux.official_plugin_sources"
        or source_document.get("catalog_version") != 1
        or not isinstance(source_document.get("plugins"), list)
    ):
        raise RuntimeError(f"Invalid official plugin source catalog: {catalog_path}")

    output_root.mkdir(parents=True, exist_ok=True)
    registry: list[dict[str, object]] = []
    defaults: list[str] = []
    expected_outputs = {"official-registry.json", "default-libraries.json"}
    for source_entry in source_document["plugins"]:
        if not isinstance(source_entry, dict):
            raise RuntimeError("Official plugin source entry must be an object")
        relative = str(source_entry.get("path", "")).strip()
        repository = str(source_entry.get("repository", "")).strip()
        plugin_source = (source_root / relative).resolve()
        if plugin_source.parent != source_root.resolve() or not plugin_source.is_dir():
            raise RuntimeError(f"Official plugin source is missing or unsafe: {relative}")
        manifest_path = plugin_source / "InxPackage.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reference = str(manifest.get("reference", "")).strip()
        if not reference:
            raise RuntimeError(f"Official plugin has no reference: {plugin_source}")
        artifact = f"{reference.replace('/', '.')}.inxpkg"
        expected_outputs.add(artifact)
        preview = InxPackage.export_source(
            str(plugin_source),
            str(output_root / artifact),
            profile="release",
        )
        artifact_path = output_root / artifact
        artifact_payload = artifact_path.read_bytes()
        registry.append(
            {
                "reference": reference,
                "name": str(preview.metadata.get("name", reference)),
                "version": str(preview.metadata.get("version", "")),
                "intro": str(preview.metadata.get("intro", "")),
                "intros": dict(preview.metadata.get("intros", {})),
                "artifact": artifact,
                "artifact_sha256": hashlib.sha256(artifact_payload).hexdigest(),
                "artifact_size": len(artifact_payload),
                "engine": str(preview.metadata.get("engine", "")),
                "dependencies": list(preview.metadata.get("dependencies", [])),
                "repository": repository,
                "pages": list(preview.metadata.get("pages", [])),
            }
        )
        if bool(source_entry.get("default", False)):
            defaults.append(reference)

    for existing in output_root.iterdir():
        if existing.is_file() and existing.name not in expected_outputs:
            existing.unlink()
    _write_json(
        output_root / "official-registry.json",
        {
            "$schema": "infernux.official_plugin_registry",
            "catalog_version": 1,
            "packages": registry,
        },
    )
    _write_json(
        output_root / "default-libraries.json",
        {
            "$schema": "infernux.default_libraries",
            "catalog_version": 1,
            "libraries": defaults,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    args = parser.parse_args()
    build(args.source_root, args.output_root, args.catalog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
