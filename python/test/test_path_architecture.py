from __future__ import annotations

from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_python_filesystem_identity_is_owned_by_path_utils():
    forbidden = re.compile(r"os\.path\.(?:normcase|realpath|commonpath|commonprefix)\s*\(")
    violations: list[str] = []
    source_root = REPOSITORY_ROOT / "python" / "Infernux"
    owner = source_root / "engine" / "path_utils.py"
    for path in source_root.rglob("*.py"):
        if path == owner or "__pycache__" in path.parts:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                violations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}")
    assert not violations, "Ad-hoc Python path identity logic: " + ", ".join(violations)


def test_python_filesystem_normalization_is_owned_by_path_utils():
    forbidden = re.compile(
        r"os\.path\.(?:abspath|normpath|relpath|samefile)\s*\(|"
        r"Path\([^\r\n]*\)\.resolve\s*\("
    )
    source_root = REPOSITORY_ROOT / "python" / "Infernux"
    owner = source_root / "engine" / "path_utils.py"
    # These modules run before engine.path_utils can be imported safely and
    # only locate their own installed package directories.
    early_bootstrap = {
        source_root / "lib" / "__init__.py",
        source_root / "resources" / "__init__.py",
    }
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        if path == owner or path in early_bootstrap or "__pycache__" in path.parts:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                violations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}")
    assert not violations, "Ad-hoc Python path normalization: " + ", ".join(violations)


def test_cpp_filesystem_identity_is_owned_by_inx_path():
    forbidden = re.compile(
        r"(?:weakly_canonical|filesystem::canonical|fs::canonical|"
        r"filesystem::relative|fs::relative|lexically_relative|lexically_normal)\s*\("
    )
    violations: list[str] = []
    source_root = REPOSITORY_ROOT / "cpp" / "infernux"
    owner = source_root / "platform" / "filesystem" / "InxPath.h"
    for suffix in ("*.h", "*.hpp", "*.cpp"):
        for path in source_root.rglob(suffix):
            if path == owner:
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if forbidden.search(line):
                    violations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}")
    assert not violations, "Ad-hoc C++ path identity logic: " + ", ".join(violations)
