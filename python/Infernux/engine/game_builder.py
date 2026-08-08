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
from Infernux.engine.player_package_audit import (
    BOOTSTRAP_NATIVE_ROOT_ALLOWLIST,
    NATIVE_ARCHIVE_SUFFIXES,
    audit_player_package,
)
from Infernux.engine.player_package_native import read_entry, read_manifest, write_pack
from Infernux.engine.path_utils import (
    is_path_within,
    path_fingerprint,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
)
from Infernux.engine.runtime_artifact_catalog import (
    RuntimeArtifactError,
    build_catalog,
    load_asset_index,
    logical_asset_type,
    logical_type_for_path,
    payload_kind_for,
    runtime_artifact_id,
    validate_artifact,
)


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
    _GAME_DATA_DIRS = ["Assets", "ProjectSettings", "materials"]
    _EXCLUDE_PATTERNS = {
        "__pycache__",
        ".git",
        ".gitignore",
        ".infernux-engine-lock.json",
        "Logs",
    }
    _ICON_EXTS = {".png", ".jpg", ".jpeg", ".ico"}
    _GAME_BUILD_EXCLUDED_PACKAGES = frozenset({"mcp", "fastmcp"})
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
    _PLAYER_PORTABLE_DOCUMENT_SUFFIXES = frozenset(
        {
            ".animclip",
            ".animfsm",
            ".animtimeline",
            ".effect",
            ".effectgroup",
            ".json",
            ".mat",
            ".particlegraph",
            ".prefab",
            ".scene",
            ".timeline",
            ".timelinefsm",
        }
    )
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
    _PLAYER_CONTENT_CONTROL_RELATIVE_PATHS = frozenset(
        {
            "BuildManifest.json",
            _PLAYER_MANIFEST_FILENAME,
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

    def _build_inner(self, _p, _blog, on_progress, cancel_event, build_start) -> str:
        """Internal build pipeline (separated for clean exception handling)."""

        _p(t("build.step.validating"), 0.00)
        self._validate()

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
        self._pack_player_bootstrap_archive(final_dir)

        _p("Packing core runtime data", 0.9805)
        self._pack_core_runtime_archive(final_dir)

        _p("Packing optional parallel runtime data", 0.9807)
        self._pack_parallel_runtime_archive(final_dir)

        _p("Packing project content", 0.981)
        self._pack_content_archive(final_dir)

        _p("Auditing packaged payload", 0.984)
        self._write_payload_manifest(final_dir)

        # The in-progress marker protects cleanup while the build is mutable.
        # Final ownership is embedded in BuildManifest so the shipped root has
        # no packaging-only sentinel outside the strict bootstrap surface.
        try:
            os.remove(self._output_marker_path(final_dir))
        except FileNotFoundError:
            pass

        _p("Auditing Player service and package manifest", 0.9847)
        audit_player_package(final_dir, write_manifest=True)

        # Log per-directory size breakdown so the user sees where size goes
        self._report_build_size(final_dir, _blog)

        _p(t("build.step.complete"), 1.0)
        elapsed_seconds = time.perf_counter() - build_start
        done_msg = t("build.completed_log").format(
            path=final_dir,
            seconds=elapsed_seconds,
        )
        Debug.log(done_msg)
        _blog(done_msg)
        return final_dir

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
        try:
            relative_path(absolute, self.project_path)
        except ValueError as exc:
            raise ValueError(
                f"Build scene must be inside the project: {scene_path}"
            ) from exc
        if not absolute.lower().endswith(".scene"):
            raise ValueError(f"Build scene must use the .scene extension: {scene_path}")
        return absolute

    def _validate(self):
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
        if self._player_launcher_path():
            target_dir = os.path.join(target_dir, f"{self.project_name}_Data")
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
        temporary = marker_path + f".{os.getpid()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as marker_file:
                json.dump(marker_payload, marker_file, indent=2, ensure_ascii=False)
                marker_file.write("\n")
            os.replace(temporary, marker_path)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass

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

# Activate Player mode before importing the engine package.
os.environ["_INFERNUX_PLAYER_MODE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

_PLAYER_ROOT = os.path.dirname(os.path.abspath(sys.argv[0]))
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

_DEBUG_MODE = __INFERNUX_DEBUG_MODE__
os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = "1" if _DEBUG_MODE else "0"

def _extract_cached_archive(_archive_path, _cache_kind, _allowed_roots=None):
    if not os.path.isfile(_archive_path):
        raise RuntimeError("Required native Player package is missing: " + _archive_path)
    _manifest = _validate_native_archive_paths(_archive_path, _allowed_roots)
    _expected_hash = str(_manifest.get("archive_sha256", ""))
    if not _expected_hash:
        raise RuntimeError("Native Player package has no archive checksum: " + _archive_path)
    if int(_manifest.get("archive_bytes", -1)) != os.path.getsize(_archive_path):
        raise RuntimeError("Native Player package size mismatch: " + _archive_path)

    # The native manifest has already verified the complete archive.  Pass
    # that trusted result to PlayerBootstrap so startup does not hash the
    # same potentially large package a second time.
    _kind_key = str(_cache_kind).upper()
    os.environ["_INFERNUX_PLAYER_" + _kind_key + "_ARCHIVE_SHA256"] = _expected_hash
    os.environ["_INFERNUX_PLAYER_" + _kind_key + "_ARCHIVE_BYTES"] = str(
        _manifest.get("archive_bytes", -1)
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
    try:
        with open(_ready_marker, "r", encoding="ascii") as _marker:
            if _marker.read().strip() == _expected_hash:
                return _cache_root
    except OSError:
        pass

    _temporary = _cache_root + "." + str(os.getpid()) + ".tmp"
    _remove_player_path(_temporary, ignore_errors=True)
    os.makedirs(_temporary, exist_ok=False)
    try:
        _NATIVE_PACK._inxpack_extract(
            _archive_path,
            _temporary,
            None if _allowed_roots is None else sorted(_allowed_roots),
        )
        with open(os.path.join(_temporary, ".ready"), "w", encoding="ascii") as _marker:
            _marker.write(_expected_hash)
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
_BUILD_MANIFEST_PATH = os.path.join(_DATA_ROOT, "BuildManifest.json")
if os.path.isfile(_BUILD_MANIFEST_PATH):
    _copy_player_file_atomic(
        _BUILD_MANIFEST_PATH,
        os.path.join(_DATA_DIR, "BuildManifest.json"),
    )

_PARALLEL_ARCHIVE = os.path.join(_DATA_ROOT, "Modules", "Parallel.inxmod")
_RUNTIME_MODULE_DIR = ""
if os.path.isfile(_PARALLEL_ARCHIVE):
    _RUNTIME_MODULE_DIR = _extract_cached_archive(
        _PARALLEL_ARCHIVE,
        "parallel",
        {"numba", "llvmlite", "numba.libs", "llvmlite.libs"},
    )
    if _RUNTIME_MODULE_DIR not in sys.path:
        sys.path.insert(0, _RUNTIME_MODULE_DIR)

_DLL_DIR_HANDLES = []
if sys.platform == "win32":
    for _dll_dir in (
        _PLAYER_ROOT,
        _CORE_RUNTIME_DIR,
        _STDLIB_RUNTIME_DIR,
        _INFERNUX_LIB_DIR,
        _RUNTIME_MODULE_DIR,
        os.path.join(_CORE_RUNTIME_DIR, "numpy.libs"),
        os.path.join(_RUNTIME_MODULE_DIR, "llvmlite", "binding"),
        os.path.join(_RUNTIME_MODULE_DIR, "llvmlite.libs"),
    ):
        if not os.path.isdir(_dll_dir):
            continue
        try:
            _DLL_DIR_HANDLES.append(os.add_dll_directory(_dll_dir))
        except OSError:
            pass

_LOGS_DIR = os.path.join(_DATA_ROOT, "Logs")
_LOG = os.path.join(_LOGS_DIR, "player.log")
os.environ["_INFERNUX_PLAYER_LOG"] = _LOG

if _DEBUG_MODE:
    _DEBUG_LOG = os.path.join(_DATA_ROOT, _SAFE_GAME_NAME + "_debug.log")
    _debug_fh = open(_DEBUG_LOG, "w", encoding="utf-8")
    sys.stdout = _debug_fh
    sys.stderr = _debug_fh

def _log(_message):
    try:
        os.makedirs(_LOGS_DIR, exist_ok=True)
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
    _log("boot: importing run_player")
    from Infernux.engine import run_player
    from Infernux.lib import LogLevel
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
        # NumPy is part of the engine runtime (batch APIs, textures, VFX and
        # native ndarray bindings), so every Player must carry it. Numba and
        # llvmlite remain conditional because only the public JIT path needs
        # their bytecode-preserving raw package copies.
        jit_set = NuitkaBuilder._JIT_NOFOLLOW_PACKAGES
        all_pkgs = user_packages or []
        compiled_pkgs = [p for p in all_pkgs if p not in jit_set]
        raw_pkgs = {"numpy"}

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

    def _player_launcher_path(self) -> str:
        # PlayerRelease is a single-process/single-entry product.  The old
        # thin launcher remains available as a legacy CMake target for
        # migration diagnostics, but it must never be copied into a game.
        return ""

    def _player_host_path(self) -> str:
        """Resolve the native single-process host; fail closed if absent."""

        if sys.platform != "win32":
            raise RuntimeError("PlayerHost packaging is currently supported on Windows only")
        candidate = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "resources",
            "player",
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
        """Replace the copied thin launcher's icon without rebuilding the runtime."""
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
        ignore = shutil.ignore_patterns(*self._EXCLUDE_PATTERNS)
        for dirname in self._GAME_DATA_DIRS:
            src = os.path.join(self.project_path, dirname)
            dst = os.path.join(data_dir, dirname)
            if os.path.isdir(src):
                _t0 = time.perf_counter()
                if sys.platform == "win32":
                    os.makedirs(dst, exist_ok=True)
                    rc = subprocess.call(
                        ["robocopy", src, dst, "/E",
                         "/MT:16", "/R:1", "/W:1", "/XJ",
                         "/COPY:DAT", "/DCOPY:DAT",
                         "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                         "/XD", "__pycache__", ".git", "Logs"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x08000000,
                    )
                    if rc >= 8:
                        Debug.log_warning(
                            f"robocopy failed for {dirname}/ (exit {rc}), "
                            f"falling back to shutil.copytree"
                        )
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        shutil.copytree(src, dst, ignore=ignore)
                else:
                    shutil.copytree(src, dst, ignore=ignore)
                Debug.log_internal(
                    f"  copied {dirname}/ in {time.perf_counter() - _t0:.2f}s"
                )

        self._prune_proven_unreachable_staged_assets(data_dir)
        self._prune_player_editor_data(data_dir)
        self._stage_library_runtime_artifacts(data_dir)

        for artifact_kind in ("RenderEffect",):
            artifact_source = os.path.join(
                self.project_path, "Library", "Artifacts", artifact_kind
            )
            if os.path.isdir(artifact_source):
                shutil.copytree(
                    artifact_source,
                    os.path.join(data_dir, "Library", "Artifacts", artifact_kind),
                    dirs_exist_ok=True,
                )

        self._copy_reachable_particle_artifacts(data_dir)
        self._copy_particle_data_interface_artifacts(data_dir)

        self._filter_shipped_requirements(data_dir)

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

    def _prune_proven_unreachable_staged_assets(self, data_dir: str) -> None:
        """Remove only indexed staging files proven outside the build closure.

        Unindexed files remain untouched because they may belong to a future
        dynamic-loading resource group. Such groups need explicit additional
        roots in the complete cooker; guessing from filenames is not safe.
        """

        indexed_paths, reachable_paths = self._project_asset_reachability_evidence()
        unreachable = indexed_paths - reachable_paths
        if not unreachable or not os.path.isdir(data_dir):
            return

        removed = 0
        for root, _dirs, filenames in os.walk(data_dir, topdown=False):
            for filename in filenames:
                path = os.path.join(root, filename)
                try:
                    staged_relative = relative_path(path, data_dir).replace("\\", "/")
                except (OSError, ValueError):
                    continue
                staged_key = staged_relative.casefold()
                source_key = staged_key[:-5] if staged_key.endswith(".meta") else staged_key
                if source_key not in unreachable:
                    continue
                try:
                    os.remove(path)
                    removed += 1
                except FileNotFoundError:
                    continue
            if not same_path(root, data_dir):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        if removed:
            Debug.log_internal(
                f"  pruned {removed} indexed staging files outside the BuildSettings closure"
            )

    @staticmethod
    def _asset_reference_values(value):
        if isinstance(value, dict):
            if value.get("$type") == "asset_ref":
                guid = value.get("guid", "")
                path_hint = value.get("path_hint", "")
                if isinstance(guid, str) and isinstance(path_hint, str) and (guid or path_hint):
                    yield guid, path_hint
            for nested in value.values():
                yield from GameBuilder._asset_reference_values(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from GameBuilder._asset_reference_values(nested)

    def _load_json_asset_references(self, source_path: str) -> set[tuple[str, str]]:
        try:
            with open(source_path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return set()
        return set(self._asset_reference_values(value))

    def _library_source_entry_path(self, entry: dict) -> str:
        raw = str(entry.get("normalized_path", "")).replace("\\", "/")
        candidate = raw if os.path.isabs(raw) else os.path.join(self.project_path, raw)
        return resolved_path(candidate)

    def _collect_library_asset_entries(self, entries: list[dict]) -> dict[str, dict]:
        """Return the current artifact-backed closure rooted at build scenes."""

        by_guid = {str(item["guid"]): item for item in entries}
        by_path = {
            str(item["normalized_path"]).replace("\\", "/").casefold(): item
            for item in entries
        }
        roots: set[str] = set()
        settings_path = os.path.join(self.project_path, "ProjectSettings", "BuildSettings.json")
        try:
            with open(settings_path, "r", encoding="utf-8") as stream:
                scenes = json.load(stream).get("scenes", [])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            scenes = []
        for configured in scenes:
            scene_path = self._resolve_build_scene_path(str(configured))
            scene_key = resolved_path(scene_path).replace("\\", "/").casefold()
            scene_entry = by_path.get(scene_key)
            if scene_entry is None:
                scene_entry = by_path.get(
                    relative_path(scene_path, self.project_path).casefold()
                )
            if scene_entry is not None:
                roots.add(str(scene_entry["guid"]))
                for guid, path_hint in self._load_json_asset_references(scene_path):
                    if guid and guid in by_guid:
                        roots.add(guid)
                    elif path_hint:
                        candidate = path_hint.replace("\\", "/").casefold()
                        roots.update(
                            str(item["guid"])
                            for key, item in by_path.items()
                            if key.endswith(candidate)
                        )

        # A missing build scene list is invalid for a normal build, but keep
        # this helper deterministic for legacy test fixtures that only stage
        # Library assets.  In that case every indexed artifact is a root.
        if not roots:
            roots.update(
                str(item["guid"])
                for item in entries
                if item.get("artifact_path") or logical_asset_type(item) == "particlegraph"
            )

        selected: dict[str, dict] = {}
        pending = sorted(roots)
        while pending:
            guid = pending.pop(0)
            if guid in selected or guid not in by_guid:
                continue
            entry = by_guid[guid]
            selected[guid] = entry
            for dependency in sorted(entry.get("dependencies", [])):
                if dependency not in selected:
                    pending.append(str(dependency))
            source_path = self._library_source_entry_path(entry)
            if os.path.isfile(source_path) and Path(source_path).suffix.casefold() in self._PLAYER_PORTABLE_DOCUMENT_SUFFIXES:
                for dependency_guid, dependency_path in self._load_json_asset_references(source_path):
                    if dependency_guid in by_guid:
                        pending.append(dependency_guid)
                    elif dependency_path:
                        suffix = dependency_path.replace("\\", "/").casefold()
                        pending.extend(
                            str(item["guid"])
                            for key, item in by_path.items()
                            if key.endswith(suffix)
                        )
        return selected

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

        index_path = os.path.join(self.project_path, "Library", "AssetIndex.json")
        if not os.path.isfile(index_path):
            return
        try:
            entries = load_asset_index(self.project_path)
            selected = self._collect_library_asset_entries(entries)
        except RuntimeArtifactError as exc:
            raise RuntimeError(f"Library artifact selection failed: {exc}") from exc

        by_guid = {str(entry["guid"]): entry for entry in entries}
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
                artifact_paths.append(f"Library/Artifacts/Particle/{stable_id}.inxparticle")
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

    def _copy_reachable_particle_artifacts(self, data_dir: str) -> None:
        """Copy only Particle artifacts referenced by scenes in BuildSettings."""
        asset_index_path = os.path.join(
            self.project_path, "Library", "AssetIndex.json"
        )
        if not os.path.isfile(asset_index_path):
            raise RuntimeError(
                "Player build requires the current Library/AssetIndex.json "
                "before selecting runtime artifacts"
            )
        references = self._collect_reachable_particle_artifacts()
        destination_root = os.path.join(
            data_dir, "Library", "Artifacts", "Particle"
        )

        # With the current AssetIndex, _stage_library_runtime_artifacts is the
        # only authority allowed to select and validate a Particle artifact.
        # This method only preserves the runtime lookup index; it must never
        # copy a stale file around that validation path.
        for item in references:
            destination = os.path.join(
                destination_root, item["stable_id"] + ".inxparticle"
            )
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
            with open(index_path, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")

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
        result: dict[str, str] = {}
        assets_root = os.path.join(self.project_path, "Assets")
        for root, _dirs, filenames in os.walk(assets_root):
            for filename in filenames:
                if not filename.endswith(".meta"):
                    continue
                meta_path = os.path.join(root, filename)
                try:
                    with open(meta_path, "r", encoding="utf-8") as stream:
                        metadata = json.load(stream).get("metadata", {})
                    guid = metadata.get("guid", {}).get("value", "")
                except (OSError, AttributeError, json.JSONDecodeError):
                    continue
                source_path = meta_path[:-5]
                if guid and os.path.isfile(source_path):
                    result[str(guid)] = source_path
        return result

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
                    for item in load_asset_index(self.project_path)
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
            if not self.enable_jit and re.match(r"^\s*numba\b", line, re.IGNORECASE):
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
        # Excluded editor-only / build-only packages
        "watchdog", "PIL", "cv2", "imageio", "psd_tools",
        "mcp", "fastmcp",
        "tkinter", "unittest", "test", "pip", "setuptools",
        "distutils", "ensurepip",
    })

    # ------------------------------------------------------------------
    # Compile user scripts
    # ------------------------------------------------------------------

    def _compile_user_scripts(self, final_dir: str):
        """Compile .py in Data/Assets/ to .pyc and remove originals.

        Also generates ``Data/_script_guid_map.json`` so that the
        player can resolve script GUIDs without the original ``.py``
        files (the C++ AssetDatabase only recognises ``.py``).
        """
        assets_dir = os.path.join(final_dir, "Data", "Assets")
        if not os.path.isdir(assets_dir):
            return

        _compile_t0 = time.perf_counter()
        _compile_count = 0
        data_dir = os.path.join(final_dir, "Data")
        guid_map: dict[str, str] = {}

        # First pass: build GUID → .pyc relative-path map from .meta
        for root, _dirs, files in os.walk(assets_dir):
            for fname in files:
                if fname.endswith(".py"):
                    py_path = os.path.join(root, fname)
                    meta_path = py_path + ".meta"
                    if os.path.isfile(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as mf:
                                meta = json.load(mf)
                            guid = (meta.get("metadata", {})
                                        .get("guid", {})
                                        .get("value", ""))
                            if guid:
                                pyc_rel = relative_path(py_path + "c", data_dir)
                                guid_map[guid] = pyc_rel
                        except (json.JSONDecodeError, OSError) as _exc:
                            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                            pass

        # Second pass: compile and remove originals
        for root, _dirs, files in os.walk(assets_dir):
            for fname in files:
                if fname.endswith(".py"):
                    py_path = os.path.join(root, fname)
                    _compile_count += 1
                    try:
                        with open(py_path, "r", encoding="utf-8") as sf:
                            source_text = sf.read()
                        sidecar_source = _jit_kernels.build_auto_parallel_sidecar_source(source_text)
                        if sidecar_source:
                            sidecar_py = py_path[:-3] + ".autop.py"
                            with open(sidecar_py, "w", encoding="utf-8", newline="\n") as apf:
                                apf.write(sidecar_source)
                            py_compile.compile(
                                sidecar_py,
                                cfile=sidecar_py + "c",
                                dfile=relative_path(sidecar_py, data_dir),
                                optimize=2,
                                doraise=True,
                            )
                            os.remove(sidecar_py)
                            Debug.log_internal(
                                f"  auto_parallel sidecar: {os.path.basename(sidecar_py)}c"
                            )
                    except (OSError, SyntaxError, py_compile.PyCompileError) as _sc_exc:
                        Debug.log_warning(
                            f"  auto_parallel sidecar generation failed for "
                            f"{fname}: {_sc_exc}"
                        )

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
            with open(manifest_path, "w", encoding="utf-8") as mf:
                json.dump(guid_map, mf)

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

    def _pack_core_runtime_archive(self, final_dir: str) -> None:
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
            if filename.casefold() in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS:
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
                    # The legacy editor launcher can live below the engine
                    # resource directory. A Player has one executable only;
                    # do not hide another executable inside Runtime.inxrt.
                    if filename.casefold().endswith(".exe"):
                        continue
                    if filename.casefold() in NuitkaBuilder._LEGACY_STATIC_SHADER_DLLS:
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
        native_manifest = write_pack(
            files,
            archive_path,
            profile=self._player_inxpack_profile(),
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

    def _pack_player_bootstrap_archive(self, final_dir: str) -> None:
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
        native_manifest = write_pack(
            sources,
            archive_path,
            profile=self._player_inxpack_profile(),
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

    def _pack_content_archive(self, final_dir: str) -> None:
        """Write project data as the native Content.inxpkg container."""

        data_root = os.path.join(final_dir, f"{self.project_name}_Data")
        if not os.path.isdir(data_root):
            raise RuntimeError("Player Data directory is missing after layout organization")

        archive_path = os.path.join(data_root, self._CONTENT_ARCHIVE_FILENAME)
        files: list[tuple[str, str]] = []
        excluded_files: list[str] = []
        source_bytes = 0
        forbidden_plaintext: list[str] = []

        for root, dirs, filenames in os.walk(data_root):
            dirs[:] = [directory for directory in dirs if directory != "Logs"]
            for filename in filenames:
                path = os.path.join(root, filename)
                relative = relative_path(path, data_root)
                portable_relative = relative.replace(os.sep, "/")
                if portable_relative in self._PLAYER_CONTENT_CONTROL_RELATIVE_PATHS:
                    continue
                source_paths = getattr(self, "_runtime_artifact_source_paths", set())
                if portable_relative.casefold() in source_paths:
                    excluded_files.append(path)
                    continue
                if os.path.splitext(portable_relative)[1].casefold() in NATIVE_ARCHIVE_SUFFIXES:
                    continue
                if self._is_player_editor_path(portable_relative):
                    excluded_files.append(path)
                    continue
                if self._is_particle_authoring_payload(relative):
                    excluded_files.append(path)
                    continue
                suffix = os.path.splitext(filename)[1].lower()
                if suffix == ".meta":
                    excluded_files.append(path)
                    continue
                if suffix in {
                    ".py", ".pyi", ".pyx", ".lua", ".cpp", ".c", ".cc",
                    ".h", ".hpp", ".glsl", ".vert", ".frag", ".comp", ".geom",
                    ".tesc", ".tese", ".hlsl", ".shader",
                }:
                    forbidden_plaintext.append(relative)
                self._rewrite_player_document_paths(path, suffix)
                files.append((relative, path))
                source_bytes += os.path.getsize(path)

        if forbidden_plaintext:
            raise RuntimeError(
                "Player content contains authoring/source files that must be "
                "compiled into Library artifacts first: "
                + ", ".join(sorted(forbidden_plaintext)[:12])
            )
        if not files:
            raise RuntimeError("Player Content.inxpkg would be empty")

        native_manifest = write_pack(
            files,
            archive_path,
            profile=self._player_inxpack_profile(),
        )
        for _relative, source_path in files:
            os.remove(source_path)
        for excluded_path in excluded_files:
            try:
                os.remove(excluded_path)
            except FileNotFoundError:
                pass
        for root, dirs, _files in os.walk(data_root, topdown=False):
            for directory in dirs:
                path = os.path.join(root, directory)
                if os.path.basename(path) == "Logs":
                    continue
                try:
                    os.rmdir(path)
                except OSError:
                    pass

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
        if not changed:
            return
        with open(path, "w", encoding="utf-8", newline="\n") as destination:
            json.dump(rewritten, destination, ensure_ascii=False, separators=(",", ":"))
            destination.write("\n")

    def _write_payload_manifest(self, final_dir: str) -> None:
        """Write the deterministic runtime artifact catalog.

        The package TOCs are the source of truth after authoring files have
        been packed.  The catalog therefore never depends on an editor path
        or on filesystem traversal order.
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
                    if payload_kind == "serialized_runtime_document" or (
                        logical_type == "runtime_metadata"
                        and entry_path.casefold().endswith(".json")
                    ):
                        # Payload is optional catalog input used only for
                        # asset_ref dependency discovery. Never inflate native,
                        # bytecode, shader, NumPy or compiled artifact entries.
                        package_entry["payload"] = read_entry(
                            package_path,
                            entry_path,
                        )
                    binding = getattr(self, "_runtime_artifact_bindings", {}).get(entry_path)
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
        catalog = build_catalog(
            package_entries,
            player_host={
                "executable": relative_path(executable, final_dir),
                "sha256": self._sha256_file(executable),
                "identity": "nuitka-player-host",
            },
            package_records=packages,
        )
        proven_residual_assets = self._residual_direct_runtime_assets(package_entries)
        if proven_residual_assets:
            raise RuntimeError(
                "Player asset cook/prune gate rejected proven residual assets: "
                + ", ".join(proven_residual_assets)
            )
        catalog_path = os.path.join(library_root, "RuntimeAssetCatalog.json")
        with open(catalog_path, "w", encoding="utf-8") as catalog_file:
            json.dump(catalog, catalog_file, indent=2, sort_keys=True)
            catalog_file.write("\n")

    def _residual_direct_runtime_assets(
        self,
        package_entries: list[dict[str, object]],
    ) -> list[str]:
        """Return only residual project payloads supported by hard evidence.

        Serialized documents and direct audio are valid current runtime inputs.
        Until the complete cooker exists, absence of an artifact is not proof
        that an asset is stale. We reject only indexed assets outside the
        BuildSettings closure, sources already replaced by a current Library
        artifact, and explicit build-only payloads. Unproven candidates remain
        visible to the package audit without breaking a real Player build.
        """

        indexed_paths, reachable_paths = self._project_asset_reachability_evidence()
        replaced_sources = {
            str(path).replace("\\", "/").casefold()
            for path in getattr(self, "_runtime_artifact_source_paths", set())
        }
        for binding in getattr(self, "_runtime_artifact_bindings", {}).values():
            if not isinstance(binding, dict):
                continue
            source_path = binding.get("source_path")
            if isinstance(source_path, str) and source_path:
                replaced_sources.add(source_path.replace("\\", "/").casefold())

        proven: list[str] = []
        unresolved: list[str] = []
        for entry in package_entries:
            package = str(entry.get("package", ""))
            runtime_path = str(entry.get("runtime_path", "")).replace("\\", "/")
            if not package.casefold().endswith(GameBuilder._CONTENT_ARCHIVE_FILENAME.casefold()):
                continue
            path_key = runtime_path.casefold()
            if self._is_explicit_build_only_content_path(runtime_path):
                proven.append(runtime_path)
                continue
            if path_key in replaced_sources:
                proven.append(runtime_path)
                continue
            logical_type = logical_type_for_path(runtime_path)
            if payload_kind_for(logical_type) not in {
                "serialized_runtime_document",
                "direct_runtime_asset",
            }:
                continue
            if path_key in indexed_paths:
                if path_key not in reachable_paths:
                    proven.append(runtime_path)
                continue
            unresolved.append(runtime_path)

        self._unproven_residual_runtime_assets = sorted(set(unresolved))
        if unresolved:
            Debug.log_warning(
                "Player asset reachability is not yet provable for: "
                + ", ".join(sorted(set(unresolved))[:12])
                + ". Keeping current runtime payload until the complete cook graph is available."
            )
        return sorted(set(proven))

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
            entries = load_asset_index(self.project_path)
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
        data["scenes"] = rel_scenes

        with open(bs, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

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
            "build_output": {
                "tool": "Infernux",
                "project_name": self.project_name,
                "project_identity": path_fingerprint(self.project_path),
            },
        }

        manifest_path = os.path.join(final_dir, "Data", "BuildManifest.json")
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

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
        _queue_dir(os.path.join(final_dir, "Infernux", "resources", "supports"))

        # Build-time-only video packages — av (PyAV/ffmpeg) and imageio
        # are used only for splash video encoding at build time.  The
        # player reads pre-extracted .infsplash blobs via struct.
        for _build_pkg in ("av", "av.libs", "imageio"):
            _queue_dir(os.path.join(final_dir, _build_pkg))

        for _mcp_pkg in self._GAME_BUILD_EXCLUDED_PACKAGES:
            _queue_dir(os.path.join(final_dir, _mcp_pkg))
        _queue_dir(os.path.join(final_dir, "Infernux", "mcp"))

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
        _queue_file(os.path.join(final_dir, "Infernux", "lib", "InfernuxLauncher.exe"))
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

        # Project metadata remains under Data/Assets because it carries the
        # stable GUID identity referenced by scenes and other assets. Engine
        # package metadata is build-time authoring state and is never shipped.
        for metadata_root in (os.path.join(final_dir, "Infernux"),):
            if not os.path.isdir(metadata_root):
                continue
            for root, _, files in os.walk(metadata_root):
                for fname in files:
                    if fname.endswith(".meta"):
                        _queue_file(os.path.join(root, fname))

        # ── Global cleanup: __pycache__, .dist-info, and stale .pyc ──
        jit_dirs = {os.path.join(final_dir, p) for p in ("numba", "llvmlite", "numpy")}
        for root, dirs, files in os.walk(final_dir, topdown=False):
            for dname in dirs:
                if dname == "__pycache__" or dname.endswith(".dist-info"):
                    dirs_to_remove.append(os.path.join(root, dname))
            # Remove stale .pyc from raw-copied JIT packages
            if any(root == jd or root.startswith(jd + os.sep) for jd in jit_dirs):
                for fname in files:
                    if fname.endswith(".pyc"):
                        files_to_remove.append(os.path.join(root, fname))
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
