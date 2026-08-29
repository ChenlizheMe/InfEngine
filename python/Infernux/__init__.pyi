from __future__ import annotations

from typing import Callable, List, Optional, Tuple, Type, TypeVar

_SerializedValue = TypeVar("_SerializedValue")

# Engine
from Infernux.engine import release_engine as release_engine
from Infernux.engine import run_headless as run_headless
from Infernux.engine import Engine as Engine
from Infernux.engine import LogLevel as LogLevel
from Infernux.application import Application as Application
from Infernux.screen import Insets as Insets
from Infernux.screen import Rect as Rect
from Infernux.screen import Screen as Screen
from Infernux.acceptance import RuntimeAcceptance as RuntimeAcceptance
from Infernux.acceptance import RuntimeAcceptanceManifest as RuntimeAcceptanceManifest
from Infernux.acceptance import RuntimeAcceptanceTest as RuntimeAcceptanceTest
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
from Infernux.lib import LineAlignment as LineAlignment
from Infernux.lib import LineTextureMode as LineTextureMode
from Infernux.lib import LineCurveWrapMode as LineCurveWrapMode
from Infernux.lib import LineGradientMode as LineGradientMode
from Infernux.lib import LineWidthKey as LineWidthKey
from Infernux.lib import LineColorKey as LineColorKey
# Components — user-facing
from Infernux.components import InxComponent as InxComponent
from Infernux.components import int_field as int_field
from Infernux.components import list_field as list_field
from Infernux.components import component_field as component_field
from Infernux.components import component_list_field as component_list_field
from Infernux.components import hide_field as hide_field
from Infernux.components import InspectorSpace as InspectorSpace
from Infernux.components import FieldType as FieldType
from Infernux.components import GameObjectRef as GameObjectRef
from Infernux.components import MaterialRef as MaterialRef
from Infernux.components import ComponentRef as ComponentRef
from Infernux.components import PrefabRef as PrefabRef
from Infernux.components import SerializableObject as SerializableObject

def serialized_field(
    default: _SerializedValue = ...,
    *,
    field_type: Optional[FieldType] = ...,
    element_type: Optional[FieldType] = ...,
    element_class: Optional[Type] = ...,
    serializable_class: Optional[Type] = ...,
    component_type: Optional[str] = ...,
    asset_type: Optional[str] = ...,
    range: Optional[Tuple[float, float]] = ...,
    tooltip: str = ...,
    display_name_key: str = ...,
    enum_labels: Optional[List[str]] = ...,
    readonly: bool = ...,
    header: str = ...,
    space: float = ...,
    group: str = ...,
    info_text: str = ...,
    multiline: bool = ...,
    slider: bool = ...,
    drag_speed: Optional[float] = ...,
    required_component: Optional[str] = ...,
    visible_when: Optional[Callable] = ...,
    hdr: bool = ...,
    hidden: bool = ...,
) -> _SerializedValue: ...
# Builtin components
from Infernux.components import Light as Light
from Infernux.components import MeshRenderer as MeshRenderer
from Infernux.components import LineRenderer as LineRenderer
from Infernux.components import SkinnedMeshRenderer as SkinnedMeshRenderer
from Infernux.components import Camera as Camera
from Infernux.components import Collider as Collider
from Infernux.components import BoxCollider as BoxCollider
from Infernux.components import SphereCollider as SphereCollider
from Infernux.components import CapsuleCollider as CapsuleCollider
from Infernux.components import CylinderCollider as CylinderCollider
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
from Infernux.components import RuntimeAcceptanceRunner as RuntimeAcceptanceRunner
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
from Infernux.core import AnimationFrame as AnimationFrame
from Infernux.core import AnimStateMachine as AnimStateMachine
from Infernux.core import AnimState as AnimState
from Infernux.core import AnimTransition as AnimTransition
from Infernux.core import AnimCondition as AnimCondition
from Infernux.core import AnimParameter as AnimParameter
from Infernux.core import AssetManager as AssetManager
from Infernux.core import TextureRef as TextureRef
from Infernux.core import ShaderRef as ShaderRef
from Infernux.core import AudioClipRef as AudioClipRef
from Infernux.core import AnimationClipRef as AnimationClipRef
from Infernux.core import AnimStateMachineRef as AnimStateMachineRef
from Infernux.core import RenderEffectRef as RenderEffectRef
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
    WaitForFrames as WaitForFrames,
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
