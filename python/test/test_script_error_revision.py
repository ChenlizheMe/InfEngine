from __future__ import annotations

from Infernux.components.script_loader import (
    _clear_script_error,
    get_script_error_by_path,
    get_script_error_revision,
    set_script_error,
)


def test_script_error_revision_changes_only_with_diagnostics(tmp_path):
    script = tmp_path / "broken_component.py"
    _clear_script_error(str(script))
    initial = get_script_error_revision()

    set_script_error(str(script), "first error")
    assert get_script_error_by_path(str(script)) == "first error"
    assert get_script_error_revision() == initial + 1

    set_script_error(str(script), "first error")
    assert get_script_error_revision() == initial + 1

    set_script_error(str(script), "updated error")
    assert get_script_error_revision() == initial + 2

    _clear_script_error(str(script))
    assert get_script_error_by_path(str(script)) is None
    assert get_script_error_revision() == initial + 3
