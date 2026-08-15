from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from hub_utils import get_bundle_dir, get_hub_data_dir, is_frozen, merge_child_env_utf8
from runtime_requirements import runtime_modules, runtime_packages
import logging


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_RUNTIME_ROOT = Path.home() / ".infernux" / "runtime"
_PUBLIC_RUNTIME_ROOT = Path("C:/Users/Public/InfernuxHub") if sys.platform == "win32" else _RUNTIME_ROOT
_RUNTIME_PACKAGES = runtime_packages()
_REQUIRED_RUNTIME_MODULES = runtime_modules()
_RUNTIME_COPY_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", "test", "tests"}
_RUNTIME_COPY_EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")


def _runtime_lib_names() -> list[str]:
    if sys.platform == "darwin":
        return ["libpython3.12.dylib", "libpython3.dylib"]
    return ["python312.lib", "python3.lib"]


def _runtime_bundle_name() -> str:
    return "runtime_bundle.zip"


class PythonRuntimeError(RuntimeError):
    pass


def _default_runtime_dir() -> str:
    if is_frozen():
        return str(_PUBLIC_RUNTIME_ROOT)

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return os.path.join(local_app_data, "InfernuxHub", "runtime")
    return str(_RUNTIME_ROOT)


def _emit_status(callback: Optional[Callable[[str], None]], message: str) -> None:
    if callback is not None:
        callback(message)


def _runtime_installer_info_for_machine() -> tuple[str, str]:
    machine = (platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    if sys.platform == "darwin":
        # macOS universal2 installer from python.org
        return (
            "python-3.12.8-macos11.pkg",
            "https://www.python.org/ftp/python/3.12.8/python-3.12.8-macos11.pkg",
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


def _run_command(args: list[str], *, timeout: int, raise_on_error: bool = False) -> subprocess.CompletedProcess:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": merge_child_env_utf8({"PYTHONDONTWRITEBYTECODE": "1"}),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _NO_WINDOW

    try:
        return subprocess.run(args, timeout=timeout, check=raise_on_error, **kwargs)
    except FileNotFoundError as exc:
        if not raise_on_error:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))
        raise PythonRuntimeError(str(exc)) from exc
    except OSError as exc:
        if not raise_on_error:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))
        raise PythonRuntimeError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise PythonRuntimeError(f"Command timed out after {timeout} seconds.\n{subprocess.list2cmdline(args)}") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise PythonRuntimeError(
            f"Command failed with exit code {exc.returncode}.\n{subprocess.list2cmdline(args)}\n{details}"
        ) from exc


def _find_python_in_root(root: str) -> Optional[str]:
    if not root or not os.path.isdir(root):
        return None

    direct_candidates = [
        os.path.join(root, "python.exe"),
        os.path.join(root, "Python.exe"),
        os.path.join(root, "Python312", "python.exe"),
        os.path.join(root, "bin", "python"),
    ]
    for candidate in direct_candidates:
        if os.path.isfile(candidate):
            return candidate

    for current_root, _dirs, files in os.walk(root):
        for filename in files:
            if sys.platform == "win32":
                if filename.lower() != "python.exe":
                    continue
            elif filename != "python":
                continue
            return os.path.join(current_root, filename)
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


def _enable_site_for_embedded_runtime(root: str) -> None:
    required_lines = ["python312.zip", ".", "Lib", "Lib/site-packages"]
    for pth_path in _pth_files(root):
        with open(pth_path, "r", encoding="utf-8") as f:
            raw_lines = [line.rstrip("\r\n") for line in f]

        output: list[str] = []
        seen: set[str] = set()
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                output.append(line)
                continue
            if stripped == "import site":
                continue
            if stripped not in seen:
                output.append(stripped)
                seen.add(stripped)

        for item in required_lines:
            if item not in seen:
                output.append(item)
                seen.add(item)
        output.append("import site")

        normalized_raw = [line.rstrip("\r\n") for line in raw_lines]
        if output == normalized_raw:
            continue

        with open(pth_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(output).rstrip() + "\n")


def _embedded_runtime_has_site_enabled(root: str) -> bool:
    required_lines = {"python312.zip", ".", "Lib", "Lib/site-packages", "import site"}
    pth_paths = _pth_files(root)
    if not pth_paths:
        return True

    for pth_path in pth_paths:
        try:
            with open(pth_path, "r", encoding="utf-8") as f:
                lines = {line.strip() for line in f if line.strip() and not line.strip().startswith("#")}
        except OSError as _exc:
            logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
            return False
        if not required_lines.issubset(lines):
            return False
    return True


def _is_python312(python_exe: str) -> bool:
    if not python_exe or not os.path.isfile(python_exe):
        return False

    completed = _run_command(
        [python_exe, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        timeout=20,
        raise_on_error=False,
    )
    return completed.returncode == 0 and (completed.stdout or "").strip() == "3.12"


def _site_packages_root(runtime_root: str) -> str:
    if sys.platform == "darwin":
        path = os.path.join(runtime_root, "lib", "python3.12", "site-packages")
    else:
        path = os.path.join(runtime_root, "Lib", "site-packages")
    os.makedirs(path, exist_ok=True)
    return path


def _has_build_support(root: str) -> bool:
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
            creationflags=_NO_WINDOW,
            env=merge_child_env_utf8(),
        )
        if completed.returncode == 0 and not os.path.exists(path):
            return
    shutil.rmtree(path, ignore_errors=True)


def _runtime_artifact_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lower_name = name.lower()
        if lower_name in _RUNTIME_COPY_EXCLUDED_DIRS or lower_name.endswith(_RUNTIME_COPY_EXCLUDED_FILE_SUFFIXES):
            ignored.add(name)
    return ignored


def _copy_tree_fast(src: str, dest: str, *, exclude_runtime_artifacts: bool = False) -> bool:
    if sys.platform != "win32" or not os.path.isdir(src) or shutil.which("robocopy") is None:
        return False

    os.makedirs(dest, exist_ok=True)
    args = [
        "robocopy", src, dest,
        "/E",
        f"/MT:{_fast_copy_threads()}",
        "/R:1", "/W:1",
        "/XJ",
        "/COPY:DAT", "/DCOPY:DAT",
    ]
    if exclude_runtime_artifacts:
        args.extend(["/XD", *_RUNTIME_COPY_EXCLUDED_DIRS, "/XF", "*.pyc", "*.pyo"])
    args.extend(["/NFL", "/NDL", "/NJH", "/NJS", "/NP"])
    completed = subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        env=merge_child_env_utf8(),
    )
    if completed.returncode < 8:
        return True

    logging.getLogger(__name__).warning(
        "robocopy failed while copying Python runtime (%s -> %s, exit %s): %s",
        src,
        dest,
        completed.returncode,
        (completed.stderr or "").strip(),
    )
    _remove_tree(dest)
    return False


def _copy_tree(src: str, dest: str) -> None:
    _remove_tree(dest)
    if not _copy_tree_fast(src, dest):
        shutil.copytree(src, dest)


def _copy_project_runtime_tree(src: str, dest: str) -> None:
    if not _copy_tree_fast(src, dest, exclude_runtime_artifacts=True):
        shutil.copytree(src, dest, ignore=_runtime_artifact_ignore)


def _copy_runtime_payload(src_root: str, dest_root: str, *, overwrite: bool) -> None:
    os.makedirs(dest_root, exist_ok=True)
    for name in os.listdir(src_root):
        source_path = os.path.join(src_root, name)
        target_path = os.path.join(dest_root, name)
        if os.path.isdir(source_path):
            if overwrite:
                _remove_tree(target_path)
            if os.path.exists(target_path):
                continue
            if not _copy_tree_fast(source_path, target_path):
                shutil.copytree(source_path, target_path)
        else:
            if not overwrite and os.path.exists(target_path):
                continue
            shutil.copy2(source_path, target_path)


def _copy_directory_contents(src_root: str, dest_root: str) -> None:
    os.makedirs(dest_root, exist_ok=True)
    for name in os.listdir(src_root):
        source_path = os.path.join(src_root, name)
        target_path = os.path.join(dest_root, name)
        if os.path.isdir(source_path):
            _remove_tree(target_path)
            if not _copy_tree_fast(source_path, target_path):
                shutil.copytree(source_path, target_path)
        else:
            shutil.copy2(source_path, target_path)


def _copy_build_support(src_root: str, dest_root: str) -> bool:
    include_src = os.path.join(src_root, "include")
    libs_src = os.path.join(src_root, "libs")
    if not os.path.isfile(os.path.join(include_src, "Python.h")):
        return False

    copied_lib = False
    os.makedirs(os.path.join(dest_root, "libs"), exist_ok=True)
    for name in _runtime_lib_names():
        source_path = os.path.join(libs_src, name)
        if not os.path.isfile(source_path):
            continue
        shutil.copy2(source_path, os.path.join(dest_root, "libs", name))
        copied_lib = True

    if not copied_lib:
        return False

    _copy_tree(include_src, os.path.join(dest_root, "include"))
    return True


def _download_file(url: str, dest: str, *, user_agent: str, timeout: int = 120) -> None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", user_agent)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


class PythonRuntimeManager:
    def __init__(self, runtime_dir: Optional[str] = None, bundle_runtime_dir: Optional[str] = None) -> None:
        _RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self._runtime_dir = os.path.abspath(runtime_dir) if runtime_dir else _default_runtime_dir()
        self._bundle_runtime_dir = os.path.abspath(bundle_runtime_dir) if bundle_runtime_dir else ""

    def installed_runtime_dir(self) -> str:
        return self._runtime_dir

    def bundled_runtime_dirs(self) -> list[str]:
        dirs = []
        if self._bundle_runtime_dir:
            dirs.append(self._bundle_runtime_dir)
        dirs.extend([
            os.path.join(get_bundle_dir(), "InfernuxHubData", "runtime"),
            os.path.join(get_bundle_dir(), "runtime"),
            os.path.join(get_bundle_dir(), "_internal", "InfernuxHubData", "runtime"),
            os.path.join(get_bundle_dir(), "_internal", "runtime"),
            os.path.join(get_bundle_dir(), "payload", "InfernuxHubData", "runtime"),
            os.path.join(get_bundle_dir(), "payload", "runtime"),
            os.path.join(get_bundle_dir(), "payload", "_internal", "InfernuxHubData", "runtime"),
            os.path.join(get_bundle_dir(), "payload", "_internal", "runtime"),
        ])
        result: list[str] = []
        seen: set[str] = set()
        for path in dirs:
            norm = os.path.normcase(os.path.abspath(path))
            if norm in seen:
                continue
            seen.add(norm)
            result.append(path)
        return result

    def private_runtime_root(self) -> str:
        return os.path.join(self.installed_runtime_dir(), "python312")

    def private_runtime_python(self) -> str:
        if sys.platform == "win32":
            return os.path.join(self.private_runtime_root(), "python.exe")
        return os.path.join(self.private_runtime_root(), "bin", "python")

    def runtime_installer_path(self) -> str:
        installer_name, _installer_url = _runtime_installer_info_for_machine()
        return os.path.join(self.installed_runtime_dir(), installer_name)

    def bundled_installer_paths(self) -> list[str]:
        installer_name, _installer_url = _runtime_installer_info_for_machine()
        return [os.path.join(path, installer_name) for path in self.bundled_runtime_dirs()]

    def bundled_runtime_bundle_paths(self) -> list[str]:
        bundle_name = _runtime_bundle_name()
        return [os.path.join(path, bundle_name) for path in self.bundled_runtime_dirs()]

    def has_runtime(self) -> bool:
        return bool(self.get_runtime_path())

    def get_runtime_path(self) -> Optional[str]:
        roots = [self.private_runtime_root()]
        for root in roots:
            candidate = _find_python_in_root(root)
            if candidate and _is_python312(candidate) and not _is_embedded_root(root):
                return candidate
        return None

    def ensure_runtime(
        self,
        *,
        on_status: Optional[Callable[[str], None]] = None,
        allow_frozen_repair: bool = False,
    ) -> str:
        python_exe = self.get_runtime_path()
        if not python_exe:
            python_exe = self._provision_managed_runtime(on_status=on_status)
        else:
            runtime_root = os.path.dirname(python_exe)
            has_build_support = _has_build_support(runtime_root)
            has_required_modules = self._has_modules(python_exe, *_REQUIRED_RUNTIME_MODULES)
            if is_frozen() and not allow_frozen_repair:
                if not has_build_support:
                    raise PythonRuntimeError(
                        "The installed managed Python 3.12 runtime is missing CPython build support files.\n"
                        "Please reinstall Infernux Hub so the runtime can be prepared during installation."
                    )
                if not has_required_modules:
                    raise PythonRuntimeError(
                        "The installed managed Python 3.12 runtime is missing required engine/build packages.\n"
                        "Please reinstall Infernux Hub so the runtime can be prepared during installation."
                    )
                return python_exe

            if allow_frozen_repair and is_frozen() and (not has_build_support or not has_required_modules):
                repaired_python = self._seed_runtime_from_bundle(overwrite=True, on_status=on_status)
                if not repaired_python:
                    repaired_python = self._install_runtime_to_root(
                        self.private_runtime_root(),
                        overwrite=True,
                        on_status=on_status,
                    )
                if repaired_python:
                    python_exe = repaired_python

            self._prepare_managed_runtime(python_exe, on_status=on_status)

        return python_exe

    def create_project_runtime(self, dest_path: str, *, on_status: Optional[Callable[[str], None]] = None) -> str:
        """Copy the full managed Python runtime to *dest_path* for a project.

        Each project owns its own complete Python copy so there is no need
        for virtual-environment indirection.
        """
        _emit_status(on_status, "Checking managed Python runtime...")
        self.ensure_runtime(allow_frozen_repair=is_frozen(), on_status=on_status)
        source = self.private_runtime_root()
        if not os.path.isdir(source):
            raise PythonRuntimeError(
                "The managed Python 3.12 runtime directory does not exist.\n"
                f"Expected at: {source}"
            )

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            if os.path.exists(dest_path):
                raise FileExistsError(dest_path)
            _emit_status(on_status, "Copying Python runtime into the project...")
            _copy_project_runtime_tree(source, dest_path)
        except OSError as exc:
            raise PythonRuntimeError(
                f"Failed to copy the managed Python runtime to {dest_path}.\n{exc}"
            ) from exc

        if sys.platform == "win32":
            project_python = os.path.join(dest_path, "python.exe")
        else:
            project_python = os.path.join(dest_path, "bin", "python")

        if not os.path.isfile(project_python):
            raise PythonRuntimeError(
                f"Runtime copy finished, but python.exe was not found at {project_python}."
            )
        return project_python

    def _provision_managed_runtime(self, *, on_status: Optional[Callable[[str], None]] = None) -> str:
        bundled_python = self._seed_runtime_from_bundle(on_status=on_status)
        if bundled_python:
            self._prepare_managed_runtime(bundled_python, on_status=on_status)
            return bundled_python

        python_exe = self._install_runtime_to_root(self.private_runtime_root(), overwrite=True, on_status=on_status)
        self._prepare_managed_runtime(python_exe, on_status=on_status)
        return python_exe

    def _seed_runtime_from_bundle(
        self,
        *,
        overwrite: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        target_root = self.installed_runtime_dir()
        target_python = self.private_runtime_python()

        for source_root in self.bundled_runtime_dirs():
            bundled_python = _find_python_in_root(os.path.join(source_root, "python312"))
            if not bundled_python or not _is_python312(bundled_python):
                continue
            if _is_embedded_root(os.path.dirname(bundled_python)):
                continue
            if os.path.normcase(os.path.abspath(source_root)) == os.path.normcase(os.path.abspath(target_root)):
                return bundled_python

            _emit_status(on_status, "Copying bundled Python 3.12 runtime...")
            _copy_runtime_payload(source_root, target_root, overwrite=overwrite)
            if os.path.isfile(target_python) and _is_python312(target_python) and not _is_embedded_root(os.path.dirname(target_python)):
                return target_python

        for bundle_path in self.bundled_runtime_bundle_paths():
            if not os.path.isfile(bundle_path):
                continue
            _emit_status(on_status, "Extracting bundled Python 3.12 runtime...")
            if overwrite:
                _remove_tree(target_root)
            os.makedirs(target_root, exist_ok=True)
            with zipfile.ZipFile(bundle_path, "r") as zf:
                zf.extractall(target_root)
            if os.path.isfile(target_python) and _is_python312(target_python) and not _is_embedded_root(os.path.dirname(target_python)):
                return target_python
        return None

    def _prepare_managed_runtime(self, python_exe: str, *, on_status: Optional[Callable[[str], None]] = None) -> None:
        runtime_root = os.path.dirname(python_exe)
        if _is_embedded_root(runtime_root):
            raise PythonRuntimeError(
                "Infernux Hub requires a full Python 3.12 runtime for Nuitka builds, but an embeddable runtime was detected."
            )
        self._ensure_runtime_build_support(runtime_root, on_status=on_status)
        self._ensure_pip(python_exe, on_status=on_status)
        self._ensure_runtime_packages(python_exe, on_status=on_status)

    def _ensure_runtime_installer(self, *, on_status: Optional[Callable[[str], None]] = None) -> str:
        installer_path = self.runtime_installer_path()
        _installer_name, installer_url = _runtime_installer_info_for_machine()
        os.makedirs(self.installed_runtime_dir(), exist_ok=True)

        if os.path.isfile(installer_path):
            return installer_path

        for candidate in self.bundled_installer_paths():
            if os.path.isfile(candidate):
                shutil.copy2(candidate, installer_path)
                return installer_path

        _emit_status(on_status, f"Downloading Python 3.12 installer for {platform.machine()}...")
        tmp_path = installer_path + ".tmp"
        try:
            _download_file(
                installer_url,
                tmp_path,
                user_agent="Infernux-Hub/1.0",
            )
        except urllib.error.URLError as exc:
            if "unknown url type: https" in str(exc).lower():
                raise PythonRuntimeError(
                    "Failed to download the Python 3.12 installer because HTTPS support is unavailable in the packaged Hub."
                ) from exc
            raise PythonRuntimeError(f"Failed to download the Python 3.12 installer.\n{exc}") from exc
        except OSError as exc:
            raise PythonRuntimeError(f"Failed to download the Python 3.12 installer.\n{exc}") from exc

        os.replace(tmp_path, installer_path)
        return installer_path

    def _install_runtime_to_root(
        self,
        runtime_root: str,
        *,
        overwrite: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        if overwrite:
            shutil.rmtree(runtime_root, ignore_errors=True)

        if sys.platform == "darwin":
            return self._install_runtime_to_root_macos(runtime_root, on_status=on_status)

        if sys.platform != "win32":
            raise PythonRuntimeError("Infernux Hub currently supports managed Python installation on Windows and macOS only.")

        installer_path = self._ensure_runtime_installer(on_status=on_status)
        install_log_path = os.path.join(os.path.dirname(installer_path), "python-install.log")
        os.makedirs(os.path.dirname(runtime_root), exist_ok=True)
        _emit_status(on_status, "Installing managed Python 3.12 runtime...")
        completed = _run_command(
            [
                installer_path,
                "/quiet",
                "/log", install_log_path,
                "InstallAllUsers=0",
                f"TargetDir={runtime_root}",
                "AssociateFiles=0",
                "PrependPath=0",
                "Shortcuts=0",
                "CompileAll=0",
                "Include_test=0",
                "Include_launcher=0",
                "InstallLauncherAllUsers=0",
                "Include_pip=1",
                "Include_dev=1",
            ],
            timeout=3600,
            raise_on_error=False,
        )
        if completed.returncode != 0:
            raise PythonRuntimeError(
                "Failed to install the managed Python 3.12 runtime.\n"
                f"Check {install_log_path}.\n"
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )

        python_exe = _find_python_in_root(runtime_root)
        if not python_exe or not _is_python312(python_exe) or _is_embedded_root(runtime_root):
            raise PythonRuntimeError(
                "Python 3.12 installation completed, but a valid full python.exe was not found afterwards."
            )
        return python_exe

    def _install_runtime_to_root_macos(
        self,
        runtime_root: str,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Install Python 3.12 on macOS using the python.org .pkg installer."""
        installer_path = self._ensure_runtime_installer(on_status=on_status)
        os.makedirs(runtime_root, exist_ok=True)
        _emit_status(on_status, "Installing managed Python 3.12 runtime (macOS)...")

        # Install the .pkg to a custom location via installer(8)
        completed = _run_command(
            ["installer", "-pkg", installer_path, "-target", "CurrentUserHomeDirectory"],
            timeout=3600,
            raise_on_error=False,
        )
        if completed.returncode != 0:
            raise PythonRuntimeError(
                "Failed to install the managed Python 3.12 runtime on macOS.\n"
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )

        # The python.org .pkg installs into /Library/Frameworks/Python.framework
        # or ~/Library/Frameworks/Python.framework for CurrentUserHomeDirectory.
        # Locate the installed python3.12 binary.
        framework_candidates = [
            os.path.expanduser("~/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"),
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
            "/usr/local/bin/python3.12",
        ]
        for candidate in framework_candidates:
            if os.path.isfile(candidate) and _is_python312(candidate):
                # Symlink the framework python into our runtime root
                dest_bin = os.path.join(runtime_root, "bin")
                os.makedirs(dest_bin, exist_ok=True)
                link_path = os.path.join(dest_bin, "python")
                if not os.path.exists(link_path):
                    os.symlink(candidate, link_path)
                return candidate

        raise PythonRuntimeError(
            "Python 3.12 .pkg installation completed, but python3.12 was not found afterwards."
        )

    def _get_pip_script_path(self, *, on_status: Optional[Callable[[str], None]] = None) -> str:
        target_path = os.path.join(self.installed_runtime_dir(), "get-pip.py")
        if os.path.isfile(target_path):
            return target_path

        for root in self.bundled_runtime_dirs():
            candidate = os.path.join(root, "get-pip.py")
            if os.path.isfile(candidate):
                shutil.copy2(candidate, target_path)
                return target_path

        _emit_status(on_status, "Downloading pip bootstrap...")
        try:
            _download_file(
                "https://bootstrap.pypa.io/get-pip.py",
                target_path,
                user_agent="Infernux-Hub/1.0",
            )
        except urllib.error.URLError as exc:
            raise PythonRuntimeError(f"Failed to download get-pip.py.\n{exc}") from exc
        except OSError as exc:
            raise PythonRuntimeError(f"Failed to download get-pip.py.\n{exc}") from exc
        return target_path

    def _bundled_python_roots(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for root in self.bundled_runtime_dirs():
            candidate = os.path.join(root, "python312")
            if not os.path.isdir(candidate):
                continue
            norm = os.path.normcase(os.path.abspath(candidate))
            if norm in seen:
                continue
            seen.add(norm)
            result.append(candidate)
        return result

    def _build_support_source_roots(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for root in self._bundled_python_roots():
            norm = os.path.normcase(os.path.abspath(root))
            if norm in seen:
                continue
            seen.add(norm)
            result.append(root)

        if not is_frozen():
            dev_root = sys.base_prefix or os.path.dirname(sys.executable)
            if dev_root and os.path.isdir(dev_root):
                norm = os.path.normcase(os.path.abspath(dev_root))
                if norm not in seen:
                    seen.add(norm)
                    result.append(dev_root)

        return result

    def _ensure_runtime_build_support(
        self,
        runtime_root: str,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        if _has_build_support(runtime_root):
            return

        _emit_status(on_status, "Preparing CPython build support files...")
        for source_root in self._build_support_source_roots():
            if os.path.normcase(os.path.abspath(source_root)) == os.path.normcase(os.path.abspath(runtime_root)):
                continue
            if _copy_build_support(source_root, runtime_root) and _has_build_support(runtime_root):
                return

        raise PythonRuntimeError(
            "Managed Python 3.12 is missing CPython build support files (Python.h / python312.lib).\n"
            "Reinstall Infernux Hub or rebuild the bundled runtime so these files are available."
        )

    def _ensure_pip(self, python_exe: str, *, on_status: Optional[Callable[[str], None]] = None) -> None:
        completed = _run_command([python_exe, "-m", "pip", "--version"], timeout=60, raise_on_error=False)
        if completed.returncode == 0:
            return

        get_pip_path = self._get_pip_script_path(on_status=on_status)
        _emit_status(on_status, "Installing pip into the managed Python runtime...")
        completed = _run_command(
            [python_exe, get_pip_path, "--no-warn-script-location"],
            timeout=1800,
            raise_on_error=False,
        )
        if completed.returncode != 0:
            raise PythonRuntimeError(
                "Failed to install pip into the managed Python runtime.\n"
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )

    def _ensure_runtime_packages(self, python_exe: str, *, on_status: Optional[Callable[[str], None]] = None) -> None:
        if self._has_modules(python_exe, *_REQUIRED_RUNTIME_MODULES):
            return

        _emit_status(on_status, "Installing managed runtime support packages...")
        args = [
            python_exe,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--prefer-binary",
            "--no-compile",
            "--no-cache-dir",
            "--upgrade",
            "--target",
            _site_packages_root(os.path.dirname(python_exe)),
        ]
        args.extend(_RUNTIME_PACKAGES)
        completed = _run_command(args, timeout=1800, raise_on_error=False)
        if completed.returncode != 0:
            raise PythonRuntimeError(
                "Failed to install support packages into the managed Python runtime.\n"
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )

        if not self._has_modules(python_exe, *_REQUIRED_RUNTIME_MODULES):
            raise PythonRuntimeError(
                "Managed Python runtime is still missing required support packages after installation."
            )

    def _has_modules(self, python_exe: str, *module_names: str) -> bool:
        checks = " and ".join(
            [f"importlib.util.find_spec('{module_name}') is not None" for module_name in module_names]
        )
        completed = _run_command(
            [python_exe, "-c", f"import importlib.util; print(int({checks}))"],
            timeout=30,
            raise_on_error=False,
        )
        return completed.returncode == 0 and (completed.stdout or "").strip() == "1"