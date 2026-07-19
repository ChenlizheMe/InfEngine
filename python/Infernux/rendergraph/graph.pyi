from __future__ import annotations

from typing import Mapping, Optional, Tuple, List, Dict

from Infernux.lib import (
    RenderGraphDescription,
    GraphPassDesc,
    GraphTextureDesc,
    MaterialPassType,
    PixelFormat,
)
from Infernux.renderstack.effect_stage import EffectScope, EffectStage

Format = PixelFormat


class TextureHandle:
    """A handle to a transient texture resource in the render graph."""

    name: str
    format: Format
    is_camera_target: bool
    size: Optional[Tuple[int, int]]
    size_divisor: int

    def __init__(
        self,
        name: str,
        format: Format,
        is_camera_target: bool = ...,
        size: Optional[Tuple[int, int]] = ...,
        size_divisor: int = ...,
    ) -> None: ...
    @property
    def is_depth(self) -> bool:
        """Returns True if this texture uses a depth format."""
        ...
    def __repr__(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...


class BufferHandle:
    """A handle to a transient buffer resource in the render graph."""

    name: str
    byte_size: int
    usage: int

    def __init__(self, name: str, byte_size: int, usage: int) -> None: ...
    def __repr__(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...


class RenderPassBuilder:
    """Fluent builder for constructing a render pass."""

    def __init__(self, name: str, graph: RenderGraph | None = ...) -> None: ...
    @property
    def name(self) -> str:
        """The name of this render pass."""
        ...
    def read(self, texture: str | TextureHandle) -> RenderPassBuilder:
        """Declare a texture as a read dependency for this pass."""
        ...
    def write_color(self, texture: str | TextureHandle, slot: int = ...) -> RenderPassBuilder:
        """Declare a color attachment output for this pass."""
        ...
    def write_depth(self, texture: str | TextureHandle) -> RenderPassBuilder:
        """Declare a depth attachment output for this pass."""
        ...
    def read_buffer(
        self, buffer: str | BufferHandle, usage: str = ...
    ) -> RenderPassBuilder:
        """Declare a storage, indirect, or transfer buffer read."""
        ...
    def write_buffer(
        self, buffer: str | BufferHandle, usage: str = ...
    ) -> RenderPassBuilder:
        """Declare a storage or transfer buffer write."""
        ...
    def set_side_effect(self, enabled: bool = ...) -> RenderPassBuilder:
        """Retain this pass for externally observable work."""
        ...
    def set_texture(self, sampler_name: str, texture: str | TextureHandle) -> RenderPassBuilder:
        """Bind a texture to a sampler input for this pass."""
        ...
    def set_textures(self, bindings: Mapping[str, object]) -> RenderPassBuilder:
        """Bind multiple textures to sampler inputs for this pass."""
        ...
    def set_clear(
        self,
        color: Optional[Tuple[float, float, float, float]] = ...,
        depth: Optional[float] = ...,
    ) -> RenderPassBuilder:
        """Set clear values for color and/or depth attachments."""
        ...
    def draw_renderers(
        self,
        queue_range: Tuple[int, int] = ...,
        sort_mode: str = ...,
        pass_tag: str = ...,
        override_material: str = ...,
        material_pass: str = ...,
    ) -> RenderPassBuilder:
        """Draw visible renderers filtered by queue range."""
        ...
    def draw_skybox(self) -> RenderPassBuilder:
        """Draw the skybox in this pass."""
        ...
    def draw_shadow_casters(
        self,
        queue_range: Tuple[int, int] = ...,
        light_index: int = ...,
        shadow_type: str = ...,
    ) -> RenderPassBuilder:
        """Draw shadow-casting geometry for a light."""
        ...
    def draw_screen_ui(
        self,
        list: str | int = ...,
    ) -> RenderPassBuilder:
        """Draw screen-space UI elements in this pass."""
        ...
    def fullscreen_quad(
        self,
        shader: str,
        **push_constants: float,
    ) -> RenderPassBuilder:
        """Draw a fullscreen quad with the specified shader."""
        ...
    def copy_texture(
        self, source: str | TextureHandle, destination: str | TextureHandle
    ) -> RenderPassBuilder:
        """Copy one graph texture into another in a copy pass."""
        ...
    def copy_buffer(
        self,
        source: str | BufferHandle,
        destination: str | BufferHandle,
        byte_count: int = ...,
    ) -> RenderPassBuilder:
        """Copy bytes between graph buffers in a copy pass."""
        ...
    def present(self, source: str | TextureHandle) -> RenderPassBuilder:
        """Export a graph texture from a present pass."""
        ...
    def set_param(self, name: str, value: float) -> RenderPassBuilder:
        """Set a push-constant parameter for this pass."""
        ...
    def __enter__(self) -> RenderPassBuilder: ...
    def __exit__(self, *args: object) -> None: ...
    def __repr__(self) -> str: ...


class RenderGraph:
    """A declarative render graph that defines texture resources and render passes."""

    def __init__(self, name: str = ...) -> None: ...
    @property
    def name(self) -> str:
        """The name of this render graph."""
        ...
    @property
    def pass_count(self) -> int:
        """Number of render passes in the graph."""
        ...
    @property
    def texture_count(self) -> int:
        """Number of texture resources in the graph."""
        ...
    @property
    def buffer_count(self) -> int:
        """Number of buffer resources in the graph."""
        ...
    @property
    def topology_sequence(self) -> List[Tuple[str, str]]:
        """Ordered list of (pass_name, type) entries defining the execution order."""
        ...
    @property
    def injection_points(self) -> list:
        """List of injection points for pass extension."""
        ...
    @property
    def effect_stages(self) -> List[EffectStage]:
        """Pipeline-declared user attachment stages in topology order."""
        ...
    def set_msaa_samples(self, samples: int) -> None:
        """Set the MSAA sample count for all render targets."""
        ...
    def create_texture(
        self,
        name: str,
        *,
        format: Format = ...,
        camera_target: bool = ...,
        size: Optional[Tuple[int, int]] = ...,
        size_divisor: int = ...,
    ) -> TextureHandle:
        """Declare a transient texture resource in the render graph."""
        ...
    def get_texture(self, name: str) -> Optional[TextureHandle]:
        """Get a texture handle by name, or None if not found."""
        ...
    def create_buffer(
        self,
        name: str,
        byte_size: int,
        *,
        storage: bool = ...,
        indirect: bool = ...,
        transfer_source: bool = ...,
        transfer_destination: bool = ...,
    ) -> BufferHandle:
        """Declare a transient buffer resource in the render graph."""
        ...
    def get_buffer(self, name: str) -> Optional[BufferHandle]:
        """Get a buffer handle by name, or None if not found."""
        ...
    def has_pass(self, name: str) -> bool:
        """Check if a render pass with the given name exists."""
        ...
    def has_injection_point(self, name: str) -> bool:
        """Check if an injection point with the given name exists."""
        ...
    def has_effect_stage(self, stable_id: str) -> bool:
        """Check for an exact declared stage ID."""
        ...
    def injection_point(
        self,
        name: str,
        *,
        display_name: str = ...,
        resources: Optional[set] = ...,
    ) -> None:
        """Declare an injection point where external passes can be inserted."""
        ...
    def effect_stage(
        self,
        stable_id: str,
        *,
        scope: EffectScope | str = ...,
        display_name: str = ...,
        inputs: Optional[set[str]] = ...,
        outputs: Optional[set[str]] = ...,
        capabilities: Optional[set[str]] = ...,
    ) -> EffectStage:
        """Declare a stable user-facing RenderEffect attachment stage."""
        ...
    def effects(self, stable_id: str, **kwargs: object) -> EffectStage:
        """Pipeline-author shorthand for ``effect_stage``."""
        ...
    def screen_ui_section(self, *, resources: set | None = ...) -> None:
        """Declare a screen UI section in the graph topology."""
        ...
    def add_pass(self, name: str) -> RenderPassBuilder:
        """Add a new render pass to the graph."""
        ...
    def add_compute_pass(self, name: str) -> RenderPassBuilder:
        """Add a compute-domain pass with typed resource accesses."""
        ...
    def add_copy_pass(self, name: str) -> RenderPassBuilder:
        """Add a transfer-domain texture or buffer copy pass."""
        ...
    def add_present_pass(self, name: str) -> RenderPassBuilder:
        """Add a final graph export pass."""
        ...
    def remove_pass(self, name: str) -> RenderPassBuilder | None:
        """Remove a render pass by name. Returns the removed builder, or None."""
        ...
    def append_pass(self, builder: RenderPassBuilder) -> None:
        """Append an existing RenderPassBuilder to the graph."""
        ...
    def set_output(self, texture: str | TextureHandle) -> None:
        """Set the final output texture of the render graph."""
        ...
    def validate_no_ip_before_first_pass(self) -> None:
        """Validate that no user extension point appears before the first pass."""
        ...
    def get_debug_string(self) -> str:
        """Return a human-readable summary of the graph for debugging."""
        ...
    def build(self) -> RenderGraphDescription:
        """Compile the graph into a RenderGraphDescription for the backend."""
        ...
