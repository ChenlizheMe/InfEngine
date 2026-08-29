from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from private_python_runtime import (
    extract_runtime_archive,
    is_current_private_runtime_root,
    remove_legacy_installer_artifacts,
    runtime_archive_for_machine,
    verify_runtime_archive,
)
from python_runtime_catalog import DEFAULT_PYTHON_RUNTIME
from runtime_requirements import RUNTIME_PROFILE_VERSION, runtime_modules, runtime_packages
import logging

_RUNTIME_PACKAGES = runtime_packages()
_RUNTIME_MODULES = runtime_modules()
_RUNTIME_PROFILE_FILENAME = ".infernux-runtime-profile.json"
_TARGET_RUNTIME = DEFAULT_PYTHON_RUNTIME
_TARGET_VERSION = _TARGET_RUNTIME.series
_TARGET_DIRECTORY = _TARGET_RUNTIME.directory_name


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
    if sys.platform == "darwin":
        return [f"lib{_TARGET_RUNTIME.unix_library_stem}.dylib", "libpython3.dylib"]
    return [f"{_TARGET_RUNTIME.windows_library_stem}.lib", "python3.lib"]


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


def _is_target_python(python_exe: str) -> bool:
    if not python_exe or not os.path.isfile(python_exe):
        return False

    completed = _run([
        python_exe,
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    ])
    return completed.returncode == 0 and (completed.stdout or "").strip() == _TARGET_VERSION


def _find_python_in_root(root: str) -> str | None:
    if not root or not os.path.isdir(root):
        return None

    if sys.platform == "win32":
        direct_candidates = [
            os.path.join(root, "python.exe"),
            os.path.join(root, "Python.exe"),
            os.path.join(root, f"Python{_TARGET_RUNTIME.major}{_TARGET_RUNTIME.minor}", "python.exe"),
        ]
    else:
        direct_candidates = [
            os.path.join(root, "bin", f"python{_TARGET_VERSION}"),
            os.path.join(root, "bin", "python3"),
            os.path.join(root, "bin", "python"),
            os.path.join(root, f"python{_TARGET_VERSION}"),
            os.path.join(root, "python3"),
        ]
    for candidate in direct_candidates:
        if _is_target_python(candidate):
            return candidate

    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    for current_root, _dirs, files in os.walk(root):
        for filename in files:
            if sys.platform == "win32":
                if filename.lower() != "python.exe":
                    continue
            elif filename not in (f"python{_TARGET_VERSION}", "python3", "python"):
                continue
            candidate = os.path.join(current_root, filename)
            if _is_target_python(candidate):
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
    archive = runtime_archive_for_machine(runtime=_TARGET_RUNTIME)
    return {
        "profile_version": RUNTIME_PROFILE_VERSION,
        "source": "runtime-cache",
        "python_archive": archive.name,
        "python_archive_sha256": archive.sha256,
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
        f"{_TARGET_RUNTIME.windows_library_stem}.pdb",
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


def _archive_cache_path(cache_root: str) -> str:
    return os.path.join(
        cache_root,
        runtime_archive_for_machine(runtime=_TARGET_RUNTIME).name,
    )


def _extract_full_runtime(dest_root: str, *, archive_cache_root: str | None = None) -> None:
    parent = os.path.dirname(dest_root)
    os.makedirs(parent, exist_ok=True)
    cache_root = os.path.abspath(archive_cache_root) if archive_cache_root else parent
    os.makedirs(cache_root, exist_ok=True)
    archive = runtime_archive_for_machine(runtime=_TARGET_RUNTIME)
    archive_path = _archive_cache_path(cache_root)
    if os.path.isfile(archive_path):
        try:
            verify_runtime_archive(archive_path, archive.sha256)
        except RuntimeError:
            os.remove(archive_path)
    if not os.path.isfile(archive_path):
        print(f"Downloading isolated Python runtime archive: {archive.url}")
        temporary = archive_path + ".tmp"
        if os.path.isfile(temporary):
            os.remove(temporary)
        _download_file(archive.url, temporary)
        verify_runtime_archive(temporary, archive.sha256)
        os.replace(temporary, archive_path)

    extract_runtime_archive(
        archive_path,
        dest_root,
        expected_sha256=archive.sha256,
        runtime=_TARGET_RUNTIME,
    )

    python_exe = _find_python_in_root(dest_root)
    if not python_exe or not _is_target_python(python_exe) or _is_embedded_root(dest_root):
        raise SystemExit(
            f"Python {_TARGET_VERSION} archive extraction completed, but a valid private runtime was not found afterwards."
        )


def _stage_clean_runtime_fallback(dest_root: str) -> None:
    os.makedirs(_BOOTSTRAP_ROOT, exist_ok=True)
    bootstrap_dir = tempfile.mkdtemp(prefix="bundle-", dir=_BOOTSTRAP_ROOT)
    bootstrap_root = os.path.join(bootstrap_dir, _TARGET_DIRECTORY)
    archive_cache_root = os.path.dirname(dest_root)

    try:
        _extract_full_runtime(bootstrap_root, archive_cache_root=archive_cache_root)
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
        and _is_target_python(python_exe)
        and not _is_embedded_root(root)
        and _has_dev_support(root)
        and _has_modules(python_exe, *_RUNTIME_MODULES)
    )


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
            candidates.append(os.path.join(local_app_data, "InfernuxHub", "runtime", _TARGET_DIRECTORY, "python.exe"))
            candidates.append(os.path.join(local_app_data, "Programs", "Python", f"Python{_TARGET_RUNTIME.major}{_TARGET_RUNTIME.minor}", "python.exe"))
        if program_files:
            candidates.append(os.path.join(program_files, f"Python{_TARGET_RUNTIME.major}{_TARGET_RUNTIME.minor}", "python.exe"))

        py_launcher = _run(["py", f"-{_TARGET_VERSION}", "-c", "import sys; print(sys.executable)"])
        if py_launcher.returncode == 0:
            value = (py_launcher.stdout or "").strip().splitlines()
            if value:
                candidates.append(value[-1].strip())
    elif sys.platform == "darwin":
        # macOS: Homebrew, python.org framework, and common paths
        candidates.extend([
            f"/usr/local/bin/python{_TARGET_VERSION}",
            f"/opt/homebrew/bin/python{_TARGET_VERSION}",
            os.path.expanduser(f"~/Library/Frameworks/Python.framework/Versions/{_TARGET_VERSION}/bin/python{_TARGET_VERSION}"),
            f"/Library/Frameworks/Python.framework/Versions/{_TARGET_VERSION}/bin/python{_TARGET_VERSION}",
        ])
    else:
        # Linux
        candidates.extend([
            f"/usr/bin/python{_TARGET_VERSION}",
            f"/usr/local/bin/python{_TARGET_VERSION}",
        ])

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
    if sys.version_info[:2] != (_TARGET_RUNTIME.major, _TARGET_RUNTIME.minor):
        current = os.path.normcase(os.path.abspath(sys.executable))
        for candidate in _candidate_python_paths():
            if not _is_target_python(candidate):
                continue
            if os.path.normcase(os.path.abspath(candidate)) == current:
                continue
            completed = subprocess.run(
                [candidate, __file__, *sys.argv[1:]],
                env=_child_env(),
            )
            return completed.returncode
        raise SystemExit(
            f"This staging script must run under Python {_TARGET_VERSION}, but got {sys.version.split()[0]} from {sys.executable}."
        )

    parser = argparse.ArgumentParser()
    parser.add_argument("--dest-root", required=True)
    args = parser.parse_args()

    dest_root = os.path.abspath(args.dest_root)
    bundle_path = _runtime_bundle_path(dest_root)
    remove_legacy_installer_artifacts(os.path.dirname(dest_root))

    existing = _find_python_in_root(dest_root)
    if existing and _is_target_python(existing):
        if (
            _is_usable_full_runtime(dest_root)
            and is_current_private_runtime_root(dest_root, runtime=_TARGET_RUNTIME)
            and _profile_matches(dest_root)
            and os.path.isfile(bundle_path)
        ):
            print(f"Bundled private Python {_TARGET_VERSION} already present: {existing}")
            return 0

        _remove_tree(dest_root)

    parent = os.path.dirname(dest_root)
    os.makedirs(parent, exist_ok=True)

    profile_path = _runtime_profile_path(dest_root)
    if os.path.isfile(bundle_path):
        os.remove(bundle_path)
    if os.path.isfile(profile_path):
        os.remove(profile_path)

    if os.path.isdir(dest_root):
        _remove_tree(dest_root)

    print(f"Runtime cache missing; generating a new bundled Python {_TARGET_VERSION} package...")
    _stage_clean_runtime_fallback(dest_root)
    _write_runtime_profile(dest_root)
    _create_runtime_bundle(dest_root)
    staged = _find_python_in_root(dest_root)
    if staged and _is_usable_full_runtime(dest_root):
        print(f"Bundled Python {_TARGET_VERSION} staged from an isolated archive: {dest_root}")
        return 0

    raise SystemExit(
        f"Unable to prepare a usable bundled Python {_TARGET_VERSION} runtime under out/package/runtime/{_TARGET_DIRECTORY}."
    )


if __name__ == "__main__":
    raise SystemExit(main())
