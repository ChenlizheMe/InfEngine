"""Project-independent platform support prerequisites owned by Infernux Hub."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path, PurePosixPath
from typing import Mapping


ANDROID_PLUGIN_REFERENCE = "infernux/platform-android"
ANDROID_SUPPORT_SCHEMA = "infernux.android_support"
ANDROID_SUPPORT_KIND = "infernux-android-support"
ANDROID_SUPPORT_VERSION = "0.1.0"
ANDROID_SUPPORT_MANIFEST = "infernux-android-support.json"
ANDROID_SUPPORT_REQUIRED_MESSAGE = (
    "Install Android compatibility from Infernux Hub before importing the "
    "Android platform plugin."
)


def _host_id() -> str:
    machine = platform.machine().casefold()
    if machine not in {"amd64", "x86_64"}:
        return ""
    if os.name == "nt":
        return "windows-x64"
    if os.name == "posix" and platform.system().casefold() == "linux":
        return "linux-x64"
    return ""


def android_support_root(
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    values = os.environ if environ is None else environ
    explicit = str(values.get("INFERNUX_ANDROID_SUPPORT_ROOT", "") or "").strip()
    if explicit:
        return Path(os.path.expandvars(os.path.expanduser(explicit))).resolve()
    data_root = str(values.get("INFERNUX_DATA_ROOT", "") or "").strip()
    if data_root:
        base = Path(os.path.expandvars(os.path.expanduser(data_root))).resolve()
    elif os.name == "nt":
        local_app_data = str(values.get("LOCALAPPDATA", "") or "").strip()
        if not local_app_data:
            return None
        base = Path(local_app_data).resolve() / "InfernuxHub"
    else:
        xdg_data = str(values.get("XDG_DATA_HOME", "") or "").strip()
        base = (
            Path(xdg_data).expanduser().resolve() / "InfernuxHub"
            if xdg_data
            else Path.home() / ".local/share/InfernuxHub"
        )
    host = _host_id()
    if not host:
        return None
    return base / "PlatformKits/android" / ANDROID_SUPPORT_VERSION / host


def _relative(value: object) -> Path:
    text = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ValueError("Android compatibility manifest contains an unsafe path")
    return Path(*path.parts)


def android_support_available(
    environ: Mapping[str, str] | None = None,
) -> bool:
    root = android_support_root(environ)
    if root is None:
        return False
    try:
        document = json.loads(
            (root / ANDROID_SUPPORT_MANIFEST).read_text(encoding="utf-8")
        )
        paths = document["paths"]
        python_paths = paths["python"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return False
    if (
        type(document) is not dict
        or document.get("$schema") != ANDROID_SUPPORT_SCHEMA
        or document.get("kind") != ANDROID_SUPPORT_KIND
        or document.get("version") != ANDROID_SUPPORT_VERSION
        or document.get("host") != _host_id()
        or type(paths) is not dict
        or set(paths) != {"sdk", "jdk", "gradle", "python"}
        or type(python_paths) is not dict
        or set(python_paths) != {"arm64-v8a", "x86_64"}
    ):
        return False
    try:
        sdk = root / _relative(paths["sdk"])
        jdk = root / _relative(paths["jdk"])
        gradle = root / _relative(paths["gradle"])
        arm64 = root / _relative(python_paths["arm64-v8a"])
        x64 = root / _relative(python_paths["x86_64"])
    except ValueError:
        return False
    executable = ".exe" if os.name == "nt" else ""
    gradle_name = "gradle.bat" if os.name == "nt" else "gradle"
    return all(
        (
            (sdk / "platforms/android-36").is_dir(),
            (sdk / "build-tools/36.0.0").is_dir(),
            (
                sdk
                / "ndk/29.0.14206865/build/cmake/android.toolchain.cmake"
            ).is_file(),
            (sdk / "platform-tools" / f"adb{executable}").is_file(),
            (jdk / "bin" / f"java{executable}").is_file(),
            (gradle / "bin" / gradle_name).is_file(),
            (arm64 / "infernux-android-python.json").is_file(),
            (x64 / "infernux-android-python.json").is_file(),
        )
    )


def android_support_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if not android_support_available(environ):
        return {}
    root = android_support_root(environ)
    assert root is not None
    document = json.loads(
        (root / ANDROID_SUPPORT_MANIFEST).read_text(encoding="utf-8")
    )
    paths = document["paths"]
    python_paths = paths["python"]
    sdk = root / _relative(paths["sdk"])
    return {
        "INFERNUX_ANDROID_SUPPORT_ROOT": str(root),
        "ANDROID_SDK_ROOT": str(sdk),
        "ANDROID_HOME": str(sdk),
        "JAVA_HOME": str(root / _relative(paths["jdk"])),
        "INFERNUX_GRADLE_HOME": str(root / _relative(paths["gradle"])),
        "INFERNUX_ANDROID_PYTHON_PREFIX_ARM64": str(
            root / _relative(python_paths["arm64-v8a"])
        ),
        "INFERNUX_ANDROID_PYTHON_PREFIX_X86_64": str(
            root / _relative(python_paths["x86_64"])
        ),
    }


def activate_android_support_environment() -> bool:
    environment = android_support_environment()
    if not environment:
        return False
    os.environ.update(environment)
    return True


def plugin_install_block_reason(
    reference: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    if str(reference or "").strip().casefold() != ANDROID_PLUGIN_REFERENCE:
        return ""
    return "" if android_support_available(environ) else ANDROID_SUPPORT_REQUIRED_MESSAGE


def require_plugin_support(
    reference: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    reason = plugin_install_block_reason(reference, environ)
    if reason:
        raise RuntimeError(reason)


__all__ = [
    "ANDROID_PLUGIN_REFERENCE",
    "ANDROID_SUPPORT_REQUIRED_MESSAGE",
    "android_support_available",
    "android_support_environment",
    "android_support_root",
    "activate_android_support_environment",
    "plugin_install_block_reason",
    "require_plugin_support",
]
