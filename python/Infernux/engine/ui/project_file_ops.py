"""
File-system CRUD operations for the Project panel.

Functions accept the required state as explicit parameters so they don't
depend on ``ProjectPanel`` internals.
"""

import json
import os
import re
import shutil
import time

from Infernux.debug import Debug
from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    relative_path,
    resolved_path,
    same_path,
)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

SCRIPT_TEMPLATE = '''
from Infernux import *
from Infernux.components import *


class {class_name}(InxComponent):
    # Public fields (automatically serialized and shown in Inspector)
    # speed = 5.0       # float (use .0 for decimals)
    
    def start(self):
        """Called before first update, after all awake() calls."""
    
    def update(self, delta_time: float):
        """Called every frame."""
'''

VERTEX_SHADER_TEMPLATE = '''#version 450

ShaderInfo {{
    Name "{shader_id}"
}}
'''

FRAGMENT_SHADER_TEMPLATE = '''#version 450

ShaderInfo {{
    Name "{shader_id}"
    ShadingModel "Unlit"
    Surface Opaque
    Queue 2000
    Properties {{
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
    }}
}}

void surface(out SurfaceData s) {{
    s = InitSurfaceData();
    vec4 texColor = texture(texSampler, v_TexCoord);
    s.albedo = texColor.rgb * v_Color * material.baseColor.rgb;
    s.alpha  = texColor.a * material.baseColor.a;
}}
'''

SCENE_TEMPLATE = '''{{
  "name": "{scene_name}",
  "isPlaying": false,
  "objects": []
}}
'''
MATERIAL_TEMPLATE = '''{{
  "name": "{material_name}",
  "builtin": false,
  "shaders": {{
    "vertex": {{
      "guid": "",
      "shader_id": "Standard",
      "path_hint": ""
    }},
    "fragment": {{
      "guid": "",
      "shader_id": "Unlit",
      "path_hint": ""
    }}
  }},
  "renderState": {{
    "cullMode": 1,
    "frontFace": 1,
    "polygonMode": 0,
    "lineWidth": 1.0,
    "depthBiasEnable": false,
    "depthBiasConstantFactor": 0.0,
    "depthBiasSlopeFactor": 0.0,
    "depthBiasClamp": 0.0,
    "topology": 3,
    "depthTestEnable": true,
    "depthWriteEnable": true,
    "depthCompareOp": 1,
    "blendEnable": false,
    "srcColorBlendFactor": 6,
    "dstColorBlendFactor": 7,
    "colorBlendOp": 0,
    "srcAlphaBlendFactor": 0,
    "dstAlphaBlendFactor": 1,
    "alphaBlendOp": 0,
    "alphaClipEnabled": false,
    "alphaClipThreshold": 0.5,
    "renderQueue": 2000,
    "stencilTestEnable": false
  }},
  "properties": {{
    "baseColor": {{
      "type": 3,
      "value": [1.0, 1.0, 1.0, 1.0]
    }}
  }}
}}
'''

PHYSIC_MATERIAL_TEMPLATE = '''{
  "friction": 0.6,
  "bounciness": 0.0,
  "friction_combine": 0,
  "bounce_combine": 0
}
'''

ANIMCLIP_TEMPLATE = '''{
  "name": "{clip_name}",
  "authoring_texture_guid": "",
  "authoring_texture_path": "",
  "frames": [],
  "fps": 12.0,
  "loop": true,
  "events": []
}
'''

ANIMCLIP3D_TEMPLATE = '''{
  "name": "{clip_name}",
  "source_model_guid": "",
  "source_model_path": "",
  "take_name": "",
  "bind_pose_bone_names": [],
  "duration_hint": 0.0,
  "events": []
}
'''

ANIMFSM_TEMPLATE = '''{
  "name": "{fsm_name}",
  "default_state": "",
  "mode": "2d",
  "states": [],
  "parameters": [],
  "entry_position": [-100.0, 50.0]
}
'''

ANIMTIMELINE_TEMPLATE = '''{
  "name": "{timeline_name}",
  "duration": 2.0,
  "apply_mode": "additive",
  "keyframes": []
}
'''

TIMELINEFSM_TEMPLATE = '''{
  "name": "{fsm_name}",
  "default_state": "",
  "mode": "timeline",
  "states": [],
  "parameters": [],
  "entry_position": [-100.0, 50.0]
}
'''


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def get_unique_name(current_path: str, base_name: str, extension: str = "") -> str:
    """Return a *base_name* that doesn't clash with existing entries in *current_path*.

    *extension* is considered when checking for conflicts but is NOT appended
    to the returned string.
    """
    name = base_name + extension
    full_path = os.path.join(current_path, name)
    full_path_no_ext = os.path.join(current_path, base_name)

    if not os.path.exists(full_path) and not os.path.exists(full_path_no_ext):
        return base_name

    counter = 1
    while True:
        candidate = f"{base_name}{counter}"
        name_with_ext = candidate + extension
        fp = os.path.join(current_path, name_with_ext)
        fp_ne = os.path.join(current_path, candidate)
        if not os.path.exists(fp) and not os.path.exists(fp_ne):
            return candidate
        counter += 1
        if counter > 999:
            break
    return f"{base_name}{counter}"


def plan_asset_paste(
    sources: list[str] | tuple[str, ...],
    destination_directory: str,
    *,
    cut: bool,
) -> list[tuple[str, str]]:
    """Plan exact non-conflicting paths for one atomic Project paste."""
    destination = resolved_path(destination_directory)
    if not destination or not os.path.isdir(destination):
        raise ValueError("asset paste destination must be an existing directory")

    candidates: list[str] = []
    seen: set[str] = set()
    for value in sources or ():
        source = resolved_path(str(value or ""))
        key = path_key(source)
        if not source or key in seen or not os.path.exists(source):
            continue
        seen.add(key)
        candidates.append(source)
    candidates.sort(key=lambda value: (len(path_key(value)), path_key(value)))

    roots: list[str] = []
    for candidate in candidates:
        if any(is_path_within(candidate, root, allow_root=True) for root in roots):
            continue
        roots.append(candidate)

    reserved: set[str] = set()
    result: list[tuple[str, str]] = []
    for source in roots:
        if os.path.isdir(source) and is_path_within(destination, source, allow_root=True):
            raise ValueError("cannot paste a directory into itself")

        name = os.path.basename(source)
        direct = os.path.join(destination, name)
        if cut and same_path(source, direct):
            continue

        if os.path.isdir(source):
            base, extension = name, ""
        else:
            base, extension = os.path.splitext(name)

        counter = 0
        while True:
            candidate_base = base if counter == 0 else f"{base}{counter}"
            candidate = os.path.join(destination, candidate_base + extension)
            candidate_without_extension = os.path.join(destination, candidate_base)
            candidate_key = path_key(candidate)
            base_key = path_key(candidate_without_extension)
            if (
                candidate_key not in reserved
                and base_key not in reserved
                and not os.path.exists(candidate)
                and not os.path.exists(candidate_without_extension)
            ):
                break
            counter += 1
            if counter > 9999:
                raise RuntimeError("could not allocate a unique asset paste path")
        reserved.add(candidate_key)
        reserved.add(base_key)
        result.append((source, resolved_path(candidate)))
    return result


def _iter_asset_move_pairs(old_path: str, new_path: str):
    if os.path.isdir(old_path):
        for dirpath, _dirnames, filenames in os.walk(old_path):
            rel_dir = relative_path(dirpath, old_path, allow_root=True)
            mapped_dir = new_path if rel_dir == "." else os.path.join(new_path, rel_dir)
            for filename in filenames:
                if filename.lower().endswith(".meta"):
                    continue
                yield os.path.join(dirpath, filename), os.path.join(mapped_dir, filename)
    elif os.path.isfile(old_path):
        if old_path.lower().endswith(".meta"):
            return
        yield old_path, new_path


def _update_build_settings_scene_path(old_path: str, new_path: str):
    """Project a scene asset move into the shared Project Settings document."""
    try:
        from Infernux.engine.interaction import ensure_project_settings_document
        from Infernux.engine.project_context import get_project_root

        root = get_project_root()
        if not root:
            return
        controller = ensure_project_settings_document(root)
        settings = controller.section("build")
        scenes = settings.get("scenes", [])
        old_norm = path_key(old_path)
        changed = False
        for i, s in enumerate(scenes):
            scene_path = s if os.path.isabs(s) else os.path.join(root, s)
            if path_key(scene_path) == old_norm:
                scenes[i] = relative_path(new_path, root)
                changed = True
        if changed:
            settings["scenes"] = scenes
            controller.apply_derived_section("build", settings)
    except Exception as _exc:
        Debug.log(f"[BuildSettings] Failed to update scene path: {_exc}")


def on_asset_mutation(change) -> None:
    """Project UI consequences registered with the global mutation service."""
    from Infernux.engine.interaction import AssetMutationKind, iter_asset_mutations

    from . import asset_details_renderer

    for mutation in iter_asset_mutations(change):
        old_path = mutation.source_path
        new_path = mutation.destination_path
        if (
            mutation.kind is AssetMutationKind.MOVED
            and new_path.lower().endswith(".scene")
        ):
            _update_build_settings_scene_path(old_path, new_path)
        asset_details_renderer.invalidate_asset(old_path)
        if new_path:
            asset_details_renderer.invalidate_asset(new_path)


def _notify_asset_moved(
    old_path: str,
    new_path: str,
    asset_database=None,
    *,
    origin="system",
    operation_id: str = "",
    publish_interaction: bool = True,
):
    from Infernux.core.assets import AssetManager

    result = AssetManager.move_asset(
        old_path,
        new_path,
        database=asset_database,
        origin=origin,
        operation_id=operation_id,
        publish_interaction=publish_interaction,
    )
    if not result:
        raise RuntimeError(f"AssetDatabase failed to move '{old_path}' to '{new_path}'")
    return result

def move_paths_batch(
    moves,
    asset_database=None,
    *,
    origin="system",
    operation_id: str = "",
    source_text_patches: dict[str, tuple[str, str]] | None = None,
):
    """Move multiple workspace roots through one editor/catalog transaction."""
    roots: list[tuple[str, str]] = []
    source_keys: set[str] = set()
    destination_keys: set[str] = set()
    for old_path, new_path in moves or ():
        if not old_path or not new_path or not os.path.exists(old_path):
            return None
        old_abs = resolved_path(old_path)
        new_abs = resolved_path(new_path)
        if same_path(old_abs, new_abs):
            continue
        old_key = path_key(old_abs)
        new_key = path_key(new_abs)
        if old_key in source_keys or new_key in destination_keys:
            raise ValueError("asset move batch contains duplicate sources or destinations")
        if os.path.exists(new_abs):
            return None
        if os.path.isdir(old_abs) and is_path_within(new_abs, old_abs):
            return None
        if any(
            is_path_within(old_abs, existing, allow_root=True)
            or is_path_within(existing, old_abs, allow_root=True)
            for existing, _destination in roots
        ):
            raise ValueError("asset move batch contains overlapping workspace roots")
        source_keys.add(old_key)
        destination_keys.add(new_key)
        roots.append((old_abs, new_abs))

    if not roots:
        return tuple()

    move_pairs = [
        pair
        for old_root, new_root in roots
        for pair in _iter_asset_move_pairs(old_root, new_root)
    ]
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import AssetMutationService

    mutations = AssetMutationService.instance()
    try:
        database = AssetManager._mutation_database(asset_database)
    except RuntimeError:
        if mutations is not None:
            mutations = None
        database = asset_database
    relocation_entries = tuple(
        (
            old_file,
            new_file,
            database.get_guid_from_path(old_file) if database is not None else "",
        )
        for old_file, new_file in move_pairs
    )
    plan = None
    if mutations is not None and move_pairs:
        plan = mutations.prepare_relocation(
            relocation_entries,
            origin=origin,
            operation_id=operation_id,
        )
    try:
        from Infernux.engine.interaction.asset_content import (
            AssetReferenceRelocationPlanner,
        )
        from Infernux.engine.project_context import get_project_root

        project_root = str(getattr(database, "project_root", "") or get_project_root() or "")
        reference_patches = AssetReferenceRelocationPlanner.build_patches(
            relocation_entries,
            database=database,
            project_root=project_root,
            source_text_patches=source_text_patches,
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if plan is not None:
            mutations.abort_relocation(plan)
        Debug.log_error(f"Asset reference relocation preflight failed: {exc}")
        return None
    workspace_moves: list[tuple[str, str]] = []
    patched_sources: list[tuple[str, tuple[str, str]]] = []
    patched_references = []
    database_moves: list[tuple[str, str]] = []
    try:
        if source_text_patches:
            from Infernux.core.document_store import write_document_text

            patches_by_key = {
                path_key(path): patch for path, patch in source_text_patches.items()
            }
            for old_abs, _new_abs in roots:
                patch = patches_by_key.get(path_key(old_abs))
                if patch is None:
                    continue
                write_document_text(old_abs, patch[1])
                patched_sources.append((old_abs, patch))

        for old_abs, new_abs in roots:
            os.makedirs(os.path.dirname(new_abs), exist_ok=True)
            shutil.move(old_abs, new_abs)
            workspace_moves.append((old_abs, new_abs))

        use_native_batch = (
            plan is not None
            and hasattr(database, "move_assets_batch")
            and all(mutation.guid for mutation in plan.mutations)
        )
        if use_native_batch:
            results = AssetManager.move_assets_batch(move_pairs, database=database)
            if len(results) != len(move_pairs) or any(not result for result in results):
                message = next(
                    (str(getattr(result, "error", "") or "") for result in results if not result),
                    "native asset relocation batch failed",
                )
                raise RuntimeError(message)
            database_moves.extend(move_pairs)
        else:
            for old_file, new_file in move_pairs:
                if plan is None and origin == "system" and not operation_id:
                    _notify_asset_moved(old_file, new_file, database)
                else:
                    _notify_asset_moved(
                        old_file,
                        new_file,
                        database,
                        origin=origin,
                        operation_id=(plan.operation_id if plan is not None else operation_id),
                        publish_interaction=plan is None,
                    )
                database_moves.append((old_file, new_file))
        if reference_patches:
            from Infernux.core.document_store import write_document_text

            for patch in reference_patches:
                AssetManager._suppress_watcher_echo("modified", patch.destination_path)
                write_document_text(patch.destination_path, patch.updated)
                patched_references.append(patch)
            for patch in reference_patches:
                result = AssetManager.reimport_asset(
                    patch.destination_path,
                    database=database,
                )
                if not result:
                    message = str(getattr(result, "error", "") or "asset reimport failed")
                    raise RuntimeError(
                        f"asset reference migration failed for '{patch.destination_path}': {message}"
                    )
        if plan is not None:
            mutations.commit_relocation(plan)
    except (OSError, RuntimeError, ValueError) as _exc:
        for old_abs, new_abs in reversed(workspace_moves):
            try:
                shutil.move(new_abs, old_abs)
            except OSError as rollback_exc:
                Debug.log_error(
                    f"Asset workspace rollback failed for '{new_abs}' -> '{old_abs}': {rollback_exc}"
                )
        for patch in reversed(patched_references):
            if not os.path.isfile(patch.source_path):
                continue
            try:
                from Infernux.core.document_store import write_document_text

                write_document_text(patch.source_path, patch.original)
            except OSError as rollback_exc:
                Debug.log_error(
                    f"Asset reference rollback failed for '{patch.source_path}': {rollback_exc}"
                )
        for old_abs, patch in reversed(patched_sources):
            if not os.path.isfile(old_abs):
                continue
            try:
                from Infernux.core.document_store import write_document_text

                write_document_text(old_abs, patch[0])
            except OSError as rollback_exc:
                Debug.log_error(f"Asset content rollback failed for '{old_abs}': {rollback_exc}")
        for old_file, new_file in reversed(database_moves):
            try:
                _notify_asset_moved(
                    new_file,
                    old_file,
                    database,
                    origin="system",
                    operation_id=(plan.operation_id if plan is not None else operation_id),
                    publish_interaction=False,
                )
            except Exception as rollback_exc:
                Debug.log_error(
                    f"Asset catalog rollback failed for '{new_file}' -> '{old_file}': {rollback_exc}"
                )
        if plan is not None:
            mutations.abort_relocation(plan)
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None

    return tuple(destination for _source, destination in roots)


def move_path(
    old_path: str,
    new_path: str,
    asset_database=None,
    *,
    origin="system",
    operation_id: str = "",
    source_text_patch: tuple[str, str] | None = None,
):
    """Move or rename one workspace root through the global transaction."""
    result = move_paths_batch(
        ((old_path, new_path),),
        asset_database,
        origin=origin,
        operation_id=operation_id,
        source_text_patches=(
            {old_path: source_text_patch} if source_text_patch is not None else None
        ),
    )
    if result is None:
        return None
    if not result:
        return resolved_path(new_path) if same_path(old_path, new_path) else None
    return result[0]


def move_item_to_directory(item_path: str, dest_dir: str, asset_database=None):
    """Move *item_path* into *dest_dir*, generating a unique name on conflicts."""
    if not item_path or not dest_dir or not os.path.exists(item_path) or not os.path.isdir(dest_dir):
        return None

    item_abs = resolved_path(item_path)
    dest_abs = resolved_path(dest_dir)

    name = os.path.basename(item_abs)
    new_path = os.path.join(dest_abs, name)
    if os.path.exists(new_path) and not same_path(new_path, item_abs):
        base, ext = os.path.splitext(name)
        if os.path.isdir(item_abs):
            base = name
            ext = ""
        unique_name = get_unique_name(dest_abs, base, ext)
        new_path = os.path.join(dest_abs, unique_name + ext)

    return move_path(item_abs, new_path, asset_database)


def _copy_file_as_new_asset(source: str, destination: str) -> None:
    """Copy one source file while regenerating asset-owned identities."""
    if source.lower().endswith(".particlegraph"):
        from dataclasses import replace
        import uuid

        from Infernux.particle.asset import ParticleGraphAsset

        graph = ParticleGraphAsset.load(source)
        replace(
            graph,
            stable_id=uuid.uuid4().hex,
            name=os.path.splitext(os.path.basename(destination))[0],
        ).save(destination)
        return
    shutil.copy2(source, destination)


def copy_path_as_new_asset(source: str, destination: str, asset_database=None):
    """Copy a file or directory as a distinct asset and import the result.

    Sidecar metadata is never copied. ParticleGraph owns an additional AOT
    identity beyond its AssetDatabase GUID, so that identity is regenerated
    while emitter, parameter, event, and node stable IDs remain intact.
    """
    if not source or not destination or not os.path.exists(source):
        return None

    source_abs = resolved_path(source)
    destination_abs = resolved_path(destination)
    if same_path(source_abs, destination_abs) or os.path.exists(destination_abs):
        return None
    if os.path.isdir(source_abs) and is_path_within(destination_abs, source_abs):
        return None

    copied_files: list[str] = []
    try:
        if os.path.isdir(source_abs):
            os.makedirs(destination_abs)
            for directory, dirnames, filenames in os.walk(source_abs):
                dirnames[:] = [
                    name for name in dirnames if not name.lower().endswith(".meta")
                ]
                relative = relative_path(directory, source_abs, allow_root=True)
                target_directory = (
                    destination_abs
                    if relative == "."
                    else os.path.join(destination_abs, relative)
                )
                os.makedirs(target_directory, exist_ok=True)
                for filename in filenames:
                    if filename.lower().endswith(".meta"):
                        continue
                    target = os.path.join(target_directory, filename)
                    _copy_file_as_new_asset(os.path.join(directory, filename), target)
                    copied_files.append(target)
        else:
            if source_abs.lower().endswith(".meta"):
                return None
            os.makedirs(os.path.dirname(destination_abs), exist_ok=True)
            _copy_file_as_new_asset(source_abs, destination_abs)
            copied_files.append(destination_abs)
    except Exception:
        if os.path.isdir(destination_abs):
            shutil.rmtree(destination_abs, ignore_errors=True)
        elif os.path.exists(destination_abs):
            try:
                os.remove(destination_abs)
            except OSError:
                pass
        raise

    if asset_database:
        for copied_file in copied_files:
            _import_new_asset(copied_file, asset_database)
    return destination_abs


# ---------------------------------------------------------------------------
# Create operations
# ---------------------------------------------------------------------------

def _write_new_text_asset(path: str, content: str) -> tuple[bool, str]:
    try:
        from Infernux.core.document_store import write_document_text
        write_document_text(path, content)
        return True, ""
    except (OSError, RuntimeError) as exc:
        return False, str(exc)


def _import_new_asset(path: str, asset_database) -> str:
    from Infernux.core.assets import AssetManager

    return AssetManager.import_asset(path, database=asset_database).guid

def create_folder(current_path: str, folder_name: str):
    """Create a folder and return ``(True, "")`` or ``(False, error_msg)``."""
    if not folder_name or not current_path:
        return False, "Invalid folder name"

    folder_name = folder_name.strip()
    if not folder_name:
        return False, "Folder name cannot be empty"

    new_path = os.path.join(current_path, folder_name)
    if os.path.exists(new_path):
        return False, f"'{folder_name}' already exists"

    try:
        os.makedirs(new_path)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def create_script(current_path: str, script_name: str, asset_database=None):
    """Create a Python script from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not script_name or not current_path:
        return False, "Invalid script name"

    script_name = script_name.strip()
    if not script_name:
        return False, "Script name cannot be empty"

    class_name = script_name
    if class_name.endswith('.py'):
        class_name = class_name[:-3]

    if not class_name.isidentifier():
        return False, "Invalid script name (must be valid Python identifier)"

    if not script_name.endswith('.py'):
        script_name = script_name + '.py'

    file_path = os.path.join(current_path, script_name)
    if os.path.exists(file_path):
        return False, f"'{script_name}' already exists"

    started = time.perf_counter()
    marks: list[tuple[str, float]] = []

    def mark(label: str) -> None:
        marks.append((label, time.perf_counter()))

    content = SCRIPT_TEMPLATE.format(class_name=class_name)
    written, error = _write_new_text_asset(file_path, content)
    mark("write")
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            mark("import")
            Debug.log_internal(
                f"[ProjectPanel] Registered script: {script_name} -> {guid}"
            )
        except Exception as exc:
            return False, str(exc)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= 10.0:
        previous = started
        pieces = []
        for label, current in marks:
            pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
            previous = current
        Debug.log_internal(
            f"[ScriptAssetProfile] create={elapsed_ms:.2f}ms "
            f"file={script_name} " + " ".join(pieces)
        )

    return True, ""


def create_shader(current_path: str, shader_name: str, shader_type: str,
                   asset_database=None):
    """Create a shader file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not shader_name or not current_path:
        return False, "Invalid shader name"

    shader_name = shader_name.strip()
    if not shader_name:
        return False, "Shader name cannot be empty"

    shader_type = str(shader_type).strip().lower()
    if shader_type not in {"vert", "frag"}:
        return False, (
            "Shader type must be 'vert' or 'frag'. Compute shaders are not supported; "
            "use an external parallel backend."
        )

    for ext in ['.vert', '.frag']:
        if shader_name.endswith(ext):
            shader_name = shader_name[:-len(ext)]
            break

    # Shader ids follow the "Title Case With Spaces" convention (e.g. "My
    # Shader"), regardless of how the file name was typed.
    words = re.split(r'[\s_\-]+', shader_name)
    shader_id = ' '.join(w[:1].upper() + w[1:] for w in words if w)
    extension = f'.{shader_type}'
    file_name = shader_name + extension
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    if shader_type == 'vert':
        content = VERTEX_SHADER_TEMPLATE.format(shader_id=shader_id)
    else:
        content = FRAGMENT_SHADER_TEMPLATE.format(shader_id=shader_id)

    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered shader: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, ""


def create_scene(current_path: str, scene_name: str, asset_database=None):
    """Create a ``.scene`` file from template. Returns ``(True, path)`` or ``(False, error_msg)``."""
    if not scene_name or not current_path:
        return False, "Invalid scene name"

    scene_name = scene_name.strip()
    if not scene_name:
        return False, "Scene name cannot be empty"

    if scene_name.endswith('.scene'):
        scene_name = scene_name[:-6]

    file_name = scene_name + '.scene'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = SCENE_TEMPLATE.format(scene_name=scene_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered scene: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, file_path


def create_material(current_path: str, material_name: str, asset_database=None):
    """Create a ``.mat`` file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not material_name or not current_path:
        return False, "Invalid material name"

    material_name = material_name.strip()
    if not material_name:
        return False, "Material name cannot be empty"

    if material_name.endswith('.mat'):
        material_name = material_name[:-4]

    file_name = material_name + '.mat'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = MATERIAL_TEMPLATE.format(material_name=material_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered material: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    # Publish the exact document already in memory. The Project panel can show
    # this preview before filesystem polling or a later material save completes.
    from Infernux.core.assets import AssetManager
    AssetManager._prime_material_preview(file_path, content)

    return True, ""


def create_physic_material(current_path: str, material_name: str, asset_database=None):
    """Create and import a strict ``.physicMaterial`` asset."""
    if not current_path or not material_name:
        return False, "Invalid PhysicMaterial name"
    material_name = material_name.strip()
    if not material_name:
        return False, "PhysicMaterial name cannot be empty"
    extension = ".physicMaterial"
    if material_name.lower().endswith(extension.lower()):
        material_name = material_name[:-len(extension)]
    file_name = material_name + extension
    file_path = os.path.join(current_path, file_name)
    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    written, error = _write_new_text_asset(file_path, PHYSIC_MATERIAL_TEMPLATE)
    if not written:
        return False, error
    if asset_database:
        guid = _import_new_asset(file_path, asset_database)
        if not guid:
            return False, f"AssetDatabase failed to import '{file_name}'"
    return True, ""


def create_prefab_from_gameobject(game_object, current_path: str,
                                  asset_database=None,
                                  source_canvas_name: str = ""):
    """Save a GameObject hierarchy as a ``.prefab`` file.

    Returns ``(True, file_path)`` or ``(False, error_msg)``.
    """
    if game_object is None or not current_path:
        return False, "Invalid parameters"

    from Infernux.engine.prefab_manager import (
        PREFAB_EXTENSION,
        _link_created_prefab_source,
        save_prefab,
    )

    prefab_name = get_unique_name(current_path, game_object.name, PREFAB_EXTENSION)
    file_path = os.path.join(current_path, prefab_name + PREFAB_EXTENSION)

    if save_prefab(game_object, file_path, asset_database=asset_database,
                   source_canvas_name=source_canvas_name):
        if not _link_created_prefab_source(game_object, file_path, asset_database):
            return False, "Prefab asset was saved, but its source hierarchy could not be linked"
        return True, file_path
    return False, "Failed to save prefab"


def create_animclip(current_path: str, clip_name: str, asset_database=None):
    """Create a ``.animclip2d`` file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not clip_name or not current_path:
        return False, "Invalid animation clip name"

    clip_name = clip_name.strip()
    if not clip_name:
        return False, "Animation clip name cannot be empty"

    if clip_name.endswith('.animclip2d'):
        clip_name = clip_name[:-11]

    file_name = clip_name + '.animclip2d'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = ANIMCLIP_TEMPLATE.format(clip_name=clip_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered animclip2d: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, ""


def create_animclip3d(current_path: str, clip_name: str, asset_database=None):
    """Create a ``.animclip3d`` file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not clip_name or not current_path:
        return False, "Invalid 3D animation clip name"

    clip_name = clip_name.strip()
    if not clip_name:
        return False, "3D animation clip name cannot be empty"

    if clip_name.endswith('.animclip3d'):
        clip_name = clip_name[:-11]

    file_name = clip_name + '.animclip3d'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = ANIMCLIP3D_TEMPLATE.format(clip_name=clip_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered animclip3d: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, ""


def create_animfsm(current_path: str, fsm_name: str, asset_database=None):
    """Create a ``.animfsm`` file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not fsm_name or not current_path:
        return False, "Invalid state machine name"

    fsm_name = fsm_name.strip()
    if not fsm_name:
        return False, "State machine name cannot be empty"

    if fsm_name.endswith('.animfsm'):
        fsm_name = fsm_name[:-8]

    file_name = fsm_name + '.animfsm'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = ANIMFSM_TEMPLATE.format(fsm_name=fsm_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered animfsm: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, ""


def create_particlegraph(current_path: str, graph_name: str, asset_database=None):
    """Create and AOT-compile a strict ``.particlegraph`` authoring asset."""
    if not graph_name or not current_path:
        return False, "Invalid Particle Graph name"

    graph_name = graph_name.strip()
    if not graph_name:
        return False, "Particle Graph name cannot be empty"
    if graph_name.lower().endswith(".particlegraph"):
        graph_name = graph_name[: -len(".particlegraph")]

    file_name = graph_name + ".particlegraph"
    file_path = os.path.join(current_path, file_name)
    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    from Infernux.particle.asset import ParticleGraphAsset

    try:
        ParticleGraphAsset(name=graph_name).save(file_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return False, str(exc)

    if asset_database:
        try:
            _import_new_asset(file_path, asset_database)
        except Exception as exc:
            return False, str(exc)
    return True, ""


def create_render_effect(
    current_path: str,
    effect_name: str,
    feature_type: str,
    asset_database=None,
):
    """Create and import one strict reusable ``.effect`` source asset."""
    if not current_path or not effect_name:
        return False, "Invalid Render Effect name"
    effect_name = effect_name.strip()
    if not effect_name:
        return False, "Render Effect name cannot be empty"
    if effect_name.lower().endswith(".effect"):
        effect_name = effect_name[:-len(".effect")]

    from Infernux.renderstack.render_effect_asset import (
        RenderEffectAsset,
        dump_render_effect_document,
    )
    from Infernux.renderstack.render_effect_compiler import get_render_effect_feature

    try:
        get_render_effect_feature(feature_type)
        content = dump_render_effect_document(RenderEffectAsset(feature_type=feature_type))
    except (TypeError, ValueError) as exc:
        return False, str(exc)

    file_name = effect_name + ".effect"
    file_path = os.path.join(current_path, file_name)
    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error
    if asset_database:
        try:
            _import_new_asset(file_path, asset_database)
        except Exception as exc:
            return False, str(exc)
    return True, ""


def create_render_effect_group(current_path: str, group_name: str, asset_database=None):
    """Create and import one empty strict ``.effectgroup`` source asset."""
    if not current_path or not group_name:
        return False, "Invalid Render Effect Group name"
    group_name = group_name.strip()
    if not group_name:
        return False, "Render Effect Group name cannot be empty"
    if group_name.lower().endswith(".effectgroup"):
        group_name = group_name[:-len(".effectgroup")]

    from Infernux.renderstack.render_effect_asset import (
        RenderEffectGroupAsset,
        dump_render_effect_document,
    )

    file_name = group_name + ".effectgroup"
    file_path = os.path.join(current_path, file_name)
    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"
    content = dump_render_effect_document(RenderEffectGroupAsset())
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error
    if asset_database:
        try:
            _import_new_asset(file_path, asset_database)
        except Exception as exc:
            return False, str(exc)
    return True, ""


def create_animtimeline(current_path: str, timeline_name: str, asset_database=None):
    """Create a ``.animtimeline`` file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not timeline_name or not current_path:
        return False, "Invalid timeline name"

    timeline_name = timeline_name.strip()
    if not timeline_name:
        return False, "Timeline name cannot be empty"

    if timeline_name.endswith('.animtimeline'):
        timeline_name = timeline_name[:-13]

    file_name = timeline_name + '.animtimeline'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = ANIMTIMELINE_TEMPLATE.format(timeline_name=timeline_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered animtimeline: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, ""


def create_timelinefsm(current_path: str, fsm_name: str, asset_database=None):
    """Create a ``.timelinefsm`` file from template. Returns ``(True, "")`` or ``(False, error_msg)``."""
    if not fsm_name or not current_path:
        return False, "Invalid timeline FSM name"

    fsm_name = fsm_name.strip()
    if not fsm_name:
        return False, "Timeline FSM name cannot be empty"

    if fsm_name.endswith('.timelinefsm'):
        fsm_name = fsm_name[:-12]

    file_name = fsm_name + '.timelinefsm'
    file_path = os.path.join(current_path, file_name)

    if os.path.exists(file_path):
        return False, f"'{file_name}' already exists"

    content = TIMELINEFSM_TEMPLATE.format(fsm_name=fsm_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered timelinefsm: {file_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

    return True, ""


# ---------------------------------------------------------------------------
# Delete & Rename
# ---------------------------------------------------------------------------

def delete_item(item_path: str, asset_database=None):
    """Delete a file or directory from the filesystem and notify AssetDatabase."""
    if not item_path or not os.path.exists(item_path):
        return False

    started = time.perf_counter()
    marks: list[tuple[str, float]] = []

    def mark(label: str) -> None:
        marks.append((label, time.perf_counter()))

    is_dir = os.path.isdir(item_path)
    deleted_script_guid = ""
    if is_dir or item_path.lower().endswith('.py'):
        from Infernux.components.script_loader import clear_deleted_script_errors
        clear_deleted_script_errors(item_path)
    if not is_dir and item_path.lower().endswith('.py'):
        if asset_database is not None:
            deleted_script_guid = str(asset_database.get_guid_from_path(item_path) or "").strip()
        if not deleted_script_guid:
            from Infernux.core.asset_types import read_meta_guid
            deleted_script_guid = read_meta_guid(item_path)

    # Notify BEFORE removing the file — GUID is still resolvable at this point
    if not is_dir:
        from Infernux.core.assets import AssetManager
        if not AssetManager.delete_asset(
            item_path,
            database=asset_database,
            guid_hint=deleted_script_guid,
        ):
            raise RuntimeError(f"AssetDatabase failed to delete '{item_path}'")
        mark("asset_database")

    try:
        if is_dir:
            import shutil
            shutil.rmtree(item_path)
        else:
            # On Windows, transient locks (antivirus, indexer, font loader)
            # can prevent deletion.  Retry with increasing delays.
            import gc
            last_exc = None
            for _attempt in range(5):
                try:
                    os.remove(item_path)
                    last_exc = None
                    break
                except PermissionError as pe:
                    last_exc = pe
                    gc.collect()
                    time.sleep(0.15 * (_attempt + 1))
            if last_exc is not None:
                Debug.log_warning(
                    f"Cannot delete '{os.path.basename(item_path)}': "
                    f"file may be in use by another process. ({last_exc})"
                )
                return False
        mark("filesystem")
    except OSError as _exc:
        Debug.log_warning(f"Delete failed: {type(_exc).__name__}: {_exc}")
        return False

    # Invalidate inspector cache so a recreated file won't reuse stale data
    from . import asset_details_renderer
    asset_details_renderer.invalidate_asset(item_path)
    mark("ui_invalidate")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= 10.0:
        previous = started
        pieces = []
        for label, current in marks:
            pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
            previous = current
        Debug.log_internal(
            f"[AssetMutationProfile] delete={elapsed_ms:.2f}ms "
            f"file={os.path.basename(item_path)} " + " ".join(pieces)
        )
    return True


def rename_destination(old_path: str, new_name: str) -> str:
    """Return the canonical destination used by an editor rename intent."""
    if not old_path or not new_name:
        return ""
    safe_name = "".join(
        c for c in new_name if c.isalnum() or c in "._- "
    ).strip()
    if not safe_name:
        return ""
    if os.path.isfile(old_path):
        _, extension = os.path.splitext(old_path)
        if extension and not safe_name.lower().endswith(extension.lower()):
            safe_name += extension
    return os.path.join(os.path.dirname(old_path), safe_name)


def do_rename(
    old_path: str,
    new_name: str,
    asset_database=None,
    *,
    origin="system",
    operation_id: str = "",
):
    """Rename a file or directory. Returns the new path on success, ``None`` on failure."""
    new_path = rename_destination(old_path, new_name)
    if not new_path:
        return None
    safe_name = os.path.basename(new_path)

    if old_path == new_path:
        return new_path  # Nothing to do

    try:
        source_text_patch = _build_rename_content_patch(old_path, new_path)
    except (OSError, ValueError, json.JSONDecodeError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None

    return move_path(
        old_path,
        new_path,
        asset_database,
        origin=origin,
        operation_id=operation_id,
        source_text_patch=source_text_patch,
    )


def _build_rename_content_patch(old_path: str, new_path: str) -> tuple[str, str] | None:
    """Build a reversible patch through the shared asset-content registry."""
    from Infernux.engine.interaction import AssetRenameContentRegistry

    return AssetRenameContentRegistry.instance().build_patch(old_path, new_path)


def _renamed_python_script_text(content: str, old_path: str, new_path: str) -> str:
    """Compatibility helper backed by the shared rename-content registry."""
    from Infernux.engine.interaction.asset_content import _rename_single_component_class

    return _rename_single_component_class(content, old_path, new_path)


def _sync_python_script_class_name_on_rename(old_path: str, new_path: str) -> None:
    """When renaming ``Foo.py`` → ``Bar.py``, also rename ``class Foo`` → ``class Bar``.

    Only rewrites when the file's primary class name matches the old stem, so
    hand-authored multi-class scripts are left untouched.
    """
    with open(old_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    updated = _renamed_python_script_text(content, old_path, new_path)
    if updated == content:
        return

    from Infernux.core.document_store import write_document_text
    write_document_text(old_path, updated)
