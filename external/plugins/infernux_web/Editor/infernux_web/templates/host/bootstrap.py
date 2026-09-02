"""Bootstrap contract between the browser host and the Infernux wasm runtime."""

from __future__ import annotations

from collections import deque
import importlib.util
import json
import marshal
import math
import os
import struct
import sys
from types import CodeType, ModuleType
from typing import Any


_events: deque[tuple[str, dict[str, Any]]] = deque(maxlen=4096)
_seen_event_kinds: set[str] = set()
_frame_count = 0
_player_session: Any = None
_player_scene_manager: Any = None
_player_initial_scene_path = ""
_player_runtime_manifest: Any = None
_player_runtime_catalog: Any = None
_player_asset_database: Any = None
_player_asset_count = 0
_player_type_count = 0
_player_initial_scene_name = ""
_player_activated = False
_screen_width = 1
_screen_height = 1
_screen_ui_renderer: Any = None
_screen_ui_texture_cache: Any = None
_screen_ui_event_processor: Any = None
_web_splash: Any = None
_runtime_api_installed = False
_player_root = "/infernux/player"
_player_python = f"{_player_root}/python/site-packages"
_runtime_data_root = ""
if os.path.isdir(_player_python) and _player_python not in sys.path:
    sys.path.insert(0, _player_python)
if os.path.isdir(_player_root):
    print("INFERNUX_WEB_COOKED_CONTENT_READY root=/infernux/player")


def _prepare_cooked_player_content() -> str:
    """Validate and extract packaged content into the browser filesystem."""

    if not os.path.isdir(_player_root):
        return ""
    data_roots = sorted(
        os.path.join(_player_root, name)
        for name in os.listdir(_player_root)
        if name.endswith("_Data")
        and os.path.isdir(os.path.join(_player_root, name))
    )
    if len(data_roots) != 1:
        raise RuntimeError(
            "Web Player requires exactly one cooked *_Data directory; "
            f"found {len(data_roots)}"
        )
    data_root = data_roots[0]
    catalog_path = os.path.join(data_root, "Library", "RuntimeAssetCatalog.json")
    with open(catalog_path, encoding="utf-8") as stream:
        catalog = json.load(stream)

    from _InfernuxWebHost import extract_package, read_entry

    packages = catalog.get("packages")
    if not isinstance(packages, list) or not packages:
        raise RuntimeError("Web Player runtime catalog has no packages")
    extracted_packages: set[str] = set()
    extracted_entries = 0
    for package_record in packages:
        if not isinstance(package_record, dict):
            raise RuntimeError("Web Player runtime catalog package is invalid")
        relative = str(package_record.get("path", ""))
        normalized = os.path.normpath(relative).replace("\\", "/")
        if not relative or normalized.startswith("../") or os.path.isabs(relative):
            raise RuntimeError("Web Player runtime package path is invalid")
        package = os.path.join(_player_root, *normalized.split("/"))
        summary = extract_package(package, data_root)
        if (
            not isinstance(summary, dict)
            or int(summary.get("archive_bytes", -1))
            != int(package_record.get("archive_bytes", -2))
        ):
            raise RuntimeError(f"Web Player package identity mismatch: {relative}")
        extracted_packages.add(normalized.casefold())
        extracted_entries += int(summary.get("entries", 0))

    scripts = 0
    scenes = 0
    for artifact in catalog.get("artifacts", ()):
        logical_type = artifact.get("logical_type")
        if logical_type not in {"compiled_script", "scene_artifact"}:
            continue
        package = os.path.join(_player_root, artifact["package"])
        package_key = os.path.normpath(str(artifact["package"])).replace("\\", "/").casefold()
        if package_key not in extracted_packages:
            raise RuntimeError(
                f"Web Player artifact references an undeclared package: {artifact['package']}"
            )
        payload = read_entry(package, artifact["runtime_path"])
        if logical_type == "compiled_script":
            if len(payload) < 16 or payload[:4] != importlib.util.MAGIC_NUMBER:
                raise RuntimeError(
                    f"Web Player script ABI mismatch: {artifact['runtime_path']}"
                )
            code = marshal.loads(payload[16:])
            if not isinstance(code, CodeType):
                raise RuntimeError(
                    f"Web Player script is not a code object: {artifact['runtime_path']}"
                )
            scripts += 1
        else:
            json.loads(payload)
            scenes += 1

    if scripts == 0 or scenes == 0:
        raise RuntimeError(
            "Web Player content must contain at least one compiled script and scene"
        )
    print(
        "INFERNUX_WEB_CONTENT_INDEX_READY "
        f"artifacts={len(catalog.get('artifacts', ()))} scripts={scripts} scenes={scenes} "
        f"extracted={extracted_entries}"
    )
    return data_root


_runtime_data_root = _prepare_cooked_player_content()


def infernux_web_configure_physics() -> bool:
    """Apply project physics settings before the native world is created."""

    if not _runtime_data_root:
        raise RuntimeError("Web Player has no extracted runtime data root")
    import _Infernux as native_module

    _install_platform_runtime_api(native_module)
    from Infernux.physics import settings as physics_settings

    physics_path = physics_settings.settings_path(_runtime_data_root)
    authored = os.path.isfile(physics_path)
    configuration = physics_settings.load(_runtime_data_root)
    if not authored:
        # Desktop defaults reserve for very large simulations. A project that
        # needs those capacities can author PhysicsSettings explicitly; a Web
        # project with no file should not commit hundreds of MiB at startup.
        configuration.update(
            temp_allocator_mb=32,
            max_jobs=1024,
            max_barriers=8,
            max_bodies=8192,
            max_body_pairs=16384,
            max_contact_constraints=8192,
        )
    # The current Web runtime is intentionally owner-thread driven. Jolt may
    # still publish jobs, but the shared scheduler consumes them serially.
    configuration["max_concurrency"] = 1
    from _InfernuxWebHost import configure_physics
    from Infernux.timing import Time

    configure_physics(json.dumps(configuration, separators=(",", ":")))
    Time._fixed_delta_time = configuration["fixed_delta_time"]
    Time._maximum_delta_time = configuration["max_fixed_delta_time"]
    print(
        "INFERNUX_WEB_PHYSICS_CONFIGURED "
        f"source={'project' if authored else 'web-default'} "
        f"temp_mb={configuration['temp_allocator_mb']} "
        f"bodies={configuration['max_bodies']}"
    )
    return True


def _register_web_shaders() -> None:
    """Load the deterministic WGSL catalog produced by the Web cook."""

    shader_root = os.path.join(_player_root, "web-shaders")
    catalog_path = os.path.join(shader_root, "catalog.json")
    if not os.path.isfile(catalog_path):
        raise RuntimeError("Web Player shader catalog is missing")
    with open(catalog_path, encoding="utf-8") as stream:
        catalog = json.load(stream)
    if (
        catalog.get("$schema") != "infernux.web_shader_catalog"
        or "version" in catalog
        or not isinstance(catalog.get("shaders"), list)
        or not catalog["shaders"]
    ):
        raise RuntimeError("Web Player shader catalog is invalid")

    from _InfernuxWebHost import register_shader

    identities: set[tuple[str, str]] = set()
    for entry in catalog["shaders"]:
        if not isinstance(entry, dict):
            raise RuntimeError("Web Player shader catalog entry is invalid")
        name = str(entry.get("name", ""))
        stage = str(entry.get("stage", ""))
        relative = str(entry.get("path", ""))
        identity = (name, stage)
        normalized = os.path.normpath(relative).replace("\\", "/")
        if (
            not name
            or stage not in {"vertex", "fragment", "compute"}
            or identity in identities
            or not relative
            or normalized.startswith("../")
            or os.path.isabs(relative)
        ):
            raise RuntimeError("Web Player shader catalog identity is invalid")
        identities.add(identity)
        shader_path = os.path.join(shader_root, *normalized.split("/"))
        with open(shader_path, "rb") as stream:
            payload = stream.read()
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(
                f"Web Player shader is not UTF-8 WGSL: {name} ({stage})"
            ) from error
        register_shader(name, stage, source)
    print(f"INFERNUX_WEB_SHADER_CATALOG_READY shaders={len(identities)}")


_register_web_shaders()


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise RuntimeError(f"Web Player document must be an object: {path}")
    return document


def _package_namespace(name: str, path: str) -> ModuleType:
    module = ModuleType(name)
    module.__file__ = os.path.join(path, "__init__.py")
    module.__package__ = name
    module.__path__ = [path]
    sys.modules[name] = module
    return module


def _install_platform_runtime_api(native_module: Any) -> None:
    """Assemble the game-facing package without importing editor bootstrap."""

    global _runtime_api_installed
    if _runtime_api_installed:
        return

    package_root = os.path.join(_player_python, "Infernux")
    package = _package_namespace("Infernux", package_root)
    _package_namespace("Infernux.engine", os.path.join(package_root, "engine"))
    _package_namespace("Infernux.core", os.path.join(package_root, "core"))
    components = _package_namespace(
        "Infernux.components", os.path.join(package_root, "components")
    )
    builtin = _package_namespace(
        "Infernux.components.builtin",
        os.path.join(package_root, "components", "builtin"),
    )
    sys.modules["Infernux.lib._Infernux"] = native_module

    import importlib

    lib = importlib.import_module("Infernux.lib")
    math_module = importlib.import_module("Infernux.math")
    debug_module = importlib.import_module("Infernux.debug")
    component_module = importlib.import_module("Infernux.components.component")
    decorators_module = importlib.import_module("Infernux.components.decorators")
    fields_module = importlib.import_module("Infernux.components.fields")
    ref_wrappers_module = importlib.import_module("Infernux.components.ref_wrappers")
    serializable_module = importlib.import_module(
        "Infernux.components.serializable_object"
    )
    lifecycle_module = importlib.import_module(
        "Infernux.components._component_lifecycle"
    )
    particle_module = importlib.import_module("Infernux.components.particle_system")
    builtin_modules = {
        "AudioListener": "audio_listener",
        "AudioSource": "audio_source",
        "Light": "light",
        "MeshRenderer": "mesh_renderer",
        "LineRenderer": "line_renderer",
        "SkinnedMeshRenderer": "skinned_mesh_renderer",
        "Camera": "camera",
        "Collider": "collider",
        "BoxCollider": "box_collider",
        "SphereCollider": "sphere_collider",
        "CapsuleCollider": "capsule_collider",
        "CylinderCollider": "cylinder_collider",
        "MeshCollider": "mesh_collider",
        "Rigidbody": "rigidbody",
        "RigidbodyConstraints": "rigidbody",
        "CollisionDetectionMode": "rigidbody",
        "RigidbodyInterpolation": "rigidbody",
    }
    for export_name, module_name in builtin_modules.items():
        source = importlib.import_module(
            f"Infernux.components.builtin.{module_name}"
        )
        value = getattr(source, export_name)
        setattr(builtin, export_name, value)
        setattr(components, export_name, value)
    builtin.__all__ = tuple(builtin_modules)

    component_exports = {
        "InxComponent": component_module.InxComponent,
        "SerializableObject": serializable_module.SerializableObject,
        "serialized_field": fields_module.serialized_field,
        "int_field": fields_module.int_field,
        "list_field": fields_module.list_field,
        "GameObjectRef": ref_wrappers_module.GameObjectRef,
        "disallow_multiple": decorators_module.disallow_multiple,
        "add_component_menu": decorators_module.add_component_menu,
        "RuntimeExecutionScheduler": lifecycle_module.RuntimeExecutionScheduler,
        "ParticleSystem": particle_module.ParticleSystem,
        "ParticleBoundsMode": particle_module.ParticleBoundsMode,
        "ParticleOffscreenPolicy": particle_module.ParticleOffscreenPolicy,
    }
    for name, value in component_exports.items():
        setattr(components, name, value)
    components.__all__ = tuple(component_exports) + tuple(builtin_modules)

    runtime_services = importlib.import_module("Infernux.runtime_services")
    web_host = importlib.import_module("_InfernuxWebHost")
    runtime_services.install_runtime_service("gpu-particles", web_host)
    runtime_services.install_runtime_service("text-input", web_host)
    screen_module = importlib.import_module("Infernux.screen")
    timing_module = importlib.import_module("Infernux.timing")
    mathf_module = importlib.import_module("Infernux.mathf")
    scene_module = importlib.import_module("Infernux.scene")
    coroutine_module = importlib.import_module("Infernux.coroutine")
    batch_module = importlib.import_module("Infernux.batch")
    instantiate_module = importlib.import_module("Infernux.instantiate")

    for source in (lib, math_module):
        exports = getattr(source, "__all__", None)
        if exports is None:
            exports = tuple(name for name in vars(source) if not name.startswith("_"))
        for name in exports:
            if hasattr(source, name):
                setattr(package, name, getattr(source, name))
    for name in components.__all__:
        setattr(package, name, getattr(components, name))
    for name in screen_module.__all__:
        setattr(package, name, getattr(screen_module, name))
    gameplay_exports = {
        "Time": timing_module.Time,
        "Mathf": mathf_module.Mathf,
        "GameObjectQuery": scene_module.GameObjectQuery,
        "LayerMask": scene_module.LayerMask,
        "SceneManager": scene_module.SceneManager,
        "Coroutine": coroutine_module.Coroutine,
        "WaitForSeconds": coroutine_module.WaitForSeconds,
        "WaitForSecondsRealtime": coroutine_module.WaitForSecondsRealtime,
        "WaitForEndOfFrame": coroutine_module.WaitForEndOfFrame,
        "WaitForFrames": coroutine_module.WaitForFrames,
        "WaitForFixedUpdate": coroutine_module.WaitForFixedUpdate,
        "WaitUntil": coroutine_module.WaitUntil,
        "WaitWhile": coroutine_module.WaitWhile,
        "batch_read": batch_module.batch_read,
        "batch_write": batch_module.batch_write,
        "Instantiate": instantiate_module.Instantiate,
        "Destroy": instantiate_module.Destroy,
    }
    for name, value in gameplay_exports.items():
        setattr(package, name, value)
    package.Debug = debug_module.Debug
    package.__version__ = "0.4.0"
    package.__all__ = tuple(
        sorted(
            {
                "Debug",
                *screen_module.__all__,
                *getattr(math_module, "__all__", ()),
                *components.__all__,
                *gameplay_exports,
                *(
                    name
                    for name in (
                        "GameObject",
                        "Transform",
                        "Component",
                        "Space",
                        "PrimitiveType",
                        "LineAlignment",
                        "LineTextureMode",
                        "LineGradientMode",
                        "LineCurveWrapMode",
                        "LineColorKey",
                        "LineWidthKey",
                    )
                    if hasattr(lib, name)
                ),
            }
        )
    )
    _runtime_api_installed = True


def _install_runtime_lifecycle_bridge(scene_manager: Any, scheduler: Any) -> None:
    from Infernux.engine.runtime_change_journal import RuntimeFrameBarrier
    from Infernux.lib import NativeRuntimeFrameBarrier

    scene_manager.set_runtime_lifecycle_callbacks(
        scheduler.begin_native_frame,
        lambda delta: scheduler.execute_native_phase("fixed_update", delta),
        lambda delta: scheduler.execute_native_phase("update", delta),
        lambda delta: scheduler.execute_native_phase("late_update", delta),
        scheduler.execute_native_editor_update,
        scheduler.end_native_frame,
    )
    barrier_map = {
        NativeRuntimeFrameBarrier.TRANSFORM_TO_PHYSICS:
            RuntimeFrameBarrier.TRANSFORM_TO_PHYSICS,
        NativeRuntimeFrameBarrier.PHYSICS_SIMULATION:
            RuntimeFrameBarrier.PHYSICS_SIMULATION,
        NativeRuntimeFrameBarrier.PHYSICS_TO_TRANSFORM:
            RuntimeFrameBarrier.PHYSICS_TO_TRANSFORM,
        NativeRuntimeFrameBarrier.TRANSFORM_RESOLVE:
            RuntimeFrameBarrier.TRANSFORM_RESOLVE,
        NativeRuntimeFrameBarrier.FINAL_TRANSFORM_RESOLVE:
            RuntimeFrameBarrier.FINAL_TRANSFORM_RESOLVE,
        NativeRuntimeFrameBarrier.ANIMATION_TIMELINE:
            RuntimeFrameBarrier.ANIMATION_TIMELINE,
        NativeRuntimeFrameBarrier.RENDER_EXTRACTION:
            RuntimeFrameBarrier.RENDER_EXTRACTION,
        NativeRuntimeFrameBarrier.RENDER_GRAPH:
            RuntimeFrameBarrier.RENDER_GRAPH,
        NativeRuntimeFrameBarrier.SNAPSHOT_PUBLICATION:
            RuntimeFrameBarrier.SNAPSHOT_PUBLICATION,
        NativeRuntimeFrameBarrier.PENDING_DESTROY:
            RuntimeFrameBarrier.PENDING_DESTROY,
    }
    scene_manager.set_runtime_frame_barrier_callback(
        lambda barrier: scheduler.consume_native_barrier(barrier_map[barrier])
    )
    scheduler.sync_native_work_availability()


def _prepare_player_asset_contract() -> None:
    global _player_initial_scene_path, _player_runtime_manifest
    global _player_runtime_catalog, _player_asset_database
    global _player_asset_count, _player_type_count, _player_initial_scene_name

    if _player_runtime_catalog is not None:
        return
    if not _runtime_data_root:
        raise RuntimeError("Web Player has no extracted runtime data root")
    os.environ["_INFERNUX_PLAYER_MODE"] = "1"
    os.environ["_INFERNUX_PLAYER_DATA_ROOT"] = _runtime_data_root
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True

    import _Infernux as native_module

    _install_platform_runtime_api(native_module)
    from _InfernuxWebHost import initialize_runtime_assets
    from Infernux.engine.player_service_graph import (
        PlayerRuntimeAssetCatalog,
        RuntimeProductManifest,
    )
    from Infernux.engine.project_context import set_project_root
    from Infernux.engine.runtime_type_registry import install_runtime_type_registry
    from Infernux.lib import AssetRegistry

    set_project_root(_runtime_data_root)
    manifest_document = _read_json(
        os.path.join(_runtime_data_root, "Player.inxmanifest")
    )
    catalog_document = _read_json(
        os.path.join(_runtime_data_root, "Library", "RuntimeAssetCatalog.json")
    )
    records_path = os.path.join(
        _runtime_data_root, "Library", "RuntimeAssetRecords.json"
    )
    records_document = _read_json(records_path)
    type_registry_path = os.path.join(
        _runtime_data_root, "Library", "RuntimeTypeRegistry.json"
    )
    build_settings = _read_json(
        os.path.join(_runtime_data_root, "ProjectSettings", "BuildSettings.json")
    )
    scenes = build_settings.get("scenes")
    if not isinstance(scenes, list) or not scenes or not isinstance(scenes[0], str):
        raise RuntimeError("Web Player BuildSettings has no initial scene")

    asset_count = int(initialize_runtime_assets(_runtime_data_root, records_path))
    print(f"INFERNUX_WEB_ASSET_REGISTRY_READY assets={asset_count}")
    database = AssetRegistry.instance().get_asset_database()
    if database is None or database.asset_count != asset_count:
        raise RuntimeError("Web Player runtime asset database was not published")
    print("INFERNUX_WEB_ASSET_DATABASE_READY")
    type_count = install_runtime_type_registry(type_registry_path)
    print(f"INFERNUX_WEB_TYPE_REGISTRY_READY types={type_count}")
    runtime_manifest = RuntimeProductManifest.from_document(manifest_document)
    runtime_catalog = PlayerRuntimeAssetCatalog.from_documents(
        _runtime_data_root,
        catalog_document,
        records_document,
    )
    print("INFERNUX_WEB_RUNTIME_CONTRACT_READY")
    import Infernux.components as runtime_components

    if not hasattr(runtime_components, "ParticleSystem"):
        raise RuntimeError("Web Player component surface omitted ParticleSystem")
    print(
        "INFERNUX_WEB_COMPONENT_SURFACE_READY "
        "audio=true particle=true"
    )
    scene_path = runtime_catalog.resolve_scene(scenes[0])
    if scene_path is None:
        raise RuntimeError(
            f"Web Player runtime catalog cannot resolve initial scene: {scenes[0]}"
        )
    _player_initial_scene_path = scene_path
    _player_runtime_manifest = runtime_manifest
    _player_runtime_catalog = runtime_catalog
    _player_asset_database = database
    _player_asset_count = asset_count
    _player_type_count = type_count
    _player_initial_scene_name = scenes[0]


def _prepare_player_runtime() -> None:
    global _player_session, _player_scene_manager

    if _player_session is not None:
        return
    _prepare_player_asset_contract()
    from Infernux.engine.player_runtime import PlayerRuntimeSession
    from Infernux.lib import SceneManager

    session = PlayerRuntimeSession(asset_database=_player_asset_database)
    session.configure_runtime_contract(
        _player_runtime_manifest, _player_runtime_catalog
    )
    scene_manager = SceneManager.instance()
    _install_runtime_lifecycle_bridge(scene_manager, session.execution_scheduler)
    scene_path = _player_initial_scene_path
    scenes = [_player_initial_scene_name]
    print(f"INFERNUX_WEB_SCENE_LOADING scene={scenes[0]}")
    if not session.load_scene(scene_path):
        raise RuntimeError(
            "Web Player could not load its initial scene: "
            f"{session.last_scene_error or scenes[0]}"
        )
    active_scene = scene_manager.get_active_scene()
    if active_scene is None:
        raise RuntimeError("Web Player scene transaction published no active scene")
    _player_session = session
    _player_scene_manager = scene_manager
    print(
        "INFERNUX_WEB_SCENE_READY "
        f"assets={_player_asset_count} types={_player_type_count} "
        f"objects={len(active_scene.get_all_objects())} scene={scenes[0]}"
    )


class _WebSplashPlayer:
    """Engine-canvas splash playback for the WebGPU Player."""

    def __init__(self, items: list[dict[str, Any]], data_root: str) -> None:
        import _InfernuxWebHost as host

        self._host = host
        self._data_root = os.path.abspath(data_root)
        self._items = [self._validate_item(item) for item in items]
        self._index = 0
        self._elapsed = 0.0
        self._texture_id = 0
        self._width = 0
        self._height = 0
        self._video_payload = b""
        self._video_frames: tuple[tuple[int, int], ...] = ()
        self._video_fps = 0.0
        self._video_frame = -1
        self._loaded = False

    @property
    def is_finished(self) -> bool:
        return self._index >= len(self._items)

    def _validate_item(self, source: Any) -> dict[str, Any]:
        if not isinstance(source, dict):
            raise RuntimeError("Web Player splash entries must be objects")
        required = {"type", "path", "layout", "duration", "fade_in", "fade_out"}
        if set(source) != required:
            raise RuntimeError("Web Player splash entry must use the current exact field set")
        item_type = str(source["type"])
        if item_type not in {"image", "video"}:
            raise RuntimeError(f"Web Player splash type is unsupported: {item_type!r}")
        layout = str(source["layout"])
        if layout not in {"logo", "contain", "cover"}:
            raise RuntimeError(f"Web Player splash layout is unsupported: {layout!r}")
        if item_type == "video" and layout != "cover":
            raise RuntimeError("Web Player video splash must use cover layout")
        relative = str(source["path"])
        normalized = os.path.normpath(relative).replace("\\", "/")
        if not relative or normalized.startswith("../") or os.path.isabs(relative):
            raise RuntimeError("Web Player splash path is invalid")
        path = os.path.abspath(os.path.join(self._data_root, *normalized.split("/")))
        if os.path.commonpath((self._data_root, path)) != self._data_root or not os.path.isfile(path):
            raise RuntimeError(f"Web Player splash asset is missing: {relative}")
        duration = float(source["duration"])
        fade_in = float(source["fade_in"])
        fade_out = float(source["fade_out"])
        if (
            not math.isfinite(duration)
            or duration <= 0.0
            or not math.isfinite(fade_in)
            or fade_in < 0.0
            or not math.isfinite(fade_out)
            or fade_out < 0.0
        ):
            raise RuntimeError("Web Player splash timing is invalid")
        return {
            "type": item_type,
            "path": path,
            "layout": layout,
            "duration": duration,
            "fade_in": fade_in,
            "fade_out": fade_out,
        }

    def _upload_image(self, encoded: bytes) -> None:
        texture_id, width, height = self._host.screen_ui_upload_image(
            encoded, self._texture_id
        )
        self._texture_id = int(texture_id)
        self._width = int(width)
        self._height = int(height)

    def _load_item(self) -> None:
        item = self._items[self._index]
        with open(item["path"], "rb") as stream:
            payload = stream.read()
        if item["type"] == "image":
            self._upload_image(payload)
        else:
            if len(payload) < 24 or payload[:8] != b"INFSPLSH":
                raise RuntimeError("Web Player video splash header is invalid")
            count, fps, width, height = struct.unpack_from("<IfII", payload, 8)
            index_end = 24 + int(count) * 8
            if (
                count == 0
                or not math.isfinite(float(fps))
                or fps <= 0.0
                or width == 0
                or height == 0
                or index_end > len(payload)
            ):
                raise RuntimeError("Web Player video splash metadata is invalid")
            frames: list[tuple[int, int]] = []
            frame_data_bytes = len(payload) - index_end
            for frame_index in range(int(count)):
                offset, size = struct.unpack_from("<II", payload, 24 + frame_index * 8)
                if size == 0 or offset > frame_data_bytes or size > frame_data_bytes - offset:
                    raise RuntimeError("Web Player video splash frame index is invalid")
                frames.append((index_end + int(offset), int(size)))
            self._video_payload = payload
            self._video_frames = tuple(frames)
            self._video_fps = float(fps)
            self._video_frame = 0
            self._upload_video_frame(0)
        self._loaded = True

    def _upload_video_frame(self, frame_index: int) -> None:
        offset, size = self._video_frames[frame_index]
        self._upload_image(self._video_payload[offset : offset + size])

    def _duration(self, item: dict[str, Any]) -> float:
        if item["type"] == "video":
            return len(self._video_frames) / self._video_fps
        return float(item["duration"])

    @staticmethod
    def _alpha(elapsed: float, duration: float, fade_in: float, fade_out: float) -> float:
        if elapsed < fade_in:
            return elapsed / fade_in if fade_in > 0.0 else 1.0
        if elapsed > duration - fade_out:
            return max(0.0, duration - elapsed) / fade_out if fade_out > 0.0 else 0.0
        return 1.0

    @staticmethod
    def _layout_rect(
        layout: str, view_width: int, view_height: int, image_width: int, image_height: int
    ):
        if layout == "logo":
            scale = min(view_width * 0.45 / image_width, view_height * 0.45 / image_height)
        elif layout == "contain":
            scale = min(view_width / image_width, view_height / image_height)
        elif layout == "cover":
            scale = max(view_width / image_width, view_height / image_height)
        else:
            raise RuntimeError(f"Web Player splash layout is unsupported: {layout!r}")
        width = image_width * scale
        height = image_height * scale
        return ((view_width - width) * 0.5, (view_height - height) * 0.5, width, height)

    def _release_item(self) -> None:
        self._video_payload = b""
        self._video_frames = ()
        self._video_fps = 0.0
        self._video_frame = -1
        self._loaded = False

    def update(self, delta_time: float, view_width: int, view_height: int) -> bool:
        if self.is_finished:
            return False
        if not self._loaded:
            self._load_item()
        item = self._items[self._index]
        duration = self._duration(item)
        if item["type"] == "video":
            frame_index = min(
                int(self._elapsed * self._video_fps), len(self._video_frames) - 1
            )
            if frame_index != self._video_frame:
                self._upload_video_frame(frame_index)
                self._video_frame = frame_index

        alpha = self._alpha(
            self._elapsed, duration, float(item["fade_in"]), float(item["fade_out"])
        )
        x, y, width, height = self._layout_rect(
            item["layout"], view_width, view_height, self._width, self._height
        )
        self._host.screen_ui_begin_frame(view_width, view_height)
        self._host.screen_ui_add_filled_rect(
            1, 0.0, 0.0, float(view_width), float(view_height), 0.0, 0.0, 0.0, 1.0, 0.0
        )
        self._host.screen_ui_add_image(
            1,
            self._texture_id,
            x,
            y,
            x + width,
            y + height,
            0.0,
            0.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            alpha,
            0.0,
            False,
            False,
            0.0,
        )
        self._elapsed += max(0.0, float(delta_time))
        if self._elapsed >= duration:
            self._release_item()
            self._index += 1
            self._elapsed = 0.0
        return not self.is_finished

    def close(self) -> None:
        if self._texture_id:
            self._host.screen_ui_release_texture(self._texture_id)
            self._texture_id = 0
        self._release_item()


def _activate_web_player_session() -> None:
    global _player_activated

    if _player_activated:
        return
    if _player_session is None or not _player_session.activate():
        raise RuntimeError("Web Player runtime session could not be activated")
    _player_activated = True
    print("INFERNUX_WEB_RUNTIME_ACTIVE")


def _create_web_splash() -> _WebSplashPlayer | None:
    manifest = _read_json(os.path.join(_runtime_data_root, "BuildManifest.json"))
    items = manifest.get("splash_items", [])
    if not isinstance(items, list):
        raise RuntimeError("Web Player splash_items must be an array")
    return _WebSplashPlayer(items, _runtime_data_root) if items else None


def infernux_web_ready(details: dict[str, Any]) -> None:
    """Receive the browser graphics and viewport contract from the native host."""

    global _screen_width, _screen_height, _screen_ui_renderer, _screen_ui_texture_cache
    global _screen_ui_event_processor, _web_splash

    print(
        "INFERNUX_WEB_HOST_READY "
        f"python=3.13 graphics={details.get('graphics_api')} "
        f"viewport={details.get('width')}x{details.get('height')}"
    )
    if _player_session is None:
        _prepare_player_runtime()
    _screen_width = max(1, int(details.get("width", 1)))
    _screen_height = max(1, int(details.get("height", 1)))
    _screen_ui_renderer = _WebScreenUIRenderer()
    _screen_ui_texture_cache = _WebScreenUITextureCache()
    from Infernux.input import Input
    from Infernux.ui.ui_event_system import UIEventProcessor

    Input.set_game_focused(True)
    Input.set_game_viewport_origin(0.0, 0.0)
    _screen_ui_event_processor = UIEventProcessor()
    _web_splash = _create_web_splash()
    if _web_splash is None:
        _activate_web_player_session()
    else:
        print(f"INFERNUX_WEB_SPLASH_READY items={len(_web_splash._items)}")
    print("INFERNUX_WEB_AUDIO_USER_ACTIVATION_PENDING")
    return None


def infernux_web_render_settings() -> dict[str, Any]:
    """Return the active RenderStack subset implemented by the Web host."""

    _prepare_player_asset_contract()
    settings: dict[str, Any] = {
        "msaa_samples": 4,
        "bloom_enabled": False,
        "bloom_threshold": 1.0,
        "bloom_intensity": 0.8,
        "bloom_scatter": 0.7,
        "bloom_clamp": 65472.0,
        "bloom_tint": [1.0, 1.0, 1.0],
        "bloom_iterations": 5,
        "tonemapping_mode": 0,
        "tonemapping_exposure": 1.0,
    }
    stack = _web_render_stack_document()
    if stack is None:
        raise RuntimeError("Web Player initial scene requires an authored RenderStack")
    pipeline_name = str(stack.get("pipeline_class_name", ""))
    if pipeline_name != "Default Forward":
        raise RuntimeError(
            "Web Player currently requires the Default Forward RenderStack pipeline; "
            f"received {pipeline_name!r}"
        )
    pipeline_parameters = stack.get("pipeline_params_json")
    if not isinstance(pipeline_parameters, str):
        raise RuntimeError("Web Player RenderStack pipeline_params_json must be a JSON string")
    parameter_document = json.loads(pipeline_parameters)
    default_parameters = parameter_document.get("__default__")
    if not isinstance(default_parameters, dict):
        raise RuntimeError("Web Player RenderStack parameters are missing the __default__ object")
    serialized_msaa = default_parameters.get("msaa_samples")
    if isinstance(serialized_msaa, dict):
        serialized_msaa = serialized_msaa.get("__enum_name__")
    sample_names = {"X1": 1, "X4": 4}
    if serialized_msaa not in sample_names:
        raise RuntimeError(
            "WebGPU supports RenderStack MSAA X1 or X4; "
            f"received {serialized_msaa!r}"
        )
    settings["msaa_samples"] = sample_names[serialized_msaa]
    for slot in stack.get("effect_slots") or ():
        fields = slot.get("fields") if isinstance(slot, dict) else None
        if not isinstance(fields, dict) or not fields.get("enabled", False):
            continue
        reference = fields.get("effect")
        if not isinstance(reference, dict):
            raise RuntimeError("Web Player enabled RenderStack slot has no effect reference")
        for feature_type, parameters in _iter_web_render_effects(reference):
            if feature_type == "infernux.post.bloom":
                settings["bloom_enabled"] = True
                settings["bloom_threshold"] = float(parameters.get("threshold", 1.0))
                settings["bloom_intensity"] = float(parameters.get("intensity", 0.8))
                settings["bloom_scatter"] = float(parameters.get("scatter", 0.7))
                settings["bloom_clamp"] = float(parameters.get("clamp", 65472.0))
                settings["bloom_iterations"] = int(parameters.get("max_iterations", 5))
                tint = parameters.get("tint", [1.0, 1.0, 1.0, 1.0])
                if not isinstance(tint, (list, tuple)) or len(tint) < 3:
                    raise RuntimeError("Web Player Bloom tint must contain at least three values")
                settings["bloom_tint"] = [float(tint[0]), float(tint[1]), float(tint[2])]
            elif feature_type == "infernux.post.tonemapping":
                settings["tonemapping_mode"] = int(parameters.get("mode", 2))
                settings["tonemapping_exposure"] = float(parameters.get("exposure", 1.0))
    print(
        "INFERNUX_WEB_RENDER_STACK_READY "
        f"msaa={settings['msaa_samples']} "
        f"bloom={int(bool(settings['bloom_enabled']))} "
        f"iterations={settings['bloom_iterations']} "
        f"tonemapping={settings['tonemapping_mode']}"
    )
    return settings


def _web_render_stack_document() -> dict[str, Any] | None:
    """Read the initial scene's serialized RenderStack configuration."""

    if not _player_initial_scene_path:
        return None
    scene_document = _read_json(_player_initial_scene_path)
    pending = list(scene_document.get("objects") or scene_document.get("game_objects") or ())
    while pending:
        game_object = pending.pop()
        if not isinstance(game_object, dict):
            continue
        pending.extend(game_object.get("children") or ())
        for component in game_object.get("components") or ():
            if not isinstance(component, dict):
                continue
            type_id = str(component.get("type_id", ""))
            if type_id.rsplit(":", 1)[-1] == "RenderStack":
                data = component.get("data")
                return data if isinstance(data, dict) else None
    return None


def _iter_web_render_effects(
    reference: Any,
    overrides: dict[str, Any] | None = None,
    trail: frozenset[str] = frozenset(),
):
    """Expand packaged effect groups without importing editor render modules."""

    guid = str(
        reference.get("guid", "")
        if isinstance(reference, dict)
        else getattr(reference, "guid", "")
    )
    path_hint = str(
        reference.get("path_hint", "")
        if isinstance(reference, dict)
        else getattr(reference, "path_hint", "")
    )
    path = _web_render_effect_path(guid, path_hint)
    identity = guid.casefold() or os.path.normcase(os.path.abspath(path))
    if not path or identity in trail:
        return
    with open(path, encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise RuntimeError(f"Web Player render effect is not an object: {path}")
    schema = document.get("$schema")
    if schema == "infernux.render_effect":
        parameters = dict(document.get("parameters") or {})
        parameters.update(overrides or {})
        yield str(document.get("feature_type", "")), parameters
        return
    if schema != "infernux.render_effect_group":
        raise RuntimeError(f"Web Player render effect has an unsupported schema: {path}")
    next_trail = trail | {identity}
    for entry in document.get("entries") or ():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        child = entry.get("asset")
        if not isinstance(child, dict):
            continue
        child_overrides = dict(entry.get("overrides") or {})
        yield from _iter_web_render_effects(child, child_overrides, next_trail)


def _web_render_effect_path(guid: str, path_hint: str) -> str:
    """Resolve a packaged render-effect reference through its GUID."""

    from Infernux.lib import AssetRegistry

    if not guid:
        raise RuntimeError(
            f"Web Player render effect has no GUID: path_hint={path_hint!r}"
        )
    database = AssetRegistry.instance().get_asset_database()
    if database is None:
        raise RuntimeError("Web Player asset database is unavailable while resolving RenderStack")
    path = str(database.get_path_from_guid(guid) or "")
    if path and not os.path.isabs(path):
        path = os.path.join(_runtime_data_root, path)
    if path and os.path.isfile(path):
        return os.path.abspath(path)
    raise RuntimeError(
        "Web Player could not resolve render effect "
        f"guid={guid!r} path_hint={path_hint!r}"
    )


def infernux_web_activate(audio_ready: bool) -> bool:
    """Acknowledge the trusted browser gesture used to unlock WebAudio."""

    if not audio_ready:
        raise RuntimeError("Web Player audio did not unlock from the user gesture")
    if _player_session is None:
        raise RuntimeError("Web Player scene was not ready before audio activation")
    print("INFERNUX_WEB_AUDIO_USER_ACTIVATED")
    return True


def infernux_web_input(kind: str, payload: dict[str, Any]) -> None:
    """Queue one normalized browser event for the engine input adapter."""

    global _screen_width, _screen_height
    if kind == "viewport":
        _screen_width = max(1, int(payload.get("width", _screen_width)))
        _screen_height = max(1, int(payload.get("height", _screen_height)))
    _events.append((kind, payload))
    if kind not in _seen_event_kinds:
        _seen_event_kinds.add(kind)
        print(f"INFERNUX_WEB_INPUT_READY kind={kind}")


def infernux_web_runtime_diagnostic(probe: int, argument: int) -> float:
    """Return one numeric, read-only runtime probe for browser acceptance."""

    from Infernux.input import Input

    if probe == 0:
        return float(Input.get_key(int(argument)))
    if probe == 5:
        return float(Input.touch_count)
    if 6 <= probe <= 10:
        touch = Input.get_touch(int(argument))
        if probe == 6:
            return float(touch.finger_id)
        if probe == 7:
            return float(touch.normalized_position[0])
        if probe == 8:
            return float(touch.normalized_position[1])
        if probe == 9:
            return float(touch.is_primary)
        phase_codes = {
            "began": 0,
            "moved": 1,
            "stationary": 2,
            "ended": 3,
            "canceled": 4,
        }
        return float(phase_codes[touch.phase.value])
    if _player_session is None:
        return float("nan")
    scheduler = _player_session.execution_scheduler
    if probe == 1:
        return float(len(scheduler.phase_plan("fixed_update")))
    if probe == 2:
        return float(len(scheduler.phase_plan("update")))
    counters = scheduler.profiler_snapshot()
    if probe == 3:
        return float(counters.get("native_phase_dispatches", 0))
    if probe == 4:
        return float(counters.get("phase_errors", 0))
    return float("nan")


def infernux_web_drain_input() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return and clear pending events without exposing the mutable queue."""

    events = tuple(_events)
    _events.clear()
    return events


class _WebScreenUIList:
    Camera = 0
    Overlay = 1


class _WebScreenUIRenderer:
    """Python protocol adapter for the engine-owned WebGPU UI consumer."""

    def __init__(self) -> None:
        import _InfernuxWebHost as host

        self._host = host

    def begin_frame(self, width: int, height: int) -> None:
        self._host.screen_ui_begin_frame(width, height)

    def begin_frame_cached(
        self, width: int, height: int, content_revision: int
    ) -> bool:
        return bool(
            self._host.screen_ui_begin_frame_cached(
                width, height, content_revision & ((1 << 64) - 1)
            )
        )

    def add_filled_rect(self, *arguments: Any) -> None:
        self._host.screen_ui_add_filled_rect(*arguments)

    def add_image(self, *arguments: Any) -> None:
        self._host.screen_ui_add_image(*arguments)

    def add_text(self, *arguments: Any) -> None:
        self._host.screen_ui_add_text(*arguments)

    def measure_text(self, *arguments: Any) -> tuple[float, float]:
        measured = self._host.screen_ui_measure_text(*arguments)
        return float(measured[0]), float(measured[1])


class _WebScreenUITextureCache:
    """GUID-resolved bridge from runtime UI assets to WebGPU texture handles."""

    def __init__(self) -> None:
        import _InfernuxWebHost as host

        self._host = host
        self._textures: dict[str, int] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self.generation = 0

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def get(self, identifier: str) -> int:
        identifier = str(identifier or "")
        if not identifier or identifier in self._failed:
            return 0
        ready = self._textures.get(identifier)
        if ready:
            return ready
        resolved = int(self._host.screen_ui_resolve_texture(identifier))
        if resolved > 0:
            self._textures[identifier] = resolved
            self._pending.discard(identifier)
            self.generation += 1
            return resolved
        if resolved < 0:
            self._failed.add(identifier)
            self._pending.discard(identifier)
        else:
            self._pending.add(identifier)
        return 0

    def poll(self) -> None:
        for identifier in tuple(self._pending):
            self.get(identifier)


def _submit_screen_ui() -> None:
    if (
        _player_scene_manager is None
        or _screen_ui_renderer is None
        or _screen_ui_texture_cache is None
    ):
        return

    from Infernux.engine.runtime_screen_ui import RuntimeScreenUISubmission
    from Infernux.engine.ui.runtime_canvas_snapshot import (
        collect_sorted_runtime_canvas_snapshot,
    )
    from Infernux.ui.enums import RenderMode
    from Infernux.ui.ui_render_dispatch import runtime_ui_revision

    scene = _player_scene_manager.get_active_scene()
    persistent_scene = _player_scene_manager.get_runtime_persistent_scene()
    if scene is None:
        canvases = ()
    else:
        canvases = tuple(
            collect_sorted_runtime_canvas_snapshot(scene, persistent_scene)
        )
    _screen_ui_texture_cache.poll()
    revision = runtime_ui_revision(
        scene,
        canvases,
        _screen_width,
        _screen_height,
        _screen_ui_texture_cache.generation,
    )
    if not _screen_ui_texture_cache.has_pending and _screen_ui_renderer.begin_frame_cached(
        _screen_width, _screen_height, revision
    ):
        return
    if _screen_ui_texture_cache.has_pending:
        _screen_ui_renderer.begin_frame(_screen_width, _screen_height)
    for canvas in canvases:
        RuntimeScreenUISubmission._submit_canvas(
            canvas,
            _screen_ui_renderer,
            _screen_ui_texture_cache.get,
            _screen_width,
            _screen_height,
            _WebScreenUIList,
            RenderMode,
        )


def _process_screen_ui_events(delta_time: float) -> None:
    """Dispatch browser mouse input through the same Screen UI event system as Players."""

    if _player_scene_manager is None or _screen_ui_event_processor is None:
        return

    from Infernux.engine.ui.runtime_canvas_snapshot import (
        collect_sorted_runtime_canvas_snapshot,
    )
    from Infernux.input import Input

    scene = _player_scene_manager.get_active_scene()
    persistent_scene = _player_scene_manager.get_runtime_persistent_scene()
    canvases = (
        tuple(collect_sorted_runtime_canvas_snapshot(scene, persistent_scene))
        if scene is not None
        else ()
    )
    if not canvases:
        _screen_ui_event_processor.reset()
        return

    mouse_x, mouse_y, scroll_x, scroll_y, held, down, up = (
        Input.get_game_mouse_frame_state(0)
    )
    canvas_positions: list[tuple[float, float]] = []
    for canvas in canvases:
        reference_width = float(canvas.reference_width)
        reference_height = float(canvas.reference_height)
        if reference_width < 1.0 or reference_height < 1.0:
            canvas_positions.append((0.0, 0.0))
            continue
        scale_x, scale_y, _ = canvas.compute_scale(
            float(_screen_width), float(_screen_height)
        )
        canvas_positions.append(
            (
                float(mouse_x) / max(float(scale_x), 1.0e-6),
                float(mouse_y) / max(float(scale_y), 1.0e-6),
            )
        )

    _screen_ui_event_processor.process(
        list(canvases),
        canvas_positions,
        bool(down),
        bool(up),
        bool(held),
        (float(scroll_x), float(scroll_y)),
        max(0.0, min(float(delta_time), 0.25)),
    )


def infernux_web_tick(delta_time: float) -> bool:
    """Advance the Python side once per browser animation frame."""

    global _frame_count, _web_splash
    if _player_session is None:
        return False
    if _web_splash is not None:
        splash_active = _web_splash.update(
            max(0.0, min(float(delta_time), 0.25)), _screen_width, _screen_height
        )
        if splash_active:
            return True
        _web_splash.close()
        _web_splash = None
        print("INFERNUX_WEB_SPLASH_COMPLETE")
        _activate_web_player_session()
        return False
    if not _player_activated:
        return False
    _player_session.tick(max(0.0, min(float(delta_time), 0.25)))
    _process_screen_ui_events(delta_time)
    _submit_screen_ui()
    _frame_count += 1
    if _frame_count == 1:
        print("INFERNUX_WEB_FIRST_FRAME_READY")
    return False


print("INFERNUX_WEB_PYTHON_READY version=3.13 runtime_stage=scene-prepared")
