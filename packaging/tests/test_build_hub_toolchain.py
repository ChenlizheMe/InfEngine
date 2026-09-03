from __future__ import annotations

import sys
import json
import zipfile
from pathlib import Path, PureWindowsPath

import pytest


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

import build_hub
from private_python_runtime import PYTHON_VERSION, runtime_archive_for_machine
from python_runtime_catalog import DEFAULT_PYTHON_RUNTIME


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


def test_hub_build_requires_the_private_runtime_bundle(tmp_path: Path):
    source_root = tmp_path / "source"
    (source_root / "packaging").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="private Python runtime bundle is missing"):
        build_hub._build_hub(
            source_root,
            tmp_path / "build",
            tmp_path / "dist",
            cmake_generator="Visual Studio 17 2022",
            build_env=None,
            tools=None,
        )


def test_hub_build_embeds_the_private_runtime_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_root = tmp_path / "source"
    packaging_dir = source_root / "packaging"
    package_dir = tmp_path / "dist"
    runtime_bundle = package_dir / "runtime" / "runtime_bundle.zip"
    runtime_bundle.parent.mkdir(parents=True)
    archive = runtime_archive_for_machine()
    marker = {
        "owner": "Infernux Hub",
        "kind": "private-python-runtime",
        "python_version": PYTHON_VERSION,
        "python_series": DEFAULT_PYTHON_RUNTIME.series,
        "source_archive": archive.name,
        "source_archive_sha256": archive.sha256,
    }
    with zipfile.ZipFile(runtime_bundle, "w") as bundle:
        bundle.writestr(
            f"{DEFAULT_PYTHON_RUNTIME.directory_name}/.infernux-private-python-runtime.json",
            json.dumps(marker),
        )
    (packaging_dir / "resources").mkdir(parents=True)
    (packaging_dir / "resources" / "icon.png").write_bytes(b"icon")
    (packaging_dir / "launcher.py").write_text("pass\n", encoding="utf-8")

    build_dir = tmp_path / "build"
    captured: list[list[str]] = []

    monkeypatch.setattr(build_hub, "_common_nuitka_command", lambda *args, **kwargs: ["nuitka"])
    monkeypatch.setattr(build_hub, "_project_version", lambda _root: "0.2.9")
    monkeypatch.setattr(build_hub, "_validate_msvc_reports", lambda _root: [])
    monkeypatch.setattr(build_hub, "_sign_windows_binary", lambda *args: None)
    monkeypatch.setattr(build_hub, "_validate_windows_payload", lambda *args: None)
    monkeypatch.setattr(build_hub, "_write_toolchain_receipt", lambda *args, **kwargs: None)

    def _fake_run(command, **_kwargs):
        captured.append(command)
        output = build_dir / "nuitka" / "launcher.dist"
        output.mkdir(parents=True)
        (output / "Infernux Hub.exe").write_bytes(b"hub")

    monkeypatch.setattr(build_hub, "_run", _fake_run)

    build_hub._build_hub(
        source_root,
        build_dir,
        package_dir,
        cmake_generator="Visual Studio 17 2022",
        build_env={},
        tools={"visual_studio": "", "msbuild": "", "cl": "", "link": ""},
    )

    assert captured
    assert any(
        argument.endswith(
            "=InfernuxHubData/runtime/runtime_bundle.zip"
        )
        and str(runtime_bundle) in argument
        for argument in captured[0]
    )


def test_hub_build_rejects_a_stale_private_runtime_bundle(tmp_path: Path):
    source_root = tmp_path / "source"
    runtime_bundle = tmp_path / "dist" / "runtime" / "runtime_bundle.zip"
    runtime_bundle.parent.mkdir(parents=True)
    with zipfile.ZipFile(runtime_bundle, "w") as bundle:
        bundle.writestr(
            f"{DEFAULT_PYTHON_RUNTIME.directory_name}/.infernux-private-python-runtime.json",
            json.dumps(
                {
                    "owner": "Infernux Hub",
                    "kind": "private-python-runtime",
                    "python_version": "3.11.9",
                    "python_series": "3.11",
                    "source_archive": "python-3.11.9-amd64.exe",
                    "source_archive_sha256": "revoked-installer",
                }
            ),
        )

    with pytest.raises(RuntimeError, match="runtime bundle is stale"):
        build_hub._build_hub(
            source_root,
            tmp_path / "build",
            tmp_path / "dist",
            cmake_generator="Visual Studio 17 2022",
            build_env=None,
            tools=None,
        )


def test_hub_build_rejects_extra_legacy_runtime_payload(tmp_path: Path):
    source_root = tmp_path / "source"
    runtime_bundle = tmp_path / "dist" / "runtime" / "runtime_bundle.zip"
    runtime_bundle.parent.mkdir(parents=True)
    archive = runtime_archive_for_machine()
    marker = {
        "owner": "Infernux Hub",
        "kind": "private-python-runtime",
        "python_version": PYTHON_VERSION,
        "python_series": DEFAULT_PYTHON_RUNTIME.series,
        "source_archive": archive.name,
        "source_archive_sha256": archive.sha256,
    }
    with zipfile.ZipFile(runtime_bundle, "w") as bundle:
        bundle.writestr(
            f"{DEFAULT_PYTHON_RUNTIME.directory_name}/.infernux-private-python-runtime.json",
            json.dumps(marker),
        )
        bundle.writestr("python311/python.exe", b"non-target runtime")

    with pytest.raises(RuntimeError, match="contains files outside"):
        build_hub._build_hub(
            source_root,
            tmp_path / "build",
            tmp_path / "dist",
            cmake_generator="Visual Studio 17 2022",
            build_env=None,
            tools=None,
        )


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


def test_nuitka_build_uses_an_ascii_temporary_directory():
    build_dir = Path(r"C:\Users\测试\source\build")
    build_env = {
        "ProgramData": r"C:\ProgramData",
        "TEMP": r"C:\Users\测试\AppData\Local\Temp",
    }

    result = build_hub._nuitka_temp_directory(build_env, build_dir)

    str(result).encode("ascii")
    assert PureWindowsPath(result).is_relative_to(
        PureWindowsPath(r"C:\ProgramData\Infernux\BuildTemp")
    )


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
