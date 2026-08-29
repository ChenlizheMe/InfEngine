"""Bootstrap contract between the browser host and the Infernux wasm runtime."""

from __future__ import annotations

from collections import deque
import hashlib
import importlib.util
import json
import marshal
import os
import sys
from types import CodeType, ModuleType
from typing import Any


_events: deque[tuple[str, dict[str, Any]]] = deque(maxlen=4096)
_seen_event_kinds: set[str] = set()
_frame_count = 0
_player_session: Any = None
_player_scene_manager: Any = None
_player_activated = False
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
            or str(summary.get("archive_sha256", "")).casefold()
            != str(package_record.get("archive_sha256", "")).casefold()
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
        or catalog.get("version") != 1
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
        if (
            len(payload) != int(entry.get("bytes", -1))
            or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
        ):
            raise RuntimeError(f"Web Player shader identity mismatch: {name} ({stage})")
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
    fields_module = importlib.import_module("Infernux.components.fields")
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
        "serialized_field": fields_module.serialized_field,
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
    package.Debug = debug_module.Debug
    package.__version__ = "0.4.0"
    package.__all__ = tuple(
        sorted(
            {
                "Debug",
                *screen_module.__all__,
                *getattr(math_module, "__all__", ()),
                *components.__all__,
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


def _prepare_player_runtime() -> None:
    global _player_session, _player_scene_manager

    if not _runtime_data_root:
        raise RuntimeError("Web Player has no extracted runtime data root")
    os.environ["_INFERNUX_PLAYER_MODE"] = "1"
    os.environ["_INFERNUX_PLAYER_DATA_ROOT"] = _runtime_data_root
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True

    import _Infernux as native_module

    _install_platform_runtime_api(native_module)
    from _InfernuxWebHost import initialize_runtime_assets
    from Infernux.engine.player_runtime import PlayerRuntimeSession
    from Infernux.engine.player_service_graph import (
        PlayerRuntimeAssetCatalog,
        RuntimeProductManifest,
    )
    from Infernux.engine.project_context import set_project_root
    from Infernux.engine.runtime_type_registry import install_runtime_type_registry
    from Infernux.lib import AssetRegistry, SceneManager

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
    session = PlayerRuntimeSession(asset_database=database)
    session.configure_runtime_contract(runtime_manifest, runtime_catalog)
    scene_path = runtime_catalog.resolve_scene(scenes[0])
    print(f"INFERNUX_WEB_SCENE_LOADING scene={scenes[0]}")
    if scene_path is None or not session.load_scene(scene_path):
        raise RuntimeError(
            "Web Player could not load its initial scene: "
            f"{session.last_scene_error or scenes[0]}"
        )
    scene_manager = SceneManager.instance()
    _install_runtime_lifecycle_bridge(scene_manager, session.execution_scheduler)
    active_scene = scene_manager.get_active_scene()
    if active_scene is None:
        raise RuntimeError("Web Player scene transaction published no active scene")
    _player_session = session
    _player_scene_manager = scene_manager
    print(
        "INFERNUX_WEB_SCENE_READY "
        f"assets={asset_count} types={type_count} "
        f"objects={len(active_scene.get_all_objects())} scene={scenes[0]}"
    )


def infernux_web_ready(details: dict[str, Any]) -> None:
    """Receive the browser graphics and viewport contract from the native host."""

    print(
        "INFERNUX_WEB_HOST_READY "
        f"python=3.13 graphics={details.get('graphics_api')} "
        f"viewport={details.get('width')}x{details.get('height')}"
    )
    if _player_session is None:
        _prepare_player_runtime()
    print("INFERNUX_WEB_USER_ACTIVATION_REQUIRED")


def infernux_web_activate(audio_ready: bool) -> bool:
    """Activate gameplay only after a trusted browser gesture unlocked audio."""

    global _player_activated
    if _player_activated:
        return True
    if not audio_ready:
        raise RuntimeError("Web Player audio did not unlock from the user gesture")
    if _player_session is None or not _player_session.activate():
        raise RuntimeError("Web Player runtime session could not be activated")
    _player_activated = True
    print("INFERNUX_WEB_RUNTIME_ACTIVE")
    return True


def infernux_web_input(kind: str, payload: dict[str, Any]) -> None:
    """Queue one normalized browser event for the engine input adapter."""

    _events.append((kind, payload))
    if kind not in _seen_event_kinds:
        _seen_event_kinds.add(kind)
        print(f"INFERNUX_WEB_INPUT_READY kind={kind}")


def infernux_web_drain_input() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return and clear pending events without exposing the mutable queue."""

    events = tuple(_events)
    _events.clear()
    return events


def infernux_web_tick(delta_time: float) -> None:
    """Advance the Python side once per browser animation frame."""

    global _frame_count
    if not _player_activated or _player_session is None:
        return
    _player_session.tick(max(0.0, min(float(delta_time), 0.25)))
    _frame_count += 1
    if _frame_count == 1:
        print("INFERNUX_WEB_FIRST_FRAME_READY")


print("INFERNUX_WEB_PYTHON_READY version=3.13 runtime_stage=scene-prepared")
