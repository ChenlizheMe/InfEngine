"""Verify development and installed-package imports of the bootstrap module."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path


EXPECTED_APIS = (
    "_inxpack_read_manifest",
    "_inxpack_extract",
    "_inxpack_read_entry",
    "_inxplayer_show_error",
    "_inxplayer_process_is_alive",
)


def _assert_api_surface(module: object) -> None:
    for name in EXPECTED_APIS:
        assert callable(getattr(module, name, None)), f"missing bootstrap API: {name}"


def _package_shell(name: str, search_paths: list[str]):
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = search_paths
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    return module


def main() -> None:
    configured_dir = os.environ.get("INFERNUX_NATIVE_MODULE_DIR")
    if not configured_dir:
        raise RuntimeError("INFERNUX_NATIVE_MODULE_DIR is required")

    native_dir = Path(configured_dir).resolve()
    if not native_dir.is_dir():
        raise RuntimeError(f"INFERNUX_NATIVE_MODULE_DIR is not a directory: {native_dir}")

    sys.path.insert(0, str(native_dir))
    dll_directory = os.add_dll_directory(str(native_dir)) if os.name == "nt" else None
    try:
        development_module = importlib.import_module("_InfernuxBootstrap")
        assert Path(development_module.__file__).resolve().parent == native_dir
        _assert_api_surface(development_module)
        assert development_module._inxplayer_process_is_alive(os.getpid()) is True

        infernux_package = _package_shell("Infernux", [])
        lib_package = _package_shell("Infernux.lib", [str(native_dir)])
        infernux_package.lib = lib_package
        installed_module = importlib.import_module("Infernux.lib._InfernuxBootstrap")
        assert Path(installed_module.__file__).resolve().parent == native_dir
        _assert_api_surface(installed_module)
        assert "Infernux.lib._Infernux" not in sys.modules
    finally:
        if dll_directory is not None:
            dll_directory.close()


if __name__ == "__main__":
    main()
