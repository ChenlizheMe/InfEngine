from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PACKAGING_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

from PySide6.QtWidgets import QApplication

import view.new_project_view as new_project_view_module
from view.new_project_view import NewProjectView


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _InstalledEngines:
    @staticmethod
    def installed_versions() -> list[str]:
        return ["0.4.0", "0.3.7"]


class _ForbiddenRuntimeProbe:
    def __getattr__(self, name):
        raise AssertionError(
            f"New Project must not query Python runtime availability: {name}"
        )


def test_new_project_lists_installed_engines_without_python_gate(
    app, monkeypatch
):
    monkeypatch.setattr(new_project_view_module, "is_frozen", lambda: True)

    dialog = NewProjectView(
        _InstalledEngines(),
        _ForbiddenRuntimeProbe(),
    )

    assert [
        dialog.version_combo.itemData(index)
        for index in range(dialog.version_combo.count())
    ] == ["0.4.0", "0.3.7"]
    assert not hasattr(dialog, "python_combo")
    assert not hasattr(dialog, "_runtime_hint")
    dialog.close()
