"""
File-system CRUD operations for the Project panel.

Functions accept the required state as explicit parameters so they don't
depend on ``ProjectPanel`` internals.
"""

import json
import os
import re
import shutil

from Infernux.debug import Debug
from Infernux.engine.path_utils import is_path_within, path_key, resolved_path, same_path


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

SCRIPT_TEMPLATE = '''
from Infernux import *


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
  "friction": 0.4,
  "bounciness": 0.0,
  "friction_combine": 0,
  "bounce_combine": 0
}
'''

ANIMCLIP_TEMPLATE = '''{
  "name": "{clip_name}",
  "authoring_texture_guid": "",
  "frame_indices": [],
  "fps": 12.0,
  "loop": true
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
  "parameters": []
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
  "parameters": []
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


def _iter_asset_move_pairs(old_path: str, new_path: str):
    if os.path.isdir(old_path):
        for dirpath, _dirnames, filenames in os.walk(old_path):
            rel_dir = relative_path(dirpath, old_path, allow_root=True)
            mapped_dir = new_path if rel_dir == "." else os.path.join(new_path, rel_dir)
            for filename in filenames:
                yield os.path.join(dirpath, filename), os.path.join(mapped_dir, filename)
    elif os.path.isfile(old_path):
        yield old_path, new_path


def _update_build_settings_scene_path(old_path: str, new_path: str):
    """Replace *old_path* with *new_path* in BuildSettings.json scene list."""
    try:
        from .build_settings_panel import load_build_settings, save_build_settings
        settings = load_build_settings()
        scenes = settings.get("scenes", [])
        old_norm = path_key(old_path)
        changed = False
        for i, s in enumerate(scenes):
            if path_key(s) == old_norm:
                scenes[i] = resolved_path(new_path)
                changed = True
        if changed:
            settings["scenes"] = scenes
            save_build_settings(settings)
    except Exception as _exc:
        Debug.log(f"[BuildSettings] Failed to update scene path: {_exc}")


def _notify_asset_moved(old_path: str, new_path: str, asset_database=None):
    from Infernux.core.assets import AssetManager
    from . import asset_details_renderer

    if not AssetManager.move_asset(old_path, new_path, database=asset_database):
        raise RuntimeError(f"AssetDatabase failed to move '{old_path}' to '{new_path}'")

    # Update BuildSettings.json when a .scene file is renamed/moved
    if old_path.lower().endswith(".scene"):
        _update_build_settings_scene_path(old_path, new_path)

    asset_details_renderer.invalidate_asset(old_path)
    asset_details_renderer.invalidate_asset(new_path)

def move_path(old_path: str, new_path: str, asset_database=None):
    """Move or rename a file/directory to *new_path* and notify asset systems."""
    if not old_path or not new_path or not os.path.exists(old_path):
        return None

    old_abs = resolved_path(old_path)
    new_abs = resolved_path(new_path)
    if same_path(old_abs, new_abs):
        return new_abs

    if os.path.isdir(old_abs) and is_path_within(new_abs, old_abs):
        return None

    move_pairs = list(_iter_asset_move_pairs(old_abs, new_abs))
    try:
        shutil.move(old_abs, new_abs)
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None

    for old_file, new_file in move_pairs:
        _notify_asset_moved(old_file, new_file, asset_database)

    return new_abs


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

    content = SCRIPT_TEMPLATE.format(class_name=class_name)
    written, error = _write_new_text_asset(file_path, content)
    if not written:
        return False, error

    if asset_database:
        try:
            guid = _import_new_asset(file_path, asset_database)
            print(f"[ProjectPanel] Registered script: {script_name} -> {guid}")
        except Exception as exc:
            return False, str(exc)

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

def _detach_prefab_instances(prefab_path: str, asset_database=None):
    """Clear prefab linkage for live instances and return the changed count."""
    guid = ""
    if asset_database:
        try:
            guid = asset_database.get_guid_from_path(prefab_path)
        except Exception:
            pass
    if not guid:
        return 0

    from Infernux.lib import SceneManager
    scene = SceneManager.instance().get_active_scene()
    if scene is None:
        return 0

    detached = 0

    def _walk(objects):
        nonlocal detached
        for obj in objects:
            try:
                obj_guid = getattr(obj, 'prefab_guid', '')
                if obj_guid == guid:
                    obj.prefab_guid = ""
                    obj.prefab_root = False
                    detached += 1
                children = list(obj.get_children()) if hasattr(obj, 'get_children') else []
                _walk(children)
            except Exception:
                pass

    try:
        roots = list(scene.get_root_objects())
        _walk(roots)
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
    if detached:
        from Infernux.engine.scene_manager import SceneFileManager

        manager = SceneFileManager.instance()
        if manager is not None:
            manager.mark_dirty()
    return detached


def delete_item(item_path: str, asset_database=None):
    """Delete a file or directory from the filesystem and notify AssetDatabase."""
    if not item_path or not os.path.exists(item_path):
        return False

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

    # For .prefab files, detach all scene instances BEFORE deleting the asset.
    # This turns prefab instances into regular scene objects instead of leaving
    # them orphaned with a dangling prefab_guid.
    if not is_dir and item_path.lower().endswith('.prefab'):
        _detach_prefab_instances(item_path, asset_database)

    # Notify BEFORE removing the file — GUID is still resolvable at this point
    if not is_dir:
        from Infernux.core.assets import AssetManager
        if not AssetManager.delete_asset(
            item_path,
            database=asset_database,
            guid_hint=deleted_script_guid,
        ):
            raise RuntimeError(f"AssetDatabase failed to delete '{item_path}'")

    try:
        if is_dir:
            import shutil
            shutil.rmtree(item_path)
        else:
            # On Windows, transient locks (antivirus, indexer, font loader)
            # can prevent deletion.  Retry with increasing delays.
            import gc
            import time
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
    except OSError as _exc:
        Debug.log_warning(f"Delete failed: {type(_exc).__name__}: {_exc}")
        return False

    # Invalidate inspector cache so a recreated file won't reuse stale data
    from . import asset_details_renderer
    asset_details_renderer.invalidate_asset(item_path)
    return True


def do_rename(old_path: str, new_name: str, asset_database=None):
    """Rename a file or directory. Returns the new path on success, ``None`` on failure."""
    from .project_utils import update_material_name_in_file

    if not old_path or not new_name:
        return None

    safe_name = "".join([c for c in new_name if c.isalnum() or c in "._- "]).strip()
    if not safe_name:
        return None

    if os.path.isfile(old_path):
        _, ext = os.path.splitext(old_path)
        if ext and not safe_name.lower().endswith(ext.lower()):
            safe_name += ext

    new_path = os.path.join(os.path.dirname(old_path), safe_name)

    if old_path == new_path:
        return new_path  # Nothing to do

    _, ext = os.path.splitext(old_path)
    if ext.lower() == '.mat' and os.path.isfile(old_path):
        try:
            update_material_name_in_file(old_path, os.path.splitext(safe_name)[0])
        except OSError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return None

    if ext.lower() == '.py' and os.path.isfile(old_path):
        try:
            _sync_python_script_class_name_on_rename(old_path, new_path)
        except OSError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return None

    return move_path(old_path, new_path, asset_database)


def _sync_python_script_class_name_on_rename(old_path: str, new_path: str) -> None:
    """When renaming ``Foo.py`` → ``Bar.py``, also rename ``class Foo`` → ``class Bar``.

    Only rewrites when the file's primary class name matches the old stem, so
    hand-authored multi-class scripts are left untouched.
    """
    import re

    old_stem = os.path.splitext(os.path.basename(old_path))[0]
    new_stem = os.path.splitext(os.path.basename(new_path))[0]
    if not old_stem.isidentifier() or not new_stem.isidentifier() or old_stem == new_stem:
        return

    with open(old_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    def _pascal_case(stem: str) -> str:
        return "".join(
            part[:1].upper() + part[1:]
            for part in stem.split("_")
            if part
        )

    rename_pairs = {
        old_stem: new_stem,
        _pascal_case(old_stem): _pascal_case(new_stem),
    }
    candidates = []
    for old_class_name, new_class_name in rename_pairs.items():
        pattern = re.compile(
            rf"^class\s+{re.escape(old_class_name)}\b",
            re.MULTILINE,
        )
        matches = list(pattern.finditer(content))
        candidates.extend((pattern, new_class_name) for _match in matches)
    if len(candidates) != 1:
        return

    pattern, new_class_name = candidates[0]
    updated = pattern.sub(f"class {new_class_name}", content, count=1)
    if updated == content:
        return

    from Infernux.core.document_store import write_document_text
    write_document_text(old_path, updated)
