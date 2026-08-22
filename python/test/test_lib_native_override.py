import os
import sys
import types

import pytest

import Infernux.lib as lib


def test_crt_preload_uses_ctypes_and_preserves_dependency_order(tmp_path, monkeypatch):
    for name in ("vcruntime140.dll", "msvcp140.dll", "concrt140.dll"):
        (tmp_path / name).write_bytes(b"crt")
    loaded = []
    monkeypatch.setattr(lib, "native_dir", str(tmp_path))
    monkeypatch.setattr(lib.sys, "platform", "win32")
    monkeypatch.setattr(lib.ctypes, "WinDLL", lambda path: loaded.append(path))

    lib._preload_bundled_crt_dlls()

    assert [os.path.basename(path) for path in loaded] == [
        "vcruntime140.dll",
        "msvcp140.dll",
        "concrt140.dll",
    ]


def test_override_loads_exact_abi_module_under_package_name(tmp_path, monkeypatch):
    abi_module = tmp_path / "_Infernux.test-abi.pyd"
    short_module = tmp_path / "_Infernux.pyd"
    abi_module.write_bytes(b"abi")
    short_module.write_bytes(b"short")
    loaded = []

    class _Loader:
        def exec_module(self, module):
            loaded.append((module.__name__, sys.modules.get(module.__name__) is module))
            module.PublicSymbol = "override"

    spec = types.SimpleNamespace(loader=_Loader())
    monkeypatch.setattr(
        lib.importlib.machinery,
        "EXTENSION_SUFFIXES",
        [".test-abi.pyd", ".pyd"],
    )
    monkeypatch.setattr(
        lib.importlib.util,
        "spec_from_file_location",
        lambda name, path: spec if path == str(abi_module) else None,
    )
    monkeypatch.setattr(
        lib.importlib.util,
        "module_from_spec",
        lambda _spec: types.ModuleType("Infernux.lib._Infernux"),
    )
    monkeypatch.setitem(
        sys.modules,
        "Infernux.lib._Infernux",
        types.ModuleType("old_native_module"),
    )

    module = lib._load_native_module_from_dir(str(tmp_path))

    assert loaded == [("Infernux.lib._Infernux", True)]
    assert sys.modules["Infernux.lib._Infernux"] is module
    assert module.PublicSymbol == "override"


def test_native_override_diagnostic_reports_actual_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(lib, "native_dir", str(tmp_path))
    monkeypatch.setattr(lib, "_collect_windows_native_load_hints", lambda: [])
    monkeypatch.setattr(lib, "_list_lib_dir_contents", lambda: [])

    with pytest.raises(ImportError) as caught:
        lib._raise_native_import_error(ImportError("load failed"))

    assert f"Native directory: {tmp_path}" in str(caught.value)
    assert f"Library directory: {lib.lib_dir}" not in str(caught.value)


def test_native_loader_without_override_preserves_package_import(monkeypatch):
    expected = types.ModuleType("Infernux.lib._Infernux")
    imported = []

    def _import_module(name):
        imported.append(name)
        return expected

    monkeypatch.setattr(lib.importlib, "import_module", _import_module)

    assert lib._load_native_module(None) is expected
    assert imported == ["Infernux.lib._Infernux"]
