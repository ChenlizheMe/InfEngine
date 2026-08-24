"""Build the Hub and installer through MSBuild with Nuitka's MSVC backend."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Mapping

from installer.payload import HUB_PAYLOAD_ARCHIVE, create_payload_archive
from private_python_runtime import PYTHON_VERSION, runtime_archive_for_machine


_VISUAL_STUDIO_GENERATOR_PREFIX = "Visual Studio "
_FORBIDDEN_WINDOWS_RUNTIME_IMPORTS = (
    "libgcc_s_",
    "libstdc++",
    "libwinpthread",
    "msys-",
)
_SIGNING_THUMBPRINT_ENV = "INFERNUX_SIGN_CERTIFICATE_THUMBPRINT"
_SIGNING_TIMESTAMP_ENV = "INFERNUX_SIGN_TIMESTAMP_URL"
_DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"


def _validate_runtime_bundle(bundle_path: Path) -> None:
    archive = runtime_archive_for_machine()
    marker_name = "python312/.infernux-private-python-runtime.json"
    try:
        with zipfile.ZipFile(bundle_path) as bundle:
            marker = json.loads(bundle.read(marker_name))
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise RuntimeError(
            "The private Python runtime bundle is invalid or missing its provenance marker. "
            "Rebuild prepare_bundled_python_runtime before packaging Infernux Hub."
        ) from exc

    if not (
        marker.get("owner") == "Infernux Hub"
        and marker.get("kind") == "private-python-runtime"
        and marker.get("python_version") == PYTHON_VERSION
        and marker.get("source_archive") == archive.name
        and marker.get("source_archive_sha256") == archive.sha256
    ):
        raise RuntimeError(
            "The private Python runtime bundle is stale or does not match the pinned "
            f"Python {PYTHON_VERSION} archive. Rebuild prepare_bundled_python_runtime."
        )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _require_msbuild_generator(cmake_generator: str) -> None:
    if os.name == "nt" and not cmake_generator.startswith(
        _VISUAL_STUDIO_GENERATOR_PREFIX
    ):
        raise RuntimeError(
            "Windows Hub packaging must be launched by the Visual Studio/MSBuild "
            "CMake preset. Run: cmake --build --preset packaging-installer"
        )


def _find_visual_studio() -> Path:
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if not program_files_x86:
        raise RuntimeError(
            "ProgramFiles(x86) is unavailable; Visual Studio cannot be located"
        )
    vswhere = (
        Path(program_files_x86)
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )
    if not vswhere.is_file():
        raise RuntimeError(f"Visual Studio locator is missing: {vswhere}")
    result = subprocess.run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    installation = Path(result.stdout.strip())
    if not installation.is_dir():
        raise RuntimeError(
            "Visual Studio with the Desktop development with C++ workload is required"
        )
    return installation


def _is_mingw_path(path: str) -> bool:
    normalized = path.replace("/", "\\").casefold()
    return "\\mingw" in normalized or "\\msys" in normalized


def _msvc_build_environment() -> tuple[dict[str, str], dict[str, str]]:
    installation = _find_visual_studio()
    vcvars = installation / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    msbuild = installation / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
    if not vcvars.is_file() or not msbuild.is_file():
        raise RuntimeError(
            f"Visual Studio C++/MSBuild tools are incomplete under {installation}"
        )

    result = subprocess.run(
        f'cmd.exe /d /c call "{vcvars}" >nul && set',
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )
    env = dict(os.environ)
    for line in result.stdout.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        key, value = line.split("=", 1)
        if key.casefold() == "path":
            for existing in tuple(env):
                if existing.casefold() == "path":
                    env.pop(existing)
            env["PATH"] = value
        else:
            env[key] = value

    # A Conda environment can expose mingw-w64 ahead of Visual Studio. Nuitka is
    # forced to MSVC below, and removing these entries prevents helper/tool lookup
    # or a future Nuitka change from silently selecting MinGW.
    env["PATH"] = os.pathsep.join(
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if not _is_mingw_path(part)
    )
    for key in ("CC", "CXX", "AR", "LD"):
        env.pop(key, None)

    tools: dict[str, str] = {
        "msbuild": str(msbuild),
        "visual_studio": str(installation),
    }
    for executable in ("cl.exe", "link.exe", "dumpbin.exe", "rc.exe"):
        resolved = shutil.which(executable, path=env.get("PATH"))
        if not resolved:
            raise RuntimeError(
                f"Required MSVC tool is unavailable after vcvars64: {executable}"
            )
        tools[executable.removesuffix(".exe")] = resolved

    for executable in ("cl", "link", "dumpbin"):
        if not Path(tools[executable]).is_relative_to(installation):
            raise RuntimeError(
                f"{executable}.exe did not resolve to Visual Studio: "
                f"{tools[executable]}"
            )
    return env, tools


def _project_version(source_root: Path) -> str:
    project = tomllib.loads(
        (source_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(project["project"]["version"])


def _windows_file_version(version: str) -> str:
    numeric = version.split("-", 1)[0].split("+", 1)[0]
    parts = numeric.split(".")
    if not 1 <= len(parts) <= 4 or any(not part.isdigit() for part in parts):
        raise RuntimeError(
            f"Cannot convert project version to a Windows file version: {version}"
        )
    return ".".join(parts + ["0"] * (4 - len(parts)))


def _windows_metadata_options(
    source_root: Path,
    *,
    product_name: str,
    description: str,
    original_filename: str,
) -> list[str]:
    file_version = _windows_file_version(_project_version(source_root))
    icon = source_root / "python" / "Infernux" / "resources" / "icons" / "icon.ico"
    if not icon.is_file():
        raise RuntimeError(f"Windows application icon is missing: {icon}")
    return [
        "--company-name=Infernux",
        f"--product-name={product_name}",
        f"--file-version={file_version}",
        f"--product-version={file_version}",
        f"--file-description={description}",
        f"--windows-icon-from-ico={icon}",
        "--copyright=Copyright (c) Infernux contributors",
        f"--trademarks={product_name}",
        f"--output-filename={original_filename}",
    ]


def _common_nuitka_command(
    output_dir: Path,
    source_root: Path,
    *,
    product_name: str,
    description: str,
    original_filename: str,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--enable-plugin=pyside6",
        "--assume-yes-for-downloads",
        f"--output-dir={output_dir}",
    ]
    if os.name == "nt":
        # This is a hard publishing contract. Toolchain reports and the final PE
        # are verified after compilation, so Nuitka cannot silently fall back.
        command.extend(["--windows-console-mode=disable", "--msvc=latest"])
        command.extend(
            _windows_metadata_options(
                source_root,
                product_name=product_name,
                description=description,
                original_filename=original_filename,
            )
        )
    else:
        command.append(f"--output-filename={original_filename}")
    return command


def _sign_windows_binary(executable: Path, env: Mapping[str, str]) -> bool:
    thumbprint = os.environ.get(_SIGNING_THUMBPRINT_ENV, "").replace(" ", "")
    if not thumbprint:
        print(
            f"Signing skipped for {executable.name}: set {_SIGNING_THUMBPRINT_ENV} "
            "to a publicly trusted code-signing certificate thumbprint."
        )
        return False
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", thumbprint):
        raise RuntimeError(
            f"{_SIGNING_THUMBPRINT_ENV} must contain a 40-character SHA-1 thumbprint"
        )
    signtool = shutil.which("signtool.exe", path=env.get("PATH"))
    if not signtool:
        raise RuntimeError("signtool.exe is required when release signing is enabled")

    timestamp_url = os.environ.get(_SIGNING_TIMESTAMP_ENV, _DEFAULT_TIMESTAMP_URL)
    sign_command = [
        signtool,
        "sign",
        "/sha1",
        thumbprint,
        "/fd",
        "SHA256",
        "/td",
        "SHA256",
        "/tr",
        timestamp_url,
        "/d",
        executable.stem,
        "/du",
        "https://infernux-engine.com/",
        str(executable),
    ]
    if os.environ.get("INFERNUX_SIGN_CERTIFICATE_STORE", "").casefold() == "machine":
        sign_command.insert(2, "/sm")
    _run(sign_command, cwd=executable.parent, env=env)
    _run(
        [signtool, "verify", "/pa", "/all", str(executable)],
        cwd=executable.parent,
        env=env,
    )
    return True


def _validate_msvc_reports(output_dir: Path) -> list[Path]:
    reports = sorted(output_dir.rglob("scons-report.txt"))
    if not reports:
        raise RuntimeError(
            f"Nuitka did not produce a compiler report under {output_dir}"
        )
    required = (
        "msvc_mode=True",
        "mingw_mode=False",
        "gcc_mode=False",
        "clang_mode=False",
        "LINK=link",
        "MSVC_InstallDirectory=",
    )
    for report in reports:
        text = report.read_text(encoding="utf-8", errors="replace")
        missing = [marker for marker in required if marker not in text]
        if missing:
            raise RuntimeError(
                f"Nuitka compiler report is not an MSVC-only build: {report} "
                f"(missing {', '.join(missing)})"
            )
    return reports


def _validate_windows_pe(executable: Path, env: Mapping[str, str]) -> None:
    dumpbin = shutil.which("dumpbin.exe", path=env.get("PATH"))
    if not dumpbin:
        raise RuntimeError(
            "dumpbin.exe is required to validate Windows release binaries"
        )
    headers = subprocess.run(
        [dumpbin, "/headers", str(executable)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
    ).stdout.casefold()
    dependents = subprocess.run(
        [dumpbin, "/dependents", str(executable)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
    ).stdout.casefold()
    if "linker version" not in headers or "machine (x64)" not in headers:
        raise RuntimeError(f"Unexpected Windows PE produced for {executable}")
    forbidden = [
        name for name in _FORBIDDEN_WINDOWS_RUNTIME_IMPORTS if name in dependents
    ]
    if forbidden:
        raise RuntimeError(
            f"MinGW/MSYS runtime imports were found in {executable}: "
            f"{', '.join(forbidden)}"
        )


def _validate_windows_payload(payload: Path, env: Mapping[str, str]) -> None:
    binaries = sorted(
        path
        for path in payload.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".exe", ".dll", ".pyd"}
    )
    if not binaries:
        raise RuntimeError(f"Hub payload contains no Windows binaries: {payload}")
    for binary in binaries:
        lowered_name = binary.name.casefold()
        if any(name in lowered_name for name in _FORBIDDEN_WINDOWS_RUNTIME_IMPORTS):
            raise RuntimeError(
                f"MinGW/MSYS runtime file was bundled into the Hub: {binary}"
            )
        _validate_windows_pe(binary, env)


def _write_toolchain_receipt(
    destination: Path,
    *,
    cmake_generator: str,
    tools: Mapping[str, str],
    reports: list[Path],
) -> None:
    destination.write_text(
        json.dumps(
            {
                "build_driver": "MSBuild",
                "cmake_generator": cmake_generator,
                "compiler": "MSVC",
                "visual_studio": tools["visual_studio"],
                "msbuild": tools["msbuild"],
                "cl": tools["cl"],
                "link": tools["link"],
                "reports": [str(path) for path in reports],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _build_hub(
    source_root: Path,
    build_dir: Path,
    package_dir: Path,
    *,
    cmake_generator: str,
    build_env: Mapping[str, str] | None,
    tools: Mapping[str, str] | None,
) -> None:
    packaging_dir = source_root / "packaging"
    runtime_bundle = package_dir / "runtime" / "runtime_bundle.zip"
    notification_file = packaging_dir / "resources" / "hub_notifications.json"
    if not runtime_bundle.is_file():
        raise RuntimeError(
            "The private Python runtime bundle is missing. Build the "
            "prepare_bundled_python_runtime target before packaging Infernux Hub."
        )
    _validate_runtime_bundle(runtime_bundle)
    if not notification_file.is_file():
        raise RuntimeError(
            "The version-scoped Hub notification resource is missing: "
            f"{notification_file}"
        )
    output_dir = build_dir / "nuitka"
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    command = _common_nuitka_command(
        output_dir,
        source_root,
        product_name="Infernux Hub",
        description="Infernux game engine management platform",
        original_filename="Infernux Hub.exe" if os.name == "nt" else "Infernux Hub",
    ) + [
        "--standalone",
        (
            f"--include-data-file={packaging_dir / 'resources' / 'icon.png'}="
            "resources/icon.png"
        ),
        (
            f"--include-data-file={runtime_bundle}="
            "InfernuxHubData/runtime/runtime_bundle.zip"
        ),
        (
            f"--include-data-file={notification_file}="
            "InfernuxHubData/hub_notifications.json"
        ),
        "--nofollow-import-to=Infernux,numpy,scipy,pandas,matplotlib,cv2,PIL,tkinter",
    ]
    if sys.platform == "darwin":
        command.append("--macos-create-app-bundle")
    command.append(str(packaging_dir / "launcher.py"))
    _run(command, cwd=packaging_dir, env=build_env)
    reports = _validate_msvc_reports(output_dir) if os.name == "nt" else []

    candidates = [output_dir / "launcher.dist", output_dir / "launcher.app"]
    produced = next((path for path in candidates if path.exists()), None)
    if produced is None:
        raise RuntimeError(
            f"Nuitka did not produce a standalone Hub under {output_dir}"
        )

    destination = package_dir / "hub"
    shutil.rmtree(destination, ignore_errors=True)
    if produced.is_dir():
        shutil.copytree(produced, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(produced, destination)

    version_file = destination / "hub-version.json"
    version_file.write_text(
        '{\n  "version": "' + _project_version(source_root) + '"\n}\n',
        encoding="utf-8",
    )
    if os.name == "nt":
        assert build_env is not None and tools is not None
        _sign_windows_binary(destination / "Infernux Hub.exe", build_env)
        _validate_windows_payload(destination, build_env)
        _write_toolchain_receipt(
            build_dir / "hub-build-toolchain.json",
            cmake_generator=cmake_generator,
            tools=tools,
            reports=reports,
        )


def _build_installer(
    source_root: Path,
    build_dir: Path,
    package_dir: Path,
    *,
    build_env: Mapping[str, str] | None,
) -> None:
    packaging_dir = source_root / "packaging"
    hub_payload = package_dir / "hub"
    if not hub_payload.is_dir():
        raise RuntimeError(f"Hub payload does not exist: {hub_payload}")

    output_dir = build_dir / "nuitka"
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload_archive = create_payload_archive(
        hub_payload, build_dir / HUB_PAYLOAD_ARCHIVE
    )
    command = _common_nuitka_command(
        output_dir,
        source_root,
        product_name="Infernux Hub Installer",
        description="Installer for the Infernux game engine management platform",
        original_filename=(
            "InfernuxHubInstaller.exe"
            if os.name == "nt"
            else "InfernuxHubInstaller"
        ),
    ) + [
        "--onefile",
        (
            f"--include-data-file={packaging_dir / 'resources' / 'icon.png'}="
            "resources/icon.png"
        ),
        f"--include-data-file={payload_archive}=payload/{HUB_PAYLOAD_ARCHIVE}",
    ]
    if os.name == "nt":
        # Avoid adding a second opaque compression layer around the already zipped
        # payload. The larger but lower-entropy executable is easier for endpoint
        # security products to inspect and is less packer-like.
        command.extend(["--windows-uac-admin", "--onefile-no-compression"])
    command.append(str(packaging_dir / "installer_gui.py"))
    _run(command, cwd=packaging_dir, env=build_env)
    if os.name == "nt":
        _validate_msvc_reports(output_dir)

    filename = "InfernuxHubInstaller.exe" if os.name == "nt" else "InfernuxHubInstaller"
    produced = output_dir / filename
    if not produced.is_file():
        raise RuntimeError(f"Nuitka did not produce the Hub installer at {produced}")
    destination_dir = package_dir / "installer"
    shutil.rmtree(destination_dir, ignore_errors=True)
    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(produced, destination_dir / filename)
    if os.name == "nt":
        assert build_env is not None
        _sign_windows_binary(destination_dir / filename, build_env)
        _validate_windows_pe(destination_dir / filename, build_env)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("hub", "installer"), required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--cmake-generator", default="")
    args = parser.parse_args()

    source_root = Path(args.source_root).resolve()
    build_dir = Path(args.build_dir).resolve()
    package_dir = Path(args.package_dir).resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)

    _require_msbuild_generator(args.cmake_generator)
    build_env: Mapping[str, str] | None = None
    tools: Mapping[str, str] | None = None
    if os.name == "nt":
        build_env, tools = _msvc_build_environment()

    if args.target == "hub":
        _build_hub(
            source_root,
            build_dir,
            package_dir,
            cmake_generator=args.cmake_generator,
            build_env=build_env,
            tools=tools,
        )
    else:
        _build_installer(
            source_root,
            build_dir,
            package_dir,
            build_env=build_env,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
