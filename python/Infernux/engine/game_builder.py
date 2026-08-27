"""
GameBuilder — packages a standalone native game from an Infernux project.

Uses **Nuitka** to compile the Python entry script into a native EXE.
All engine code, dependencies, and the CPython runtime are bundled into
a self-contained directory.  User scripts (.py in Assets/) are compiled
to .pyc with ``py_compile`` for source protection.

Windows output layout::

    <OutputDir>/
    <GameName>.exe          ← the single native Player entry point
        <GameName>_Data/
    Content.inxpkg      ← deterministic native Player content container
            BuildManifest.json  ← display mode and boot settings
            Runtime/            ← private CPython/Nuitka/native engine payload
            Library/RuntimeAssetCatalog.json
            Modules/Parallel.inxmod  ← optional native Numba/LLVM module
"""

from __future__ import annotations

import ast
import copy
import ctypes
import io
import importlib.machinery
import json
import hashlib
import inspect
import os
import py_compile
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import Infernux._jit_kernels as _jit_kernels
import Infernux.resources as _resources
from Infernux.debug import Debug
from Infernux.engine.build_cancellation import BuildCancelled
from Infernux.engine.i18n import t
from Infernux.engine.nuitka_builder import NuitkaBuilder
from Infernux.engine.player_package_native import (
    read_entry,
    read_manifest,
    using_test_backend,
    write_pack,
    write_pack_isolated,
)
from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    path_fingerprint,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
)
from Infernux.engine.build_settings import load_build_settings
from Infernux.engine.runtime_artifact_catalog import (
    RUNTIME_JSON_DOCUMENT_SUFFIXES,
    RuntimeArtifactError,
    build_catalog,
    load_asset_index,
    logical_asset_type,
    logical_type_for_path,
    payload_kind_for,
    RUNTIME_DOCUMENT_PAYLOAD_KINDS,
    runtime_artifact_id,
    runtime_artifact_reason_for,
    source_fingerprint,
    validate_artifact,
)
from Infernux.engine.player_service_graph import (
    PLAYER_MANIFEST_SCHEMA,
    PLAYER_MANIFEST_VERSION,
    RuntimeFeatureSet,
    RuntimeFlavor,
    player_runtime_contract_sections,
)
from Infernux.engine.runtime_type_registry import (
    RUNTIME_TYPE_REGISTRY_SCHEMA,
    RUNTIME_TYPE_REGISTRY_VERSION,
)


_NATIVE_PLAYER_ARCHIVE_SUFFIXES = frozenset({".inxrt", ".inxpkg", ".inxmod"})


def _ensure_video_splash_packages() -> None:
    try:
        import imageio.v3  # noqa: F401
        import av  # noqa: F401
        return
    except ImportError:
        Debug.log_internal(
            "Video splash dependencies missing — installing imageio and av automatically..."
        )
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "imageio", "av", "--quiet"],
        )

    import imageio.v3  # noqa: F401
    import av  # noqa: F401


_BuildCancelled = BuildCancelled


def _yield_editor_thread() -> None:
    """Release the GIL so the editor can keep rendering during a Player build."""
    time.sleep(0)


class BuildOutputDirectoryError(ValueError):
    """Raised when the chosen build output directory is unsafe to reuse."""

    def __init__(
        self,
        reason: str,
        path: str,
        *,
        marker_filename: str,
        entries: Optional[list[str]] = None,
    ):
        self.reason = reason
        self.path = path
        self.marker_filename = marker_filename
        self.entries = list(entries or [])

        if reason == "required":
            message = "Output directory is required."
        elif reason == "path-is-file":
            message = f"Output path is a file, not a directory: {path}"
        elif reason == "path-not-directory":
            message = f"Output path is not a directory: {path}"
        else:
            preview = ", ".join(self.entries[:5])
            if len(self.entries) > 5:
                preview += ", ..."
            message = (
                "Output directory must be empty before building, unless it already contains "
                f"{marker_filename} from a previous Infernux build.\n"
                f"Directory: {path}"
            )
            if preview:
                message += f"\nFound: {preview}"

        super().__init__(message)


def _remove_player_path(path: str, *, ignore_errors: bool = False) -> None:
    """Remove one file or directory tree using only boot-required stdlib."""

    try:
        if os.path.isdir(path) and not os.path.islink(path):
            for root, directories, files in os.walk(path, topdown=False):
                for filename in files:
                    os.remove(os.path.join(root, filename))
                for directory in directories:
                    child = os.path.join(root, directory)
                    if os.path.islink(child):
                        os.remove(child)
                    else:
                        os.rmdir(child)
            os.rmdir(path)
        else:
            os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        if not ignore_errors:
            raise


def _copy_player_file_atomic(source: str, destination: str) -> None:
    """Copy one file through a durable same-directory temporary file."""

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + f".{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(source, "rb") as source_file, open(temporary, "xb") as output_file:
            while True:
                chunk = source_file.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def _write_json_atomic(
    destination: str,
    payload: object,
    *,
    indent: int | None = 2,
) -> None:
    """Durably replace one JSON document without exposing partial bytes."""

    destination = os.fspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + f".{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=indent,
                sort_keys=True,
                separators=None if indent is not None else (",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def _player_builder_process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _publish_player_cache(
    temporary_root: str,
    cache_root: str,
    expected_hash: str,
    *,
    timeout_seconds: float = 30.0,
    # The lock is created immediately before its PID metadata is written.
    # Keep this grace period short enough that a dead publisher cannot consume
    # the whole wait timeout, while a live PID remains protected indefinitely.
    stale_lock_seconds: float = 0.5,
) -> str:
    """Publish one completed Player cache with recoverable ownership locking."""

    temporary_root = os.fspath(temporary_root)
    cache_root = os.fspath(cache_root)
    ready_marker = os.path.join(cache_root, ".ready")
    lock_path = cache_root + ".lock"
    os.makedirs(os.path.dirname(cache_root), exist_ok=True)

    def is_ready() -> bool:
        try:
            with open(ready_marker, "r", encoding="ascii") as marker:
                return marker.read().strip() == expected_hash
        except OSError:
            return False

    def process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        return bool(_NATIVE_PACK._inxplayer_process_is_alive(pid))

    def lock_is_stale() -> bool:
        try:
            lock_stat = os.stat(lock_path)
        except OSError:
            return False
        if time.time() - lock_stat.st_mtime < stale_lock_seconds:
            return False
        try:
            with open(lock_path, "r", encoding="ascii") as lock_file:
                lock_pid = int(lock_file.read().strip() or "0")
        except (OSError, ValueError):
            lock_pid = 0
        return not process_is_alive(lock_pid)

    def reclaim_stale_lock() -> bool:
        if not lock_is_stale():
            return False
        stale_path = lock_path + f".stale.{os.getpid()}.{time.time_ns()}"
        try:
            os.replace(lock_path, stale_path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            os.remove(stale_path)
        except FileNotFoundError:
            pass
        return True

    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if is_ready():
                return cache_root
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if is_ready():
                    return cache_root
                reclaim_stale_lock()
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Timed out waiting for Player cache publication: {cache_root}"
                    )
                time.sleep(0.02)
                continue

            try:
                lock_payload = str(os.getpid()).encode("ascii")
                os.write(lock_fd, lock_payload)
                os.fsync(lock_fd)
                if is_ready():
                    return cache_root
                if os.path.isdir(cache_root):
                    _remove_player_path(cache_root)
                elif os.path.exists(cache_root):
                    os.remove(cache_root)
                os.replace(temporary_root, cache_root)
                return cache_root
            finally:
                try:
                    os.close(lock_fd)
                finally:
                    try:
                        os.remove(lock_path)
                    except FileNotFoundError:
                        pass
    finally:
        _remove_player_path(temporary_root, ignore_errors=True)


# The standalone Player boot script must use exactly the same publication
# protocol as the editor-side builder.  Keep this as generated source rather
# than maintaining a second, inevitably divergent implementation.
_PLAYER_CACHE_PUBLISH_SOURCE = textwrap.dedent(
    inspect.getsource(_remove_player_path)
    + "\n\n\n"
    + inspect.getsource(_copy_player_file_atomic)
    + "\n\n\n"
    + inspect.getsource(_publish_player_cache)
).strip()


from ._build_splash import BuildSplashMixin
from ._build_dependencies import BuildDependencyMixin


class GameBuilder(BuildSplashMixin, BuildDependencyMixin):
    """Build a standalone native game distribution using Nuitka."""

    OUTPUT_MARKER_FILENAME = ".infernux-build-output"
    _BUILD_TEMP_DIR_NAME = "_build_temp"
    _GAME_DATA_FILES = frozenset(
        {
            "ProjectSettings/BuildSettings.json",
            "ProjectSettings/PhysicsSettings.json",
            "ProjectSettings/TagLayerSettings.json",
        }
    )
    _EXCLUDE_PATTERNS = {
        "__pycache__",
        ".git",
        ".gitignore",
        ".infernux-engine-lock.json",
        "Logs",
    }
    _ICON_EXTS = {".png", ".jpg", ".jpeg", ".ico"}
    # Editor-only package code is excluded structurally by package file roles.
    # Do not reserve third-party names: user games may depend on any PyPI package.
    _GAME_BUILD_EXCLUDED_PACKAGES = frozenset()
    _PLAYER_EXCLUDED_CONTENT_RELATIVE_PATHS = frozenset(
        {
            "ProjectSettings/.infernux-engine-lock.json",
            "ProjectSettings/agent_tools.json",
            "ProjectSettings/mcp_capabilities.json",
            "ProjectSettings/requirements.txt",
            # These files belong to the Editor workspace, not to a shipped
            # Player. They used to be removed by _cleanup_dist after the
            # content archive had already consumed them.
            "ProjectSettings/EditorSettings.json",
            "ProjectSettings/GameView.ini",
        }
    )
    _PLAYER_PORTABLE_DOCUMENT_SUFFIXES = RUNTIME_JSON_DOCUMENT_SUFFIXES
    _NUMPY_RUNTIME_EXCLUDED_DIRECTORIES = frozenset(
        {
            "__pycache__",
            "_examples",
            "doc",
            "docs",
            "include",
            "testing",
            "tests",
        }
    )
    _NUMPY_RUNTIME_EXCLUDED_SUFFIXES = frozenset(
        {".c", ".cc", ".cpp", ".h", ".hpp", ".md", ".pc", ".pxd", ".pxi", ".pyi", ".pyx", ".rst"}
    )
    _NUMPY_RUNTIME_LEGAL_FILE_MARKERS = (
        "license",
        "copying",
        "notice",
        "authors",
    )
    _PLAYER_RUNTIME_ICON_FILES = frozenset(
        {
            # The Player bootstrap uses icon.png for the game window and
            # native built-in materials resolve these three gizmo textures
            # during renderer startup. All other icon files belong to Editor
            # panels and are intentionally excluded from Runtime.inxrt.
            "icon.png",
            "gizmo_camera.png",
            "gizmo_light.png",
            "gizmo_particle.png",
        }
    )

    @classmethod
    def _include_numpy_runtime_file(cls, filename: str) -> bool:
        """Keep importable NumPy payload and legal notices, not test/docs data."""

        lowered = filename.casefold()
        if lowered == "py.typed":
            return False
        # NumPy ships compiled test modules beside its production extensions,
        # e.g. _multiarray_tests.cp312-win_amd64.pyd. They are never needed by
        # engine runtime imports and are unambiguously test-only by name.
        if lowered.endswith(".pyd") and "_tests" in lowered:
            return False
        if any(marker in lowered for marker in cls._NUMPY_RUNTIME_LEGAL_FILE_MARKERS):
            return True
        return Path(filename).suffix.casefold() not in cls._NUMPY_RUNTIME_EXCLUDED_SUFFIXES | {
            ".html",
            ".txt",
        }
    # These extensions are not imported by the generated boot source before
    # Runtime.inxrt extraction. All loose non-bootstrap native files are
    # classified dynamically when Runtime.inxrt is assembled.
    _PLAYER_DEFERRED_STDLIB_FILES = frozenset(
        {
            "_decimal.pyd",
            "_hashlib.pyd",
            "_multiprocessing.pyd",
            "_queue.pyd",
            "_socket.pyd",
            "_ssl.pyd",
            "_uuid.pyd",
            "_wmi.pyd",
            "pyexpat.pyd",
            "select.pyd",
            "unicodedata.pyd",
            "libcrypto-3-x64.dll",
            "libcrypto-3.dll",
            "libssl-3-x64.dll",
            "libssl-3.dll",
            "libexpat.dll",
            "libexpat-1.dll",
        }
    )
    # Editor localization is loaded by the Editor only.  Keep this explicit
    # because ``--include-package-data=Infernux`` otherwise makes it easy for
    # Nuitka to copy the directory back into a Player staging tree.
    _PLAYER_EDITOR_DATA_DIRECTORIES = frozenset(
        {os.path.join("Infernux", "engine", "locales")}
    )
    _PLAYER_EXCLUDED_PARTICLE_AUTHORING_SUFFIXES = (
        ".particlegraph",
        ".particlegraph.meta",
        ".particle.py",
        ".particle.py.meta",
        ".particle.pyc",
        ".particle.pyc.meta",
    )
    _CONTENT_ARCHIVE_FILENAME = "Content.inxpkg"
    _RUNTIME_ARCHIVE_FILENAME = "Runtime.inxrt"
    _PARALLEL_ARCHIVE_FILENAME = "Parallel.inxmod"
    _PLAYER_MANIFEST_FILENAME = "Player.inxmanifest"
    _PLAYER_PACKAGE_INDEX_FILENAME = "PackageIndex.inxmanifest"
    _RUNTIME_ASSET_RECORDS_FILENAME = "RuntimeAssetRecords.json"
    _RUNTIME_TYPE_REGISTRY_FILENAME = "RuntimeTypeRegistry.json"
    _PLAYER_CONTENT_CONTROL_RELATIVE_PATHS = frozenset(
        {
            "BuildManifest.json",
            _PLAYER_MANIFEST_FILENAME,
            _PLAYER_PACKAGE_INDEX_FILENAME,
            _CONTENT_ARCHIVE_FILENAME,
        }
    )
    _PARTICLE_RUNTIME_INDEX_FILENAME = "RuntimeIndex.json"
    def __init__(
        self,
        project_path: str,
        output_dir: str,
        *,
        game_name: str = "",
        icon_path: Optional[str] = None,
        display_mode: str = "fullscreen_borderless",
        window_width: int = 1280,
        window_height: int = 720,
        window_resizable: bool = True,
        splash_items: Optional[List[Dict]] = None,
        debug_mode: bool = False,
        lto: bool = True,
        enable_jit: bool = False,
    ):
        self.project_path = resolved_path(project_path)
        self.project_name = game_name.strip() if game_name.strip() else os.path.basename(self.project_path)
        self.output_dir = resolved_path(output_dir)
        self.icon_path = resolved_path(icon_path) if icon_path else ""
        self._built_icon_path = ""
        self.display_mode = display_mode
        self.window_width = window_width
        self.window_height = window_height
        self.window_resizable = window_resizable
        self.splash_items = list(splash_items) if splash_items else []
        self.debug_mode = debug_mode
        self.lto = lto
        self.enable_jit = enable_jit
        self._full_build_validated = False
        self._runtime_type_records: list[dict[str, object]] = []
        self._build_output_transaction: dict[str, str] | None = None
        self._asset_index_entries_snapshot: list[dict] | None = None

    def _player_inxpack_profile(self) -> str:
        """Return the compression profile for this concrete Player build."""

        return "development" if self.debug_mode else "release"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        on_progress: Optional[Callable[[str, float], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Run the full build pipeline.  Returns the final output directory."""

        build_start = time.perf_counter()
        _stage_t0 = build_start
        # A build is an immutable transaction.  The editor preflight may have
        # frozen a catalog before this worker starts; retain that snapshot so
        # file watching cannot change the cooked closure mid-build.

        # Keep build diagnostics outside the project. A Player build must not
        # mutate the author's project by creating Logs/build.log, and a
        # successful build must not leave build logs in the distribution.
        log_dir = tempfile.mkdtemp(prefix="infernux-player-build-")
        build_log_path = os.path.join(log_dir, "build.log")
        build_log = open(build_log_path, "w", encoding="utf-8")
        build_succeeded = False

        def _blog(msg: str):
            """Write to both the engine console and the build log file."""
            try:
                build_log.write(msg + "\n")
                build_log.flush()
            except OSError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass

        def _p(msg: str, pct: float):
            nonlocal _stage_t0
            if cancel_event is not None and cancel_event.is_set():
                raise _BuildCancelled()
            now = time.perf_counter()
            elapsed = now - _stage_t0
            _stage_t0 = now
            if on_progress:
                on_progress(msg, pct)
            log_msg = (
                f"[Build {pct:.0%}] {msg}  (prev stage {elapsed:.2f}s, "
                f"total {now - build_start:.1f}s)"
            )
            Debug.log_internal(log_msg)
            _blog(log_msg)

        try:
            result = self._build_inner(_p, _blog, on_progress, cancel_event, build_start)
            build_succeeded = True
            return result
        except _BuildCancelled:
            _blog("Build cancelled by user.")
            raise
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _blog(f"BUILD FAILED: {tb}")
            Debug.log_error(
                f"Build failed — see {build_log_path} for details.\n{exc}"
            )
            raise
        finally:
            try:
                build_log.close()
            except OSError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass
            if build_succeeded:
                shutil.rmtree(log_dir, ignore_errors=True)
            self._abort_output_transaction()

    def _build_inner(self, _p, _blog, on_progress, cancel_event, build_start) -> str:
        """Internal build pipeline (separated for clean exception handling)."""

        _p(t("build.step.validating"), 0.00)
        self._validate()
        self._begin_output_transaction()

        _p(t("build.step.cleaning_output"), 0.02)
        self._clean_output()
        self._write_output_marker(self.output_dir, state="in_progress")

        _p(t("build.step.collecting_deps"), 0.04)
        user_packages = self._collect_user_dependencies()

        _p(t("build.step.generating_boot"), 0.05)
        boot_script = self._generate_boot_script()

        _p(t("build.step.nuitka_compilation"), 0.06)
        try:
            dist_dir = self._run_nuitka(
                boot_script,
                on_progress,
                user_packages,
                cancel_event,
            )
        finally:
            # The boot source lives under the output directory. Remove it
            # before the compiled dist is moved there, otherwise the layout
            # pass can accidentally ship it inside Runtime/_build_temp.
            self._cleanup_temp(boot_script)

        _p(t("build.step.organizing_output"), 0.86)
        final_dir = self._organize_output(dist_dir)

        _p(t("build.step.copying_data"), 0.88)
        self._copy_game_data(final_dir)

        _p(t("build.step.compiling_scripts"), 0.91)
        self._compile_user_scripts(final_dir)
        self._compile_player_plugin_scripts(final_dir)
        self._write_runtime_asset_records(final_dir)

        _p(t("build.step.processing_splash"), 0.93)
        self._process_build_icon(final_dir)
        self._process_splash_items(final_dir)

        _p(t("build.step.fixing_scenes"), 0.96)
        self._relativize_scenes(final_dir)

        _p(t("build.step.generating_manifest"), 0.97)
        self._generate_manifest(final_dir)

        _p(t("build.step.cleaning_redundant"), 0.98)
        self._cleanup_dist(final_dir)

        _p("Organizing Player distribution", 0.9802)
        self._organize_player_layout(final_dir)

        _p("Packing Player bootstrap", 0.98035)
        self._pack_player_bootstrap_archive(
            final_dir,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

        _p("Packing core runtime data", 0.9805)
        self._pack_core_runtime_archive(
            final_dir,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

        _p("Packing optional parallel runtime data", 0.9807)
        self._pack_parallel_runtime_archive(final_dir)

        _p("Packing project content", 0.981)
        self._pack_content_archive(
            final_dir,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

        _p("Writing Player runtime catalog", 0.984)
        self._write_payload_manifest(final_dir)

        # The in-progress marker protects cleanup while the build is mutable.
        # Final ownership is embedded in BuildManifest so the shipped root has
        # no packaging-only sentinel outside the strict bootstrap surface.
        try:
            os.remove(self._output_marker_path(final_dir))
        except FileNotFoundError:
            pass

        # Log per-directory size breakdown so the user sees where size goes
        self._report_build_size(final_dir, _blog)

        final_dir = self._commit_output_transaction(final_dir)

        _p(t("build.step.complete"), 1.0)
        elapsed_seconds = time.perf_counter() - build_start
        done_msg = t("build.completed_log").format(
            path=final_dir,
            seconds=elapsed_seconds,
        )
        Debug.log(done_msg)
        _blog(done_msg)
        return final_dir

    def _begin_output_transaction(self) -> None:
        """Claim an isolated sibling staging directory for this build."""

        if self._build_output_transaction is not None:
            raise RuntimeError("Player output transaction is already active")
        final_dir = resolved_path(self.output_dir)
        parent = os.path.dirname(final_dir)
        basename = os.path.basename(final_dir.rstrip("\\/"))
        os.makedirs(parent, exist_ok=True)
        lock_path = final_dir + ".infernux-build.lock"
        prefix = f".{basename}.infernux-staging-"
        backup_prefix = f".{basename}.infernux-previous-"

        def reclaim_stale_lock() -> None:
            try:
                lock_age = time.time() - os.stat(lock_path).st_mtime
            except OSError:
                return
            try:
                with open(lock_path, "r", encoding="utf-8") as stream:
                    owner = json.load(stream)
            except (OSError, json.JSONDecodeError, TypeError):
                owner = {}
            try:
                pid = int(owner.get("pid", 0)) if isinstance(owner, dict) else 0
            except (TypeError, ValueError):
                pid = 0
            if _player_builder_process_is_alive(pid) or (not pid and lock_age < 2.0):
                raise RuntimeError(
                    f"Another Player build owns this output directory: {final_dir}"
                )
            stale_staging = (
                str(owner.get("staging", "")) if isinstance(owner, dict) else ""
            )
            stale_backup = (
                str(owner.get("backup", "")) if isinstance(owner, dict) else ""
            )
            if stale_backup:
                stale_backup = resolved_path(stale_backup)
                if (
                    same_path(os.path.dirname(stale_backup), parent)
                    and os.path.basename(stale_backup).startswith(backup_prefix)
                    and os.path.exists(stale_backup)
                ):
                    if os.path.exists(final_dir):
                        _remove_player_path(stale_backup, ignore_errors=True)
                    else:
                        os.replace(stale_backup, final_dir)
            if stale_staging:
                stale_staging = resolved_path(stale_staging)
                if (
                    same_path(os.path.dirname(stale_staging), parent)
                    and os.path.basename(stale_staging).startswith(prefix)
                ):
                    _remove_player_path(stale_staging, ignore_errors=True)
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass

        for _attempt in range(2):
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                reclaim_stale_lock()
        else:
            raise RuntimeError(f"Unable to claim Player output: {final_dir}")

        generation = f"{os.getpid()}-{time.time_ns()}"
        staging = os.path.join(parent, f"{prefix}{generation}")
        backup = os.path.join(parent, f"{backup_prefix}{generation}")
        try:
            lock_payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "project_identity": path_fingerprint(self.project_path),
                    "staging": portable_path(staging),
                    "backup": portable_path(backup),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            os.write(lock_fd, lock_payload)
            os.fsync(lock_fd)
        finally:
            os.close(lock_fd)

        try:
            os.makedirs(staging, exist_ok=False)
        except Exception:
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass
            raise
        self._build_output_transaction = {
            "final": final_dir,
            "staging": resolved_path(staging),
            "backup": resolved_path(backup),
            "lock": lock_path,
        }
        self.output_dir = resolved_path(staging)

    def _commit_output_transaction(self, staged_dir: str) -> str:
        """Publish a complete staging product while preserving the old one."""

        state = self._build_output_transaction
        if state is None or not same_path(staged_dir, state["staging"]):
            raise RuntimeError("Player output transaction staging identity changed")
        final_dir = state["final"]
        backup = state["backup"]
        moved_old = False
        try:
            if os.path.exists(final_dir):
                os.replace(final_dir, backup)
                moved_old = True
            try:
                os.replace(state["staging"], final_dir)
            except Exception:
                if moved_old and not os.path.exists(final_dir):
                    os.replace(backup, final_dir)
                raise
            if moved_old:
                _remove_player_path(backup, ignore_errors=True)
            self.output_dir = final_dir
            self._release_output_transaction_lock()
            self._build_output_transaction = None
            return final_dir
        except Exception:
            self.output_dir = final_dir
            raise

    def _release_output_transaction_lock(self) -> None:
        state = self._build_output_transaction
        if state is None:
            return
        try:
            os.remove(state["lock"])
        except FileNotFoundError:
            pass

    def _abort_output_transaction(self) -> None:
        state = self._build_output_transaction
        if state is None:
            return
        self.output_dir = state["final"]
        _remove_player_path(state["staging"], ignore_errors=True)
        self._release_output_transaction_lock()
        self._build_output_transaction = None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _resolve_build_scene_path(self, scene_path: str) -> str:
        if type(scene_path) is not str or not scene_path.strip():
            raise ValueError("BuildSettings scenes must contain non-empty strings")
        candidate = (
            scene_path
            if os.path.isabs(scene_path)
            else os.path.join(self.project_path, scene_path)
        )
        absolute = resolved_path(candidate)
        assets_root = resolved_path(os.path.join(self.project_path, "Assets"))
        if not is_path_within(absolute, assets_root, allow_root=False):
            raise ValueError(
                f"Build scene must be inside the project Assets folder: {scene_path}"
            )
        if not absolute.lower().endswith(".scene"):
            raise ValueError(f"Build scene must use the .scene extension: {scene_path}")
        return absolute

    def _validate(self):
        self._full_build_validated = False
        bs = os.path.join(
            self.project_path, "ProjectSettings", "BuildSettings.json"
        )
        if not os.path.isfile(bs):
            raise FileNotFoundError(
                "BuildSettings.json not found. "
                "Open Build Settings in the editor and add at least one scene."
            )
        with open(bs, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        scenes = data.get("scenes", [])
        if type(scenes) is not list or not scenes:
            raise ValueError(
                "Build list is empty. Add at least one scene in Build Settings."
            )
        resolved_scenes = [self._resolve_build_scene_path(scene) for scene in scenes]
        missing = [scene for scene in resolved_scenes if not os.path.isfile(scene)]
        if missing:
            names = ", ".join(os.path.basename(m) for m in missing)
            raise FileNotFoundError(f"Scene file(s) not found: {names}")

        entries = self._asset_index_entries()
        selected = self._collect_library_asset_entries(entries)
        self._ensure_selected_particle_artifacts(selected)
        self._validate_animation_clip_assets()

        if self.icon_path:
            if not os.path.isfile(self.icon_path):
                raise FileNotFoundError(f"Build icon not found: {self.icon_path}")
            ext = os.path.splitext(self.icon_path)[1].lower()
            if ext not in self._ICON_EXTS:
                raise ValueError(
                    "Build icon must be a .png, .jpg, .jpeg, or .ico file."
                )

        self._validate_output_directory()
        self._full_build_validated = True

    def _asset_index_entries(self) -> list[dict]:
        """Return this build transaction's immutable AssetIndex snapshot."""

        if self._asset_index_entries_snapshot is None:
            self._asset_index_entries_snapshot = copy.deepcopy(
                load_asset_index(self.project_path)
            )
        # Callers build lookup maps from these documents.  Keep the canonical
        # snapshot isolated in case a future cook stage mutates an entry.
        return copy.deepcopy(self._asset_index_entries_snapshot)

    def freeze_asset_index_entries(self, entries: list[dict]) -> None:
        """Bind this one Player build to an editor-published AssetIndex.

        This is intentionally a copy rather than a path: the editor's file
        watcher may regenerate ``Library/AssetIndex.json`` while the Player
        cook is compiling scripts and packaging resources.
        """

        self._asset_index_entries_snapshot = copy.deepcopy(entries)

    def _validate_animation_clip_assets(self) -> None:
        """Reject dangling SpriteFrame references before packaging content."""
        from Infernux.core.animation_clip import AnimationClip

        guid_paths = self._build_asset_guid_index()
        assets_root = os.path.join(self.project_path, "Assets")
        for root, _directories, filenames in os.walk(assets_root):
            for filename in filenames:
                if not filename.casefold().endswith(".animclip2d"):
                    continue
                clip_path = os.path.join(root, filename)
                clip = AnimationClip.load(clip_path)
                if clip is None:
                    raise ValueError(
                        f"AnimationClip is not valid current JSON: {clip_path}"
                    )
                try:
                    clip.validate_sprite_frame_references(
                        project_root=self.project_path,
                        guid_paths=guid_paths,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"AnimationClip build validation failed for '{clip_path}': {exc}"
                    ) from exc

    def _output_marker_path(self, directory: Optional[str] = None) -> str:
        target_dir = resolved_path(directory or self.output_dir)
        return os.path.join(target_dir, self.OUTPUT_MARKER_FILENAME)

    def _validate_output_directory(self) -> None:
        if not self.output_dir:
            raise BuildOutputDirectoryError(
                "required",
                self.output_dir,
                marker_filename=self.OUTPUT_MARKER_FILENAME,
            )

        if os.path.isfile(self.output_dir):
            raise BuildOutputDirectoryError(
                "path-is-file",
                self.output_dir,
                marker_filename=self.OUTPUT_MARKER_FILENAME,
            )

        if not os.path.exists(self.output_dir):
            return

        if not os.path.isdir(self.output_dir):
            raise BuildOutputDirectoryError(
                "path-not-directory",
                self.output_dir,
                marker_filename=self.OUTPUT_MARKER_FILENAME,
            )

        entries = [entry.name for entry in os.scandir(self.output_dir)]
        if not entries:
            return
        marker_path = self._output_marker_path(self.output_dir)
        if self._is_owned_output_marker(marker_path):
            return
        if self._is_owned_build_manifest(self.output_dir):
            return

        raise BuildOutputDirectoryError(
            "not-empty-unmarked",
            self.output_dir,
            marker_filename=self.OUTPUT_MARKER_FILENAME,
            entries=sorted(entries),
        )

    # ------------------------------------------------------------------
    # Clean output
    # ------------------------------------------------------------------

    def _clean_output(self):
        os.makedirs(self.output_dir, exist_ok=True)
        self._validate_output_directory()

        for name in os.listdir(self.output_dir):
            path = os.path.join(self.output_dir, name)
            if os.path.isdir(path) and not os.path.islink(path):
                if sys.platform == "win32":
                    subprocess.run(
                        ["cmd", "/c", "rd", "/s", "/q", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except FileNotFoundError as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    continue

            if os.path.exists(path):
                raise OSError(f"Failed to clean output path: {path}")

    def _is_owned_output_marker(self, marker_path: str) -> bool:
        try:
            with open(marker_path, "r", encoding="utf-8") as marker_file:
                payload = json.load(marker_file)
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("tool") == "Infernux"
            and payload.get("kind") == "build-output"
            and payload.get("project_name") == self.project_name
            and payload.get("project_identity") == path_fingerprint(self.project_path)
            and payload.get("state") in {"in_progress", "complete"}
        )

    def _is_owned_build_manifest(self, directory: str) -> bool:
        manifest_path = os.path.join(
            resolved_path(directory),
            f"{self.project_name}_Data",
            "BuildManifest.json",
        )
        try:
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                payload = json.load(manifest_file)
        except (OSError, json.JSONDecodeError):
            return False
        ownership = payload.get("build_output", {}) if isinstance(payload, dict) else {}
        return (
            isinstance(ownership, dict)
            and ownership.get("tool") == "Infernux"
            and ownership.get("project_name") == self.project_name
            and ownership.get("project_identity") == path_fingerprint(self.project_path)
        )

    def _write_output_marker(self, final_dir: str, *, state: str = "complete") -> None:
        if state not in {"in_progress", "complete"}:
            raise ValueError(f"Unsupported build output state: {state}")
        marker_path = self._output_marker_path(final_dir)
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        marker_payload = {
            "tool": "Infernux",
            "kind": "build-output",
            "project_name": self.project_name,
            "project_identity": path_fingerprint(self.project_path),
            "state": state,
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_json_atomic(marker_path, marker_payload)

    # ------------------------------------------------------------------
    # Generate boot script (temporary, fed to Nuitka)
    # ------------------------------------------------------------------

    def _generate_boot_script(self) -> str:
        """Generate the native-bridge entry script consumed by Nuitka."""

        boot_src = '''\
"""Infernux Game — compiled entry point."""
import os
import sys
import time

_BOOT_STARTED = time.perf_counter()
_BOOT_PHASES = []

def _mark_boot_phase(_name):
    _BOOT_PHASES.append((_name, time.perf_counter() - _BOOT_STARTED))

# Activate Player mode before importing the engine package.
os.environ["_INFERNUX_PLAYER_MODE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

_PLAYER_ROOT = os.path.dirname(sys.executable)
_EXE_STEM = os.path.splitext(os.path.basename(sys.argv[0]))[0]
_DATA_ROOT = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
if not _DATA_ROOT:
    _DATA_ROOT = os.path.join(_PLAYER_ROOT, _EXE_STEM + "_Data")
# Player.inxmanifest and the native package files live in the outer Data
# directory. Keep this separate from the extracted project root.
os.environ["_INFERNUX_PLAYER_DATA_ROOT"] = _DATA_ROOT
_RUNTIME_ROOT = os.environ.get("_INFERNUX_PLAYER_RUNTIME_ROOT", "").strip() or _PLAYER_ROOT
if _PLAYER_ROOT not in sys.path:
    sys.path.insert(0, _PLAYER_ROOT)

_GAME_NAME = _EXE_STEM or "InfernuxPlayer"
_SAFE_GAME_NAME = "".join(
    _ch if _ch not in '<>:"/\\\\|?*' else '_' for _ch in _GAME_NAME
)

try:
    import _InfernuxBootstrap as _NATIVE_PACK
except ImportError as _bootstrap_error:
    raise RuntimeError(
        "The Player bootstrap InxPack API is unavailable; "
        "Python/ZIP/LZMA package readers are not supported."
    ) from _bootstrap_error

_mark_boot_phase("native_bootstrap")

for _bootstrap_api in (
    "_inxpack_read_manifest",
    "_inxpack_extract",
    "_inxpack_read_entry",
    "_inxplayer_show_error",
    "_inxplayer_process_is_alive",
):
    if not hasattr(_NATIVE_PACK, _bootstrap_api):
        raise RuntimeError("The Player bootstrap is missing API: " + _bootstrap_api)

def _validate_native_archive_paths(_archive_path, _allowed_roots=None):
    """Reject unsafe or out-of-contract entries before native extraction."""
    _manifest = dict(_NATIVE_PACK._inxpack_read_manifest(_archive_path))
    _roots = None if _allowed_roots is None else set(_allowed_roots)
    for _item in _manifest.get("files", []):
        _name = str(_item.get("path", "")).replace("\\\\", "/")
        _parts = _name.split("/")
        if (
            not _name
            or "\\x00" in _name
            or _name.startswith("/")
            or (_name.startswith("\\\\"))
            or (len(_name) >= 2 and _name[1] == ":")
            or any(_part in {"", ".", ".."} for _part in _parts)
        ):
            raise RuntimeError(
                "Native Player package contains an unsafe entry path: " + _name
            )
        if _roots is not None and _parts[0] not in _roots:
            raise RuntimeError(
                "Native Player package contains an unexpected root: " + _name
            )
    return _manifest

def _load_player_package_index():
    """Read the tiny pre-runtime package identity index without json/stdlib."""
    _index_path = os.path.join(_DATA_ROOT, "PackageIndex.inxmanifest")
    _records = {}
    try:
        with open(_index_path, "r", encoding="ascii") as _stream:
            _header = _stream.readline().strip()
            if _header != "INFERNUX_PLAYER_PACKAGE_INDEX_V1":
                return _records
            for _line in _stream:
                _parts = _line.rstrip("\\r\\n").split("\\t")
                if len(_parts) != 3:
                    continue
                _kind, _archive_hash, _archive_bytes = _parts
                if (
                    _kind not in {"runtime", "content", "parallel"}
                    or len(_archive_hash) != 64
                    or any(_ch not in "0123456789abcdef" for _ch in _archive_hash)
                ):
                    continue
                try:
                    _archive_bytes = int(_archive_bytes)
                except ValueError:
                    continue
                if _archive_bytes < 0:
                    continue
                _records[_kind] = (_archive_hash, _archive_bytes)
    except OSError:
        pass
    return _records

_PLAYER_PACKAGE_INDEX = _load_player_package_index()
for _package_kind, (_package_hash, _package_bytes) in _PLAYER_PACKAGE_INDEX.items():
    _kind_key = str(_package_kind).upper()
    os.environ["_INFERNUX_PLAYER_" + _kind_key + "_ARCHIVE_SHA256"] = _package_hash
    os.environ["_INFERNUX_PLAYER_" + _kind_key + "_ARCHIVE_BYTES"] = str(_package_bytes)

_DEBUG_MODE = __INFERNUX_DEBUG_MODE__
os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = "1" if _DEBUG_MODE else "0"

def _extract_cached_archive(_archive_path, _cache_kind, _allowed_roots=None):
    if not os.path.isfile(_archive_path):
        raise RuntimeError("Required native Player package is missing: " + _archive_path)
    _archive_stat = os.stat(_archive_path)
    _indexed_identity = _PLAYER_PACKAGE_INDEX.get(str(_cache_kind))
    _manifest = None
    if _indexed_identity is None:
        _manifest = _validate_native_archive_paths(_archive_path, _allowed_roots)
        _expected_hash = str(_manifest.get("archive_sha256", ""))
        _expected_bytes = int(_manifest.get("archive_bytes", -1))
    else:
        _expected_hash, _expected_bytes = _indexed_identity
    if not _expected_hash:
        raise RuntimeError("Native Player package has no archive checksum: " + _archive_path)
    if _expected_bytes != _archive_stat.st_size:
        raise RuntimeError("Native Player package size mismatch: " + _archive_path)
    # The build-authored digest is the durable archive identity.  File times
    # change when a Player is copied, downloaded, or restored by an installer;
    # including mtime here forced a complete extraction again even though the
    # package bytes were identical.
    _source_identity = _expected_hash + "\\n" + str(_archive_stat.st_size)

    # The native manifest has already verified the complete archive.  Pass
    # that trusted result to PlayerBootstrap so startup does not hash the
    # same potentially large package a second time.
    _kind_key = str(_cache_kind).upper()
    os.environ["_INFERNUX_PLAYER_" + _kind_key + "_ARCHIVE_SHA256"] = _expected_hash
    os.environ["_INFERNUX_PLAYER_" + _kind_key + "_ARCHIVE_BYTES"] = str(
        _expected_bytes
    )

    _cache_parent = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CACHE_HOME")
        or os.path.join(os.path.expanduser("~"), ".cache")
    )
    _cache_root = os.path.join(
        _cache_parent,
        "Infernux",
        "PlayerCache",
        _SAFE_GAME_NAME,
        _cache_kind + "-" + _expected_hash[:20],
    )
    _ready_marker = os.path.join(_cache_root, ".ready")
    _source_marker = os.path.join(_cache_root, ".source")
    try:
        with open(_ready_marker, "r", encoding="ascii") as _marker, open(
            _source_marker, "r", encoding="ascii"
        ) as _source:
            if (
                _marker.read().strip() == _expected_hash
                and _source.read().strip() == _source_identity
            ):
                return _cache_root
    except OSError:
        pass

    _temporary = _cache_root + "." + str(os.getpid()) + ".tmp"
    _remove_player_path(_temporary, ignore_errors=True)
    os.makedirs(_temporary, exist_ok=False)
    try:
        _extracted_manifest = dict(
            _NATIVE_PACK._inxpack_extract(
                _archive_path,
                _temporary,
                None if _allowed_roots is None else sorted(_allowed_roots),
            )
        )
        if (
            str(_extracted_manifest.get("archive_sha256", "")) != _expected_hash
            or int(_extracted_manifest.get("archive_bytes", -1)) != _expected_bytes
        ):
            raise RuntimeError(
                "Native Player package identity does not match its build index: "
                + _archive_path
            )
        with open(os.path.join(_temporary, ".ready"), "w", encoding="ascii") as _marker:
            _marker.write(_expected_hash)
        with open(os.path.join(_temporary, ".source"), "w", encoding="ascii") as _source:
            _source.write(_source_identity)
        _publish_player_cache(_temporary, _cache_root, _expected_hash)
        return _cache_root
    finally:
        _remove_player_path(_temporary, ignore_errors=True)
    return _cache_root

_RUNTIME_ARCHIVE = os.path.join(_DATA_ROOT, "Runtime.inxrt")
_CORE_RUNTIME_DIR = _extract_cached_archive(
    _RUNTIME_ARCHIVE,
    "runtime",
    {"Infernux", "numpy", "numpy.libs", "stdlib"},
)
_mark_boot_phase("runtime_ready")
_STDLIB_RUNTIME_DIR = os.path.join(_CORE_RUNTIME_DIR, "stdlib")
_INFERNUX_LIB_DIR = os.path.join(_CORE_RUNTIME_DIR, "Infernux", "lib")
os.environ["INFERNUX_NATIVE_MODULE_DIR"] = _INFERNUX_LIB_DIR
for _runtime_import_dir in (
    _CORE_RUNTIME_DIR,
    _STDLIB_RUNTIME_DIR,
    _INFERNUX_LIB_DIR,
):
    if os.path.isdir(_runtime_import_dir) and _runtime_import_dir not in sys.path:
        sys.path.append(_runtime_import_dir)
_RUNTIME_ROOT = _CORE_RUNTIME_DIR
os.environ["_INFERNUX_PLAYER_RUNTIME_ROOT"] = _RUNTIME_ROOT
os.environ["_INFERNUX_PACKAGED_RESOURCE_ROOT"] = os.path.join(
    _CORE_RUNTIME_DIR, "Infernux", "resources"
)

_CONTENT_ARCHIVE = os.path.join(_DATA_ROOT, "Content.inxpkg")
_DATA_DIR = _extract_cached_archive(_CONTENT_ARCHIVE, "content")
_mark_boot_phase("content_ready")
_BUILD_MANIFEST_PATH = os.path.join(_DATA_ROOT, "BuildManifest.json")
if os.path.isfile(_BUILD_MANIFEST_PATH):
    _copy_player_file_atomic(
        _BUILD_MANIFEST_PATH,
        os.path.join(_DATA_DIR, "BuildManifest.json"),
    )

_PARALLEL_ARCHIVE = os.path.join(_DATA_ROOT, "Modules", "Parallel.inxmod")
_RUNTIME_MODULE_DIR = ""
_DLL_DIR_HANDLES = []

_PARALLEL_RUNTIME_READY = False

def _register_player_dll_directory(_dll_dir):
    if sys.platform != "win32" or not os.path.isdir(_dll_dir):
        return
    try:
        _DLL_DIR_HANDLES.append(os.add_dll_directory(_dll_dir))
    except OSError:
        pass

def _ensure_parallel_runtime():
    """Mount the optional Numba/LLVM payload only when a script imports it."""
    global _PARALLEL_RUNTIME_READY, _RUNTIME_MODULE_DIR
    if _PARALLEL_RUNTIME_READY or not os.path.isfile(_PARALLEL_ARCHIVE):
        return _RUNTIME_MODULE_DIR
    # Python's import lock serializes finder callbacks, so another lock would
    # only enlarge the tiny pre-runtime bootstrap closure.
    _RUNTIME_MODULE_DIR = _extract_cached_archive(
        _PARALLEL_ARCHIVE,
        "parallel",
        {"numba", "llvmlite", "numba.libs", "llvmlite.libs"},
    )
    if _RUNTIME_MODULE_DIR not in sys.path:
        sys.path.insert(0, _RUNTIME_MODULE_DIR)
    for _parallel_dll_dir in (
        _RUNTIME_MODULE_DIR,
        os.path.join(_RUNTIME_MODULE_DIR, "llvmlite", "binding"),
        os.path.join(_RUNTIME_MODULE_DIR, "llvmlite.libs"),
    ):
        _register_player_dll_directory(_parallel_dll_dir)
    _PARALLEL_RUNTIME_READY = True
    _mark_boot_phase("parallel_ready")
    return _RUNTIME_MODULE_DIR

class _ParallelRuntimeFinder:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if root not in {"numba", "llvmlite"}:
            return None
        _ensure_parallel_runtime()
        # Returning None resumes the normal finder chain with the newly
        # mounted module directory. Avoid recursively calling find_spec here.
        return None

if os.path.isfile(_PARALLEL_ARCHIVE):
    sys.meta_path.insert(0, _ParallelRuntimeFinder())
_mark_boot_phase("parallel_deferred")

if sys.platform == "win32":
    for _dll_dir in (
        _PLAYER_ROOT,
        _CORE_RUNTIME_DIR,
        _STDLIB_RUNTIME_DIR,
        _INFERNUX_LIB_DIR,
        os.path.join(_CORE_RUNTIME_DIR, "numpy.libs"),
    ):
        _register_player_dll_directory(_dll_dir)

_STATE_HOME = (
    os.environ.get("LOCALAPPDATA", "").strip()
    or os.environ.get("XDG_STATE_HOME", "").strip()
    or os.path.join(os.path.expanduser("~"), ".local", "state")
)
_PLAYER_STATE_ROOT = os.path.join(
    _STATE_HOME, "Infernux", "Players", _SAFE_GAME_NAME
)
_LOGS_DIR = os.path.join(_PLAYER_STATE_ROOT, "Logs")
_LOG = os.path.join(_LOGS_DIR, "player.log")
os.environ["_INFERNUX_PLAYER_LOG"] = _LOG
os.makedirs(_LOGS_DIR, exist_ok=True)

if _DEBUG_MODE:
    _DEBUG_LOG = os.path.join(_LOGS_DIR, _SAFE_GAME_NAME + "_debug.log")
    _debug_fh = open(_DEBUG_LOG, "w", encoding="utf-8")
    sys.stdout = _debug_fh
    sys.stderr = _debug_fh

def _log(_message):
    try:
        with open(_LOG, "a", encoding="utf-8") as _stream:
            _stream.write(str(_message) + "\\n")
    except OSError:
        pass
    if _DEBUG_MODE:
        print(_message, flush=True)

def _crash_report(_exc):
    try:
        _traceback = __import__("traceback").format_exc()
    except Exception:
        _traceback = type(_exc).__name__ + ": " + repr(_exc)
    _log("CRASH: " + _traceback)
    try:
        with open(os.path.join(_LOGS_DIR, "crash.log"), "w", encoding="utf-8") as _stream:
            _stream.write(_traceback)
    except OSError:
        pass
    if os.environ.get("_INFERNUX_PLAYER_CONTROL_FILE"):
        return
    try:
        _NATIVE_PACK._inxplayer_show_error(
            "Infernux Error",
            "Failed to start. Details in crash.log\\n\\n" + _traceback[-800:],
        )
    except Exception:
        pass

try:
    _log(
        "boot phases: "
        + ", ".join(_name + "=" + format(_elapsed, ".3f") + "s" for _name, _elapsed in _BOOT_PHASES)
    )
    _log("boot: importing run_player")
    from Infernux.engine import run_player
    from Infernux.lib import LogLevel
    _log("boot: imports ready at " + format(time.perf_counter() - _BOOT_STARTED, ".3f") + "s")
    _log("boot: calling run_player")
    run_player(
        project_path=_DATA_DIR,
        engine_log_level=LogLevel.Debug if _DEBUG_MODE else LogLevel.Info,
    )
    _log("boot: run_player returned")
except Exception as _exc:
    _crash_report(_exc)
    sys.exit(1)
finally:
    if _DEBUG_MODE:
        try:
            _debug_fh.close()
        except Exception:
            pass
'''
        boot_src = boot_src.replace(
            "__INFERNUX_DEBUG_MODE__",
            "True" if self.debug_mode else "False",
            1,
        )
        boot_src = boot_src.replace(
            "def _extract_cached_archive(",
            _PLAYER_CACHE_PUBLISH_SOURCE
            + "\n\n\ndef _extract_cached_archive(",
            1,
        )
        boot_dir = os.path.join(self.output_dir, self._BUILD_TEMP_DIR_NAME)
        os.makedirs(boot_dir, exist_ok=True)
        boot_path = os.path.join(boot_dir, "boot.py")
        with open(boot_path, "w", encoding="utf-8") as f:
            f.write(boot_src)
        return boot_path

    # ------------------------------------------------------------------
    # Nuitka compilation
    # ------------------------------------------------------------------

    def _run_nuitka(
        self,
        boot_script: str,
        on_progress: Optional[Callable[[str, float], None]],
        user_packages: Optional[List[str]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Invoke NuitkaBuilder. Returns the dist directory path."""
        # NumPy and packaging are part of the engine runtime.  The latter is
        # required by the runtime PluginManager for version/specifier checks;
        # both must remain importable from the source-less runtime archive.
        # Numba and llvmlite remain conditional because only the public JIT
        # path needs their bytecode-preserving raw package copies.
        jit_set = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES
        all_pkgs = user_packages or []
        compiled_pkgs = [p for p in all_pkgs if p not in jit_set]
        raw_pkgs = {"numpy", "packaging"}

        player_icon = self.icon_path
        if not player_icon:
            candidate = os.path.join(
                _resources.get_package_resources_path(), "icons", "icon.png"
            )
            if os.path.isfile(candidate):
                player_icon = candidate

        nk = NuitkaBuilder(
            entry_script=boot_script,
            output_dir=self.output_dir,
            output_filename=(
                "_InfernuxPlayer.pyd"
                if sys.platform == "win32"
                else "_InfernuxPlayer.so"
            ),
            product_name="Infernux Player",
            icon_path=player_icon or None,
            extra_include_packages=compiled_pkgs,
            extra_requirements_files=self._project_requirement_files(),
            raw_copy_packages=sorted(raw_pkgs),
            # Release wheels prebuild this dependency closure into the base
            # Runtime Pack, while the packages themselves remain in a small
            # optional module selected by the user's build setting.
            runtime_support_packages=["numba", "llvmlite"],
            console_mode="force" if self.debug_mode else "disable",
            lto=self.lto,
            # Runtime compilation is independent from Data/ project content
            # and product branding. The generic Player is renamed after the
            # prebuilt pack is restored.
            runtime_pack_cache=True,
            player_module=True,
        )

        def _nk_progress(msg: str, pct: float):
            # Map Nuitka's 0–1 range into our 0.06–0.85 range
            mapped = 0.06 + pct * 0.79
            if on_progress:
                on_progress(msg, mapped)

        dist_dir = nk.build(on_progress=_nk_progress, cancel_event=cancel_event)
        if self.enable_jit:
            if on_progress:
                on_progress(t("build.step.injecting_jit"), 0.85)
            if not nk.install_runtime_module(
                dist_dir,
                module_name="parallel",
                packages=["numba", "llvmlite"],
                archive_only=True,
                profile=self._player_inxpack_profile(),
            ):
                raise RuntimeError("Unable to stage the parallel Runtime Module")
        return dist_dir

    def _player_host_path(self) -> str:
        """Resolve the native single-process host; fail closed if absent."""

        if sys.platform != "win32":
            raise RuntimeError("PlayerHost packaging is currently supported on Windows only")
        candidate = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "resources",
            "player_runtime",
            "InfernuxPlayerHost.exe",
        )
        if not os.path.isfile(candidate):
            raise RuntimeError(
                "InfernuxPlayerHost.exe is missing; refusing to package a legacy "
                "Nuitka executable Player"
            )
        return candidate

    # ------------------------------------------------------------------
    # Organize output: move dist contents to the final output directory
    # ------------------------------------------------------------------

    def _organize_output(self, dist_dir: str) -> str:
        """Move Nuitka dist contents from staging into self.output_dir.

        The dist_dir lives in an ASCII-safe staging area (e.g.
        ``C:\\_InxBuild\\<hash>\\boot.dist``).  We move every item
        into the user's chosen output directory.
        Returns the final directory path.
        """
        final_dir = self.output_dir
        os.makedirs(final_dir, exist_ok=True)

        _move_t0 = time.perf_counter()

        if sys.platform == "win32":
            # robocopy /MOVE /E is dramatically faster than per-item
            # shutil.move for large directory trees (native NTFS ops).
            rc = subprocess.call(
                ["robocopy", dist_dir, final_dir, "/E", "/MOVE",
                 "/MT:16", "/R:1", "/W:1", "/XJ",
                 "/COPY:DAT", "/DCOPY:DAT",
                 "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            if rc >= 8:
                Debug.log_warning(
                    f"robocopy /MOVE failed (exit {rc}), falling back to Python move"
                )
                for item in os.listdir(dist_dir):
                    src = os.path.join(dist_dir, item)
                    dst = os.path.join(final_dir, item)
                    if os.path.exists(dst):
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    shutil.move(src, dst)
        else:
            for item in os.listdir(dist_dir):
                src = os.path.join(dist_dir, item)
                dst = os.path.join(final_dir, item)
                if os.path.exists(dst):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    else:
                        os.remove(dst)
                shutil.move(src, dst)

        game_name = f"{self.project_name}.exe" if sys.platform == "win32" else self.project_name
        game_executable = os.path.join(final_dir, game_name)
        if sys.platform == "win32":
            host = self._player_host_path()
            if os.path.exists(game_executable):
                os.remove(game_executable)
            shutil.copy2(host, game_executable)
        elif not os.path.isfile(game_executable):
            raise RuntimeError("PlayerHost packaging requires a native game entry point")

        Debug.log_internal(
            f"  moved dist to output in {time.perf_counter() - _move_t0:.2f}s"
        )

        # Remove the now-empty staging parent
        staging_parent = os.path.dirname(dist_dir)
        if sys.platform == "win32":
            subprocess.run(
                ["cmd", "/c", "rd", "/s", "/q", staging_parent],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            shutil.rmtree(staging_parent, ignore_errors=True)

        return final_dir

    def _organize_player_layout(self, final_dir: str) -> None:
        """Create the single-entry <Game>_Data layout before packing."""

        data_source = os.path.join(final_dir, "Data")
        if not os.path.isdir(data_source):
            raise RuntimeError(
                "Player layout organization requires the current Data staging directory"
            )
        if os.path.isdir(os.path.join(final_dir, "RuntimeModules")):
            raise RuntimeError(
                "Legacy RuntimeModules layout detected; native module staging must "
                "produce Parallel.inxmod at the final package root"
            )

        data_name = f"{self.project_name}_Data"
        data_root = os.path.join(final_dir, data_name)
        if os.path.exists(data_root):
            raise RuntimeError(f"Player Data target already exists: {data_root}")
        os.replace(data_source, data_root)

        executable_names = [
            name
            for name in os.listdir(final_dir)
            if name.casefold().endswith(".exe")
            and os.path.isfile(os.path.join(final_dir, name))
        ]
        expected_executable = f"{self.project_name}.exe"
        if executable_names != [expected_executable]:
            raise RuntimeError(
                "Player layout requires exactly one root executable named "
                f"{expected_executable}; found {executable_names}"
            )

    @staticmethod
    def _windows_icon_resource(icon_path: str) -> bytes:
        """Convert a supported source image to a multi-resolution ICO payload."""
        if os.path.splitext(icon_path)[1].lower() == ".ico":
            with open(icon_path, "rb") as source:
                return source.read()

        try:
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise RuntimeError(
                "Pillow is required to convert the configured Windows build icon"
            ) from exc

        with Image.open(icon_path) as source:
            image = source.convert("RGBA")
            image = ImageOps.contain(image, (256, 256), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            canvas.alpha_composite(
                image,
                ((256 - image.width) // 2, (256 - image.height) // 2),
            )
            output = io.BytesIO()
            canvas.save(
                output,
                format="ICO",
                sizes=((256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)),
            )
            return output.getvalue()

    @classmethod
    def _apply_windows_executable_icon(cls, executable: str, icon_path: str) -> None:
        """Replace the copied PlayerHost icon without rebuilding the runtime."""
        if sys.platform != "win32":
            return

        import ctypes
        from ctypes import wintypes

        payload = cls._windows_icon_resource(icon_path)
        if len(payload) < 6:
            raise RuntimeError(f"Build icon is not a valid ICO payload: {icon_path}")
        reserved, icon_type, count = struct.unpack_from("<HHH", payload, 0)
        if reserved != 0 or icon_type != 1 or count == 0 or len(payload) < 6 + count * 16:
            raise RuntimeError(f"Build icon is not a valid ICO payload: {icon_path}")

        images: list[tuple[int, bytes]] = []
        group_entries = bytearray(struct.pack("<HHH", 0, 1, count))
        for index in range(count):
            entry = struct.unpack_from("<BBBBHHII", payload, 6 + index * 16)
            width, height, colors, entry_reserved, planes, bits, byte_count, offset = entry
            end = offset + byte_count
            if offset < 6 + count * 16 or end > len(payload):
                raise RuntimeError(f"Build icon contains an invalid image entry: {icon_path}")
            resource_id = index + 1
            images.append((resource_id, payload[offset:end]))
            group_entries.extend(
                struct.pack(
                    "<BBBBHHIH",
                    width,
                    height,
                    colors,
                    entry_reserved,
                    planes,
                    bits,
                    byte_count,
                    resource_id,
                )
            )

        kernel32 = ctypes.windll.kernel32
        kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
        kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
        kernel32.UpdateResourceW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.WORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.UpdateResourceW.restype = wintypes.BOOL
        kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
        kernel32.EndUpdateResourceW.restype = wintypes.BOOL

        update = kernel32.BeginUpdateResourceW(executable, False)
        if not update:
            raise RuntimeError(
                f"Unable to open Player launcher icon resources (Windows error {ctypes.get_last_error()})"
            )

        buffers = []
        committed = False
        try:
            # The bundled launcher uses group 101. Update both neutral and
            # English resources so Explorer cannot select the stale group.
            for language in (0, 0x0409):
                for resource_id, image_bytes in images:
                    image_buffer = ctypes.create_string_buffer(image_bytes)
                    buffers.append(image_buffer)
                    if not kernel32.UpdateResourceW(
                        update,
                        ctypes.c_void_p(3),
                        ctypes.c_void_p(resource_id),
                        language,
                        image_buffer,
                        len(image_bytes),
                    ):
                        raise RuntimeError(
                            f"Unable to write Player icon image (Windows error {ctypes.get_last_error()})"
                        )
                group_buffer = ctypes.create_string_buffer(bytes(group_entries))
                buffers.append(group_buffer)
                if not kernel32.UpdateResourceW(
                    update,
                    ctypes.c_void_p(14),
                    ctypes.c_void_p(101),
                    language,
                    group_buffer,
                    len(group_entries),
                ):
                    raise RuntimeError(
                        f"Unable to write Player icon group (Windows error {ctypes.get_last_error()})"
                    )
            if not kernel32.EndUpdateResourceW(update, False):
                raise RuntimeError(
                    f"Unable to commit Player icon resources (Windows error {ctypes.get_last_error()})"
                )
            committed = True
        finally:
            if not committed:
                kernel32.EndUpdateResourceW(update, True)

    def _process_build_icon(self, final_dir: str) -> None:
        """Stage the project icon for the runtime window and taskbar."""
        self._built_icon_path = ""
        if not self.icon_path:
            return
        extension = os.path.splitext(self.icon_path)[1].lower()
        branding_dir = os.path.join(final_dir, "Data", "Branding")
        os.makedirs(branding_dir, exist_ok=True)
        destination = os.path.join(branding_dir, "icon" + extension)
        shutil.copy2(self.icon_path, destination)
        self._built_icon_path = portable_path(relative_path(destination, os.path.join(final_dir, "Data")))

    # ------------------------------------------------------------------
    # Game data
    # ------------------------------------------------------------------

    def _copy_game_data(self, final_dir: str):
        """Copy authored data and selected runtime artifacts to Data/."""
        self._runtime_artifact_bindings = {}
        self._runtime_artifact_source_paths = set()
        data_dir = os.path.join(final_dir, "Data")
        # Runtime settings are an explicit whitelist. Recursively copying the
        # authoring ProjectSettings directory would make every future Editor
        # or MCP document an accidental Player dependency.
        for relative in sorted(self._GAME_DATA_FILES):
            source = os.path.join(self.project_path, *relative.split("/"))
            if not os.path.isfile(source):
                continue
            destination = os.path.join(data_dir, *relative.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)

        self._copy_cooked_assets(data_dir)
        self._prune_player_editor_data(data_dir)
        self._stage_player_plugins(data_dir)
        self._stage_library_runtime_artifacts(data_dir)
        self._stage_library_runtime_documents(data_dir)

        self._copy_reachable_particle_artifacts(data_dir)
        self._copy_particle_data_interface_artifacts(data_dir)

        self._filter_shipped_requirements(data_dir)

    def _stage_player_plugins(self, data_dir: str) -> None:
        """Stage enabled package files by GUID and structural Runtime policy."""

        from Infernux.plugins.package import player_file_exported
        from Infernux.plugins.registry import PluginRegistry

        registry = PluginRegistry(self.project_path)
        document = registry.load()
        runtime_records: list[dict[str, object]] = []
        assets_root = resolved_path(os.path.join(self.project_path, "Assets"))
        packages_root = resolved_path(os.path.join(self.project_path, "Packages"))
        indexed = {
            str(entry.get("guid", "")).casefold(): entry
            for entry in self._asset_index_entries()
            if str(entry.get("guid", "")).strip()
        }
        for raw in document.get("installed", []):
            if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
                continue
            record = dict(raw)
            runtime_files: list[dict[str, object]] = []
            for raw_file in record.get("files", []):
                if not isinstance(raw_file, dict):
                    continue
                logical = portable_path(str(raw_file.get("logical_path", ""))).strip("/")
                if not logical or not player_file_exported(record, logical):
                    continue
                guid = str(raw_file.get("guid", "")).casefold()
                entry = indexed.get(guid)
                if entry is None:
                    raise RuntimeError(
                        f"Player plugin asset GUID is absent from AssetIndex: "
                        f"{record.get('reference', '')}:{guid}"
                    )
                source = self._library_source_entry_path(entry)
                if not (
                    is_path_within(source, assets_root, allow_root=False)
                    or is_path_within(source, packages_root, allow_root=False)
                ):
                    raise RuntimeError(
                        f"Player plugin GUID resolved outside Assets/Packages: {guid}: {source}"
                    )
                current_relative = portable_path(relative_path(source, self.project_path))
                file_record = dict(raw_file)
                file_record["path_hint"] = current_relative
                runtime_files.append(file_record)
                if is_path_within(source, packages_root, allow_root=False):
                    destination = os.path.join(data_dir, *current_relative.split("/"))
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(source, destination)
                    meta = source + ".meta"
                    if not os.path.isfile(meta):
                        raise RuntimeError(f"Player plugin asset has no GUID metadata: {source}")
                    shutil.copy2(meta, destination + ".meta")
            if not runtime_files:
                continue
            record["files"] = runtime_files
            runtime_records.append(record)

        runtime_registry = {
            "$schema": document.get("$schema", "infernux.plugin_registry"),
            "registry_version": document.get("registry_version", 2),
            # Discovery/install sources are Editor concerns. A Player only
            # needs the lifecycle records selected above.
            "packages": [],
            "installed": runtime_records,
        }
        _write_json_atomic(
            os.path.join(data_dir, "ProjectSettings", "InxPlugins.json"),
            runtime_registry,
        )

    @staticmethod
    def _compile_player_plugin_scripts(final_dir: str) -> None:
        """Compile exported package scripts without exposing source."""

        root = os.path.join(final_dir, "Data", "Packages")
        if not os.path.isdir(root):
            return
        for directory, _folders, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                source = os.path.join(directory, filename)
                try:
                    py_compile.compile(
                        source,
                        cfile=source + "c",
                        dfile=relative_path(source, os.path.join(final_dir, "Data")),
                        optimize=2,
                        doraise=True,
                    )
                    os.remove(source)
                except py_compile.PyCompileError as exc:
                    raise RuntimeError(
                        f"Player plugin script compilation failed: {source}: {exc}"
                    ) from exc

    def _copy_cooked_assets(self, data_dir: str) -> None:
        """Stage every current project asset selected from the AssetIndex."""

        try:
            entries = self._asset_index_entries()
            if not entries and self._full_build_validated:
                raise RuntimeError(
                    "Player asset cook selected no runtime assets; refresh Assets "
                    "and add at least one valid build scene"
                )
            selected = self._collect_library_asset_entries(entries)
        except RuntimeArtifactError as exc:
            raise RuntimeError(f"Player asset cook failed: {exc}") from exc

        self._cooked_asset_entries = dict(selected)

        assets_root = resolved_path(os.path.join(self.project_path, "Assets"))
        builtin_resources_roots = self._builtin_resource_roots()
        copied: set[str] = set()

        def copy_source(source_path: str, *, reason: str) -> None:
            source = resolved_path(source_path)
            if not is_path_within(source, assets_root, allow_root=False):
                raise RuntimeError(
                    f"Player asset cook source is outside Assets ({reason}): {source}"
                )
            if not os.path.isfile(source):
                raise RuntimeError(
                    f"Player asset cook source is missing ({reason}): {source}"
                )
            if source.casefold().endswith(".meta"):
                return
            relative = relative_path(source, self.project_path).replace("\\", "/")
            key = relative.casefold()
            if key in copied:
                return
            destination = os.path.join(data_dir, *relative.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
            copied.add(key)

        for guid in sorted(selected):
            entry = selected[guid]
            source = self._library_source_entry_path(entry)
            validation_root = self.project_path
            if not is_path_within(source, validation_root, allow_root=False):
                validation_root = next(
                    (
                        root
                        for root in builtin_resources_roots
                        if is_path_within(source, root, allow_root=False)
                    ),
                    validation_root,
                )
            try:
                source_fingerprint(validation_root, entry)
            except RuntimeArtifactError as exc:
                raise RuntimeError(
                    f"Player asset cook rejected stale AssetIndex entry {guid}: {exc}"
                ) from exc
            if str(entry.get("artifact_path", "") or "").strip():
                # The current Library artifact replaces its authoring source;
                # _stage_library_runtime_artifacts validates and stages it.
                continue
            if is_path_within(source, assets_root, allow_root=False):
                copy_source(source, reason=f"AssetIndex GUID {guid}")
                continue
            if bool(entry.get("read_only", False)) and any(
                is_path_within(source, root, allow_root=False)
                for root in builtin_resources_roots
            ):
                # Built-in resources are supplied once by Runtime.inxrt. They
                # may participate in the GUID closure, but must never be
                # duplicated into project Content or treated as author files.
                continue
            raise RuntimeError(
                "Player asset cook selected an unsupported source outside "
                f"Assets: guid={guid}, source={source}"
            )

        if not copied and self._full_build_validated:
            raise RuntimeError(
                "Player asset cook selected no runtime assets; refresh Assets "
                "and add at least one valid build scene"
            )

    @classmethod
    def _is_player_editor_path(cls, relative: str) -> bool:
        """Return whether a staged path belongs exclusively to the Editor."""

        normalized = str(relative).replace("\\", "/").lstrip("/").casefold()
        return normalized in {
            path.casefold() for path in cls._PLAYER_EXCLUDED_CONTENT_RELATIVE_PATHS
        }

    def _prune_player_editor_data(self, data_dir: str) -> None:
        """Remove known Editor services before any Player content is cooked."""

        for relative in sorted(self._PLAYER_EXCLUDED_CONTENT_RELATIVE_PATHS):
            candidate = os.path.join(data_dir, *relative.split("/"))
            if not os.path.isfile(candidate):
                continue
            try:
                os.remove(candidate)
            except FileNotFoundError:
                continue
        self._remove_empty_directory_tree(os.path.join(data_dir, "Logs"))

    @staticmethod
    def _asset_reference_values(value, known_guids: frozenset[str] = frozenset()):
        if isinstance(value, dict):
            if value.get("$type") == "asset_ref":
                guid = value.get("guid", "")
                path_hint = value.get("path_hint", "")
                if isinstance(guid, str) and isinstance(path_hint, str) and (guid or path_hint):
                    yield guid, path_hint
            for nested in value.values():
                yield from GameBuilder._asset_reference_values(nested, known_guids)
        elif isinstance(value, list):
            for nested in value:
                yield from GameBuilder._asset_reference_values(nested, known_guids)
        elif isinstance(value, str):
            if value in known_guids:
                # Native scene/component serializers use compact GUID scalars
                # for fields such as MeshRenderer.materials.
                yield value, ""
                return
            if value.startswith("python:"):
                # Python component identities embed the owning script GUID in
                # the type id instead of storing a separate asset reference.
                fields = value.split(":", 2)
                if len(fields) == 3 and fields[1] in known_guids:
                    yield fields[1], ""

    def _load_json_asset_references(
        self,
        source_path: str,
        *,
        known_guids: frozenset[str] = frozenset(),
    ) -> set[tuple[str, str]]:
        try:
            with open(source_path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Player cook cannot read indexed runtime document: {source_path}"
            ) from exc
        return set(self._asset_reference_values(value, known_guids))

    def _library_source_entry_path(self, entry: dict) -> str:
        raw = str(entry.get("normalized_path", "")).replace("\\", "/")
        candidate = raw if os.path.isabs(raw) else os.path.join(self.project_path, raw)
        return resolved_path(candidate)

    def _builtin_resource_roots(self) -> tuple[str, ...]:
        """Return every source root that may own indexed engine resources.

        Graphical Editor startup mirrors resources into ``Library/Resources``
        before importing them. A caller-controlled Headless host can index the
        package resource tree directly instead. Both identities describe the
        same immutable Runtime.inxrt payload and must produce the same cooked
        GUID catalog.
        """

        candidates = (
            os.path.join(self.project_path, "Library", "Resources"),
            getattr(_resources, "resources_path", ""),
            _resources.get_package_resources_path(),
        )
        roots: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            root = resolved_path(candidate)
            identity = path_key(root)
            if identity in seen:
                continue
            seen.add(identity)
            roots.append(root)
        return tuple(roots)

    def _builtin_resource_relative_path(self, source_path: str) -> str:
        source = resolved_path(source_path)
        for root in self._builtin_resource_roots():
            if is_path_within(source, root, allow_root=False):
                return relative_path(source, root).replace("\\", "/")
        return ""

    def _collect_library_asset_entries(self, entries: list[dict]) -> dict[str, dict]:
        """Select every current imported product beneath ``Assets``.

        Player builds deliberately use a conservative content policy while
        the engine is still evolving: authoring sources are converted to
        Library products, but no scene-reachability analysis is allowed to
        remove a project asset.  Runtime script choices, RenderStack pipeline
        providers and effect groups are dynamic and cannot be proven from a
        static scene closure without changing Editor behavior.

        Direct AssetIndex dependencies are still followed so a selected
        project product can retain read-only engine artifacts it explicitly
        references.  Engine shaders are also symbolic runtime entry points:
        RenderGraph and FullscreenRenderer address hidden stages by ShaderInfo
        name, so no serialized project GUID can express those edges.  Preserve
        every read-only built-in shader identity in the Player catalog while
        keeping its bytes exclusively in Runtime.inxrt.  This is validation
        and identity propagation, not content pruning.
        """

        by_guid = {str(item["guid"]): item for item in entries}
        assets_root = resolved_path(os.path.join(self.project_path, "Assets"))
        indexed_source_paths = {
            path_key(self._library_source_entry_path(entry))
            for entry in entries
        }
        for configured_scene in load_build_settings(self.project_path).get("scenes", []):
            scene_path = self._resolve_build_scene_path(configured_scene)
            if path_key(scene_path) not in indexed_source_paths:
                raise RuntimeError(
                    "BuildSettings scene is absent from the current AssetIndex: "
                    f"{configured_scene}"
                )
        roots = {
            str(entry["guid"])
            for entry in entries
            if is_path_within(
                self._library_source_entry_path(entry),
                assets_root,
                allow_root=False,
            )
        }
        builtin_shader_roots = tuple(
            resolved_path(os.path.join(root, "shaders"))
            for root in self._builtin_resource_roots()
        )
        roots.update(
            str(entry["guid"])
            for entry in entries
            if bool(entry.get("read_only", False))
            and logical_asset_type(entry) == "shader"
            and any(
                is_path_within(
                    self._library_source_entry_path(entry),
                    builtin_shader_root,
                    allow_root=False,
                )
                for builtin_shader_root in builtin_shader_roots
            )
        )

        selected: dict[str, dict] = {}
        pending = sorted(roots)
        while pending:
            guid = pending.pop(0)
            if guid in selected:
                continue
            if guid not in by_guid:
                raise RuntimeError(
                    "AssetIndex dependency is absent from the current catalog: "
                    f"{guid}"
                )
            entry = by_guid[guid]
            selected[guid] = entry
            for dependency in sorted(entry.get("dependencies", [])):
                if dependency not in selected:
                    pending.append(str(dependency))
        return selected

    def _add_asset_reference_roots(
        self,
        roots: set[str],
        source_path: str,
        *,
        by_guid: dict[str, dict],
        by_path: dict[str, dict],
        known_guids: frozenset[str],
    ) -> None:
        for guid, path_hint in self._load_json_asset_references(
            source_path,
            known_guids=known_guids,
        ):
            if guid and guid in by_guid:
                roots.add(guid)
            elif path_hint:
                candidate = path_hint.replace("\\", "/").casefold()
                roots.update(
                    str(item["guid"])
                    for key, item in by_path.items()
                    if key.endswith(candidate)
                )

    def _ensure_selected_particle_artifacts(self, selected: dict[str, dict]) -> None:
        """Compile any selected ParticleGraph whose Library product is missing."""
        from Infernux.engine.project_context import using_project_root
        from Infernux.particle.artifact import ParticleArtifactRegistry

        with using_project_root(self.project_path):
            for guid, entry in selected.items():
                if logical_asset_type(entry) != "particlegraph":
                    continue
                source_path = self._library_source_entry_path(entry)
                try:
                    ParticleArtifactRegistry.ensure_source_compiled(
                        source_path, guid=str(guid)
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Library particle artifact compile failed: {source_path}: {exc}"
                    ) from exc

    def _particle_library_artifacts(self) -> dict[str, dict]:
        index_path = os.path.join(
            self.project_path, "Library", "Artifacts", "Particle", self._PARTICLE_RUNTIME_INDEX_FILENAME
        )
        if not os.path.isfile(index_path):
            return {}
        try:
            with open(index_path, "r", encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Particle RuntimeIndex is unreadable: {index_path}") from exc
        result = {}
        for item in document.get("entries", []):
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("guid", "path_hint", "stable_id")):
                raise RuntimeError("Particle RuntimeIndex contains an invalid entry")
            result[item["guid"] or item["path_hint"].replace("\\", "/").casefold()] = item
        return result

    def _stage_library_runtime_artifacts(self, data_dir: str) -> None:
        """Stage current compiled artifacts and remember source replacements."""

        try:
            entries = self._asset_index_entries()
            selected = self._collect_library_asset_entries(entries)
        except RuntimeArtifactError as exc:
            raise RuntimeError(f"Library artifact selection failed: {exc}") from exc

        by_guid = {str(entry["guid"]): entry for entry in entries}
        self._ensure_selected_particle_artifacts(selected)
        particle_index = self._particle_library_artifacts()
        copied: set[str] = set()
        for guid in sorted(selected):
            entry = selected[guid]
            source_path = self._library_source_entry_path(entry)
            asset_type = logical_asset_type(entry)
            artifact_paths: list[str] = []
            relative_artifact = str(entry.get("artifact_path", "")).replace("\\", "/")
            if relative_artifact:
                artifact_paths.append(relative_artifact)
            elif asset_type == "particlegraph":
                particle = particle_index.get(guid)
                if particle is None:
                    source_relative = relative_path(source_path, self.project_path).replace("\\", "/").casefold()
                    particle = next(
                        (
                            item
                            for item in particle_index.values()
                            if str(item.get("path_hint", "")).replace("\\", "/").casefold()
                            in {source_relative, f"assets/{source_relative}"}
                            or source_relative.endswith(
                                str(item.get("path_hint", "")).replace("\\", "/").casefold()
                            )
                        ),
                        None,
                    )
                stable_id = (
                    particle["stable_id"]
                    if particle is not None
                    else self._particle_source_stable_id(source_path)
                )
                artifact_paths.append(
                    self._particle_artifact_relative(guid, stable_id)
                )
            if not artifact_paths:
                if asset_type in {"texture", "mesh", "particlegraph"}:
                    raise RuntimeError(
                        "Library asset has no compiled artifact path: "
                        f"guid={guid}, source={source_path}"
                    )
                continue

            binding = None
            for relative_artifact in artifact_paths:
                artifact_path = resolved_path(os.path.join(self.project_path, relative_artifact))
                try:
                    binding = validate_artifact(self.project_path, entry, artifact_path)
                except RuntimeArtifactError as exc:
                    raise RuntimeError(f"Library artifact selection failed: {exc}") from exc
                artifact_name = relative_artifact.replace("\\", "/")
                destination = os.path.join(data_dir, "Library", "Artifacts", *artifact_name.split("/")[2:])
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                if artifact_name not in copied:
                    shutil.copy2(artifact_path, destination)
                    copied.add(artifact_name)
                source_relative = relative_path(source_path, self.project_path).replace("\\", "/")
                self._runtime_artifact_source_paths.add(source_relative.casefold())
                binding = dict(binding)
                binding["runtime_path"] = f"Library/Artifacts/{artifact_name.split('/', 2)[-1]}" if artifact_name.startswith("Library/Artifacts/") else artifact_name
                binding["dependencies"] = []
                for dependency_guid in sorted(entry.get("dependencies", [])):
                    dependency = by_guid.get(str(dependency_guid))
                    if dependency is None:
                        raise RuntimeError(f"Library artifact dependency GUID is missing: {dependency_guid}")
                    dependency_path = str(dependency.get("artifact_path", "")).replace("\\", "/")
                    if dependency_path:
                        binding["dependencies"].append(
                            runtime_artifact_id("Content.inxpkg", dependency_path)
                        )
                self._runtime_artifact_bindings[f"Library/Artifacts/{artifact_name.split('/', 2)[-1]}"] = binding

            if asset_type == "mesh":
                skin_relative = f"Library/Artifacts/SkinnedMesh/{guid}.inxskin"
                skin_path = resolved_path(os.path.join(self.project_path, skin_relative))
                if os.path.isfile(skin_path):
                    skin_binding = validate_artifact(self.project_path, entry, skin_path)
                    destination = os.path.join(data_dir, "Library", "Artifacts", "SkinnedMesh", f"{guid}.inxskin")
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(skin_path, destination)
                    skin_binding["runtime_path"] = skin_relative
                    skin_binding["dependencies"] = list(binding.get("dependencies", [])) if binding else []
                    self._runtime_artifact_bindings[skin_relative] = skin_binding

    def _stage_library_runtime_documents(self, data_dir: str) -> None:
        """Cook remaining runtime-ready project payloads into Library.

        Serialized resources still use their established loader formats, but
        the Player consumes a deterministic GUID-addressed build artifact
        instead of the authoring Assets path. This is intentionally separate
        from importer-produced texture, mesh and particle artifacts.
        """

        assets_root = resolved_path(os.path.join(self.project_path, "Assets"))
        compiled_guids = {
            str(binding.get("source_guid", ""))
            for binding in self._runtime_artifact_bindings.values()
            if isinstance(binding, dict) and binding.get("source_guid")
        }
        for guid, entry in sorted(
            getattr(self, "_cooked_asset_entries", {}).items()
        ):
            guid = str(guid)
            if guid in compiled_guids:
                continue
            if any(part in guid for part in ("/", "\\")) or guid in {".", ".."}:
                raise RuntimeError(f"Player asset GUID is not path-safe: {guid!r}")

            source_path = self._library_source_entry_path(entry)
            if not is_path_within(source_path, assets_root, allow_root=False):
                continue
            suffix = Path(source_path).suffix.casefold()
            if suffix == ".py":
                # User scripts are compiled after data staging.
                continue

            source_relative = relative_path(
                source_path, self.project_path
            ).replace("\\", "/")
            logical_type = logical_type_for_path(source_relative)
            payload_kind = payload_kind_for(logical_type)
            if payload_kind == "serialized_runtime_document":
                artifact_directory = "Document"
            elif logical_type == "audio":
                artifact_directory = "Audio"
            elif payload_kind == "direct_runtime_asset":
                artifact_directory = "Blob"
            else:
                continue

            # Project shaders remain GLSL runtime inputs. They are packed in
            # Content.inxpkg rather than shipped as loose authoring files, so
            # Player uses the exact same dynamic linker as the Editor and can
            # form shader combinations that did not exist at build time.
            artifact_suffix = suffix
            runtime_path = (
                f"Library/Artifacts/{artifact_directory}/{guid}{artifact_suffix}"
            )
            destination = os.path.join(data_dir, *runtime_path.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source_path, destination)
            self._rewrite_player_document_paths(destination, suffix)

            source_state = entry.get("source", {})
            source_fingerprint = {
                "size": int(source_state.get("size", 0)),
                "modified_ns": int(source_state.get("modified_ns", 0)),
                "content_hash": str(entry.get("content_hash", "")),
            }
            artifact_sha256 = self._sha256_file(destination)
            binding = {
                "source_guid": guid,
                "source_path": source_relative,
                "source_fingerprint": source_fingerprint,
                "artifact_path": runtime_path,
                "artifact_sha256": artifact_sha256,
                "artifact_source_hash": str(entry.get("content_hash", "")),
                "dependencies": [],
            }
            self._runtime_artifact_bindings[runtime_path] = binding
            self._runtime_artifact_source_paths.add(source_relative.casefold())

    def _copy_reachable_particle_artifacts(self, data_dir: str) -> None:
        """Publish the runtime index for every imported ParticleGraph.

        The historical method name is retained for internal compatibility,
        but selection follows the same complete-Assets policy as the rest of
        the Player cook.  A graph selected dynamically by a script must not
        disappear merely because no serialized scene referenced it.
        """
        references = self._collect_reachable_particle_artifacts()
        if not references:
            return

        destination_root = os.path.join(
            data_dir, "Library", "Artifacts", "Particle"
        )

        # With the current AssetIndex, _stage_library_runtime_artifacts is the
        # only authority allowed to select and validate a Particle artifact.
        # This method only preserves the runtime lookup index; it must never
        # copy a stale file around that validation path.
        for item in references:
            relative = self._particle_artifact_relative(item["guid"], item["stable_id"])
            destination = os.path.join(destination_root, os.path.basename(relative))
            if not os.path.isfile(destination):
                raise RuntimeError(
                    "Reachable ParticleGraph was not staged from its current "
                    f"Library artifact: {destination}"
                )
        if references:
            os.makedirs(destination_root, exist_ok=True)
            index_path = os.path.join(
                destination_root, self._PARTICLE_RUNTIME_INDEX_FILENAME
            )
            payload = {
                "$schema": "infernux.particle_runtime_index",
                "entries": references,
            }
            _write_json_atomic(index_path, payload)

    def _collect_reachable_particle_stable_ids(self) -> set[str]:
        return {
            item["stable_id"]
            for item in self._collect_reachable_particle_artifacts()
        }

    def _collect_reachable_particle_artifacts(self) -> list[dict[str, str]]:
        settings_path = os.path.join(
            self.project_path, "ProjectSettings", "BuildSettings.json"
        )
        with open(settings_path, "r", encoding="utf-8", errors="replace") as stream:
            settings = json.load(stream)

        references: set[tuple[str, str]] = set()
        for configured_scene in settings.get("scenes", ()):
            scene_path = self._resolve_build_scene_path(configured_scene)
            try:
                with open(scene_path, "r", encoding="utf-8") as stream:
                    scene = json.load(stream)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Build scene is not valid current JSON: {scene_path}"
                ) from exc
            self._collect_particle_asset_references(scene, references)

        # Reuse the same AssetIndex closure as content/artifact staging so a
        # ParticleGraph declared only through an Additional Cook Root also
        # receives a runtime lookup entry.
        live_index_path = os.path.join(
            self.project_path, "Library", "AssetIndex.json"
        )
        if (
            self._asset_index_entries_snapshot is None
            and not os.path.isfile(live_index_path)
        ):
            if references:
                raise RuntimeError(
                    "Player build requires the current Library/AssetIndex.json "
                    "before selecting runtime artifacts"
                )
            return []
        try:
            selected_entries = getattr(self, "_cooked_asset_entries", None)
            if selected_entries is None:
                indexed_entries = self._asset_index_entries()
                selected_entries = self._collect_library_asset_entries(indexed_entries)
        except RuntimeArtifactError as exc:
            raise RuntimeError(f"Particle runtime cook failed: {exc}") from exc
        for entry in selected_entries.values():
            if logical_asset_type(entry) != "particlegraph":
                continue
            source_path = self._library_source_entry_path(entry)
            references.add(
                (
                    str(entry.get("guid", "")),
                    relative_path(source_path, self.project_path).replace("\\", "/"),
                )
            )

        entries: list[dict[str, str]] = []
        guid_index: Optional[dict[str, str]] = None
        for guid, path_hint in sorted(references):
            source_path = self._resolve_particle_source_path(path_hint)
            if source_path is None and guid:
                if guid_index is None:
                    guid_index = self._build_asset_guid_index()
                source_path = guid_index.get(guid)
            if source_path is None:
                raise RuntimeError(
                    "Build scene references a ParticleGraph that cannot be resolved: "
                    f"guid={guid!r}, path_hint={path_hint!r}"
                )
            entries.append(
                {
                    "guid": guid,
                    "path_hint": path_hint.replace("\\", "/"),
                    "stable_id": self._particle_source_stable_id(source_path),
                }
            )
        return entries

    def _particle_artifact_relative(self, guid: str, stable_id: str = "") -> str:
        from Infernux.particle.artifact import particle_artifact_filename

        candidates = []
        if guid:
            candidates.append(
                f"Library/Artifacts/Particle/{particle_artifact_filename(guid)}"
            )
        if stable_id:
            legacy = f"Library/Artifacts/Particle/{particle_artifact_filename(stable_id)}"
            if legacy not in candidates:
                candidates.append(legacy)
        for relative in candidates:
            if os.path.isfile(os.path.join(self.project_path, relative)):
                return relative
        return candidates[0] if candidates else ""

    @classmethod
    def _collect_particle_asset_references(
        cls,
        value,
        references: set[tuple[str, str]],
    ) -> None:
        if type(value) is dict:
            if (
                value.get("$type") == "asset_ref"
                and value.get("asset_type") == "ParticleGraph"
            ):
                guid = value.get("guid", "")
                path_hint = value.get("path_hint", "")
                if type(guid) is not str or type(path_hint) is not str:
                    raise RuntimeError("ParticleGraph asset references must use string identity")
                if guid or path_hint:
                    references.add((guid, path_hint))
            for nested in value.values():
                cls._collect_particle_asset_references(nested, references)
        elif type(value) is list:
            for nested in value:
                cls._collect_particle_asset_references(nested, references)

    def _resolve_particle_source_path(self, path_hint: str) -> Optional[str]:
        if not path_hint:
            return None
        candidate = resolved_path(os.path.join(self.project_path, path_hint))
        try:
            relative_path(candidate, self.project_path)
        except ValueError as exc:
            raise RuntimeError(
                f"ParticleGraph path escapes the project: {path_hint}"
            ) from exc
        return candidate if os.path.isfile(candidate) else None

    def _build_asset_guid_index(self) -> dict[str, str]:
        try:
            entries = self._asset_index_entries()
        except RuntimeArtifactError as exc:
            raise RuntimeError(f"Player GUID lookup failed: {exc}") from exc
        return {
            str(entry["guid"]): self._library_source_entry_path(entry)
            for entry in entries
        }

    @staticmethod
    def _particle_source_stable_id(source_path: str) -> str:
        lower = source_path.lower()
        if not lower.endswith(".particlegraph"):
            raise RuntimeError(
                "Player builds only accept saved ParticleGraph sources; "
                f"ParticleScript is Preview/Future: {source_path}"
            )
        try:
            with open(source_path, "r", encoding="utf-8") as stream:
                stable_id = json.load(stream).get("stable_id", "")
        except (OSError, AttributeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"ParticleGraph source is not valid current JSON: {source_path}"
            ) from exc

        if type(stable_id) is not str or not stable_id.strip():
            raise RuntimeError(
                f"Particle source must declare a non-empty stable_id: {source_path}"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", stable_id):
            raise RuntimeError(
                f"Particle source stable_id is not artifact-safe: {stable_id!r}"
            )
        return stable_id

    @classmethod
    def _is_particle_authoring_payload(cls, relative: str) -> bool:
        lower = relative.casefold()
        if lower.endswith(cls._PLAYER_EXCLUDED_PARTICLE_AUTHORING_SUFFIXES):
            return True
        filename = lower.rsplit("/", 1)[-1]
        return bool(
            re.fullmatch(
                r".+\.particle\.[a-z0-9_-]+(?:\.opt-\d+)?\.pyc(?:\.meta)?",
                filename,
            )
        )

    def _copy_particle_data_interface_artifacts(self, data_dir: str) -> None:
        """Copy the imported payloads referenced by sampled particle interfaces."""
        particle_root = os.path.join(data_dir, "Library", "Artifacts", "Particle")
        if not os.path.isdir(particle_root):
            return

        dependencies: set[tuple[str, str]] = set()
        for root, _dirs, filenames in os.walk(particle_root):
            for filename in filenames:
                if not filename.endswith(".inxparticle"):
                    continue
                artifact_path = os.path.join(root, filename)
                try:
                    with open(artifact_path, "r", encoding="utf-8") as artifact_file:
                        artifact = json.load(artifact_file)
                    for emitter in artifact["kernel_ir"]["emitters"]:
                        interfaces = {
                            item["stable_id"]: item
                            for item in emitter["data_interfaces"]
                            if type(item) is dict and type(item.get("stable_id")) is str
                        }
                        sampled = set()
                        for stage_name in ("init", "update", "rendering"):
                            for instruction in emitter[stage_name]["instructions"]:
                                if instruction.get("opcode") not in {
                                    "sample_vector_field",
                                    "collide_sdf_position",
                                    "collide_sdf_velocity",
                                }:
                                    continue
                                immediates = dict(instruction.get("immediates", ()))
                                sampled.add(immediates.get("interface"))
                        for stable_id in sampled:
                            interface = interfaces.get(stable_id)
                            if interface is None:
                                raise RuntimeError(
                                    f"Particle artifact {filename!r} references missing Data Interface {stable_id!r}"
                                )
                            if interface.get("kind") == "vector_field":
                                reference = interface.get("texture")
                                kind, extension = "Texture", ".inxtex"
                            elif interface.get("kind") == "sdf_volume":
                                reference = interface.get("texture")
                                kind, extension = "Texture", ".inxtex"
                            else:
                                continue
                            guid = reference.get("guid") if type(reference) is dict else ""
                            if type(guid) is not str or not guid:
                                raise RuntimeError(
                                    f"Particle Data Interface {stable_id!r} must reference an imported asset GUID before building"
                                )
                            dependencies.add((kind, guid + extension))
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"Particle artifact is not a valid current build input: {artifact_path}"
                    ) from exc

        for kind, filename in sorted(dependencies):
            source = os.path.join(
                self.project_path, "Library", "Artifacts", kind, filename
            )
            if not os.path.isfile(source):
                raise RuntimeError(
                    f"Particle build dependency is missing: Library/Artifacts/{kind}/{filename}"
                )
            destination = os.path.join(
                data_dir, "Library", "Artifacts", kind, filename
            )
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
            guid = filename.rsplit(".", 1)[0]
            try:
                indexed = {
                    str(item["guid"]): item
                    for item in self._asset_index_entries()
                }.get(guid)
                if indexed is None:
                    raise RuntimeError(
                        f"Particle Data Interface artifact has no AssetIndex source: {kind}/{filename}"
                    )
                binding = validate_artifact(self.project_path, indexed, source)
            except RuntimeArtifactError as exc:
                raise RuntimeError(
                    f"Particle Data Interface artifact is stale: {kind}/{filename}: {exc}"
                ) from exc
            binding["runtime_path"] = f"Library/Artifacts/{kind}/{filename}"
            binding["dependencies"] = []
            self._runtime_artifact_bindings[binding["runtime_path"]] = binding
            self._runtime_artifact_source_paths.add(
                str(binding["source_path"]).replace("\\", "/").casefold()
            )

    def _filter_shipped_requirements(self, data_dir: str) -> None:
        req_file = os.path.join(data_dir, "ProjectSettings", "requirements.txt")
        if not os.path.isfile(req_file):
            return

        with open(req_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        def _keep(line: str) -> bool:
            if self._is_game_build_excluded_requirement(line):
                return False
            if not self.enable_jit and re.match(r"^\s*(?:numba|llvmlite)\b", line, re.IGNORECASE):
                return False
            return True

        filtered = [line for line in lines if _keep(line)]
        if len(filtered) == len(lines):
            return

        with open(req_file, "w", encoding="utf-8") as f:
            f.writelines(filtered)

    # ------------------------------------------------------------------
    # Collect user script dependencies
    # ------------------------------------------------------------------

    # Packages that are already bundled by the engine or excluded on
    # purpose — never add them via --include-package even if a user
    # script imports them.
    _BUILTIN_MODULES = frozenset({
        # Standard library (always available in the Nuitka bundle)
        *sys.stdlib_module_names,
        # Engine packages (already followed by Nuitka via boot.py)
        "Infernux",
        # Editor-only / build-only packages owned by the engine itself.
        "watchdog", "PIL", "cv2", "imageio", "psd_tools",
        "tkinter", "unittest", "test", "pip", "setuptools",
        "distutils", "ensurepip",
    })

    # ------------------------------------------------------------------
    # Compile user scripts
    # ------------------------------------------------------------------

    @staticmethod
    def _runtime_script_module_name(runtime_path: str) -> str:
        normalized = runtime_path.replace("\\", "/")
        if not normalized.startswith("Assets/") or not normalized.endswith(".pyc"):
            raise RuntimeError(f"Invalid Player script runtime path: {runtime_path}")
        parts = normalized[len("Assets/"):-4].split("/")
        if parts and parts[-1] == "__init__":
            parts.pop()
        if not parts or any(not part.isidentifier() for part in parts):
            raise RuntimeError(
                f"Player component script has no stable module identity: {runtime_path}"
            )
        return ".".join(parts)

    @classmethod
    def _runtime_component_type_records(
        cls,
        source_text: str,
        *,
        script_guid: str,
        runtime_path: str,
    ) -> list[dict[str, object]]:
        """Extract Player component identities without executing author code."""
        tree = ast.parse(source_text, filename=runtime_path)
        component_bases = {"InxComponent"}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in {
                "Infernux",
                "Infernux.components",
            }:
                for alias in node.names:
                    if alias.name == "InxComponent" or alias.name == "*":
                        component_bases.add(alias.asname or "InxComponent")

        def base_name(expression: ast.expr) -> str:
            if isinstance(expression, ast.Name):
                return expression.id
            if isinstance(expression, ast.Attribute):
                return expression.attr
            return ""

        lifecycle_names = {
            "awake", "start", "fixed_update", "update", "late_update",
            "on_enable", "on_disable", "on_destroy",
            "on_collision_enter", "on_collision_stay", "on_collision_exit",
            "on_trigger_enter", "on_trigger_stay", "on_trigger_exit",
        }
        module_name = cls._runtime_script_module_name(runtime_path)
        from Infernux.components.component_identity import component_type_guid

        records: list[dict[str, object]] = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(base_name(base) in component_bases for base in node.bases):
                continue
            if not script_guid:
                raise RuntimeError(
                    f"Player component script has no Asset GUID: {runtime_path}"
                )
            component_bases.add(node.name)
            lifecycle = sorted(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in lifecycle_names
            )
            type_guid = component_type_guid(script_guid, node.name)
            records.append(
                {
                    "script_guid": script_guid,
                    "type_guid": type_guid,
                    "type_id": (
                        f"python:{script_guid}:{type_guid}:{module_name}:{node.name}"
                    ),
                    "module": module_name,
                    "qualname": node.name,
                    "runtime_path": runtime_path,
                    "lifecycle": lifecycle,
                    "schema_sha256": hashlib.sha256(
                        ast.dump(node, include_attributes=False).encode("utf-8")
                    ).hexdigest(),
                }
            )
        return records

    def _compile_user_scripts(self, final_dir: str):
        """Compile .py in Data/Assets/ to .pyc and remove originals.

        Script identity comes only from the current Library AssetIndex. The
        authoring ``.meta`` files never enter staging or the Player package.
        """
        assets_dir = os.path.join(final_dir, "Data", "Assets")
        if not os.path.isdir(assets_dir):
            return

        _compile_t0 = time.perf_counter()
        _compile_count = 0
        data_dir = os.path.join(final_dir, "Data")
        guid_map: dict[str, str] = {}
        project_assets = os.path.join(self.project_path, "Assets")
        cooked_guid_by_path = {
            relative_path(self._library_source_entry_path(entry), self.project_path)
            .replace("\\", "/")
            .casefold(): str(guid)
            for guid, entry in getattr(self, "_cooked_asset_entries", {}).items()
            if is_path_within(
                self._library_source_entry_path(entry),
                project_assets,
                allow_root=False,
            )
        }

        # R7 embeds verified parallel implementations in the owning script
        # bytecode. Purge stale outputs from older incremental build folders
        # before they can be mistaken for user scripts or shipped.
        for root, _dirs, files in os.walk(assets_dir):
            for fname in files:
                if fname.endswith((".autop.py", ".autop.pyc")):
                    try:
                        os.remove(os.path.join(root, fname))
                    except FileNotFoundError:
                        pass

        # First pass: build GUID -> .pyc relative-path map from the selected
        # current AssetIndex closure. There is deliberately no .meta fallback.
        for root, _dirs, files in os.walk(assets_dir):
            for fname in files:
                if fname.endswith(".py"):
                    py_path = os.path.join(root, fname)
                    staged_relative = relative_path(py_path, data_dir).replace("\\", "/")
                    indexed_guid = cooked_guid_by_path.get(staged_relative.casefold(), "")
                    if not indexed_guid:
                        raise RuntimeError(
                            "Player script cook found a staged script without a "
                            f"current AssetIndex identity: {staged_relative}"
                        )
                    guid_map[indexed_guid] = relative_path(py_path + "c", data_dir)

        # Second pass: compile and remove originals
        guid_by_runtime_path = {
            str(path).replace("\\", "/").casefold(): str(guid)
            for guid, path in guid_map.items()
        }
        runtime_type_records: list[dict[str, object]] = []
        for root, _dirs, files in os.walk(assets_dir):
            for fname in files:
                if fname.endswith(".py"):
                    py_path = os.path.join(root, fname)
                    _compile_count += 1
                    try:
                        with open(py_path, "r", encoding="utf-8") as sf:
                            source_text = sf.read()
                        runtime_path = relative_path(py_path + "c", data_dir).replace("\\", "/")
                        script_guid = guid_by_runtime_path.get(runtime_path.casefold(), "")
                        runtime_type_records.extend(
                            self._runtime_component_type_records(
                                source_text,
                                script_guid=script_guid,
                                runtime_path=runtime_path,
                            )
                        )
                        declarations = _jit_kernels.auto_parallel_declarations(source_text)
                        if not self.enable_jit:
                            required = [name for name, policy in declarations if policy == "required"]
                            if required:
                                raise RuntimeError(
                                    "parallel_policy='required' needs the Auto Parallel build option: "
                                    + ", ".join(required)
                                )
                        else:
                            embedded_source = _jit_kernels.build_auto_parallel_embedded_source(source_text)
                            if embedded_source is not None:
                                with open(py_path, "w", encoding="utf-8", newline="\n") as compiled_source:
                                    compiled_source.write(embedded_source)
                                Debug.log_internal(
                                    f"  embedded Typed HIR kernels: {fname}"
                                )
                    except ValueError as exc:
                        raise RuntimeError(
                            f"auto_parallel compilation rejected for {fname}: {exc}"
                        ) from exc
                    except OSError as exc:
                        Debug.log_warning(f"  Typed HIR preparation failed for {fname}: {exc}")

                    try:
                        py_compile.compile(
                            py_path,
                            cfile=py_path + "c",
                            dfile=relative_path(py_path, data_dir),
                            optimize=2,
                            doraise=True,
                        )
                        os.remove(py_path)
                    except py_compile.PyCompileError as _exc:
                        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                        pass

        Debug.log_internal(
            f"  compiled {_compile_count} scripts in "
            f"{time.perf_counter() - _compile_t0:.2f}s"
        )

        # Write manifest
        if guid_map:
            manifest_path = os.path.join(data_dir, "_script_guid_map.json")
            _write_json_atomic(manifest_path, guid_map, indent=None)
        self._runtime_type_records = sorted(
            runtime_type_records,
            key=lambda record: str(record["type_guid"]),
        )
        library_dir = os.path.join(data_dir, "Library")
        os.makedirs(library_dir, exist_ok=True)
        _write_json_atomic(
            os.path.join(library_dir, self._RUNTIME_TYPE_REGISTRY_FILENAME),
            {
                "$schema": RUNTIME_TYPE_REGISTRY_SCHEMA,
                "registry_version": RUNTIME_TYPE_REGISTRY_VERSION,
                "types": self._runtime_type_records,
            },
        )

    def _write_runtime_asset_records(self, final_dir: str) -> None:
        """Persist cooked GUID identity without shipping editor sidecars."""

        data_dir = os.path.join(final_dir, "Data")
        assets_root = resolved_path(os.path.join(self.project_path, "Assets"))
        project_prefix = portable_path(resolved_path(self.project_path)).rstrip("/") + "/"

        def portable_metadata(value):
            if isinstance(value, dict):
                return {key: portable_metadata(item) for key, item in value.items()}
            if isinstance(value, list):
                return [portable_metadata(item) for item in value]
            if isinstance(value, str):
                normalized = portable_path(value)
                if normalized.casefold().startswith(project_prefix.casefold()):
                    return normalized[len(project_prefix):]
            return value

        cooked_entries = getattr(self, "_cooked_asset_entries", {})
        compiled_bindings_by_guid: dict[str, list[tuple[str, dict[str, object]]]] = {}
        for runtime_path, binding in getattr(
            self, "_runtime_artifact_bindings", {}
        ).items():
            if not isinstance(binding, dict):
                continue
            source_guid = str(binding.get("source_guid", ""))
            if not source_guid:
                continue
            compiled_bindings_by_guid.setdefault(source_guid, []).append(
                (str(runtime_path).replace("\\", "/"), binding)
            )

        staged: list[dict[str, object]] = []
        primary_artifact_by_guid: dict[str, str] = {}
        for guid, entry in sorted(cooked_entries.items()):
            source_path = self._library_source_entry_path(entry)
            is_project_asset = is_path_within(
                source_path, assets_root, allow_root=False
            )
            builtin_relative = (
                self._builtin_resource_relative_path(source_path)
                if bool(entry.get("read_only", False))
                else ""
            )
            is_builtin_resource = bool(builtin_relative)
            if not is_project_asset and not is_builtin_resource:
                continue
            runtime_path = (
                relative_path(source_path, self.project_path).replace("\\", "/")
                if is_project_asset
                else f"Library/Resources/{builtin_relative}"
            )
            if runtime_path.casefold().endswith(".py"):
                runtime_path += "c"
            compiled_bindings = sorted(
                compiled_bindings_by_guid.get(str(guid), []),
                key=lambda item: item[0],
            )
            payloads = [
                {
                    "package": self._CONTENT_ARCHIVE_FILENAME,
                    "runtime_path": path,
                }
                for path, _binding in compiled_bindings
            ]
            if not payloads and is_builtin_resource:
                payloads = [
                    {
                        "package": self._RUNTIME_ARCHIVE_FILENAME,
                        "runtime_path": f"Infernux/resources/{builtin_relative}",
                    }
                ]
            elif not payloads:
                payloads = [
                    {
                        "package": self._CONTENT_ARCHIVE_FILENAME,
                        "runtime_path": runtime_path,
                    }
                ]
            artifact_ids = [
                runtime_artifact_id(payload["package"], payload["runtime_path"])
                for payload in payloads
            ]
            pass_through_reason = (
                ""
                if compiled_bindings
                else self._runtime_artifact_reason(runtime_path)
            )
            primary_artifact_by_guid[str(guid)] = artifact_ids[0]

            metadata = portable_metadata(entry.get("metadata", {}))
            metadata_entries = metadata.get("metadata") if isinstance(metadata, dict) else None
            if not isinstance(metadata_entries, dict) or not metadata_entries:
                raise RuntimeError(
                    "Player asset metadata was not compiled into the current AssetIndex: "
                    f"guid={guid}, path={runtime_path}. Refusing to discard the .meta sidecar."
                )
            file_path = metadata_entries.get("file_path")
            if isinstance(file_path, dict):
                file_path["value"] = runtime_path
            source_state = entry.get("source", {})
            source_fingerprint = {
                "size": int(source_state.get("size", 0)),
                "modified_ns": int(source_state.get("modified_ns", 0)),
                "content_hash": str(entry.get("content_hash", "")),
            }
            staged.append(
                {
                    "guid": str(guid),
                    "runtime_path": runtime_path,
                    "resource_type": int(entry.get("resource_type", 0)),
                    "artifact_path": str(entry.get("artifact_path", "")).replace("\\", "/"),
                    "dependency_guids": sorted(
                        str(item) for item in entry.get("dependencies", [])
                    ),
                    "runtime_artifact_ids": artifact_ids,
                    "primary_runtime_artifact_id": artifact_ids[0],
                    "runtime_artifact_reason": pass_through_reason,
                    "runtime_artifacts": [
                        dict(payload, runtime_artifact_id=artifact_id)
                        for payload, artifact_id in zip(payloads, artifact_ids)
                    ],
                    "source_fingerprint": source_fingerprint,
                    "metadata": metadata,
                    "_payloads": payloads,
                    "_compiled_bindings": compiled_bindings,
                }
            )

        records: list[dict[str, object]] = []
        identity_bindings: dict[str, dict[str, object]] = {}
        for staged_record in staged:
            dependency_guids = staged_record.pop("dependency_guids")
            payloads = staged_record.pop("_payloads")
            compiled_bindings = staged_record.pop("_compiled_bindings")
            dependency_ids = sorted(
                {
                    primary_artifact_by_guid[dependency_guid]
                    for dependency_guid in dependency_guids
                    if dependency_guid in primary_artifact_by_guid
                }
            )
            missing_dependencies = sorted(
                dependency_guid
                for dependency_guid in dependency_guids
                if dependency_guid not in primary_artifact_by_guid
            )
            if missing_dependencies:
                raise RuntimeError(
                    "Player runtime asset dependency is outside the cooked closure: "
                    f"guid={staged_record['guid']}, missing={missing_dependencies}"
                )
            staged_record["dependencies"] = dependency_ids
            records.append(staged_record)

            base_binding = {
                "source_guid": staged_record["guid"],
                "source_path": staged_record["runtime_path"],
                "source_fingerprint": staged_record["source_fingerprint"],
                "dependencies": dependency_ids,
            }
            reason = str(staged_record.get("runtime_artifact_reason", ""))
            if reason:
                base_binding["runtime_artifact_reason"] = reason
            compiled_by_path = dict(compiled_bindings)
            for payload in payloads:
                payload_path = str(payload["runtime_path"])
                binding = dict(compiled_by_path.get(payload_path, base_binding))
                binding["source_guid"] = staged_record["guid"]
                binding["source_path"] = staged_record["runtime_path"]
                binding["source_fingerprint"] = staged_record["source_fingerprint"]
                binding["dependencies"] = dependency_ids
                identity_bindings[payload_path] = binding

        self._runtime_asset_identity_bindings = identity_bindings

        library_dir = os.path.join(data_dir, "Library")
        os.makedirs(library_dir, exist_ok=True)
        destination = os.path.join(library_dir, self._RUNTIME_ASSET_RECORDS_FILENAME)
        _write_json_atomic(
            destination,
            {
                "$schema": "infernux.runtime_asset_records",
                "records_version": 2,
                "entries": records,
            },
        )

    @staticmethod
    def _runtime_artifact_reason(runtime_path: str) -> str:
        """Reject an asset that failed to produce a Library artifact."""

        logical_type = logical_type_for_path(runtime_path)
        payload_kind = payload_kind_for(logical_type)
        reason = runtime_artifact_reason_for(logical_type)
        if payload_kind in RUNTIME_DOCUMENT_PAYLOAD_KINDS:
            raise RuntimeError(
                "Player source payload has no current Library artifact and direct "
                f"runtime shipping is forbidden: {runtime_path}"
                + (f" (expected reason: {reason})" if reason else "")
            )
        return ""

    @staticmethod
    def _sha256_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _remove_empty_directory_tree(path: str) -> None:
        """Remove only empty directories, re-checking the filesystem state."""

        if not os.path.isdir(path):
            return
        for root, _dirs, _files in os.walk(path, topdown=False):
            try:
                with os.scandir(root) as entries:
                    if next(entries, None) is not None:
                        continue
                os.rmdir(root)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                # A non-empty or concurrently used directory is retained.
                continue

    @classmethod
    def _is_retained_player_content_entry(cls, name: str) -> bool:
        """Return whether a Data-root entry must survive content packing."""

        folded = name.casefold()
        if folded in {
            cls._CONTENT_ARCHIVE_FILENAME.casefold(),
            cls._RUNTIME_ARCHIVE_FILENAME.casefold(),
            cls._PLAYER_MANIFEST_FILENAME.casefold(),
            cls._PLAYER_PACKAGE_INDEX_FILENAME.casefold(),
            cls.OUTPUT_MARKER_FILENAME.casefold(),
            "bootstrap.inxrt",
            "buildmanifest.json",
            "logs",
            "modules",
        }:
            return True
        return os.path.splitext(folded)[1] in _NATIVE_PLAYER_ARCHIVE_SUFFIXES

    @staticmethod
    def _remove_directory_tree(path: str) -> None:
        """Delete one staged directory without a Python-side unlink loop.

        Player content finalize used to ``os.remove`` every packed file. That
        keeps the GIL almost continuously, so the editor tick cannot run even
        though the worker is a background thread. Windows ``rd /s /q`` (and
        ``shutil.rmtree`` elsewhere) release the GIL for the whole tree.
        """

        if not os.path.isdir(path):
            return
        if sys.platform == "win32":
            subprocess.run(
                ["cmd", "/c", "rd", "/s", "/q", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            Debug.log_internal(f"Failed to remove packed content tree: {path}")

    @staticmethod
    def _park_player_paths(paths: list[str], park_root: str) -> list[tuple[str, str]]:
        """Move retained leftovers out of trees that are about to be deleted."""

        os.makedirs(park_root, exist_ok=True)
        parked: list[tuple[str, str]] = []
        for index, path in enumerate(paths):
            if not os.path.lexists(path):
                continue
            destination = os.path.join(park_root, f"{index:04d}")
            os.replace(path, destination)
            parked.append((path, destination))
        return parked

    @staticmethod
    def _restore_parked_player_paths(parked: list[tuple[str, str]]) -> None:
        for original, parked_path in parked:
            if not os.path.lexists(parked_path):
                continue
            parent = os.path.dirname(original)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.lexists(original):
                continue
            os.replace(parked_path, original)

    def _strip_authoring_from_module_root(self, modules_root: str) -> None:
        """Keep native module archives and drop any staged authoring leftovers."""

        for root, _dirs, filenames in os.walk(modules_root):
            for filename in filenames:
                if (
                    os.path.splitext(filename)[1].casefold()
                    in _NATIVE_PLAYER_ARCHIVE_SUFFIXES
                ):
                    continue
                path = os.path.join(root, filename)
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        self._remove_empty_directory_tree(modules_root)

    def _finalize_packed_content_sources(
        self,
        data_root: str,
        retained_paths: list[str],
        *,
        on_progress: Optional[Callable[[str, float], None]],
        cancel_event: Optional[threading.Event],
    ) -> None:
        """Remove staged authoring trees after Content.inxpkg has been written."""

        leftover_dirs: list[str] = []
        leftover_files: list[str] = []
        modules_root = ""
        with os.scandir(data_root) as entries:
            for entry in entries:
                if self._is_retained_player_content_entry(entry.name):
                    if (
                        entry.name.casefold() == "modules"
                        and entry.is_dir(follow_symlinks=False)
                    ):
                        modules_root = entry.path
                    continue
                if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                    leftover_dirs.append(entry.path)
                else:
                    leftover_files.append(entry.path)

        leftover_dirs.sort()
        leftover_files.sort()
        park_targets = [
            path
            for path in retained_paths
            if any(
                is_path_within(path, directory, allow_root=False)
                for directory in leftover_dirs
            )
        ]
        steps = len(leftover_dirs) + (1 if leftover_files else 0)
        completed = 0
        park_root = ""
        parked: list[tuple[str, str]] = []
        try:
            if park_targets:
                park_root = os.path.join(
                    os.path.dirname(data_root),
                    f".infernux-packkeep-{os.getpid()}-{time.time_ns()}",
                )
                parked = self._park_player_paths(park_targets, park_root)
            for directory in leftover_dirs:
                completed += 1
                self._report_content_pack_progress(
                    on_progress,
                    cancel_event,
                    f"Finalizing packed project content ({os.path.basename(directory)})",
                    0.9834 + (completed / max(1, steps)) * 0.0005,
                )
                self._remove_directory_tree(directory)
            if leftover_files:
                completed += 1
                self._report_content_pack_progress(
                    on_progress,
                    cancel_event,
                    "Finalizing packed project content",
                    0.9834 + (completed / max(1, steps)) * 0.0005,
                )
                for path in leftover_files:
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
            self._restore_parked_player_paths(parked)
            parked = []
        finally:
            self._restore_parked_player_paths(parked)
            if park_root:
                shutil.rmtree(park_root, ignore_errors=True)
        if modules_root:
            self._strip_authoring_from_module_root(modules_root)

    def _pack_core_runtime_archive(
        self,
        final_dir: str,
        *,
        on_progress: Optional[Callable[[str, float], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Write the always-required runtime payload as Runtime.inxrt."""

        data_root = os.path.join(final_dir, f"{self.project_name}_Data")
        archive_path = os.path.join(data_root, self._RUNTIME_ARCHIVE_FILENAME)
        roots = [
            os.path.join(final_dir, "numpy"),
            os.path.join(final_dir, "numpy.libs"),
            os.path.join(final_dir, "Infernux"),
        ]
        files: list[tuple[str, str]] = []
        deferred_sources: list[str] = []
        deferred_source_set: set[str] = set()
        source_bytes = 0
        for filename in sorted(self._PLAYER_DEFERRED_STDLIB_FILES):
            source_path = os.path.join(final_dir, filename)
            if not os.path.isfile(source_path):
                continue
            files.append((os.path.join("stdlib", filename).replace(os.sep, "/"), source_path))
            deferred_sources.append(source_path)
            deferred_source_set.add(source_path)
            source_bytes += os.path.getsize(source_path)
        bootstrap_root_names = {
            "_infernuxbootstrap.pyd",
            "_infernuxplayer.pyd",
            "python312.dll",
            "_ctypes.pyd",
            "ffi.dll",
            "infernuxfoundation.dll",
        }
        package_lib = os.path.join(final_dir, "Infernux", "lib")
        for filename in sorted(os.listdir(final_dir)):
            source_path = os.path.join(final_dir, filename)
            if not os.path.isfile(source_path):
                continue
            if filename.casefold() in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_DLLS:
                deferred_sources.append(source_path)
                deferred_source_set.add(source_path)
                continue
            suffix = os.path.splitext(filename)[1].casefold()
            if suffix not in {".dll", ".pyd", ".so", ".dylib"}:
                continue
            if source_path in deferred_source_set:
                continue
            if filename.startswith("_InfernuxPlayer") and filename.endswith(
                tuple(importlib.machinery.EXTENSION_SUFFIXES)
            ):
                continue
            if filename.casefold() in bootstrap_root_names:
                continue
            package_copy = os.path.join(package_lib, filename)
            if os.path.isfile(package_copy):
                deferred_sources.append(source_path)
                deferred_source_set.add(source_path)
                continue
            if filename.casefold().startswith("_infernux"):
                deferred_sources.append(source_path)
                deferred_source_set.add(source_path)
                continue
            files.append((f"stdlib/{filename}", source_path))
            deferred_sources.append(source_path)
            deferred_source_set.add(source_path)
            source_bytes += os.path.getsize(source_path)
        for payload_root in roots:
            if not os.path.isdir(payload_root):
                continue
            is_numpy_package = os.path.basename(payload_root).casefold() == "numpy"
            for root, dirs, filenames in os.walk(payload_root):
                if is_numpy_package:
                    dirs[:] = [
                        directory
                        for directory in dirs
                        if directory.casefold() not in self._NUMPY_RUNTIME_EXCLUDED_DIRECTORIES
                    ]
                for filename in filenames:
                    # A Player has one executable only. Never hide another
                    # executable inside Runtime.inxrt.
                    if filename.casefold().endswith(".exe"):
                        continue
                    if filename.casefold() in NuitkaBuilder._FORBIDDEN_LEGACY_NATIVE_DLLS:
                        continue
                    if is_numpy_package and not self._include_numpy_runtime_file(filename):
                        continue
                    source_path = os.path.join(root, filename)
                    portable_source = relative_path(source_path, final_dir)
                    if (
                        portable_source.startswith("Infernux/resources/icons/")
                        and filename.casefold() not in self._PLAYER_RUNTIME_ICON_FILES
                    ):
                        continue
                    files.append((portable_source, source_path))
                    source_bytes += os.path.getsize(source_path)
        if not files:
            raise RuntimeError("Runtime.inxrt contains no native runtime payload")
        native_manifest = self._write_finalize_pack(
            files,
            archive_path,
            message="Packing core runtime data",
            fraction=0.9805,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        for source_path in deferred_sources:
            try:
                os.remove(source_path)
            except FileNotFoundError:
                pass
        for payload_root in roots:
            shutil.rmtree(payload_root, ignore_errors=True)
        # The source-less Infernux package and native closure now live only
        # inside Runtime.inxrt. Do not leave an empty package directory behind.
        self._remove_empty_directory_tree(os.path.join(final_dir, "Infernux"))
        ratio = native_manifest["archive_bytes"] / max(1, source_bytes)
        Debug.log_internal(
            f"Packed Runtime.inxrt: "
            f"{native_manifest['archive_bytes'] / (1024 * 1024):.1f} MB "
            f"({ratio:.1%} of {source_bytes / (1024 * 1024):.1f} MB)"
        )

    def _pack_player_bootstrap_archive(
        self,
        final_dir: str,
        *,
        on_progress: Optional[Callable[[str, float], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Pack the pre-extraction CPython/PlayerHost startup closure.

        The visible package root is deliberately reduced to ``<Game>.exe``
        and ``<Game>_Data``.  Everything needed before Runtime.inxrt can be
        mounted lives in this archive and is extracted into the per-user
        warm cache by PlayerHost.
        """

        data_root = os.path.join(final_dir, f"{self.project_name}_Data")
        archive_path = os.path.join(data_root, "Bootstrap.inxrt")
        extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
        player_modules = sorted(
            path
            for path in Path(final_dir).iterdir()
            if path.is_file()
            and path.name.startswith("_InfernuxPlayer")
            and path.name.endswith(extension_suffixes)
        )
        if len(player_modules) != 1:
            found = ", ".join(path.name for path in player_modules) or "none"
            raise RuntimeError(
                "Bootstrap.inxrt requires exactly one ABI-named _InfernuxPlayer "
                f"extension module; found: {found}"
            )
        player_module = player_modules[0]
        required = {
            "python312.dll": os.path.join(final_dir, "python312.dll"),
            "_ctypes.pyd": os.path.join(final_dir, "_ctypes.pyd"),
            "ffi.dll": os.path.join(final_dir, "ffi.dll"),
            "_InfernuxBootstrap.pyd": os.path.join(final_dir, "_InfernuxBootstrap.pyd"),
            player_module.name: str(player_module),
            "Infernux/lib/InfernuxFoundation.dll": os.path.join(
                final_dir, "Infernux", "lib", "InfernuxFoundation.dll"
            ),
        }
        for name, source in required.items():
            if not os.path.isfile(source):
                raise RuntimeError(
                    f"Bootstrap.inxrt is incomplete: missing {name}; refusing "
                    "to package a legacy executable Player"
                )

        sources = [(name, source) for name, source in sorted(required.items())]
        for optional_name in (
            "python3.dll",
            "zlib.dll",
            "vcruntime140.dll",
            "vcruntime140_1.dll",
        ):
            source = os.path.join(final_dir, optional_name)
            if os.path.isfile(source):
                sources.append((optional_name, source))

        bootstrap_stdlib = Path(final_dir) / "stdlib"
        if not (bootstrap_stdlib / "encodings" / "__init__.pyc").is_file():
            raise RuntimeError(
                "Bootstrap.inxrt is incomplete: missing isolated Python encodings package"
            )
        for source in sorted(path for path in bootstrap_stdlib.rglob("*") if path.is_file()):
            if source.suffix.casefold() != ".pyc":
                raise RuntimeError(
                    "Bootstrap.inxrt stdlib must contain source-less Python bytecode only: "
                    f"{source.relative_to(bootstrap_stdlib).as_posix()}"
                )
            logical = source.relative_to(Path(final_dir)).as_posix()
            sources.append((logical, str(source)))
        native_manifest = self._write_finalize_pack(
            sources,
            archive_path,
            message="Packing Player bootstrap",
            fraction=0.98035,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        for name, source in required.items():
            if "/" not in name:
                os.remove(source)
        for optional_name in (
            "python3.dll",
            "zlib.dll",
            "vcruntime140.dll",
            "vcruntime140_1.dll",
        ):
            try:
                os.remove(os.path.join(final_dir, optional_name))
            except FileNotFoundError:
                pass
        shutil.rmtree(bootstrap_stdlib)
        root_foundation = os.path.join(final_dir, "InfernuxFoundation.dll")
        if os.path.isfile(root_foundation):
            os.remove(root_foundation)
        Debug.log_internal(
            "Packed Bootstrap.inxrt: "
            f"{native_manifest['archive_bytes'] / (1024 * 1024):.1f} MB"
        )

    def _pack_parallel_runtime_archive(self, final_dir: str) -> None:
        """Move the optional native parallel module into the final layout."""

        legacy_paths: list[str] = []
        for root, directories, filenames in os.walk(final_dir):
            if "RuntimeModules" in directories:
                legacy_paths.append(os.path.join(root, "RuntimeModules"))
            for filename in filenames:
                if filename.casefold().endswith((".zip", ".inxpack", "-module.json")):
                    legacy_paths.append(os.path.join(root, filename))
        if any(os.path.exists(path) for path in legacy_paths):
            raise RuntimeError(
                "Legacy runtime module payload detected; rebuild the optional "
                "module with the native Parallel.inxmod path"
            )

        source = os.path.join(final_dir, self._PARALLEL_ARCHIVE_FILENAME)
        if not os.path.isfile(source):
            if self.enable_jit:
                raise RuntimeError(
                    "Parallel module was requested but Parallel.inxmod was not staged"
                )
            return

        data_root = os.path.join(final_dir, f"{self.project_name}_Data")
        module_root = os.path.join(data_root, "Modules")
        os.makedirs(module_root, exist_ok=True)
        destination = os.path.join(module_root, self._PARALLEL_ARCHIVE_FILENAME)
        if os.path.exists(destination):
            os.remove(destination)
        os.replace(source, destination)

    def _report_content_pack_progress(
        self,
        on_progress: Optional[Callable[[str, float], None]],
        cancel_event: Optional[threading.Event],
        message: str,
        fraction: float,
    ) -> None:
        _yield_editor_thread()
        if cancel_event is not None and cancel_event.is_set():
            raise _BuildCancelled()
        if on_progress:
            on_progress(message, fraction)

    def _write_finalize_pack(
        self,
        files: list[tuple[str, str]],
        destination: str,
        *,
        message: str,
        fraction: float,
        on_progress: Optional[Callable[[str, float], None]],
        cancel_event: Optional[threading.Event],
    ) -> dict[str, object]:
        """Write one Finalize container without monopolizing Editor Python."""

        if using_test_backend():
            return write_pack(
                files,
                destination,
                profile=self._player_inxpack_profile(),
            )
        return write_pack_isolated(
            files,
            destination,
            profile=self._player_inxpack_profile(),
            cancel_event=cancel_event,
            on_wait=lambda: self._report_content_pack_progress(
                on_progress,
                cancel_event,
                message,
                fraction,
            ),
        )

    def _pack_content_archive(
        self,
        final_dir: str,
        *,
        on_progress: Optional[Callable[[str, float], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Write project data as the native Content.inxpkg container."""

        data_root = os.path.join(final_dir, f"{self.project_name}_Data")
        if not os.path.isdir(data_root):
            raise RuntimeError("Player Data directory is missing after layout organization")

        archive_path = os.path.join(data_root, self._CONTENT_ARCHIVE_FILENAME)
        files: list[tuple[str, str]] = []
        retained_paths: list[str] = []
        source_bytes = 0
        forbidden_plaintext: list[str] = []
        forbidden_direct_payloads: list[str] = []
        catalog_payloads: dict[str, bytes] = {}
        processed = 0
        last_report = 0.0
        # This cache belongs to exactly one Content.inxpkg generation.  Clear
        # it before walking the staging tree so a failed/retried build cannot
        # reuse payloads from an older package.
        self._packed_content_catalog_payloads = {}

        for root, dirs, filenames in os.walk(data_root):
            for directory in dirs:
                if directory == "Logs":
                    retained_paths.append(os.path.join(root, directory))
            dirs[:] = [directory for directory in dirs if directory != "Logs"]
            for filename in filenames:
                path = os.path.join(root, filename)
                relative = relative_path(path, data_root)
                portable_relative = relative.replace(os.sep, "/")
                if portable_relative in self._PLAYER_CONTENT_CONTROL_RELATIVE_PATHS:
                    continue
                source_paths = getattr(self, "_runtime_artifact_source_paths", set())
                if portable_relative.casefold() in source_paths:
                    continue
                if (
                    os.path.splitext(portable_relative)[1].casefold()
                    in _NATIVE_PLAYER_ARCHIVE_SUFFIXES
                ):
                    retained_paths.append(path)
                    continue
                if self._is_player_editor_path(portable_relative):
                    continue
                if self._is_particle_authoring_payload(relative):
                    continue
                suffix = os.path.splitext(filename)[1].lower()
                if suffix == ".meta":
                    continue
                packed_runtime_shader = (
                    suffix
                    in {
                        ".glsl",
                        ".vert",
                        ".frag",
                        ".comp",
                        ".geom",
                        ".tesc",
                        ".tese",
                        ".hlsl",
                        ".shader",
                    }
                    and portable_relative.casefold().startswith(
                        "library/artifacts/blob/"
                    )
                )
                if not packed_runtime_shader and suffix in {
                    ".py", ".pyi", ".pyx", ".lua", ".cpp", ".c", ".cc",
                    ".h", ".hpp", ".glsl", ".vert", ".frag", ".comp", ".geom",
                    ".tesc", ".tese", ".hlsl", ".shader",
                }:
                    forbidden_plaintext.append(relative)
                logical_type = logical_type_for_path(portable_relative)
                if payload_kind_for(logical_type) in RUNTIME_DOCUMENT_PAYLOAD_KINDS:
                    forbidden_direct_payloads.append(relative)
                self._rewrite_player_document_paths(path, suffix)
                if self._runtime_catalog_payload_required(portable_relative):
                    with open(path, "rb") as source:
                        catalog_payloads[portable_relative] = source.read()
                files.append((relative, path))
                source_bytes += os.path.getsize(path)
                processed += 1
                _yield_editor_thread()
                now = time.perf_counter()
                if processed == 1 or now - last_report >= 0.05:
                    last_report = now
                    self._report_content_pack_progress(
                        on_progress,
                        cancel_event,
                        f"Packing project content ({processed} files)",
                        min(0.9828, 0.981 + processed * 1e-6),
                    )

        if forbidden_plaintext:
            raise RuntimeError(
                "Player content contains authoring/source files that must be "
                "compiled into Library artifacts first: "
                + ", ".join(sorted(forbidden_plaintext)[:12])
            )
        if forbidden_direct_payloads:
            raise RuntimeError(
                "Player content contains direct or serialized runtime payloads; "
                "cook them into Library artifacts before packing: "
                + ", ".join(sorted(forbidden_direct_payloads)[:12])
            )
        if not files:
            raise RuntimeError("Player Content.inxpkg would be empty")

        self._report_content_pack_progress(
            on_progress,
            cancel_event,
            "Compressing project content",
            0.9829,
        )
        native_manifest = self._write_finalize_pack(
            files,
            archive_path,
            message="Compressing project content",
            fraction=0.9829,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        manifest_entries = {
            str(entry["path"]).replace("\\", "/"): entry
            for entry in native_manifest.get("files", [])
        }
        verified_payloads: dict[str, tuple[str, bytes]] = {}
        for entry_path, payload in catalog_payloads.items():
            manifest_entry = manifest_entries.get(entry_path)
            if manifest_entry is None:
                raise RuntimeError(
                    f"Packed content manifest omitted catalog payload: {entry_path}"
                )
            expected_sha256 = str(manifest_entry["sha256"])
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if actual_sha256.casefold() != expected_sha256.casefold():
                raise RuntimeError(
                    f"Packed content catalog payload changed while packing: {entry_path}"
                )
            verified_payloads[entry_path] = (expected_sha256, payload)
        self._packed_content_catalog_payloads = verified_payloads
        self._report_content_pack_progress(
            on_progress,
            cancel_event,
            "Finalizing packed project content",
            0.9834,
        )
        self._finalize_packed_content_sources(
            data_root,
            retained_paths,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

        ratio = native_manifest["archive_bytes"] / max(1, source_bytes)
        Debug.log_internal(
            f"Packed {native_manifest['file_count']} content files into "
            f"{native_manifest['archive_bytes'] / (1024 * 1024):.1f} MB "
            f"({ratio:.1%} of {source_bytes / (1024 * 1024):.1f} MB)"
        )

    def _rewrite_player_document_paths(self, path: str, suffix: str) -> None:
        """Make project-owned absolute references portable in the build copy."""

        if suffix not in self._PLAYER_PORTABLE_DOCUMENT_SUFFIXES:
            return
        try:
            with open(path, "r", encoding="utf-8") as source:
                document = json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return

        project_root = resolved_path(self.project_path)
        changed = False

        def rewrite(value):
            nonlocal changed
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if not isinstance(value, str) or not os.path.isabs(value):
                return value
            try:
                absolute = resolved_path(value)
            except (OSError, ValueError):
                return value
            if not is_path_within(absolute, project_root, allow_root=True):
                return value
            try:
                rewritten_path = portable_path(relative_path(absolute, project_root))
            except ValueError:
                return value
            changed = True
            return rewritten_path

        rewritten = rewrite(document)
        _yield_editor_thread()
        if not changed:
            return
        _write_json_atomic(path, rewritten, indent=None)

    def _write_payload_manifest(self, final_dir: str) -> None:
        """Write the deterministic runtime catalog and bootstrap manifest.

        The package TOCs are the source of truth after authoring files have
        been packed.  The catalog therefore never depends on an editor path
        or on filesystem traversal order.  This is deliberately not a Player
        finished package scan: it only records the package table of contents
        already produced by the packer.
        """

        data_root = os.path.join(final_dir, f"{self.project_name}_Data")
        library_root = os.path.join(data_root, "Library")
        os.makedirs(library_root, exist_ok=True)
        stale_marker = os.path.join(final_dir, "_infernux_runtime_pack.json")
        try:
            os.remove(stale_marker)
        except FileNotFoundError:
            pass

        package_paths = [
            os.path.join(data_root, self._RUNTIME_ARCHIVE_FILENAME),
            os.path.join(data_root, self._CONTENT_ARCHIVE_FILENAME),
        ]
        parallel_path = os.path.join(
            data_root, "Modules", self._PARALLEL_ARCHIVE_FILENAME
        )
        if os.path.isfile(parallel_path):
            package_paths.append(parallel_path)

        packages: list[dict[str, object]] = []
        package_entries: list[dict[str, object]] = []
        for package_path in package_paths:
            if not os.path.isfile(package_path):
                raise RuntimeError(
                    f"Runtime asset catalog required native package is missing: "
                    f"{package_path}"
                )
            try:
                package_manifest = read_manifest(package_path)
                package_relative = relative_path(package_path, final_dir)
                package_record = {
                    "path": package_relative,
                    "archive_sha256": package_manifest["archive_sha256"],
                    "archive_bytes": package_manifest["archive_bytes"],
                    "file_count": package_manifest["file_count"],
                    "raw_bytes": package_manifest["raw_bytes"],
                    "stored_bytes": package_manifest["stored_bytes"],
                    "codec": package_manifest["codec"],
                }
                for entry in package_manifest.get("files", []):
                    entry_path = str(entry["path"]).replace("\\", "/")
                    logical_type = logical_type_for_path(entry_path)
                    payload_kind = payload_kind_for(logical_type)
                    package_entry = {
                        "package": package_relative,
                        "runtime_path": entry_path,
                        "bytes": entry["raw_bytes"],
                        "sha256": entry["sha256"],
                    }
                    if self._runtime_catalog_payload_required(entry_path):
                        # Payload is optional catalog input used only for
                        # dependency discovery. Cooked document artifacts are
                        # JSON too; binary artifacts, bytecode, shaders and
                        # NumPy payloads remain unopened.
                        cached_payload = None
                        if os.path.basename(package_path) == self._CONTENT_ARCHIVE_FILENAME:
                            cached_payload = getattr(
                                self, "_packed_content_catalog_payloads", {}
                            ).get(entry_path)
                        if (
                            cached_payload is not None
                            and str(cached_payload[0]).casefold()
                            == str(entry["sha256"]).casefold()
                        ):
                            package_entry["payload"] = cached_payload[1]
                        else:
                            package_entry["payload"] = read_entry(
                                package_path,
                                entry_path,
                            )
                    binding = getattr(
                        self, "_runtime_asset_identity_bindings", {}
                    ).get(entry_path)
                    if binding is None:
                        binding = getattr(
                            self, "_runtime_artifact_bindings", {}
                        ).get(entry_path)
                    if binding is not None:
                        package_entry["asset_binding"] = binding
                    package_entries.append(package_entry)
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Runtime asset catalog cannot validate {package_path}: {exc}"
                ) from exc
            packages.append(package_record)

        executable = os.path.join(final_dir, f"{self.project_name}.exe")
        if not os.path.isfile(executable):
            raise RuntimeError(f"Player executable is missing: {executable}")
        proven_residual_assets = self._residual_direct_runtime_assets(package_entries)
        if proven_residual_assets:
            raise RuntimeError(
                "Player Content contains direct or serialized runtime payloads; "
                "all such assets must be converted to Library artifacts: "
                + ", ".join(proven_residual_assets)
            )
        catalog = build_catalog(
            package_entries,
            player_host={
                "executable": relative_path(executable, final_dir),
                "sha256": self._sha256_file(executable),
                "identity": "nuitka-player-host",
            },
            package_records=packages,
        )
        catalog_path = os.path.join(library_root, "RuntimeAssetCatalog.json")
        _write_json_atomic(catalog_path, catalog)

        # The native boot stub intentionally avoids importing the full Python
        # stdlib before Runtime.inxrt is mounted. Keep a tiny ASCII identity
        # index beside the packages so an already verified cache can be opened
        # without hashing every large archive twice on every launch.
        package_by_name = {
            os.path.basename(str(record["path"])).casefold(): record
            for record in packages
        }
        package_index_lines = ["INFERNUX_PLAYER_PACKAGE_INDEX_V1"]
        for kind, filename in (
            ("runtime", self._RUNTIME_ARCHIVE_FILENAME),
            ("content", self._CONTENT_ARCHIVE_FILENAME),
            ("parallel", self._PARALLEL_ARCHIVE_FILENAME),
        ):
            record = package_by_name.get(filename.casefold())
            if record is None:
                continue
            package_index_lines.append(
                "\t".join(
                    (
                        kind,
                        str(record["archive_sha256"]),
                        str(int(record["archive_bytes"])),
                    )
                )
            )
        package_index_path = os.path.join(
            data_root, self._PLAYER_PACKAGE_INDEX_FILENAME
        )
        package_index_temporary = (
            package_index_path + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            with open(
                package_index_temporary, "x", encoding="ascii", newline="\n"
            ) as package_index_stream:
                package_index_stream.write("\n".join(package_index_lines) + "\n")
                package_index_stream.flush()
                os.fsync(package_index_stream.fileno())
            os.replace(package_index_temporary, package_index_path)
        finally:
            try:
                os.remove(package_index_temporary)
            except FileNotFoundError:
                pass

        flavor = (
            RuntimeFlavor.PLAYER_DEBUG
            if self.debug_mode
            else RuntimeFlavor.PLAYER_RELEASE
        )
        features = RuntimeFeatureSet(
            jit=bool(self.enable_jit),
            parallel=bool(self.enable_jit),
            optional_subsystems=("splash",) if self.splash_items else (),
        )
        runtime_contract = player_runtime_contract_sections(flavor, features)
        player_manifest = {
            "$schema": PLAYER_MANIFEST_SCHEMA,
            "manifest_version": PLAYER_MANIFEST_VERSION,
            "product": {
                "layout": "single_executable_native_packages",
                **runtime_contract["product"],
                "entry_points": [os.path.basename(executable)],
                "single_entry_point": True,
            },
            "features": runtime_contract["features"],
            "services": runtime_contract["services"],
            "runtime_policy": runtime_contract["runtime_policy"],
        }
        _write_json_atomic(
            os.path.join(data_root, self._PLAYER_MANIFEST_FILENAME),
            player_manifest,
        )
        self._packed_content_catalog_payloads = {}

    @staticmethod
    def _runtime_catalog_payload_required(entry_path: str) -> bool:
        """Return whether dependency discovery needs this entry's JSON bytes."""

        normalized = str(entry_path).replace("\\", "/")
        logical_type = logical_type_for_path(normalized)
        payload_kind = payload_kind_for(logical_type)
        cooked_json_document = (
            normalized.casefold().startswith("library/artifacts/document/")
            and Path(normalized).suffix.casefold() in RUNTIME_JSON_DOCUMENT_SUFFIXES
        )
        return (
            payload_kind == "serialized_runtime_document"
            or cooked_json_document
            or (
                logical_type == "runtime_metadata"
                and normalized.casefold().endswith(".json")
            )
        )

    def _residual_direct_runtime_assets(
        self,
        package_entries: list[dict[str, object]],
    ) -> list[str]:
        """Return every direct or serialized payload left in Content.inxpkg."""

        residual: list[str] = []
        for entry in package_entries:
            package = str(entry.get("package", ""))
            runtime_path = str(entry.get("runtime_path", "")).replace("\\", "/")
            if not package.casefold().endswith(GameBuilder._CONTENT_ARCHIVE_FILENAME.casefold()):
                continue
            if self._is_explicit_build_only_content_path(runtime_path):
                residual.append(runtime_path)
                continue
            logical_type = logical_type_for_path(runtime_path)
            if payload_kind_for(logical_type) in RUNTIME_DOCUMENT_PAYLOAD_KINDS:
                residual.append(runtime_path)

        return sorted(set(residual))

    def _project_asset_reachability_evidence(self) -> tuple[set[str], set[str]]:
        """Return indexed and reachable project paths when the scene closure is provable."""

        settings_path = os.path.join(
            self.project_path, "ProjectSettings", "BuildSettings.json"
        )
        try:
            with open(settings_path, "r", encoding="utf-8") as stream:
                configured_scenes = json.load(stream).get("scenes", [])
            if not isinstance(configured_scenes, list) or not configured_scenes:
                return set(), set()
            entries = self._asset_index_entries()
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            AttributeError,
            RuntimeArtifactError,
        ):
            return set(), set()

        indexed: set[str] = set()
        entry_paths: dict[str, str] = {}
        for entry in entries:
            try:
                source_path = self._library_source_entry_path(entry)
                source_relative = relative_path(source_path, self.project_path)
            except (OSError, ValueError):
                return set(), set()
            normalized = source_relative.replace("\\", "/").casefold()
            indexed.add(normalized)
            entry_paths[str(entry.get("guid", ""))] = normalized

        scene_paths: set[str] = set()
        for configured in configured_scenes:
            if not isinstance(configured, str) or not configured.strip():
                return set(), set()
            try:
                scene_relative = relative_path(
                    self._resolve_build_scene_path(configured), self.project_path
                )
            except (OSError, ValueError):
                return set(), set()
            scene_paths.add(scene_relative.replace("\\", "/").casefold())
        if not scene_paths.issubset(indexed):
            return set(), set()

        try:
            selected = self._collect_library_asset_entries(entries)
        except (OSError, ValueError, RuntimeArtifactError):
            return set(), set()
        reachable = set(scene_paths)
        reachable.update(
            entry_paths[guid]
            for guid in selected
            if guid in entry_paths
        )
        return indexed, reachable

    def _is_explicit_build_only_content_path(self, runtime_path: str) -> bool:
        path_key = runtime_path.replace("\\", "/").casefold()
        excluded = {
            path.replace("\\", "/").casefold()
            for path in self._PLAYER_EXCLUDED_CONTENT_RELATIVE_PATHS
        }
        return (
            path_key in excluded
            or path_key.endswith(".meta")
            or path_key == "logs"
            or path_key.startswith("logs/")
        )
    # Splash items
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Relativize scene paths
    # ------------------------------------------------------------------

    def _relativize_scenes(self, final_dir: str):
        bs = os.path.join(
            final_dir, "Data", "ProjectSettings", "BuildSettings.json"
        )
        if not os.path.isfile(bs):
            return
        with open(bs, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)

        scenes = data.get("scenes", [])
        rel_scenes = []
        for scene_path in scenes:
            absolute = self._resolve_build_scene_path(scene_path)
            rel = relative_path(absolute, self.project_path)
            rel_scenes.append(portable_path(rel))
        # BuildSettings is an authoring document.  The Player needs only the
        # ordered scene list; output paths, compiler switches, editor icon
        # sources and cook roots belong to BuildManifest/catalog generation
        # and must never leak workstation paths into the shipped content.
        _write_json_atomic(bs, {"scenes": rel_scenes})

    # ------------------------------------------------------------------
    # Generate BuildManifest.json
    # ------------------------------------------------------------------

    def _generate_manifest(self, final_dir: str):
        """Write BuildManifest.json with display mode, splash config, etc."""
        bs = os.path.join(
            final_dir, "Data", "ProjectSettings", "BuildSettings.json"
        )
        scenes = []
        if os.path.isfile(bs):
            with open(bs, "r", encoding="utf-8", errors="replace") as f:
                scenes = json.load(f).get("scenes", [])

        splash_runtime = []
        for item in self.splash_items:
            built = item.get("_built_path")
            if not built:
                continue
            splash_runtime.append({
                "type": item.get("type", "image"),
                "path": built,
                "duration": item.get("duration", 3.0),
                "fade_in": item.get("fade_in", 0.5),
                "fade_out": item.get("fade_out", 0.5),
            })

        flavor = (
            RuntimeFlavor.PLAYER_DEBUG
            if self.debug_mode
            else RuntimeFlavor.PLAYER_RELEASE
        )
        features = RuntimeFeatureSet(
            jit=bool(self.enable_jit),
            parallel=bool(self.enable_jit),
            optional_subsystems=("splash",) if splash_runtime else (),
        )

        manifest = {
            "game_name": self.project_name,
            "icon_path": self._built_icon_path,
            "debug_build": bool(self.debug_mode),
            "display_mode": self.display_mode,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "window_resizable": self.window_resizable,
            "scenes": scenes,
            "splash_items": splash_runtime,
            "runtime_contract": player_runtime_contract_sections(flavor, features),
            "build_output": {
                "tool": "Infernux",
                "project_name": self.project_name,
                "project_identity": path_fingerprint(self.project_path),
            },
        }

        manifest_path = os.path.join(final_dir, "Data", "BuildManifest.json")
        _write_json_atomic(manifest_path, manifest)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_dist(self, final_dir: str):
        """Remove editor-only and redundant files from the build output."""
        removed_bytes = 0
        dirs_to_remove: list[str] = []
        files_to_remove: list[str] = []

        def _queue_dir(d: str):
            if os.path.isdir(d):
                dirs_to_remove.append(d)

        def _queue_file(f: str):
            if os.path.isfile(f):
                files_to_remove.append(f)

        # Directories that are entirely unnecessary at runtime
        _queue_dir(os.path.join(final_dir, "Infernux", "lib", "_player_runtime"))
        for _editor_data_dir in self._PLAYER_EDITOR_DATA_DIRECTORIES:
            _queue_dir(os.path.join(final_dir, _editor_data_dir))
        # Keep engine icons. Built-in renderer materials resolve camera/light
        # gizmo textures during native startup, before Player mode is applied.
        # The complete directory is small and is an engine resource contract,
        # not disposable Editor cache data.
        _queue_dir(
            os.path.join(final_dir, "Infernux", "resources", "project_templates")
        )

        # Build-time-only video packages — av (PyAV/ffmpeg) and imageio
        # are used only for splash video encoding at build time.  The
        # player reads pre-extracted .infsplash blobs via struct.
        for _build_pkg in ("av", "av.libs", "imageio"):
            _queue_dir(os.path.join(final_dir, _build_pkg))

        # Remove any leaked ffmpeg DLLs from the dist root that Nuitka's
        # DLL scanner may have copied from the av package.
        _FFMPEG_PREFIXES = (
            "avcodec", "avformat", "avutil", "avfilter", "avdevice",
            "swresample", "swscale",
        )
        for fname in os.listdir(final_dir):
            if fname.lower().endswith(".dll") and any(
                fname.lower().startswith(p) for p in _FFMPEG_PREFIXES
            ):
                _queue_file(os.path.join(final_dir, fname))

        # Individual files not needed at runtime
        _queue_file(os.path.join(final_dir, "Infernux", "lib", "_Infernux.pyi"))
        _queue_file(os.path.join(final_dir, "Data", "ProjectSettings", "EditorSettings.json"))
        _queue_file(os.path.join(final_dir, "Data", "ProjectSettings", "GameView.ini"))
        # A packaged Player reads the bundled Infernux/resources directory
        # directly. The editor's synchronized Library copy is redundant and
        # can contain another full copy of the engine font and icons.
        _queue_dir(os.path.join(final_dir, "Data", "Library", "Resources"))

        # Remove the platform-tagged .pyd duplicate — Nuitka standardises
        # to the short name (_Infernux.pyd) and --include-package-data
        # copies the original cp312-win_amd64.pyd as well.
        lib_dir_dup = os.path.join(final_dir, "Infernux", "lib")
        if os.path.isdir(lib_dir_dup):
            for fname in os.listdir(lib_dir_dup):
                if fname.endswith(".pyd") and ".cp" in fname:
                    short = fname.split(".")[0] + ".pyd"
                    if os.path.isfile(os.path.join(lib_dir_dup, short)):
                        _queue_file(os.path.join(lib_dir_dup, fname))

        # The full bridge stays package-qualified for normal post-extraction
        # imports. Any root copy is redundant; only _InfernuxBootstrap is a
        # pre-extraction extension.
        lib_dir = os.path.join(final_dir, "Infernux", "lib")
        package_native_module = os.path.join(lib_dir, "_Infernux.pyd")
        root_native_module = os.path.join(final_dir, "_Infernux.pyd")
        if os.path.isfile(root_native_module) and os.path.isfile(package_native_module):
            _queue_file(root_native_module)
        if os.path.isdir(lib_dir):
            for fname in os.listdir(lib_dir):
                if fname.casefold().startswith("_infernuxbootstrap"):
                    _queue_file(os.path.join(lib_dir, fname))

        # Metadata is authoring/import state. Runtime identity has already
        # moved into AssetIndex-derived records, so no engine metadata is kept.
        for metadata_root in (os.path.join(final_dir, "Infernux"),):
            if not os.path.isdir(metadata_root):
                continue
            for root, _, files in os.walk(metadata_root):
                for fname in files:
                    if fname.endswith(".meta"):
                        _queue_file(os.path.join(root, fname))

        # ── Global cleanup: cache directories and packaging metadata ──
        # Raw runtime dependencies are deliberately compiled to adjacent
        # sourceless .pyc files by NuitkaBuilder. Those files are the package
        # implementation and must survive until Runtime.inxrt is assembled.
        for root, dirs, files in os.walk(final_dir, topdown=False):
            for dname in dirs:
                if dname == "__pycache__" or dname.endswith(".dist-info"):
                    dirs_to_remove.append(os.path.join(root, dname))
            for fname in files:
                if os.path.splitext(fname)[1].lower() in {".pdb", ".lib", ".exp", ".pyi"}:
                    files_to_remove.append(os.path.join(root, fname))

        # ── Execute removals ─────────────────────────────────────────
        # 1. Remove individual files (fast, no subprocess)
        for f in files_to_remove:
            try:
                removed_bytes += os.path.getsize(f)
            except OSError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass
            try:
                os.remove(f)
            except OSError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass

        # 2. Count bytes in queued dirs, then batch-remove
        for d in dirs_to_remove:
            if not os.path.isdir(d):
                continue
            for r, _, fs in os.walk(d):
                for fname in fs:
                    try:
                        removed_bytes += os.path.getsize(os.path.join(r, fname))
                    except OSError as _exc:
                        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                        pass

        if sys.platform == "win32" and dirs_to_remove:
            # Single cmd process to remove all directories at once
            rd_args = []
            for d in dirs_to_remove:
                if os.path.isdir(d):
                    rd_args.extend(["rd", "/s", "/q", d, "&"])
            if rd_args:
                rd_args.pop()  # remove trailing "&"
                subprocess.run(
                    ["cmd", "/c"] + rd_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            for d in dirs_to_remove:
                shutil.rmtree(d, ignore_errors=True)

        # Empty package directories are never useful in a Player.  This also
        # removes ``Infernux`` after editor-only data has been cleaned and the
        # runtime resources have been packed into Runtime.inxrt.
        self._remove_empty_directory_tree(os.path.join(final_dir, "Infernux"))

        mb = removed_bytes / (1024 * 1024)
        Debug.log_internal(f"Cleaned {mb:.1f} MB of redundant files from build")

    @staticmethod
    def _cleanup_temp(boot_script: str):
        """Synchronously remove the temporary boot script directory."""
        boot_dir = os.path.dirname(boot_script)
        if not os.path.isdir(boot_dir):
            return
        shutil.rmtree(boot_dir)

    # ------------------------------------------------------------------
    # Build size report
    # ------------------------------------------------------------------

    @staticmethod
    def _report_build_size(final_dir: str, _blog: Callable[[str], None]) -> None:
        """Log a per-directory size breakdown of the final build output."""
        total = 0
        entries: list[tuple[str, int]] = []

        for item in os.scandir(final_dir):
            if item.is_dir(follow_symlinks=False):
                sz = 0
                for root, _, files in os.walk(item.path):
                    for f in files:
                        try:
                            sz += os.path.getsize(os.path.join(root, f))
                        except OSError as _exc:
                            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                            pass
                entries.append((item.name + "/", sz))
            elif item.is_file(follow_symlinks=False):
                sz = item.stat().st_size
                entries.append((item.name, sz))
            else:
                continue
            total += sz

        entries.sort(key=lambda x: x[1], reverse=True)
        lines = [f"Build size report — total {total / (1024*1024):.1f} MB"]
        for name, sz in entries:
            mb = sz / (1024 * 1024)
            pct = (sz / total * 100) if total else 0
            if mb >= 0.1:
                lines.append(f"  {mb:7.1f} MB  {pct:4.1f}%  {name}")
        report = "\n".join(lines)
        Debug.log_internal(report)
        _blog(report)
