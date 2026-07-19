"""
Infernux Component System

Provides Python-based component definition for the Entity-Component system.
Users can create custom components by inheriting from InxComponent.

Example:
    from Infernux.components import InxComponent, serialized_field
    
    class PlayerController(InxComponent):
        speed: float = serialized_field(default=5.0, range=(0, 100), tooltip="Movement speed")
        
        def start(self):
            print(f"Player started with speed {self.speed}")
        
        def update(self, delta_time: float):
            pos = self.transform.position
            # Move logic...
"""

from .component import InxComponent
from .builtin_component import BuiltinComponent, CppProperty
from .builtin import (
    Light,
    MeshRenderer,
    SkinnedMeshRenderer,
    Camera,
    Collider,
    BoxCollider,
    SphereCollider,
    CapsuleCollider,
    MeshCollider,
    Rigidbody,
    RigidbodyConstraints,
    CollisionDetectionMode,
    RigidbodyInterpolation,
    AudioSource,
    AudioListener,
    SpriteRenderer,
)
from Infernux.lib import Transform, Component
from .serializable_object import SerializableObject
from .value_codec import ValueCodecDescriptor, ValueCodecRegistry, VALUE_CODECS
from .serialized_field import (
    serialized_field,
    int_field,
    list_field,
    component_field,
    component_list_field,
    hide_field,
    FieldType,
    get_serialized_fields,
    get_field_value,
    set_field_value,
    # Unity-style Annotated[] field markers
    Range,
    Tooltip,
    Header,
    Space,
    Group,
    InfoText,
    DragSpeed,
    RequiredComponent,
    Multiline,
    ReadOnly,
    HideInInspector,
    NonSerialized,
    HDR,
    Color,
)
from .ref_wrappers import GameObjectRef, MaterialRef, ComponentRef, PrefabRef
from .script_loader import (
    load_component_from_file,
    load_all_components_from_file,
    create_component_instance,
    load_and_create_component,
    get_component_info,
    ScriptLoadError,
)
from .registry import (
    get_type,
    get_all_types,
    T,
)
from .decorators import (
    require_component,
    disallow_multiple,
    execute_in_edit_mode,
    add_component_menu,
    icon,
    help_url,
    # Unity-style aliases
    RequireComponent,
    DisallowMultipleComponent,
    ExecuteInEditMode,
    AddComponentMenu,
    HelpURL,
    Icon,
)
from .spirit_animator import SpiritAnimator
from .skeletal_animator import SkeletalAnimator
from .timeline_action import TimelineAction
from .particle_system import ParticleSystem

__all__ = [
    "InxComponent",
    "Component",
    "Transform",
    "Light",
    "MeshRenderer",
    "SkinnedMeshRenderer",
    "Camera",
    "Collider",
    "BoxCollider",
    "SphereCollider",
    "CapsuleCollider",
    "MeshCollider",
    "Rigidbody",
    "RigidbodyConstraints",
    "CollisionDetectionMode",
    "RigidbodyInterpolation",
    "AudioSource",
    "AudioListener",
    "SpriteRenderer",
    "serialized_field",
    "int_field",
    "hide_field",
    "FieldType",
    # Annotated[] field markers
    "Range",
    "Tooltip",
    "Header",
    "Space",
    "Group",
    "InfoText",
    "DragSpeed",
    "RequiredComponent",
    "Multiline",
    "ReadOnly",
    "HideInInspector",
    "NonSerialized",
    "HDR",
    "Color",
    "GameObjectRef",
    "MaterialRef",
    "ComponentRef",
    "PrefabRef",
    "SerializableObject",
    "ValueCodecDescriptor",
    "ValueCodecRegistry",
    "VALUE_CODECS",
    "list_field",
    "component_field",
    "component_list_field",
    "get_serialized_fields",
    "get_field_value",
    "set_field_value",
    "load_component_from_file",
    "load_all_components_from_file",
    "create_component_instance",
    "load_and_create_component",
    "get_component_info",
    "ScriptLoadError",
    # Type lookup
    "get_type",
    "get_all_types",
    "T",
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
    # Animation
    "ParticleSystem",
    "SpiritAnimator",
    "SkeletalAnimator",
    "TimelineAction",
]
