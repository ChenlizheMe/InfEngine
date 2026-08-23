from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[2] / "cmake" / "clean_installed_infernux.py"
_SPEC = importlib.util.spec_from_file_location("clean_installed_infernux", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_guard_rejects_loaded_installed_package(monkeypatch, tmp_path: Path) -> None:
    package = tmp_path / "Infernux"
    module = package / "lib" / "InfernuxRendererRuntime.dll"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setattr(
        _MODULE,
        "_loaded_windows_package_modules",
        lambda _roots: [(1234, "python.exe", module)],
    )

    with pytest.raises(RuntimeError, match="close all editors/players"):
        _MODULE.guard_not_loaded((tmp_path,))


def test_guard_accepts_idle_installed_package(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "Infernux").mkdir()
    monkeypatch.setattr(
        _MODULE,
        "_loaded_windows_package_modules",
        lambda _roots: [],
    )

    _MODULE.guard_not_loaded((tmp_path,))

