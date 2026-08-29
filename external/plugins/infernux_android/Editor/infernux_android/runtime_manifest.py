"""Provenance and integrity contract for Android CPython prefixes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_NAME = "infernux-android-python.json"
MANIFEST_SCHEMA = 1
RUNTIME_KIND = "infernux-android-cpython"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_runtime_payload(prefix: Path) -> dict[str, int | str]:
    """Hash every regular file in a prefix except the manifest itself."""

    root = prefix.resolve()
    if not root.is_dir():
        raise ValueError(f"Android Python prefix does not exist: {root}")
    entries = tuple(root.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise ValueError(
                "Android Python prefixes must not contain symbolic links: "
                f"{path.relative_to(root).as_posix()}"
            )
    manifest_path = root / MANIFEST_NAME
    files = sorted(
        (path for path in entries if path != manifest_path and path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    digest = hashlib.sha256()
    byte_count = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        byte_count += size
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return {
        "algorithm": "sha256-tree-v1",
        "sha256": digest.hexdigest(),
        "files": len(files),
        "bytes": byte_count,
    }


def create_runtime_manifest(
    prefix: Path,
    *,
    abi: str,
    python_version: str,
    cpython_android_api: int,
    minimum_android_api: int,
    ndk_version: str,
    source_url: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Create and persist a deterministic manifest for one prepared prefix."""

    root = prefix.resolve()
    source_digest = source_sha256.strip().casefold()
    if abi not in {"arm64-v8a", "x86_64"}:
        raise ValueError(f"Unsupported Android ABI: {abi}")
    if not python_version.startswith("3.13."):
        raise ValueError("Android Player runtime manifests require CPython 3.13.x")
    if cpython_android_api < 21:
        raise ValueError("CPython Android API must be at least 21")
    if minimum_android_api < cpython_android_api:
        raise ValueError(
            "Runtime minimum Android API cannot be lower than the CPython API"
        )
    if not ndk_version.strip():
        raise ValueError("Android NDK version is required")
    if not source_url.startswith("https://"):
        raise ValueError("CPython source URL must use HTTPS")
    if len(source_digest) != 64 or any(
        character not in "0123456789abcdef" for character in source_digest
    ):
        raise ValueError("CPython source SHA-256 must contain 64 hexadecimal digits")

    wheels = []
    for wheel in sorted((root / "wheels").glob("*.whl"), key=lambda path: path.name):
        wheels.append(
            {
                "file": wheel.name,
                "sha256": _sha256_file(wheel),
                "bytes": wheel.stat().st_size,
            }
        )
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "kind": RUNTIME_KIND,
        "target": {
            "abi": abi,
            "minimum_android_api": minimum_android_api,
        },
        "cpython": {
            "version": python_version,
            "android_api": cpython_android_api,
            "source": {
                "url": source_url,
                "sha256": source_digest,
            },
        },
        "toolchain": {"ndk_version": ndk_version.strip()},
        "wheels": wheels,
        "payload": hash_runtime_payload(root),
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def validate_runtime_manifest(
    prefix: Path,
    *,
    expected_abi: str,
    expected_python_series: str,
    application_minimum_android_api: int,
) -> dict[str, Any]:
    """Validate provenance, compatibility, wheel records, and payload bytes."""

    root = prefix.resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(
            f"Android Python prefix has no {MANIFEST_NAME}: {root}. "
            "Prepare or stamp it with scripts/setup/android_python_runtime.py."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Android Python runtime manifest is unreadable: {manifest_path}"
        ) from error
    try:
        schema = manifest["schema"]
        kind = manifest["kind"]
        target = manifest["target"]
        cpython = manifest["cpython"]
        toolchain = manifest["toolchain"]
        wheels = manifest["wheels"]
        payload = manifest["payload"]
        abi = target["abi"]
        minimum_api = target["minimum_android_api"]
        python_version = cpython["version"]
        cpython_api = cpython["android_api"]
        source = cpython["source"]
        ndk_version = toolchain["ndk_version"]
    except (KeyError, TypeError) as error:
        raise ValueError(
            f"Android Python runtime manifest has an incomplete schema: {manifest_path}"
        ) from error
    if schema != MANIFEST_SCHEMA or kind != RUNTIME_KIND:
        raise ValueError(
            f"Unsupported Android Python runtime manifest schema or kind: {manifest_path}"
        )
    if abi != expected_abi:
        raise ValueError(
            f"Android Python prefix targets {abi}, but this build targets {expected_abi}: {root}"
        )
    if not isinstance(python_version, str) or not python_version.startswith(
        expected_python_series + "."
    ):
        raise ValueError(
            "Android Player requires CPython "
            f"{expected_python_series}, but the runtime manifest provides "
            f"{python_version}: {root}"
        )
    if (
        not isinstance(cpython_api, int)
        or not isinstance(minimum_api, int)
        or cpython_api < 21
        or minimum_api < cpython_api
    ):
        raise ValueError(f"Android Python runtime API contract is invalid: {manifest_path}")
    if minimum_api > application_minimum_android_api:
        raise ValueError(
            f"Android Python runtime requires API {minimum_api}, but the Player minimum "
            f"is API {application_minimum_android_api}: {root}"
        )
    if not isinstance(ndk_version, str) or not ndk_version.strip():
        raise ValueError(f"Android Python runtime NDK provenance is missing: {manifest_path}")
    source_digest = str(source.get("sha256", "")) if isinstance(source, dict) else ""
    if (
        not isinstance(source, dict)
        or not str(source.get("url", "")).startswith("https://")
        or len(source_digest) != 64
        or any(character not in "0123456789abcdef" for character in source_digest)
    ):
        raise ValueError(f"Android Python runtime source provenance is invalid: {manifest_path}")
    if not isinstance(wheels, list):
        raise ValueError(f"Android Python runtime wheel records are invalid: {manifest_path}")
    for record in wheels:
        try:
            wheel_name = record["file"]
            expected_hash = record["sha256"]
            expected_bytes = record["bytes"]
        except (KeyError, TypeError) as error:
            raise ValueError(
                f"Android Python runtime wheel record is incomplete: {manifest_path}"
            ) from error
        if (
            not isinstance(wheel_name, str)
            or Path(wheel_name).name != wheel_name
            or not wheel_name.endswith(".whl")
        ):
            raise ValueError(
                f"Android Python runtime wheel name is unsafe: {wheel_name}"
            )
        wheel = root / "wheels" / wheel_name
        if (
            not wheel.is_file()
            or wheel.stat().st_size != expected_bytes
            or _sha256_file(wheel) != expected_hash
        ):
            raise ValueError(f"Android Python runtime wheel does not match its manifest: {wheel}")

    actual_payload = hash_runtime_payload(root)
    if payload != actual_payload:
        raise ValueError(
            f"Android Python prefix payload does not match {MANIFEST_NAME}: {root}"
        )
    return manifest
