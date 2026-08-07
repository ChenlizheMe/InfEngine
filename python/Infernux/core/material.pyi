"""Type stubs for Infernux.core.material."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from Infernux.lib import InxMaterial

_VK_BLEND_SRC_ALPHA: int
_VK_BLEND_ONE_MINUS_SRC_ALPHA: int
_VK_BLEND_OP_ADD: int
_RENDER_QUEUE_OPAQUE: int
_RENDER_QUEUE_TRANSPARENT: int
_DEFAULT_ALPHA_CLIP_THRESHOLD: float


class Material:
    """Pythonic wrapper around C++ InxMaterial."""

    def __init__(self, native: InxMaterial) -> None:
        """Wrap an existing C++ InxMaterial."""
        ...

    # Factory methods
    @staticmethod
    def create_lit(name: str = ...) -> Material:
        """Create a new material with the default lit (PBR) shader."""
        ...
    @staticmethod
    def create_unlit(name: str = ...) -> Material:
        """Create a new material with the unlit shader."""
        ...
    @staticmethod
    def from_native(native: InxMaterial) -> Material:
        """Wrap an existing C++ InxMaterial instance."""
        ...
    def to_dict(self) -> dict:
        """Return the material document without encoding JSON text."""
        ...
    def serialize_document(self) -> dict: ...
    def apply_dict(self, document: dict) -> bool:
        """Transactionally apply a material document."""
        ...
    def deserialize_document(self, document: dict) -> bool: ...
    @staticmethod
    def load(file_path: str) -> Optional[Material]:
        """Load a material from a file path."""
        ...
    @staticmethod
    def get(name: str) -> Optional[Material]:
        """Get a cached material by name."""
        ...

    # Context manager
    def __enter__(self) -> Material:
        """Enter context manager for automatic disposal."""
        ...
    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        """Exit context manager and dispose resources."""
        ...
    def dispose(self) -> None:
        """Release the underlying native material resources."""
        ...
    def clone(self) -> Material:
        """Create a deep copy of this material, including shader state and properties."""
        ...

    # Properties
    @property
    def native(self) -> InxMaterial:
        """The underlying C++ InxMaterial object."""
        ...
    @property
    def name(self) -> str:
        """The display name of the material."""
        ...
    @name.setter
    def name(self, value: str) -> None: ...
    @property
    def guid(self) -> str:
        """The globally unique identifier for this material."""
        ...
    @property
    def render_queue(self) -> int:
        """The render queue priority for draw order sorting."""
        ...
    @render_queue.setter
    def render_queue(self, value: int) -> None: ...
    @property
    def shader_name(self) -> str:
        """The name of the shader program used by this material."""
        ...
    @shader_name.setter
    def shader_name(self, name: str) -> None: ...
    @property
    def vert_shader_name(self) -> str:
        """The vertex shader name override."""
        ...
    @vert_shader_name.setter
    def vert_shader_name(self, name: str) -> None: ...
    @property
    def frag_shader_name(self) -> str:
        """The fragment shader name override."""
        ...
    @frag_shader_name.setter
    def frag_shader_name(self, name: str) -> None: ...
    def set_shader(self, shader_name: str) -> None:
        """Set the shader used by this material."""
        ...
    @property
    def is_builtin(self) -> bool:
        """Whether this is a built-in engine material."""
        ...

    # Render state properties
    @property
    def render_state_overrides(self) -> int:
        """Bitmask of render state overrides applied to this material."""
        ...
    @render_state_overrides.setter
    def render_state_overrides(self, value: int) -> None: ...
    @property
    def cull_mode(self) -> int:
        """The face culling mode (0=None, 1=Front, 2=Back)."""
        ...
    @cull_mode.setter
    def cull_mode(self, value: int) -> None: ...
    @property
    def depth_write_enable(self) -> bool:
        """Whether depth buffer writing is enabled."""
        ...
    @depth_write_enable.setter
    def depth_write_enable(self, value: bool) -> None: ...
    @property
    def depth_test_enable(self) -> bool:
        """Whether depth testing is enabled."""
        ...
    @depth_test_enable.setter
    def depth_test_enable(self, value: bool) -> None: ...
    @property
    def depth_compare_op(self) -> int:
        """The depth comparison operator."""
        ...
    @depth_compare_op.setter
    def depth_compare_op(self, value: int) -> None: ...
    @property
    def blend_enable(self) -> bool:
        """Whether alpha blending is enabled."""
        ...
    @blend_enable.setter
    def blend_enable(self, value: bool) -> None: ...
    @property
    def surface_type(self) -> str:
        """The surface type ('opaque' or 'transparent')."""
        ...
    @surface_type.setter
    def surface_type(self, value: str) -> None: ...
    @property
    def alpha_clip_enabled(self) -> bool:
        """Whether alpha clipping (cutout) is enabled."""
        ...
    @alpha_clip_enabled.setter
    def alpha_clip_enabled(self, value: bool) -> None: ...
    @property
    def alpha_clip_threshold(self) -> float:
        """The alpha value threshold for clipping."""
        ...
    @alpha_clip_threshold.setter
    def alpha_clip_threshold(self, value: float) -> None: ...

    # Shader property setters
    def set_float(self, name: str, value: float) -> None:
        """Set a float uniform property on the material."""
        ...
    def set_int(self, name: str, value: int) -> None:
        """Set an integer uniform property on the material."""
        ...
    def set_color(self, name: str, r: float, g: float, b: float, a: float = ...) -> None:
        """Set a color uniform property on the material."""
        ...
    def set_vector2(self, name: str, x: float, y: float) -> None:
        """Set a 2D vector uniform property on the material."""
        ...
    def set_vector3(self, name: str, x: float, y: float, z: float) -> None:
        """Set a 3D vector uniform property on the material."""
        ...
    def set_vector4(self, name: str, x: float, y: float, z: float, w: float) -> None:
        """Set a 4D vector uniform property on the material."""
        ...
    def set_texture_guid(self, name: str, texture_guid: str) -> None:
        """Assign a texture to a sampler slot by GUID."""
        ...
    def clear_texture(self, name: str) -> None:
        """Remove the texture assigned to a sampler slot."""
        ...

    # Shader property getters
    def get_float(self, name: str, default: float = ...) -> float:
        """Get a float uniform property value."""
        ...
    def get_int(self, name: str, default: int = ...) -> int:
        """Get an integer uniform property value."""
        ...
    def get_color(self, name: str) -> Tuple[float, float, float, float]:
        """Get a color property as an (R, G, B, A) tuple."""
        ...
    def get_vector2(self, name: str) -> Tuple[float, float]:
        """Get a 2D vector property as an (X, Y) tuple."""
        ...
    def get_vector3(self, name: str) -> Tuple[float, float, float]:
        """Get a 3D vector property as an (X, Y, Z) tuple."""
        ...
    def get_vector4(self, name: str) -> Tuple[float, float, float, float]:
        """Get a 4D vector property as an (X, Y, Z, W) tuple."""
        ...
    def get_texture(self, name: str) -> Optional[str]:
        """Get the GUID of the texture assigned to a sampler slot."""
        ...
    def has_property(self, name: str) -> bool:
        """Check whether a shader property exists on this material."""
        ...
    def get_property(self, name: str) -> Any:
        """Get a shader property value by name."""
        ...
    def get_all_properties(self) -> dict:
        """Get all shader properties as a dictionary."""
        ...

    # Serialization
    def to_dict(self) -> dict:
        """Serialize the material to a dictionary."""
        ...
    def save(self, file_path: str) -> bool:
        """Save the material to a file."""
        ...

    # High-level property setters
    def set_param(self, name: str, value: Any) -> None:
        """Set a non-texture material property using type/shape dispatch."""
        ...
    def set_texture(self, name: str, value: Any) -> None:
        """Set a texture property from GUID, path, Texture, or None."""
        ...

    # Flush
    def flush(self) -> None:
        """Force-write any pending changes to disk."""
        ...
    @staticmethod
    def flush_all_pending() -> None:
        """Flush all materials that have throttled pending saves."""
        ...

    def __repr__(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...
