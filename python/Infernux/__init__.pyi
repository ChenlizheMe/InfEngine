from __future__ import annotations

# Engine
from Infernux.engine import release_engine as release_engine
from Infernux.engine import run_headless as run_headless
from Infernux.engine import Engine as Engine
from Infernux.engine import LogLevel as LogLevel
from Infernux.application import Application as Application
# Math
from Infernux.math import Vector2 as Vector2
from Infernux.math import Vector3 as Vector3
from Infernux.math import vec4f as vec4f
from Infernux.math import quatf as quatf
from Infernux.math import vector2 as vector2
from Infernux.math import vector3 as vector3
from Infernux.math import vector4 as vector4
from Infernux.math import quaternion as quaternion
# Game Objects
from Infernux.lib import GameObject as GameObject
from Infernux.lib import Transform as Transform
from Infernux.lib import Component as Component
from Infernux.lib import Space as Space
from Infernux.lib import PrimitiveType as PrimitiveType
# Components — user-facing
from Infernux.components import InxComponent as InxComponent
from Infernux.components import serialized_field as serialized_field
from Infernux.components import int_field as int_field
from Infernux.components import list_field as list_field
from Infernux.components import component_field as component_field
from Infernux.components import component_list_field as component_list_field
from Infernux.components import hide_field as hide_field
from Infernux.components import FieldType as FieldType
from Infernux.components import GameObjectRef as GameObjectRef
from Infernux.components import MaterialRef as MaterialRef
from Infernux.components import ComponentRef as ComponentRef
from Infernux.components import PrefabRef as PrefabRef
from Infernux.components import SerializableObject as SerializableObject
# Builtin components
from Infernux.components import Light as Light
from Infernux.components import MeshRenderer as MeshRenderer
from Infernux.components import SkinnedMeshRenderer as SkinnedMeshRenderer
from Infernux.components import Camera as Camera
from Infernux.components import Collider as Collider
from Infernux.components import BoxCollider as BoxCollider
from Infernux.components import SphereCollider as SphereCollider
from Infernux.components import CapsuleCollider as CapsuleCollider
from Infernux.components import MeshCollider as MeshCollider
from Infernux.components import Rigidbody as Rigidbody
from Infernux.components import RigidbodyConstraints as RigidbodyConstraints
from Infernux.components import CollisionDetectionMode as CollisionDetectionMode
from Infernux.components import RigidbodyInterpolation as RigidbodyInterpolation
from Infernux.components import AudioSource as AudioSource
from Infernux.components import AudioListener as AudioListener
from Infernux.components import SpriteRenderer as SpriteRenderer
from Infernux.components import SpiritAnimator as SpiritAnimator
from Infernux.components import SkeletalAnimator as SkeletalAnimator
# Decorators
from Infernux.components import require_component as require_component
from Infernux.components import disallow_multiple as disallow_multiple
from Infernux.components import execute_in_edit_mode as execute_in_edit_mode
from Infernux.components import add_component_menu as add_component_menu
from Infernux.components import icon as icon
from Infernux.components import help_url as help_url
from Infernux.components import RequireComponent as RequireComponent
from Infernux.components import DisallowMultipleComponent as DisallowMultipleComponent
from Infernux.components import ExecuteInEditMode as ExecuteInEditMode
from Infernux.components import AddComponentMenu as AddComponentMenu
from Infernux.components import HelpURL as HelpURL
from Infernux.components import Icon as Icon
# Core assets
from Infernux.core import Material as Material
from Infernux.core import Texture as Texture
from Infernux.core import Shader as Shader
from Infernux.core import AudioClip as AudioClip
from Infernux.core import AnimationClip as AnimationClip
from Infernux.core import AnimStateMachine as AnimStateMachine
from Infernux.core import AnimState as AnimState
from Infernux.core import AnimTransition as AnimTransition
from Infernux.core import AnimParameter as AnimParameter
from Infernux.core import AssetManager as AssetManager
from Infernux.core import TextureRef as TextureRef
from Infernux.core import ShaderRef as ShaderRef
from Infernux.core import AudioClipRef as AudioClipRef
from Infernux.core import AnimationClipRef as AnimationClipRef
from Infernux.core import AnimStateMachineRef as AnimStateMachineRef
# Debug — class only (use Debug.log / Debug.log_warning / …)
from Infernux.debug import Debug as Debug
# Submodules
from Infernux import core as core
from Infernux import rendergraph as rendergraph
from Infernux import renderstack as renderstack
from Infernux import scene as scene
from Infernux import input as input
from Infernux import ui as ui
# Scene
from Infernux.scene import GameObjectQuery as GameObjectQuery
from Infernux.scene import LayerMask as LayerMask
from Infernux.scene import SceneManager as SceneManager
# Timing & math utilities
from Infernux.timing import Time as Time
from Infernux.mathf import Mathf as Mathf
# Coroutines
from Infernux.coroutine import (
    Coroutine as Coroutine,
    WaitForSeconds as WaitForSeconds,
    WaitForSecondsRealtime as WaitForSecondsRealtime,
    WaitForEndOfFrame as WaitForEndOfFrame,
    WaitForFixedUpdate as WaitForFixedUpdate,
    WaitUntil as WaitUntil,
    WaitWhile as WaitWhile,
)
# Batch processing
from Infernux.batch import batch_read as batch_read
from Infernux.batch import batch_write as batch_write
# JIT helpers (lazy-loaded via __getattr__ at runtime)
from Infernux import jit as jit
from Infernux.jit import JIT_AVAILABLE as JIT_AVAILABLE
from Infernux.jit import ensure_jit_runtime as ensure_jit_runtime
from Infernux.jit import njit as njit
from Infernux.jit import warmup as warmup
