"""Build the standalone Infernux Hub update release."""

from __future__ import annotations

import argparse
import json
import platform
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


MANIFEST_SCHEMA = "infernux.hub_update"
PRODUCT_NAME = "InfernuxHub"
SUPPORTED_PLATFORMS = frozenset({"windows-x64", "linux-x64"})
INSTALLER_ONLY_PATHS = frozenset(
    {PurePosixPath("InfernuxHubData/runtime/runtime_bundle.zip")}
)


def project_version(source_root: str | Path | None = None) -> str:
    root = Path(source_root).resolve() if source_root else Path(__file__).resolve().parents[1]
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = document.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml does not declare project.version")
    return version


def host_platform_id(
    *, system: str | None = None, machine: str | None = None
) -> str:
    system_name = (system or platform.system()).casefold()
    architecture = (machine or platform.machine()).casefold()
    if architecture not in {"amd64", "x86_64"}:
        raise RuntimeError(
            f"Infernux Hub has no x64 release contract for architecture {architecture!r}"
        )
    if system_name == "windows":
        return "windows-x64"
    if system_name == "linux":
        return "linux-x64"
    raise RuntimeError(
        f"Infernux Hub has no release contract for host system {system_name!r}"
    )


def manifest_asset_name(platform_id: str) -> str:
    if platform_id not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported Infernux Hub platform: {platform_id!r}")
    return f"InfernuxHub-{platform_id}-manifest.json"


def _payload_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and tuple(part.casefold() for part in path.relative_to(root).parts[:2])
        != ("infernuxhubdata", "shared")
        and PurePosixPath(path.relative_to(root).as_posix()) not in INSTALLER_ONLY_PATHS
        and not (
            path.parent == root
            and path.name.startswith("InfernuxHub-")
            and path.name.endswith("-manifest.json")
        )
    )


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise ValueError(f"Unsafe update path: {value!r}")
    if tuple(part.casefold() for part in path.parts[:2]) == ("infernuxhubdata", "shared"):
        raise ValueError("Hub updates cannot own user shared resources")
    return path


def create_manifest(
    root: str | Path,
    version: str,
    platform_id: str | None = None,
) -> dict[str, object]:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Hub directory does not exist: {root_path}")
    return {
        "$schema": MANIFEST_SCHEMA,
        "product": PRODUCT_NAME,
        "version": version,
        "platform": platform_id or host_platform_id(),
        "files": [
            {"path": path.relative_to(root_path).as_posix()}
            for path in _payload_files(root_path)
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
        or document["platform"] not in SUPPORTED_PLATFORMS
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
        for path in _payload_files(root_path):
            archive.write(path, path.relative_to(root_path).as_posix())
    return output


def build_release_artifacts(
    hub_dir: str | Path,
    version: str,
    output_dir: str | Path,
    platform_id: str | None = None,
) -> tuple[Path, Path]:
    release_platform = platform_id or host_platform_id()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    hub_root = Path(hub_dir).resolve()
    manifest_document = create_manifest(hub_root, version, release_platform)
    manifest = write_manifest(
        manifest_document, output / manifest_asset_name(release_platform)
    )
    write_manifest(
        manifest_document, hub_root / manifest_asset_name(release_platform)
    )
    archive = create_full_zip(
        hub_root, output / f"InfernuxHub-{version}-{release_platform}-full.zip"
    )
    return archive, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-dir", required=True)
    parser.add_argument("--version", default=project_version())
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--platform", choices=sorted(SUPPORTED_PLATFORMS))
    arguments = parser.parse_args()
    for artifact in build_release_artifacts(
        arguments.hub_dir,
        arguments.version,
        arguments.output_dir,
        arguments.platform,
    ):
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
