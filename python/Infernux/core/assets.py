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
import hashlib
import os
import threading
import time
import weakref
import uuid
from dataclasses import dataclass
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
    RENDER_EFFECT_EXTENSIONS,
    PARTICLE_GRAPH_EXTENSIONS,
    asset_category_from_extension,
)
from Infernux.core.animation_clip import AnimationClip
from Infernux.core.animation_clip3d import AnimationClip3D
from Infernux.core.anim_state_machine import AnimStateMachine
from Infernux.debug import Debug
from Infernux.engine.path_utils import path_key, portable_path, resolved_path

# ── Constants ──
_META_SUPPRESSION_TIMEOUT: float = 2.0  # seconds
_DEFAULT_DEBOUNCE_SEC: float = 0.35  # seconds
_RUNTIME_VOLUME_TEXTURE_EXTENSIONS = frozenset({".inxvfield", ".inxsdf"})


@dataclass(slots=True)
class _AssetRevisionState:
    """Monotonic persistence/preview state owned by the unified AssetManager."""

    edit_revision: int = 0
    requested_write_revision: int = 0
    persisted_revision: int = 0
    imported_disk_revision: int = 0
    preview_dependency_revision: int = 0
    expected_file_state: Any = None
    persisted_file_state: Any = None
    content_token: str = ""
    commit_chain_token: str = ""


@dataclass(slots=True)
class _PendingDocumentWrite:
    path: str
    ticket: Any
    edit_revision: int
    requested_write_revision: int
    commit_token: str
    snapshot: str
    content_token: str
    imported_disk_revision: int = 0
    expected_file_state: Any = None
    callback: Optional[Callable[[str], None]] = None


@dataclass(slots=True)
class _SelfWriteCommit:
    """The last exact publication that a watcher may acknowledge."""

    ticket: Any
    commit_token: str
    content_token: str
    file_state: Any = None


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
    _pending_gpu_texture_reloads_lock = threading.Lock()

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

    # JSON snapshots are captured on the main thread, while DocumentStore owns
    # coalescing and atomic IO on native worker threads.
    _render_effect_save_snapshots: Dict[str, str] = {}
    _document_save_expected_states: Dict[str, Any] = {}
    _document_write_metadata: Dict[str, Dict[str, Any]] = {}
    _asset_revision_states: Dict[str, _AssetRevisionState] = {}
    # Keep every ticket, including a ticket superseded by a newer queued
    # generation. This queue is the sole owner of pending document writes.
    _pending_document_write_records: Dict[str, list[_PendingDocumentWrite]] = {}
    _self_write_commits: Dict[str, list[_SelfWriteCommit]] = {}

    # Cached reference to C++ AssetRegistry singleton
    _registry = None

    # Paths for which .meta-watcher notifications should be suppressed.
    # Maps normalized path → expiry time (monotonic).  The Apply flow already
    # handles the reload synchronously, so all watcher events that arrive
    # within the window are redundant (Windows may fire >1 event per write).
    _meta_write_suppression: Dict[str, float] = {}
    _watcher_echo_suppression: Dict[
        tuple[str, str, str],
        tuple[int, int, int] | None,
    ] = {}

    @classmethod
    def initialize(cls, engine) -> None:
        """Initialize the AssetManager with the engine.

        Called once during engine startup. Sets up the C++ AssetDatabase
        reference and AssetRegistry for unified asset management.
        """
        native = getattr(engine, "_engine", engine)
        if native is None or not hasattr(native, "get_asset_database"):
            raise RuntimeError("AssetManager requires an initialized native engine")
        database = native.get_asset_database()
        if database is None:
            raise RuntimeError("AssetManager requires an initialized AssetDatabase")

        from Infernux.lib import AssetRegistry

        registry = AssetRegistry.instance()
        if registry is None:
            raise RuntimeError("AssetManager requires the native AssetRegistry")
        cls._engine = engine
        cls._asset_database = database
        cls._registry = registry

    @classmethod
    def require_asset_database(cls):
        """Return the process AssetDatabase or fail on invalid engine lifecycle."""
        if cls._asset_database is None:
            raise RuntimeError("AssetManager is not initialized with an AssetDatabase")
        return cls._asset_database

    @classmethod
    def release_engine(cls, engine=None) -> None:
        """Drop every project/native reference before its Engine is destroyed."""
        if engine is not None and cls._engine is not engine:
            return
        cls._cache.clear()
        cls._texture_cache.clear()
        with cls._pending_gpu_texture_reloads_lock:
            cls._pending_gpu_texture_reloads.clear()
        cls._scheduled_saves.clear()
        cls._material_save_snapshots.clear()
        cls._render_effect_save_snapshots.clear()
        cls._document_save_expected_states.clear()
        cls._document_write_metadata.clear()
        cls._asset_revision_states.clear()
        cls._pending_document_write_records.clear()
        cls._self_write_commits.clear()
        cls._meta_write_suppression.clear()
        cls._watcher_echo_suppression.clear()
        cls._asset_database = None
        cls._registry = None
        cls._engine = None

    @classmethod
    def refresh_pending(cls) -> bool:
        """Return whether the native catalog is scanning or committing.

        Published query snapshots remain readable during refresh, but asset
        mutation APIs are intentionally unavailable until the atomic commit
        completes. Runtime product builders use this as a readiness gate.
        """
        database = cls._asset_database
        if database is None:
            return False
        try:
            return bool(database.refresh_pending)
        except (AttributeError, RuntimeError):
            return False

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
        cls.register_save_strategy("render_effect", cls._save_render_effect_resource)
        cls.register_save_strategy("physic_material", lambda resource: resource.save() is not False)
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
    def _submit_internal_script_change(
        cls,
        path: str,
        *,
        catalog_event: str | None,
    ) -> None:
        """Feed successful internal Python mutations into the shared collector.

        Asset mutations suppress their matching watchdog echo, so ordinary
        Python files need an explicit ingress here.  ParticleScript source is
        owned by the particle compiler and is intentionally excluded.
        """
        lower = str(path or "").lower()
        if not lower.endswith(".py") or lower.endswith(".particle.py"):
            return
        # A lost script-change event silently breaks component/script refresh;
        # failures here must surface instead of being suppressed.
        from Infernux.engine.interaction import current_action_origin
        from Infernux.engine.resources_manager import ResourcesManager

        action_origin = current_action_origin()
        origin_value = getattr(action_origin, "value", str(action_origin))
        collector_origin = {
            "automation": "automation",
            "external": "watchdog",
        }.get(origin_value, "editor")
        manager = ResourcesManager.instance()
        if manager is not None:
            manager.submit_script_change(
                path,
                origin=collector_origin,
                catalog_event=catalog_event,
                change_kind=catalog_event or "modified",
            )

    @classmethod
    def import_asset(cls, path: str, *, database=None, suppress_watcher_echo: bool = True):
        """Import one new asset and publish its editor-visible creation."""
        profile_script = str(path or "").lower().endswith(".py")
        profile_started = time.perf_counter()
        profile_marks: list[tuple[str, float]] = []

        def mark(label: str) -> None:
            if profile_script:
                profile_marks.append((label, time.perf_counter()))

        asset_database = cls._mutation_database(database)
        # Meta sidecars are written through DocumentStore atomic replace, which
        # briefly deletes the previous .meta and must not trigger rebuild work.
        cls._suppress_meta_watcher(path)
        result = asset_database.import_asset(path)
        mark("database")
        if not result:
            cls._meta_write_suppression.pop(cls._normalize_asset_path(path), None)
            return result
        effect_error = cls._compile_render_effect_runtime(path, result.guid)
        if effect_error:
            from Infernux.lib import AssetMutationErrorCode

            result.succeeded = False
            result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
            result.error = effect_error
            return result
        particle_error = cls._compile_particle_runtime(
            path,
            result.guid,
            database=asset_database,
        )
        if particle_error:
            from Infernux.lib import AssetMutationErrorCode

            result.succeeded = False
            result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
            result.error = particle_error
            return result
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("created", path)
        cls._invalidate_shader_authoring_cache(path)
        cls._invalidate_project_panel_cache()
        cls._prime_material_preview(path)
        mark("cache_invalidation")
        cls._publish_asset_content_change(path, "created", guid=result.guid)
        mark("interaction_publish")
        if suppress_watcher_echo:
            cls._submit_internal_script_change(path, catalog_event="created")
        mark("collector_submit")
        if profile_script:
            elapsed_ms = (time.perf_counter() - profile_started) * 1000.0
            if elapsed_ms >= 10.0:
                previous = profile_started
                pieces = []
                for label, current in profile_marks:
                    pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
                    previous = current
                Debug.log_internal(
                    f"[ScriptAssetProfile] import={elapsed_ms:.2f}ms "
                    f"file={os.path.basename(path)} " + " ".join(pieces)
                )
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

        is_ordinary_script = ext == ".py" and not str(path).lower().endswith(".particle.py")
        if is_ordinary_script:
            # Ordinary scripts have their own validated collector and atomic
            # class-publication transaction. Broadcasting the same edit to
            # every generic asset/preview listener first made source saves pay
            # for two independent invalidation graphs on the main thread.
            # Their exact bytes and SHA-256 revision are already captured by
            # ScriptChangeCollector; the asset revision ledger exists for
            # persisted document/preview products and would hash the same
            # Python source a second time.
            cls.invalidate(guid)
            if suppress_watcher_echo:
                cls._suppress_watcher_echo("modified", path)
                cls._submit_internal_script_change(path, catalog_event="modified")
            return result

        effect_error = cls._compile_render_effect_runtime(path, guid)
        if effect_error:
            from Infernux.lib import AssetMutationErrorCode

            result.succeeded = False
            result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
            result.error = effect_error
            return result
        particle_error = cls._compile_particle_runtime(
            path,
            guid,
            database=asset_database,
        )
        if particle_error:
            from Infernux.lib import AssetMutationErrorCode

            result.succeeded = False
            result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
            result.error = particle_error
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
        elif ext == ".py" and not str(path).lower().endswith(".particle.py"):
            # Ordinary project scripts are published by the unified script
            # collector below.  AssetManager owns the catalog/cache mutation,
            # but must never execute a script or reload the registry before
            # the collector has produced a validated revision.
            pass
        elif (
            ext not in IMAGE_EXTENSIONS or ext in _RUNTIME_VOLUME_TEXTURE_EXTENSIONS
        ) and not cls._is_compiled_authoring_source(path):
            # Textures have one authoritative runtime publication path below:
            # native reload_texture(). Runtime vector fields/SDFs are the
            # exception: their immutable volume generation is owned by the
            # loaded registry object and must advance in place on reimport.
            registry = cls._get_registry()
            if registry and registry.is_loaded(guid) and not registry.reload_asset(guid):
                from Infernux.lib import AssetMutationErrorCode
                result.succeeded = False
                result.error_code = AssetMutationErrorCode.RUNTIME_APPLY_FAILED
                result.error = "loaded asset registry rejected reload"
                return result

        cls._invalidate_shader_authoring_cache(path)
        if os.path.splitext(path)[1].lower() not in RENDER_EFFECT_EXTENSIONS:
            cls.invalidate(guid)
        if ext in IMAGE_EXTENSIONS:
            cls._invalidate_texture_ui_cache(path)
            cls._schedule_gpu_texture_reload(path)
        from Infernux.core.asset_types import MESH_EXTENSIONS
        if ext in MESH_EXTENSIONS:
            cls._reload_mesh_asset(path)
        cls.note_imported_disk_change(path)
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("modified", path)
        cls._publish_asset_content_change(path, "modified", guid=guid)
        if suppress_watcher_echo:
            cls._submit_internal_script_change(path, catalog_event="modified")
        return result

    @classmethod
    def _compile_render_effect_runtime(cls, path: str, guid: str) -> str:
        """Compile and publish an effect artifact before notifying live users."""
        if os.path.splitext(path)[1].lower() not in RENDER_EFFECT_EXTENSIONS:
            return ""
        try:
            from Infernux.renderstack.render_effect import RenderEffect
            from Infernux.renderstack.render_effect_asset import RenderEffectAsset
            from Infernux.renderstack.render_effect_compiler import (
                RenderEffectArtifactRegistry,
            )

            artifact, document = RenderEffectArtifactRegistry.compile_and_publish(
                path,
                guid=guid,
            )
            loaded = cls._get_cached(guid)
            if isinstance(loaded, RenderEffect) and isinstance(document, RenderEffectAsset):
                loaded._publish_compiled_source(
                    document,
                    artifact_revision=artifact.revision,
                    file_path=path,
                    guid=guid,
                )
            return ""
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return f"render effect compile failed; keeping last-known-good: {exc}"

    @staticmethod
    def _is_particle_source(path: str) -> bool:
        lower = str(path or "").lower()
        return lower.endswith(".particlegraph") or lower.endswith(".particle.py")

    @classmethod
    def _is_compiled_authoring_source(cls, path: str) -> bool:
        return (
            os.path.splitext(path)[1].lower() in RENDER_EFFECT_EXTENSIONS
            or cls._is_particle_source(path)
        )

    @classmethod
    def _compile_particle_runtime(cls, path: str, guid: str, *, database=None) -> str:
        """Compile ParticleGraph/ParticleScript before publishing file changes."""
        if not cls._is_particle_source(path):
            return ""
        try:
            from Infernux.particle.artifact import ParticleArtifactRegistry

            runtime_artifact_path = ""
            if database is not None:
                from Infernux.lib import ResourceType

                runtime_artifact_path = str(
                    database.get_runtime_artifact_path(
                        guid,
                        ResourceType.ParticleGraph,
                    )
                    or ""
                )
            ParticleArtifactRegistry.compile_path(
                path,
                guid=guid,
                runtime_artifact_path=runtime_artifact_path,
            )
            return ""
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return f"particle compile failed; keeping last-known-good: {exc}"

    @classmethod
    def _publish_asset_content_change(
        cls,
        path: str,
        event_type: str = "modified",
        *,
        guid: str = "",
    ) -> None:
        """Publish one committed database consequence to Interaction Core."""
        if not path:
            return
        # Dropped mutation events desynchronize every listener (panels,
        # preload catalogs, undo); failures must surface.
        from Infernux.engine.interaction import (
            AssetMutationKind,
            AssetMutationService,
            current_action_origin,
        )

        service = AssetMutationService.instance()
        if service is not None:
            service.publish_content_change(
                path,
                AssetMutationKind(str(event_type).casefold()),
                guid=guid,
                origin=current_action_origin(),
            )

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
        origin="system",
        operation_id: str = "",
        publish_interaction: bool = True,
    ):
        """Commit one GUID-stable catalog move and update loaded runtime state.

        Project workspace operations preflight and publish a complete relocation
        batch themselves, so they disable the per-entry interaction publication.
        """
        asset_database = cls._mutation_database(database)
        guid = asset_database.get_guid_from_path(old_path)
        mutations = None
        plan = None
        if publish_interaction:
            from Infernux.engine.interaction import ActionOrigin, AssetMutationService

            mutations = AssetMutationService.instance()
            action_origin = ActionOrigin(origin)
            if mutations is not None and action_origin is not ActionOrigin.EXTERNAL:
                plan = mutations.prepare_relocation(
                    ((old_path, new_path, guid),),
                    origin=action_origin,
                    operation_id=operation_id,
                )
        result = asset_database.move_asset(old_path, new_path)
        if not result:
            if mutations is not None and plan is not None:
                mutations.abort_relocation(plan)
            return result
        cls._finalize_asset_move(
            old_path,
            new_path,
            guid=guid,
            suppress_watcher_echo=suppress_watcher_echo,
        )
        if mutations is not None:
            if plan is not None:
                mutations.commit_relocation(plan)
            elif publish_interaction:
                mutations.publish_move(
                    old_path,
                    new_path,
                    guid=guid,
                    origin=origin,
                    operation_id=operation_id,
                )
        return result

    @classmethod
    def _finalize_asset_move(
        cls,
        old_path: str,
        new_path: str,
        *,
        guid: str = "",
        suppress_watcher_echo: bool = True,
    ) -> None:
        """Apply loaded-runtime and editor-cache consequences of a catalog move."""
        registry = cls._get_registry()
        if registry and guid:
            registry.update_loaded_asset_path(guid, new_path)
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
        if suppress_watcher_echo and new_path.lower().endswith(".py"):
            from Infernux.engine.resources_manager import ResourcesManager

            resources = ResourcesManager.instance()
            if resources is not None:
                resources.reload_moved_script(old_path, new_path)

    @classmethod
    def move_assets_batch(
        cls,
        moves,
        *,
        database=None,
        suppress_watcher_echo: bool = True,
    ):
        """Commit one native catalog batch, then project runtime consequences."""
        asset_database = cls._mutation_database(database)
        pairs = tuple((str(old), str(new)) for old, new in moves)
        results = asset_database.move_assets_batch(pairs)
        if len(results) != len(pairs) or any(not result for result in results):
            return results
        for (old_path, new_path), result in zip(pairs, results):
            cls._finalize_asset_move(
                old_path,
                new_path,
                guid=str(result.guid or ""),
                suppress_watcher_echo=suppress_watcher_echo,
            )
        return results

    @classmethod
    def delete_asset(
        cls,
        path: str,
        *,
        database=None,
        suppress_watcher_echo: bool = True,
        guid_hint: str = "",
    ):
        """Evict loaded state while preserving serialized references by GUID.

        A deleted asset becomes a missing reference in live documents. Keeping
        that identity intact lets Undo or a later reimport reconnect it without
        silently rewriting user data.
        """
        from Infernux.core.asset_types import MATERIAL_EXTENSIONS

        profile_script = str(path or "").lower().endswith(".py")
        profile_started = time.perf_counter()
        profile_marks: list[tuple[str, float]] = []

        def mark(label: str) -> None:
            if profile_script:
                profile_marks.append((label, time.perf_counter()))

        asset_database = cls._mutation_database(database)
        guid = asset_database.get_guid_from_path(path) or str(guid_hint or "").strip()
        ext = os.path.splitext(path)[1].lower()

        # The catalog mutation owns dependency notification.  Do not evict
        # live payloads before it commits: a failed metadata/database delete
        # must leave the running editor exactly as it was.
        result = asset_database.delete_asset(path)
        mark("database")
        if not result:
            return result

        registry = cls._get_registry()
        if registry and guid:
            registry.remove_asset(guid)
        if guid:
            cls.invalidate(guid)

        if ext in MATERIAL_EXTENSIONS:
            if guid:
                cls._remove_material_pipeline(guid)
            else:
                cls._remove_material_pipeline_by_path(path)
        if ext in IMAGE_EXTENSIONS:
            cls._invalidate_texture_ui_cache(path)
            cls._schedule_gpu_texture_reload(path)

        if ext == ".py" and guid:
            from Infernux.engine.play_mode import PlayModeManager

            play_mode = PlayModeManager.instance()
            if play_mode is not None:
                play_mode.mark_components_missing_for_script(guid, path)
        mark("runtime_detach")
        if suppress_watcher_echo:
            cls._suppress_watcher_echo("deleted", path)
        cls._invalidate_shader_authoring_cache(path)
        cls._invalidate_project_panel_cache()
        cls._publish_asset_content_change(path, "deleted", guid=guid)
        mark("interaction_publish")
        if profile_script:
            elapsed_ms = (time.perf_counter() - profile_started) * 1000.0
            if elapsed_ms >= 10.0:
                previous = profile_started
                pieces = []
                for label, current in profile_marks:
                    pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
                    previous = current
                Debug.log_internal(
                    f"[ScriptAssetProfile] asset_delete={elapsed_ms:.2f}ms "
                    f"file={os.path.basename(path)} " + " ".join(pieces)
                )
        return result

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

        if asset_category == "material":
            # The document controller owns the durable target.  Python
            # Material wrappers intentionally expose data rather than relying
            # on a view/native object's incidental file_path attribute.
            cls.schedule_save(
                key,
                lambda: save_handler(resource_obj, key),
                debounce_sec=debounce_sec,
            )
        else:
            cls.schedule_save(
                key,
                lambda: save_handler(resource_obj),
                debounce_sec=debounce_sec,
            )

    @classmethod
    def cancel_scheduled_save(cls, key: str) -> bool:
        """Cancel a debounced write that has not reached its persistence backend."""
        removed = cls._scheduled_saves.pop(key, None) is not None
        normalized = path_key(key) if key else ""
        if normalized:
            cls._material_save_snapshots.pop(normalized, None)
            cls._render_effect_save_snapshots.pop(normalized, None)
            cls._document_save_expected_states.pop(normalized, None)
            cls._document_write_metadata.pop(normalized, None)
        return removed

    @classmethod
    def _asset_revision_state(cls, file_path: str) -> _AssetRevisionState:
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            raise ValueError("asset revision state requires a file path")
        return cls._asset_revision_states.setdefault(normalized, _AssetRevisionState())

    @classmethod
    def _ensure_commit_chain_token(
        cls,
        file_path: str,
        *,
        document_id: str = "",
    ) -> str:
        state = cls._asset_revision_state(file_path)
        if not state.commit_chain_token:
            state.commit_chain_token = (
                f"document:{document_id}"
                if document_id
                else f"asset:{uuid.uuid4().hex}"
            )
        metadata = cls._document_write_metadata.setdefault(path_key(file_path), {})
        metadata["commit_chain_token"] = state.commit_chain_token
        return state.commit_chain_token

    @staticmethod
    def _content_token(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _file_state_signature(state: Any):
        """Return the CAS identity without using modified time as ordering."""
        if state is None:
            return None
        if isinstance(state, dict):
            exists = state.get("exists")
            size = state.get("size", 0)
            content_hash = state.get("content_hash", state.get("contentHash", 0))
        elif isinstance(state, (tuple, list)) and len(state) >= 3:
            exists, size, content_hash = state[0], state[1], state[2]
        else:
            exists = getattr(state, "exists", None)
            size = getattr(state, "size", 0)
            content_hash = getattr(state, "content_hash", 0)
        if exists is None:
            return None
        try:
            return bool(exists), int(size or 0), int(content_hash or 0)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _capture_file_fingerprint(cls, file_path: str):
        """Capture the same content identity used by native DocumentStore."""
        try:
            from Infernux.core.document_store import capture_document_file_state

            signature = cls._file_state_signature(
                capture_document_file_state(file_path)
            )
            if signature is not None:
                return signature
        except (ImportError, OSError, RuntimeError, AttributeError, TypeError, ValueError):
            pass

        target = str(file_path or "")
        if not os.path.isfile(target):
            return False, 0, 0
        digest = 1469598103934665603
        size = 0
        try:
            with open(target, "rb") as stream:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    for value in chunk:
                        digest ^= value
                        digest = (digest * 1099511628211) & ((1 << 64) - 1)
        except OSError:
            return None
        return True, size, digest

    @classmethod
    def _file_state_matches(cls, file_path: str, expected_state: Any) -> bool:
        expected = cls._file_state_signature(expected_state)
        return expected is not None and expected == cls._capture_file_fingerprint(file_path)

    @classmethod
    def _register_self_write_commit(cls, record: _PendingDocumentWrite) -> None:
        committed_state = getattr(record.ticket, "committed_file_state", None)
        if committed_state is None:
            return
        entries = cls._self_write_commits.setdefault(record.path, [])
        if any(entry.commit_token == record.commit_token for entry in entries):
            return
        entries.append(
            _SelfWriteCommit(
                ticket=record.ticket,
                commit_token=record.commit_token,
                content_token=record.content_token,
                file_state=committed_state,
            )
        )

    @classmethod
    def register_local_commit(
        cls,
        file_path: str,
        *,
        commit_token: str,
        content_token: str = "",
        file_state: Any = None,
        edit_revision: int = 0,
        document_id: str = "",
    ):
        """Publish a completed editor write to the watcher/CAS ledger.

        This is the synchronous counterpart of ``_submit_document_snapshot``.
        It is intentionally based on the exact committed file fingerprint, so
        the next watcher notification can acknowledge the editor's own write
        without a time-based suppression window.
        """
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            raise ValueError("local commit requires a file path")
        if not str(commit_token or "").strip():
            raise ValueError("local commit requires a commit token")
        if file_state is None:
            from Infernux.core.document_store import capture_document_file_state

            file_state = capture_document_file_state(file_path)
        state = cls._asset_revision_state(normalized)
        state.persisted_file_state = file_state
        state.expected_file_state = file_state
        if edit_revision:
            state.edit_revision = max(state.edit_revision, int(edit_revision))
            state.persisted_revision = max(state.persisted_revision, int(edit_revision))
        if content_token:
            state.content_token = str(content_token)
        if document_id:
            metadata = cls._document_write_metadata.setdefault(normalized, {})
            metadata["document_id"] = str(document_id)
        entries = cls._self_write_commits.setdefault(normalized, [])
        entries[:] = [
            entry
            for entry in entries
            if entry.file_state != file_state
        ]
        entries.append(
            _SelfWriteCommit(
                ticket=None,
                commit_token=str(commit_token),
                content_token=str(content_token or ""),
                file_state=file_state,
            )
        )
        return file_state

    @classmethod
    def has_pending_local_revision(cls, file_path: str) -> bool:
        """Return whether local authored content must win over a watcher event."""
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            return False
        state = cls._asset_revision_states.get(normalized)
        if state is not None and state.edit_revision > state.persisted_revision:
            return True
        if cls._material_save_snapshots.get(normalized) is not None:
            return True
        if cls._render_effect_save_snapshots.get(normalized) is not None:
            return True
        return any(
            not bool(getattr(record.ticket, "is_complete", False))
            for record in cls._pending_document_write_records.get(normalized, ())
        )

    @classmethod
    def local_write_event_state(cls, file_path: str) -> str:
        """Classify a modified watcher event before the importer touches memory."""
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            return "none"
        if cls._acknowledge_self_write(normalized, file_path):
            return "ack"
        records = cls._pending_document_write_records.get(normalized, ())
        for record in records:
            ticket = record.ticket
            if not bool(getattr(ticket, "is_complete", False)):
                return "pending"
            if str(getattr(ticket, "status", "") or "").lower() != "succeeded":
                continue
            committed = getattr(ticket, "committed_file_state", None)
            if committed is not None and cls._file_state_matches(file_path, committed):
                cls._register_self_write_commit(record)
                return "ack"

        if cls._material_save_snapshots.get(normalized) is not None:
            return "pending"
        if cls._render_effect_save_snapshots.get(normalized) is not None:
            return "pending"
        state = cls._asset_revision_states.get(normalized)
        if state is not None and state.edit_revision > state.persisted_revision:
            # A completed write with a mismatching fingerprint is an external
            # edit; only an unsent revision should defer the event.
            if not any(
                bool(getattr(record.ticket, "is_complete", False))
                and str(getattr(record.ticket, "status", "") or "").lower()
                == "succeeded"
                for record in records
            ):
                return "pending"
        return "none"

    @classmethod
    def _acknowledge_self_write(cls, normalized: str, file_path: str) -> bool:
        """Consume an exact self-write fingerprint, never by elapsed time."""
        entries = cls._self_write_commits.get(normalized, ())
        if not entries:
            return False
        for entry in entries:
            if cls._file_state_matches(file_path, entry.file_state):
                # Keep the exact committed fingerprint alive: Windows can
                # deliver several notifications for one atomic replace, and
                # every duplicate must be acknowledged without a time window.
                return True
        # The path no longer contains the committed bytes.  Let the event pass
        # through as a real external edit rather than swallowing it.
        cls._self_write_commits.pop(normalized, None)
        return False

    @classmethod
    def note_imported_disk_change(cls, file_path: str) -> None:
        """Advance the disk/dependency side of one asset's revision ledger."""
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            return
        state = cls._asset_revision_state(normalized)
        state.imported_disk_revision += 1
        state.preview_dependency_revision += 1
        try:
            from Infernux.core.document_store import capture_document_file_state

            state.persisted_file_state = capture_document_file_state(file_path)
            state.expected_file_state = state.persisted_file_state
        except (ImportError, OSError, RuntimeError, ValueError):
            state.persisted_file_state = None
            state.expected_file_state = None
        cls._self_write_commits.pop(normalized, None)

    @classmethod
    def note_asset_edit(
        cls,
        file_path: str,
        *,
        edit_revision: int = 0,
        document_id: str = "",
        content_token: str = "",
        expected_file_state: Any = None,
        material_json: str = "",
    ) -> _AssetRevisionState:
        """Publish one in-memory edit through the shared asset revision ledger.

        ``edit_revision`` may originate from DocumentRegistry, but the ledger
        keeps its own monotonic high-water mark so Undo/Redo or repeated UI
        callbacks can never make an older async job look newer.  Preview
        invalidation is part of the same publication, before disk IO begins.
        """
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            raise ValueError("asset edit requires a file path")
        state = cls._asset_revision_state(normalized)
        token = str(content_token or "")
        if not token and material_json:
            token = cls._content_token(material_json)
        metadata = cls._document_write_metadata.setdefault(normalized, {})
        if document_id:
            metadata["document_id"] = str(document_id)
        cls._ensure_commit_chain_token(normalized, document_id=document_id)
        if expected_file_state is not None:
            metadata["expected_file_state"] = expected_file_state
            if state.persisted_file_state is None:
                state.expected_file_state = expected_file_state
        if token and token == state.content_token:
            if edit_revision:
                state.edit_revision = max(state.edit_revision, int(edit_revision))
            return state

        state.edit_revision = max(
            state.edit_revision + 1,
            int(edit_revision or 0),
            1,
        )
        state.content_token = token
        state.preview_dependency_revision += 1
        metadata["edit_revision"] = state.edit_revision
        if token:
            metadata["content_token"] = token

        if material_json:
            # This is deliberately live-document input.  It keeps the preview
            # on the in-memory value while persistence is still asynchronous.
            cls._invalidate_material_ui_cache(file_path)
            cls._prime_material_preview(file_path, material_json)
        return state

    @classmethod
    def preview_dependency_signature(cls, file_path: str) -> int:
        """Return a deterministic stamp for a resource and its dependencies."""
        normalized = path_key(file_path) if file_path else ""
        if not normalized:
            return 0
        parts = [normalized]
        state = cls._asset_revision_states.get(normalized)
        if state is not None:
            parts.extend(
                (
                    str(state.imported_disk_revision),
                    str(state.preview_dependency_revision),
                    str(state.persisted_revision),
                )
            )
        try:
            guid = cls._get_guid_from_path(file_path) or ""
            from Infernux.lib import AssetDependencyGraph

            graph = AssetDependencyGraph.instance()
            dependencies = graph.get_dependencies(guid) if guid else set()
            database = cls._asset_database
            for dependency_guid in sorted(str(value) for value in dependencies):
                dependency_path = ""
                if database is not None:
                    dependency_path = str(database.get_path_from_guid(dependency_guid) or "")
                dependency_key = path_key(dependency_path) if dependency_path else dependency_guid
                dependency_state = cls._asset_revision_states.get(dependency_key)
                parts.append(
                    ":".join(
                        (
                            dependency_guid,
                            dependency_key,
                            str(
                                getattr(
                                    dependency_state,
                                    "preview_dependency_revision",
                                    0,
                                )
                            ),
                            str(getattr(dependency_state, "imported_disk_revision", 0)),
                            str(getattr(dependency_state, "persisted_revision", 0)),
                        )
                    )
                )
        except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
            # A project can be inspected before the dependency graph is
            # initialized.  The asset-local revision remains a valid stamp.
            pass
        digest = hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "little", signed=False)

    @classmethod
    def set_material_save_snapshot(
        cls,
        file_path: str,
        json_str: str,
        *,
        edit_revision: int = 0,
        document_id: str = "",
        expected_file_state: Any = None,
    ):
        """Pre-capture a material JSON snapshot for async save.

        Called by the inspector when the final material state is known
        (drag-end / structural change).  The debounced save handler
        uses this snapshot instead of calling native_mat.serialize()
        on the main thread.
        """
        if file_path and "::submat:" in file_path:
            return
        if file_path and json_str:
            normalized = path_key(file_path)
            cls._material_save_snapshots[normalized] = json_str
            cls.note_asset_edit(
                file_path,
                edit_revision=edit_revision,
                document_id=document_id,
                content_token=cls._content_token(json_str),
                expected_file_state=expected_file_state,
                material_json=json_str,
            )

    @classmethod
    def set_render_effect_save_snapshot(
        cls,
        file_path: str,
        json_str: str,
        *,
        edit_revision: int = 0,
        document_id: str = "",
        expected_file_state: Any = None,
    ) -> None:
        """Replace the pending immutable snapshot for one RenderEffect asset."""
        if file_path and json_str:
            cls._render_effect_save_snapshots[path_key(file_path)] = json_str
            cls.note_asset_edit(
                file_path,
                edit_revision=edit_revision,
                document_id=document_id,
                content_token=cls._content_token(json_str),
                expected_file_state=expected_file_state,
            )

    @classmethod
    def set_document_save_expected_state(
        cls,
        file_path: str,
        state,
        *,
        edit_revision: int = 0,
        document_id: str = "",
    ) -> None:
        """Freeze the DocumentRegistry baseline for the next durable write."""
        normalized = path_key(file_path) if file_path else ""
        if normalized:
            cls._document_save_expected_states[normalized] = state
            metadata = cls._document_write_metadata.setdefault(normalized, {})
            metadata["expected_file_state"] = state
            if document_id:
                metadata["document_id"] = str(document_id)
            revision_state = cls._asset_revision_state(normalized)
            cls._ensure_commit_chain_token(normalized, document_id=document_id)
            if revision_state.persisted_file_state is None:
                revision_state.expected_file_state = state
            if edit_revision:
                revision_state.edit_revision = max(
                    revision_state.edit_revision,
                    int(edit_revision),
                )
                metadata["edit_revision"] = revision_state.edit_revision

    @classmethod
    def _submit_document_snapshot(
        cls,
        file_path: str,
        snapshot: str,
        *,
        callback: Optional[Callable[[str], None]] = None,
    ):
        """Submit one immutable snapshot with a monotonic editor commit token."""
        normalized = path_key(file_path)
        state = cls._asset_revision_state(normalized)
        content_token = cls._content_token(snapshot)
        metadata = cls._document_write_metadata.get(normalized, {})
        chain_token = cls._ensure_commit_chain_token(
            normalized,
            document_id=str(metadata.get("document_id", "") or ""),
        )
        if content_token != state.content_token:
            state.edit_revision = max(state.edit_revision + 1, 1)
            state.preview_dependency_revision += 1
            state.content_token = content_token

        cls._document_save_expected_states.pop(normalized, None)
        # Inspector and other editor asset writes replace the current target.
        # A stale CAS baseline (slider drag after a disk edit, a mtime bump,
        # or the last commit in this document chain) must not reject the
        # user's in-memory values. Explicit callers that still want
        # protection pass expected_file_state to submit_document_text.
        expected = None

        state.requested_write_revision += 1
        requested_revision = state.requested_write_revision
        commit_token = uuid.uuid4().hex
        from Infernux.core.document_store import submit_document_text

        ticket = submit_document_text(
            file_path,
            snapshot,
            expected_file_state=expected,
            commit_chain_token=chain_token,
        )
        record = _PendingDocumentWrite(
            path=normalized,
            ticket=ticket,
            edit_revision=state.edit_revision,
            requested_write_revision=requested_revision,
            commit_token=commit_token,
            snapshot=snapshot,
            content_token=content_token,
            imported_disk_revision=state.imported_disk_revision,
            expected_file_state=expected,
            callback=callback,
        )
        cls._pending_document_write_records.setdefault(normalized, []).append(record)
        return ticket

    @classmethod
    def poll_pending_asset_writes(cls) -> None:
        """Retire every submitted ticket without allowing an old one to regress state."""
        for path, records in tuple(cls._pending_document_write_records.items()):
            remaining: list[_PendingDocumentWrite] = []
            for record in records:
                ticket = record.ticket
                if not bool(getattr(ticket, "is_complete", False)):
                    remaining.append(record)
                    continue
                status = str(getattr(ticket, "status", "") or "").lower()
                state = cls._asset_revision_state(path)
                if status == "succeeded":
                    cls._register_self_write_commit(record)
                    disk_is_current = (
                        record.imported_disk_revision == state.imported_disk_revision
                    )
                    if disk_is_current:
                        state.persisted_revision = max(
                            state.persisted_revision,
                            record.edit_revision,
                        )
                    committed_state = getattr(ticket, "committed_file_state", None)
                    if committed_state is not None and disk_is_current:
                        state.persisted_file_state = committed_state
                        state.expected_file_state = committed_state
                    if record.content_token == state.content_token and record.callback is None:
                        state.content_token = record.content_token
                elif status not in {"superseded", "cancelled"}:
                    detail = str(getattr(ticket, "error", "") or "").strip()
                    Debug.log_error(
                        f"Asset document write failed for '{path}' "
                        f"({status or 'unknown'}): {detail or 'no diagnostic'}"
                    )
                current_revision = (
                    record.content_token == state.content_token
                    and record.requested_write_revision == state.requested_write_revision
                    and record.imported_disk_revision == state.imported_disk_revision
                )
                if record.callback is not None and current_revision:
                    record.callback(status)

            if remaining:
                cls._pending_document_write_records[path] = remaining
            else:
                cls._pending_document_write_records.pop(path, None)

    @classmethod
    def _save_render_effect_resource(cls, resource_obj):
        """Submit a coalesced RenderEffect source write to DocumentStore."""
        file_path = str(getattr(resource_obj, "file_path", "") or "")
        if not file_path:
            return False
        norm_path = path_key(file_path)
        snapshot = cls._render_effect_save_snapshots.pop(norm_path, "")
        if not snapshot:
            from Infernux.renderstack.render_effect_asset import dump_render_effect_document

            snapshot = dump_render_effect_document(resource_obj.to_asset())
        try:
            return cls._submit_document_snapshot(
                file_path,
                snapshot,
                callback=getattr(resource_obj, "_on_save_completed", None),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            Debug.log_error(f"RenderEffect save submission failed for '{file_path}': {exc}")
            return False

    @classmethod
    def _save_material_resource(cls, resource_obj, target_path: str = ""):
        """Save a material resource without evicting its live in-memory value."""
        file_path = str(target_path or getattr(resource_obj, "file_path", "") or "")
        norm_path = path_key(file_path) if file_path else ""
        snapshot = cls._material_save_snapshots.pop(norm_path, "")
        if not snapshot:
            serialize = getattr(resource_obj, "serialize", None)
            if callable(serialize):
                snapshot = serialize() or ""
        if not file_path or not snapshot:
            return False
        # The live material has already been published by the editor. Record
        # that JSON for the preview/revision ledger before queuing disk IO;
        # disk still contains the previous value at this point.
        cls.note_asset_edit(
            file_path,
            content_token=cls._content_token(snapshot),
            material_json=snapshot,
        )
        callback = lambda status, _path=file_path, _snapshot=snapshot: (
            cls.on_material_saved(_path, _snapshot)
            if status == "succeeded"
            else None
        )
        try:
            ticket = cls._submit_document_snapshot(
                file_path,
                snapshot,
                callback=callback,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            Debug.log_error(f"Material save submission failed for '{file_path}': {exc}")
            return False
        return ticket

    @classmethod
    def on_material_saved(cls, path: str, material_json: str = "") -> None:
        """Hand a committed material preview back from memory to disk.

        The async write callback runs only after the atomic replace completed.
        Publishing the exact committed snapshot before releasing authoring
        ownership prevents ProjectPanel from briefly rendering the previous
        disk revision during the hand-off.
        """
        if not path:
            return
        if material_json:
            cls._prime_material_preview(path, material_json)
        cls.invalidate_path(path)
        cls._invalidate_material_ui_cache(path)
        if not cls.has_pending_local_revision(path):
            from Infernux.engine.ui.asset_resource_preview import (
                invalidate_live_material_preview,
            )

            invalidate_live_material_preview(path)

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
    def flush_scheduled_saves(cls, key: Optional[str] = None, *, force: bool = False):
        """Execute due scheduled saves. If key is given, only flush that key."""
        now = time.perf_counter()

        if key is not None:
            record = cls._scheduled_saves.get(key)
            if not record:
                # A runtime owner (for example RenderEffect.flush()) may have
                # submitted the shared path before its Inspector controller
                # reaches the footer in the same frame. Return that exact
                # receipt so DocumentRegistry can still own completion.
                pending = cls._pending_document_write_records.get(path_key(key), ())
                return pending[-1].ticket if pending else False
            if not force and bool(record.get("wait_one_flush", False)):
                record["wait_one_flush"] = False
                return False
            if not force and now < float(record.get("deadline", 0.0)):
                return False
            result: object = True
            try:
                save_fn = record.get("save_fn")
                if callable(save_fn):
                    value = save_fn()
                    result = False if value is False else (True if value is None else value)
            finally:
                cls._scheduled_saves.pop(key, None)
            return result

        due_keys = []
        for k, v in cls._scheduled_saves.items():
            if not force and bool(v.get("wait_one_flush", False)):
                v["wait_one_flush"] = False
                continue
            if force or now >= float(v.get("deadline", 0.0)):
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
        return bool(due_keys)

    @classmethod
    def flush_all_asset_writes(cls) -> None:
        """Force pending snapshots into DocumentStore and wait for durability."""
        cls.flush_scheduled_saves(force=True)
        from Infernux.core.document_store import DocumentStore

        DocumentStore.flush()
        cls.poll_pending_asset_writes()

    @classmethod
    def begin_asset_write_flush(cls) -> None:
        """Submit all pending asset snapshots without blocking the UI thread.

        Call :meth:`asset_writes_idle` from a frame-driven transaction until
        it returns ``True``.  This preserves the same durable boundary as
        ``flush_all_asset_writes`` without turning a Player-build click into a
        synchronous filesystem stall.
        """
        cls.flush_scheduled_saves(force=True)

    @classmethod
    def asset_writes_idle(cls) -> bool:
        """Poll submitted writes and report whether the durable queue drained."""
        cls.poll_pending_asset_writes()
        if cls._pending_document_write_records:
            return False
        from Infernux.core.document_store import DocumentStore

        return DocumentStore.is_idle()

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
        if ext in {".effect", ".effectgroup"}:
            from Infernux.renderstack.render_effect import RenderEffect
            return RenderEffect
        if ext in PARTICLE_GRAPH_EXTENSIONS:
            from Infernux.particle.asset import ParticleGraphAsset
            return ParticleGraphAsset
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
        from Infernux.renderstack.render_effect import EditableRenderEffectGroup, RenderEffect
        if asset_type is RenderEffect or (
            asset_type is None
            and path.lower().endswith((".effect", ".effectgroup"))
        ):
            try:
                from Infernux.renderstack.render_effect_asset import (
                    RenderEffectAsset,
                    RenderEffectGroupAsset,
                )
                from Infernux.renderstack.render_effect_compiler import (
                    RenderEffectArtifactRegistry,
                )

                guid = cls._get_guid_from_path(path) or ""
                artifact, document = RenderEffectArtifactRegistry.compile_and_publish(
                    path,
                    guid=guid,
                )
                if isinstance(document, RenderEffectAsset):
                    effect = RenderEffect(document, file_path=path, guid=guid)
                    effect._artifact_revision = artifact.revision
                    return effect
                if isinstance(document, RenderEffectGroupAsset):
                    return EditableRenderEffectGroup(
                        document,
                        file_path=path,
                        guid=guid,
                    )
                return None
            except (OSError, RuntimeError, TypeError, ValueError):
                return None
        from Infernux.particle.asset import ParticleGraphAsset
        if asset_type is ParticleGraphAsset or (
            asset_type is None and path.lower().endswith(".particlegraph")
        ):
            try:
                from Infernux.particle.artifact import ParticleArtifactRegistry

                guid = cls._get_guid_from_path(path) or ""
                ParticleArtifactRegistry.compile_path(path, guid=guid)
                return ParticleGraphAsset.load(path)
            except (OSError, RuntimeError, TypeError, ValueError):
                return None
        return None

    @classmethod
    def _schedule_gpu_texture_reload(cls, path: str) -> None:
        """Queue native GPU texture invalidation for the next post-draw tick."""
        key = cls._normalize_asset_path(path) or path_key(path)
        with cls._pending_gpu_texture_reloads_lock:
            cls._pending_gpu_texture_reloads[key] = path

        guid = cls._get_guid_from_path(path)
        if guid:
            cls._texture_cache.pop(guid, None)
            cls._cache.pop(guid, None)

    @classmethod
    def flush_pending_gpu_texture_reloads(
        cls,
        *,
        paths: Optional[List[str]] = None,
        max_items: Optional[int] = 1,
    ) -> int:
        """Publish queued GPU textures at a safe point without an unbounded frame.

        Normal post-draw ticks publish one texture. Explicit import UI may pass
        ``paths`` to synchronously publish only the asset the user applied.
        """
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be positive or None")
        with cls._pending_gpu_texture_reloads_lock:
            if paths is None:
                keys = list(cls._pending_gpu_texture_reloads)
            else:
                keys = []
                for path in paths:
                    key = cls._normalize_asset_path(path) or path_key(path)
                    if key in cls._pending_gpu_texture_reloads and key not in keys:
                        keys.append(key)
            if max_items is not None:
                keys = keys[:max_items]
            pending = [
                cls._pending_gpu_texture_reloads.pop(key)
                for key in keys
            ]
        for path in pending:
            cls._reload_gpu_texture_now(path)
        return len(pending)

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
            fingerprint = cls._capture_file_fingerprint(target)
        # Keep the exact fingerprint until the watcher presents it. A delayed
        # notification is a self-write only when its committed bytes match.
        cls._watcher_echo_suppression[key] = fingerprint

    @classmethod
    def is_watcher_echo_suppressed(cls, event_type: str, path: str, destination: str = "") -> bool:
        normalized = path_key(path) if path else ""
        if event_type == "modified" and normalized:
            # A watcher can run before the main thread polls the native ticket.
            # Inspect completed records directly so the editor's own replace is
            # acknowledged by commit fingerprint, not by a time window.
            for record in cls._pending_document_write_records.get(normalized, ()):
                if not bool(getattr(record.ticket, "is_complete", False)):
                    continue
                if str(getattr(record.ticket, "status", "") or "").lower() != "succeeded":
                    continue
                committed = getattr(record.ticket, "committed_file_state", None)
                if committed is not None and cls._file_state_matches(path, committed):
                    cls._register_self_write_commit(record)
                    return True

            if cls._acknowledge_self_write(normalized, path):
                return True

        key = cls._watcher_echo_key(event_type, path, destination)
        suppression = cls._watcher_echo_suppression.get(key)
        if suppression is None:
            return False
        expected_fingerprint = suppression
        if expected_fingerprint is None:
            cls._watcher_echo_suppression.pop(key, None)
            return False
        target = destination if event_type == "moved" else path
        current_fingerprint = cls._capture_file_fingerprint(target)
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
        from Infernux.engine.bootstrap import EditorBootstrap
        bs = EditorBootstrap.instance()
        pp = getattr(bs, "project_panel", None) if bs else None
        if pp is not None:
            pp.invalidate_dir_cache()
            native = cls._native_engine()
            if native is not None and hasattr(native, "request_full_speed_frame"):
                native.request_full_speed_frame()

    @classmethod
    def _prime_material_preview(cls, path: str, material_json: str = "") -> None:
        """Schedule the first material thumbnail as part of asset publication."""
        if os.path.splitext(path)[1].lower() != ".mat":
            return
        native = cls._native_engine()
        if native is None or not hasattr(native, "query_or_schedule_material_preview"):
            return
        try:
            normalized = resolved_path(path)
            live_document = str(material_json or "")
            stamp = 0 if live_document else int(os.stat(normalized).st_mtime_ns)
            resource_key = f"mat|{normalized}"
            native.query_or_schedule_material_preview(
                resource_key,
                normalized,
                live_document,
                stamp,
                bool(live_document),
            )
            if hasattr(native, "request_full_speed_frame"):
                native.request_full_speed_frame()
        except (OSError, RuntimeError) as exc:
            Debug.log_suppressed("AssetManager._prime_material_preview", exc)

    @staticmethod
    def _normalize_asset_path(path: str) -> str:
        return portable_path(path_key(path)) if path else ""

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

        from Infernux.ui import get_shared_cache
        cache = get_shared_cache()
        for ident in identifiers:
            if ident:
                cache.invalidate(ident)

        native = cls._native_engine()

        if native is not None:
            for ident in identifiers:
                if ident:
                    native.invalidate_texture_preview_task(f"ui_img|{ident}")

        from Infernux.engine.ui.asset_resource_preview import invalidate_resource_preview
        invalidate_resource_preview(path)

        from Infernux.engine.ui.window_manager import WindowManager
        wm = WindowManager.instance()
        if wm is not None:
            for panel in list(getattr(wm, "_window_instances", {}).values()):
                invalidate = getattr(panel, "invalidate_texture_thumbnail", None)
                if callable(invalidate):
                    invalidate(path)

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

        from Infernux.engine.ui.window_manager import WindowManager
        wm = WindowManager.instance()
        if wm is not None:
            for panel in list(getattr(wm, "_window_instances", {}).values()):
                invalidate = getattr(panel, "invalidate_material_thumbnail", None)
                if callable(invalidate):
                    invalidate(path)

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
    def invalidate_project_panel_cache(cls) -> None:
        """Refresh Project Panel listing after meta/import changes (explicit call only)."""
        cls._invalidate_project_panel_cache()
