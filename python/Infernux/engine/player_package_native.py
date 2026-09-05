"""Thin Python bridge to the native InxPack writer/reader.

The package format and all compression/checksum logic live in the C++
filesystem module.  This file only converts Python arguments and deliberately
raises when the native backend is unavailable.  Tests may install a fake
backend with ``set_test_backend``; production always uses the package-owned
native module.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from typing import Any, Callable

from Infernux.engine.path_utils import lexical_path


_test_backend: Any | None = None

ASSET_CATALOG_ARCHIVE_FILENAME = "AssetCatalog.inxcat"
ASSET_CATALOG_ENTRY_PATH = "RuntimeAssetCatalog.json"
BUILD_MANIFEST_ENTRY_PATH = "BuildManifest.json"


def set_test_backend(backend: Any | None) -> None:
    """Install a fake native backend for contract tests only."""

    global _test_backend
    _test_backend = backend


def using_test_backend() -> bool:
    """Return whether contract tests replaced the native package backend."""

    return _test_backend is not None


def _backend() -> Any:
    if _test_backend is not None:
        return _test_backend
    return importlib.import_module("Infernux.lib._Infernux")


def write_pack(
    files: Iterable[tuple[str, str | os.PathLike[str]]],
    destination: str | os.PathLike[str],
    *,
    compression_level: int | None = None,
    profile: str = "development",
) -> dict[str, object]:
    source_pairs = [(str(path), os.fspath(source)) for path, source in files]
    return dict(
        _backend()._inxpack_write(
            source_pairs,
            os.fspath(destination),
            compression_level,
            profile,
        )
    )


def _remove_worker_temporary_outputs(destination: str) -> None:
    directory = os.path.dirname(destination) or "."
    prefix = os.path.basename(destination) + ".tmp.inxpkg."
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def write_pack_isolated(
    files: Iterable[tuple[str, str | os.PathLike[str]]],
    destination: str | os.PathLike[str],
    *,
    compression_level: int | None = None,
    profile: str = "development",
    cancel_event: threading.Event | None = None,
    on_wait: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Write a package in a helper process so Editor Python stays responsive."""

    if using_test_backend():
        return write_pack(
            files,
            destination,
            compression_level=compression_level,
            profile=profile,
        )

    destination_text = os.fspath(destination)
    source_pairs = [(str(path), os.fspath(source)) for path, source in files]
    output_directory = os.path.dirname(lexical_path(destination_text))
    os.makedirs(output_directory, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".infernux-inxpack-worker-", dir=output_directory
    ) as workspace:
        request_path = os.path.join(workspace, "request.json")
        response_path = os.path.join(workspace, "response.json")
        stderr_path = os.path.join(workspace, "stderr.log")
        with open(request_path, "w", encoding="utf-8") as output:
            json.dump(
                {
                    "files": source_pairs,
                    "destination": destination_text,
                    "compression_level": compression_level,
                    "profile": profile,
                },
                output,
                ensure_ascii=False,
            )

        environment = os.environ.copy()
        environment.setdefault("PYTHONUTF8", "1")
        package_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        existing_python_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (package_root, existing_python_path) if part
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        with open(stderr_path, "wb") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "Infernux.engine._inxpack_worker",
                    request_path,
                    response_path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=environment,
                creationflags=creationflags,
            )
            try:
                while process.poll() is None:
                    if on_wait is not None:
                        on_wait()
                    if cancel_event is not None and cancel_event.is_set():
                        process.terminate()
                        try:
                            process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        _remove_worker_temporary_outputs(destination_text)
                        raise RuntimeError("InxPack write was cancelled")
                    time.sleep(0.05)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise

        response: dict[str, object] = {}
        try:
            with open(response_path, "r", encoding="utf-8") as source:
                loaded = json.load(source)
                if isinstance(loaded, dict):
                    response = loaded
        except (OSError, ValueError):
            pass
        if process.returncode != 0 or not response.get("ok"):
            try:
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as source:
                    diagnostics = source.read().strip()
            except OSError:
                diagnostics = ""
            message = str(response.get("error") or diagnostics or "unknown worker failure")
            _remove_worker_temporary_outputs(destination_text)
            raise RuntimeError(f"InxPack worker failed: {message}")
        manifest = response.get("manifest")
        if not isinstance(manifest, dict):
            raise RuntimeError("InxPack worker returned no manifest")
        return manifest


def read_manifest(path: str | os.PathLike[str]) -> dict[str, object]:
    return dict(_backend()._inxpack_read_manifest(os.fspath(path)))


def extract_pack(
    path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    allowed_roots: set[str] | None = None,
) -> dict[str, object]:
    roots = None if allowed_roots is None else sorted(str(root) for root in allowed_roots)
    return dict(_backend()._inxpack_extract(os.fspath(path), os.fspath(destination), roots))


def read_entry(path: str | os.PathLike[str], entry_path: str) -> bytes:
    return bytes(_backend()._inxpack_read_entry(os.fspath(path), str(entry_path)))


__all__ = [
    "ASSET_CATALOG_ARCHIVE_FILENAME",
    "ASSET_CATALOG_ENTRY_PATH",
    "BUILD_MANIFEST_ENTRY_PATH",
    "extract_pack",
    "read_entry",
    "read_manifest",
    "set_test_backend",
    "using_test_backend",
    "write_pack",
    "write_pack_isolated",
]
