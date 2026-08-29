from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from python_runtime_catalog import (
    DEFAULT_PYTHON_RUNTIME,
    PythonRuntimeId,
    SUPPORTED_PYTHON_RUNTIMES,
    runtime_release,
)

_DEFAULT_RELEASE = runtime_release(DEFAULT_PYTHON_RUNTIME)
PYTHON_VERSION = _DEFAULT_RELEASE.patch_version
PYTHON_BUILD_RELEASE = _DEFAULT_RELEASE.build_release
PRIVATE_RUNTIME_MARKER = ".infernux-private-python-runtime.json"


@dataclass(frozen=True)
class RuntimeArchive:
    name: str
    url: str
    sha256: str


def runtime_archive_for_machine(
    *,
    system: str | None = None,
    machine: str | None = None,
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> RuntimeArchive:
    release = runtime_release(runtime)
    platform_name = (system or sys.platform).lower()
    architecture = (
        machine
        or platform.machine()
        or os.environ.get("PROCESSOR_ARCHITECTURE")
        or ""
    ).lower()

    if platform_name in {"win32", "windows"}:
        if architecture in {"amd64", "x86_64"}:
            target = "x86_64-pc-windows-msvc"
        elif architecture in {"x86", "i386", "i686"}:
            target = "i686-pc-windows-msvc"
        elif architecture in {"arm64", "aarch64"}:
            target = "aarch64-pc-windows-msvc"
        else:
            raise RuntimeError(
                f"No isolated Infernux Python runtime archive is published for Windows {architecture or 'unknown'}."
            )
    elif platform_name in {"darwin", "macos"}:
        if architecture in {"arm64", "aarch64"}:
            target = "aarch64-apple-darwin"
        elif architecture in {"amd64", "x86_64"}:
            target = "x86_64-apple-darwin"
        else:
            raise RuntimeError(
                f"No isolated Infernux Python runtime archive is published for macOS {architecture or 'unknown'}."
            )
    elif platform_name.startswith("linux"):
        if architecture in {"arm64", "aarch64"}:
            target = "aarch64-unknown-linux-gnu"
        elif architecture in {"amd64", "x86_64"}:
            target = "x86_64-unknown-linux-gnu"
        else:
            raise RuntimeError(
                f"No isolated Infernux Python runtime archive is published for Linux {architecture or 'unknown'}."
            )
    else:
        raise RuntimeError(f"Unsupported platform for the Infernux private Python runtime: {platform_name}")

    name = (
        f"cpython-{release.patch_version}+{release.build_release}-{target}-"
        "install_only.tar.gz"
    )
    return RuntimeArchive(
        name=name,
        url=(
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{release.build_release}/{name.replace('+', '%2B')}"
        ),
        sha256=release.archive_sha256[target],
    )


def verify_runtime_archive(
    archive_path: str | os.PathLike[str], expected_sha256: str
) -> None:
    archive = Path(archive_path)
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"Unable to read private Python runtime archive: {archive}") from exc

    actual = digest.hexdigest()
    if actual.lower() != expected_sha256.lower():
        raise RuntimeError(
            "Private Python runtime archive checksum mismatch: "
            f"expected {expected_sha256}, got {actual}."
        )


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)


def write_private_runtime_marker(
    runtime_root: str | os.PathLike[str],
    archive_name: str,
    archive_sha256: str = "",
    *,
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> None:
    release = runtime_release(runtime)
    root = Path(runtime_root)
    marker = root / PRIVATE_RUNTIME_MARKER
    payload = {
        "owner": "Infernux Hub",
        "kind": "private-python-runtime",
        "python_version": release.patch_version,
        "python_series": release.runtime_id.series,
        "source_archive": archive_name,
        "source_archive_sha256": archive_sha256,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def is_private_runtime_root(runtime_root: str | os.PathLike[str]) -> bool:
    marker = Path(runtime_root) / PRIVATE_RUNTIME_MARKER
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("owner") == "Infernux Hub"
        and payload.get("kind") == "private-python-runtime"
    )


def is_current_private_runtime_root(
    runtime_root: str | os.PathLike[str],
    *,
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> bool:
    marker = Path(runtime_root) / PRIVATE_RUNTIME_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        release = runtime_release(runtime)
        archive = runtime_archive_for_machine(runtime=release.runtime_id)
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
        return False
    return (
        payload.get("owner") == "Infernux Hub"
        and payload.get("kind") == "private-python-runtime"
        and payload.get("python_version") == release.patch_version
        and payload.get("python_series", release.runtime_id.series)
        == release.runtime_id.series
        and payload.get("source_archive") == archive.name
        and payload.get("source_archive_sha256") == archive.sha256
    )


def extract_runtime_archive(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> None:
    archive = Path(archive_path).resolve()
    target = Path(destination).resolve()
    if not archive.is_file():
        raise RuntimeError(f"Private Python runtime archive not found: {archive}")
    if expected_sha256:
        verify_runtime_archive(archive, expected_sha256)

    target.parent.mkdir(parents=True, exist_ok=True)
    extract_root = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.extract-", dir=target.parent)
    )
    try:
        try:
            with tarfile.open(archive, mode="r:gz") as package:
                package.extractall(extract_root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(f"Invalid private Python runtime archive: {archive}") from exc

        unpacked_runtime = extract_root / "python"
        if not unpacked_runtime.is_dir():
            raise RuntimeError(
                "Unexpected private Python runtime archive layout: missing the python/ root."
            )

        if target.exists():
            _remove_tree(target)
        os.replace(unpacked_runtime, target)
        write_private_runtime_marker(
            target,
            archive.name,
            expected_sha256 or "",
            runtime=runtime,
        )
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


def remove_legacy_installer_artifacts(
    runtime_cache_root: str | os.PathLike[str],
    *,
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> None:
    """Prune obsolete bootstrap files and non-target packaging runtimes.

    This function only operates on the explicitly supplied packaging cache,
    never on Hub's managed user-runtime directory. Keep only the pinned
    runtime being staged so stale output from an older ABI cannot enter a
    fresh Hub installer.
    """
    root = Path(runtime_cache_root)
    if not root.is_dir():
        return
    target_runtime = PythonRuntimeId.parse(runtime)
    for runtime_id in SUPPORTED_PYTHON_RUNTIMES:
        if runtime_id == target_runtime:
            continue
        stale_runtime = root / runtime_id.directory_name
        if stale_runtime.is_dir():
            shutil.rmtree(stale_runtime)

    for pattern in (
        "python-3.12*.exe",
        "python-3.12*.pkg",
        "python-3.13*.exe",
        "python-3.13*.pkg",
    ):
        for artifact in root.glob(pattern):
            if artifact.is_file():
                artifact.unlink()

    expected_archive = runtime_archive_for_machine(runtime=target_runtime).name
    for artifact in root.glob("cpython-*-install_only.tar.gz*"):
        if artifact.is_file() and artifact.name != expected_archive:
            artifact.unlink()
