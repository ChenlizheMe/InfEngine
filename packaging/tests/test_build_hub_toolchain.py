from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

import build_hub


_VALID_MSVC_REPORT = """\
LINK=link
MSVC_InstallDirectory=C:\\Program Files\\Microsoft Visual Studio\\2022\\Community
gcc_mode=False
clang_mode=False
msvc_mode=True
mingw_mode=False
"""


def test_msbuild_generator_is_required_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(build_hub.os, "name", "nt")

    build_hub._require_msbuild_generator("Visual Studio 17 2022")
    with pytest.raises(RuntimeError, match="Visual Studio/MSBuild"):
        build_hub._require_msbuild_generator("Ninja")


def test_msvc_report_validation_accepts_only_msvc(tmp_path: Path):
    report = tmp_path / "launcher.build" / "scons-report.txt"
    report.parent.mkdir()
    report.write_text(_VALID_MSVC_REPORT, encoding="utf-8")

    assert build_hub._validate_msvc_reports(tmp_path) == [report]

    report.write_text(
        _VALID_MSVC_REPORT.replace("msvc_mode=True", "msvc_mode=False").replace(
            "mingw_mode=False", "mingw_mode=True"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="not an MSVC-only build"):
        build_hub._validate_msvc_reports(tmp_path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            r"C:\\ProgramData\\anaconda3\\envs\\infernux\\Library\\mingw-w64\\bin",
            True,
        ),
        (r"C:\\msys64\\usr\\bin", True),
        (r"C:\\Program Files\\Microsoft Visual Studio\\2022\\Community", False),
    ],
)
def test_mingw_paths_are_removed_from_release_environment(path: str, expected: bool):
    assert build_hub._is_mingw_path(path) is expected


@pytest.mark.parametrize(
    ("project_version", "file_version"),
    [
        ("0.2.9", "0.2.9.0"),
        ("1.2.3.4", "1.2.3.4"),
        ("0.3.0-preview.1", "0.3.0.0"),
        ("2.0.0+local", "2.0.0.0"),
    ],
)
def test_project_versions_are_valid_windows_file_versions(
    project_version: str, file_version: str
):
    assert build_hub._windows_file_version(project_version) == file_version


def test_invalid_windows_file_version_is_rejected():
    with pytest.raises(RuntimeError, match="Windows file version"):
        build_hub._windows_file_version("preview")


def test_unsigned_build_is_explicitly_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("INFERNUX_SIGN_CERTIFICATE_THUMBPRINT", raising=False)

    assert build_hub._sign_windows_binary(tmp_path / "app.exe", {}) is False
