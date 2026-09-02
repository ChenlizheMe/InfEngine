from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
MODEL_DIR = PACKAGING_DIR / "model"
for directory in (PACKAGING_DIR, MODEL_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

project_model = importlib.import_module("project_model")


def test_explicit_python_abi_probe_does_not_fall_back_to_hub_python(monkeypatch) -> None:
    monkeypatch.setattr(
        project_model.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing runtime")),
    )

    with pytest.raises(OSError, match="missing runtime"):
        project_model._python_cp_tag("missing-python")


def test_explicit_python_abi_probe_rejects_invalid_output(monkeypatch) -> None:
    monkeypatch.setattr(
        project_model.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-a-tag\n", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="Unable to determine the Python ABI tag"):
        project_model._python_cp_tag("python")


def test_distribution_scan_propagates_filesystem_failures(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    original_listdir = os.listdir

    def fail_selected_directory(path):
        if Path(path) == site_packages:
            raise PermissionError("catalog denied")
        return original_listdir(path)

    monkeypatch.setattr(project_model.os, "listdir", fail_selected_directory)

    with pytest.raises(PermissionError, match="catalog denied"):
        project_model._distribution_files_present(str(site_packages), "Infernux")
    with pytest.raises(PermissionError, match="catalog denied"):
        project_model._remove_installed_distribution(str(site_packages), "Infernux")
