"""
Infernux Scene Utilities — Unity-style static query API.

Provides top-level helper classes for finding GameObjects by tag/layer,
plus LayerMask utilities for constructing bitmasks, and a SceneManager
for loading scenes by name or build index (aligned with Unity's
``UnityEngine.SceneManagement.SceneManager``).

Example:
    from Infernux.scene import GameObjectQuery, LayerMask, SceneManager

    player = GameObjectQuery.find_with_tag("Player")
    enemies = GameObjectQuery.find_game_objects_with_tag("Enemy")
    ui_objects = GameObjectQuery.find_game_objects_in_layer(5)

    mask = LayerMask.get_mask("Default", "Water")

    # Load a scene by build index or name
    SceneManager.load_scene(0)
    SceneManager.load_scene("MainMenu")

    # Get the active scene
    scene = SceneManager.get_active_scene()
"""

import os
from typing import Union, Optional, List

from Infernux.lib import SceneManager as _NativeSceneManager, TagLayerManager
from Infernux.debug import Debug


class GameObjectQuery:
    """
    Static helper methods for Unity-style GameObject queries.
    
    Operates on the currently active scene.
    """

    @staticmethod
    def find(name: str):
        """Find a GameObject by name in the active scene."""
        scene = _NativeSceneManager.instance().get_active_scene()
        return scene.find(name) if scene else None

    @staticmethod
    def find_with_tag(tag: str):
        """Find the first GameObject with a given tag in the active scene."""
        scene = _NativeSceneManager.instance().get_active_scene()
        return scene.find_with_tag(tag) if scene else None

    @staticmethod
    def find_game_objects_with_tag(tag: str) -> list:
        """Find all GameObjects with a given tag in the active scene."""
        scene = _NativeSceneManager.instance().get_active_scene()
        return scene.find_game_objects_with_tag(tag) if scene else []

    @staticmethod
    def find_game_objects_in_layer(layer: int) -> list:
        """Find all GameObjects in a given layer in the active scene."""
        scene = _NativeSceneManager.instance().get_active_scene()
        return scene.find_game_objects_in_layer(layer) if scene else []

    @staticmethod
    def find_by_id(object_id: int):
        """Find a GameObject by its unique ID."""
        scene = _NativeSceneManager.instance().get_active_scene()
        return scene.find_by_id(object_id) if scene else None


class LayerMask:
    """
    Unity-style layer mask utilities.
    
    Layers are integers 0-31. A LayerMask is a 32-bit bitmask where
    bit N corresponds to layer N.
    
    Example:
        mask = LayerMask.get_mask("Default", "Water", "UI")
        if mask & LayerMask.get_mask("Default"):
            print("Default layer is in the mask")
    """

    @staticmethod
    def get_mask(*layer_names: str) -> int:
        """Create a layer mask from one or more layer names."""
        mgr = TagLayerManager.instance()
        mask = 0
        for name in layer_names:
            idx = mgr.get_layer_by_name(name)
            if idx >= 0:
                mask |= (1 << idx)
        return mask

    @staticmethod
    def layer_to_name(layer: int) -> str:
        """Convert a layer index to its name."""
        return TagLayerManager.instance().get_layer_name(layer)

    @staticmethod
    def name_to_layer(name: str) -> int:
        """Convert a layer name to its index (-1 if not found)."""
        return TagLayerManager.instance().get_layer_by_name(name)


# ---------------------------------------------------------------------------
# SceneManager — Unity-aligned scene loading & query API
# ---------------------------------------------------------------------------

class SceneManager:
    """
    Unity-style scene management API, aligned with
    ``UnityEngine.SceneManagement.SceneManager``.

    Scenes must first be added to the build list via the Build Settings panel.
    At runtime (play mode), use this class to load scenes by name or build
    index.

    Scene loading during play mode is **deferred** to the end of the current
    frame (just like Unity's ``SceneManager.LoadScene``).  This prevents
    crashes caused by modifying the scene hierarchy while C++ is iterating
    over it during lifecycle callbacks (``Start``/``Update``).

    Example::

        from Infernux.scene import SceneManager

        # Load by build index
        SceneManager.load_scene(0)

    # Load by scene name (filename without extension)
    SceneManager.load_scene("Level_01")

    # Prepare in the background, then switch at a safe frame boundary
    SceneManager.wait_for_load_scene("Level_02")

        # Get the active scene
        scene = SceneManager.get_active_scene()

        # Query available scenes
        print(SceneManager.scene_count)
        for i, name in enumerate(SceneManager.get_all_scene_names()):
            print(f"  {i}: {name}")
    """

    # Pending scene load request — deferred until end-of-frame when in play mode
    _pending_scene_load: Optional[str] = None  # resolved file path
    _active_scene_transaction = None
    _active_scene_load_path: Optional[str] = None
    _active_scene_file_manager = None
    _active_scene_wait_for_ready = False
    _active_scene_hold_for_activation = False
    _scene_load_generation = 0
    _active_scene_load_generation = 0
    _runtime_scene_service = None

    @staticmethod
    def install_runtime_service(service) -> None:
        """Install the packaged Player scene owner."""
        if service is None:
            raise ValueError("runtime scene service is required")
        SceneManager._runtime_scene_service = service

    @staticmethod
    def remove_runtime_service(service) -> None:
        if SceneManager._runtime_scene_service is service:
            SceneManager._runtime_scene_service = None

    # ------------------------------------------------------------------
    # Unity-aligned API
    # ------------------------------------------------------------------

    active_scene = None  # overwritten by the property below

    class _ActiveSceneDescriptor:
        """Static property descriptor — ``SceneManager.active_scene``."""
        def __get__(self, obj, objtype=None):
            return _NativeSceneManager.instance().get_active_scene()

    active_scene = _ActiveSceneDescriptor()

    @staticmethod
    def get_active_scene():
        """Return the currently active Scene (Unity: ``SceneManager.GetActiveScene()``)."""
        return _NativeSceneManager.instance().get_active_scene()

    @staticmethod
    def get_scene_by_name(name: str):
        """Find a scene path from the build list by name (Unity: ``GetSceneByName``).

        Returns the resolved file path, or ``None``.
        """
        target = name.lower()
        for p in SceneManager._load_build_list():
            n = os.path.splitext(os.path.basename(p))[0]
            if n.lower() == target:
                return p
        return None

    @staticmethod
    def get_scene_by_build_index(build_index: int):
        """Return a scene path by build index (Unity: ``GetSceneByBuildIndex``).

        Returns the resolved file path, or ``None``.
        """
        scenes = SceneManager._load_build_list()
        if 0 <= build_index < len(scenes):
            return scenes[build_index]
        return None

    @staticmethod
    def get_scene_at(index: int):
        """Return a scene path at a given index (Unity: ``GetSceneAt``).

        Currently equivalent to ``get_scene_by_build_index``.
        """
        return SceneManager.get_scene_by_build_index(index)

    # ------------------------------------------------------------------
    # Scene loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_build_list() -> List[str]:
        """Return the list of scene file paths from BuildSettings.json.

        In packaged builds the paths are stored relative to the project
        (Data/) directory.  This method resolves them to absolute paths
        using ``get_project_root()`` so that callers can safely pass them
        to ``os.path.isfile()`` and ``load_from_file()``.
        """
        from Infernux.engine.build_settings import load_build_settings
        data = load_build_settings()
        scenes = list(data["scenes"])

        from Infernux.engine.project_context import get_project_root
        root = get_project_root()
        if root:
            scenes = [
                os.path.join(root, p) if not os.path.isabs(p) else p
                for p in scenes
            ]
        return scenes

    @staticmethod
    def _resolve_build_scene(reference: str, scenes: List[str]) -> Optional[str]:
        """Resolve a Unity-style scene name, filename, or project path."""
        from Infernux.engine.path_utils import portable_path, relative_path, same_path
        from Infernux.engine.project_context import get_project_root

        value = str(reference or "").strip()
        if not value:
            return None

        root = get_project_root()
        target = portable_path(value).casefold()
        path_reference = "/" in target or os.path.isabs(value)

        if path_reference:
            if root:
                absolute_target = value if os.path.isabs(value) else os.path.join(root, value)
                for candidate in scenes:
                    if same_path(candidate, absolute_target):
                        return candidate

            for candidate in scenes:
                candidate_keys = {portable_path(candidate).casefold()}
                if root:
                    try:
                        candidate_keys.add(relative_path(candidate, root).casefold())
                    except ValueError:
                        pass
                if target in candidate_keys:
                    return candidate
            return None

        target_filename = os.path.basename(value).casefold()
        target_has_extension = target_filename.endswith(".scene")
        for candidate in scenes:
            filename = os.path.basename(candidate).casefold()
            if target_has_extension:
                if filename == target_filename:
                    return candidate
            elif os.path.splitext(filename)[0] == target_filename:
                return candidate
        return None

    @staticmethod
    def _resolve_load_path(scene: Union[int, str]) -> Optional[str]:
        """Resolve and validate one public scene-load request."""
        scenes = SceneManager._load_build_list()
        if not scenes:
            Debug.log_warning("SceneManager: Build list is empty.")
            return None

        path: Optional[str] = None

        if isinstance(scene, int):
            if 0 <= scene < len(scenes):
                path = scenes[scene]
            else:
                Debug.log_warning(
                    f"SceneManager: Build index {scene} out of range "
                    f"(0..{len(scenes) - 1})."
                )
                return None
        elif isinstance(scene, str):
            path = SceneManager._resolve_build_scene(scene, scenes)
            if path is None:
                Debug.log_warning(
                    f"SceneManager: Scene '{scene}' not found in build list."
                )
                return None
        else:
            Debug.log_warning("SceneManager: scene must be int or str.")
            return None

        # Packaged Players intentionally do not ship authoring ``.scene``
        # documents at their BuildSettings paths.  Keep the logical build-list
        # reference intact and let the immutable RuntimeAssetCatalog resolve it
        # to the cooked scene artifact.  Editor/runtime-with-files paths still
        # retain the eager disk validation below.
        if SceneManager._runtime_scene_service is not None:
            return path

        # Validate authoring file exists.
        if not os.path.isfile(path):
            Debug.log_warning(f"SceneManager: Scene file not found: {path}")
            return None
        return path

    @staticmethod
    def load_scene(scene: Union[int, str]) -> bool:
        """Load a scene from the build list.

        During play mode the request starts at the next safe frame boundary.
        Use :meth:`wait_for_load_scene` when the scene should begin preparing
        immediately while the current scene keeps running.
        """
        path = SceneManager._resolve_load_path(scene)
        if path is None:
            return False

        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(runtime_service.request_load(path))

        # --- Defer during play mode to avoid invalidating C++ iterators ---
        if SceneManager._is_in_play_mode():
            generation = SceneManager._begin_scene_load_request()
            if generation is None:
                return False
            SceneManager._pending_scene_load = path
            Debug.log_internal(
                f"SceneManager: Scene load deferred to end-of-frame — "
                f"{os.path.basename(path)}"
            )
            return True

        # --- Not in play mode: load immediately (editor double-click, etc.) ---
        return SceneManager._do_load(path)

    @staticmethod
    def wait_for_load_scene(scene: Union[int, str]) -> bool:
        """Prepare a scene in the background and switch when it is ready.

        File IO and native document validation begin immediately on the engine
        JobSystem. The current scene remains active until the prepared document
        reaches a safe frame boundary, where resource/script preflight and the
        final scene replacement are completed transactionally. ``Awake`` and
        ``Start`` therefore never run on a worker thread.

        The method is intentionally non-blocking; ``True`` means preparation
        was accepted. Use :meth:`is_scene_load_pending` to observe completion.
        Outside Play mode it behaves like :meth:`load_scene`.
        """
        path = SceneManager._resolve_load_path(scene)
        if path is None:
            return False

        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(runtime_service.request_prepared_load(path))

        if not SceneManager._is_in_play_mode():
            return SceneManager._do_load(path)
        generation = SceneManager._begin_scene_load_request()
        if generation is None:
            return False
        return SceneManager._start_runtime_load(
            path, wait_for_ready=True, generation=generation
        )

    @staticmethod
    def prepare_scene(scene: Union[int, str]) -> bool:
        """Prepare a scene without replacing the live scene.

        Reading, resource preflight, and Python component preflight advance in
        the background while the current scene keeps running. The transaction
        stops immediately before its live-scene commit until
        :meth:`activate_prepared_scene` is called. This is useful for seamless
        cinematic transitions where the commit must happen under a fully
        opaque frame.
        """
        path = SceneManager._resolve_load_path(scene)
        if path is None:
            return False

        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(
                runtime_service.request_prepared_load(
                    path, hold_for_activation=True
                )
            )

        if not SceneManager._is_in_play_mode():
            return False
        generation = SceneManager._begin_scene_load_request()
        if generation is None:
            return False
        return SceneManager._start_runtime_load(
            path,
            wait_for_ready=True,
            hold_for_activation=True,
            generation=generation,
        )

    @staticmethod
    def is_scene_prepared() -> bool:
        """Return whether a held scene transaction is ready to publish."""
        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(runtime_service.is_prepared)
        transaction = SceneManager._active_scene_transaction
        return bool(
            SceneManager._active_scene_hold_for_activation
            and transaction is not None
            and getattr(transaction, "status", "") == "ready_to_commit"
        )

    @staticmethod
    def activate_prepared_scene() -> bool:
        """Publish the scene previously prepared by :meth:`prepare_scene`."""
        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(runtime_service.activate_prepared_load())
        if not SceneManager.is_scene_prepared():
            return False
        SceneManager._active_scene_hold_for_activation = False
        return True

    @staticmethod
    def _is_in_play_mode() -> bool:
        """Check whether the engine is currently in play mode.

        The editor owns this state through ``PlayModeManager``. Packaged
        Players intentionally do not construct that editor service, so the
        native scene manager is the authoritative fallback there.
        """
        if SceneManager._runtime_scene_service is not None:
            return bool(_NativeSceneManager.instance().is_playing())
        from Infernux.engine.play_mode import PlayModeManager, PlayModeState
        pm = PlayModeManager.instance()
        if pm is not None:
            return pm.state != PlayModeState.EDIT
        return bool(_NativeSceneManager.instance().is_playing())

    @staticmethod
    def _do_load(path: str) -> bool:
        """Perform the actual scene file load (must be called outside C++ iteration)."""
        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(runtime_service.load_initial(path))
        # Use the editor SceneFileManager if available (handles Python component
        # restore, etc.), otherwise fall back to raw C++ _NativeSceneManager.
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm:
            return sfm.open_scene(path)

        # Runtime/current-schema path. Python component validation must happen
        # before the native staging graph commits.
        sm = _NativeSceneManager.instance()
        active = sm.get_active_scene()
        if not active:
            active = sm.create_scene("Scene")
        from Infernux.lib import AssetRegistry
        asset_database = AssetRegistry.instance().get_asset_database()
        from Infernux.engine.scene_document_transaction import SceneDocumentTransaction
        transaction = SceneDocumentTransaction(
            active,
            path=path,
            asset_database=asset_database,
            clear_registries=True,
            before_commit=getattr(sm, "prepare_active_scene_replacement", None),
        )
        loaded = transaction.run_to_completion(raise_on_failure=False)
        if not loaded:
            Debug.log_warning(f"SceneManager: failed to load {path}: {transaction.error}")
            return False
        Debug.log_internal(f"Scene loaded (runtime): {os.path.basename(path)}")
        return True

    @staticmethod
    def _create_runtime_load_transaction(path: str):
        """Create a transaction without changing the live scene."""
        from Infernux.engine.scene_manager import SceneFileManager

        sfm = SceneFileManager.instance()
        if sfm is not None:
            return sfm._create_open_scene_transaction(path), sfm

        sm = _NativeSceneManager.instance()
        scene = sm.get_active_scene()
        if scene is None:
            scene = sm.create_scene("Scene")
        from Infernux.lib import AssetRegistry
        from Infernux.engine.scene_document_transaction import SceneDocumentTransaction

        asset_database = AssetRegistry.instance().get_asset_database()
        return (
            SceneDocumentTransaction(
                scene,
                path=path,
                asset_database=asset_database,
                clear_registries=True,
                before_commit=getattr(sm, "prepare_active_scene_replacement", None),
            ),
            None,
        )

    @staticmethod
    def _clear_runtime_load_state() -> None:
        SceneManager._pending_scene_load = None
        SceneManager._active_scene_transaction = None
        SceneManager._active_scene_load_path = None
        SceneManager._active_scene_file_manager = None
        SceneManager._active_scene_wait_for_ready = False
        SceneManager._active_scene_hold_for_activation = False
        SceneManager._active_scene_load_generation = 0

    @staticmethod
    def _is_runtime_load_transaction_complete(transaction) -> bool:
        """Return terminal state without requiring the concrete transaction type."""
        complete = getattr(transaction, "is_complete", None)
        if complete is not None:
            return bool(complete() if callable(complete) else complete)
        return str(getattr(transaction, "status", "")).strip().lower() in {
            "completed",
            "failed",
            "cancelled",
        }

    @staticmethod
    def _begin_scene_load_request() -> Optional[int]:
        """Supersede unfinished preparation and return the newest request token."""
        transaction = SceneManager._active_scene_transaction
        if transaction is not None and not SceneManager._is_runtime_load_transaction_complete(
            transaction
        ):
            try:
                if not transaction.cancel():
                    Debug.log_warning(
                        "SceneManager: the active scene transaction is already committing."
                    )
                    return None
            except Exception as exc:
                Debug.log_error(f"SceneManager: failed to cancel stale scene load: {exc}")
                return None
        SceneManager._clear_runtime_load_state()
        SceneManager._scene_load_generation += 1
        return SceneManager._scene_load_generation

    @staticmethod
    def _start_runtime_load(
        path: str,
        *,
        wait_for_ready: bool,
        hold_for_activation: bool = False,
        generation: Optional[int] = None,
    ) -> bool:
        """Start background preparation now; live-scene mutation happens later."""
        if generation is None:
            generation = SceneManager._scene_load_generation
        if generation != SceneManager._scene_load_generation:
            return False
        try:
            transaction, sfm = SceneManager._create_runtime_load_transaction(path)
            if transaction is None:
                return False
            transaction.start()
        except Exception as exc:
            Debug.log_error(f"SceneManager: failed to start scene load: {exc}")
            return False
        SceneManager._active_scene_transaction = transaction
        SceneManager._active_scene_load_path = path
        SceneManager._active_scene_file_manager = sfm
        SceneManager._active_scene_wait_for_ready = bool(wait_for_ready)
        SceneManager._active_scene_hold_for_activation = bool(hold_for_activation)
        SceneManager._active_scene_load_generation = generation
        mode = "background preparation" if wait_for_ready else "runtime load"
        Debug.log_internal(
            f"SceneManager: started {mode} - {os.path.basename(path)}"
        )
        return True

    @staticmethod
    def process_pending_load():
        """Process a deferred scene load if one is pending.

        Called by ``PlayModeManager.tick()`` once per frame, after C++
        lifecycle calls (Update / LateUpdate / EndFrame) have finished.
        """
        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            runtime_service.process_pending_load()
            return

        transaction = SceneManager._active_scene_transaction
        if transaction is None:
            path = SceneManager._pending_scene_load
            if path is None:
                return
            SceneManager._pending_scene_load = None
            SceneManager._start_runtime_load(
                path,
                wait_for_ready=False,
                generation=SceneManager._scene_load_generation,
            )
            return

        active_generation = SceneManager._active_scene_load_generation
        if (
            active_generation > 0
            and active_generation != SceneManager._scene_load_generation
        ):
            if not SceneManager._is_runtime_load_transaction_complete(transaction):
                transaction.cancel()
            SceneManager._clear_runtime_load_state()
            return

        if (
            SceneManager._active_scene_hold_for_activation
            and getattr(transaction, "status", "") == "ready_to_commit"
        ):
            return

        # Advance one transaction phase per frame.  In particular, never spin
        # here while a JobSystem asset ticket is outstanding: doing so turns
        # wait_for_load_scene() into a main-thread busy wait and freezes the
        # frame that initiated an otherwise asynchronous scene load.
        if not transaction.poll():
            return

        path = SceneManager._active_scene_load_path
        sfm = SceneManager._active_scene_file_manager
        SceneManager._clear_runtime_load_state()
        if not transaction.succeeded:
            Debug.log_error(f"SceneManager: runtime scene load failed for {path}: {transaction.error}")
            return
        if sfm is not None:
            sfm._finish_open_scene(path, runtime_load=True)

        from Infernux.timing import Time
        Time._reset_frame_delta()

        sm = _NativeSceneManager.instance()
        scene = sm.get_active_scene()
        if scene:
            sm._start_active_scene_for_play()

    @staticmethod
    def is_scene_load_pending() -> bool:
        """Return whether a deferred runtime scene load is queued or executing."""
        runtime_service = SceneManager._runtime_scene_service
        if runtime_service is not None:
            return bool(runtime_service.is_load_pending)
        return (
            SceneManager._pending_scene_load is not None
            or SceneManager._active_scene_transaction is not None
        )

    # ------------------------------------------------------------------
    # Build-list queries
    # ------------------------------------------------------------------

    @staticmethod
    def get_scene_count() -> int:
        """Return the number of scenes in the build list."""
        return len(SceneManager._load_build_list())

    @staticmethod
    def get_scene_name(build_index: int) -> Optional[str]:
        """Return the scene name for a build index, or None if out of range."""
        scenes = SceneManager._load_build_list()
        if 0 <= build_index < len(scenes):
            return os.path.splitext(os.path.basename(scenes[build_index]))[0]
        return None

    @staticmethod
    def get_scene_path(build_index: int) -> Optional[str]:
        """Return the absolute scene file path for a build index."""
        scenes = SceneManager._load_build_list()
        if 0 <= build_index < len(scenes):
            return scenes[build_index]
        return None

    @staticmethod
    def get_build_index(name: str) -> int:
        """Return the build index for a scene name, or -1 if not found."""
        target = name.lower()
        for i, p in enumerate(SceneManager._load_build_list()):
            n = os.path.splitext(os.path.basename(p))[0]
            if n.lower() == target:
                return i
        return -1

    @staticmethod
    def get_all_scene_names() -> List[str]:
        """Return a list of all scene names in build order."""
        return [
            os.path.splitext(os.path.basename(p))[0]
            for p in SceneManager._load_build_list()
        ]

    @staticmethod
    def dont_destroy_on_load(game_object) -> None:
        """Mark *game_object* so it survives scene loads (Unity: ``DontDestroyOnLoad``)."""
        _NativeSceneManager.instance().dont_destroy_on_load(game_object)


__all__ = [
    "GameObjectQuery",
    "LayerMask",
    "TagLayerManager",
    "SceneManager",
]
