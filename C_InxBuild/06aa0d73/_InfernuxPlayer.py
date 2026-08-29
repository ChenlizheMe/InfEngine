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
    _ch if _ch not in '<>:"/\\|?*' else '_' for _ch in _GAME_NAME
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
        _name = str(_item.get("path", "")).replace("\\", "/")
        _parts = _name.split("/")
        if (
            not _name
            or "\x00" in _name
            or _name.startswith("/")
            or (_name.startswith("\\"))
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
                _parts = _line.rstrip("\r\n").split("\t")
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

_DEBUG_MODE = True
os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = "1" if _DEBUG_MODE else "0"

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
    _source_identity = _expected_hash + "\n" + str(_archive_stat.st_size)

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
            _stream.write(str(_message) + "\n")
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
            "Failed to start. Details in crash.log\n\n" + _traceback[-800:],
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
