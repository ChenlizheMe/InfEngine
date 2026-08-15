from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

try:
    import winreg
except ImportError:
    winreg = None

from runtime_requirements import RUNTIME_PROFILE_VERSION, runtime_modules, runtime_packages
import logging

_RUNTIME_PACKAGES = runtime_packages()
_RUNTIME_MODULES = runtime_modules()
_RUNTIME_PROFILE_FILENAME = ".infernux-runtime-profile.json"


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, **(extra or {})}
    if sys.platform == "win32":
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
_RUNTIME_PRUNE_DIR_NAMES = {"__pycache__", ".pytest_cache", "test", "tests"}
_RUNTIME_PRUNE_FILE_SUFFIXES = (".pyc", ".pyo")
if sys.platform == "win32":
    _BOOTSTRAP_ROOT = os.path.join(os.environ.get("SystemDrive", "C:"), "_InxRuntime")
else:
    _BOOTSTRAP_ROOT = os.path.join(os.path.expanduser("~"), ".infernux", "_InxRuntime")


def _runtime_lib_names() -> list[str]:
    version = f"{sys.version_info.major}{sys.version_info.minor}"
    if sys.platform == "darwin":
        return [f"libpython{version}.dylib", "libpython3.dylib"]
    return [f"python{version}.lib", "python3.lib"]


def _run(args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "env": _child_env({"PYTHONDONTWRITEBYTECODE": "1"}),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000
    return subprocess.run(args, **kwargs)


def _runtime_installer_info_for_machine() -> tuple[str, str]:
    machine = (platform.machine() if sys.platform != "win32" else os.environ.get("PROCESSOR_ARCHITECTURE", "")).lower()
    if sys.platform == "darwin":
        return (
            "python-3.12.8-macos11.pkg",
            "https://www.python.org/ftp/python/3.12.8/python-3.12.8-macos11.pkg",
        )
    if sys.platform.startswith("linux"):
        # python.org publishes no Linux binaries; use the relocatable
        # python-build-standalone "install_only" archive (Astral).
        arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
        name = f"cpython-3.12.8+20241206-{arch}-unknown-linux-gnu-install_only.tar.gz"
        return (
            name,
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"20241206/cpython-3.12.8%2B20241206-{arch}-unknown-linux-gnu-install_only.tar.gz",
        )
    if machine in {"amd64", "x86_64"}:
        return (
            "python-3.12.8-amd64.exe",
            "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe",
        )
    if machine in {"arm64", "aarch64"}:
        return (
            "python-3.12.8-arm64.exe",
            "https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe",
        )
    if machine in {"x86", "i386", "i686"}:
        return (
            "python-3.12.8.exe",
            "https://www.python.org/ftp/python/3.12.8/python-3.12.8.exe",
        )
    return (
        "python-3.12.8-amd64.exe",
        "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe",
    )


def _is_python312(python_exe: str) -> bool:
    if not python_exe or not os.path.isfile(python_exe):
        return False

    completed = _run([
        python_exe,
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    ])
    return completed.returncode == 0 and (completed.stdout or "").strip() == "3.12"


def _find_python_in_root(root: str) -> str | None:
    if not root or not os.path.isdir(root):
        return None

    if sys.platform == "win32":
        direct_candidates = [
            os.path.join(root, "python.exe"),
            os.path.join(root, "Python.exe"),
            os.path.join(root, "Python312", "python.exe"),
        ]
    else:
        direct_candidates = [
            os.path.join(root, "bin", "python3.12"),
            os.path.join(root, "bin", "python3"),
            os.path.join(root, "bin", "python"),
            os.path.join(root, "python3.12"),
            os.path.join(root, "python3"),
        ]
    for candidate in direct_candidates:
        if _is_python312(candidate):
            return candidate

    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    for current_root, _dirs, files in os.walk(root):
        for filename in files:
            if sys.platform == "win32":
                if filename.lower() != "python.exe":
                    continue
            elif filename not in ("python3.12", "python3", "python"):
                continue
            candidate = os.path.join(current_root, filename)
            if _is_python312(candidate):
                return candidate
    return None


def _pth_files(root: str) -> list[str]:
    if not root or not os.path.isdir(root):
        return []
    return [
        os.path.join(root, name)
        for name in os.listdir(root)
        if name.lower().endswith("._pth") and os.path.isfile(os.path.join(root, name))
    ]


def _is_embedded_root(root: str) -> bool:
    return bool(_pth_files(root))


def _has_dev_support(root: str) -> bool:
    include_dir = os.path.join(root, "include")
    if sys.platform == "darwin":
        libs_dir = os.path.join(root, "lib")
    else:
        libs_dir = os.path.join(root, "libs")
    if not os.path.isfile(os.path.join(include_dir, "Python.h")):
        return False
    return any(os.path.isfile(os.path.join(libs_dir, name)) for name in _runtime_lib_names())


def _fast_copy_threads() -> int:
    raw_value = os.environ.get("INFERNUX_FAST_COPY_THREADS", "16")
    try:
        return max(1, min(128, int(raw_value)))
    except ValueError:
        return 16


def _remove_tree(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "rd", "/s", "/q", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        if completed.returncode == 0 and not os.path.exists(path):
            return
    shutil.rmtree(path, ignore_errors=True)


def _runtime_artifact_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lower_name = name.lower()
        if lower_name in _RUNTIME_PRUNE_DIR_NAMES or lower_name.endswith(_RUNTIME_PRUNE_FILE_SUFFIXES):
            ignored.add(name)
    return ignored


def _copy_tree(src: str, dest: str) -> None:
    if sys.platform == "win32" and shutil.which("robocopy") is not None:
        os.makedirs(dest, exist_ok=True)
        completed = subprocess.run(
            [
                "robocopy", src, dest,
                "/E",
                f"/MT:{_fast_copy_threads()}",
                "/R:1", "/W:1",
                "/XJ",
                "/COPY:DAT", "/DCOPY:DAT",
                "/XD", *_RUNTIME_PRUNE_DIR_NAMES,
                "/XF", "*.pyc", "*.pyo",
                "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0x08000000,
        )
        if completed.returncode < 8:
            return
        logging.getLogger(__name__).warning(
            "robocopy failed while staging runtime (%s -> %s, exit %s): %s",
            src,
            dest,
            completed.returncode,
            (completed.stderr or "").strip(),
        )
        _remove_tree(dest)

    shutil.copytree(src, dest, ignore=_runtime_artifact_ignore)


def _has_modules(python_exe: str, *module_names: str) -> bool:
    checks = " and ".join(
        [f"importlib.util.find_spec('{module_name}') is not None" for module_name in module_names]
    ) or "1"
    completed = _run(
        [python_exe, "-c", f"import importlib.util; print(int({checks}))"],
        timeout=60,
    )
    return completed.returncode == 0 and (completed.stdout or "").strip() == "1"


def _runtime_profile_path(dest_root: str) -> str:
    return os.path.join(os.path.dirname(dest_root), _RUNTIME_PROFILE_FILENAME)


def _runtime_profile_payload() -> dict[str, object]:
    installer_name, _installer_url = _runtime_installer_info_for_machine()
    return {
        "profile_version": RUNTIME_PROFILE_VERSION,
        "source": "runtime-cache",
        "python_installer": installer_name,
        "packages": list(_RUNTIME_PACKAGES),
    }


def _profile_matches(dest_root: str) -> bool:
    profile_path = _runtime_profile_path(dest_root)
    if not os.path.isfile(profile_path):
        return False

    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, json.JSONDecodeError) as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        return False

    return profile == _runtime_profile_payload()


def _write_runtime_profile(dest_root: str) -> None:
    profile_path = _runtime_profile_path(dest_root)
    tmp_path = profile_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(_runtime_profile_payload(), f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, profile_path)


def _prune_runtime_root(dest_root: str) -> None:
    for rel_path in (
        "python.pdb",
        "python312.pdb",
        "pythonw.pdb",
        os.path.join("Lib", "test"),
        os.path.join("Lib", "idlelib"),
        "Tools",
    ):
        abs_path = os.path.join(dest_root, rel_path)
        if os.path.isdir(abs_path):
            _remove_tree(abs_path)
        elif os.path.isfile(abs_path):
            os.remove(abs_path)

    for current_root, dirs, files in os.walk(dest_root, topdown=True):
        for dirname in list(dirs):
            if dirname.lower() in _RUNTIME_PRUNE_DIR_NAMES:
                _remove_tree(os.path.join(current_root, dirname))
                dirs.remove(dirname)
        for filename in files:
            if filename.lower().endswith(_RUNTIME_PRUNE_FILE_SUFFIXES):
                try:
                    os.remove(os.path.join(current_root, filename))
                except OSError as _exc:
                    logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)


def _ensure_pip(python_exe: str) -> None:
    completed = _run([python_exe, "-m", "pip", "--version"], timeout=60)
    if completed.returncode == 0:
        return

    completed = _run([python_exe, "-m", "ensurepip", "--upgrade"], timeout=600)
    if completed.returncode != 0:
        raise SystemExit(
            "Failed to bootstrap pip into the staged Python runtime.\n"
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )


def _ensure_builder_packages(root: str) -> None:
    target_python = _find_python_in_root(root)
    if not target_python:
        raise SystemExit(f"No python.exe found after preparing runtime: {root}")

    _ensure_pip(target_python)

    completed = _run([
        target_python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--prefer-binary",
        "--no-compile",
        "--no-cache-dir",
        "--upgrade",
        *_RUNTIME_PACKAGES,
    ], timeout=1800)
    if completed.returncode != 0:
        raise SystemExit(
            "Failed to prepare Python builder packages.\n"
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )

    if not _has_modules(target_python, *_RUNTIME_MODULES):
        raise SystemExit(
            "Python runtime was staged, but required builder packages are not importable.\n"
            f"Required modules: {', '.join(_RUNTIME_MODULES)}"
        )


def _download_file(url: str, dest: str) -> None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Infernux-Stage-Runtime/1.0")
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _runtime_bundle_path(dest_root: str) -> str:
    return os.path.join(os.path.dirname(dest_root), "runtime_bundle.zip")


def _create_runtime_bundle(dest_root: str) -> None:
    bundle_path = _runtime_bundle_path(dest_root)
    tmp_bundle = bundle_path + ".tmp"
    if os.path.isfile(tmp_bundle):
        os.remove(tmp_bundle)

    with zipfile.ZipFile(tmp_bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, _dirs, files in os.walk(dest_root):
            rel_dir = os.path.relpath(root, os.path.dirname(dest_root))
            for filename in files:
                source_path = os.path.join(root, filename)
                archive_name = os.path.join(rel_dir, filename)
                zf.write(source_path, archive_name)

    os.replace(tmp_bundle, bundle_path)


def _installer_cache_path(cache_root: str) -> str:
    installer_name, _installer_url = _runtime_installer_info_for_machine()
    return os.path.join(cache_root, installer_name)


def _install_full_runtime(dest_root: str, *, installer_cache_root: str | None = None) -> None:
    parent = os.path.dirname(dest_root)
    os.makedirs(parent, exist_ok=True)
    cache_root = os.path.abspath(installer_cache_root) if installer_cache_root else parent
    os.makedirs(cache_root, exist_ok=True)
    installer_path = _installer_cache_path(cache_root)
    if not os.path.isfile(installer_path):
        _installer_name, installer_url = _runtime_installer_info_for_machine()
        print(f"Downloading official Python installer: {installer_url}")
        _download_file(installer_url, installer_path)

    _remove_tree(dest_root)

    if sys.platform == "darwin":
        # macOS: use the python.org .pkg installer
        completed = _run([
            "installer", "-pkg", installer_path, "-target", "CurrentUserHomeDirectory",
        ], timeout=3600)
        if completed.returncode != 0:
            raise SystemExit(
                "Failed to install official Python 3.12 on macOS.\n"
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )
        # Link the framework python into dest_root
        framework_candidates = [
            os.path.expanduser("~/Library/Frameworks/Python.framework/Versions/3.12"),
            "/Library/Frameworks/Python.framework/Versions/3.12",
        ]
        for fw_root in framework_candidates:
            fw_python = os.path.join(fw_root, "bin", "python3.12")
            if os.path.isfile(fw_python) and _is_python312(fw_python):
                shutil.copytree(fw_root, dest_root, symlinks=True)
                return
        raise SystemExit(
            "Python 3.12 .pkg installation completed, but the framework was not found afterwards."
        )

    if sys.platform.startswith("linux"):
        # python-build-standalone "install_only" tarball: extract and relocate.
        import tarfile

        extract_root = dest_root + ".extract"
        _remove_tree(extract_root)
        os.makedirs(extract_root, exist_ok=True)
        with tarfile.open(installer_path, "r:gz") as tar:
            tar.extractall(extract_root)  # noqa: S202 — trusted release artifact
        # Archive layout: python/{bin,lib,include,...}
        inner = os.path.join(extract_root, "python")
        if not os.path.isdir(inner):
            raise SystemExit(
                "Unexpected python-build-standalone archive layout: missing 'python/' root."
            )
        shutil.move(inner, dest_root)
        _remove_tree(extract_root)
        staged = os.path.join(dest_root, "bin", "python3.12")
        if not _is_python312(staged):
            raise SystemExit("Staged Linux Python runtime failed the 3.12 verification.")
        return

    if sys.platform != "win32":
        raise SystemExit("Bundled full Python staging is only supported on Windows, macOS, and Linux.")

    install_log_path = os.path.join(os.path.dirname(installer_path), "python-install.log")
    completed = _run([
        installer_path,
        "/quiet",
        "/log", install_log_path,
        "InstallAllUsers=0",
        f"TargetDir={dest_root}",
        "AssociateFiles=0",
        "PrependPath=0",
        "Shortcuts=0",
        "CompileAll=0",
        "Include_test=0",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "Include_pip=1",
        "Include_dev=1",
    ], timeout=3600)
    if completed.returncode != 0:
        raise SystemExit(
            "Failed to install official Python 3.12 into the bundled runtime directory.\n"
            f"Check {install_log_path}.\n"
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )

    python_exe = _find_python_in_root(dest_root)
    if not python_exe or not _is_python312(python_exe) or _is_embedded_root(dest_root):
        raise SystemExit(
            "Python 3.12 installation completed, but a valid full python.exe was not found afterwards."
        )


def _stage_clean_runtime_fallback(dest_root: str) -> None:
    os.makedirs(_BOOTSTRAP_ROOT, exist_ok=True)
    bootstrap_dir = tempfile.mkdtemp(prefix="bundle-", dir=_BOOTSTRAP_ROOT)
    bootstrap_root = os.path.join(bootstrap_dir, "python312")
    installer_cache_root = os.path.dirname(dest_root)

    try:
        _install_full_runtime(bootstrap_root, installer_cache_root=installer_cache_root)
        _ensure_builder_packages(bootstrap_root)
        _prune_runtime_root(bootstrap_root)

        _remove_tree(dest_root)
        _copy_tree(bootstrap_root, dest_root)
    finally:
        _remove_tree(bootstrap_dir)


def _is_usable_full_runtime(root: str) -> bool:
    python_exe = _find_python_in_root(root)
    return bool(
        python_exe
        and _is_python312(python_exe)
        and not _is_embedded_root(root)
        and _has_dev_support(root)
        and _has_modules(python_exe, *_RUNTIME_MODULES)
    )


def _prepare_existing_runtime_cache(dest_root: str) -> bool:
    python_exe = _find_python_in_root(dest_root)
    if not python_exe or not _is_python312(python_exe) or _is_embedded_root(dest_root):
        return False
    if not _has_dev_support(dest_root):
        return False

    _ensure_builder_packages(dest_root)
    _prune_runtime_root(dest_root)
    _write_runtime_profile(dest_root)
    _create_runtime_bundle(dest_root)
    return _is_usable_full_runtime(dest_root)


def _registry_candidates() -> list[str]:
    if winreg is None:
        return []

    candidates: list[str] = []
    keys = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Python\PythonCore\3.12\InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\PythonCore\3.12\InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Python\PythonCore\3.12\InstallPath"),
    ]

    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                install_path, _ = winreg.QueryValueEx(key, None)
        except OSError as _exc:
            logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
            continue

        if install_path:
            candidates.append(os.path.join(install_path, "python.exe"))
    return candidates


def _candidate_python_paths() -> list[str]:
    candidates: list[str] = []

    explicit_root = os.environ.get("INFERNUX_BUNDLED_PYTHON_ROOT")
    explicit_exe = os.environ.get("INFERNUX_BUNDLED_PYTHON_EXE")
    if explicit_exe:
        candidates.append(explicit_exe)
    if explicit_root:
        found = _find_python_in_root(explicit_root)
        if found:
            candidates.append(found)

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")

        if local_app_data:
            candidates.append(os.path.join(local_app_data, "InfernuxHub", "runtime", "python312", "python.exe"))
            candidates.append(os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"))
        if program_files:
            candidates.append(os.path.join(program_files, "Python312", "python.exe"))

        py_launcher = _run(["py", "-3.12", "-c", "import sys; print(sys.executable)"])
        if py_launcher.returncode == 0:
            value = (py_launcher.stdout or "").strip().splitlines()
            if value:
                candidates.append(value[-1].strip())
    elif sys.platform == "darwin":
        # macOS: Homebrew, python.org framework, and common paths
        candidates.extend([
            "/usr/local/bin/python3.12",
            "/opt/homebrew/bin/python3.12",
            os.path.expanduser("~/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"),
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
        ])
    else:
        # Linux
        candidates.extend([
            "/usr/bin/python3.12",
            "/usr/local/bin/python3.12",
        ])

    candidates.extend(_registry_candidates())

    current_python = sys.executable
    if current_python:
        candidates.append(current_python)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return deduped


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        current = os.path.normcase(os.path.abspath(sys.executable))
        for candidate in _candidate_python_paths():
            if not _is_python312(candidate):
                continue
            if os.path.normcase(os.path.abspath(candidate)) == current:
                continue
            completed = subprocess.run(
                [candidate, __file__, *sys.argv[1:]],
                env=_child_env(),
            )
            return completed.returncode
        raise SystemExit(
            f"This staging script must run under Python 3.12, but got {sys.version.split()[0]} from {sys.executable}."
        )

    parser = argparse.ArgumentParser()
    parser.add_argument("--dest-root", required=True)
    args = parser.parse_args()

    dest_root = os.path.abspath(args.dest_root)
    bundle_path = _runtime_bundle_path(dest_root)

    existing = _find_python_in_root(dest_root)
    if existing and _is_python312(existing):
        if _is_usable_full_runtime(dest_root):
            if not os.path.isfile(bundle_path):
                _write_runtime_profile(dest_root)
                _create_runtime_bundle(dest_root)
            print(f"Bundled minimal Python 3.12 already present: {existing}")
            return 0

        if _prepare_existing_runtime_cache(dest_root):
            print(f"Prepared bundled runtime cache from existing runtime folder: {dest_root}")
            return 0

        if os.path.isfile(bundle_path):
            print(f"Runtime folder is incomplete; using cached bundled runtime package: {bundle_path}")
            return 0

        _remove_tree(dest_root)

    if os.path.isfile(bundle_path):
        print(f"Using cached bundled runtime package: {bundle_path}")
        return 0

    parent = os.path.dirname(dest_root)
    os.makedirs(parent, exist_ok=True)

    profile_path = _runtime_profile_path(dest_root)
    if os.path.isfile(bundle_path):
        os.remove(bundle_path)
    if os.path.isfile(profile_path):
        os.remove(profile_path)

    if os.path.isdir(dest_root) and _prepare_existing_runtime_cache(dest_root):
        print(f"Prepared bundled runtime cache from runtime folder: {dest_root}")
        return 0

    print("Runtime cache missing; generating a new bundled Python 3.12 package...")
    _stage_clean_runtime_fallback(dest_root)
    _write_runtime_profile(dest_root)
    _create_runtime_bundle(dest_root)
    staged = _find_python_in_root(dest_root)
    if staged and _is_usable_full_runtime(dest_root):
        print(f"Bundled minimal Python 3.12 staged from official installer: {dest_root}")
        return 0

    raise SystemExit(
        "Unable to prepare a usable bundled Python 3.12 runtime under packaging/runtime/python312."
    )


if __name__ == "__main__":
    raise SystemExit(main())