"""Current runtime contract for Android CPython prefixes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MANIFEST_NAME = "infernux-android-python.json"
MANIFEST_SCHEMA = "infernux.android_python_runtime"
RUNTIME_KIND = "infernux-android-cpython"


def create_runtime_manifest(
    prefix: Path,
    *,
    abi: str,
    python_version: str,
    cpython_android_api: int,
    minimum_android_api: int,
    ndk_version: str,
    source_url: str,
) -> dict[str, Any]:
    """Create and persist the current manifest for one prepared prefix."""

    root = prefix.resolve()
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
    wheels = []
    for wheel in sorted((root / "wheels").glob("*.whl"), key=lambda path: path.name):
        wheels.append(
            {
                "file": wheel.name,
                "bytes": wheel.stat().st_size,
            }
        )
    manifest: dict[str, Any] = {
        "$schema": MANIFEST_SCHEMA,
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
            },
        },
        "toolchain": {"ndk_version": ndk_version.strip()},
        "wheels": wheels,
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
    """Validate the current runtime compatibility contract and required files."""

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
    if type(manifest) is not dict or set(manifest) != {
        "$schema",
        "kind",
        "target",
        "cpython",
        "toolchain",
        "wheels",
    }:
        raise ValueError(
            f"Android Python runtime manifest does not match the current contract: {manifest_path}"
        )
    try:
        schema = manifest["$schema"]
        kind = manifest["kind"]
        target = manifest["target"]
        cpython = manifest["cpython"]
        toolchain = manifest["toolchain"]
        wheels = manifest["wheels"]
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
    if (
        type(target) is not dict
        or set(target) != {"abi", "minimum_android_api"}
        or type(cpython) is not dict
        or set(cpython) != {"version", "android_api", "source"}
        or type(source) is not dict
        or set(source) != {"url"}
        or type(toolchain) is not dict
        or set(toolchain) != {"ndk_version"}
    ):
        raise ValueError(
            f"Android Python runtime manifest does not match the current contract: {manifest_path}"
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
    if not str(source["url"]).startswith("https://"):
        raise ValueError(f"Android Python runtime source provenance is invalid: {manifest_path}")
    if not isinstance(wheels, list):
        raise ValueError(f"Android Python runtime wheel records are invalid: {manifest_path}")
    for record in wheels:
        if type(record) is not dict or set(record) != {"file", "bytes"}:
            raise ValueError(
                f"Android Python runtime wheel record does not match the current contract: {manifest_path}"
            )
        try:
            wheel_name = record["file"]
            expected_bytes = record["bytes"]
        except (KeyError, TypeError) as error:
            raise ValueError(
                f"Android Python runtime wheel record is incomplete: {manifest_path}"
            ) from error
        if (
            not isinstance(wheel_name, str)
            or Path(wheel_name).name != wheel_name
            or not wheel_name.endswith(".whl")
            or type(expected_bytes) is not int
            or expected_bytes < 0
        ):
            raise ValueError(
                f"Android Python runtime wheel name is unsafe: {wheel_name}"
            )
        wheel = root / "wheels" / wheel_name
        if not wheel.is_file() or wheel.stat().st_size != expected_bytes:
            raise ValueError(f"Android Python runtime wheel does not match its manifest: {wheel}")
    include_dir = root / f"include/python{expected_python_series}"
    stdlib_dir = root / f"lib/python{expected_python_series}"
    if not include_dir.is_dir() or not stdlib_dir.is_dir():
        raise ValueError(f"Android Python prefix is incomplete: {root}")
    return manifest
