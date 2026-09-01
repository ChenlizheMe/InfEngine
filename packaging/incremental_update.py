"""Build and apply verified incremental packages for Infernux Hub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


MANIFEST_SCHEMA = "infernux.hub_update"
PRODUCT_NAME = "InfernuxHub"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Unsafe update path: {value!r}")
    return path


def create_manifest(root: str | Path, version: str) -> dict:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"Hub directory does not exist: {root_path}")

    files = []
    for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
        relative = path.relative_to(root_path).as_posix()
        files.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return {
        "$schema": MANIFEST_SCHEMA,
        "product": PRODUCT_NAME,
        "version": version,
        "platform": "windows-x64",
        "files": files,
    }


def write_manifest(manifest: dict, destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_manifest(source: str | Path) -> dict:
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    if data.get("$schema") != MANIFEST_SCHEMA or data.get("product") != PRODUCT_NAME:
        raise ValueError("Unsupported Infernux Hub update manifest")
    for entry in data.get("files", []):
        _safe_relative_path(entry["path"])
    return data


def _manifest_files(manifest: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in manifest.get("files", [])}


def create_full_zip(root: str | Path, destination: str | Path) -> Path:
    root_path = Path(root).resolve()
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(root_path).as_posix())
    return output


def create_patch(
    root: str | Path,
    base_manifest: dict,
    target_manifest: dict,
    destination: str | Path,
) -> Path:
    root_path = Path(root).resolve()
    base_files = _manifest_files(base_manifest)
    target_files = _manifest_files(target_manifest)
    changed = [
        path for path, entry in target_files.items()
        if path not in base_files or entry["sha256"] != base_files[path].get("sha256")
    ]
    deleted = sorted(set(base_files) - set(target_files))
    metadata = {
        "$schema": MANIFEST_SCHEMA,
        "product": PRODUCT_NAME,
        "base_version": base_manifest["version"],
        "target_version": target_manifest["version"],
        "files": [target_files[path] for path in sorted(changed)],
        "delete": deleted,
    }

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("hub-update.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        for relative in sorted(changed):
            safe = _safe_relative_path(relative)
            source = root_path.joinpath(*safe.parts)
            if not source.is_file():
                raise FileNotFoundError(f"Manifest file is missing: {source}")
            archive.write(source, f"payload/{safe.as_posix()}")
    return output


def apply_patch(install_dir: str | Path, patch_file: str | Path, current_version: str) -> str:
    """Apply a patch transactionally enough for tests and an external updater.

    All changed files are verified in a staging directory before the live tree
    is touched. Existing changed/deleted files are backed up and restored if a
    copy or delete fails.
    """
    install_root = Path(install_dir).resolve()
    if not install_root.is_dir():
        raise FileNotFoundError(f"Hub install directory does not exist: {install_root}")

    with tempfile.TemporaryDirectory(prefix="infernux-hub-update-") as temp:
        stage = Path(temp) / "stage"
        backup = Path(temp) / "backup"
        stage.mkdir()
        backup.mkdir()
        with zipfile.ZipFile(patch_file, "r") as archive:
            names = set(archive.namelist())
            if "hub-update.json" not in names:
                raise ValueError("Patch does not contain hub-update.json")
            metadata = json.loads(archive.read("hub-update.json").decode("utf-8"))
            if metadata.get("$schema") != MANIFEST_SCHEMA or metadata.get("product") != PRODUCT_NAME:
                raise ValueError("Unsupported Infernux Hub patch")
            if metadata.get("base_version") != current_version:
                raise ValueError(
                    f"Patch requires Hub {metadata.get('base_version')}, current version is {current_version}"
                )

            changed = []
            for entry in metadata.get("files", []):
                relative = _safe_relative_path(entry["path"])
                member = f"payload/{relative.as_posix()}"
                if member not in names:
                    raise ValueError(f"Patch payload is missing {relative.as_posix()}")
                target = stage.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                if target.stat().st_size != entry["size"] or _sha256(target) != entry["sha256"]:
                    raise ValueError(f"Patch verification failed for {relative.as_posix()}")
                changed.append(relative)
            deleted = [_safe_relative_path(path) for path in metadata.get("delete", [])]

        affected = changed + deleted
        existed: set[PurePosixPath] = set()
        try:
            for relative in affected:
                live = install_root.joinpath(*relative.parts)
                if live.is_file():
                    saved = backup.joinpath(*relative.parts)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live, saved)
                    existed.add(relative)

            for relative in changed:
                source = stage.joinpath(*relative.parts)
                destination = install_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            for relative in deleted:
                target = install_root.joinpath(*relative.parts)
                if target.is_file():
                    target.unlink()
        except Exception:
            for relative in affected:
                live = install_root.joinpath(*relative.parts)
                saved = backup.joinpath(*relative.parts)
                if relative in existed:
                    live.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, live)
                elif live.is_file():
                    live.unlink()
            raise

    return str(metadata["target_version"])


def build_release_artifacts(
    hub_dir: str | Path,
    version: str,
    output_dir: str | Path,
    base_manifest_path: str | Path | None = None,
) -> list[Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = create_manifest(hub_dir, version)
    manifest_path = write_manifest(manifest, output / f"InfernuxHub-{version}-manifest.json")
    stable_manifest = write_manifest(manifest, output / "InfernuxHub-manifest.json")
    full_zip = create_full_zip(
        hub_dir, output / f"InfernuxHub-{version}-windows-x64-full.zip"
    )
    artifacts = [full_zip, manifest_path, stable_manifest]
    if base_manifest_path:
        base = load_manifest(base_manifest_path)
        if base["version"] != version:
            patch = create_patch(
                hub_dir,
                base,
                manifest,
                output / (
                    f"InfernuxHub-{base['version']}-to-{version}-windows-x64.patch.zip"
                ),
            )
            artifacts.append(patch)
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Infernux Hub full and incremental artifacts")
    parser.add_argument("--hub-dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-manifest")
    args = parser.parse_args()
    for artifact in build_release_artifacts(
        args.hub_dir, args.version, args.output_dir, args.base_manifest
    ):
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
