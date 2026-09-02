"""Build the standalone Infernux Hub update release."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path, PurePosixPath


MANIFEST_SCHEMA = "infernux.hub_update"
PRODUCT_NAME = "InfernuxHub"


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise ValueError(f"Unsafe update path: {value!r}")
    return path


def create_manifest(root: str | Path, version: str) -> dict[str, object]:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Hub directory does not exist: {root_path}")
    return {
        "$schema": MANIFEST_SCHEMA,
        "product": PRODUCT_NAME,
        "version": version,
        "platform": "windows-x64",
        "files": [
            {"path": path.relative_to(root_path).as_posix()}
            for path in sorted(item for item in root_path.rglob("*") if item.is_file())
        ],
    }


def validate_manifest(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "$schema",
        "product",
        "version",
        "platform",
        "files",
    }:
        raise ValueError("Infernux Hub manifest does not match the current contract")
    if (
        document["$schema"] != MANIFEST_SCHEMA
        or document["product"] != PRODUCT_NAME
        or document["platform"] != "windows-x64"
        or not isinstance(document["version"], str)
        or not isinstance(document["files"], list)
    ):
        raise ValueError("Infernux Hub manifest does not match the current contract")
    seen: set[str] = set()
    for entry in document["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path"}:
            raise ValueError("Infernux Hub manifest file entry is invalid")
        path = _safe_relative_path(entry["path"])
        normalized = path.as_posix()
        if normalized in seen:
            raise ValueError(f"Duplicate Hub manifest path: {normalized}")
        seen.add(normalized)
    return document


def write_manifest(manifest: dict[str, object], destination: str | Path) -> Path:
    validate_manifest(manifest)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_manifest(source: str | Path) -> dict[str, object]:
    return validate_manifest(json.loads(Path(source).read_text(encoding="utf-8")))


def create_full_zip(root: str | Path, destination: str | Path) -> Path:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Hub directory does not exist: {root_path}")
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(root_path).as_posix())
    return output


def build_release_artifacts(
    hub_dir: str | Path,
    version: str,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = write_manifest(
        create_manifest(hub_dir, version), output / "InfernuxHub-manifest.json"
    )
    archive = create_full_zip(
        hub_dir, output / f"InfernuxHub-{version}-windows-x64-full.zip"
    )
    return archive, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True)
    arguments = parser.parse_args()
    for artifact in build_release_artifacts(
        arguments.hub_dir, arguments.version, arguments.output_dir
    ):
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
