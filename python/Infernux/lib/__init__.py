try:
    import ctypes
except ModuleNotFoundError:
    # CPython's Emscripten build intentionally has no dynamic-loader module.
    # All native Player bindings are statically registered by the browser host.
    ctypes = None
import glob
import importlib
import importlib.machinery
import importlib.util
import os
import sys
from functools import wraps


def _log_suppressed(exc: BaseException) -> None:
    """Best-effort log for early-init code (Debug may not be available yet)."""
    try:
        from Infernux.debug import Debug
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    except Exception:
        pass


lib_dir = os.path.join(os.path.dirname(__file__))
lib_dir = os.path.abspath(lib_dir)
native_dir = lib_dir

_dll_dir_handles = []


def _register_native_search_dir(path: str) -> None:
    if not path or not os.path.isdir(path):
        return

    norm = os.path.abspath(path)
    if norm not in sys.path:
        sys.path.insert(0, norm)

    if sys.platform == "win32":
        handle = os.add_dll_directory(norm)
        _dll_dir_handles.append(handle)
        path_entries = os.environ.get("PATH", "").split(";") if os.environ.get("PATH") else []
        if norm not in path_entries:
            os.environ["PATH"] = norm + (";" + os.environ["PATH"] if os.environ.get("PATH") else "")
    elif sys.platform == "darwin":
        dyld_path = os.environ.get("DYLD_LIBRARY_PATH", "")
        parts = dyld_path.split(":") if dyld_path else []
        if norm not in parts:
            os.environ["DYLD_LIBRARY_PATH"] = norm + ((":" + dyld_path) if dyld_path else "")
    else:
        ld_path = os.environ.get("LD_LIBRARY_PATH", "")
        parts = ld_path.split(":") if ld_path else []
        if norm not in parts:
            os.environ["LD_LIBRARY_PATH"] = norm + ((":" + ld_path) if ld_path else "")


def _register_native_module_override() -> str | None:
    global native_dir

    override = os.environ.get("INFERNUX_NATIVE_MODULE_DIR")
    if override is None:
        return None

    native_dir = os.path.abspath(override)
    if not os.path.isdir(native_dir):
        raise ImportError(f"INFERNUX_NATIVE_MODULE_DIR is not a directory: {native_dir}")
    if native_dir not in __path__:
        __path__.insert(0, native_dir)
    _register_native_search_dir(native_dir)
    return native_dir


def _native_module_candidate(directory: str) -> str:
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        candidate = os.path.join(directory, f"_Infernux{suffix}")
        if os.path.isfile(candidate):
            return candidate
    suffixes = ", ".join(importlib.machinery.EXTENSION_SUFFIXES)
    raise ImportError(
        f"No ABI-compatible _Infernux extension found under {directory}; "
        f"expected one of: {suffixes}"
    )


def _load_native_module_from_dir(directory: str):
    """Load the package-qualified native module from an explicit directory."""

    module_name = f"{__name__}._Infernux"
    module_path = _native_module_candidate(directory)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create an extension loader for {module_path}")

    previous = sys.modules.get(module_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    return module


def _load_native_module(override_dir: str | None):
    if override_dir is not None:
        return _load_native_module_from_dir(override_dir)
    return importlib.import_module(f"{__name__}._Infernux")


def _export_native_module(module) -> None:
    globals()["_Infernux"] = module
    export_names = getattr(module, "__all__", None)
    if export_names is None:
        export_names = [name for name in vars(module) if not name.startswith("_")]
    globals().update({name: getattr(module, name) for name in export_names})


def _iter_dev_native_search_dirs():
    repo_root = os.path.abspath(os.path.join(lib_dir, "..", "..", ".."))
    build_root = os.path.join(repo_root, "out", "build")
    configs = ("RelWithDebInfo", "Release", "Debug")

    # Preset build trees stage an ABI-complete runtime in python-sync. Prefer
    # the most recently built compatible directory so switching Python ABIs or
    # CMake presets does not require copying binaries into the source package.
    preset_candidates = glob.glob(os.path.join(build_root, "*", "python-sync"))
    for config in configs:
        preset_candidates.extend(
            glob.glob(os.path.join(build_root, "*", config))
        )

    def newest_compatible_module(directory: str) -> float:
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            candidate = os.path.join(directory, f"_Infernux{suffix}")
            if os.path.isfile(candidate):
                return os.path.getmtime(candidate)
        return -1.0

    seen = set()
    for candidate in sorted(
        preset_candidates, key=newest_compatible_module, reverse=True
    ):
        normalized = os.path.abspath(candidate)
        if normalized in seen or newest_compatible_module(normalized) < 0:
            continue
        seen.add(normalized)
        yield normalized

    # Retain the legacy single-config layout for older developer build trees.
    for config in configs:
        candidate = os.path.join(build_root, config)
        normalized = os.path.abspath(candidate)
        if normalized not in seen:
            seen.add(normalized)
            yield normalized

    externals = (
        ("external", "assimp", "bin"),
        ("external", "glslang", "glslang"),
        ("external", "JoltPhysics"),
        ("external", "SDL"),
    )
    for prefix in externals:
        for config in configs:
            yield os.path.join(build_root, *prefix, config)


_SYSTEM_DLL_CHECKS = (
    ("MSVCP140.dll", "Install or repair the Microsoft Visual C++ Redistributable."),
    ("VCRUNTIME140.dll", "Install or repair the Microsoft Visual C++ Redistributable."),
    ("VCRUNTIME140_1.dll", "Install or repair the Microsoft Visual C++ Redistributable."),
    ("vulkan-1.dll", "Install a current GPU driver or the Vulkan Runtime."),
)

_ENGINE_DLLS = (
    "SDL3.dll",
    "assimp-vc143-mt.dll",
    "Jolt.dll",
)


def _collect_windows_native_load_hints():
    if sys.platform != "win32":
        return []

    hints = []

    for dll_name, remedy in _SYSTEM_DLL_CHECKS:
        try:
            ctypes.WinDLL(dll_name)
        except OSError:
            hints.append(f"Missing system DLL: {dll_name}. {remedy}")

    if not glob.glob(os.path.join(native_dir, "_Infernux*.pyd")):
        hints.append(f"Missing _Infernux*.pyd under {native_dir}. Reinstall the Infernux wheel.")

    for dll_name in _ENGINE_DLLS:
        full = os.path.join(native_dir, dll_name)
        if not os.path.isfile(full):
            hints.append(f"Missing engine DLL: {dll_name}. Reinstall the Infernux wheel.")
        else:
            try:
                ctypes.WinDLL(full)
            except OSError as e:
                hint = (
                    f"Engine DLL present but failed to load: {dll_name} ({e}). "
                    f"A dependency of this DLL may be missing."
                )
                # WinError 1114 = "A dynamic link library initialization
                # routine failed". For engine DLLs the dominant real-world
                # cause is a CPU without AVX executing AVX instructions in a
                # static initializer (issue #47, Pentium/Celeron + UHD 610).
                # Builds from 0.1.7 on use an SSE4.2 baseline; older wheels
                # need an upgrade.
                if getattr(e, "winerror", None) == 1114:
                    hint += (
                        " (WinError 1114 on older wheels usually means this "
                        "CPU lacks AVX support — upgrade to Infernux >= 0.1.7, "
                        "which uses an SSE4.2 baseline.)"
                    )
                hints.append(hint)

    return hints


def _list_lib_dir_contents():
    try:
        entries = sorted(os.listdir(native_dir))
        dlls = [e for e in entries if e.lower().endswith((".dll", ".pyd", ".so", ".dylib"))]
        return dlls
    except OSError:
        return []


def _raise_native_import_error(exc):
    lines = [
        "Failed to load the Infernux native module.",
        f"Native directory: {native_dir}",
        f"Original error: {exc}",
    ]

    hints = _collect_windows_native_load_hints()
    if hints:
        lines.append("Diagnostic results:")
        lines.extend(f"  - {hint}" for hint in hints)
    elif sys.platform == "darwin":
        if not glob.glob(os.path.join(native_dir, "_Infernux*.so")):
            lines.append(f"Missing _Infernux*.so under {native_dir}. Build the native module first.")
        lines.append(
            "Likely causes: missing Vulkan SDK (MoltenVK), or the native module was not built for this architecture."
        )
    elif sys.platform == "win32":
        lines.append(
            "Likely causes: a missing Vulkan runtime or missing Microsoft Visual C++ runtime DLLs."
        )

    found = _list_lib_dir_contents()
    if found:
        lines.append(f"Native files found in lib directory ({len(found)}):")
        lines.extend(f"  {f}" for f in found)
    else:
        lines.append("WARNING: No native files found in lib directory!")

    raise ImportError("\n".join(lines)) from exc


def _preload_bundled_crt_dlls() -> None:
    """Pre-load MSVC CRT DLLs bundled alongside ``_Infernux.pyd``.

    On machines without a system-wide Visual C++ Redistributable install,
    ``os.add_dll_directory()`` alone is not always sufficient —
    ``_Infernux.pyd`` (and the engine DLLs it depends on) may still fail
    to resolve ``vcruntime140.dll`` / ``msvcp140.dll`` at load time.

    Explicitly loading them via ``ctypes.WinDLL`` before the native module
    guarantees they are resident in the process and the dynamic linker can
    satisfy the dependency.
    """
    if sys.platform != "win32":
        return

    # Order matters: vcruntime first, then msvcp / concrt (they depend
    # on vcruntime).
    _CRT_LOAD_ORDER = (
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll",
        "msvcp140_codecvt_ids.dll",
        "concrt140.dll",
    )

    for name in _CRT_LOAD_ORDER:
        full = os.path.join(native_dir, name)
        if os.path.isfile(full):
            try:
                ctypes.WinDLL(full)
            except OSError as _exc:
                _log_suppressed(_exc)
                pass  # Best-effort; the import below will give a clear error.


_native_override_dir = _register_native_module_override()
_register_native_search_dir(lib_dir)
_preload_bundled_crt_dlls()

try:
    _native_module = _load_native_module(_native_override_dir)
except (ModuleNotFoundError, ImportError, OSError) as _initial_native_error:
    if _native_override_dir is not None:
        _raise_native_import_error(_initial_native_error)
    _native_module = None
    _last_native_error = _initial_native_error
    for candidate in _iter_dev_native_search_dirs():
        _register_native_search_dir(candidate)
        native_dir = candidate
        _preload_bundled_crt_dlls()
        try:
            _native_module = _load_native_module_from_dir(candidate)
            break
        except (ModuleNotFoundError, ImportError, OSError) as exc:
            _last_native_error = exc
    if _native_module is None:
        _raise_native_import_error(_last_native_error)

_export_native_module(_native_module)
_SceneDocumentReadTicket = _native_module._SceneDocumentReadTicket
_preflight_scene_resource_dependencies = (
    _native_module._preflight_scene_resource_dependencies
)
_collect_scene_resource_dependencies = (
    _native_module._collect_scene_resource_dependencies
)
_schedule_scene_document_read = _native_module._schedule_scene_document_read
_pump_inline_jobs = _native_module._pump_inline_jobs

# `import *` skips underscore-prefixed names.  Re-export internal C++
# helpers so that `from Infernux import lib; lib._cds_register_class`
# works for the Python-side CDS bridge and batch API.
for _internal_name in (
    "_cds_register_class",
    "_cds_register_field",
    "_cds_schema_begin",
    "_cds_schema_prepare_class",
    "_cds_schema_prepare_field",
    "_cds_schema_has_class",
    "_cds_schema_find_class",
    "_cds_schema_get_field_id",
    "_cds_schema_discard_class",
    "_cds_schema_reserve",
    "_cds_schema_alloc",
    "_cds_schema_free",
    "_cds_schema_is_alive",
    "_cds_schema_get",
    "_cds_schema_set",
    "_cds_schema_migrate_slot",
    "_cds_schema_seal",
    "_cds_schema_final_class_id",
    "_cds_schema_commit",
    "_cds_schema_finalize",
    "_cds_schema_rollback",
    "_cds_schema_active",
    "_cds_alloc",
    "_cds_free",
    "_cds_is_alive",
    "_cds_reserve",
    "_cds_capacity",
    "_cds_alive_count",
    "_cds_get",
    "_cds_set",
    "_cds_batch_gather",
    "_cds_batch_scatter",
    "_transform_batch_read",
    "_transform_batch_write",
    "_create_scene_transform_batch_handle",
):
    if hasattr(_native_module, _internal_name):
        globals()[_internal_name] = getattr(_native_module, _internal_name)


_INVALID_NATIVE_LIFETIME_MARKERS = (
    "access violation",
    "rtti",
    "null pointer",
    "instance is null",
    "has been destroyed",
    "use after free",
)


def _is_native_lifetime_error(exc) -> bool:
    """Return True when *exc* looks like a stale native-object access."""
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc).strip().lower()
    return any(marker in message for marker in _INVALID_NATIVE_LIFETIME_MARKERS)


def _zero_vec3():
    return Vector3(0.0, 0.0, 0.0)


def _one_vec3():
    return Vector3(1.0, 1.0, 1.0)


def _identity_quat():
    return quatf(0.0, 0.0, 0.0, 1.0)


def _identity_matrix4x4():
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _native_safe_default(obj, name: str):
    """Return a conservative fallback for invalid native-object access."""
    if name in {"id", "component_id", "game_object_id", "child_count", "get_child_count"}:
        return 0
    if name in {"active", "enabled", "has_changed", "is_trigger", "is_active_in_hierarchy", "is_child_of"}:
        return False
    if name in {"name", "type_name"}:
        return ""
    if name in {"transform", "get_transform", "game_object", "get_parent", "parent", "root", "get_component",
                "get_cpp_component", "get_py_component", "get_child", "find", "collider"}:
        return None
    if name in {"get_components", "get_cpp_components", "get_py_components", "get_children"}:
        return []
    if name in {"serialize"}:
        return "{}"
    if name in {"deserialize", "remove_component", "remove_py_component"}:
        return False
    if name in {"position", "local_position", "euler_angles", "local_euler_angles", "forward", "up", "right",
                "local_forward", "local_up", "local_right", "contact_point", "contact_normal", "relative_velocity",
                "point", "normal"}:
        return _zero_vec3()
    if name in {"local_scale", "lossy_scale"}:
        return _one_vec3()
    if name in {"rotation", "local_rotation"}:
        return _identity_quat()
    if name in {"local_to_world_matrix", "world_to_local_matrix"}:
        return _identity_matrix4x4()
    if name == "distance":
        return 0.0

    if name.startswith("get_") and name.endswith("s"):
        return []
    if name.startswith("get_"):
        return None
    if name.startswith(("is_", "has_")):
        return False
    if name.startswith(("set_", "add_", "move_", "wake_", "sleep", "look_", "translate", "rotate", "detach_", "clear")):
        return None
    if name.startswith("remove_"):
        return False
    return None


def _wrap_native_callable(obj, name: str, func):
    @wraps(func)
    def _guarded(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RuntimeError as exc:
            if _is_native_lifetime_error(exc):
                return _native_safe_default(obj, name)
            raise

    setattr(_guarded, "_infernux_native_guarded", True)
    return _guarded


def _install_native_lifetime_guard(cls) -> None:
    """Patch a pybind class so stale native pointers fail safely in Python."""
    if getattr(cls, "_infernux_native_lifetime_guard_installed", False):
        return

    original_getattribute = cls.__getattribute__
    original_setattr = cls.__setattr__

    def _guarded_getattribute(self, name):
        try:
            value = original_getattribute(self, name)
        except RuntimeError as exc:
            if _is_native_lifetime_error(exc):
                return _native_safe_default(self, name)
            raise

        if name.startswith("__"):
            return value
        if callable(value) and not getattr(value, "_infernux_native_guarded", False):
            return _wrap_native_callable(self, name, value)
        return value

    def _guarded_setattr(self, name, value):
        try:
            return original_setattr(self, name, value)
        except RuntimeError as exc:
            if _is_native_lifetime_error(exc):
                return None
            raise

    def _guarded_bool(self):
        try:
            identifier = _guarded_getattribute(self, "id")
        except AttributeError:
            identifier = 0
        if identifier is None or identifier == 0:
            try:
                identifier = _guarded_getattribute(self, "component_id")
            except AttributeError:
                identifier = 0
        return bool(identifier)

    cls.__getattribute__ = _guarded_getattribute
    cls.__setattr__ = _guarded_setattr
    cls.__bool__ = _guarded_bool
    cls._infernux_native_lifetime_guard_installed = True


for _native_cls in (GameObject, Component, Transform, RaycastHit, CollisionInfo):
    _install_native_lifetime_guard(_native_cls)


class _Vec3WritebackProxy:
    """Write-through proxy so ``transform.position.x += dt`` actually persists.

    pybind returns Vector3 by value; mutating components of that copy is a silent
    no-op. This proxy commits component and in-place arithmetic back to the owner.
    """

    __slots__ = ("_owner", "_prop", "_value", "_setter")

    def __init__(self, owner, prop: str, value, setter):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_prop", prop)
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_setter", setter)

    def _commit(self) -> None:
        self._setter(self._owner, self._value)

    def _set_component(self, name: str, val) -> None:
        setattr(self._value, name, float(val))
        self._commit()

    @property
    def x(self):
        return self._value.x

    @x.setter
    def x(self, val):
        self._set_component("x", val)

    @property
    def y(self):
        return self._value.y

    @y.setter
    def y(self, val):
        self._set_component("y", val)

    @property
    def z(self):
        return self._value.z

    @z.setter
    def z(self, val):
        self._set_component("z", val)

    @property
    def r(self):
        return self._value.x

    @r.setter
    def r(self, val):
        self._set_component("x", val)

    @property
    def g(self):
        return self._value.y

    @g.setter
    def g(self, val):
        self._set_component("y", val)

    @property
    def b(self):
        return self._value.z

    @b.setter
    def b(self, val):
        self._set_component("z", val)

    def __getattr__(self, name):
        return getattr(self._value, name)

    def __setattr__(self, name, val):
        if name in _Vec3WritebackProxy.__slots__:
            object.__setattr__(self, name, val)
            return
        if name in {"x", "y", "z", "r", "g", "b"}:
            # Routed via properties when looked up on the class; keep for safety.
            object.__getattribute__(type(self), name).__set__(self, val)
            return
        raise AttributeError(f"_Vec3WritebackProxy has no attribute '{name}'")

    def __iadd__(self, other):
        object.__setattr__(self, "_value", self._value + other)
        self._commit()
        return self

    def __isub__(self, other):
        object.__setattr__(self, "_value", self._value - other)
        self._commit()
        return self

    def __imul__(self, other):
        object.__setattr__(self, "_value", self._value * other)
        self._commit()
        return self

    def __itruediv__(self, other):
        object.__setattr__(self, "_value", self._value / other)
        self._commit()
        return self

    def __add__(self, other):
        return self._value + other

    def __radd__(self, other):
        return other + self._value

    def __sub__(self, other):
        return self._value - other

    def __rsub__(self, other):
        return other - self._value

    def __mul__(self, other):
        return self._value * other

    def __rmul__(self, other):
        return other * self._value

    def __truediv__(self, other):
        return self._value / other

    def __rtruediv__(self, other):
        return other / self._value

    def __neg__(self):
        return -self._value

    def __eq__(self, other):
        return self._value == (other._value if isinstance(other, _Vec3WritebackProxy) else other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __bool__(self):
        return bool(self._value)

    def __len__(self):
        return 3

    def __getitem__(self, index):
        if index == 0:
            return self._value.x
        if index == 1:
            return self._value.y
        if index == 2:
            return self._value.z
        raise IndexError("Vector3 index out of range")

    def __setitem__(self, index, val):
        x, y, z = self._value.x, self._value.y, self._value.z
        if index == 0:
            x = float(val)
        elif index == 1:
            y = float(val)
        elif index == 2:
            z = float(val)
        else:
            raise IndexError("Vector3 index out of range")
        object.__setattr__(self, "_value", type(self._value)(x, y, z))
        self._commit()

    def __iter__(self):
        yield self._value.x
        yield self._value.y
        yield self._value.z

    def __repr__(self):
        return repr(self._value)

    def __str__(self):
        return str(self._value)

    def __copy__(self):
        return type(self._value)(self._value.x, self._value.y, self._value.z)

    def __deepcopy__(self, memo):
        return self.__copy__()


def _unwrap_vec3(value):
    if isinstance(value, _Vec3WritebackProxy):
        return value._value
    return value


def _install_transform_vec3_writeback() -> None:
    """Make Transform vec3 properties commit component / in-place edits."""
    for prop_name in (
        "position",
        "local_position",
        "euler_angles",
        "local_euler_angles",
        "local_scale",
    ):
        current = getattr(Transform, prop_name)
        orig_get = current.fget
        orig_set = current.fset
        if orig_get is None or orig_set is None:
            continue
        doc = getattr(current, "__doc__", None)

        def _make(name, getter, setter, documentation):
            def _get(self):
                return _Vec3WritebackProxy(self, name, getter(self), setter)

            def _set(self, value):
                setter(self, _unwrap_vec3(value))

            return property(_get, _set, doc=documentation)

        setattr(Transform, prop_name, _make(prop_name, orig_get, orig_set, doc))


_install_transform_vec3_writeback()


_native_game_object_add_component = GameObject.add_component
_native_game_object_remove_component = GameObject.remove_component
_native_game_object_can_remove_component = GameObject.can_remove_component
_native_game_object_get_remove_component_blockers = GameObject.get_remove_component_blockers
_native_game_object_get_py_component = GameObject.get_py_component
_native_game_object_get_component = GameObject.get_component
_native_game_object_get_components = GameObject.get_components
_native_game_object_get_component_in_children = GameObject.get_component_in_children
_native_game_object_get_component_in_parent = GameObject.get_component_in_parent
def _native_game_object_instantiate(
    original,
    parent=None,
    instantiate_in_world_space=False,
    configure_created=None,
):
    scene = original.scene
    if scene is None:
        raise RuntimeError("instantiate(): source GameObject is detached from a Scene")
    from Infernux.engine.component_restore import clone_game_object_transactionally

    return clone_game_object_transactionally(
        scene,
        original,
        parent,
        instantiate_in_world_space=instantiate_in_world_space,
        configure_created=configure_created,
    )


def _call_native_game_object(method_name: str, native_method, game_object, *args):
    try:
        return native_method(game_object, *args)
    except RuntimeError as exc:
        if _is_native_lifetime_error(exc):
            return _native_safe_default(game_object, method_name)
        raise


def _is_vector3_like(value) -> bool:
    return value is not None and hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z") and not hasattr(value, "w")


def _is_quat_like(value) -> bool:
    return value is not None and hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z") and hasattr(value, "w")


def _resolve_game_object_instantiate_source(original):
    if isinstance(original, GameObject):
        return "game_object", original

    try:
        from Infernux.components.ref_wrappers import GameObjectRef, PrefabRef
        if isinstance(original, PrefabRef):
            return "prefab", original
        if isinstance(original, GameObjectRef):
            return "game_object", original.resolve()
    except Exception as _exc:
        _log_suppressed(_exc)
        pass

    resolver = getattr(original, "resolve", None)
    if callable(resolver):
        try:
            resolved = resolver()
        except Exception:
            resolved = None
        if isinstance(resolved, GameObject):
            return "game_object", resolved

    return "game_object", None


def _coerce_parent_game_object(parent):
    if parent is None:
        return None
    if isinstance(parent, GameObject):
        return parent

    try:
        from Infernux.components.ref_wrappers import GameObjectRef
        if isinstance(parent, GameObjectRef):
            return parent.resolve()
    except Exception as _exc:
        _log_suppressed(_exc)
        pass

    game_object = getattr(parent, "game_object", None)
    if isinstance(game_object, GameObject):
        return game_object

    raise TypeError(
        "instantiate(): parent must be a GameObject, Transform, GameObjectRef, or None"
    )


def _instantiate_prefab_reference(
    prefab_ref,
    parent=None,
    instantiate_in_world_space=False,
    configure_created=None,
):
    current_path = getattr(prefab_ref, "path_hint", "")
    guid = getattr(prefab_ref, "guid", "")
    if not guid and not current_path:
        return None

    from Infernux.engine.prefab_manager import instantiate_prefab

    if guid:
        adb = None
        registry = AssetRegistry.instance()
        if registry:
            adb = registry.get_asset_database()
        result = instantiate_prefab(
            guid=guid,
            parent=parent,
            asset_database=adb,
            instantiate_in_world_space=instantiate_in_world_space,
            configure_created=configure_created,
        )
        if result is not None:
            return result

    if current_path and os.path.isfile(current_path):
        return instantiate_prefab(
            file_path=current_path,
            parent=parent,
            instantiate_in_world_space=instantiate_in_world_space,
            configure_created=configure_created,
        )

    return None


def _parse_instantiate_arguments(args, kwargs):
    if len(args) > 3:
        raise TypeError("instantiate(): expected at most 4 arguments including original")

    position = kwargs.pop("position", None)
    rotation = kwargs.pop("rotation", None)
    parent_was_keyword = "parent" in kwargs
    parent = kwargs.pop("parent", None)
    instantiate_in_world_space = kwargs.pop("instantiate_in_world_space", kwargs.pop("instantiateInWorldSpace", None))
    if kwargs:
        unexpected = ", ".join(sorted(kwargs.keys()))
        raise TypeError(f"instantiate(): unexpected keyword arguments: {unexpected}")

    if len(args) == 1:
        parent = args[0]
        if instantiate_in_world_space is None:
            instantiate_in_world_space = False
    elif len(args) == 2:
        if _is_vector3_like(args[0]) and _is_quat_like(args[1]):
            position, rotation = args
        else:
            parent = args[0]
            instantiate_in_world_space = args[1]
    elif len(args) == 3:
        position, rotation, parent = args
        if instantiate_in_world_space is None:
            instantiate_in_world_space = True

    if position is not None and not _is_vector3_like(position):
        raise TypeError("instantiate(): position must be a Vector3")
    if rotation is not None and not _is_quat_like(rotation):
        raise TypeError("instantiate(): rotation must be a quatf")
    if instantiate_in_world_space is None:
        instantiate_in_world_space = not (
            parent_was_keyword
            and position is None
            and rotation is None
        )
    if not isinstance(instantiate_in_world_space, bool):
        raise TypeError("instantiate(): instantiate_in_world_space must be a bool")

    return position, rotation, parent, instantiate_in_world_space


def _game_object_instantiate(original, *args, **kwargs):
    batch_positions = kwargs.pop("positions", None)
    if batch_positions is not None:
        if args:
            raise TypeError("instantiate(): batch positions cannot be combined with positional overloads")
        batch_rotations = kwargs.pop("rotations", None)
        batch_scales = kwargs.pop("scales", None)
        parent_arg = kwargs.pop("parent", None)
        instantiate_in_world_space = kwargs.pop(
            "instantiate_in_world_space",
            kwargs.pop("instantiateInWorldSpace", True),
        )
        return_objects = kwargs.pop("return_objects", True)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs.keys()))
            raise TypeError(f"instantiate(): unexpected keyword arguments: {unexpected}")
        if not isinstance(instantiate_in_world_space, bool):
            raise TypeError("instantiate(): instantiate_in_world_space must be a bool")
        if not isinstance(return_objects, bool):
            raise TypeError("instantiate(): return_objects must be a bool")

        import numpy as np

        positions = np.ascontiguousarray(batch_positions, dtype=np.float32)
        if positions.ndim != 2 or positions.shape[1] != 3:
            raise TypeError("instantiate(): positions must have shape (N, 3)")
        rotations = None
        if batch_rotations is not None:
            rotations = np.ascontiguousarray(batch_rotations, dtype=np.float32)
            if rotations.shape != (positions.shape[0], 4):
                raise TypeError("instantiate(): rotations must have shape (N, 4) in x, y, z, w order")
        scales = None
        if batch_scales is not None:
            scales = np.ascontiguousarray(batch_scales, dtype=np.float32)
            if scales.shape != (positions.shape[0], 3):
                raise TypeError("instantiate(): scales must have shape (N, 3)")

        parent = _coerce_parent_game_object(parent_arg) if parent_arg is not None else None
        source_kind, source = _resolve_game_object_instantiate_source(original)
        if source_kind == "game_object" and source is None:
            return []

        def _contains_python_components(root):
            stack = [root]
            while stack:
                current = stack.pop()
                if current.get_py_components():
                    return True
                stack.extend(current.get_children())
            return False

        # Pure-native hierarchies use one owner-thread transaction: capacity
        # reservation and renderer publication are coalesced, while every
        # returned item remains a normal independent GameObject.
        if source_kind == "game_object" and not _contains_python_components(source):
            result = source.scene._clone_game_objects(
                source,
                positions,
                rotations,
                scales,
                parent,
                instantiate_in_world_space,
                return_objects,
            )
            return list(result) if return_objects else int(result)

        # Python-backed components retain the existing transactional preflight
        # and publication contract. This path is intentionally correctness
        # first; native-only high-volume geometry takes the fast path above.
        result = []
        for index in range(positions.shape[0]):
            position = Vector3(*map(float, positions[index]))
            rotation = None if rotations is None else quatf(*map(float, rotations[index]))
            scalar_kwargs = {
                "position": position,
                "parent": parent,
                "instantiate_in_world_space": instantiate_in_world_space,
            }
            if rotation is not None:
                scalar_kwargs["rotation"] = rotation
            instance = _game_object_instantiate(original, **scalar_kwargs)
            if instance is None:
                continue
            if scales is not None:
                instance.transform.local_scale = Vector3(*map(float, scales[index]))
            result.append(instance)
        return result if return_objects else len(result)

    position, rotation, parent_arg, instantiate_in_world_space = _parse_instantiate_arguments(args, kwargs)
    parent = _coerce_parent_game_object(parent_arg) if parent_arg is not None else None

    def _configure_created(instance):
        if position is not None:
            instance.transform.position = position
        if rotation is not None:
            instance.transform.rotation = rotation

    source_kind, source = _resolve_game_object_instantiate_source(original)
    if source_kind == "prefab":
        instance = _instantiate_prefab_reference(
            source,
            parent,
            instantiate_in_world_space,
            _configure_created,
        )
    else:
        if source is None:
            return None
        instance = _call_native_game_object(
            "instantiate",
            _native_game_object_instantiate,
            source,
            parent,
            instantiate_in_world_space,
            _configure_created,
        )

    if instance is None:
        return None

    return instance


def _resolve_component_api_types():
    from Infernux.components.component import InxComponent
    from Infernux.components.builtin_component import BuiltinComponent
    from Infernux.components.registry import get_type

    return InxComponent, BuiltinComponent, get_type


def _resolve_builtin_wrapper(component_type):
    _, BuiltinComponent, _ = _resolve_component_api_types()

    if isinstance(component_type, type) and issubclass(component_type, BuiltinComponent):
        return component_type
    if isinstance(component_type, str):
        return BuiltinComponent._builtin_registry.get(component_type)

    cpp_type_name = getattr(component_type, "_cpp_type_name", "")
    if cpp_type_name:
        return BuiltinComponent._builtin_registry.get(cpp_type_name)

    type_name = getattr(component_type, "__name__", "")
    if type_name:
        return BuiltinComponent._builtin_registry.get(type_name)

    return None


def _resolve_python_component_class(component_type):
    InxComponent, BuiltinComponent, get_type = _resolve_component_api_types()

    if isinstance(component_type, type) and issubclass(component_type, InxComponent):
        if issubclass(component_type, BuiltinComponent):
            return None
        return component_type

    if isinstance(component_type, str):
        component_cls = get_type(component_type)
        if component_cls is not None and not issubclass(component_cls, BuiltinComponent):
            return component_cls

    return None


def _find_python_component_by_name(game_object, type_name: str):
    for component in game_object.get_py_components() or []:
        component_name = getattr(component.__class__, "__name__", "")
        if component_name == type_name or getattr(component, "type_name", "") == type_name:
            return component
    return None


def _find_python_components_by_name(game_object, type_name: str):
    return [
        component
        for component in (game_object.get_py_components() or [])
        if getattr(component.__class__, "__name__", "") == type_name
        or getattr(component, "type_name", "") == type_name
    ]


def _wrap_builtin_component(game_object, wrapper_cls, cpp_component):
    if cpp_component is None:
        return None
    return wrapper_cls._get_or_create_wrapper(cpp_component, game_object)


def _wrap_builtin_component_list(game_object, wrapper_cls, cpp_components):
    return [wrapper_cls._get_or_create_wrapper(component, game_object) for component in cpp_components]


def _resolve_public_component(component):
    if component is None:
        return None

    py_component_getter = getattr(component, "get_py_component", None)
    if callable(py_component_getter):
        try:
            return py_component_getter()
        except RuntimeError as exc:
            if _is_native_lifetime_error(exc):
                return None
            raise

    return component


def _unwrap_component_argument(game_object, component):
    native_getter = getattr(component, "_get_bound_native_component", None)
    if callable(native_getter):
        native_component = native_getter()
        if native_component is not None:
            return (native_component, False)

    for py_component in game_object.get_py_components() or []:
        if py_component is component:
            return (component, True)

    return (component, False)


def _game_object_add_component(self, component_type):
    python_component_cls = _resolve_python_component_class(component_type)
    if python_component_cls is not None:
        return self.add_py_component(python_component_cls())

    builtin_wrapper_cls = _resolve_builtin_wrapper(component_type)
    if builtin_wrapper_cls is not None:
        cpp_type_name = getattr(builtin_wrapper_cls, "_cpp_type_name", builtin_wrapper_cls.__name__)
        cpp_component = _call_native_game_object(
            "add_component", _native_game_object_add_component, self, cpp_type_name
        )
        return _wrap_builtin_component(self, builtin_wrapper_cls, cpp_component)

    return _call_native_game_object("add_component", _native_game_object_add_component, self, component_type)


def _game_object_get_component(self, component_type):
    builtin_wrapper_cls = _resolve_builtin_wrapper(component_type)
    if builtin_wrapper_cls is not None:
        cpp_type_name = getattr(builtin_wrapper_cls, "_cpp_type_name", builtin_wrapper_cls.__name__)
        cpp_component = self.get_cpp_component(cpp_type_name)
        return _wrap_builtin_component(self, builtin_wrapper_cls, cpp_component)

    if isinstance(component_type, str):
        python_component = _find_python_component_by_name(self, component_type)
        if python_component is not None:
            return python_component

    python_component_cls = _resolve_python_component_class(component_type)
    if python_component_cls is not None:
        python_component = self.get_py_component(python_component_cls)
        if python_component is not None:
            return python_component

    resolved_type_name = getattr(component_type, "_cpp_type_name", "") or getattr(component_type, "__name__", "")
    if resolved_type_name:
        return _call_native_game_object("get_component", _native_game_object_get_component, self, resolved_type_name)

    return _call_native_game_object("get_component", _native_game_object_get_component, self, component_type)


def _python_component_matches_type(component, component_type) -> bool:
    try:
        if isinstance(component, component_type):
            return True
    except TypeError:
        return False
    requested_identity = getattr(component_type, "_get_type_guid", None)
    actual_identity = getattr(type(component), "_get_type_guid", None)
    if not callable(requested_identity) or not callable(actual_identity):
        return False
    requested_guid = str(requested_identity() or "")
    return bool(requested_guid) and requested_guid == str(actual_identity() or "")


def _game_object_get_py_component(self, component_type):
    component = _native_game_object_get_py_component(self, component_type)
    if component is not None:
        return component
    for candidate in self.get_py_components() or ():
        if _python_component_matches_type(candidate, component_type):
            return candidate
    return None


def _game_object_get_components(self, component_type=None):
    if component_type is None:
        raw_components = _call_native_game_object("get_components", _native_game_object_get_components, self)
        public_components = []
        for component in raw_components or []:
            public_component = _resolve_public_component(component)
            if public_component is not None:
                public_components.append(public_component)
        return public_components

    builtin_wrapper_cls = _resolve_builtin_wrapper(component_type)
    if builtin_wrapper_cls is not None:
        cpp_type_name = getattr(builtin_wrapper_cls, "_cpp_type_name", builtin_wrapper_cls.__name__)
        cpp_components = self.get_cpp_components(cpp_type_name)
        return _wrap_builtin_component_list(self, builtin_wrapper_cls, cpp_components)

    if isinstance(component_type, str):
        python_components = _find_python_components_by_name(self, component_type)
        if python_components:
            return python_components

    python_component_cls = _resolve_python_component_class(component_type)
    if python_component_cls is not None:
        python_components = [
            component
            for component in (self.get_py_components() or [])
            if _python_component_matches_type(component, python_component_cls)
        ]
        if python_components:
            return python_components

    if isinstance(component_type, str):
        return self.get_cpp_components(component_type)

    type_name = getattr(component_type, "__name__", "")
    if type_name:
        return self.get_cpp_components(type_name)
    return []


def _game_object_get_component_in_children(self, component_type, include_inactive=False):
    result = _call_native_game_object(
        "get_component_in_children",
        _native_game_object_get_component_in_children,
        self,
        component_type,
        include_inactive,
    )
    builtin_wrapper_cls = _resolve_builtin_wrapper(component_type)
    if builtin_wrapper_cls is None and result is not None:
        return result
    python_component_cls = _resolve_python_component_class(component_type)
    if builtin_wrapper_cls is None and python_component_cls is not None:
        def find_python_component(current):
            if include_inactive or current.is_active_in_hierarchy():
                component = current.get_py_component(python_component_cls)
                if component is not None:
                    return component
            for child in current.get_children() or ():
                component = find_python_component(child)
                if component is not None:
                    return component
            return None

        return find_python_component(self)
    if builtin_wrapper_cls is None:
        return None
    result_game_object = getattr(result, "game_object", self)
    return _wrap_builtin_component(result_game_object, builtin_wrapper_cls, result)


def _game_object_get_component_in_parent(self, component_type, include_inactive=False):
    result = _call_native_game_object(
        "get_component_in_parent",
        _native_game_object_get_component_in_parent,
        self,
        component_type,
        include_inactive,
    )
    builtin_wrapper_cls = _resolve_builtin_wrapper(component_type)
    if builtin_wrapper_cls is None and result is not None:
        return result
    python_component_cls = _resolve_python_component_class(component_type)
    if builtin_wrapper_cls is None and python_component_cls is not None:
        current = self
        while current is not None:
            if include_inactive or current.is_active_in_hierarchy():
                component = current.get_py_component(python_component_cls)
                if component is not None:
                    return component
            current = current.get_parent()
        return None
    if builtin_wrapper_cls is None:
        return None
    result_game_object = getattr(result, "game_object", self)
    return _wrap_builtin_component(result_game_object, builtin_wrapper_cls, result)


def _game_object_remove_component(self, component):
    unwrapped_component, is_python_component = _unwrap_component_argument(self, component)
    if is_python_component:
        return self.remove_py_component(unwrapped_component)
    return _call_native_game_object("remove_component", _native_game_object_remove_component, self, unwrapped_component)


def _game_object_can_remove_component(self, component):
    unwrapped_component, is_python_component = _unwrap_component_argument(self, component)
    if is_python_component:
        return True
    return _call_native_game_object(
        "can_remove_component", _native_game_object_can_remove_component, self, unwrapped_component
    )


def _game_object_get_remove_component_blockers(self, component):
    unwrapped_component, is_python_component = _unwrap_component_argument(self, component)
    if is_python_component:
        return []
    return _call_native_game_object(
        "get_remove_component_blockers",
        _native_game_object_get_remove_component_blockers,
        self,
        unwrapped_component,
    )


GameObject.add_component = _game_object_add_component
GameObject.remove_component = _game_object_remove_component
GameObject.can_remove_component = _game_object_can_remove_component
GameObject.get_remove_component_blockers = _game_object_get_remove_component_blockers
GameObject.get_py_component = _game_object_get_py_component
GameObject.get_component = _game_object_get_component
GameObject.get_components = _game_object_get_components
GameObject.get_component_in_children = _game_object_get_component_in_children
GameObject.get_component_in_parent = _game_object_get_component_in_parent
GameObject.instantiate = staticmethod(_game_object_instantiate)


def _game_object_self_alias(self):
    object_id = int(getattr(self, "id", 0) or 0)
    if object_id <= 0:
        return None
    return self


GameObject.game_object = property(
    _game_object_self_alias,
    doc="Unity-style self alias so GameObject fields can be accessed via .game_object.",
)
