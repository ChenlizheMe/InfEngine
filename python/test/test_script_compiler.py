from __future__ import annotations

from Infernux.engine.script_compiler import ScriptCompiler


def test_check_source_validates_captured_bytes_without_rereading_disk(tmp_path):
    path = tmp_path / "controller.py"
    path.write_text("def broken(:\n", encoding="utf-8")

    errors = ScriptCompiler().check_source(str(path), b"value = 1\n")

    assert errors == []


def test_check_source_reports_errors_from_the_captured_snapshot(tmp_path):
    path = tmp_path / "controller.py"
    path.write_text("value = 1\n", encoding="utf-8")

    errors = ScriptCompiler().check_source(str(path), b"def broken(:\n")

    assert len(errors) == 1
    assert errors[0].error_type == "syntax"
    assert errors[0].file_path == str(path)
