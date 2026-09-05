"""
NuitkaBuilder compiles a Python entry script into a standalone native EXE
using Nuitka (Python → C → native binary). The output is a self-contained
directory containing the EXE, all required native libraries and the embedded
Python runtime.

On Windows, Infernux requires an MSVC toolchain for game builds.
Intermediate compilation is kept in the owning project's build cache and
moved to the final destination afterwards.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import py_compile
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from Infernux.debug import Debug
from Infernux.engine.build_cancellation import BuildCancelled
from Infernux.engine.i18n import t
from Infernux.engine.path_utils import path_key, relative_path, resolved_path
from Infernux.engine.player_package_native import extract_pack, read_manifest, write_pack
from Infernux.engine.player_service_graph import forbidden_player_service_modules
from Infernux.engine.python_abi import (
    BOOTSTRAP_NATIVE_MANIFEST_FILENAME,
    BOOTSTRAP_NATIVE_MANIFEST_SCHEMA,
    LINUX_PYTHON_SHARED_PREFIX,
    PYTHON_RUNTIME_DIRECTORY,
    PYTHON_VERSION,
    WINDOWS_LIBFFI_DLL_PATTERNS,
    WINDOWS_PYTHON_DLL,
    player_native_library_filenames,
    stdlib_extension_module_sources,
)

_MAX_RUNTIME_PACKS = 4
_RUNTIME_HASH_STATE_FILENAME = "content-hashes.json"
_RUNTIME_HASH_CACHE_MIN_BYTES = 1024 * 1024
_RUNTIME_ARCHIVE_FILENAME = "Runtime.inxrt"
_RUNTIME_PACK_MANIFEST_FILENAME = "Player.inxmanifest"
_PACKAGED_RUNTIME_DIRNAME = "_runtime_packs"
_RUNTIME_MODULE_ARCHIVE_FILENAME = "Parallel.inxmod"
_RUNTIME_MODULE_MANIFEST_FILENAME = "Player.inxmanifest"
_PACKAGED_RUNTIME_MODULE_DIRNAME = "_runtime_modules"
_RUNTIME_PACK_FORBIDDEN_SUFFIXES = frozenset(
    {".bak", ".exp", ".lib", ".meta", ".pdb", ".py", ".pyi", ".pyo"}
)
_RUNTIME_MODULE_FORBIDDEN_SUFFIXES = _RUNTIME_PACK_FORBIDDEN_SUFFIXES | frozenset(
    {".c", ".cc", ".cpp", ".h", ".hpp", ".pyx"}
)


def _runtime_payload_matches(left: Path, right: Path) -> bool:
    """Return whether two published runtime directories contain one payload."""

    try:
        left_manifest = json.loads(
            (left / _RUNTIME_MODULE_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        right_manifest = json.loads(
            (right / _RUNTIME_MODULE_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return bool(left_manifest.get("archive_sha256")) and left_manifest.get(
        "archive_sha256"
    ) == right_manifest.get("archive_sha256")


def _publish_runtime_directory(temporary: Path, destination: Path) -> None:
    """Publish a runtime payload despite Windows scanner and build races."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for attempt in range(40):
        if destination.is_dir() and _runtime_payload_matches(destination, temporary):
            return
        try:
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            return
        except OSError as exc:
            last_error = exc
            # Antivirus and a concurrent CMake invocation can briefly retain a
            # handle after generation. A competing publisher may also finish
            # while this process waits, so revalidate on every iteration.
            if attempt + 1 < 40:
                time.sleep(0.05)
    if destination.is_dir() and _runtime_payload_matches(destination, temporary):
        return
    assert last_error is not None
    raise last_error


def _authoring_service_module_names() -> frozenset[str]:
    """Translate the authoritative service graph paths into import names."""

    modules: set[str] = set()
    for path in forbidden_player_service_modules():
        normalized = str(path).replace("\\", "/")
        if not normalized.endswith(".pyc"):
            continue
        modules.add(normalized[:-4].replace("/", "."))
    return frozenset(modules)


def _find_player_module_output(staging_dir: str) -> str:
    """Return the ABI-named extension module emitted by Nuitka module mode."""

    module_prefix = "_InfernuxPlayer"
    extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    candidates = sorted(
        path
        for path in Path(staging_dir).iterdir()
        if path.is_file()
        and path.name.startswith(module_prefix)
        and path.name.endswith(extension_suffixes)
    )
    if len(candidates) != 1:
        found = ", ".join(path.name for path in candidates) or "none"
        raise RuntimeError(
            "Nuitka Player module output is missing or ambiguous; "
            f"expected one ABI extension for {module_prefix}, found: {found}"
        )
    return str(candidates[0])

_AUTO_INSTALLABLE_PACKAGES = {
    "nuitka": "nuitka",
    "ordered_set": "ordered-set",
    "PIL": "Pillow",
    "numba": "numba",
    "llvmlite": "llvmlite",
}


_BuildCancelled = BuildCancelled


def _windows_ascii_build_alias(build_cache_root: str) -> str:
    """Create an ASCII-only junction to one project-owned build cache."""

    if sys.platform != "win32" or build_cache_root.isascii():
        return ""

    os.makedirs(build_cache_root, exist_ok=True)
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    alias_parent = os.path.join(program_data, "Infernux", "BuildLinks")
    os.makedirs(alias_parent, exist_ok=True)
    alias = os.path.join(
        alias_parent,
        f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}",
    )
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", alias, build_cache_root],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not os.path.isdir(alias):
        raise RuntimeError(
            "Windows game builds require an ASCII compiler path, but the "
            f"build-cache junction could not be created: {result.stdout.strip()}"
        )
    return alias


def _remove_windows_build_alias(alias: str) -> None:
    if alias and os.path.lexists(alias):
        os.rmdir(alias)


def _terminate_process_tree(proc: subprocess.Popen, *, timeout: float = 1.0) -> None:
    """Stop the compiler process tree created for one cancelled build."""
    if proc.poll() is not None:
        return

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _has_msvc_toolchain() -> bool:
    if shutil.which("cl"):
        return True

    return bool(_find_msvc_environment_scripts())


def _which_in_env(executable: str, env: dict[str, str]) -> str:
    return shutil.which(executable, path=env.get("PATH", "")) or ""


def _split_env_paths(value: str) -> list[str]:
    return [entry for entry in (value or "").split(os.pathsep) if entry]


def _dedupe_env_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        normalized = path_key(os.path.expandvars(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(path)
    return result


def _set_env_paths(env: dict[str, str], key: str, paths: list[str]) -> None:
    env[key] = os.pathsep.join(_dedupe_env_paths(paths))


def _env_path_has_file(env: dict[str, str], key: str, filename: str) -> bool:
    for directory in _split_env_paths(env.get(key, "")):
        if os.path.isfile(os.path.join(os.path.expandvars(directory.strip().strip('"')), filename)):
            return True
    return False


def _with_trailing_backslash(path: str) -> str:
    return resolved_path(path).rstrip("\\/") + "\\"


def _version_sort_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for item in re.split(r"[^0-9]+", version):
        if not item:
            continue
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _windows_sdk_roots_from_registry() -> list[str]:
    if sys.platform != "win32":
        return []

    try:
        import winreg
    except ImportError:
        return []

    roots: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if not path:
            return
        root = resolved_path(os.path.expandvars(path.strip().strip('"')))
        if not os.path.isdir(root):
            return
        normalized = path_key(root)
        if normalized in seen:
            return
        seen.add(normalized)
        roots.append(root)

    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    views = [0]
    for view_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        view_value = getattr(winreg, view_name, 0)
        if view_value and view_value not in views:
            views.append(view_value)

    keys = (
        r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots",
    )
    value_names = ("KitsRoot11", "KitsRoot10", "KitsRoot")
    for hive in hives:
        for access in views:
            for key_name in keys:
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | access) as key_handle:
                        for value_name in value_names:
                            try:
                                value, _kind = winreg.QueryValueEx(key_handle, value_name)
                            except OSError:
                                continue
                            if isinstance(value, str):
                                _add(value)
                except OSError:
                    continue
    return roots


def _windows_sdk_roots(env: Optional[dict[str, str]] = None) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if not path:
            return
        root = resolved_path(os.path.expandvars(path.strip().strip('"')))
        if not os.path.isdir(root):
            return
        normalized = path_key(root)
        if normalized in seen:
            return
        seen.add(normalized)
        roots.append(root)

    if env:
        _add(env.get("INFERNUX_WINDOWS_SDK_DIR", ""))
        _add(env.get("WindowsSdkDir", ""))
        _add(env.get("UniversalCRTSdkDir", ""))
    _add(os.environ.get("INFERNUX_WINDOWS_SDK_DIR", ""))
    _add(os.environ.get("WindowsSdkDir", ""))
    _add(os.environ.get("UniversalCRTSdkDir", ""))
    for root in _windows_sdk_roots_from_registry():
        _add(root)
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    _add(os.path.join(program_files_x86, "Windows Kits", "10"))
    _add(os.path.join(program_files, "Windows Kits", "10"))
    return roots


def _find_windows_sdk_layout(env: Optional[dict[str, str]] = None) -> dict[str, object]:
    if sys.platform != "win32":
        return {}

    for sdk_root in _windows_sdk_roots(env):
        include_root = os.path.join(sdk_root, "Include")
        lib_root = os.path.join(sdk_root, "Lib")
        bin_root = os.path.join(sdk_root, "bin")
        if not os.path.isdir(include_root):
            continue

        versions = [
            name for name in os.listdir(include_root)
            if os.path.isdir(os.path.join(include_root, name))
        ]
        for version in sorted(versions, key=_version_sort_key, reverse=True):
            include_dirs = [
                os.path.join(include_root, version, component)
                for component in ("ucrt", "shared", "um", "winrt", "cppwinrt")
                if os.path.isdir(os.path.join(include_root, version, component))
            ]
            lib_dirs = [
                os.path.join(lib_root, version, component, "x64")
                for component in ("ucrt", "um")
                if os.path.isdir(os.path.join(lib_root, version, component, "x64"))
            ]
            bin_dirs = [
                os.path.join(bin_root, version, "x64"),
                os.path.join(bin_root, "x64"),
            ]
            tool_dirs = [
                path for path in bin_dirs
                if os.path.isfile(os.path.join(path, "rc.exe"))
                and os.path.isfile(os.path.join(path, "mt.exe"))
            ]
            has_windows_headers = os.path.isfile(os.path.join(include_root, version, "um", "Windows.h"))
            has_um_libs = os.path.isdir(os.path.join(lib_root, version, "um", "x64"))
            has_ucrt_libs = os.path.isdir(os.path.join(lib_root, version, "ucrt", "x64"))
            if tool_dirs and include_dirs and lib_dirs and has_windows_headers and has_um_libs and has_ucrt_libs:
                return {
                    "root": sdk_root,
                    "version": version,
                    "tool_dirs": tool_dirs,
                    "include_dirs": include_dirs,
                    "lib_dirs": lib_dirs,
                }
    return {}


def _augment_windows_sdk_environment(env: dict[str, str]) -> dict[str, str]:
    if sys.platform != "win32":
        return env

    layout = _find_windows_sdk_layout(env)
    if not layout:
        return env

    augmented = dict(env)
    tool_dirs = [str(path) for path in layout.get("tool_dirs", [])]
    include_dirs = [str(path) for path in layout.get("include_dirs", [])]
    lib_dirs = [str(path) for path in layout.get("lib_dirs", [])]

    _set_env_paths(augmented, "PATH", tool_dirs + _split_env_paths(augmented.get("PATH", "")))
    _set_env_paths(augmented, "INCLUDE", include_dirs + _split_env_paths(augmented.get("INCLUDE", "")))
    _set_env_paths(augmented, "LIB", lib_dirs + _split_env_paths(augmented.get("LIB", "")))
    _set_env_paths(augmented, "LIBPATH", lib_dirs + _split_env_paths(augmented.get("LIBPATH", "")))

    sdk_root = str(layout.get("root", ""))
    sdk_version = str(layout.get("version", ""))
    if sdk_root:
        augmented["WindowsSdkDir"] = _with_trailing_backslash(sdk_root)
        augmented["UniversalCRTSdkDir"] = _with_trailing_backslash(sdk_root)
        augmented["MSSDK_DIR"] = _with_trailing_backslash(sdk_root)
    if tool_dirs:
        augmented["WindowsSdkBinPath"] = _with_trailing_backslash(tool_dirs[0])
    if sdk_version:
        sdk_version = sdk_version.rstrip("\\/")
        augmented["WindowsSDKVersion"] = sdk_version
        augmented["UCRTVersion"] = sdk_version
    return augmented


def _msvc_env_missing_parts(env: dict[str, str]) -> list[str]:
    missing: list[str] = []
    if not _which_in_env("cl.exe", env):
        missing.append("cl.exe")
    if not _which_in_env("link.exe", env):
        missing.append("link.exe")
    if not _which_in_env("rc.exe", env):
        missing.append("rc.exe")
    if not _which_in_env("mt.exe", env):
        missing.append("mt.exe")
    if not env.get("INCLUDE"):
        missing.append("INCLUDE")
    elif not _env_path_has_file(env, "INCLUDE", "excpt.h"):
        missing.append("MSVC INCLUDE (excpt.h)")
    if not env.get("LIB"):
        missing.append("LIB")
    elif not _env_path_has_file(env, "LIB", "vcruntime.lib"):
        missing.append("MSVC LIB (vcruntime.lib)")
    return missing


def _msvc_env_ready(env: dict[str, str]) -> bool:
    return not _msvc_env_missing_parts(env)


def _force_msvc_tool_variables(env: dict[str, str]) -> dict[str, str]:
    updated = dict(env)
    # MSVC's linker consumes LINK/_LINK_ environment variables as additional
    # linker options.  Setting LINK to a full path like "C:\Program Files\..."
    # makes link.exe interpret "C:\Program" as an input object file.
    updated.pop("LINK", None)
    updated.pop("_LINK_", None)
    sdk_root = updated.get("WindowsSdkDir") or updated.get("UniversalCRTSdkDir")
    if sdk_root:
        updated["WindowsSdkDir"] = _with_trailing_backslash(sdk_root)
        updated["UniversalCRTSdkDir"] = _with_trailing_backslash(sdk_root)
        updated.setdefault("MSSDK_DIR", _with_trailing_backslash(sdk_root))
    if updated.get("WindowsSDKVersion"):
        updated["WindowsSDKVersion"] = updated["WindowsSDKVersion"].rstrip("\\/")
    if updated.get("UCRTVersion"):
        updated["UCRTVersion"] = updated["UCRTVersion"].rstrip("\\/")

    cl_path = _which_in_env("cl.exe", updated) or "cl.exe"
    updated["CC"] = cl_path
    updated["CXX"] = cl_path

    rc_path = _which_in_env("rc.exe", updated)
    if rc_path:
        updated["RC"] = rc_path
    mt_path = _which_in_env("mt.exe", updated)
    if mt_path:
        updated["MT"] = mt_path
    return updated


def _windows_toolchain_summary(env: dict[str, str]) -> str:
    def _short(value: str) -> str:
        return value or "<missing>"

    return (
        "MSVC toolchain: "
        f"cl={_short(_which_in_env('cl.exe', env))}, "
        f"link={_short(_which_in_env('link.exe', env))}, "
        f"rc={_short(_which_in_env('rc.exe', env))}, "
        f"mt={_short(_which_in_env('mt.exe', env))}, "
        f"excpt.h={_env_path_has_file(env, 'INCLUDE', 'excpt.h')}, "
        f"vcruntime.lib={_env_path_has_file(env, 'LIB', 'vcruntime.lib')}, "
        f"WindowsSdkDir={env.get('WindowsSdkDir', '<missing>')}, "
        f"WindowsSDKVersion={env.get('WindowsSDKVersion', '<missing>')}, "
        f"UCRTVersion={env.get('UCRTVersion', '<missing>')}, "
        f"MSSDK_DIR={env.get('MSSDK_DIR', '<missing>')}"
    )


def _visual_studio_roots_from_vswhere() -> list[str]:
    roots: list[str] = []
    vswhere = os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "Microsoft Visual Studio",
        "Installer",
        "vswhere.exe",
    )
    if not os.path.isfile(vswhere):
        return roots

    try:
        completed = subprocess.run(
            [
                vswhere,
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return roots

    if completed.returncode != 0:
        return roots

    for line in (completed.stdout or "").splitlines():
        root = line.strip()
        if root and os.path.isdir(root):
            roots.append(root)
    return roots


def _visual_studio_roots_from_registry() -> list[str]:
    """Discover Visual Studio installation roots from Windows registry.

    ``vswhere`` is the official and most reliable discovery API, but registry
    fallback matters for non-standard or partially repaired installs where the
    VS Installer utility is missing from its usual location.  The SxS and Setup
    keys are written with the actual installation path, so custom drives are
    covered here.
    """
    if sys.platform != "win32":
        return []

    try:
        import winreg
    except ImportError:
        return []

    roots: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if not path:
            return
        root = resolved_path(os.path.expandvars(path.strip().strip('"')))
        if not os.path.isdir(root):
            return
        normalized = path_key(root)
        if normalized in seen:
            return
        seen.add(normalized)
        roots.append(root)

    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    views = [0]
    for view_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        value = getattr(winreg, view_name, 0)
        if value and value not in views:
            views.append(value)

    # VS7 SxS values are named by major version (e.g. "17.0") and point to
    # the VS installation root, even when the user installs on a custom drive.
    sx_s_values: list[tuple[float, str]] = []
    sx_s_keys = (
        r"SOFTWARE\Microsoft\VisualStudio\SxS\VS7",
        r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\SxS\VS7",
    )
    for hive in hives:
        for access in views:
            for key_name in sx_s_keys:
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | access) as key:
                        index = 0
                        while True:
                            try:
                                name, value, _kind = winreg.EnumValue(key, index)
                            except OSError:
                                break
                            index += 1
                            try:
                                version = float(str(name).split(".", 1)[0])
                            except ValueError:
                                version = 0.0
                            if isinstance(value, str):
                                sx_s_values.append((version, value))
                except OSError:
                    continue

    for _version, path in sorted(sx_s_values, reverse=True):
        _add(path)

    # Newer installers also expose per-instance Setup keys.  These are useful
    # when SxS is absent but the installer registration is intact.
    setup_instance_keys = (
        r"SOFTWARE\Microsoft\VisualStudio\Setup\Instances",
        r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\Setup\Instances",
    )
    for hive in hives:
        for access in views:
            for key_name in setup_instance_keys:
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | access) as key:
                        index = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(key, index)
                            except OSError:
                                break
                            index += 1
                            try:
                                with winreg.OpenKey(key, subkey_name) as instance_key:
                                    value, _kind = winreg.QueryValueEx(instance_key, "InstallationPath")
                            except OSError:
                                continue
                            if isinstance(value, str):
                                _add(value)
                except OSError:
                    continue

    return roots


def _find_msvc_environment_scripts() -> list[tuple[str, list[str]]]:
    """Return candidate VS environment scripts for x64 MSVC builds."""
    roots: list[str] = []
    explicit_script = os.environ.get("INFERNUX_VCVARSALL", "")
    if explicit_script and os.path.isfile(explicit_script):
        script_name = os.path.basename(explicit_script).lower()
        if script_name == "vsdevcmd.bat":
            return [(explicit_script, ["-arch=x64", "-host_arch=x64"])]
        if script_name == "vcvars64.bat":
            return [(explicit_script, [])]
        return [(explicit_script, ["x64"])]

    explicit_vs_root = os.environ.get("INFERNUX_VSINSTALLDIR", "")
    if explicit_vs_root and os.path.isdir(explicit_vs_root):
        roots.append(explicit_vs_root)

    roots.extend(_visual_studio_roots_from_vswhere())
    roots.extend(_visual_studio_roots_from_registry())

    for env_name in ("VSINSTALLDIR", "VCINSTALLDIR"):
        root = os.environ.get(env_name, "")
        if env_name == "VCINSTALLDIR" and root:
            root = resolved_path(os.path.join(root, "..", ".."))
        if root and os.path.isdir(root):
            roots.append(root)

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for year in ("2022", "2019"):
        for edition in ("BuildTools", "Community", "Professional", "Enterprise"):
            roots.append(os.path.join(program_files, "Microsoft Visual Studio", year, edition))

    candidates: list[tuple[str, list[str]]] = []
    seen_roots: set[str] = set()
    for root in roots:
        normalized_root = path_key(root)
        if normalized_root in seen_roots:
            continue
        seen_roots.add(normalized_root)

        for script, args in (
            (os.path.join(root, "Common7", "Tools", "VsDevCmd.bat"), ["-arch=x64", "-host_arch=x64"]),
            (os.path.join(root, "VC", "Auxiliary", "Build", "vcvars64.bat"), []),
            (os.path.join(root, "VC", "Auxiliary", "Build", "vcvarsall.bat"), ["x64"]),
        ):
            if os.path.isfile(script):
                candidates.append((script, args))
    return candidates


def _capture_msvc_environment(
    script_path: str,
    args: list[str],
    base_env: dict[str, str],
) -> dict[str, str]:
    env = dict(base_env)
    env["VSCMD_SKIP_SENDTELEMETRY"] = "1"
    quoted_args = " ".join(args)
    temp_root = env.get("TEMP") if os.path.isdir(env.get("TEMP", "")) else tempfile.gettempdir()
    batch_dir = tempfile.mkdtemp(prefix="inx_vcvars_", dir=temp_root)
    batch_path = os.path.join(batch_dir, "capture_env.bat")
    try:
        with open(batch_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("@echo off\n")
            f.write(f'call "{script_path}" {quoted_args} >nul\n')
            f.write("if errorlevel 1 exit /b %errorlevel%\n")
            f.write("set\n")

        completed = subprocess.run(
            ["cmd", "/d", "/c", batch_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=90,
            creationflags=0x08000000,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "").strip())
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

    captured: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            captured[key] = value
    return captured


def _ensure_windows_msvc_environment(env: dict[str, str]) -> dict[str, str]:
    """Merge a Visual Studio Developer Command Prompt environment.

    End users normally launch Infernux from the Hub or Explorer, not from a
    Developer Command Prompt.  Nuitka's SCons backend needs MSVC/Windows SDK
    variables such as PATH, INCLUDE, LIB, and the cl.exe location; otherwise it
    can fail with the misleading internal message "scons environment variable
    CC is not set" even when Visual Studio is installed.
    """
    if sys.platform != "win32":
        return env

    env = _augment_windows_sdk_environment(dict(env))
    if _msvc_env_ready(env):
        env = _force_msvc_tool_variables(env)
        Debug.log_internal(_windows_toolchain_summary(env))
        return env

    failures: list[str] = []
    for script_path, args in _find_msvc_environment_scripts():
        try:
            captured = _capture_msvc_environment(script_path, args, env)
        except Exception as exc:
            failures.append(f"{script_path}: {exc}")
            continue

        merged = dict(env)
        merged.update(captured)
        merged = _augment_windows_sdk_environment(merged)
        if _msvc_env_ready(merged):
            merged = _force_msvc_tool_variables(merged)
            Debug.log_internal(f"Loaded MSVC build environment from {script_path}")
            Debug.log_internal(_windows_toolchain_summary(merged))
            return merged

        missing = ", ".join(_msvc_env_missing_parts(merged)) or "unknown"
        failures.append(f"{script_path}: missing {missing} after initialization")

    details = "\n".join(failures[-3:])
    raise RuntimeError(
        "Windows game builds require an initialized MSVC + Windows SDK build environment.\n"
        "Visual Studio was detected, but Infernux could not initialize the C++ toolchain "
        "for Nuitka/SCons.\n"
        "Install or repair Visual Studio 2022 with 'Desktop development with C++', "
        "including MSVC v143 and a Windows 10/11 SDK, then try again. If the SDK is already installed, "
        "make sure WindowsSdkDir points at the Windows Kits root or repair the VS workload so rc.exe/mt.exe are registered.\n"
        f"Details:\n{details}"
    )


def _run_python(python_exe: str, args: List[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    from Infernux.runtime_utf8 import merge_child_env

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "env": merge_child_env({"PYTHONDONTWRITEBYTECODE": "1"}),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000
    return subprocess.run([python_exe, *args], **kwargs)


def _python_version(python_exe: str) -> str:
    try:
        completed = _run_python(
            python_exe,
            ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _is_embeddable_python_exe(python_exe: str) -> bool:
    try:
        root = os.path.dirname(resolved_path(python_exe))
        return any(name.lower().endswith("._pth") for name in os.listdir(root))
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return False


def _is_valid_builder_python(python_exe: str) -> bool:
    return bool(
        python_exe
        and os.path.isfile(python_exe)
        and _python_version(python_exe) == PYTHON_VERSION
        and not _is_embeddable_python_exe(python_exe)
    )


def _dedupe_paths(paths: List[str]) -> List[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        normalized = path_key(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(path)
    return deduped


def _resolve_builder_python() -> str:
    if _is_valid_builder_python(sys.executable):
        return sys.executable

    raise RuntimeError(
        f"Nuitka builds must run from a non-embeddable Python {PYTHON_VERSION} "
        "environment.\n"
        "In the packaged Hub workflow, each project owns a full Python copy "
        f"under .runtime/{PYTHON_RUNTIME_DIRECTORY}/ — open the project through "
        "its runtime and build from there."
    )


def _ensure_python_packages(python_exe: str, *module_names: str) -> None:
    import time as _time
    missing_packages: list[str] = []
    _check_t0 = _time.perf_counter()

    # Check all modules in a single subprocess instead of one per module.
    check_script = (
        "import importlib.util, sys; "
        "mods = sys.argv[1:]; "
        "print(','.join(str(int(importlib.util.find_spec(m) is not None)) for m in mods))"
    )
    completed = _run_python(
        python_exe,
        ["-c", check_script, *module_names],
        timeout=30,
    )
    if completed.returncode == 0 and (completed.stdout or "").strip():
        results = (completed.stdout or "").strip().split(",")
        for module_name, available in zip(module_names, results):
            if available.strip() != "1":
                package_name = _AUTO_INSTALLABLE_PACKAGES.get(module_name)
                if package_name and package_name not in missing_packages:
                    missing_packages.append(package_name)
    else:
        # Fallback: treat all as potentially missing
        for module_name in module_names:
            package_name = _AUTO_INSTALLABLE_PACKAGES.get(module_name)
            if package_name and package_name not in missing_packages:
                missing_packages.append(package_name)

    Debug.log_internal(
        f"  package availability check for {len(module_names)} modules in "
        f"{_time.perf_counter() - _check_t0:.2f}s"
    )

    if not missing_packages:
        return

    Debug.log_internal(
        "Missing build packages detected — installing automatically: "
        + ", ".join(missing_packages)
    )
    _pip_t0 = _time.perf_counter()
    subprocess.check_call(
        [python_exe, "-m", "pip", "install", *missing_packages, "--quiet"],
    )
    Debug.log_internal(
        f"  pip install completed in {_time.perf_counter() - _pip_t0:.2f}s"
    )


def _install_requirements_files(
    python_exe: str,
    requirement_files: List[str],
    state_dir: str,
) -> None:
    os.makedirs(state_dir, exist_ok=True)
    interpreter_key = hashlib.sha256(path_key(python_exe).encode("utf-8")).hexdigest()[:24]
    state_path = os.path.join(state_dir, f"{interpreter_key}.json")
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}

    changed = False
    for requirement_file in requirement_files:
        if not requirement_file or not os.path.isfile(requirement_file):
            continue
        with open(requirement_file, "rb") as source:
            requirement_hash = hashlib.sha256(source.read()).hexdigest()
        state_key = path_key(requirement_file)
        if state.get(state_key) == requirement_hash:
            Debug.log_internal(f"Project requirements unchanged; reusing builder environment: {requirement_file}")
            continue
        Debug.log_internal(
            f"Installing project requirements into builder Python: {requirement_file}"
        )
        subprocess.check_call(
            [
                python_exe,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                requirement_file,
                "--quiet",
            ],
        )
        state[state_key] = requirement_hash
        changed = True

    if changed:
        temporary = state_path + f".{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
        os.replace(temporary, state_path)


class NuitkaBuilder:
    """Wraps Nuitka compilation for Infernux standalone builds."""

    _RUNTIME_PACK_LAYOUT = "engine-native-short-name"

    # Packages that are excluded from Nuitka compilation and injected as raw
    # site-packages. NumPy and packaging are engine runtime dependencies;
    # Numba/llvmlite additionally require Python bytecode for LLVM JIT.
    _JIT_NOFOLLOW_PACKAGES = frozenset({"numba", "llvmlite", "numpy", "packaging"})
    # Build/publish helpers run in the authoring process and are not part of
    # the generated Player import graph. Changing them must not invalidate
    # the expensive Nuitka runtime cache.
    _PLAYER_POST_BUILD_ONLY_FILES = frozenset(
        {
            "engine/_build_dependencies.py",
            "engine/_build_splash.py",
            "engine/build_cancellation.py",
            "engine/game_builder.py",
            "engine/player_package_audit.py",
            "engine/prebuilt_runtime.py",
        }
    )
    # Source-less Runtime.inxrt must not expose the compiler that produced it.
    # Keep this separate from _PLAYER_POST_BUILD_ONLY_FILES so changes to the
    # compiler policy continue to invalidate the prebuilt runtime cache.
    _PLAYER_RUNTIME_EXCLUDED_FILES = frozenset({"engine/nuitka_builder.py"})
    _GAME_BUILD_EXCLUDED_PACKAGES = frozenset()
    _ENGINE_MANAGED_RUNTIME_PACKAGES = frozenset(
        {"infernux", "numba", "llvmlite", "numpy", "packaging"}
    )
    _PLAYER_NATIVE_CONTRACT_FILENAME = "PlayerNativeContract.json"
    _PLAYER_NATIVE_CONTRACT = {
        "contract": "infernux.player-native",
        "runtime_linkage": "static",
    }
    _GAME_BUILD_NOFOLLOW_MODULES = frozenset()
    # Keep the Player import graph explicit. PlayerGUI legitimately uses the
    # small viewport utility module, but none of the authoring panels,
    # previews, dialogs, or project/file-management services belong in a
    # shipped game. These guards also protect against a future transitive
    # import accidentally pulling the editor back into Nuitka's graph.
    _PLAYER_EDITOR_ONLY_MODULES = frozenset({
        "Infernux.engine.bootstrap",
        "Infernux.engine.bootstrap_project",
        "Infernux.engine.bootstrap_inspector",
        "Infernux.engine.bootstrap_wiring",
        "Infernux.engine._bootstrap_panels",
        "Infernux.engine._bootstrap_selection",
        "Infernux.engine._bootstrap_trace",
        "Infernux.engine._bootstrap_wiring",
        "Infernux.engine.candidate_import",
        "Infernux.engine.deferred_task",
        "Infernux.engine.hierarchy_creation_service",
        "Infernux.engine.i18n",
        "Infernux.engine.ide_preference",
        "Infernux.engine.library_sync",
        "Infernux.engine.play_mode",
        "Infernux.engine.preferences_store",
        "Infernux.engine.project_requirements",
        "Infernux.engine.project_view_settings",
        "Infernux.engine._play_mode_serialization",
        "Infernux.engine.resources_manager",
        "Infernux.engine.scene_document_transaction",
        "Infernux.engine.scene_manager",
        "Infernux.engine.ui.editor_panel",
        "Infernux.engine.ui.editor_services",
        "Infernux.engine.ui.panel_registry",
        "Infernux.engine.ui.window_manager",
        "Infernux.engine.ui.project_file_ops",
        "Infernux.engine.ui.asset_resource_preview",
        "Infernux.engine.ui.asset_details_renderer",
        "Infernux.engine.ui.asset_save_dialog",
        "Infernux.engine.ui.inspector_components",
        "Infernux.engine.ui.inspector_material",
        "Infernux.engine.ui.inspector_renderstack",
        "Infernux.engine.ui.render_effect_inspector",
        "Infernux.engine.ui.particle_graph_editor_panel",
        "Infernux.engine.ui.node_graph_editor_panel",
        "Infernux.engine.ui.animclip2d_editor_panel",
        "Infernux.engine.ui.animfsm_editor_panel",
        "Infernux.engine.ui.animtimeline_editor_panel",
        "Infernux.engine.ui.ui_editor_panel",
        "Infernux.engine.ui.scene_view_panel",
        "Infernux.engine.ui.game_view_panel",
        "Infernux.engine.ui.console_panel",
        "Infernux.engine.ui.hierarchy_panel",
        "Infernux.engine.ui.preferences_panel",
        "Infernux.engine.ui.build_settings_panel",
        "Infernux.engine.ui.environment_settings_panel",
        "Infernux.engine.ui.tag_layer_settings",
        "Infernux.engine.ui.external_document_conflict",
        "Infernux.engine.ui.unsaved_changes_dialog",
        "Infernux.engine.ui.dirty_panel_confirmation",
    }) | _authoring_service_module_names()
    _PLAYER_EDITOR_ONLY_PACKAGE_PREFIXES = frozenset({
        "Infernux.engine._bootstrap",
        "Infernux.engine.bootstrap_hierarchy",
        "Infernux.engine.bootstrap_inspector",
        "Infernux.engine.interaction",
        "Infernux.engine.undo",
        "Infernux.gizmos",
    })
    _PLAYER_RUNTIME_UI_MODULES = frozenset({
        "Infernux.engine.ui",
        "Infernux.engine.ui.engine_status",
        "Infernux.engine.ui.runtime_canvas_snapshot",
        "Infernux.engine.ui.theme",
        "Infernux.engine.ui.viewport_utils",
    })

    # Directories stripped from raw-copied JIT packages to slim down
    # the build output.  These are never needed at runtime.
    _JIT_STRIP_DIRS: dict[str, list[str]] = {
        "numba": [
            "tests", "cuda", "testing", "pycc", "scripts",
            # CUDA is ~4 MB and only needed for GPU compute
            # tests/testing are ~10 MB of test fixtures
        ],
        "numpy": [
            "tests", "f2py", "testing", "doc",
            "_pyinstaller", "distutils",
            # f2py is 1.6 MB Fortran build tooling
        ],
        "llvmlite": [
            "tests",
        ],
    }

    def __init__(
        self,
        entry_script: str,
        output_dir: str,
        *,
        build_cache_root: str,
        output_filename: str = "Game.exe",
        product_name: str = "Infernux Game",
        file_version: str = "1.0.0.0",
        icon_path: Optional[str] = None,
        extra_include_packages: Optional[List[str]] = None,
        extra_include_data: Optional[List[str]] = None,
        extra_requirements_files: Optional[List[str]] = None,
        raw_copy_packages: Optional[List[str]] = None,
        runtime_support_packages: Optional[List[str]] = None,
        console_mode: str = "disable",
        lto: bool = True,
        runtime_pack_cache: bool = False,
        packaged_runtime_lookup: bool = True,
        player_module: bool = False,
    ):
        self.entry_script = resolved_path(entry_script)
        self.output_dir = resolved_path(output_dir)
        configured_cache_root = os.environ.get("INFERNUX_NUITKA_ROOT", "").strip()
        self._build_cache_root = resolved_path(
            os.path.expanduser(configured_cache_root)
            if configured_cache_root
            else build_cache_root
        )
        self._staging_root = os.path.join(self._build_cache_root, "Staging")
        self._nuitka_cache_dir = os.path.join(self._build_cache_root, "Nuitka")
        self._runtime_pack_dir = os.path.join(self._build_cache_root, "RuntimePacks")
        self._requirements_state_dir = os.path.join(
            self._build_cache_root,
            "Requirements",
        )
        # Platform-normalized executable name: the Windows-style ".exe"
        # default is stripped on Linux/macOS so callers don't need to care.
        if sys.platform != "win32" and output_filename.lower().endswith(".exe"):
            output_filename = output_filename[:-4]
        self.output_filename = output_filename
        self.product_name = product_name
        self.file_version = file_version
        self.icon_path = icon_path
        self.console_mode = console_mode
        self.lto = lto
        self.runtime_pack_cache = bool(runtime_pack_cache)
        self.packaged_runtime_lookup = bool(packaged_runtime_lookup)
        self.player_module = bool(player_module)
        self.last_runtime_pack_key = ""
        self.last_runtime_compatibility_key = ""
        self._engine_fingerprint_cache = ""
        self.extra_include_packages = [
            pkg for pkg in list(extra_include_packages or [])
            if not self._is_game_build_excluded_package(pkg)
        ]
        self.extra_include_data = list(extra_include_data or [])
        self.extra_requirements_files = [
            resolved_path(path)
            for path in list(extra_requirements_files or [])
            if path
        ]
        self.raw_copy_packages = sorted(set(raw_copy_packages or []))
        self.runtime_support_packages = sorted(set(runtime_support_packages or []))

        # Staging directory — unique per build to allow parallel builds
        tag = hashlib.md5(self.output_dir.encode()).hexdigest()[:8]
        self._staging_dir = os.path.join(self._staging_root, tag)
        self._builder_python = _resolve_builder_python()

    @classmethod
    def _is_game_build_excluded_package(cls, package_name: str) -> bool:
        root = (package_name or "").split(".", 1)[0].lower().replace("_", "-")
        return root in cls._GAME_BUILD_EXCLUDED_PACKAGES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        on_progress: Optional[Callable[[str, float], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        *,
        force_runtime_rebuild: bool = False,
    ) -> str:
        """Run Nuitka compilation.  Returns the dist directory path."""
        import time as _time
        _build_t0 = _time.perf_counter()
        _stage_t0 = _build_t0

        def _p(msg: str, pct: float):
            nonlocal _stage_t0
            if cancel_event is not None and cancel_event.is_set():
                raise _BuildCancelled()
            now = _time.perf_counter()
            elapsed = now - _stage_t0
            _stage_t0 = now
            if on_progress:
                on_progress(msg, pct)
            Debug.log_internal(
                f"[NuitkaBuilder {pct:.0%}] {msg}  (prev {elapsed:.2f}s, "
                f"nuitka total {now - _build_t0:.1f}s)"
            )

        _p(t("build.step.preparing_staging"), 0.03)
        self._prepare_staging()

        _p(t("build.step.building_command"), 0.05)
        cmd = self._build_command()
        _p(f"cmd: {' '.join(cmd)}", 0.05)

        runtime_pack_key = self._runtime_pack_fingerprint(cmd) if self.runtime_pack_cache else ""
        compatibility_key = self._runtime_pack_compatibility_key() if runtime_pack_key else ""
        self.last_runtime_pack_key = runtime_pack_key
        self.last_runtime_compatibility_key = compatibility_key
        dist_dir = None
        if runtime_pack_key and not force_runtime_rebuild:
            dist_dir = self._restore_runtime_pack(
                runtime_pack_key,
                compatibility_key=compatibility_key if self.packaged_runtime_lookup else "",
            )
        if dist_dir is None:
            self._check_nuitka()
            _p(t("build.step.running_nuitka"), 0.10)
            dist_dir = self._run_nuitka(cmd, on_progress, cancel_event)

            _p(t("build.step.injecting_libs"), 0.85)
            self._inject_native_libs(dist_dir)
            if self.player_module:
                self._inject_engine_python_runtime(dist_dir)
                self._inject_python_runtime_stdlib(dist_dir)
                self._inject_python_bootstrap_runtime(dist_dir)

            if self.raw_copy_packages:
                _p(t("build.step.injecting_jit"), 0.87)
                self._inject_jit_packages(dist_dir)

            if sys.platform == "win32" and not self.player_module:
                _p(t("build.step.embedding_manifest"), 0.90)
                self._embed_utf8_manifest(dist_dir)

                _p(t("build.step.signing_exe"), 0.92)
                self._sign_executable(dist_dir)

            if runtime_pack_key:
                _p("Caching reusable Infernux Runtime Pack", 0.94)
                self._store_runtime_pack(
                    runtime_pack_key,
                    dist_dir,
                    compatibility_key=compatibility_key,
                    overwrite=force_runtime_rebuild,
                )
        else:
            _p("Reused prebuilt Infernux Runtime Pack", 0.94)

        _p(t("build.step.cleaning_artifacts"), 0.95)
        self._cleanup_build_artifacts()

        _p(t("build.step.nuitka_complete"), 1.0)
        return dist_dir

    def _runtime_pack_fingerprint(self, cmd: List[str]) -> str:
        """Fingerprint every input that can change a reusable Player runtime."""
        digest = hashlib.sha256()
        digest.update(b"runtime-pack\0")
        digest.update(self._RUNTIME_PACK_LAYOUT.encode("ascii"))
        normalized_command = []
        for index, argument in enumerate(cmd):
            value = str(argument)
            if index == 0:
                value = "<PYTHON>"
            value = value.replace(self._staging_dir, "<STAGING>")
            value = value.replace(getattr(self, "_staged_entry", ""), "<ENTRY>")
            if value.startswith("--jobs="):
                value = "--jobs=<AUTO>"
            normalized_command.append(value)
        digest.update(json.dumps(normalized_command, sort_keys=True).encode("utf-8"))

        with open(self._staged_entry, "rb") as entry_file:
            digest.update(entry_file.read())
        for requirement_file in sorted(self.extra_requirements_files):
            if os.path.isfile(requirement_file):
                with open(requirement_file, "rb") as source:
                    digest.update(source.read())

        digest.update(self._builder_environment_fingerprint())

        # Final Player auditing and archive writing happen after this cache is
        # restored. They must not force another LTO compilation.
        digest.update(self._player_compile_input_fingerprint().encode("ascii"))
        return digest.hexdigest()

    def _runtime_pack_compatibility_key(self) -> str:
        """Return a machine-independent key for a wheel-shipped Player pack."""
        requirements: list[str] = []
        for requirement_file in sorted(self.extra_requirements_files):
            if not os.path.isfile(requirement_file):
                continue
            normalized_lines: list[str] = []
            with open(requirement_file, "r", encoding="utf-8-sig") as source:
                for raw_line in source:
                    line = raw_line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    match = re.match(r"([A-Za-z0-9_.-]+)", line)
                    root = (
                        match.group(1).split(".", 1)[0].lower().replace("_", "-")
                        if match
                        else ""
                    )
                    if root in self._ENGINE_MANAGED_RUNTIME_PACKAGES:
                        continue
                    normalized_lines.append(line)
            if normalized_lines:
                requirements.append("\n".join(normalized_lines))

        def custom_packages(packages: list[str]) -> list[str]:
            return sorted(
                {
                    package
                    for package in packages
                    if package.split(".", 1)[0].lower().replace("_", "-")
                    not in self._ENGINE_MANAGED_RUNTIME_PACKAGES
                }
            )

        payload = {
            "contract": "infernux-runtime-pack",
            "layout": self._RUNTIME_PACK_LAYOUT,
            "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
            "platform": sys.platform,
            "machine": platform.machine().lower(),
            "console_mode": "module" if self.player_module else self.console_mode,
            "lto": bool(self.lto),
            "player_module": bool(getattr(self, "player_module", False)),
            "archive_format": "infernux-native-inxpack",
            # NumPy, Numba and llvmlite are engine-managed closures.
            # Branding/data post-processing also happens after core compile.
            "custom_include_packages": custom_packages(self.extra_include_packages),
            "custom_raw_packages": custom_packages(self.raw_copy_packages),
            "custom_requirements": sorted(requirements),
            "engine_fingerprint": self._player_compile_input_fingerprint(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _player_compile_input_fingerprint(self) -> str:
        if getattr(self, "_engine_fingerprint_cache", ""):
            return self._engine_fingerprint_cache

        import Infernux

        digest = hashlib.sha256()
        package_root = Path(resolved_path(Infernux.__file__)).parent
        hash_state = self._load_runtime_hash_state()
        live_hash_keys: set[str] = set()
        for path in sorted(package_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(package_root).as_posix()
            if (
                "/__pycache__/" in f"/{relative}/"
                or relative == _PACKAGED_RUNTIME_DIRNAME
                or relative.startswith(f"{_PACKAGED_RUNTIME_DIRNAME}/")
                or relative == _PACKAGED_RUNTIME_MODULE_DIRNAME
                or relative.startswith(f"{_PACKAGED_RUNTIME_MODULE_DIRNAME}/")
                or relative in self._PLAYER_POST_BUILD_ONLY_FILES
                or relative == "test"
                or relative.startswith("test/")
                or path.suffix.lower()
                in {".pyc", ".pdb", ".lib", ".exp", ".meta", ".bak"}
                or relative.endswith(".pyi")
            ):
                continue
            digest.update(relative.encode("utf-8"))
            content_hash, state_key = self._cached_file_hash(path, hash_state)
            live_hash_keys.add(state_key)
            digest.update(content_hash.encode("ascii"))

        # Development editors may load the native module from an immutable
        # runtime snapshot while python/Infernux/lib remains locked by an
        # older process.  The Player pack must follow the actually loaded
        # native payload, and its cache key must change with that payload.
        native_payload_dir = self._native_payload_dir()
        package_lib_dir = package_root / "lib"
        if path_key(native_payload_dir) != path_key(package_lib_dir):
            for path in sorted(
                self._native_payload_files(native_payload_dir),
                key=lambda item: item.name.casefold(),
            ):
                digest.update(f"native/{path.name}".encode("utf-8"))
                content_hash, state_key = self._cached_file_hash(path, hash_state)
                live_hash_keys.add(state_key)
                digest.update(content_hash.encode("ascii"))
        self._store_runtime_hash_state(
            {key: value for key, value in hash_state.items() if key in live_hash_keys}
        )
        self._engine_fingerprint_cache = digest.hexdigest()
        return self._engine_fingerprint_cache

    def _builder_environment_fingerprint(self) -> bytes:
        package_names = sorted({
            "nuitka",
            *(name.split(".", 1)[0] for name in self.extra_include_packages),
            *(name.split(".", 1)[0] for name in self.raw_copy_packages),
            *(
                name.split(".", 1)[0]
                for name in getattr(self, "runtime_support_packages", [])
            ),
        })
        script = f"""
import importlib.metadata as metadata
import json
import platform
import sys

versions = {{}}
for name in {package_names!r}:
    try:
        versions[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        versions[name] = "<missing>"
print(json.dumps({{
    "executable": sys.executable,
    "version": sys.version,
    "platform": platform.platform(),
    "packages": versions,
}}, sort_keys=True))
"""
        try:
            result = _run_python(self._builder_python, ["-c", script], timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().encode("utf-8")
        except Exception as exc:
            Debug.log_warning(f"Runtime Pack environment fingerprint failed: {exc}")
        return (
            f"{self._builder_python}\0{sys.version}\0{sys.platform}"
        ).encode("utf-8")

    def _runtime_hash_state_path(self) -> str:
        return os.path.join(self._runtime_pack_dir, _RUNTIME_HASH_STATE_FILENAME)

    def _load_runtime_hash_state(self) -> dict[str, dict]:
        try:
            with open(self._runtime_hash_state_path(), "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
            return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _cached_file_hash(path: Path, state: dict[str, dict]) -> tuple[str, str]:
        stat = path.stat()
        key = path_key(path)
        cached = state.get(key, {})
        if (
            stat.st_size >= _RUNTIME_HASH_CACHE_MIN_BYTES
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("ctime_ns") == stat.st_ctime_ns
            and isinstance(cached.get("sha256"), str)
        ):
            return cached["sha256"], key

        file_digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                file_digest.update(chunk)
        value = file_digest.hexdigest()
        state[key] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "sha256": value,
        }
        return value, key

    def _store_runtime_hash_state(self, state: dict[str, dict]) -> None:
        os.makedirs(self._runtime_pack_dir, exist_ok=True)
        state_path = self._runtime_hash_state_path()
        temporary = state_path + f".{os.getpid()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, sort_keys=True)
            os.replace(temporary, state_path)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass

    def _runtime_pack_path(self, runtime_pack_key: str) -> str:
        return os.path.join(self._runtime_pack_dir, runtime_pack_key)

    def _restore_runtime_pack(
        self,
        runtime_pack_key: str,
        *,
        compatibility_key: str = "",
    ) -> Optional[str]:
        """Restore a cached runtime by build key, then by packaged compatibility key."""
        restored = self._restore_runtime_pack_root(
            self._runtime_pack_path(runtime_pack_key),
            expected_fingerprint=runtime_pack_key,
            touch_manifest=True,
        )
        if restored is not None or not compatibility_key:
            return restored

        for root in self._packaged_runtime_roots():
            restored = self._restore_runtime_pack_root(
                os.path.join(root, compatibility_key),
                expected_compatibility_key=compatibility_key,
                expected_engine_fingerprint=self._player_compile_input_fingerprint(),
                touch_manifest=False,
            )
            if restored is not None:
                Debug.log_internal(
                    f"Packaged Runtime Pack hit: {compatibility_key[:16]} ({root})"
                )
                return restored
        return None

    @staticmethod
    def _packaged_runtime_roots() -> list[str]:
        roots: list[str] = []
        configured = os.environ.get("INFERNUX_PREBUILT_RUNTIME_PACK_DIR", "")
        if configured:
            roots.append(resolved_path(os.path.expanduser(configured)))
        try:
            import Infernux

            roots.append(
                str(
                    Path(resolved_path(Infernux.__file__)).parent
                    / _PACKAGED_RUNTIME_DIRNAME
                )
            )
        except (ImportError, OSError):
            pass
        return list(dict.fromkeys(roots))

    def _restore_runtime_pack_root(
        self,
        pack_root: str,
        *,
        expected_fingerprint: str = "",
        expected_compatibility_key: str = "",
        expected_engine_fingerprint: str = "",
        touch_manifest: bool = False,
    ) -> Optional[str]:
        """Restore one native Runtime.inxrt cache into Nuitka's staging tree."""

        manifest_path = os.path.join(pack_root, _RUNTIME_PACK_MANIFEST_FILENAME)
        archive_path = os.path.join(pack_root, _RUNTIME_ARCHIVE_FILENAME)
        try:
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            native_manifest = read_manifest(archive_path)
        except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
            return None

        if (
            (expected_fingerprint and manifest.get("fingerprint") != expected_fingerprint)
            or (
                expected_compatibility_key
                and manifest.get("compatibility_key") != expected_compatibility_key
            )
            or (
                expected_engine_fingerprint
                and manifest.get("engine_fingerprint") != expected_engine_fingerprint
            )
            or manifest.get("archive") != _RUNTIME_ARCHIVE_FILENAME
            or not os.path.isfile(archive_path)
            or manifest.get("archive_bytes") != native_manifest.get("archive_bytes")
            or manifest.get("archive_sha256") != native_manifest.get("archive_sha256")
        ):
            return None

        destination = os.path.join(self._staging_dir, "boot.dist")
        temporary = destination + f".{os.getpid()}.tmp"
        shutil.rmtree(destination, ignore_errors=True)
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            os.makedirs(temporary, exist_ok=False)
            extract_pack(archive_path, temporary)
            os.replace(temporary, destination)
        except (OSError, RuntimeError, ValueError) as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            Debug.log_warning(f"Runtime Pack restore failed: {exc}")
            return None
        if touch_manifest:
            try:
                os.utime(manifest_path, None)
            except OSError:
                pass
        Debug.log_internal(
            f"Runtime Pack cache hit: {manifest.get('fingerprint', '')[:16]}"
        )
        return destination

    def _store_runtime_pack(
        self,
        runtime_pack_key: str,
        dist_dir: str,
        *,
        compatibility_key: str = "",
        overwrite: bool = False,
    ) -> None:
        """Cache the compiled runtime through the native InxPack writer."""

        os.makedirs(self._runtime_pack_dir, exist_ok=True)
        pack_root = self._runtime_pack_path(runtime_pack_key)
        metadata = {
            "kind": "runtime-cache",
            "fingerprint": runtime_pack_key,
            "compatibility_key": compatibility_key,
            "engine_fingerprint": self._player_compile_input_fingerprint(),
            "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
            "platform": sys.platform,
            "machine": platform.machine().lower(),
            "console_mode": self.console_mode,
            "lto": bool(self.lto),
            "created_at": time.time(),
        }
        with open(
            os.path.join(dist_dir, "_infernux_runtime_pack.json"),
            "w",
            encoding="utf-8",
        ) as marker_file:
            json.dump(metadata, marker_file, indent=2, sort_keys=True)
            marker_file.write("\n")

        if os.path.isdir(pack_root):
            existing_archive = os.path.join(pack_root, _RUNTIME_ARCHIVE_FILENAME)
            existing_manifest = os.path.join(pack_root, _RUNTIME_PACK_MANIFEST_FILENAME)
            if not overwrite and os.path.isfile(existing_archive) and os.path.isfile(existing_manifest):
                return
            shutil.rmtree(pack_root, ignore_errors=True)
        temporary = pack_root + f".{os.getpid()}.tmp"
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            os.makedirs(temporary, exist_ok=False)
            archive_path = os.path.join(temporary, _RUNTIME_ARCHIVE_FILENAME)
            source_files: list[tuple[str, str]] = []
            for source_path in sorted(Path(dist_dir).rglob("*")):
                if not source_path.is_file():
                    continue
                if source_path.name in {
                    "_infernux_runtime_pack.json",
                    _RUNTIME_PACK_MANIFEST_FILENAME,
                    _RUNTIME_ARCHIVE_FILENAME,
                }:
                    continue
                if source_path.suffix.lower() in _RUNTIME_PACK_FORBIDDEN_SUFFIXES:
                    continue
                relative = source_path.relative_to(dist_dir).as_posix()
                if self._is_redundant_engine_native_alias(source_path, Path(dist_dir)):
                    continue
                source_files.append((relative, str(source_path)))
            if not source_files:
                raise RuntimeError("Native Runtime.inxrt would be empty")

            native_manifest = write_pack(source_files, archive_path)
            metadata.update(
                {
                    "archive": _RUNTIME_ARCHIVE_FILENAME,
                    "archive_sha256": native_manifest["archive_sha256"],
                    "archive_bytes": native_manifest["archive_bytes"],
                    "uncompressed_bytes": native_manifest["raw_bytes"],
                    "file_count": native_manifest["file_count"],
                    "compression": native_manifest["codec"],
                }
            )
            with open(
                os.path.join(temporary, _RUNTIME_PACK_MANIFEST_FILENAME),
                "w",
                encoding="utf-8",
            ) as manifest_file:
                json.dump(metadata, manifest_file, indent=2, sort_keys=True)
                manifest_file.write("\n")
            _publish_runtime_directory(Path(temporary), Path(pack_root))
            self._prune_runtime_packs()
            ratio = native_manifest["archive_bytes"] / max(1, native_manifest["raw_bytes"])
            Debug.log_internal(
                f"Runtime Pack cached: {runtime_pack_key[:16]} "
                f"({native_manifest['file_count']} files, {ratio:.1%} of source size)"
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _is_redundant_engine_native_alias(source_path: Path, dist_root: Path) -> bool:
        """Exclude the ABI alias when Nuitka already emitted its runtime short name."""
        if source_path.parent != dist_root / "Infernux" / "lib":
            return False
        name = source_path.name
        if name.startswith("_Infernux.cp") and name.endswith(".pyd"):
            return (source_path.parent / "_Infernux.pyd").is_file()
        if name.startswith("_Infernux.cpython-") and name.endswith(".so"):
            return (source_path.parent / "_Infernux.so").is_file()
        return False

    def export_runtime_pack(self, destination_root: str) -> str:
        """Copy the most recently built pack into a wheel package-data root."""
        if not self.last_runtime_pack_key or not self.last_runtime_compatibility_key:
            raise RuntimeError("build() must complete before exporting a Runtime Pack")
        source = Path(self._runtime_pack_path(self.last_runtime_pack_key))
        if not source.is_dir():
            raise RuntimeError(f"Runtime Pack cache is missing: {source}")

        destination = Path(resolved_path(destination_root)) / self.last_runtime_compatibility_key
        temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, temporary)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        return str(destination)

    def export_runtime_module(
        self,
        destination_root: str,
        *,
        module_name: str = "parallel",
        packages: Optional[List[str]] = None,
        profile: str = "development",
    ) -> str:
        """Build a wheel-shipped optional module through native InxPack."""

        if module_name != "parallel":
            raise ValueError(f"Unsupported Runtime Module: {module_name}")
        if not self.last_runtime_compatibility_key:
            raise RuntimeError("build() must complete before exporting a Runtime Module")

        selected_packages = sorted(set(packages or ["numba", "llvmlite"]))
        destination = (
            Path(resolved_path(destination_root)) / self.last_runtime_compatibility_key
        )
        temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
        payload_root = Path(tempfile.mkdtemp(prefix="infernux-runtime-module-"))
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            self._inject_jit_packages(str(payload_root), packages=selected_packages)
            temporary.mkdir(parents=True, exist_ok=False)
            archive_path = temporary / _RUNTIME_MODULE_ARCHIVE_FILENAME
            source_files: list[tuple[str, str]] = []
            for source_path in sorted(payload_root.rglob("*")):
                if not source_path.is_file():
                    continue
                if source_path.suffix.lower() in _RUNTIME_MODULE_FORBIDDEN_SUFFIXES:
                    continue
                source_files.append(
                    (source_path.relative_to(payload_root).as_posix(), str(source_path))
                )
            if not source_files:
                raise RuntimeError("Parallel Runtime Module contains no files")
            native_manifest = write_pack(
                source_files,
                archive_path,
                profile=profile,
            )
            manifest = {
                "kind": "runtime-module",
                "module": module_name,
                "compatibility_key": self.last_runtime_compatibility_key,
                "engine_fingerprint": self._player_compile_input_fingerprint(),
                "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
                "platform": sys.platform,
                "machine": platform.machine().lower(),
                "packages": selected_packages,
                "archive": _RUNTIME_MODULE_ARCHIVE_FILENAME,
                "archive_sha256": native_manifest["archive_sha256"],
                "archive_bytes": native_manifest["archive_bytes"],
                "uncompressed_bytes": native_manifest["raw_bytes"],
                "file_count": native_manifest["file_count"],
                "compression": native_manifest["codec"],
                "compression_profile": profile,
                "created_at": time.time(),
            }
            with (temporary / _RUNTIME_MODULE_MANIFEST_FILENAME).open(
                "w", encoding="utf-8"
            ) as manifest_file:
                json.dump(manifest, manifest_file, indent=2, sort_keys=True)
                manifest_file.write("\n")
            _publish_runtime_directory(temporary, destination)
            return str(destination)
        finally:
            shutil.rmtree(payload_root, ignore_errors=True)
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _packaged_runtime_module_roots() -> list[str]:
        roots: list[str] = []
        configured = os.environ.get("INFERNUX_PREBUILT_RUNTIME_MODULE_DIR", "")
        if configured:
            roots.append(resolved_path(os.path.expanduser(configured)))
        try:
            import Infernux

            roots.append(
                str(
                    Path(resolved_path(Infernux.__file__)).parent
                    / _PACKAGED_RUNTIME_MODULE_DIRNAME
                )
            )
        except (ImportError, OSError):
            pass
        return list(dict.fromkeys(roots))

    def install_runtime_module(
        self,
        dist_dir: str,
        *,
        module_name: str = "parallel",
        packages: Optional[List[str]] = None,
        archive_only: bool = False,
        profile: str = "development",
    ) -> bool:
        """Install or stage an optional native Runtime Module."""

        if module_name != "parallel":
            raise ValueError(f"Unsupported Runtime Module: {module_name}")
        selected_packages = sorted(set(packages or ["numba", "llvmlite"]))
        compatibility_key = self.last_runtime_compatibility_key
        if compatibility_key:
            for root in self._packaged_runtime_module_roots():
                module_root = Path(root) / compatibility_key
                manifest_path = module_root / _RUNTIME_MODULE_MANIFEST_FILENAME
                archive_path = module_root / _RUNTIME_MODULE_ARCHIVE_FILENAME
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    native_manifest = read_manifest(archive_path)
                except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
                    continue
                if (
                    manifest.get("kind") != "runtime-module"
                    or manifest.get("module") != module_name
                    or manifest.get("compatibility_key") != compatibility_key
                    or manifest.get("engine_fingerprint")
                    != self._player_compile_input_fingerprint()
                    or sorted(manifest.get("packages", [])) != selected_packages
                    or manifest.get("archive") != _RUNTIME_MODULE_ARCHIVE_FILENAME
                    or manifest.get("archive_bytes") != native_manifest.get("archive_bytes")
                    or manifest.get("archive_sha256") != native_manifest.get("archive_sha256")
                ):
                    continue

                if archive_only:
                    destination = Path(dist_dir) / _RUNTIME_MODULE_ARCHIVE_FILENAME
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        destination.unlink()
                    if profile == "development":
                        shutil.copy2(archive_path, destination)
                    else:
                        temporary = Path(
                            tempfile.mkdtemp(prefix="infernux-module-repack-")
                        )
                        try:
                            allowed_roots = set(selected_packages) | {
                                f"{package}.libs" for package in selected_packages
                            }
                            extract_pack(
                                archive_path,
                                str(temporary),
                                allowed_roots=allowed_roots,
                            )
                            source_files = [
                                (source.relative_to(temporary).as_posix(), str(source))
                                for source in sorted(temporary.rglob("*"))
                                if source.is_file()
                            ]
                            write_pack(source_files, destination, profile=profile)
                        finally:
                            shutil.rmtree(temporary, ignore_errors=True)
                    Debug.log_internal(
                        f"Packaged {module_name} Runtime Module staged native: "
                        f"{compatibility_key[:16]}"
                    )
                    return True

                temporary = Path(tempfile.mkdtemp(prefix="infernux-module-"))
                try:
                    allowed_roots = set(selected_packages) | {
                        f"{package}.libs" for package in selected_packages
                    }
                    extract_pack(
                        archive_path,
                        str(temporary),
                        allowed_roots=allowed_roots,
                    )
                    destination_root = Path(dist_dir)
                    for child in temporary.iterdir():
                        destination = destination_root / child.name
                        if destination.is_dir():
                            shutil.rmtree(destination)
                        elif destination.exists():
                            destination.unlink()
                        shutil.move(str(child), destination)
                    Debug.log_internal(
                        f"Packaged {module_name} Runtime Module installed native: "
                        f"{compatibility_key[:16]}"
                    )
                    return True
                except (OSError, RuntimeError, ValueError) as exc:
                    Debug.log_warning(f"Runtime Module restore failed: {exc}")
                finally:
                    shutil.rmtree(temporary, ignore_errors=True)

        Debug.log_internal(
            f"Packaged {module_name} Runtime Module unavailable; "
            "building it from the local build environment"
        )
        if archive_only:
            temporary_root = Path(tempfile.mkdtemp(prefix="infernux-module-export-"))
            try:
                exported = Path(
                    self.export_runtime_module(
                        str(temporary_root),
                        module_name=module_name,
                        packages=selected_packages,
                        profile=profile,
                    )
                )
                source = exported / _RUNTIME_MODULE_ARCHIVE_FILENAME
                if not source.is_file():
                    raise RuntimeError(
                        f"Native {_RUNTIME_MODULE_ARCHIVE_FILENAME} is missing from {exported}"
                    )
                destination = Path(dist_dir) / _RUNTIME_MODULE_ARCHIVE_FILENAME
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination.unlink()
                shutil.copy2(source, destination)
                return True
            finally:
                shutil.rmtree(temporary_root, ignore_errors=True)
        self._inject_jit_packages(dist_dir, packages=selected_packages)
        return all((Path(dist_dir) / package).is_dir() for package in selected_packages)

    def _prune_runtime_packs(self) -> None:
        try:
            packs = [
                path for path in Path(self._runtime_pack_dir).iterdir()
                if path.is_dir() and not path.name.endswith(".tmp")
            ]
        except OSError:
            return
        packs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in packs[_MAX_RUNTIME_PACKS:]:
            shutil.rmtree(stale, ignore_errors=True)

    # ------------------------------------------------------------------
    # Nuitka availability check
    # ------------------------------------------------------------------

    def _check_nuitka(self):
        """Ensure Nuitka and build-time project dependencies are installed."""
        import time as _time
        try:
            _t0 = _time.perf_counter()
            _ensure_python_packages(
                self._builder_python,
                "nuitka",
                "ordered_set",
                *self.extra_include_packages,
                *self.raw_copy_packages,
                *getattr(self, "runtime_support_packages", []),
            )
            Debug.log_internal(
                f"  _ensure_python_packages in {_time.perf_counter() - _t0:.2f}s"
            )
            _t1 = _time.perf_counter()
            _install_requirements_files(
                self._builder_python,
                self.extra_requirements_files,
                self._requirements_state_dir,
            )
            Debug.log_internal(
                f"  _install_requirements_files in {_time.perf_counter() - _t1:.2f}s"
            )
        except Exception as exc:
            raise RuntimeError(
                "Failed to prepare the builder Python environment.  "
                f"Builder Python: {self._builder_python}\n"
                "Please run manually:\n"
                "    pip install nuitka ordered-set\n"
                "and install the project's requirements.txt if needed."
            ) from exc

    # ------------------------------------------------------------------
    # Staging directory
    # ------------------------------------------------------------------

    def _prepare_staging(self):
        """Create a clean ASCII-only staging directory.

        Using a short ASCII-only staging directory avoids temporary-path
        edge cases and keeps compiler output paths stable on Windows.
        """
        if os.path.isdir(self._staging_dir):
            if sys.platform == "win32":
                subprocess.run(
                    ["cmd", "/c", "rd", "/s", "/q", self._staging_dir],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                shutil.rmtree(self._staging_dir, ignore_errors=True)
        os.makedirs(self._staging_dir, exist_ok=True)

        # Nuitka module mode requires the source module name and output module
        # name to match. Standalone builds retain the historical boot.py name.
        staged_name = "_InfernuxPlayer.py" if getattr(self, "player_module", False) else "boot.py"
        staged_script = os.path.join(self._staging_dir, staged_name)
        shutil.copy2(self.entry_script, staged_script)
        self._staged_entry = staged_script

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def _build_command(self) -> List[str]:
        """Assemble the Nuitka command line.

        All output paths point to the ASCII-safe staging directory.
        """
        # A few lightweight tests and integrations construct this object
        # without running __init__. Keep the ordinary builder path robust.
        player_module = getattr(self, "player_module", False)
        cmd = [
            self._builder_python, "-m", "nuitka",
            "--module" if player_module else "--standalone",
            "--assume-yes-for-downloads",
            f"--output-dir={self._staging_dir}",
            # Disable Nuitka's deployment-time hard-crash when an excluded
            # module is imported.  Some modules are legitimately excluded
            # but lazily imported with graceful fallback (try/except or
            # None checks); the default deployment flag converts those
            # into RuntimeErrors which is counter-productive.
            "--no-deployment-flag=excluded-module-usage",
        ]
        if not player_module:
            cmd.append("--follow-imports")
            cmd.append(f"--output-filename={self.output_filename}")
        else:
            # Keep the extension a true single-module bootstrap. Even followed
            # stdlib bytecode has triggered recursion in Nuitka's module loader
            # before this module body runs. Every non-frozen dependency is
            # therefore supplied as a physical Bootstrap/Runtime payload.
            cmd.append("--nofollow-imports")

        if sys.platform == "win32":
            if not player_module:
                cmd.append(f"--windows-console-mode={self.console_mode}")
            # Do not pass --msvc=latest here.  _run_nuitka initializes a full
            # cl/link/rc/mt + Windows SDK environment before spawning Nuitka;
            # forcing "latest" makes SCons run its own VS/SDK discovery again,
            # which is exactly what fails on some machines with a valid SDK.

        # Link-time optimization for smaller and faster binaries
        if self.lto:
            cmd.append("--lto=yes")

        # Strip docstrings and assert statements for smaller output
        cmd.append("--python-flag=-OO")

        # Tell Nuitka to exclude large dev-only frameworks at the module
        # level — catches transitive imports that --nofollow-import-to
        # might miss.
        cmd.append("--noinclude-pytest-mode=nofollow")
        cmd.append("--noinclude-unittest-mode=nofollow")
        cmd.append("--noinclude-setuptools-mode=nofollow")

        # Parallel C compilation
        cmd.append("--jobs=%d" % max(1, os.cpu_count() - 1))

        # Include package data (fonts, shaders, icons…) but NOT the whole
        # package as source. Standalone mode traces reachable imports, while
        # module mode uses only the explicit include/follow selections below,
        # as required by Nuitka. This avoids compiling the entire editor UI.
        if not player_module:
            cmd += [
                "--include-package-data=Infernux",
                f"--noinclude-data-files=Infernux/{_PACKAGED_RUNTIME_DIRNAME}/*",
                f"--noinclude-data-files=Infernux/{_PACKAGED_RUNTIME_MODULE_DIRNAME}/*",
                # Editor-only localization must never enter a Player staging tree.
                "--noinclude-data-files=Infernux/engine/locales/*",
                "--noinclude-data-files=Infernux/engine/locales/**/*.json",
            ]

        if not player_module:
            cmd.append("--include-module=_InfernuxBootstrap")
            cmd.append("--include-module=Infernux.lib._Infernux")
            cmd.append("--include-module=ctypes")

            # csv is needed by importlib.metadata (Python's own import system)
            # but Nuitka may not auto-detect it when JIT packages are excluded.
            cmd.append("--include-module=csv")

        # Prevent Nuitka from following into editor-only modules that the
        # standalone player never uses.  The _INFERNUX_PLAYER_MODE guard
        # in __init__ already prevents runtime loading, but --nofollow
        # also speeds up Nuitka's compile-time analysis significantly.
        #
        # RenderStack hot reload is explicitly Editor-only, so the Player can
        # exclude ResourcesManager and its watcher rather than carrying a
        # dormant authoring service.
        for _editor_mod in (
            "watchdog",
            "PIL",
            "cv2",
            "imageio",
            "psd_tools",
            "av",  # PyAV/ffmpeg — build-time splash encoding only
        ):
            cmd.append(f"--nofollow-import-to={_editor_mod}")

        for _editor_mod in sorted(self._PLAYER_EDITOR_ONLY_MODULES):
            cmd.append(f"--nofollow-import-to={_editor_mod}")

        for _excluded_mod in sorted(self._GAME_BUILD_NOFOLLOW_MODULES):
            cmd.append(f"--nofollow-import-to={_excluded_mod}")

        # Exclude JIT packages from Nuitka compilation — they will be
        # injected as raw site-packages afterwards so numba retains the
        # Python bytecode it needs for LLVM JIT at runtime.
        runtime_dependency_packages = set(self.raw_copy_packages) | set(
            getattr(self, "runtime_support_packages", [])
        )
        _nofollow_jit = self._JIT_NOFOLLOW_PACKAGES | runtime_dependency_packages
        for _jit_pkg in sorted(_nofollow_jit):
            cmd.append(f"--nofollow-import-to={_jit_pkg}")

        # Auto-discover stdlib modules that the raw-copied JIT packages
        # import transitively.  Nuitka can't discover them because the
        # packages are excluded via --nofollow-import-to, so we trace
        # them in a subprocess and include them explicitly.
        if runtime_dependency_packages and not player_module:
            for _stdlib_mod in self._discover_jit_stdlib_deps():
                cmd.append(f"--include-module={_stdlib_mod}")

        # Numba's parallel backend (prange / parallel=True) imports
        # multiprocessing lazily at JIT compile time — NOT at
        # ``import numba``.  The auto-discovery above therefore misses
        # it.  Include it unconditionally when JIT packages are bundled.
        if not player_module and _nofollow_jit & self._JIT_NOFOLLOW_PACKAGES:
            cmd.append("--include-module=multiprocessing")

        if not player_module:
            for pkg in self.extra_include_packages:
                if pkg not in _nofollow_jit:
                    cmd.append(f"--include-package={pkg}")

            for pattern in self.extra_include_data:
                cmd.append(f"--include-package-data={pattern}")

        # Product metadata (Windows)
        if sys.platform == "win32":
            cmd.append(f"--product-name={self.product_name}")
            cmd.append(f"--file-version={self.file_version}")
            cmd.append(f"--product-version={self.file_version}")

            if self.icon_path and os.path.isfile(self.icon_path):
                ico = self._ensure_ico(self.icon_path)
                if ico:
                    cmd.append(f"--windows-icon-from-ico={ico}")

        # Exclude heavy dev/test modules that aren't needed at runtime
        for mod in ("tkinter", "unittest", "test", "pip",
                    "setuptools", "distutils", "ensurepip"):
            cmd.append(f"--nofollow-import-to={mod}")

        cmd.append(self._staged_entry)
        return cmd

    # ------------------------------------------------------------------
    # Auto-discover JIT package stdlib dependencies
    # ------------------------------------------------------------------

    def _discover_jit_stdlib_deps(self) -> List[str]:
        """Import raw_copy_packages in the builder Python and return the
        set of stdlib top-level modules they transitively load.

        This replaces the manual list approach — numba/llvmlite/numpy
        pull in dozens of stdlib modules (email, csv, html, http, …)
        that Nuitka cannot discover because we exclude these packages
        via --nofollow-import-to.
        """
        import time as _time
        _t0 = _time.perf_counter()

        pkgs_arg = ",".join(sorted(
            set(self.raw_copy_packages)
            | set(getattr(self, "runtime_support_packages", []))
        ))
        # The subprocess: record modules before vs after importing the
        # packages, then report only stdlib top-level names.
        trace_script = (
            "import sys; "
            "before = set(sys.modules); "
            "pkgs = '$PKGS'.split(','); "
            "[__import__(p) for p in pkgs]; "
            "after = set(sys.modules); "
            "new = {m.split('.')[0] for m in after - before}; "
            "stdlib = sorted(new & sys.stdlib_module_names); "
            "print(','.join(stdlib))"
        ).replace("$PKGS", pkgs_arg)

        try:
            result = _run_python(
                self._builder_python,
                ["-c", trace_script],
                timeout=120,
            )
            if result.returncode != 0 or not result.stdout.strip():
                Debug.log_warning(
                    f"JIT stdlib trace failed (exit {result.returncode}): "
                    f"{(result.stderr or '').strip()[:200]}"
                )
                return []

            mods = [m for m in result.stdout.strip().split(",") if m]
            Debug.log_internal(
                f"  JIT stdlib trace: {len(mods)} modules in "
                f"{_time.perf_counter() - _t0:.2f}s  "
                f"({', '.join(mods[:10])}{'…' if len(mods) > 10 else ''})"
            )
            return mods
        except Exception as exc:
            Debug.log_warning(f"JIT stdlib trace error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Nuitka execution
    # ------------------------------------------------------------------

    def _run_nuitka(
        self,
        cmd: List[str],
        on_progress: Optional[Callable[[str, float], None]],
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Run Nuitka as a subprocess and stream output.  Returns dist dir."""
        if cancel_event is not None and cancel_event.is_set():
            raise BuildCancelled()

        if sys.platform == "win32" and not _has_msvc_toolchain():
            raise RuntimeError("Compiling a Player runtime requires Microsoft Visual C++ Build Tools (MSVC).")
        if sys.platform.startswith("linux") and shutil.which("gcc") is None and shutil.which("clang") is None:
            raise RuntimeError("Compiling a Player runtime requires a C compiler (gcc or clang).")

        alias = _windows_ascii_build_alias(self._build_cache_root)
        process_build_root = alias or self._build_cache_root
        try:
            return self._run_nuitka_process(
                cmd,
                on_progress,
                cancel_event,
                process_build_root=process_build_root,
            )
        finally:
            _remove_windows_build_alias(alias)

    def _run_nuitka_process(
        self,
        cmd: List[str],
        on_progress: Optional[Callable[[str, float], None]],
        cancel_event: Optional[threading.Event],
        *,
        process_build_root: str,
    ) -> str:
        """Execute Nuitka through the compiler-visible build-cache path."""

        process_staging_dir = os.path.join(
            process_build_root,
            *relative_path(self._staging_dir, self._build_cache_root).split("/"),
        )
        process_nuitka_cache_dir = os.path.join(process_build_root, "Nuitka")
        process_cmd = [
            str(argument).replace(self._build_cache_root, process_build_root)
            for argument in cmd
        ]

        env = os.environ.copy()

        # Redirect TEMP / TMP to an ASCII-safe location so MinGW's
        # std::filesystem never encounters non-ASCII characters.
        safe_tmp = os.path.join(process_staging_dir, "_tmp")
        os.makedirs(safe_tmp, exist_ok=True)
        env["TEMP"] = safe_tmp
        env["TMP"] = safe_tmp

        safe_profile = os.path.join(process_staging_dir, "_profile")
        safe_local_appdata = os.path.join(safe_profile, "AppData", "Local")
        safe_roaming_appdata = os.path.join(safe_profile, "AppData", "Roaming")
        for path in (safe_profile, safe_local_appdata, safe_roaming_appdata):
            os.makedirs(path, exist_ok=True)

        env["USERPROFILE"] = safe_profile
        env["HOME"] = safe_profile
        env["LOCALAPPDATA"] = safe_local_appdata
        env["APPDATA"] = safe_roaming_appdata
        if sys.platform == "win32":
            drive, tail = os.path.splitdrive(safe_profile)
            env["HOMEDRIVE"] = drive or "C:"
            env["HOMEPATH"] = tail or "\\"

        # Use a persistent cache directory so Nuitka can reuse compiled C
        # code across builds — this is the single biggest speed win.
        os.makedirs(process_nuitka_cache_dir, exist_ok=True)
        env["NUITKA_CACHE_DIR"] = process_nuitka_cache_dir

        # If we switch away from the current interpreter to a reusable build
        # venv, preserve the current import roots so Nuitka can still resolve
        # the live Infernux package and project-installed dependencies.
        pythonpath_entries: list[str] = []
        existing_pythonpath = env.get("PYTHONPATH", "")
        if existing_pythonpath:
            pythonpath_entries.extend([p for p in existing_pythonpath.split(os.pathsep) if p])
        pythonpath_entries.extend(
            path for path in sys.path
            if path and os.path.isdir(path)
        )
        if pythonpath_entries:
            env["PYTHONPATH"] = os.pathsep.join(_dedupe_paths(pythonpath_entries))

        if sys.platform == "win32":
            env = _ensure_windows_msvc_environment(env)

        from Infernux.runtime_utf8 import apply_utf8_defaults

        env = apply_utf8_defaults(env)

        import time as _time
        _nuitka_proc_t0 = _time.perf_counter()
        process_group_args = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            process_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=process_staging_dir,
            **process_group_args,
        )

        lines_collected: List[str] = []
        output_queue: queue.Queue[Optional[str]] = queue.Queue()

        def _read_output() -> None:
            try:
                if proc.stdout is not None:
                    for output_line in proc.stdout:
                        output_queue.put(output_line)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=_read_output, name="InfernuxNuitkaOutput", daemon=True)
        reader.start()
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise BuildCancelled()
                try:
                    line = output_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    break
                line = line.rstrip()
                lines_collected.append(line)
                if on_progress:
                    # Crude progress: Nuitka logs many lines; we map to 10%–85%
                    pct = min(0.85, 0.10 + len(lines_collected) * 0.001)
                    on_progress(line[-80:] if len(line) > 80 else line, pct)
        except BuildCancelled:
            _terminate_process_tree(proc)
            reader.join(timeout=2.0)
            raise
        except Exception:
            _terminate_process_tree(proc)
            reader.join(timeout=2.0)
            raise

        proc.wait()
        reader.join(timeout=2.0)
        _nuitka_elapsed = _time.perf_counter() - _nuitka_proc_t0
        Debug.log_internal(
            f"  Nuitka subprocess finished in {_nuitka_elapsed:.1f}s  "
            f"({len(lines_collected)} output lines, exit {proc.returncode})"
        )

        if proc.returncode != 0:
            tail = "\n".join(lines_collected[-30:])
            diagnostics = self._read_scons_diagnostics()
            if diagnostics:
                tail = tail + "\n\n" + diagnostics
            raise RuntimeError(
                f"Nuitka compilation failed (exit code {proc.returncode}).\n"
                f"Last output:\n{tail}"
            )

        if self.player_module:
            module_path = _find_player_module_output(self._staging_dir)
            dist_dir = os.path.join(self._staging_dir, "boot.dist")
            os.makedirs(dist_dir, exist_ok=True)
            shutil.move(module_path, os.path.join(dist_dir, os.path.basename(module_path)))
            return dist_dir

        # Nuitka places standalone output in <staging_dir>/boot.dist/
        dist_dir = os.path.join(self._staging_dir, "boot.dist")
        if not os.path.isdir(dist_dir):
            raise RuntimeError(
                f"Nuitka dist directory not found: {dist_dir}\n"
                "Compilation may have failed silently."
            )
        return dist_dir

    def _read_scons_diagnostics(self) -> str:
        build_dir = os.path.join(self._staging_dir, "boot.build")
        chunks: list[str] = []
        for filename in ("scons-report.txt", "scons-error-report.txt"):
            path = os.path.join(build_dir, filename)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read().strip()
            except OSError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                continue
            if text:
                chunks.append(f"--- {filename} ---\n{text[-5000:]}")
        return "\n\n".join(chunks)

    # ------------------------------------------------------------------
    # Inject native engine libraries
    # ------------------------------------------------------------------

    @staticmethod
    def _native_module_file(lib_dir: Path) -> Path:
        """Resolve the current interpreter's exact Infernux extension module."""
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            candidate = lib_dir / f"_Infernux{suffix}"
            if candidate.is_file():
                return candidate
        raise RuntimeError(
            f"Infernux native payload has no compatible _Infernux module: {lib_dir}"
        )

    @staticmethod
    def _bootstrap_module_file(lib_dir: Path) -> Path:
        """Resolve the lightweight extension used before Runtime.inxrt extraction."""
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            candidate = lib_dir / f"_InfernuxBootstrap{suffix}"
            if candidate.is_file():
                return candidate
        raise RuntimeError(
            f"Infernux native payload has no compatible _InfernuxBootstrap module: {lib_dir}"
        )

    @staticmethod
    def _native_payload_files(lib_dir: Path) -> list[Path]:
        """Return one ABI-compatible extension module plus its shared libraries."""
        if sys.platform == "win32":
            wanted = (".dll",)
        elif sys.platform == "darwin":
            wanted = (".so", ".dylib")
        else:
            wanted = (".so",)

        module_file = NuitkaBuilder._native_module_file(lib_dir)
        bootstrap_module = NuitkaBuilder._bootstrap_module_file(lib_dir)
        files = [module_file]
        for path in lib_dir.iterdir():
            if not path.is_file() or path in {module_file, bootstrap_module}:
                continue
            # Never mix a stale short-name extension with the selected
            # ABI-tagged module. Nuitka creates the short name itself.
            if path.name.startswith("_Infernux"):
                continue
            if path.suffix.lower() in wanted or (
                sys.platform.startswith("linux")
                and path.name.startswith("lib")
                and ".so" in path.name
            ):
                files.append(path)
        return sorted(files, key=lambda path: path.name.lower())

    @classmethod
    def _player_native_payload_is_compatible(cls, lib_dir: Path) -> bool:
        """Return whether *lib_dir* contains a self-contained Player bridge."""

        try:
            cls._native_payload_files(lib_dir)
            contract = json.loads(
                (lib_dir / cls._PLAYER_NATIVE_CONTRACT_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        present = {
            path.name.casefold() for path in cls._native_payload_files(lib_dir)
        }
        required = {
            name.casefold() for name in player_native_library_filenames()
        }
        return contract == cls._PLAYER_NATIVE_CONTRACT and required <= present

    @classmethod
    def _source_build_player_native_dirs(cls, loaded_dir: Path) -> list[Path]:
        """Find shipping-native siblings of an Editor development payload."""

        build_roots: list[Path] = []
        for parent in (loaded_dir, *loaded_dir.parents):
            if (
                parent.name.casefold() == "build"
                and parent.parent.name.casefold() == "out"
            ):
                build_roots.append(parent)
                break
        try:
            import Infernux

            package_root = Path(resolved_path(Infernux.__file__)).parent
            repository_root = package_root.parents[1]
            build_roots.append(repository_root / "out" / "build")
        except (ImportError, OSError, IndexError, TypeError):
            pass

        candidates: list[Path] = []
        seen: set[str] = set()
        for build_root in build_roots:
            for pattern in ("*/python-sync", "*/Release", "*/RelWithDebInfo"):
                for candidate in build_root.glob(pattern):
                    key = path_key(candidate)
                    if key in seen or path_key(candidate) == path_key(loaded_dir):
                        continue
                    seen.add(key)
                    if cls._player_native_payload_is_compatible(candidate):
                        candidates.append(candidate)

        def module_timestamp(candidate: Path) -> int:
            try:
                return cls._native_module_file(candidate).stat().st_mtime_ns
            except OSError:
                return -1

        return sorted(candidates, key=module_timestamp, reverse=True)

    @staticmethod
    def _native_payload_dir() -> Path:
        """Resolve the atomic native payload used by this builder process."""
        override = os.environ.get("INFERNUX_NATIVE_MODULE_DIR", "").strip()
        if override:
            candidate = Path(resolved_path(override))
            if not candidate.is_dir():
                raise RuntimeError(
                    f"INFERNUX_NATIVE_MODULE_DIR is not a directory: {candidate}"
                )
            NuitkaBuilder._native_payload_files(candidate)
            if not NuitkaBuilder._player_native_payload_is_compatible(candidate):
                raise RuntimeError(
                    "INFERNUX_NATIVE_MODULE_DIR points to an Editor development "
                    "runtime. Build and select a static Release Player runtime."
                )
            return candidate

        native_module = importlib.import_module("Infernux.lib._Infernux")
        module_file = getattr(native_module, "__file__", "")
        if not module_file:
            raise RuntimeError("Loaded Infernux native module has no filesystem path")
        candidate = Path(resolved_path(module_file)).parent
        NuitkaBuilder._native_payload_files(candidate)
        if NuitkaBuilder._player_native_payload_is_compatible(candidate):
            return candidate

        alternatives = NuitkaBuilder._source_build_player_native_dirs(candidate)
        if alternatives:
            selected = alternatives[0]
            Debug.log_internal(
                "Selected static Player native payload instead of the active "
                f"Editor development runtime: {selected}"
            )
            return selected
        raise RuntimeError(
            "The active Infernux native module is an Editor development runtime "
            "and cannot be packaged into a Player. Build the platform Release "
            "preset first (for example: cmake --build --preset windows-msvc-release)."
        )

    def _inject_native_libs(self, dist_dir: str):
        """Stage the bootstrap root and complete package-qualified native runtime.

        Nuitka won't automatically pick up .pyd files built outside its
        compilation scope (pybind11 extensions), so we inject them into
        the correct package subdirectory so that
        ``from ._Infernux import *`` (relative import in Infernux.lib)
        can find the .pyd, and ``os.add_dll_directory(lib_dir)`` picks
        up the companion DLLs.
        """
        import time as _time
        _inject_t0 = _time.perf_counter()
        lib_dir = self._native_payload_dir()

        # Target: <dist>/Infernux/lib/  — mirrors the installed package
        # structure so relative imports work at runtime.
        target_dir = Path(dist_dir) / "Infernux" / "lib"
        target_dir.mkdir(parents=True, exist_ok=True)

        dist_root = Path(dist_dir)

        native_files = self._native_payload_files(lib_dir)
        native_module = self._native_module_file(lib_dir)
        bootstrap_module = self._bootstrap_module_file(lib_dir)

        for src in native_files:
            # The CPython DLL is owned exclusively by the pre-extraction
            # CPython bootstrap. Nuitka normally emits it at the dist root;
            # refresh that copy and never duplicate it inside Runtime.inxrt.
            if (
                sys.platform == "win32"
                and src.name.casefold() == WINDOWS_PYTHON_DLL.casefold()
            ):
                shutil.copy2(src, dist_root / src.name)
                try:
                    (target_dir / src.name).unlink()
                except FileNotFoundError:
                    pass
                Debug.log_internal(f"  Injected (CPython bootstrap): {src.name}")
                continue

            # Native modules + shared libs go into the package subdir; on
            # Linux/macOS the module's RPATH ($ORIGIN/@loader_path) finds its
            # dependencies right there.
            dst_pkg = target_dir / src.name
            shutil.copy2(src, dst_pkg)
            Debug.log_internal(f"  Injected (lib): {src.name}")

            if src == native_module:
                # Nuitka imports extension modules through a canonical short
                # filename. Always overwrite that file as well; leaving the
                # Nuitka-discovered copy in place can pair an old pybind ABI
                # with freshly injected engine DLLs and crash before frame 1.
                canonical_name = "_Infernux.pyd" if sys.platform == "win32" else "_Infernux.so"
                canonical_module = target_dir / canonical_name
                if canonical_module != dst_pkg:
                    shutil.copy2(src, canonical_module)
                    Debug.log_internal(f"  Injected (canonical lib): {canonical_name}")

            # Foundation is the bootstrap module's sole Infernux dependency
            # and must remain available before Runtime.inxrt is extracted.
            if src.name.casefold() in {
                "infernuxfoundation.dll",
                "libinfernuxfoundation.so",
            }:
                dst_root = dist_root / src.name
                shutil.copy2(src, dst_root)
                Debug.log_internal(f"  Injected (bootstrap dependency): {src.name}")
            elif sys.platform == "win32" and src.suffix.casefold() == ".dll":
                try:
                    (dist_root / src.name).unlink()
                except FileNotFoundError:
                    pass

        full_root_name = "_Infernux.pyd" if sys.platform == "win32" else "_Infernux.so"
        try:
            (dist_root / full_root_name).unlink()
        except FileNotFoundError:
            pass

        bootstrap_name = (
            "_InfernuxBootstrap.pyd"
            if sys.platform == "win32"
            else "_InfernuxBootstrap.so"
        )
        shutil.copy2(bootstrap_module, dist_root / bootstrap_name)
        Debug.log_internal(f"  Injected (bootstrap root): {bootstrap_name}")

        Debug.log_internal(
            f"  native lib injection total: {_time.perf_counter() - _inject_t0:.2f}s  "
            f"({len(native_files) + 1} files)"
        )

    def _python_bootstrap_runtime_sources(self) -> tuple[dict[str, Path], Path]:
        """Resolve the CPython files required before Runtime.inxrt is mounted."""

        python_root = Path(resolved_path(self._builder_python)).parent
        ctypes_spec = importlib.util.find_spec("_ctypes")
        encodings_spec = importlib.util.find_spec("encodings")
        if ctypes_spec is None or not ctypes_spec.origin:
            raise RuntimeError("Builder Python has no loadable _ctypes extension")
        if encodings_spec is None or not encodings_spec.submodule_search_locations:
            raise RuntimeError("Builder Python has no encodings package")

        # Standalone CPython can link _ctypes into libpython itself.
        ctypes_path = (
            None if ctypes_spec.origin == "built-in"
            else Path(resolved_path(ctypes_spec.origin))
        )
        environment_root = python_root.parent
        environment_roots: list[Path] = []
        environment_root_keys: set[str] = set()
        environment_candidates = [environment_root]
        if path_key(self._builder_python) == path_key(sys.executable):
            # A project venv usually contains only a launcher and site-packages.
            # Its CPython shared library remains in the managed base runtime.
            # Follow that base only for the interpreter running this builder;
            # an explicitly supplied foreign interpreter must stay isolated.
            environment_candidates.extend(
                (
                    Path(resolved_path(sys.prefix)),
                    Path(resolved_path(sys.exec_prefix)),
                    Path(resolved_path(sys.base_prefix)),
                    Path(resolved_path(sys.base_exec_prefix)),
                )
            )
        for candidate in environment_candidates:
            key = path_key(candidate)
            if key in environment_root_keys:
                continue
            environment_root_keys.add(key)
            environment_roots.append(candidate)

        search_roots_list = [python_root]
        if ctypes_path is not None:
            search_roots_list.append(ctypes_path.parent)
        for root in environment_roots:
            search_roots_list.extend(
                (
                    root,
                    root / "DLLs",
                    root / "Library" / "bin",
                    root / "lib",
                )
            )
        search_roots = tuple(dict.fromkeys(search_roots_list))

        def find_runtime_file(filename: str, *, required: bool) -> Optional[Path]:
            for root in search_roots:
                candidate = root / filename
                if candidate.is_file():
                    return candidate
            if required:
                raise RuntimeError(
                    f"Builder Python bootstrap dependency is missing: {filename}"
                )
            return None

        if sys.platform == "win32":
            if ctypes_path is None:
                raise RuntimeError("Windows PlayerHost requires an external _ctypes module")
            sources: dict[str, Path] = {
                WINDOWS_PYTHON_DLL: find_runtime_file(
                    WINDOWS_PYTHON_DLL, required=True
                ),
                "_ctypes.pyd": ctypes_path,
            }
            ffi_sources: dict[str, Path] = {}
            for root in search_roots:
                if not root.is_dir():
                    continue
                for pattern in WINDOWS_LIBFFI_DLL_PATTERNS:
                    for source in sorted(root.glob(pattern)):
                        if source.is_file():
                            ffi_sources.setdefault(source.name.casefold(), source)
            if not ffi_sources:
                raise RuntimeError(
                    "Builder Python bootstrap dependency is missing: Windows libffi DLL"
                )
            for source in ffi_sources.values():
                sources[source.name] = source
            for optional_name in (
                "python3.dll",
                "zlib.dll",
                "vcruntime140.dll",
                "vcruntime140_1.dll",
            ):
                source = find_runtime_file(optional_name, required=False)
                if source is not None:
                    sources[optional_name] = source
        elif sys.platform.startswith("linux"):
            python_candidates = sorted(
                candidate
                for root in environment_roots
                for pattern in (
                    f"{LINUX_PYTHON_SHARED_PREFIX}.1.0",
                    LINUX_PYTHON_SHARED_PREFIX,
                )
                for candidate in (root / "lib").glob(pattern)
                if candidate.is_file()
            )
            if not python_candidates:
                raise RuntimeError(
                    "Builder Python bootstrap dependency is missing: "
                    f"{LINUX_PYTHON_SHARED_PREFIX}"
                )
            python_library = python_candidates[0]
            ffi_candidates = sorted(
                candidate
                for root in environment_roots
                for candidate in (root / "lib").glob("libffi.so*")
                if candidate.is_file()
            )
            if ctypes_path is not None and not ffi_candidates:
                raise RuntimeError(
                    "Builder Python bootstrap dependency is missing: libffi.so"
                )
            sources = {
                python_library.name: python_library,
            }
            if ctypes_path is not None:
                sources[ctypes_path.name] = ctypes_path
            for source in ffi_candidates:
                sources.setdefault(source.name, source)
        else:
            raise RuntimeError(
                f"PlayerHost bootstrap is unsupported on {sys.platform}"
            )

        # Module-mode no longer embeds stdlib extensions. The complete pure
        # stdlib is imported before Runtime.inxrt can be mounted, so its native
        # extension closure belongs to the same Bootstrap.inxrt phase.
        for name, source in stdlib_extension_module_sources().items():
            sources.setdefault(name, source)

        dependency_patterns = (
            (
                "bzip2*.dll",
                "libbz2*.dll",
                "libcrypto-*.dll",
                "libexpat*.dll",
                "ffi-*.dll",
                "ffi.dll",
                "libffi*.dll",
                "liblzma*.dll",
                "libssl-*.dll",
                "sqlite3.dll",
                "zlib*.dll",
            )
            if sys.platform == "win32"
            else (
                "libbz2.so*",
                "libcrypto.so*",
                "libexpat.so*",
                "libffi.so*",
                "liblzma.so*",
                "libssl.so*",
                "libsqlite3.so*",
                "libz.so*",
            )
        )
        for root in search_roots:
            if not root.is_dir():
                continue
            for pattern in dependency_patterns:
                for source in root.glob(pattern):
                    if source.is_file():
                        sources.setdefault(source.name, source)
        encodings_root = Path(next(iter(encodings_spec.submodule_search_locations)))
        return sources, encodings_root

    def _inject_python_bootstrap_runtime(self, dist_dir: str) -> None:
        """Stage an explicit isolated-CPython bootstrap closure for PlayerHost."""

        sources, encodings_root = self._python_bootstrap_runtime_sources()
        dist_root = Path(dist_dir)
        for name, source in sources.items():
            shutil.copy2(source, dist_root / name)
            # This closure has one owner: Bootstrap.inxrt, not Infernux/lib.
            (dist_root / "Infernux" / "lib" / name).unlink(missing_ok=True)
        manifest_path = dist_root / BOOTSTRAP_NATIVE_MANIFEST_FILENAME
        temporary_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{os.getpid()}.tmp"
        )
        try:
            temporary_manifest.write_text(
                json.dumps(
                    {
                        "$schema": BOOTSTRAP_NATIVE_MANIFEST_SCHEMA,
                        "files": sorted(sources),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_manifest, manifest_path)
        finally:
            try:
                temporary_manifest.unlink()
            except FileNotFoundError:
                pass

        target_encodings = dist_root / "stdlib" / "encodings"
        target_encodings.mkdir(parents=True, exist_ok=True)
        for source in sorted(encodings_root.glob("*.py")):
            destination = target_encodings / f"{source.stem}.pyc"
            py_compile.compile(
                str(source),
                cfile=str(destination),
                dfile=f"<infernux-stdlib>/encodings/{source.name}",
                doraise=True,
                optimize=2,
            )
        if not (target_encodings / "__init__.pyc").is_file():
            raise RuntimeError("Unable to stage the Python encodings bootstrap package")

    def _inject_python_runtime_stdlib(
        self,
        dist_dir: str,
        *,
        source_root: str | os.PathLike[str] | None = None,
    ) -> None:
        """Stage the isolated, source-less CPython bootstrap standard library."""

        stdlib_root = (
            Path(source_root)
            if source_root is not None
            else Path(resolved_path(os.__file__)).parent
        )
        # Nuitka's module loader imports parts of the ordinary standard library
        # before _InfernuxPlayer's module body can mount Runtime.inxrt.  Keep the
        # complete pure-Python closure in Bootstrap.inxrt's existing stdlib path.
        destination_root = Path(dist_dir) / "stdlib"
        shutil.rmtree(destination_root, ignore_errors=True)
        destination_root.mkdir(parents=True, exist_ok=False)

        excluded_directories = {
            "__pycache__",
            "ensurepip",
            "idlelib",
            "lib2to3",
            "site-packages",
            "test",
            "tests",
            "tkinter",
            "turtledemo",
            "venv",
        }
        for source_path in sorted(stdlib_root.rglob("*")):
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(stdlib_root)
            if any(part.casefold() in excluded_directories for part in relative.parts):
                continue
            # Native extensions and their DLL closure are staged separately.
            # Documentation, migration grammars, pickles and other development
            # data are not part of the Player's isolated interpreter closure.
            if source_path.suffix.casefold() != ".py":
                continue

            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            py_compile.compile(
                str(source_path),
                cfile=str(destination.with_suffix(".pyc")),
                dfile=f"<infernux-stdlib>/{relative.as_posix()}",
                doraise=True,
                optimize=2,
            )

        if not (destination_root / "__future__.pyc").is_file():
            raise RuntimeError("Player Bootstrap standard library is incomplete")
        if any(destination_root.rglob("*.py")):
            raise RuntimeError("Player Bootstrap standard library contains source files")

    def _inject_engine_python_runtime(self, dist_dir: str) -> None:
        """Stage Infernux as source-less runtime bytecode.

        ``_InfernuxPlayer`` must remain a small bootstrap extension. Following
        the complete engine package into that extension has proven unsafe in
        Nuitka module mode: its loader can recurse before the module body gets
        a chance to mount Runtime.inxrt. The engine package therefore lives in
        Runtime.inxrt and is imported by ordinary CPython after extraction.
        """

        import Infernux

        source_root = Path(resolved_path(Infernux.__file__)).parent
        destination_root = Path(dist_dir) / "Infernux"
        destination_root.mkdir(parents=True, exist_ok=True)

        excluded_roots = {
            "__pycache__",
            "_runtime_modules",
            "_runtime_packs",
            "test",
        }
        excluded_suffixes = {
            ".bak",
            ".exp",
            ".lib",
            ".meta",
            ".pdb",
            ".pyc",
            ".pyi",
            ".pyo",
        }

        copied_bytecode = 0
        for source_path in sorted(source_root.rglob("*")):
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(source_root)
            if any(part.casefold() in excluded_roots for part in relative.parts):
                continue
            relative_posix = relative.as_posix()
            if (
                relative.parts
                and relative.parts[0].casefold() == "lib"
                and source_path.suffix.casefold() != ".py"
            ):
                # Keep Infernux.lib as an importable Python package, while its
                # native closure remains exclusively owned by _inject_native_libs.
                continue
            if relative_posix.startswith("engine/locales/"):
                continue
            if self._is_player_runtime_excluded_source(relative_posix):
                continue
            if source_path.suffix.casefold() in excluded_suffixes:
                continue

            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source_path.suffix.casefold() == ".py":
                destination = destination.with_suffix(".pyc")
                try:
                    py_compile.compile(
                        str(source_path),
                        cfile=str(destination),
                        dfile=f"<infernux-runtime>/{relative_posix}",
                        doraise=True,
                        optimize=2,
                    )
                except (OSError, py_compile.PyCompileError) as exc:
                    raise RuntimeError(
                        f"Unable to compile Player runtime module '{relative_posix}'"
                    ) from exc
                copied_bytecode += 1
            else:
                shutil.copy2(source_path, destination)

        if not (destination_root / "__init__.pyc").is_file():
            raise RuntimeError("Player Runtime is missing Infernux/__init__.pyc")
        if copied_bytecode == 0 or any(destination_root.rglob("*.py")):
            raise RuntimeError("Player Runtime engine package is not source-less")

    @classmethod
    def _is_player_runtime_excluded_source(cls, relative_posix: str) -> bool:
        """Return whether an Infernux package source is authoring/build-only."""
        relative = str(relative_posix or "").replace("\\", "/")
        if (
            relative in cls._PLAYER_POST_BUILD_ONLY_FILES
            or relative in cls._PLAYER_RUNTIME_EXCLUDED_FILES
        ):
            return True
        if not relative.endswith(".py"):
            return False

        module = "Infernux." + relative[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        if module in cls._PLAYER_EDITOR_ONLY_MODULES:
            return True
        if any(
            module == prefix or module.startswith(prefix + ".")
            for prefix in cls._PLAYER_EDITOR_ONLY_PACKAGE_PREFIXES
        ):
            return True
        if (
            module.startswith("Infernux.engine.ui.")
            and module not in cls._PLAYER_RUNTIME_UI_MODULES
        ):
            return True
        return False

    # ------------------------------------------------------------------
    # Inject raw JIT packages
    # ------------------------------------------------------------------

    def _inject_jit_packages(
        self,
        dist_dir: str,
        packages: Optional[List[str]] = None,
    ):
        """Copy raw site-packages into the Nuitka dist for JIT-dependent packages.

        Numba requires Python bytecode at runtime for LLVM JIT compilation.
        Nuitka's C compilation removes bytecode, so these packages must be
        copied as-is from the builder environment's site-packages.
        """
        selected_packages = sorted(set(
            self.raw_copy_packages if packages is None else packages
        ))
        if not selected_packages:
            return

        import time as _time
        _t0 = _time.perf_counter()

        # Discover the builder site-packages directory containing every raw
        # package. In Conda, getsitepackages() also returns the environment
        # root, which is not itself the package directory.
        result = _run_python(
            self._builder_python,
            ["-c",
             "import site, os, json; "
             "print(json.dumps(site.getsitepackages()))"],
        )
        import json as _json
        candidates = _json.loads(result.stdout.strip())
        site_packages = ""
        for cand in reversed(candidates):
            if os.path.isdir(cand) and all(
                os.path.isdir(os.path.join(cand, pkg))
                for pkg in selected_packages
            ):
                site_packages = cand
                break
        if not site_packages:
            raise RuntimeError(
                "Builder environment does not contain every raw runtime package "
                f"{selected_packages!r} in one site-packages directory; "
                f"searched {candidates!r}"
            )

        Debug.log_internal(
            f"  site-packages resolved: {site_packages}  "
            f"({_time.perf_counter() - _t0:.2f}s)"
        )

        copied: list[str] = []
        dist_root = Path(dist_dir)

        for pkg in selected_packages:
            src = os.path.join(site_packages, pkg)
            if not os.path.isdir(src):
                raise RuntimeError(
                    f"Raw runtime package '{pkg}' disappeared from "
                    f"'{site_packages}' during Player construction"
                )

            _pkg_t0 = _time.perf_counter()
            dst = dist_root / pkg
            if dst.exists():
                if sys.platform == "win32":
                    subprocess.run(
                        ["cmd", "/c", "rd", "/s", "/q", str(dst)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    shutil.rmtree(dst)

            # Use robocopy on Windows for significantly faster bulk copy.
            # /XD skips directories that are never needed at runtime,
            # cutting the copy volume substantially (especially numpy).
            # Package-specific strip dirs come from _JIT_STRIP_DIRS.
            xd_dirs = ["__pycache__", "tests", "test"]
            xd_dirs.extend(self._JIT_STRIP_DIRS.get(pkg, []))
            if sys.platform == "win32":
                rc = subprocess.call(
                    ["robocopy", src, str(dst), "/E",
                     "/MT:16", "/R:1", "/W:1", "/XJ",
                     "/COPY:DAT", "/DCOPY:DAT",
                     "/XD", *xd_dirs,
                     "/XF", "*.pdb", "*.lib", "*.a",
                     "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                if rc >= 8:
                    raise RuntimeError(
                        f"Unable to copy raw runtime package '{pkg}' with "
                        f"robocopy (exit {rc})"
                    )
            else:
                shutil.copytree(src, dst)
                # Strip on non-Windows too
                for strip_dir in xd_dirs:
                    strip_path = dst / strip_dir
                    if strip_path.is_dir():
                        shutil.rmtree(strip_path)

            # Raw JIT packages are runtime dependencies, not authoring
            # sources. Keep the bytecode Python needs for lazy imports, but
            # never ship the package's plaintext .py files. Source-less adjacent
            # .pyc files are intentional: Python can import them without a
            # source file, whereas __pycache__ entries alone cannot be
            # discovered by SourcelessFileLoader.
            self._compile_raw_python_sources(dst)

            elapsed = _time.perf_counter() - _pkg_t0
            copied.append(f"{pkg} ({elapsed:.1f}s)")

            # Copy companion .libs directory if it exists (e.g. numpy.libs
            # contains OpenBLAS DLLs that numpy's C extensions need).
            libs_name = f"{pkg}.libs"
            libs_src = os.path.join(site_packages, libs_name)
            if os.path.isdir(libs_src):
                libs_dst = dist_root / libs_name
                if libs_dst.exists():
                    if sys.platform == "win32":
                        subprocess.run(
                            ["cmd", "/c", "rd", "/s", "/q", str(libs_dst)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        shutil.rmtree(libs_dst)
                if sys.platform == "win32":
                    rc = subprocess.call(
                        ["robocopy", libs_src, str(libs_dst), "/E",
                         "/MT:16", "/R:1", "/W:1", "/XJ",
                         "/COPY:DAT", "/DCOPY:DAT",
                         "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x08000000,
                    )
                    if rc >= 8:
                        raise RuntimeError(
                            f"Unable to copy raw runtime companion '{libs_name}' "
                            f"with robocopy (exit {rc})"
                        )
                else:
                    shutil.copytree(libs_src, libs_dst)
                copied.append(f"{libs_name}")

        if copied:
            Debug.log_internal(
                f"  JIT package injection: {', '.join(copied)}  "
                f"(total {_time.perf_counter() - _t0:.1f}s)"
            )

    @staticmethod
    def _compile_raw_python_sources(package_root: str | os.PathLike[str]) -> None:
        """Replace raw package sources with importable adjacent bytecode."""

        root = Path(package_root)
        if not root.is_dir():
            return

        for source_path in sorted(root.rglob("*.py")):
            bytecode_path = Path(str(source_path) + "c")
            try:
                py_compile.compile(
                    str(source_path),
                    cfile=str(bytecode_path),
                    doraise=True,
                    optimize=2,
                )
                source_path.unlink()
            except (OSError, py_compile.PyCompileError) as exc:
                raise RuntimeError(
                    "Unable to compile raw runtime package source "
                    f"'{source_path}' to bytecode before packaging"
                ) from exc

    # ------------------------------------------------------------------
    # UTF-8 application manifest (Windows)
    # ------------------------------------------------------------------

    # Complete manifest that tells Windows to use UTF-8 as the process's
    # ANSI code page (Windows 10 1903+).  Without this, any path
    # containing non-ASCII characters (e.g. Chinese usernames) causes
    # the C++ engine to fail with "No mapping for the Unicode character
    # exists in the target multi-byte code page".
    _UTF8_MANIFEST = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        b'<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">\r\n'
        b'  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">\r\n'
        b'    <security>\r\n'
        b'      <requestedPrivileges>\r\n'
        b'        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>\r\n'
        b'      </requestedPrivileges>\r\n'
        b'    </security>\r\n'
        b'  </trustInfo>\r\n'
        b'  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">\r\n'
        b'    <application>\r\n'
        b'      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>\r\n'
        b'    </application>\r\n'
        b'  </compatibility>\r\n'
        b'  <application xmlns="urn:schemas-microsoft-com:asm.v3">\r\n'
        b'    <windowsSettings>\r\n'
        b'      <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>\r\n'
        b'      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>\r\n'
        b'    </windowsSettings>\r\n'
        b'  </application>\r\n'
        b'</assembly>\r\n'
    )

    def _embed_utf8_manifest(self, dist_dir: str):
        """Embed an application manifest with UTF-8 active code page.

        Uses the Win32 resource-update API so no external tools (mt.exe,
        rc.exe) are required.  Replaces the default Nuitka manifest.
        """
        import ctypes
        from ctypes import wintypes

        exe_path = os.path.join(dist_dir, self.output_filename)
        if not os.path.isfile(exe_path):
            Debug.log_warning(
                f"Cannot embed manifest: EXE not found at {exe_path}"
            )
            return

        k32 = ctypes.windll.kernel32

        # --- open for resource update --------------------------------
        k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
        k32.BeginUpdateResourceW.restype = wintypes.HANDLE
        h = k32.BeginUpdateResourceW(exe_path, False)
        if not h:
            Debug.log_warning(
                f"BeginUpdateResource failed (error {ctypes.GetLastError()})"
            )
            return

        # RT_MANIFEST = 24, CREATEPROCESS_MANIFEST_RESOURCE_ID = 1
        RT_MANIFEST = 24
        MANIFEST_ID = 1
        data = self._UTF8_MANIFEST

        k32.UpdateResourceW.argtypes = [
            wintypes.HANDLE,   # hUpdate
            wintypes.LPVOID,   # lpType  (MAKEINTRESOURCE)
            wintypes.LPVOID,   # lpName  (MAKEINTRESOURCE)
            wintypes.WORD,     # wLanguage
            ctypes.c_char_p,   # lpData
            wintypes.DWORD,    # cb
        ]
        k32.UpdateResourceW.restype = wintypes.BOOL

        ok = k32.UpdateResourceW(h, RT_MANIFEST, MANIFEST_ID, 0, data, len(data))
        if not ok:
            Debug.log_warning(
                f"UpdateResource failed (error {ctypes.GetLastError()})"
            )
            k32.EndUpdateResourceW(h, True)  # discard changes
            return

        k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
        k32.EndUpdateResourceW.restype = wintypes.BOOL
        k32.EndUpdateResourceW(h, False)

        Debug.log_internal("Embedded UTF-8 active-code-page manifest")

    # ------------------------------------------------------------------
    # Code signing (reduces antivirus false positives)
    # ------------------------------------------------------------------

    def _sign_executable(self, dist_dir: str):
        """Sign the built EXE with a self-signed certificate.

        Unsigned executables — especially those compiled with MinGW —
        are far more likely to trigger antivirus false positives because
        they lack an Authenticode signature.  This method creates a
        self-signed code-signing certificate (cached per-machine) and
        applies it to the output EXE using PowerShell's
        ``Set-AuthenticodeSignature``.

        A self-signed certificate won't prevent SmartScreen warnings
        (that requires a purchased EV certificate), but it does help
        with heuristic-based AV scanners that penalise unsigned binaries.
        """
        exe_path = os.path.join(dist_dir, self.output_filename)
        if not os.path.isfile(exe_path):
            return

        # Use PowerShell to: (1) find or create a self-signed code signing
        # cert in CurrentUser\\My, (2) sign the EXE.
        ps_script = r'''
$ErrorActionPreference = "Stop"
$certName = "Infernux Build Signing"
$securityModulePath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
if (-not (Test-Path -LiteralPath $securityModulePath)) {
    Write-Output "UNSUPPORTED:security-module"
    exit 0
}

Import-Module $securityModulePath -ErrorAction Stop

if (-not (Get-PSDrive -Name Cert -ErrorAction SilentlyContinue)) {
    Write-Output "UNSUPPORTED:cert-drive"
    exit 0
}

$setAuth = Get-Command Set-AuthenticodeSignature -ErrorAction SilentlyContinue
if (-not $setAuth) {
    Write-Output "UNSUPPORTED:set-authenticode"
    exit 0
}

$newSelfSigned = Get-Command New-SelfSignedCertificate -ErrorAction SilentlyContinue

$cert = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object {
            $_.Subject -eq "CN=$certName" -and
            $_.NotAfter -gt (Get-Date) -and
            $_.HasPrivateKey -and
            ($_.EnhancedKeyUsageList | Where-Object { $_.FriendlyName -eq "Code Signing" })
        } |
        Select-Object -First 1

if (-not $cert) {
    if (-not $newSelfSigned) {
        Write-Output "UNSUPPORTED:new-self-signed-certificate"
        exit 0
    }

    $cert = New-SelfSignedCertificate `
        -Subject "CN=$certName" `
        -Type CodeSigningCert `
        -CertStoreLocation Cert:\CurrentUser\My `
        -NotAfter (Get-Date).AddYears(5)
}

$result = Set-AuthenticodeSignature -FilePath $EXE_PATH -Certificate $cert -HashAlgorithm SHA256
if ($null -eq $result) {
    Write-Output "UNSUPPORTED:no-result"
    exit 0
}

Write-Output ("STATUS:" + [string]$result.Status)
if ($result.StatusMessage) {
    Write-Output ("MESSAGE:" + [string]$result.StatusMessage)
}
if ($result.SignerCertificate) {
    Write-Output ("SIGNER:" + [string]$result.SignerCertificate.Thumbprint)
}
Write-Output ("CERT:" + [string]$cert.Thumbprint)
'''
        ps_script = ps_script.replace("$EXE_PATH", f'"{exe_path}"')
        try:
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
            windows_powershell_root = os.path.join(
                system_root, "System32", "WindowsPowerShell", "v1.0"
            )
            powershell_exe = os.path.join(windows_powershell_root, "powershell.exe")
            signing_env = os.environ.copy()
            # The Editor may itself be launched from PowerShell 7. Its inherited
            # PSModulePath can make Windows PowerShell load incompatible type
            # data before Microsoft.PowerShell.Security, producing duplicate
            # ObjectSecurity members. Signing needs only the inbox modules.
            signing_env["PSModulePath"] = os.path.join(windows_powershell_root, "Modules")
            r = subprocess.run(
                [powershell_exe, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True, text=True, timeout=60, env=signing_env,
            )
            stdout_lines = [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]
            stderr_text = (r.stderr or "").strip()

            unsupported = next((line for line in stdout_lines if line.startswith("UNSUPPORTED:")), "")
            status_line = next((line for line in stdout_lines if line.startswith("STATUS:")), "")
            message_line = next((line for line in stdout_lines if line.startswith("MESSAGE:")), "")
            signer_line = next((line for line in stdout_lines if line.startswith("SIGNER:")), "")
            cert_line = next((line for line in stdout_lines if line.startswith("CERT:")), "")

            if r.returncode != 0:
                details = stderr_text or "\n".join(stdout_lines)
                Debug.log_warning(f"Code signing failed: {details}")
                return

            if unsupported:
                reason = unsupported.split(":", 1)[1]
                Debug.log_internal(f"Code signing skipped: unsupported PowerShell signing environment ({reason})")
                return

            status = status_line.split(":", 1)[1] if status_line else ""
            message = message_line.split(":", 1)[1] if message_line else ""
            signer_thumbprint = signer_line.split(":", 1)[1].strip().upper() if signer_line else ""
            cert_thumbprint = cert_line.split(":", 1)[1].strip().upper() if cert_line else ""

            if status == "Valid":
                Debug.log_internal("Signed EXE with self-signed certificate")
            elif (
                status in {"UnknownError", "NotTrusted"}
                and signer_thumbprint
                and signer_thumbprint == cert_thumbprint
            ):
                # A self-signed certificate is expected to terminate at an
                # untrusted root unless the user explicitly installs it into a
                # trust store. The Authenticode signature is nevertheless
                # present and cryptographically associated with our cert.
                Debug.log_internal(
                    "Signed EXE with self-signed certificate; the local root is not trusted"
                )
            else:
                details = message or stderr_text or "\n".join(stdout_lines)
                Debug.log_warning(f"Code signing returned: {status or details}")
        except Exception as exc:
            Debug.log_warning(f"Code signing skipped: {exc}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_build_artifacts(self):
        """Remove Nuitka's intermediate .build directory from staging.

        Deletion runs in a background daemon thread so the caller doesn't
        block.  On Windows we use ``rd /s /q`` which is dramatically faster
        than Python's shutil.rmtree (native NTFS batch-delete vs per-file
        unlink syscalls).
        """
        dirs_to_remove: list[str] = []
        build_dir = os.path.join(self._staging_dir, "boot.build")
        if os.path.isdir(build_dir):
            dirs_to_remove.append(build_dir)
        safe_tmp = os.path.join(self._staging_dir, "_tmp")
        if os.path.isdir(safe_tmp):
            dirs_to_remove.append(safe_tmp)
        # Remove the copied boot script (tiny file, do it synchronously)
        staged_script = os.path.join(self._staging_dir, "boot.py")
        if os.path.isfile(staged_script):
            os.remove(staged_script)

        if dirs_to_remove:
            def _bg_remove(paths: list[str]):
                for p in paths:
                    if sys.platform == "win32":
                        # rd /s /q is 5-10x faster than shutil.rmtree on NTFS
                        subprocess.run(
                            ["cmd", "/c", "rd", "/s", "/q", p],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        shutil.rmtree(p, ignore_errors=True)

            t = threading.Thread(target=_bg_remove, args=(dirs_to_remove,), daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Icon conversion
    # ------------------------------------------------------------------

    def _ensure_ico(self, icon_path: str) -> Optional[str]:
        """Return a .ico path, converting from PNG/JPG if needed.

        Nuitka's ``--windows-icon-from-ico`` requires a real .ico file.
        If the source is already .ico, return it as-is.  Otherwise
        convert via Pillow (no ImageMagick needed).
        """
        ext = os.path.splitext(icon_path)[1].lower()
        if ext == ".ico":
            return icon_path

        try:
            _ensure_python_packages(self._builder_python, "PIL")
            from PIL import Image
        except ImportError:
            Debug.log_warning(
                "Pillow not installed — skipping icon embedding.  "
                "Install with: pip install Pillow"
            )
            return None

        ico_path = os.path.join(self._staging_dir, "icon.ico")
        try:
            img = Image.open(icon_path)
            # Standard Windows icon sizes
            sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
            img.save(ico_path, format="ICO", sizes=sizes)
            Debug.log_internal(f"Converted {os.path.basename(icon_path)} → icon.ico")
            return ico_path
        except Exception as exc:
            Debug.log_warning(f"Icon conversion failed: {exc}")
            return None
