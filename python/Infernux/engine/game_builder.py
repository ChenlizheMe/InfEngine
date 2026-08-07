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
        Content.inxpack      ← deterministic Player content container
            Content.json        ← integrity and cache manifest
            BuildManifest.json  ← display mode and boot settings
            Runtime/            ← private CPython/Nuitka/native engine payload
            RuntimeModules/
                core/           ← compressed NumPy and engine resources
                parallel/       ← optional compressed Numba/LLVM module
"""

from __future__ import annotations

import io
import json
import hashlib
import os
import py_compile
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import Infernux._jit_kernels as _jit_kernels
import Infernux.resources as _resources
from Infernux.debug import Debug
from Infernux.engine.build_cancellation import BuildCancelled
from Infernux.engine.i18n import t
from Infernux.engine.nuitka_builder import NuitkaBuilder
from Infernux.engine.player_package_audit import audit_player_package
from Infernux.engine.player_package_format import write_pack
from Infernux.engine.path_utils import (
    path_fingerprint,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
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

from ._build_splash import BuildSplashMixin
from ._build_dependencies import BuildDependencyMixin


class GameBuilder(BuildSplashMixin, BuildDependencyMixin):
    """Build a standalone native game distribution using Nuitka."""

    OUTPUT_MARKER_FILENAME = ".infernux-build-output"
    _BUILD_TEMP_DIR_NAME = "_build_temp"
    _GAME_DATA_DIRS = ["Assets", "ProjectSettings", "materials"]
    _EXCLUDE_PATTERNS = {"__pycache__", ".git", ".gitignore", ".infernux-engine-lock.json"}
    _ICON_EXTS = {".png", ".jpg", ".jpeg", ".ico"}
    _GAME_BUILD_EXCLUDED_PACKAGES = frozenset({"mcp", "fastmcp"})
    _PLAYER_EXCLUDED_CONTENT_RELATIVE_PATHS = frozenset(
        {
            "ProjectSettings/agent_tools.json",
            "ProjectSettings/mcp_capabilities.json",
        }
    )
    _PLAYER_EXCLUDED_PARTICLE_AUTHORING_SUFFIXES = (
        ".particlegraph",
        ".particlegraph.meta",
        ".particle.py",
        ".particle.py.meta",
        ".particle.pyc",
        ".particle.pyc.meta",
    )
    _CONTENT_ARCHIVE_FILENAME = "Content.inxpack"
    _CONTENT_MANIFEST_FILENAME = "Content.json"
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

        # ── Build log file ────────────────────────────────────────────
        log_dir = os.path.join(self.project_path, "Logs")
        os.makedirs(log_dir, exist_ok=True)
        build_log_path = os.path.join(log_dir, "build.log")
        build_log = open(build_log_path, "w", encoding="utf-8")

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
            return self._build_inner(_p, _blog, on_progress, cancel_event, build_start)
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

    def _build_inner(self, _p, _blog, on_progress, cancel_event, build_start) -> str:
        """Internal build pipeline (separated for clean exception handling)."""

        _p(t("build.step.validating"), 0.00)
        self._validate()

        _p(t("build.step.cleaning_output"), 0.02)
        self._clean_output()

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

        _p("Packing core runtime data", 0.9805)
        self._pack_core_runtime_archive(final_dir)

        _p("Packing optional parallel runtime data", 0.9807)
        self._pack_parallel_runtime_archive(final_dir)

        _p("Packing project content", 0.981)
        self._pack_content_archive(final_dir)

        _p("Auditing packaged payload", 0.984)
        self._write_payload_manifest(final_dir)

        _p("Organizing Player distribution", 0.9845)
        self._organize_player_layout(final_dir)

        _p("Auditing Player service and package manifest", 0.9847)
        audit_player_package(final_dir, write_manifest=True)

        _p(t("build.step.writing_marker"), 0.985)
        self._write_output_marker(final_dir)

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
        if entries == [self._BUILD_TEMP_DIR_NAME]:
            temp_dir = os.path.join(self.output_dir, self._BUILD_TEMP_DIR_NAME)
            if os.path.isdir(temp_dir) and not os.path.islink(temp_dir):
                return

        if os.path.isfile(self._output_marker_path(self.output_dir)):
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

    def _write_output_marker(self, final_dir: str) -> None:
        marker_path = self._output_marker_path(final_dir)
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        marker_payload = {
            "tool": "Infernux",
            "kind": "build-output",
            "project_name": self.project_name,
            "project_identity": path_fingerprint(self.project_path),
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(marker_payload, f, indent=2, ensure_ascii=False)
            f.write("\n")

    # ------------------------------------------------------------------
    # Generate boot script (temporary, fed to Nuitka)
    # ------------------------------------------------------------------

    def _generate_boot_script(self) -> str:
        """Generate the entry script that Nuitka will compile into the EXE.

        Returns the path to the temporary boot script.
        """
        boot_src = '''\
"""Infernux Game — compiled entry point."""
import hashlib
import json
import os
import shutil
import struct
import sys
import traceback
import lzma

# Activate player mode BEFORE any Infernux imports so the engine
# package skips heavy editor-only UI panels and watchdog file watcher.
os.environ["_INFERNUX_PLAYER_MODE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

# Resolve the private runtime and public data roots. The Windows launcher sets
# these explicitly; the fallback keeps older flat distributions portable.
_DIR = os.environ.get("_INFERNUX_PLAYER_RUNTIME_ROOT", "").strip()
if not _DIR:
    _DIR = os.path.dirname(resolved_path(sys.argv[0]))
    if not os.path.isdir(os.path.join(_DIR, "Data")):
        _DIR = os.path.dirname(resolved_path(sys.executable))
_DATA_ROOT = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
if not _DATA_ROOT:
    _DATA_ROOT = os.path.join(_DIR, "Data")
_MODULE_ROOT = os.environ.get("_INFERNUX_PLAYER_MODULE_ROOT", "").strip()
if not _MODULE_ROOT:
    _MODULE_ROOT = os.path.join(_DIR, "RuntimeModules")

_BUILD_MANIFEST = {}
try:
    with open(os.path.join(_DATA_ROOT, "BuildManifest.json"), "r", encoding="utf-8") as _mf:
        _BUILD_MANIFEST = json.load(_mf)
except (OSError, ValueError):
    pass
_DEBUG_MODE = bool(_BUILD_MANIFEST.get("debug_build", False))
_GAME_NAME = str(_BUILD_MANIFEST.get("game_name", "InfernuxPlayer") or "InfernuxPlayer")
_SAFE_GAME_NAME = "".join(_ch if _ch not in '<>:"/\\\\|?*' else '_' for _ch in _GAME_NAME)
os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = "1" if _DEBUG_MODE else "0"

def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _read_json(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)

_PLAYER_PACK_MAGIC = b"INXPCK1\\x00"

def _read_player_pack_manifest(archive_path):
    with open(archive_path, "rb") as source:
        if source.read(len(_PLAYER_PACK_MAGIC)) != _PLAYER_PACK_MAGIC:
            raise RuntimeError("Unsupported Player container: " + archive_path)
        raw_length = source.read(8)
        if len(raw_length) != 8:
            raise RuntimeError("Truncated Player container header: " + archive_path)
        header_length = struct.unpack("<Q", raw_length)[0]
        if header_length > 128 * 1024 * 1024:
            raise RuntimeError("Player container header is too large: " + archive_path)
        header = json.loads(source.read(header_length).decode("utf-8"))
    if not isinstance(header, dict) or header.get("format") != "infernux-player-pack":
        raise RuntimeError("Invalid Player container manifest: " + archive_path)
    return header, len(_PLAYER_PACK_MAGIC) + 8 + header_length

def _extract_cached_archive(archive_path, manifest_path, cache_kind, allowed_roots=None):
    manifest = _read_json(manifest_path)
    expected_hash = str(manifest.get("archive_sha256", ""))
    if (
        not expected_hash
        or int(manifest.get("archive_bytes", -1)) != os.path.getsize(archive_path)
        or _sha256_file(archive_path) != expected_hash
    ):
        raise RuntimeError("Packaged archive failed integrity validation: " + archive_path)

    cache_parent = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CACHE_HOME")
        or os.path.join(os.path.expanduser("~"), ".cache")
    )
    cache_root = os.path.join(
        cache_parent,
        "Infernux",
        "PlayerCache",
        _SAFE_GAME_NAME,
        cache_kind + "-" + expected_hash[:20],
    )
    ready_marker = os.path.join(cache_root, ".ready")
    try:
        with open(ready_marker, "r", encoding="ascii") as marker:
            if marker.read().strip() == expected_hash:
                return cache_root
    except OSError:
        pass

    temporary = cache_root + "." + str(os.getpid()) + ".tmp"
    shutil.rmtree(temporary, ignore_errors=True)
    os.makedirs(temporary, exist_ok=False)
    try:
        header, payload_base = _read_player_pack_manifest(archive_path)
        entries = header.get("files", [])
        if (
            len(entries) != int(manifest.get("file_count", -1))
            or int(header.get("raw_bytes", -1))
            != int(manifest.get("uncompressed_bytes", -1))
        ):
            raise RuntimeError("Packaged archive does not match its manifest")
        package_size = os.path.getsize(archive_path)
        with open(archive_path, "rb") as source:
            for entry in entries:
                normalized = str(entry.get("path", "")).replace("\\\\", "/")
                parts = tuple(normalized.split("/"))
                if normalized.startswith("/") or not parts or any(
                    part in {"", ".", ".."} for part in parts
                ) or ":" in parts[0]:
                    raise RuntimeError("Unsafe packaged archive entry: " + normalized)
                if allowed_roots is not None and parts[0] not in allowed_roots:
                    raise RuntimeError("Unexpected runtime module entry: " + normalized)
                offset = int(entry.get("offset", -1))
                stored_bytes = int(entry.get("stored_bytes", -1))
                raw_bytes = int(entry.get("raw_bytes", -1))
                if offset < 0 or stored_bytes < 0 or payload_base + offset + stored_bytes > package_size:
                    raise RuntimeError("Out-of-range packaged entry: " + normalized)
                source.seek(payload_base + offset)
                data = source.read(stored_bytes)
                if entry.get("codec") == "lzma":
                    data = lzma.decompress(data)
                elif entry.get("codec") != "store":
                    raise RuntimeError("Unknown packaged codec: " + normalized)
                if len(data) != raw_bytes:
                    raise RuntimeError("Packaged entry size mismatch: " + normalized)
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry.get("sha256"):
                    raise RuntimeError("Packaged entry checksum mismatch: " + normalized)
                output = os.path.join(temporary, *parts)
                os.makedirs(os.path.dirname(output), exist_ok=True)
                with open(output, "wb") as destination:
                    destination.write(data)
        with open(os.path.join(temporary, ".ready"), "w", encoding="ascii") as marker:
            marker.write(expected_hash)
        os.makedirs(os.path.dirname(cache_root), exist_ok=True)
        if os.path.isdir(cache_root):
            try:
                with open(ready_marker, "r", encoding="ascii") as marker:
                    if marker.read().strip() == expected_hash:
                        return cache_root
            except OSError:
                pass
            shutil.rmtree(cache_root, ignore_errors=True)
        try:
            os.replace(temporary, cache_root)
        except OSError:
            if not os.path.isfile(ready_marker):
                raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return cache_root

_CORE_ROOT = os.path.join(_MODULE_ROOT, "core")
_CORE_MANIFEST = os.path.join(_CORE_ROOT, "core-module.json")
_CORE_ARCHIVE = os.path.join(_CORE_ROOT, "core-module.inxpack")
_CORE_RUNTIME_DIR = ""
if os.path.isfile(_CORE_MANIFEST) and os.path.isfile(_CORE_ARCHIVE):
    _core_data = _read_json(_CORE_MANIFEST)
    _CORE_RUNTIME_DIR = _extract_cached_archive(
        _CORE_ARCHIVE,
        _CORE_MANIFEST,
        "core",
        set(_core_data.get("allowed_roots", [])),
    )
    if _CORE_RUNTIME_DIR not in sys.path:
        sys.path.insert(0, _CORE_RUNTIME_DIR)
    os.environ["_INFERNUX_PACKAGED_RESOURCE_ROOT"] = os.path.join(
        _CORE_RUNTIME_DIR, "Infernux", "resources"
    )

_DATA_DIR = _DATA_ROOT
_CONTENT_ARCHIVE = os.path.join(_DATA_DIR, "Content.inxpack")
_CONTENT_MANIFEST = os.path.join(_DATA_DIR, "Content.json")
if os.path.isfile(_CONTENT_ARCHIVE) and os.path.isfile(_CONTENT_MANIFEST):
    _DATA_DIR = _extract_cached_archive(
        _CONTENT_ARCHIVE,
        _CONTENT_MANIFEST,
        "content",
    )
    shutil.copy2(
        os.path.join(_DATA_ROOT, "BuildManifest.json"),
        os.path.join(_DATA_DIR, "BuildManifest.json"),
    )

_PARALLEL_ROOT = os.path.join(_MODULE_ROOT, "parallel")
_PARALLEL_MANIFEST = os.path.join(_PARALLEL_ROOT, "parallel-module.json")
_PARALLEL_ARCHIVE = os.path.join(_PARALLEL_ROOT, "parallel-module.inxpack")
_RUNTIME_MODULE_DIR = ""
if os.path.isfile(_PARALLEL_MANIFEST) and os.path.isfile(_PARALLEL_ARCHIVE):
    _parallel_data = _read_json(_PARALLEL_MANIFEST)
    _allowed_packages = set(_parallel_data.get("packages", []))
    _allowed_packages.update(_package + ".libs" for _package in tuple(_allowed_packages))
    _RUNTIME_MODULE_DIR = _extract_cached_archive(
        _PARALLEL_ARCHIVE,
        _PARALLEL_MANIFEST,
        "parallel",
        _allowed_packages,
    )
    if _RUNTIME_MODULE_DIR not in sys.path:
        sys.path.insert(0, _RUNTIME_MODULE_DIR)

# Ensure the raw-copied NumPy runtime and optional JIT packages are importable.
# Nuitka standalone may not include the exe directory in sys.path by default.
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

# On Windows, add the exe directory as a DLL search path so that
# native extensions inside raw-copied packages can find their .dll deps.
_DLL_DIR_HANDLES = []
if sys.platform == 'win32':
    try:
        _DLL_DIR_HANDLES.append(os.add_dll_directory(_DIR))
    except OSError:
        pass
    if _RUNTIME_MODULE_DIR:
        for _dll_dir in (
            _RUNTIME_MODULE_DIR,
            os.path.join(_RUNTIME_MODULE_DIR, "llvmlite", "binding"),
            os.path.join(_RUNTIME_MODULE_DIR, "llvmlite.libs"),
        ):
            if not os.path.isdir(_dll_dir):
                continue
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(_dll_dir))
            except OSError:
                pass
    if _CORE_RUNTIME_DIR:
        for _dll_dir in (
            _CORE_RUNTIME_DIR,
            os.path.join(_CORE_RUNTIME_DIR, "numpy.libs"),
        ):
            if not os.path.isdir(_dll_dir):
                continue
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(_dll_dir))
            except OSError:
                pass
    # Pre-load bundled MSVC CRT DLLs so the dynamic linker can resolve
    # them even on machines without Visual C++ Redistributable installed.
    import ctypes as _ctypes
    for _crt in ('vcruntime140.dll', 'vcruntime140_1.dll',
                 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
                 'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll',
                 'concrt140.dll'):
        _crt_path = os.path.join(_DIR, _crt)
        if os.path.isfile(_crt_path):
            try:
                _ctypes.WinDLL(_crt_path)
            except OSError:
                pass
    del _ctypes

# Logs go into Data/Logs/ to keep the root directory clean
_LOGS_DIR = os.path.join(_DATA_ROOT, "Logs")
os.makedirs(_LOGS_DIR, exist_ok=True)
_LOG = os.path.join(_LOGS_DIR, "player.log")
os.environ["_INFERNUX_PLAYER_LOG"] = _LOG

# Debug mode: write a detailed log next to the executable
if _DEBUG_MODE:
    _DEBUG_LOG = os.path.join(_DATA_ROOT, _SAFE_GAME_NAME + "_debug.log")
    _debug_fh = open(_DEBUG_LOG, "w", encoding="utf-8")
    sys.stdout = _debug_fh
    sys.stderr = _debug_fh

# Clear previous log
try:
    open(_LOG, "w", encoding="utf-8").close()
except OSError:
    pass

def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as _f:
            _f.write(str(msg) + "\\n")
    except OSError:
        pass
    if _DEBUG_MODE:
        print(msg, flush=True)

def _crash_report(exc):
    """Write crash details to a log file and show a Windows message box."""
    tb_text = traceback.format_exc()
    _log("CRASH: " + tb_text)
    log_path = os.path.join(_LOGS_DIR, "crash.log")
    try:
        with open(log_path, "w", encoding="utf-8") as _f:
            _f.write(tb_text)
    except OSError:
        pass
    # Supervisor-managed Debug validation must remain remotely recoverable.
    # The process exits after logging instead of blocking on a platform modal.
    if os.environ.get("_INFERNUX_PLAYER_CONTROL_FILE"):
        return
    # Try to show a native message box (works even without console)
    try:
        import ctypes
        msg = f"Failed to start.  Details in crash.log\\n\\n" + tb_text[-800:]
        ctypes.windll.user32.MessageBoxW(0, msg, "Infernux Error", 0x10)
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
        # Write boot script to a temp location (NuitkaBuilder will copy
        # it into its ASCII-safe staging directory).
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

        runtime_executable = "InfernuxPlayer.exe" if sys.platform == "win32" else "InfernuxPlayer"
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
            output_filename=runtime_executable,
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
            ):
                raise RuntimeError("Unable to stage the parallel Runtime Module")
        return dist_dir

    def _player_launcher_path(self) -> str:
        # PlayerRelease is a single-process/single-entry product.  The old
        # thin launcher remains available as a legacy CMake target for
        # migration diagnostics, but it must never be copied into a game.
        return ""

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

        runtime_name = "InfernuxPlayer.exe" if sys.platform == "win32" else "InfernuxPlayer"
        game_name = f"{self.project_name}.exe" if sys.platform == "win32" else self.project_name
        runtime_executable = os.path.join(final_dir, runtime_name)
        game_executable = os.path.join(final_dir, game_name)
        if (
            not self._player_launcher_path()
            and os.path.isfile(runtime_executable)
            and not same_path(runtime_executable, game_executable)
        ):
            os.replace(runtime_executable, game_executable)

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
        """Hide implementation payload behind a Unity-style Windows layout."""
        launcher_path = self._player_launcher_path()
        if not launcher_path:
            return

        data_source = os.path.join(final_dir, "Data")
        if not os.path.isdir(data_source):
            raise RuntimeError("Player Data directory is missing before layout organization")

        data_name = f"{self.project_name}_Data"
        data_root = os.path.join(final_dir, data_name)
        runtime_root = os.path.join(data_root, "Runtime")
        module_source = os.path.join(final_dir, "RuntimeModules")
        module_target = os.path.join(data_root, "RuntimeModules")
        game_executable = os.path.join(
            final_dir,
            f"{self.project_name}.exe",
        )

        if os.path.exists(data_root):
            shutil.rmtree(data_root)
        os.replace(data_source, data_root)
        if os.path.isdir(module_source):
            os.replace(module_source, module_target)
        os.makedirs(runtime_root, exist_ok=True)

        for name in list(os.listdir(final_dir)):
            if name == data_name:
                continue
            source = os.path.join(final_dir, name)
            destination = os.path.join(runtime_root, name)
            if os.path.exists(destination):
                if os.path.isdir(destination):
                    shutil.rmtree(destination)
                else:
                    os.remove(destination)
            shutil.move(source, destination)

        shutil.copy2(launcher_path, game_executable)
        if self.icon_path:
            self._apply_windows_executable_icon(game_executable, self.icon_path)
        layout_manifest = {
            "layout": "infernux-windows-player",
            "launcher": os.path.basename(game_executable),
            "data_directory": data_name,
            "runtime_directory": "Runtime",
            "runtime_modules_directory": "RuntimeModules",
        }
        with open(
            os.path.join(data_root, "PlayerLayout.json"),
            "w",
            encoding="utf-8",
        ) as manifest_file:
            json.dump(layout_manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")

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
                         "/XD", "__pycache__", ".git"],
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

    def _copy_reachable_particle_artifacts(self, data_dir: str) -> None:
        """Copy only Particle artifacts referenced by scenes in BuildSettings."""
        source_root = os.path.join(
            self.project_path, "Library", "Artifacts", "Particle"
        )
        if not os.path.isdir(source_root):
            return

        references = self._collect_reachable_particle_artifacts()
        stable_ids = {item["stable_id"] for item in references}
        destination_root = os.path.join(
            data_dir, "Library", "Artifacts", "Particle"
        )
        for stable_id in sorted(stable_ids):
            filename = stable_id + ".inxparticle"
            source = os.path.join(source_root, filename)
            if not os.path.isfile(source):
                raise RuntimeError(
                    "Reachable ParticleGraph has no current AOT artifact: "
                    f"Library/Artifacts/Particle/{filename}"
                )
            os.makedirs(destination_root, exist_ok=True)
            shutil.copy2(source, os.path.join(destination_root, filename))

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

    def _pack_core_runtime_archive(self, final_dir: str) -> None:
        """Compress always-required Python data that can load from cache."""
        module_root = os.path.join(final_dir, "RuntimeModules", "core")
        os.makedirs(module_root, exist_ok=True)
        archive_path = os.path.join(module_root, "core-module.inxpack")
        manifest_path = os.path.join(module_root, "core-module.json")
        roots = [
            os.path.join(final_dir, "numpy"),
            os.path.join(final_dir, "numpy.libs"),
            os.path.join(final_dir, "Infernux", "resources"),
        ]
        files: list[tuple[str, str]] = []
        uncompressed_bytes = 0
        for payload_root in roots:
            if not os.path.isdir(payload_root):
                continue
            for root, _dirs, filenames in os.walk(payload_root):
                for filename in filenames:
                    source_path = os.path.join(root, filename)
                    relative = relative_path(source_path, final_dir)
                    files.append((source_path, relative))
                    uncompressed_bytes += os.path.getsize(source_path)
        if not files:
            raise RuntimeError("Core Runtime Module contains no files")

        manifest = write_pack(
            ((relative, source_path) for source_path, relative in files),
            archive_path,
        )
        manifest.update({
            "module": "core",
            "uncompressed_bytes": manifest["raw_bytes"],
            "compression": "lzma-or-store",
            "allowed_roots": ["Infernux", "numpy", "numpy.libs"],
        })
        with open(manifest_path, "w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")

        for payload_root in roots:
            shutil.rmtree(payload_root, ignore_errors=True)

        archive_bytes = os.path.getsize(archive_path)
        ratio = archive_bytes / max(1, uncompressed_bytes)
        Debug.log_internal(
            f"Packed core runtime data into {archive_bytes / (1024 * 1024):.1f} MB "
            f"({ratio:.1%} of {uncompressed_bytes / (1024 * 1024):.1f} MB)"
        )

    def _pack_parallel_runtime_archive(self, final_dir: str) -> None:
        """Convert the optional legacy module cache into an InxPack."""
        module_root = os.path.join(final_dir, "RuntimeModules", "parallel")
        legacy_archive = os.path.join(module_root, "parallel-module.zip")
        if not os.path.isfile(legacy_archive):
            return

        # The legacy cache is read only during build conversion.  It is never
        # copied to the final Player directory or loaded by PlayerBootstrap.
        import zipfile

        temporary_root = os.path.join(
            final_dir,
            self._BUILD_TEMP_DIR_NAME,
            "parallel-module-conversion",
        )
        shutil.rmtree(temporary_root, ignore_errors=True)
        os.makedirs(temporary_root, exist_ok=True)
        try:
            with zipfile.ZipFile(legacy_archive, "r") as archive:
                archive.extractall(temporary_root)
            files: list[tuple[str, str]] = []
            for root, _dirs, filenames in os.walk(temporary_root):
                for filename in filenames:
                    source = os.path.join(root, filename)
                    relative = relative_path(source, temporary_root)
                    files.append((source, relative))
            if not files:
                raise RuntimeError("Parallel Runtime Module contains no files")

            archive_path = os.path.join(module_root, "parallel-module.inxpack")
            manifest = write_pack(
                ((relative, source) for source, relative in files),
                archive_path,
            )
            manifest.update({
                "module": "parallel",
                "packages": ["llvmlite", "numba"],
                "uncompressed_bytes": manifest["raw_bytes"],
                "compression": "lzma-or-store",
            })
            with open(
                os.path.join(module_root, "parallel-module.json"),
                "w",
                encoding="utf-8",
            ) as manifest_file:
                json.dump(manifest, manifest_file, indent=2, sort_keys=True)
                manifest_file.write("\n")
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)
            try:
                os.remove(legacy_archive)
            except FileNotFoundError:
                pass

    def _pack_content_archive(self, final_dir: str) -> None:
        """Pack runtime content into one validated Player content archive."""
        data_root = os.path.join(final_dir, "Data")
        if not os.path.isdir(data_root):
            raise RuntimeError("Player Data directory is missing")

        archive_path = os.path.join(data_root, self._CONTENT_ARCHIVE_FILENAME)
        manifest_path = os.path.join(data_root, self._CONTENT_MANIFEST_FILENAME)
        retained = {
            "BuildManifest.json",
            "BuildPayload.json",
            self._CONTENT_ARCHIVE_FILENAME,
            self._CONTENT_MANIFEST_FILENAME,
        }
        files: list[tuple[str, str]] = []
        project_bytecode_count = 0
        project_metadata_count = 0
        plaintext_project_scripts: list[str] = []
        excluded_files: list[str] = []
        uncompressed_bytes = 0

        for root, dirs, filenames in os.walk(data_root):
            dirs[:] = [directory for directory in dirs if directory != "Logs"]
            for filename in filenames:
                path = os.path.join(root, filename)
                relative = relative_path(path, data_root)
                if "/" not in relative and relative in retained:
                    continue
                if relative in self._PLAYER_EXCLUDED_CONTENT_RELATIVE_PATHS:
                    excluded_files.append(path)
                    continue
                if self._is_particle_authoring_payload(relative):
                    excluded_files.append(path)
                    continue
                suffix = os.path.splitext(filename)[1].lower()
                if suffix == ".meta":
                    # Metadata is consumed during Editor-side GUID resolution;
                    # it is never a Player runtime payload.
                    excluded_files.append(path)
                    continue
                if relative.startswith("Assets/") and suffix == ".py":
                    plaintext_project_scripts.append(relative)
                elif relative.startswith("Assets/") and suffix == ".pyc":
                    project_bytecode_count += 1
                if suffix == ".meta":
                    project_metadata_count += 1
                files.append((path, relative))
                uncompressed_bytes += os.path.getsize(path)

        if plaintext_project_scripts:
            raise RuntimeError(
                "Packaged Player still contains plaintext project scripts: "
                + ", ".join(plaintext_project_scripts[:8])
            )
        if not files:
            raise RuntimeError("Player content archive would be empty")

        manifest = write_pack(
            ((relative, source_path) for source_path, relative in files),
            archive_path,
        )
        manifest.update({
            "uncompressed_bytes": manifest["raw_bytes"],
            "compression": "lzma-or-store",
            "project_bytecode_count": project_bytecode_count,
            "project_metadata_count": 0,
        })
        with open(manifest_path, "w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")

        for source_path, _relative in files:
            os.remove(source_path)
        for excluded_path in excluded_files:
            os.remove(excluded_path)
        for root, dirs, _files in os.walk(data_root, topdown=False):
            for directory in dirs:
                path = os.path.join(root, directory)
                if os.path.basename(path) == "Logs":
                    continue
                try:
                    os.rmdir(path)
                except OSError:
                    pass

        archive_bytes = os.path.getsize(archive_path)
        ratio = archive_bytes / max(1, uncompressed_bytes)
        Debug.log_internal(
            f"Packed {len(files)} content files into {archive_bytes / (1024 * 1024):.1f} MB "
            f"({ratio:.1%} of {uncompressed_bytes / (1024 * 1024):.1f} MB)"
        )

    def _write_payload_manifest(self, final_dir: str) -> None:
        """Audit release layout and describe what remains inspectable."""
        data_root = os.path.join(final_dir, "Data")
        assets_root = os.path.join(data_root, "Assets")
        user_source_files: list[str] = []
        user_bytecode_files: list[str] = []
        project_meta_files: list[str] = []
        third_party_source_files: list[str] = []
        forbidden_runtime_files: list[str] = []
        native_binary_count = 0
        native_binary_bytes = 0
        total_files = 0
        total_bytes = 0

        for root, _dirs, files in os.walk(final_dir):
            for filename in files:
                path = os.path.join(root, filename)
                relative = relative_path(path, final_dir)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                total_files += 1
                total_bytes += size
                suffix = os.path.splitext(filename)[1].lower()
                in_assets = root == assets_root or root.startswith(assets_root + os.sep)
                if suffix in {
                    ".bak",
                    ".exp",
                    ".lib",
                    ".meta",
                    ".pdb",
                    ".pyc",
                    ".pyi",
                    ".pyo",
                } and not (in_assets and suffix == ".pyc"):
                    forbidden_runtime_files.append(relative)
                if in_assets and suffix == ".py":
                    user_source_files.append(relative)
                elif in_assets and suffix == ".pyc":
                    user_bytecode_files.append(relative)
                if (root == data_root or root.startswith(data_root + os.sep)) and suffix == ".meta":
                    project_meta_files.append(relative)
                elif suffix == ".py":
                    third_party_source_files.append(relative)
                if suffix in {".exe", ".dll", ".pyd", ".so", ".dylib"}:
                    native_binary_count += 1
                    native_binary_bytes += size

        if user_source_files:
            raise RuntimeError(
                "Packaged Player still contains plaintext project scripts: "
                + ", ".join(user_source_files[:8])
            )
        if third_party_source_files:
            raise RuntimeError(
                "Packaged Player still contains plaintext runtime sources: "
                + ", ".join(third_party_source_files[:8])
            )
        if forbidden_runtime_files:
            raise RuntimeError(
                "Packaged Player still contains build-time artifacts: "
                + ", ".join(forbidden_runtime_files[:8])
            )
        runtime_pack = {}
        runtime_marker_path = os.path.join(final_dir, "_infernux_runtime_pack.json")
        try:
            with open(runtime_marker_path, "r", encoding="utf-8") as marker_file:
                runtime_pack = json.load(marker_file)
        except (OSError, json.JSONDecodeError):
            pass

        content_manifest = {}
        try:
            with open(
                os.path.join(data_root, self._CONTENT_MANIFEST_FILENAME),
                "r",
                encoding="utf-8",
            ) as content_file:
                content_manifest = json.load(content_file)
        except (OSError, json.JSONDecodeError):
            pass

        payload = {
            "layout": (
                "infernux-windows-player"
                if self._player_launcher_path()
                else "infernux-player-directory"
            ),
            "code_protection": {
                "engine": "nuitka-native",
                "project_scripts": "cpython-optimized-bytecode",
                "strong_encryption": False,
                "plaintext_project_script_count": 0,
                "project_bytecode_count": content_manifest.get(
                    "project_bytecode_count", len(user_bytecode_files)
                ),
                "project_metadata_count": content_manifest.get(
                    "project_metadata_count", len(project_meta_files)
                ),
                "third_party_python_source_count": 0,
            },
            "content": content_manifest,
            "runtime_pack_fingerprint": runtime_pack.get("fingerprint", ""),
            "files": {
                "count": total_files,
                "bytes": total_bytes,
                "native_binary_count": native_binary_count,
                "native_binary_bytes": native_binary_bytes,
            },
        }
        manifest_path = os.path.join(data_root, "BuildPayload.json")
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as manifest_file:
            json.dump(payload, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")

    # ------------------------------------------------------------------
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

        # Remove duplicate engine DLLs from Infernux/lib/ — they already
        # exist in the dist root (placed by Nuitka / _inject_native_libs)
        # and the root copy is what the OS DLL loader finds.  Keep only
        # .pyd files in Infernux/lib/ (needed for relative imports).
        lib_dir = os.path.join(final_dir, "Infernux", "lib")
        if os.path.isdir(lib_dir):
            for fname in os.listdir(lib_dir):
                if fname.lower().endswith(".dll"):
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

        # Ensure Data/Logs exists for runtime log output
        logs_dir = os.path.join(final_dir, "Data", "Logs")
        os.makedirs(logs_dir, exist_ok=True)

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
