from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "acceptance" / "editor_project_smoke.py"
)
_SPEC = importlib.util.spec_from_file_location("editor_project_smoke", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_native_dialog_result_requires_the_exact_selected_path(tmp_path: Path) -> None:
    selected = tmp_path / "selected.txt"

    assert _MODULE._require_dialog_path(
        {"accepted": True, "cancelled": False, "path": str(selected), "error": ""},
        str(selected),
    ) == str(selected.resolve())


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"accepted": False, "cancelled": True, "path": "", "error": ""}, "did not accept"),
        ({"accepted": False, "cancelled": False, "path": "", "error": "portal failed"}, "portal failed"),
        ({"accepted": True, "cancelled": False, "path": "wrong.txt", "error": ""}, "expected"),
    ],
)
def test_native_dialog_result_rejects_cancel_error_and_wrong_path(
    tmp_path: Path, result: dict[str, object], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _MODULE._require_dialog_path(result, str(tmp_path / "selected.txt"))
