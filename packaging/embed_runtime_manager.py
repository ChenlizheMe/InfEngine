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
from private_python_runtime import (
    extract_runtime_archive,
    is_current_private_runtime_root,
    runtime_archive_for_machine,
    verify_runtime_archive,
)
from python_runtime_catalog import (
    DEFAULT_PYTHON_RUNTIME,
    PythonRuntimeId,
    SUPPORTED_PYTHON_RUNTIMES,
    runtime_release,
)
from runtime_requirements import runtime_modules, runtime_packages
import logging


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_RUNTIME_ROOT = Path.home() / ".infernux" / "runtime"
_PUBLIC_RUNTIME_ROOT = Path("C:/Users/Public/InfernuxHub") if sys.platform == "win32" else _RUNTIME_ROOT
_RUNTIME_PACKAGES = runtime_packages()
_REQUIRED_RUNTIME_MODULES = runtime_modules()
_RUNTIME_COPY_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", "test", "tests"}
_RUNTIME_COPY_EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")


def _runtime_lib_names(runtime: str | PythonRuntimeId) -> list[str]:
    runtime_id = PythonRuntimeId.parse(runtime)
    if sys.platform == "darwin":
        return [f"lib{runtime_id.unix_library_stem}.dylib", "libpython3.dylib"]
    return [f"{runtime_id.windows_library_stem}.lib", "python3.lib"]


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


def _enable_site_for_embedded_runtime(
    root: str, runtime: str | PythonRuntimeId
) -> None:
    runtime_id = PythonRuntimeId.parse(runtime)
    required_lines = [
        f"{runtime_id.windows_library_stem}.zip",
        ".",
        "Lib",
        "Lib/site-packages",
    ]
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


def _embedded_runtime_has_site_enabled(
    root: str, runtime: str | PythonRuntimeId
) -> bool:
    runtime_id = PythonRuntimeId.parse(runtime)
    required_lines = {
        f"{runtime_id.windows_library_stem}.zip",
        ".",
        "Lib",
        "Lib/site-packages",
        "import site",
    }
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


def _is_python_version(
    python_exe: str, runtime: str | PythonRuntimeId
) -> bool:
    if not python_exe or not os.path.isfile(python_exe):
        return False

    runtime_id = PythonRuntimeId.parse(runtime)
    completed = _run_command(
        [python_exe, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        timeout=20,
        raise_on_error=False,
    )
    return (
        completed.returncode == 0
        and (completed.stdout or "").strip() == runtime_id.series
    )


def _site_packages_root(
    runtime_root: str, runtime: str | PythonRuntimeId
) -> str:
    runtime_id = PythonRuntimeId.parse(runtime)
    if sys.platform == "darwin":
        path = os.path.join(
            runtime_root, "lib", runtime_id.unix_library_stem, "site-packages"
        )
    else:
        path = os.path.join(runtime_root, "Lib", "site-packages")
    os.makedirs(path, exist_ok=True)
    return path


def _has_build_support(root: str, runtime: str | PythonRuntimeId) -> bool:
    include_dir = os.path.join(root, "include")
    if sys.platform == "darwin":
        libs_dir = os.path.join(root, "lib")
    else:
        libs_dir = os.path.join(root, "libs")
    if not os.path.isfile(os.path.join(include_dir, "Python.h")):
        return False
    return any(
        os.path.isfile(os.path.join(libs_dir, name))
        for name in _runtime_lib_names(runtime)
    )


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


def _copy_build_support(
    src_root: str, dest_root: str, runtime: str | PythonRuntimeId
) -> bool:
    include_src = os.path.join(src_root, "include")
    libs_src = os.path.join(src_root, "libs")
    if not os.path.isfile(os.path.join(include_src, "Python.h")):
        return False

    copied_lib = False
    os.makedirs(os.path.join(dest_root, "libs"), exist_ok=True)
    for name in _runtime_lib_names(runtime):
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
    def __init__(
        self,
        runtime_dir: Optional[str] = None,
        bundle_runtime_dir: Optional[str] = None,
        *,
        default_version: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
    ) -> None:
        _RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self._runtime_dir = os.path.abspath(runtime_dir) if runtime_dir else _default_runtime_dir()
        self._bundle_runtime_dir = os.path.abspath(bundle_runtime_dir) if bundle_runtime_dir else ""
        self._default_runtime = PythonRuntimeId.parse(default_version)
        runtime_release(self._default_runtime)

    @property
    def default_version(self) -> str:
        return self._default_runtime.series

    @staticmethod
    def supported_versions() -> list[str]:
        return [runtime.series for runtime in SUPPORTED_PYTHON_RUNTIMES]

    def _runtime_id(
        self, version: str | PythonRuntimeId | None = None
    ) -> PythonRuntimeId:
        runtime_id = self._default_runtime if version is None else PythonRuntimeId.parse(version)
        runtime_release(runtime_id)
        return runtime_id

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

    def private_runtime_root(
        self, version: str | PythonRuntimeId | None = None
    ) -> str:
        runtime_id = self._runtime_id(version)
        return os.path.join(self.installed_runtime_dir(), runtime_id.directory_name)

    def private_runtime_python(
        self, version: str | PythonRuntimeId | None = None
    ) -> str:
        runtime_root = self.private_runtime_root(version)
        if sys.platform == "win32":
            return os.path.join(runtime_root, "python.exe")
        return os.path.join(runtime_root, "bin", "python")

    def runtime_archive_path(
        self, version: str | PythonRuntimeId | None = None
    ) -> str:
        runtime_id = self._runtime_id(version)
        return os.path.join(
            self.installed_runtime_dir(),
            runtime_archive_for_machine(runtime=runtime_id).name,
        )

    def bundled_runtime_bundle_paths(self) -> list[str]:
        bundle_name = _runtime_bundle_name()
        return [os.path.join(path, bundle_name) for path in self.bundled_runtime_dirs()]

    def installed_versions(self) -> list[str]:
        return [
            runtime.series
            for runtime in SUPPORTED_PYTHON_RUNTIMES
            if self.get_runtime_path(runtime)
        ]

    def has_runtime(
        self, version: str | PythonRuntimeId | None = None
    ) -> bool:
        return bool(self.get_runtime_path(version))

    def get_runtime_path(
        self, version: str | PythonRuntimeId | None = None
    ) -> Optional[str]:
        runtime_id = self._runtime_id(version)
        roots = [self.private_runtime_root(runtime_id)]
        for root in roots:
            candidate = _find_python_in_root(root)
            if (
                candidate
                and _is_python_version(candidate, runtime_id)
                and not _is_embedded_root(root)
                and is_current_private_runtime_root(root, runtime=runtime_id)
            ):
                return candidate
        return None

    def ensure_runtime(
        self,
        *,
        version: str | PythonRuntimeId | None = None,
        on_status: Optional[Callable[[str], None]] = None,
        allow_frozen_repair: bool = False,
    ) -> str:
        runtime_id = self._runtime_id(version)
        python_exe = self.get_runtime_path(runtime_id)
        if not python_exe:
            python_exe = self._provision_managed_runtime(
                runtime_id, on_status=on_status
            )
        else:
            runtime_root = os.path.dirname(python_exe)
            has_build_support = _has_build_support(runtime_root, runtime_id)
            has_required_modules = self._has_modules(python_exe, *_REQUIRED_RUNTIME_MODULES)
            if is_frozen() and not allow_frozen_repair:
                if not has_build_support:
                    raise PythonRuntimeError(
                        f"The installed managed Python {runtime_id.series} runtime is missing CPython build support files.\n"
                        "Please reinstall Infernux Hub so the runtime can be prepared during installation."
                    )
                if not has_required_modules:
                    raise PythonRuntimeError(
                        f"The installed managed Python {runtime_id.series} runtime is missing required engine/build packages.\n"
                        "Please reinstall Infernux Hub so the runtime can be prepared during installation."
                    )
                return python_exe

            if allow_frozen_repair and is_frozen() and (not has_build_support or not has_required_modules):
                repaired_python = self._seed_runtime_from_bundle(
                    version=runtime_id,
                    overwrite=True,
                    on_status=on_status,
                )
                if not repaired_python:
                    repaired_python = self._extract_runtime_to_root(
                        self.private_runtime_root(runtime_id),
                        version=runtime_id,
                        overwrite=True,
                        on_status=on_status,
                    )
                if repaired_python:
                    python_exe = repaired_python

            self._prepare_managed_runtime(
                python_exe, runtime_id, on_status=on_status
            )

        return python_exe

    def create_project_runtime(
        self,
        dest_path: str,
        *,
        version: str | PythonRuntimeId | None = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Copy the full managed Python runtime to *dest_path* for a project.

        Each project owns its own complete Python copy so there is no need
        for virtual-environment indirection.
        """
        runtime_id = self._runtime_id(version)
        _emit_status(
            on_status, f"Checking managed Python {runtime_id.series} runtime..."
        )
        if not self.has_runtime(runtime_id):
            raise PythonRuntimeError(
                f"Python {runtime_id.series} is not installed in Infernux Hub.\n"
                f"Install Python {runtime_id.series} from the Installs page first."
            )
        self.ensure_runtime(
            version=runtime_id,
            allow_frozen_repair=is_frozen(),
            on_status=on_status,
        )
        source = self.private_runtime_root(runtime_id)
        if not os.path.isdir(source):
            raise PythonRuntimeError(
                f"The managed Python {runtime_id.series} runtime directory does not exist.\n"
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

    def _provision_managed_runtime(
        self,
        version: str | PythonRuntimeId,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        runtime_id = self._runtime_id(version)
        bundled_python = self._seed_runtime_from_bundle(
            version=runtime_id, on_status=on_status
        )
        if bundled_python:
            self._prepare_managed_runtime(
                bundled_python, runtime_id, on_status=on_status
            )
            return bundled_python

        python_exe = self._extract_runtime_to_root(
            self.private_runtime_root(runtime_id),
            version=runtime_id,
            overwrite=True,
            on_status=on_status,
        )
        self._prepare_managed_runtime(python_exe, runtime_id, on_status=on_status)
        return python_exe

    def _seed_runtime_from_bundle(
        self,
        *,
        version: str | PythonRuntimeId | None = None,
        overwrite: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        runtime_id = self._runtime_id(version)
        target_root = self.installed_runtime_dir()
        target_python = self.private_runtime_python(runtime_id)

        for source_root in self.bundled_runtime_dirs():
            bundled_root = os.path.join(source_root, runtime_id.directory_name)
            if not is_current_private_runtime_root(
                bundled_root, runtime=runtime_id
            ):
                continue
            bundled_python = _find_python_in_root(bundled_root)
            if not bundled_python or not _is_python_version(
                bundled_python, runtime_id
            ):
                continue
            if _is_embedded_root(os.path.dirname(bundled_python)):
                continue
            if os.path.normcase(os.path.abspath(source_root)) == os.path.normcase(os.path.abspath(target_root)):
                return bundled_python

            _emit_status(
                on_status,
                f"Copying bundled Python {runtime_id.series} runtime...",
            )
            _copy_runtime_payload(bundled_root, self.private_runtime_root(runtime_id), overwrite=overwrite)
            if (
                os.path.isfile(target_python)
                and _is_python_version(target_python, runtime_id)
                and not _is_embedded_root(os.path.dirname(target_python))
                and is_current_private_runtime_root(
                    self.private_runtime_root(runtime_id), runtime=runtime_id
                )
            ):
                return target_python

        for bundle_path in self.bundled_runtime_bundle_paths():
            if not os.path.isfile(bundle_path):
                continue
            runtime_prefix = runtime_id.directory_name + "/"
            try:
                with zipfile.ZipFile(bundle_path, "r") as zf:
                    runtime_members = [
                        member
                        for member in zf.infolist()
                        if member.filename.replace("\\", "/").startswith(
                            runtime_prefix
                        )
                    ]
                    if not runtime_members:
                        continue
                    _emit_status(
                        on_status,
                        f"Extracting bundled Python {runtime_id.series} runtime...",
                    )
                    _remove_tree(self.private_runtime_root(runtime_id))
                    os.makedirs(target_root, exist_ok=True)
                    zf.extractall(target_root, members=runtime_members)
            except (OSError, zipfile.BadZipFile) as exc:
                raise PythonRuntimeError(
                    f"The bundled Python {runtime_id.series} runtime is invalid.\n"
                    f"{exc}"
                ) from exc
            if (
                os.path.isfile(target_python)
                and _is_python_version(target_python, runtime_id)
                and not _is_embedded_root(os.path.dirname(target_python))
                and is_current_private_runtime_root(
                    self.private_runtime_root(runtime_id), runtime=runtime_id
                )
            ):
                return target_python
            _remove_tree(self.private_runtime_root(runtime_id))
        return None

    def _prepare_managed_runtime(
        self,
        python_exe: str,
        version: str | PythonRuntimeId | None = None,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        runtime_id = self._runtime_id(version)
        runtime_root = os.path.dirname(python_exe)
        if _is_embedded_root(runtime_root):
            raise PythonRuntimeError(
                f"Infernux Hub requires a full Python {runtime_id.series} runtime for Nuitka builds, but an embeddable runtime was detected."
            )
        self._ensure_runtime_build_support(
            runtime_root, runtime_id, on_status=on_status
        )
        self._ensure_pip(python_exe, on_status=on_status)
        self._ensure_runtime_packages(
            python_exe, runtime_id, on_status=on_status
        )

    def _ensure_runtime_archive(
        self,
        version: str | PythonRuntimeId | None = None,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        runtime_id = self._runtime_id(version)
        archive = runtime_archive_for_machine(runtime=runtime_id)
        archive_path = self.runtime_archive_path(runtime_id)
        os.makedirs(self.installed_runtime_dir(), exist_ok=True)

        if os.path.isfile(archive_path):
            try:
                verify_runtime_archive(archive_path, archive.sha256)
                return archive_path
            except RuntimeError:
                os.remove(archive_path)

        _emit_status(
            on_status,
            f"Downloading isolated Python {runtime_id.series} runtime for {platform.machine()}...",
        )
        tmp_path = archive_path + ".tmp"
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
            _download_file(
                archive.url,
                tmp_path,
                user_agent="Infernux-Hub/1.0",
            )
            verify_runtime_archive(tmp_path, archive.sha256)
            os.replace(tmp_path, archive_path)
        except urllib.error.URLError as exc:
            if "unknown url type: https" in str(exc).lower():
                raise PythonRuntimeError(
                    f"Failed to download the private Python {runtime_id.series} runtime because HTTPS support is unavailable in the packaged Hub."
                ) from exc
            raise PythonRuntimeError(
                f"Failed to download the private Python {runtime_id.series} runtime.\n{exc}"
            ) from exc
        except OSError as exc:
            raise PythonRuntimeError(
                f"Failed to download the private Python {runtime_id.series} runtime.\n{exc}"
            ) from exc
        except RuntimeError as exc:
            raise PythonRuntimeError(str(exc)) from exc
        finally:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)

        return archive_path

    def _extract_runtime_to_root(
        self,
        runtime_root: str,
        *,
        version: str | PythonRuntimeId | None = None,
        overwrite: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        runtime_id = self._runtime_id(version)
        expected_root = os.path.normcase(
            os.path.realpath(self.private_runtime_root(runtime_id))
        )
        requested_root = os.path.normcase(os.path.realpath(runtime_root))
        if requested_root != expected_root:
            raise PythonRuntimeError(
                "Refusing to deploy the private Python runtime outside the Hub-owned "
                f"{runtime_id.directory_name} directory."
            )
        if overwrite:
            shutil.rmtree(runtime_root, ignore_errors=True)

        archive_path = self._ensure_runtime_archive(
            runtime_id, on_status=on_status
        )
        os.makedirs(os.path.dirname(runtime_root), exist_ok=True)
        _emit_status(
            on_status,
            f"Extracting private Python {runtime_id.series} runtime...",
        )
        try:
            archive = runtime_archive_for_machine(runtime=runtime_id)
            extract_runtime_archive(
                archive_path,
                runtime_root,
                expected_sha256=archive.sha256,
                runtime=runtime_id,
            )
        except RuntimeError as exc:
            raise PythonRuntimeError(str(exc)) from exc

        python_exe = _find_python_in_root(runtime_root)
        if (
            not python_exe
            or not _is_python_version(python_exe, runtime_id)
            or _is_embedded_root(runtime_root)
        ):
            raise PythonRuntimeError(
                f"Private Python {runtime_id.series} extraction completed, but a valid full runtime was not found afterwards."
            )
        return python_exe

    def reinstall_runtime(
        self,
        version: str | PythonRuntimeId | None = None,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Replace the Hub-owned runtime from a verified bundled/downloaded archive."""
        runtime_id = self._runtime_id(version)
        python_exe = self._seed_runtime_from_bundle(
            version=runtime_id, overwrite=True, on_status=on_status
        )
        if not python_exe:
            python_exe = self._extract_runtime_to_root(
                self.private_runtime_root(runtime_id),
                version=runtime_id,
                overwrite=True,
                on_status=on_status,
            )
        self._prepare_managed_runtime(python_exe, runtime_id, on_status=on_status)
        return python_exe

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

    def _bundled_python_roots(
        self, version: str | PythonRuntimeId | None = None
    ) -> list[str]:
        runtime_id = self._runtime_id(version)
        result: list[str] = []
        seen: set[str] = set()
        for root in self.bundled_runtime_dirs():
            candidate = os.path.join(root, runtime_id.directory_name)
            if not os.path.isdir(candidate):
                continue
            norm = os.path.normcase(os.path.abspath(candidate))
            if norm in seen:
                continue
            seen.add(norm)
            result.append(candidate)
        return result

    def _build_support_source_roots(
        self, version: str | PythonRuntimeId | None = None
    ) -> list[str]:
        runtime_id = self._runtime_id(version)
        result: list[str] = []
        seen: set[str] = set()

        for root in self._bundled_python_roots(runtime_id):
            norm = os.path.normcase(os.path.abspath(root))
            if norm in seen:
                continue
            seen.add(norm)
            result.append(root)

        current_runtime = PythonRuntimeId(sys.version_info.major, sys.version_info.minor)
        if not is_frozen() and current_runtime == runtime_id:
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
        version: str | PythonRuntimeId | None = None,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        runtime_id = self._runtime_id(version)
        if _has_build_support(runtime_root, runtime_id):
            return

        _emit_status(on_status, "Preparing CPython build support files...")
        for source_root in self._build_support_source_roots(runtime_id):
            if os.path.normcase(os.path.abspath(source_root)) == os.path.normcase(os.path.abspath(runtime_root)):
                continue
            if _copy_build_support(
                source_root, runtime_root, runtime_id
            ) and _has_build_support(runtime_root, runtime_id):
                return

        raise PythonRuntimeError(
            f"Managed Python {runtime_id.series} is missing CPython build support files "
            f"(Python.h / {runtime_id.windows_library_stem}.lib).\n"
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

    def _ensure_runtime_packages(
        self,
        python_exe: str,
        version: str | PythonRuntimeId | None = None,
        *,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        runtime_id = self._runtime_id(version)
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
            _site_packages_root(os.path.dirname(python_exe), runtime_id),
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
