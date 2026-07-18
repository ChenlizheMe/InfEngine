"""
Asset Manager — Python-side unified asset loading & caching.

Provides a singleton interface for loading assets by path or GUID,
with WeakRef-based caching to avoid duplicate loads.

Usage::

    from Infernux.core.assets import AssetManager

    # Load by path
    mat = AssetManager.load("Assets/Materials/gold.mat")

    # Load by GUID
    mat = AssetManager.load_by_guid("a1b2c3d4-e5f6-...")

    # Search
    mats = AssetManager.find_assets("*.mat")
"""

from __future__ import annotations

import fnmatch
import os
import time
import weakref
from typing import Any, Callable, Dict, List, Optional, Type

from Infernux.core.material import Material
from Infernux.core.texture import Texture
from Infernux.core.shader import Shader
from Infernux.core.audio_clip import AudioClip
from Infernux.core.asset_types import (
    IMAGE_EXTENSIONS, SHADER_EXTENSIONS, MATERIAL_EXTENSIONS, AUDIO_EXTENSIONS,
    ANIMCLIP_EXTENSIONS,
    ANIMCLIP3D_EXTENSIONS,
    ANIMFSM_EXTENSIONS,
    asset_category_from_extension,
)
from Infernux.core.animation_clip import AnimationClip
from Infernux.core.animation_clip3d import AnimationClip3D
from Infernux.core.anim_state_machine import AnimStateMachine
from Infernux.debug import Debug

# ── Constants ──
_META_SUPPRESSION_TIMEOUT: float = 2.0  # seconds
_DEFAULT_DEBOUNCE_SEC: float = 0.35  # seconds


class AssetManager:
    """Python-side asset loading & caching manager (singleton pattern).

    Integrates with the C++ AssetDatabase for GUID ↔ path resolution
    and caches loaded assets via weak references.
    """

    # Weak-ref cache: guid → weakref to loaded Python wrapper
    _cache: Dict[str, weakref.ref] = {}

    # Strong-ref cache for textures: guid → Texture
    # Textures are expensive to reload from disk, so keep them alive.
    _texture_cache: Dict[str, Any] = {}

    # Native GPU texture reloads are deferred to a post-draw safe point.
    # Applying import settings can happen inside ImGui callbacks while the
    # current scene still has pending destroy/update work; doing Vulkan cache
    # eviction there is fragile in large live scenes.
    _pending_gpu_texture_reloads: Dict[str, str] = {}

    # Reference to the C++ AssetDatabase (set during engine init)
    _asset_database = None

    # Reference to engine for resource pipeline
    _engine = None

    # Debounced save scheduler: key -> {deadline: float, save_fn: callable}
    _scheduled_saves: Dict[str, Dict[str, Any]] = {}

    # Category -> strategy callables
    _import_apply_handlers: Dict[str, Callable[[str, object], bool]] = {}
    _save_handlers: Dict[str, Callable[[object], object]] = {}
    _execution_strategies_initialized: bool = False

    # Pre-captured material JSON snapshots for async save.
    # Key = normalized file path, value = serialized JSON string.
    _material_save_snapshots: Dict[str, str] = {}

    # Cached reference to C++ AssetRegistry singleton
    _registry = None

    # Paths for which .meta-watcher notifications should be suppressed.
    # Maps normalized path → expiry time (monotonic).  The Apply flow already
    # handles the reload synchronously, so all watcher events that arrive
    # within the window are redundant (Windows may fire >1 event per write).
    _meta_write_suppression: Dict[str, float] = {}
    _watcher_echo_suppression: Dict[
        tuple[str, str, str],
        tuple[float, tuple[int, int] | None],
    ] = {}

    @classmethod
    def initialize(cls, engine) -> None:
        """Initialize the AssetManager with the engine.

        Called once during engine startup. Sets up the C++ AssetDatabase
        reference and AssetRegistry for unified asset management.
        """
        cls._engine = engine
        native = cls._native_engine()
        if native is not None and hasattr(native, "get_asset_database"):
            cls._asset_database = native.get_asset_database()
        # Cache the AssetRegistry singleton
        cls._registry = cls._resolve_registry()

    @classmethod
    def load(cls, path: str, asset_type: Optional[Type] = None) -> Optional[Any]:
        """Load an asset by file path.

        Supports: .mat (Material)
        More types will be added as wrappers are implemented.

        Args:
            path: File path to the asset (relative or absolute).
            asset_type: Optional type hint. If None, inferred from extension.

        Returns:
            The loaded asset wrapper, or None if loading failed.
        """
        # Try GUID-based cache first
        guid = cls._get_guid_from_path(path)
        if guid:
            cached = cls._get_cached(guid)
            if cached is not None:
                return cached

        # Infer type from extension if not specified
        ext = os.path.splitext(path)[1].lower()
        resolved_type = asset_type or cls._type_from_extension(ext)

        asset = cls._load_by_type(path, resolved_type)
        if asset is not None and guid:
            cls._put_cache(guid, asset)
        return asset

    @classmethod
    def load_by_guid(cls, guid: str, asset_type: Optional[Type] = None) -> Optional[Any]:
        """Load an asset by its GUID.

        Args:
            guid: The asset GUID string.
            asset_type: Optional type hint.

        Returns:
            The loaded asset wrapper, or None.
        """
        # Check cache
        cached = cls._get_cached(guid)
        if cached is not None:
            return cached

        # Resolve path from GUID
        path = cls._get_path_from_guid(guid)
        if not path:
            return None

        ext = os.path.splitext(path)[1].lower()
        resolved_type = asset_type or cls._type_from_extension(ext)

        asset = cls._load_by_type(path, resolved_type)
        if asset is not None:
            if hasattr(asset, "_guid"):
                try:
                    asset._guid = guid
                except (AttributeError, TypeError):
                    # Some asset wrappers expose _guid as a property without
                    # a setter; the underlying GUID lookup still works via the
                    # cache key, so a missing setter is benign here.
                    pass
            cls._put_cache(guid, asset)
        return asset

    @classmethod
    def find_assets(cls, pattern: str, asset_type: Optional[Type] = None) -> List[str]:
        """Search for asset paths matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g. "*.mat", "Assets/Textures/*.png").
            asset_type: If specified, filter by type.

        Returns:
            List of matching asset paths.
        """
        if not cls._asset_database:
            return []

        results = []
        try:
            guids = cls._asset_database.get_all_guids()
            for guid in guids:
                path = cls._asset_database.get_path_from_guid(guid)
                if path and fnmatch.fnmatch(os.path.basename(path), pattern):
                    if asset_type is not None:
                        ext = os.path.splitext(path)[1].lower()
                        if cls._type_from_extension(ext) != asset_type:
                            continue
                    results.append(path)
        except Exception as e:
            from Infernux.debug import Debug
            Debug.log_warning(f"find_assets error: {e}")
        return results

    @classmethod
    def invalidate(cls, guid: str) -> None:
        """Invalidate a cached asset (e.g. on file change).

        Args:
            guid: GUID of the asset to invalidate.
        """
        cls._cache.pop(guid, None)
        cls._texture_cache.pop(guid, None)

    @classmethod
    def invalidate_path(cls, path: str) -> None:
        """Invalidate a cached asset by path."""
        guid = cls._get_guid_from_path(path)
        if guid:
            cls.invalidate(guid)

    @classmethod
    def flush(cls) -> None:
        """Clear all cached assets."""
        cls._cache.clear()
        cls._texture_cache.clear()

    # ======================================================================
    # Unified execution APIs (Inspector-facing)
    # ======================================================================

    @classmethod
    def register_import_strategy(cls, asset_category: str, apply_fn: Callable[[str, object], bool]):
        """Register import-settings apply function for an asset category."""
        cls._import_apply_handlers[asset_category] = apply_fn

    @classmethod
    def register_save_strategy(cls, asset_category: str, save_fn: Callable[[object], object]):
        """Register save function for an editable asset category."""
        cls._save_handlers[asset_category] = save_fn

    @classmethod
    def _ensure_execution_strategies(cls):
        if cls._execution_strategies_initialized:
            return

        from Infernux.core.asset_types import write_texture_import_settings, write_audio_import_settings, write_mesh_import_settings

        cls.register_import_strategy("texture", write_texture_import_settings)
        cls.register_import_strategy("audio", write_audio_import_settings)
        cls.register_import_strategy("mesh", write_mesh_import_settings)
        cls.register_save_strategy("material", cls._save_material_resource)
        cls.register_save_strategy("animclip", cls._save_animclip_resource)
        cls.register_save_strategy("animclip3d", cls._save_animclip3d_resource)
        cls.register_save_strategy("animfsm", cls._save_animfsm_resource)
        cls.register_save_strategy("animtimeline", cls._save_animtimeline_resource)
        cls.register_save_strategy("timelinefsm", cls._save_animfsm_resource)

        cls._execution_strategies_initialized = True

    @classmethod
    def apply_import_settings(cls, asset_category: str, path: str, settings_obj) -> bool:
        """Apply import settings by category and trigger reimport in one unified step."""
        cls._ensure_execution_strategies()

        apply_fn = cls._import_apply_handlers.get(asset_category)
        if apply_fn is None:
            return False

        # Import-settings writes go through DocumentStore atomic replace of the
        # .meta sidecar; suppress only META_DELETED echoes for that path.
        cls._suppress_meta_watcher(path)

        ok = apply_fn(path, settings_obj)
        if not ok:
            cls._meta_write_suppression.pop(cls._normalize_asset_path(path), None)
            return False
        if cls.reimport_asset(path):
            return True
        cls._meta_write_suppression.pop(cls._normalize_asset_path(path), None)
        return False

    @classmethod
    def _mutation_database(cls, database=None):
        result = database if database is not None else cls._asset_database
        if result is None:
            raise RuntimeError("AssetManager requires an initialized AssetDatabase")
        return result

    @staticmethod
    def _mutation_failure(operation: str, path: str, error_code, error: str, *, guid: str = "", previous_path: str = ""):
        from Infernux.lib import AssetMutationResult

        result = AssetMutationResult()
        result.operation = operation
        result.path = path
        result.previous_path = previous_path
        result.guid = guid
        result.error_code = error_code
        result.error = error
        return result

    @classmethod
    def _suppress_meta_watcher(cls, path: str) -> None:
        """Ignore transient META_DELETED echoes from DocumentStore meta writes."""
        normalized = cls._normalize_asset_path(path)
        if normalized:
            cls._meta_write_suppression[normalized] = time.monotonic() + _META_SUPPRESSION_TIMEOUT

    @classmethod
    def import_asset(cls, path: str, *, database=None, suppress_watcher_echo: bool = True):
        """Import one new asset and publish its editor-visible creation."""
        asset_database = cls._mutation_database(database)
        # Meta sidecars are written through DocumentStore atomic replace, which
        # briefly deletes the previous .meta and must not trigger rebuild work.
        cls._suppress_meta_watcher(path)
        result = asset_database.import_asset(path)
        if not result:
            cls._meta_write_suppression.pop(cls._normalize_asset_path(path), None)
            return result
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("created", path)
        cls._invalidate_shader_authoring_cache(path)
        cls._invalidate_project_panel_cache()
        cls._prime_material_preview(path)
        cls._emit_editor_asset_changed(path, "created")
        return result

    @classmethod
    def reimport_asset(cls, path: str, *, database=None, suppress_watcher_echo: bool = True):
        """Reimport through AssetDatabase, then refresh any loaded runtime copy."""
        asset_database = cls._mutation_database(database)
        guid = asset_database.get_guid_from_path(path)
        if not guid:
            from Infernux.lib import AssetMutationErrorCode
            return cls._mutation_failure(
                "reimport", path, AssetMutationErrorCode.NOT_FOUND, "asset is not registered"
            )

        ext = os.path.splitext(path)[1].lower()
        previous_shader_id = ""
        if ext in SHADER_EXTENSIONS:
            metadata = asset_database.get_meta_by_guid(guid)
            if metadata is not None and metadata.has_key("shader_id"):
                previous_shader_id = metadata.get_string("shader_id")
        native = cls._native_engine()
        has_shader_runtime = bool(
            ext in SHADER_EXTENSIONS and native is not None and native.has_renderer
        )

        # Persist metadata before touching runtime state. Pre-reload used to run
        # first and could abort reimport (and meta rebuild) on transient IO races
        # while DocumentStore was still publishing the asset or its .meta sidecar.
        cls._suppress_meta_watcher(path)
        result = asset_database.reimport_asset(path)
        if not result:
            cls._meta_write_suppression.pop(cls._normalize_asset_path(path), None)
            return result

        if has_shader_runtime:
            error = native.reload_shader_runtime(path, previous_shader_id)
            if error:
                Debug.log_error(error)
                from Infernux.lib import AssetMutationErrorCode
                result.succeeded = False
                result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
                result.error = error
                return result
        else:
            registry = cls._get_registry()
            if registry and registry.is_loaded(guid) and not registry.reload_asset(guid):
                from Infernux.lib import AssetMutationErrorCode
                result.succeeded = False
                result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
                result.error = "loaded asset registry rejected reload"
                return result

        cls._invalidate_shader_authoring_cache(path)
        cls.invalidate(guid)
        if ext in IMAGE_EXTENSIONS:
            cls._invalidate_texture_ui_cache(path)
            cls._schedule_gpu_texture_reload(path)
        from Infernux.core.asset_types import MESH_EXTENSIONS
        if ext in MESH_EXTENSIONS:
            cls._reload_mesh_asset(path)
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("modified", path)
        cls._emit_editor_asset_changed(path, "modified")
        return result

    @classmethod
    def _emit_editor_asset_changed(cls, path: str, event_type: str = "modified") -> None:
        if not path:
            return
        try:
            from Infernux.engine.ui.event_bus import EditorEventBus, EditorEvent

            bus = EditorEventBus.instance()
            bus.emit(EditorEvent.ASSET_CHANGED, path, event_type)
        except Exception as exc:
            Debug.log_suppressed("AssetManager._emit_editor_asset_changed", exc)

    @staticmethod
    def _invalidate_shader_authoring_cache(path: str) -> None:
        """Publish shader catalog changes independently of runtime renderer state."""
        if os.path.splitext(path)[1].lower() not in SHADER_EXTENSIONS:
            return
        try:
            from Infernux.engine.ui import inspector_shader_utils

            inspector_shader_utils.bump_shader_property_generation()
        except ImportError:
            pass

    @classmethod
    def move_asset(
        cls,
        old_path: str,
        new_path: str,
        *,
        database=None,
        suppress_watcher_echo: bool = True,
    ):
        """Commit a GUID-stable move, then patch all loaded path-bearing state."""
        asset_database = cls._mutation_database(database)
        guid = asset_database.get_guid_from_path(old_path)
        result = asset_database.move_asset(old_path, new_path)
        if not result:
            return result

        registry = cls._get_registry()
        if registry:
            registry.update_loaded_asset_path(old_path, new_path)
        if guid:
            cls.invalidate(guid)
        if os.path.splitext(old_path)[1].lower() in IMAGE_EXTENSIONS:
            cls._invalidate_texture_ui_cache(old_path)
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("moved", old_path, new_path)
        cls._invalidate_shader_authoring_cache(old_path)
        if os.path.splitext(old_path)[1].lower() != os.path.splitext(new_path)[1].lower():
            cls._invalidate_shader_authoring_cache(new_path)
        cls._invalidate_project_panel_cache()
        cls._emit_editor_asset_changed(new_path, "moved")
        return result

    @classmethod
    def delete_asset(cls, path: str, *, database=None, suppress_watcher_echo: bool = True):
        """Evict loaded state before deleting the database record and metadata."""
        from Infernux.core.asset_types import MATERIAL_EXTENSIONS

        asset_database = cls._mutation_database(database)
        guid = asset_database.get_guid_from_path(path)
        registry = cls._get_registry()
        if registry and guid:
            registry.remove_asset(guid)
        if guid:
            cls.invalidate(guid)

        ext = os.path.splitext(path)[1].lower()
        if ext in MATERIAL_EXTENSIONS:
            if guid:
                cls._remove_material_pipeline(guid)
            else:
                cls._remove_material_pipeline_by_path(path)
        if ext in IMAGE_EXTENSIONS:
            cls._invalidate_texture_ui_cache(path)
            cls._clear_deleted_texture_from_active_ui(path)
            cls._schedule_gpu_texture_reload(path)

        result = asset_database.delete_asset(path)
        if not result:
            return result
        cls._clear_deleted_live_references(guid, path)
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("deleted", path)
        cls._invalidate_shader_authoring_cache(path)
        cls._invalidate_project_panel_cache()
        cls._emit_editor_asset_changed(path, "deleted")
        return result

    @classmethod
    def _clear_deleted_live_references(cls, guid: str, path: str) -> dict:
        from Infernux.engine.asset_reference_cleanup import clear_deleted_asset_references

        try:
            return clear_deleted_asset_references(guid, path)
        except Exception as exc:
            Debug.log_error(f"Failed to clear live references for deleted asset '{path}': {exc}")
            return {"references_cleared": 0, "components_changed": 0, "fields": []}

    @classmethod
    def schedule_save(cls, key: str, save_fn: Callable[[], object], debounce_sec: float = _DEFAULT_DEBOUNCE_SEC):
        """Schedule a debounced save callback for a resource key (usually file path)."""
        record = cls._scheduled_saves.get(key)
        if record is not None:
            record["save_fn"] = save_fn
            # Preserve an already-armed next-flush save so continuous edits
            # still commit once per frame instead of being postponed forever.
            if float(debounce_sec) > 0.0:
                record["deadline"] = time.perf_counter() + float(debounce_sec)
                record["wait_one_flush"] = False
            return

        wait_one_flush = float(debounce_sec) <= 0.0
        cls._scheduled_saves[key] = {
            "deadline": time.perf_counter() + max(0.0, float(debounce_sec)),
            "save_fn": save_fn,
            "wait_one_flush": wait_one_flush,
        }

    @classmethod
    def schedule_asset_save(cls, asset_category: str, key: str, resource_obj, debounce_sec: float = _DEFAULT_DEBOUNCE_SEC):
        """Schedule a debounced save by category strategy, without exposing save callback to caller."""
        if asset_category == "material" and key and "::submat:" in key:
            return
        # Fast path: if a record already exists for this key, just bump the
        # deadline.  This avoids creating a new lambda + dict lookup through
        # the strategy registry on every slider-drag frame.
        record = cls._scheduled_saves.get(key)
        if record is not None:
            if float(debounce_sec) > 0.0:
                record["deadline"] = time.perf_counter() + float(debounce_sec)
                record["wait_one_flush"] = False
            return

        cls._ensure_execution_strategies()

        save_handler = cls._save_handlers.get(asset_category)
        if save_handler is None:
            return

        cls.schedule_save(key, lambda: save_handler(resource_obj), debounce_sec=debounce_sec)

    @classmethod
    def set_material_save_snapshot(cls, file_path: str, json_str: str):
        """Pre-capture a material JSON snapshot for async save.

        Called by the inspector when the final material state is known
        (drag-end / structural change).  The debounced save handler
        uses this snapshot instead of calling native_mat.serialize()
        on the main thread.
        """
        if file_path and "::submat:" in file_path:
            return
        if file_path and json_str:
            cls._material_save_snapshots[os.path.normpath(file_path)] = json_str

    @classmethod
    def _save_material_resource(cls, resource_obj):
        """Save a material resource and invalidate editor preview caches."""
        file_path = getattr(resource_obj, "file_path", "") or ""

        # Use pre-captured snapshot if available (avoids main-thread serialize).
        norm_path = os.path.normpath(file_path) if file_path else ""
        snapshot = cls._material_save_snapshots.pop(norm_path, "")

        # Prefer C++ async save path when available to avoid main-thread stalls.
        native = cls._native_engine()
        if native and hasattr(native, "schedule_material_save_snapshot_task") and file_path:
            try:
                if not snapshot:
                    serialize = getattr(resource_obj, "serialize", None)
                    if callable(serialize):
                        snapshot = serialize() or ""
                if snapshot:
                    key = f"material-save|{file_path}"
                    ok = bool(native.schedule_material_save_snapshot_task(key, file_path, snapshot))
                    if ok:
                        cls.on_material_saved(file_path)
                        return True
            except Exception as exc:
                Debug.log_suppressed("AssetManager.schedule_material_save_async.native_path", exc)

        # Fallback for older native builds — run synchronous save on the
        # IO thread pool to avoid blocking the main/render thread.
        save = getattr(resource_obj, "save", None)
        if not callable(save):
            return False

        from Infernux.core.asset_types import _io_pool

        def _fallback_save():
            try:
                saved = bool(save())
                if saved and file_path:
                    cls.on_material_saved(file_path)
                return saved
            except Exception as exc:
                Debug.log_suppressed("AssetManager.schedule_material_save_async.fallback_save", exc)
                return False

        _io_pool.submit(_fallback_save)
        # Optimistically invalidate caches now; actual file write may
        # complete a few ms later, but mtime-based systems will reconverge.
        if file_path:
            cls.on_material_saved(file_path)
        return True

    @classmethod
    def on_material_saved(cls, path: str) -> None:
        """Invalidate caches that depend on a material asset's file contents."""
        if not path:
            return
        # Inspector already holds the live material. Suppress the DocumentStore
        # publish echo; match_any covers async writes whose mtime changes after
        # this call returns.
        cls._suppress_watcher_echo("modified", path, match_any=True)
        cls.invalidate_path(path)
        cls._invalidate_material_ui_cache(path)

    @classmethod
    def _save_animclip_resource(cls, resource_obj):
        """Save an AnimationClip resource."""
        save = getattr(resource_obj, "save", None)
        if not callable(save):
            return False
        return save()

    @classmethod
    def _save_animclip3d_resource(cls, resource_obj):
        """Save an AnimationClip3D resource."""
        save = getattr(resource_obj, "save", None)
        if not callable(save):
            return False
        return save()

    @classmethod
    def _save_animfsm_resource(cls, resource_obj):
        """Save an AnimStateMachine resource."""
        save = getattr(resource_obj, "save", None)
        if not callable(save):
            return False
        return save()

    @classmethod
    def _save_animtimeline_resource(cls, resource_obj):
        """Save an AnimationTimeline resource."""
        save = getattr(resource_obj, "save", None)
        if not callable(save):
            return False
        return save()

    @classmethod
    def flush_scheduled_saves(cls, key: Optional[str] = None):
        """Execute due scheduled saves. If key is given, only flush that key."""
        now = time.perf_counter()

        if key is not None:
            record = cls._scheduled_saves.get(key)
            if not record:
                return
            if bool(record.get("wait_one_flush", False)):
                record["wait_one_flush"] = False
                return
            if now < float(record.get("deadline", 0.0)):
                return
            try:
                save_fn = record.get("save_fn")
                if callable(save_fn):
                    save_fn()
            finally:
                cls._scheduled_saves.pop(key, None)
            return

        due_keys = []
        for k, v in cls._scheduled_saves.items():
            if bool(v.get("wait_one_flush", False)):
                v["wait_one_flush"] = False
                continue
            if now >= float(v.get("deadline", 0.0)):
                due_keys.append(k)
        for k in due_keys:
            record = cls._scheduled_saves.get(k)
            try:
                if record:
                    save_fn = record.get("save_fn")
                    if callable(save_fn):
                        save_fn()
            finally:
                cls._scheduled_saves.pop(k, None)

    # ==========================================================================
    # Internal helpers
    # ==========================================================================

    @classmethod
    def _native_engine(cls):
        """Return the underlying C++ engine handle (unwrap Python wrapper if needed)."""
        engine = cls._engine
        if engine is None:
            return None
        return getattr(engine, '_engine', engine)

    @classmethod
    def _resolve_registry(cls):
        """Resolve the C++ AssetRegistry singleton (lazy, cached)."""
        try:
            from Infernux.lib import AssetRegistry
            return AssetRegistry.instance()
        except (ImportError, RuntimeError, AttributeError) as exc:
            Debug.log_suppressed("AssetManager._resolve_registry", exc)
            return None

    @classmethod
    def _get_registry(cls):
        """Return the cached AssetRegistry, resolving lazily if needed."""
        if cls._registry is None:
            cls._registry = cls._resolve_registry()
        return cls._registry

    @classmethod
    def _get_guid_from_path(cls, path: str) -> Optional[str]:
        if not cls._asset_database:
            return None
        try:
            guid = cls._asset_database.get_guid_from_path(path)
            return guid if guid else None
        except Exception as e:
            from Infernux.debug import Debug
            Debug.log_warning(f"_get_guid_from_path failed for '{path}': {e}")
            return None

    @classmethod
    def _get_path_from_guid(cls, guid: str) -> Optional[str]:
        if not cls._asset_database:
            return None
        try:
            path = cls._asset_database.get_path_from_guid(guid)
            return path if path else None
        except Exception as e:
            from Infernux.debug import Debug
            Debug.log_warning(f"_get_path_from_guid failed for '{guid}': {e}")
            return None

    @classmethod
    def _get_cached(cls, guid: str) -> Optional[Any]:
        # Strong texture cache (never GC'd until explicit invalidation)
        tex = cls._texture_cache.get(guid)
        if tex is not None:
            return tex
        ref = cls._cache.get(guid)
        if ref is not None:
            obj = ref()
            if obj is not None:
                return obj
            # Dead reference — clean up
            del cls._cache[guid]
        return None

    @classmethod
    def _put_cache(cls, guid: str, asset) -> None:
        if isinstance(asset, Texture):
            cls._texture_cache[guid] = asset
        try:
            cls._cache[guid] = weakref.ref(asset)
        except TypeError:
            # Object doesn't support weakref (e.g. some pybind types) —
            # caching is best-effort and the asset will simply be reloaded
            # next time it is requested.
            pass

    @classmethod
    def _type_from_extension(cls, ext: str) -> Optional[Type]:
        """Map file extension to Python asset type."""
        ext = ext.lower()
        if ext in MATERIAL_EXTENSIONS:
            return Material
        if ext in IMAGE_EXTENSIONS:
            return Texture
        if ext in SHADER_EXTENSIONS:
            return Shader
        if ext in AUDIO_EXTENSIONS:
            return AudioClip
        if ext in ANIMCLIP_EXTENSIONS:
            return AnimationClip
        if ext in ANIMCLIP3D_EXTENSIONS:
            return AnimationClip3D
        if ext in ANIMFSM_EXTENSIONS:
            return AnimStateMachine
        if ext == ".effect":
            from Infernux.renderstack.render_effect import RenderEffect
            return RenderEffect
        return None

    @classmethod
    def _load_by_type(cls, path: str, asset_type: Optional[Type]) -> Optional[Any]:
        """Load an asset given its path and resolved type."""
        if asset_type is Material or (asset_type is None and path.endswith(".mat")):
            return Material.load(path)
        if asset_type is Texture:
            return Texture.load(path)
        # Shader is a static utility — return a ShaderAssetInfo descriptor instead
        if asset_type is Shader:
            from Infernux.core.asset_types import ShaderAssetInfo
            guid = cls._get_guid_from_path(path) or ""
            return ShaderAssetInfo.from_path(path, guid=guid)
        if asset_type is AudioClip:
            return AudioClip.load(path)
        if asset_type is AnimationClip:
            return AnimationClip.load(path)
        if asset_type is AnimationClip3D:
            return AnimationClip3D.load(path)
        if asset_type is AnimStateMachine:
            return AnimStateMachine.load(path)
        from Infernux.renderstack.render_effect import RenderEffect
        if asset_type is RenderEffect or (asset_type is None and path.endswith(".effect")):
            return RenderEffect.load(path)
        return None

    @classmethod
    def _schedule_gpu_texture_reload(cls, path: str) -> None:
        """Queue native GPU texture invalidation for the next post-draw tick."""
        key = cls._normalize_asset_path(path) or os.path.abspath(path)
        cls._pending_gpu_texture_reloads[key] = path

        guid = cls._get_guid_from_path(path)
        if guid:
            cls._texture_cache.pop(guid, None)
            cls._cache.pop(guid, None)

    @classmethod
    def flush_pending_gpu_texture_reloads(cls) -> None:
        """Run queued native GPU texture reloads between frames."""
        if not cls._pending_gpu_texture_reloads:
            return
        pending = list(cls._pending_gpu_texture_reloads.values())
        cls._pending_gpu_texture_reloads.clear()
        for path in pending:
            cls._reload_gpu_texture_now(path)

    @classmethod
    def _reload_gpu_texture_now(cls, path: str) -> None:
        """Invalidate the C++ GPU texture cache so materials re-resolve it.

        The runtime uses GUID-based cache keys, so we resolve path → GUID first.
        Falls back to path-based invalidation for textures not yet in AssetDatabase.
        """
        guid = cls._get_guid_from_path(path)
        native = cls._native_engine()
        if native is not None and hasattr(native, 'reload_texture'):
            # ReloadTexture(path) is the designated file-system → GUID boundary
            # adapter: it resolves (or registers) the GUID once, then the whole
            # renderer-side invalidation chain runs GUID-only.
            native.reload_texture(path)
        # Evict from the Python-side strong cache
        if guid:
            cls._texture_cache.pop(guid, None)
            cls._cache.pop(guid, None)

    @classmethod
    def is_meta_watcher_suppressed(cls, path: str) -> bool:
        """Return whether the current .meta watcher event should be ignored."""
        normalized = cls._normalize_asset_path(path)
        if not normalized:
            return False
        expiry = cls._meta_write_suppression.get(normalized)
        if expiry is None:
            return False
        if time.monotonic() < expiry:
            return True
        cls._meta_write_suppression.pop(normalized, None)
        return False

    @classmethod
    def _watcher_echo_key(cls, event_type: str, path: str, destination: str = ""):
        return (
            event_type,
            cls._normalize_asset_path(path),
            cls._normalize_asset_path(destination),
        )

    @classmethod
    def _suppress_watcher_echo(
        cls,
        event_type: str,
        path: str,
        destination: str = "",
        *,
        match_any: bool = False,
    ) -> None:
        key = cls._watcher_echo_key(event_type, path, destination)
        if match_any:
            fingerprint = None
        else:
            target = destination if event_type == "moved" else path
            try:
                stat = os.stat(target)
                fingerprint = (stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                fingerprint = False
        cls._watcher_echo_suppression[key] = (
            time.monotonic() + _META_SUPPRESSION_TIMEOUT,
            fingerprint,
        )

    @classmethod
    def is_watcher_echo_suppressed(cls, event_type: str, path: str, destination: str = "") -> bool:
        now = time.monotonic()
        for key, (expiry, _fingerprint) in tuple(cls._watcher_echo_suppression.items()):
            if expiry <= now:
                cls._watcher_echo_suppression.pop(key, None)
        key = cls._watcher_echo_key(event_type, path, destination)
        suppression = cls._watcher_echo_suppression.get(key)
        if suppression is None:
            return False
        _expiry, expected_fingerprint = suppression
        # match_any: time-window suppression for async DocumentStore publishes
        # whose final mtime is not known yet when the save is scheduled.
        if expected_fingerprint is None:
            return True
        target = destination if event_type == "moved" else path
        try:
            stat = os.stat(target)
            current_fingerprint = (stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            current_fingerprint = False
        if current_fingerprint == expected_fingerprint:
            return True
        cls._watcher_echo_suppression.pop(key, None)
        return False

    @classmethod
    def _reload_mesh_asset(cls, path: str) -> None:
        """Reload a mesh asset in AssetRegistry so updated import settings take effect."""
        guid = cls._get_guid_from_path(path)
        native = cls._native_engine()
        if native is not None and hasattr(native, 'reload_mesh'):
            native.reload_mesh(path)
        if guid:
            cls._cache.pop(guid, None)

    @classmethod
    def _invalidate_project_panel_cache(cls) -> None:
        """Refresh Project Panel listing (embedded materials/animations depend on .meta)."""
        try:
            from Infernux.engine.bootstrap import EditorBootstrap
            bs = EditorBootstrap.instance()
            pp = getattr(bs, "project_panel", None) if bs else None
            if pp is not None:
                pp.invalidate_dir_cache()
                native = cls._native_engine()
                if native is not None and hasattr(native, "request_full_speed_frame"):
                    native.request_full_speed_frame()
        except Exception as exc:
            from Infernux.debug import Debug
            Debug.log_suppressed("AssetManager._invalidate_project_panel_cache", exc)

    @classmethod
    def _prime_material_preview(cls, path: str, material_json: str = "") -> None:
        """Schedule the first material thumbnail as part of asset publication."""
        if os.path.splitext(path)[1].lower() != ".mat":
            return
        native = cls._native_engine()
        if native is None or not hasattr(native, "query_or_schedule_material_preview"):
            return
        try:
            normalized = os.path.normpath(path)
            live_document = str(material_json or "")
            stamp = 0 if live_document else int(os.stat(normalized).st_mtime_ns)
            resource_key = f"matedit|{normalized}" if live_document else f"mat|{normalized}"
            native.query_or_schedule_material_preview(
                resource_key, normalized, live_document, stamp,
            )
            if hasattr(native, "request_full_speed_frame"):
                native.request_full_speed_frame()
        except (OSError, RuntimeError) as exc:
            Debug.log_suppressed("AssetManager._prime_material_preview", exc)

    @staticmethod
    def _normalize_asset_path(path: str) -> str:
        if not path:
            return ""
        result = os.path.normpath(path).replace("\\", "/")
        if os.name == "nt":
            result = result.lower()
        return result

    @classmethod
    def _invalidate_texture_ui_cache(cls, path: str) -> None:
        """Invalidate editor-side UI texture previews for a texture asset path."""
        # Resolve GUID — the UI cache is keyed by GUID when possible
        guid = cls._get_guid_from_path(path)
        normalized = cls._normalize_asset_path(path)
        # Collect all identifiers to invalidate (GUID + path variants)
        identifiers = {path, normalized, normalized.replace("/", "\\")}
        if guid:
            identifiers.add(guid)

        try:
            from Infernux.ui import get_shared_cache
            cache = get_shared_cache()
            for ident in identifiers:
                if ident:
                    cache.invalidate(ident)
        except Exception as exc:
            Debug.log_suppressed("AssetManager._invalidate_texture_ui_cache.shared_cache", exc)

        native = cls._native_engine()

        if native is not None:
            for ident in identifiers:
                if not ident:
                    continue
                try:
                    native.invalidate_texture_preview_task(f"ui_img|{ident}")
                except Exception as exc:
                    Debug.log_suppressed(
                        "AssetManager._invalidate_texture_ui_cache.native_preview_task",
                        exc,
                    )

        try:
            from Infernux.engine.ui.asset_resource_preview import invalidate_resource_preview
            invalidate_resource_preview(path)
        except Exception as exc:
            Debug.log_suppressed("AssetManager._invalidate_texture_ui_cache.resource_preview", exc)

        try:
            from Infernux.engine.ui.window_manager import WindowManager
            wm = WindowManager.instance()
            if wm is not None:
                for panel in list(getattr(wm, "_window_instances", {}).values()):
                    invalidate = getattr(panel, "invalidate_texture_thumbnail", None)
                    if callable(invalidate):
                        invalidate(path)
        except Exception as exc:
            Debug.log_suppressed("AssetManager._invalidate_texture_ui_cache.panels", exc)

    @classmethod
    def _invalidate_material_ui_cache(cls, path: str) -> None:
        """Invalidate editor-side cached material thumbnails for a material path."""
        if not path:
            return

        # NOTE: We intentionally do NOT call invalidate_resource_preview() here.
        # The C++ preview system is stamp-driven: the Inspector updates its
        # cache_tag (and thus the stamp) 120 ms after editing settles, which
        # naturally re-schedules a render.  The ProjectPanel detects mtime
        # changes after each file save.  Forcing a C++ readyStamp reset on
        # every save was causing unnecessary GPU render-pass stalls during
        # continuous slider dragging.

        try:
            from Infernux.engine.ui.window_manager import WindowManager
            wm = WindowManager.instance()
            if wm is not None:
                for panel in list(getattr(wm, "_window_instances", {}).values()):
                    invalidate = getattr(panel, "invalidate_material_thumbnail", None)
                    if callable(invalidate):
                        invalidate(path)
        except Exception as exc:
            Debug.log_suppressed("AssetManager._invalidate_material_ui_cache.panels", exc)

    @classmethod
    def _remove_material_pipeline(cls, material_key: str) -> None:
        """Remove MaterialPipelineManager render data by material key."""
        native = cls._native_engine()
        if native is not None and hasattr(native, 'remove_material_pipeline') and material_key:
            native.remove_material_pipeline(material_key)

    @classmethod
    def _remove_material_pipeline_by_path(cls, path: str) -> None:
        """Remove MaterialPipelineManager render data for a material by file path.

        The pipeline manager keys by material name (stem of filename), so we
        derive the key from the path and call engine.remove_material_pipeline().
        """
        import os
        native = cls._native_engine()
        if native is None or not hasattr(native, 'remove_material_pipeline'):
            return
        mat_name = os.path.splitext(os.path.basename(path))[0]
        if mat_name:
            native.remove_material_pipeline(mat_name)

    @classmethod
    def _clear_deleted_texture_from_active_ui(cls, path: str) -> bool:
        """Clear stale texture_path fields from active UI Python components."""
        normalized = cls._normalize_asset_path(path)
        if not normalized:
            return False

        changed = False

        try:
            from Infernux.lib import SceneManager

            scene = SceneManager.instance().get_active_scene()
            if scene is None:
                return False

            for game_object in scene.get_all_objects():
                if game_object is None:
                    continue
                for py_comp in game_object.get_py_components():
                    tex_path = getattr(py_comp, "texture_path", None)
                    if not isinstance(tex_path, str) or not tex_path:
                        continue
                    if cls._normalize_asset_path(tex_path) != normalized:
                        continue
                    setattr(py_comp, "texture_path", "")
                    changed = True
        except Exception as exc:
            Debug.log_suppressed("AssetManager._clear_deleted_texture_from_active_ui.scan", exc)
            return False

        if changed:
            try:
                from Infernux.engine.scene_manager import SceneFileManager

                sfm = SceneFileManager.instance()
                if sfm is not None:
                    sfm.mark_dirty()
            except Exception as exc:
                Debug.log_suppressed(
                    "AssetManager._clear_deleted_texture_from_active_ui.mark_dirty",
                    exc,
                )

        return changed

    @classmethod
    def invalidate_project_panel_cache(cls) -> None:
        """Refresh Project Panel listing after meta/import changes (explicit call only)."""
        cls._invalidate_project_panel_cache()
