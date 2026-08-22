"""Convenient top-level runtime API for game scripts.

This module intentionally re-exports the most common scripting symbols so
user scripts can simply do ``from Infernux import *``.
"""

import importlib

# ── Runtime API (used by game scripts) ─────────────────────────────
from Infernux.engine import release_engine, run_headless, Engine, LogLevel
from Infernux.application import Application
from Infernux.acceptance import RuntimeAcceptance, RuntimeAcceptanceManifest, RuntimeAcceptanceTest
from Infernux.math import Vector2, Vector3, vec4f, quatf, vector2, vector3, vector4, quaternion
from Infernux import components as _components_module
from Infernux.components import serialized_field
from Infernux.components import *
from Infernux import core
from Infernux.core import *
from Infernux.lib import GameObject, Transform, Component, Space, PrimitiveType
from Infernux.debug import Debug, debug, log, log_warning, log_error, log_exception
from Infernux import scene
from Infernux.scene import GameObjectQuery, LayerMask, SceneManager
from Infernux.timing import Time
from Infernux.mathf import Mathf
from Infernux.coroutine import (
    Coroutine,
    WaitForSeconds,
    WaitForSecondsRealtime,
    WaitForEndOfFrame,
    WaitForFrames,
    WaitForFixedUpdate,
    WaitUntil,
    WaitWhile,
)
from Infernux.batch import batch_read, batch_write, create_batch_handle
from Infernux.instantiate import Instantiate, Destroy


def __getattr__(name: str):
    """Lazily expose optional JIT helpers without bloating ``from Infernux import *``.

    This keeps Numba out of ordinary star-import paths while still supporting:

        from Infernux import njit
        from Infernux import jit
    """
    if name == "jit":
        return importlib.import_module("Infernux.jit")
    if name in {
        "njit", "warmup", "ensure_jit_runtime",
        "JIT_AVAILABLE",
    }:
        jit_module = importlib.import_module("Infernux.jit")
        return getattr(jit_module, name)
    if name in {"input", "rendergraph", "renderstack", "ui"}:
        module = importlib.import_module(f"Infernux.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module 'Infernux' has no attribute {name!r}")


# ── Public API surface for ``from Infernux import *`` ──────────────
# Curated list: only symbols commonly needed in game scripts.
# Internal / advanced helpers stay accessible via their submodules
# (e.g. ``from Infernux.debug import debug``).
__all__ = [
    # Engine
    "Engine",
    "Application",
    "RuntimeAcceptance",
    "RuntimeAcceptanceManifest",
    "RuntimeAcceptanceTest",
    "LogLevel",
    "release_engine",
    "run_headless",
    # Math
    "Vector2",
    "Vector3",
    "vec4f",
    "quatf",
    "vector2",
    "vector3",
    "vector4",
    "quaternion",
    # Game Objects
    "GameObject",
    "Transform",
    "Component",
    "Space",
    "PrimitiveType",
    # Components — user-facing
    "InxComponent",
    "serialized_field",
    "int_field",
    "list_field",
    "component_field",
    "component_list_field",
    "hide_field",
    "InspectorSpace",
    "FieldType",
    "Color",
    "GameObjectRef",
    "MaterialRef",
    "ComponentRef",
    "PrefabRef",
    "SerializableObject",
    # Builtin components
    "Light",
    "MeshRenderer",
    "SkinnedMeshRenderer",
    "Camera",
    "Collider",
    "BoxCollider",
    "SphereCollider",
    "CapsuleCollider",
    "CylinderCollider",
    "MeshCollider",
    "Rigidbody",
    "RigidbodyConstraints",
    "CollisionDetectionMode",
    "RigidbodyInterpolation",
    "AudioSource",
    "AudioListener",
    "SpriteRenderer",
    "SpiritAnimator",
    "SkeletalAnimator",
    "RuntimeAcceptanceRunner",
    # Decorators
    "require_component",
    "disallow_multiple",
    "execute_in_edit_mode",
    "add_component_menu",
    "icon",
    "help_url",
    "RequireComponent",
    "DisallowMultipleComponent",
    "ExecuteInEditMode",
    "AddComponentMenu",
    "HelpURL",
    "Icon",
    # Core assets
    "Material",
    "Texture",
    "Shader",
    "AudioClip",
    "AnimationClip",
    "AnimationFrame",
    "AnimStateMachine",
    "AnimState",
    "AnimTransition",
    "AnimCondition",
    "AnimParameter",
    "AssetManager",
    "TextureRef",
    "ShaderRef",
    "AudioClipRef",
    "AnimationClipRef",
    "AnimStateMachineRef",
    "RenderEffectRef",
    # Debug — class only (use Debug.log / Debug.log_warning / …)
    "Debug",
    # Submodules
    "core",
    "rendergraph",
    "renderstack",
    "scene",
    "input",
    "ui",
    # Scene
    "GameObjectQuery",
    "LayerMask",
    "SceneManager",
    # Timing & math utilities
    "Time",
    "Mathf",
    # Coroutines
    "Coroutine",
    "WaitForSeconds",
    "WaitForSecondsRealtime",
    "WaitForEndOfFrame",
    "WaitForFrames",
    "WaitForFixedUpdate",
    "WaitUntil",
    "WaitWhile",
    # Batch processing
    "batch_read",
    "batch_write",
    # Object lifecycle
    "Instantiate",
    "Destroy",
]
