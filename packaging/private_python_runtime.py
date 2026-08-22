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


PYTHON_VERSION = "3.12.13"
PYTHON_BUILD_RELEASE = "20260805"
PRIVATE_RUNTIME_MARKER = ".infernux-private-python-runtime.json"

_ARCHIVE_SHA256 = {
    "x86_64-pc-windows-msvc": "d731ce7dddcfad4a9521aac48626ca06326003fe4771a366e0fce6eb58709451",
    "i686-pc-windows-msvc": "8ba10b61abc62e2f6ec0863d8496c077c4431e8621a812e0bd3f8cae8cd5dbec",
    "aarch64-pc-windows-msvc": "78fbbffa040de2dd6e4c97001103cacf5770743c02b2493ea9eda711ea41743c",
    "x86_64-apple-darwin": "718a89c781a7fb0a8cf7cd37c8cad0f91968438493285aa878f51228dcc9c7ed",
    "aarch64-apple-darwin": "b8caf71c009e95507a306ba7ff18335e840b678d23b4d79ec026527553a99e5d",
    "x86_64-unknown-linux-gnu": "919043a06d8136147b24077c3bb32ec058e66c586ce5465b0f0eb018f242a655",
    "aarch64-unknown-linux-gnu": "c2083943c86dbb21ca0211238362fd922de7b0475688f26c135cf5d20a1c2f48",
}


@dataclass(frozen=True)
class RuntimeArchive:
    name: str
    url: str
    sha256: str


def runtime_archive_for_machine(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> RuntimeArchive:
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
        f"cpython-{PYTHON_VERSION}+{PYTHON_BUILD_RELEASE}-{target}-install_only.tar.gz"
    )
    return RuntimeArchive(
        name=name,
        url=(
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{PYTHON_BUILD_RELEASE}/{name.replace('+', '%2B')}"
        ),
        sha256=_ARCHIVE_SHA256[target],
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
) -> None:
    root = Path(runtime_root)
    marker = root / PRIVATE_RUNTIME_MARKER
    payload = {
        "owner": "Infernux Hub",
        "kind": "private-python-runtime",
        "python_version": PYTHON_VERSION,
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


def is_current_private_runtime_root(runtime_root: str | os.PathLike[str]) -> bool:
    marker = Path(runtime_root) / PRIVATE_RUNTIME_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        archive = runtime_archive_for_machine()
    except (OSError, json.JSONDecodeError, RuntimeError):
        return False
    return (
        payload.get("owner") == "Infernux Hub"
        and payload.get("kind") == "private-python-runtime"
        and payload.get("python_version") == PYTHON_VERSION
        and payload.get("source_archive") == archive.name
        and payload.get("source_archive_sha256") == archive.sha256
    )


def extract_runtime_archive(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
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
        write_private_runtime_marker(target, archive.name, expected_sha256 or "")
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


def remove_legacy_installer_artifacts(runtime_cache_root: str | os.PathLike[str]) -> None:
    root = Path(runtime_cache_root)
    if not root.is_dir():
        return
    for pattern in ("python-3.12*.exe", "python-3.12*.pkg"):
        for artifact in root.glob(pattern):
            if artifact.is_file():
                artifact.unlink()
