"""
RenderGraph builder API.

Pure Python graph builder that constructs a RenderGraphDescription.
The description is then sent to C++ for DAG compilation and execution.

Design: builder pattern with a fluent API for straightforward authoring.

    graph = RenderGraph("ForwardPipeline")
    graph.create_texture("color", camera_target=True)
    graph.create_texture("depth", format=Format.D32_SFLOAT)

    with graph.add_pass("Opaque") as p:
        p.write_color("color")
        p.write_depth("depth")
        p.set_clear(color=(0.1, 0.1, 0.1, 1.0), depth=1.0)
        p.draw_renderers(queue_range=(0, 2500), sort_mode="front_to_back")

    graph.set_output("color")
    desc = graph.build()  # -> RenderGraphDescription (C++ POD)
"""

from __future__ import annotations

import warnings
from itertools import count
from typing import Mapping, Optional, Tuple, List, Dict

# Try to import the native types. If unavailable, we define stubs so the
# Python-side graph can still be built and tested without a running engine.
from Infernux.lib import (
    RenderGraphDescription,
    GraphPassDesc,
    GraphCommandDesc,
    GraphCommandType,
    GraphPassType,
    GraphBufferDesc,
    GraphBufferUsage,
    GraphBufferAccessDesc,
    GraphBufferAccessType,
    GraphTextureDesc,
    MaterialPassType,
    PixelFormat,
)
_HAS_NATIVE = True
_SOURCE_REVISION_COUNTER = count(1)


# ============================================================================
# Public format type
# ============================================================================

Format = PixelFormat


# ============================================================================
# TextureHandle — lightweight handle to a graph texture resource
# ============================================================================

class TextureHandle:
    """Opaque handle to a texture resource in the RenderGraph.

    Not meant to be constructed directly — use ``RenderGraph.create_texture()``.
    """

    def __init__(self, name: str, format: Format, is_camera_target: bool = False,
                 size: "Optional[Tuple[int, int]]" = None,
                 size_divisor: int = 0):
        self.name = name
        self.format = format
        self.is_camera_target = is_camera_target
        self.size = size  # (width, height) or None for scene target size
        self.size_divisor = size_divisor  # >1: scene_size / divisor

    @property
    def is_depth(self) -> bool:
        return self.format.is_depth

    def __repr__(self) -> str:
        tag = " [camera_target]" if self.is_camera_target else ""
        return f"<TextureHandle '{self.name}' {self.format.name}{tag}>"

    def __eq__(self, other):
        if isinstance(other, TextureHandle):
            return self.name == other.name
        return NotImplemented

    def __hash__(self):
        return hash(self.name)


class BufferHandle:
    """Opaque handle to a transient graph buffer."""

    def __init__(self, name: str, byte_size: int, usage: int):
        self.name = name
        self.byte_size = byte_size
        self.usage = usage

    def __repr__(self) -> str:
        return f"<BufferHandle '{self.name}' {self.byte_size} bytes>"

    def __eq__(self, other):
        if isinstance(other, BufferHandle):
            return self.name == other.name
        return NotImplemented

    def __hash__(self):
        return hash(self.name)


# ============================================================================
# RenderPassBuilder — configures a single pass in the graph
# ============================================================================

class RenderPassBuilder:
    """Builder for configuring a single render pass.

    Provides a fluent API for declaring inputs, outputs, clear settings,
    and the render action for a pass. Also usable as a context manager::

        with graph.add_pass("OpaquePass") as p:
            p.write_color("color")
            p.draw_renderers(queue_range=(0, 2500))

    All resource arguments (``read``, ``write_color``, ``write_depth``,
    ``set_input``) accept **either** a string alias (resolved via
    ``graph.get_texture()``) or a ``TextureHandle`` directly.
    """

    def __init__(self, name: str, graph: "RenderGraph | None" = None,
                 pass_type: str = "raster"):
        self._name = name
        self._graph = graph
        self._pass_type = pass_type
        self._reads: List[str] = []
        self._buffer_accesses: List[Tuple[str, str]] = []
        self._write_colors: Dict[int, str] = {}  # slot -> texture_name (MRT)
        self._write_depth: Optional[str] = None
        self._clear_color: Optional[Tuple[float, float, float, float]] = None
        self._clear_depth: Optional[float] = None
        self._action = "none"
        self._material_pass = "forward"
        self._queue_min = 0
        self._queue_max = 5000
        self._sort_mode = "none"
        self._pass_tag = ""
        self._override_material = ""
        self._input_bindings: Dict[str, str] = {}  # sampler -> texture_name
        self._light_index = 0
        self._screen_ui_list = 0
        self._shader_name: str = ""
        self._push_constants: Dict[str, float] = {}
        self._source_resource = ""
        self._destination_resource = ""
        self._copy_bytes = 0
        self._side_effect = False

    @property
    def name(self) -> str:
        return self._name

    # ---- String / handle resolution ----

    def _resolve(self, texture) -> "TextureHandle":
        """Resolve a string alias or ``TextureHandle`` to ``TextureHandle``."""
        if isinstance(texture, str):
            if self._graph is None:
                raise ValueError(
                    f"Cannot resolve alias '{texture}' without graph reference"
                )
            handle = self._graph.get_texture(texture)
            if handle is None:
                raise ValueError(
                    f"Texture '{texture}' not found in graph "
                    f"'{self._graph.name}'"
                )
            return handle
        return texture

    def _resolve_buffer(self, buffer) -> "BufferHandle":
        if isinstance(buffer, str):
            if self._graph is None:
                raise ValueError(
                    f"Cannot resolve buffer alias '{buffer}' without graph reference"
                )
            handle = self._graph.get_buffer(buffer)
            if handle is None:
                raise ValueError(
                    f"Buffer '{buffer}' not found in graph '{self._graph.name}'"
                )
            return handle
        if not isinstance(buffer, BufferHandle):
            raise TypeError("Expected a buffer alias or BufferHandle")
        return buffer

    # ---- Resource declarations ----

    def read(self, texture) -> "RenderPassBuilder":
        """Declare a texture input dependency.

        Args:
            texture: Texture alias (``str``) or ``TextureHandle``.
        """
        handle = self._resolve(texture)
        if handle.name not in self._reads:
            self._reads.append(handle.name)
        return self

    def write_color(self, texture, slot: int = 0) -> "RenderPassBuilder":
        """Declare a color output attachment.

        Args:
            texture: Texture alias (``str``) or ``TextureHandle``.
            slot: Color attachment slot (0 = primary, higher = MRT).
        """
        handle = self._resolve(texture)
        self._write_colors[slot] = handle.name
        return self

    def write_depth(self, texture) -> "RenderPassBuilder":
        """Declare the depth output attachment.

        Args:
            texture: Texture alias (``str``) or ``TextureHandle``.
        """
        handle = self._resolve(texture)
        self._write_depth = handle.name
        return self

    def read_buffer(self, buffer, usage: str = "storage") -> "RenderPassBuilder":
        """Declare a storage, indirect, or transfer buffer read."""
        handle = self._resolve_buffer(buffer)
        access = {
            "storage": "storage_read",
            "indirect": "indirect_read",
            "transfer": "transfer_read",
        }.get(str(usage).strip().lower())
        if access is None:
            raise ValueError(f"Unknown buffer read usage '{usage}'")
        self._buffer_accesses.append((handle.name, access))
        return self

    def write_buffer(self, buffer, usage: str = "storage") -> "RenderPassBuilder":
        """Declare a storage or transfer buffer write."""
        handle = self._resolve_buffer(buffer)
        access = {
            "storage": "storage_write",
            "transfer": "transfer_write",
        }.get(str(usage).strip().lower())
        if access is None:
            raise ValueError(f"Unknown buffer write usage '{usage}'")
        self._buffer_accesses.append((handle.name, access))
        return self

    def set_side_effect(self, enabled: bool = True) -> "RenderPassBuilder":
        """Retain this pass for externally observable work."""
        self._side_effect = bool(enabled)
        return self

    def set_texture(
        self,
        sampler_name: str,
        texture,
    ) -> "RenderPassBuilder":
        """Bind a graph texture to a shader sampler.

        Args:
            sampler_name: Sampler name in the shader
                          (e.g. ``"shadowMap"``).
            texture: Texture alias (``str``) or ``TextureHandle``.
        """
        handle = self._resolve(texture)
        self._input_bindings[sampler_name] = handle.name
        if handle.name not in self._reads:
            self._reads.append(handle.name)
        return self

    def set_textures(
        self,
        bindings: Mapping[str, object],
    ) -> "RenderPassBuilder":
        """Bind multiple graph textures to shader samplers in one call.

        This removes repeated ``set_texture()`` boilerplate in multi-input
        passes such as deferred lighting and post-processing.

        Args:
            bindings: Mapping of ``sampler_name -> texture`` where *texture*
                is either a string alias or ``TextureHandle``.
        """
        for sampler_name, texture in bindings.items():
            self.set_texture(sampler_name, texture)
        return self

    # ---- Clear settings ----

    def set_clear(
        self,
        color: Optional[Tuple[float, float, float, float]] = None,
        depth: Optional[float] = None,
    ) -> "RenderPassBuilder":
        """Set clear values for this pass.

        Args:
            color: RGBA clear color tuple, or None to not clear color.
            depth: Depth clear value, or None to not clear depth.
        """
        self._clear_color = color
        self._clear_depth = depth
        return self

    # ---- Render actions ----

    def draw_renderers(
        self,
        queue_range: Tuple[int, int] = (0, 5000),
        sort_mode: str = "none",
        pass_tag: str = "",
        override_material: str = "",
        material_pass: str = "forward",
    ) -> "RenderPassBuilder":
        """Configure this pass to draw scene renderers.

        Args:
            queue_range: (min, max) inclusive render queue range for filtering.
                         Opaque = (0, 2500), Transparent = (2501, 5000).
            sort_mode: Sorting strategy — "front_to_back", "back_to_front",
                       or "none".
            pass_tag: Filter draw calls by shader pass tag (empty = no filter).
            override_material: Force all objects to use this material name
                               (empty = per-object material).
            material_pass: Linked material program used by this pass. Supported
                           values are ``forward``, ``gbuffer``, ``depth``,
                           ``picking``, and ``motion``.
        """
        normalized_pass = str(material_pass).strip().lower()
        if normalized_pass not in {"forward", "gbuffer", "depth", "picking", "motion"}:
            raise ValueError(f"Unknown material pass '{material_pass}'")
        self._action = "draw_renderers"
        self._material_pass = normalized_pass
        self._queue_min, self._queue_max = queue_range
        self._sort_mode = sort_mode
        self._pass_tag = pass_tag
        self._override_material = override_material
        return self

    def draw_skybox(self) -> "RenderPassBuilder":
        """Configure this pass to draw the procedural skybox."""
        self._action = "draw_skybox"
        return self

    def draw_shadow_casters(
        self,
        queue_range: Tuple[int, int] = (0, 2999),
        light_index: int = 0,
    ) -> "RenderPassBuilder":
        """Configure this pass to render shadow casters into a depth-only shadow map.

        Each material uses its own vertex shader with a per-material shadow
        fragment variant (auto-generated).  Front-face culling and depth bias
        are applied for shadow acne prevention.

        Hard vs. soft shadows are NOT configured here: the shadow map only
        stores depth. Filtering (16-tap Vogel-disk PCF) is selected per light
        via ``light.shadows = LightShadows.Soft``.

        Args:
            queue_range: (min, max) inclusive render queue range for shadow casters.
                         Default (0, 2999) covers all opaque geometry regardless of queue.
            light_index: Index of the shadow-casting light (0 = first directional).
        """
        self._action = "draw_shadow_casters"
        self._queue_min, self._queue_max = queue_range
        self._light_index = light_index
        return self

    def draw_screen_ui(
        self,
        list: str = "camera",
    ) -> "RenderPassBuilder":
        """Configure this pass to draw screen-space UI.

        The UI commands are accumulated via InxScreenUIRenderer during BuildFrame
        and rendered here inside the scene render graph.

        Args:
            list: ``"camera"`` (before post-process, affected by post-processing)
                  or ``"overlay"`` (after post-process, on top of everything).
        """
        _str_to_int = {"camera": 0, "overlay": 1}
        value = _str_to_int.get(list.lower())
        if value is None:
            raise ValueError(
                f"Unknown screen UI list '{list}'. "
                f"Expected 'camera' or 'overlay'."
            )
        self._action = "draw_screen_ui"
        self._screen_ui_list = value
        return self

    def set_param(
        self,
        name: str,
        value: float,
    ) -> "RenderPassBuilder":
        """Set a named push-constant parameter for this pass.

        Parameters are passed to the fragment shader as push constants
        in the order they are declared.  Call once per parameter::

            p.set_param("intensity", 0.8)
            p.set_param("threshold", 1.0)

        Args:
            name: Parameter name (must match the shader push-constant
                  struct field name).
            value: Float value.
        """
        self._push_constants[name] = float(value)
        return self

    def fullscreen_quad(
        self,
        shader: str,
    ) -> "RenderPassBuilder":
        """Configure this pass to draw a fullscreen triangle with a named shader.

        The vertex shader is always ``fullscreen_triangle``; the fragment
        shader is looked up by *shader* (which must have a matching
        ``@shader_id``).

        Use ``set_param()`` to pass push constants and ``set_input()``
        to bind input textures before calling this method.

        Args:
            shader: Fragment shader id (e.g. ``"bloom_prefilter"``).
        """
        self._action = "fullscreen_quad"
        self._shader_name = shader
        return self

    def copy_texture(self, source, destination) -> "RenderPassBuilder":
        """Copy one graph texture into another in a copy pass."""
        if self._pass_type != "copy":
            raise ValueError("copy_texture() requires graph.add_copy_pass()")
        self._action = "copy_texture"
        self._source_resource = self._resolve(source).name
        self._destination_resource = self._resolve(destination).name
        return self

    def copy_buffer(self, source, destination, byte_count: int = 0) -> "RenderPassBuilder":
        """Copy bytes between graph buffers in a copy pass."""
        if self._pass_type != "copy":
            raise ValueError("copy_buffer() requires graph.add_copy_pass()")
        if byte_count < 0:
            raise ValueError("byte_count must be >= 0")
        source_handle = self._resolve_buffer(source)
        destination_handle = self._resolve_buffer(destination)
        source_handle.usage |= int(GraphBufferUsage.TRANSFER_SOURCE)
        destination_handle.usage |= int(GraphBufferUsage.TRANSFER_DESTINATION)
        self._action = "copy_buffer"
        self._source_resource = source_handle.name
        self._destination_resource = destination_handle.name
        self._copy_bytes = int(byte_count)
        return self

    def present(self, source) -> "RenderPassBuilder":
        """Export a graph texture from a present pass."""
        if self._pass_type != "present":
            raise ValueError("present() requires graph.add_present_pass()")
        handle = self._resolve(source)
        self._action = "present"
        self._source_resource = handle.name
        if self._graph is not None:
            self._graph.set_output(handle)
        return self

    # ---- Context manager support ----

    def __enter__(self) -> "RenderPassBuilder":
        return self

    def __exit__(self, *args):
        pass

    def __repr__(self) -> str:
        return (f"<RenderPassBuilder '{self._name}' "
                f"action={self._action}>")


# ============================================================================
# RenderGraph — the main graph builder
# ============================================================================

class RenderGraph:
    """Python-side RenderGraph topology builder.

    Unified API: textures have string aliases, injection points are
    declared inline, and the topology sequence is auto-recorded.

    Example::

        graph = RenderGraph("ForwardPipeline")

        graph.create_texture("color", camera_target=True)
        graph.create_texture("depth", format=Format.D32_SFLOAT)

        with graph.add_pass("OpaquePass") as p:
            p.write_color("color")
            p.write_depth("depth")
            p.set_clear(color=(0.1, 0.1, 0.1, 1.0), depth=1.0)
            p.draw_renderers(queue_range=(0, 2500), sort_mode="front_to_back")

        graph.injection_point("after_opaque", resources={"color", "depth"})

        graph.set_output("color")
        desc = graph.build()
    """

    def __init__(self, name: str = "RenderGraph"):
        self._name = name
        self._textures: List[TextureHandle] = []
        self._buffers: List[BufferHandle] = []
        self._passes: List[RenderPassBuilder] = []
        self._output: Optional[str] = None
        self._msaa_samples: int = 0  # 0 = no preference (keep current)
        # Topology auto-recording
        self._topology: List[Tuple[str, str]] = []
        self._injection_points_list: List = []  # List[InjectionPoint]
        # Optional callback invoked at each injection_point() (set by RenderStack)
        self._injection_callback = None

    @property
    def name(self) -> str:
        return self._name

    def set_msaa_samples(self, samples: int) -> None:
        """Set MSAA sample count for this graph (1=off, 2, 4, 8).

        The setting is applied to the engine before the graph executes.
        Use 0 to leave the current MSAA setting unchanged.
        """
        if samples not in (0, 1, 2, 4, 8):
            raise ValueError(f"Invalid MSAA sample count: {samples}. Must be 0, 1, 2, 4, or 8.")
        self._msaa_samples = samples

    @property
    def pass_count(self) -> int:
        return len(self._passes)

    @property
    def texture_count(self) -> int:
        return len(self._textures)

    @property
    def buffer_count(self) -> int:
        return len(self._buffers)

    @property
    def topology_sequence(self) -> List[Tuple[str, str]]:
        """Auto-recorded topology: ``[("pass", name), ("ip", display), ...]``."""
        return list(self._topology)

    @property
    def injection_points(self) -> list:
        """All injection points declared via ``injection_point()``."""
        return list(self._injection_points_list)

    # ---- Resource creation ----

    def create_texture(
        self,
        name: str,
        *,
        format: Format = Format.RGBA8_UNORM,
        camera_target: bool = False,
        size: "Optional[Tuple[int, int]]" = None,
        size_divisor: int = 0,
    ) -> TextureHandle:
        """Create a texture resource.

        Unified method — use keyword args for special textures::

            graph.create_texture("color", camera_target=True)
            graph.create_texture("depth", format=Format.D32_SFLOAT)
            graph.create_texture("shadow_map", format=Format.D32_SFLOAT, size=(4096, 4096))
            graph.create_texture("bloom_half", size_divisor=2)  # half-res

        Args:
            name: Unique string alias (e.g. ``"color"``, ``"depth"``).
            format: Texture format.
            camera_target: If ``True``, this is the camera's main color
                output. Resolution and format are determined by the engine.
            size: (width, height) custom resolution. ``None`` uses the
                scene render target size. Useful for shadow maps.
            size_divisor: Divide scene resolution by this value (>1).
                Mutually exclusive with *size*.
        """
        if size is not None and size_divisor > 0:
            raise ValueError(
                f"Texture '{name}' cannot use both size and size_divisor"
            )
        if size is not None:
            if size[0] <= 0 or size[1] <= 0:
                raise ValueError(
                    f"Texture '{name}' size must be positive, got {size}"
                )
        if size_divisor == 1:
            raise ValueError(
                f"Texture '{name}' size_divisor=1 has no effect; use 0 or >1"
            )
        if size_divisor < 0:
            raise ValueError(
                f"Texture '{name}' size_divisor must be >= 0"
            )
        if camera_target and format.is_depth:
            raise ValueError(
                f"Texture '{name}' cannot be a camera_target depth texture"
            )

        if self.get_texture(name) is not None or self.get_buffer(name) is not None:
            raise ValueError(
                f"Resource '{name}' already exists in graph '{self._name}'"
            )

        handle = TextureHandle(name, format, is_camera_target=camera_target,
                               size=size, size_divisor=size_divisor)
        self._textures.append(handle)
        return handle

    def get_texture(self, name: str) -> Optional[TextureHandle]:
        """Look up a texture by its string alias.

        Returns:
            ``TextureHandle`` or ``None`` if not found.
        """
        for tex in self._textures:
            if tex.name == name:
                return tex
        return None

    def create_buffer(
        self,
        name: str,
        byte_size: int,
        *,
        storage: bool = True,
        indirect: bool = False,
        transfer_source: bool = False,
        transfer_destination: bool = False,
    ) -> BufferHandle:
        """Create a transient graph buffer with backend-neutral usage flags."""
        if byte_size <= 0:
            raise ValueError(f"Buffer '{name}' byte_size must be positive")
        if self.get_texture(name) is not None or self.get_buffer(name) is not None:
            raise ValueError(f"Resource '{name}' already exists in graph '{self._name}'")
        usage = int(GraphBufferUsage.NONE)
        if storage:
            usage |= int(GraphBufferUsage.STORAGE)
        if indirect:
            usage |= int(GraphBufferUsage.INDIRECT)
        if transfer_source:
            usage |= int(GraphBufferUsage.TRANSFER_SOURCE)
        if transfer_destination:
            usage |= int(GraphBufferUsage.TRANSFER_DESTINATION)
        if usage == int(GraphBufferUsage.NONE):
            raise ValueError(f"Buffer '{name}' must declare at least one usage")
        handle = BufferHandle(name, int(byte_size), usage)
        self._buffers.append(handle)
        return handle

    def get_buffer(self, name: str) -> Optional[BufferHandle]:
        """Look up a graph buffer by alias."""
        for buffer in self._buffers:
            if buffer.name == name:
                return buffer
        return None

    # ---- Query helpers ----

    def has_pass(self, name: str) -> bool:
        """Check if a pass with *name* has already been added."""
        return any(p._name == name for p in self._passes)

    def has_injection_point(self, name: str) -> bool:
        """Check if an injection point with *name* has been declared."""
        return any(ip.name == name for ip in self._injection_points_list)

    # ---- Injection points ----

    def injection_point(
        self,
        name: str,
        *,
        display_name: str = "",
        resources: Optional[set] = None,
    ) -> None:
        """Declare an injection point at the current topology position.

        RenderStack injects user-mounted passes here during graph build.

        Args:
            name: Unique identifier (e.g. ``"after_opaque"``).
            display_name: Editor label (auto-generated from *name* if empty).
            resources: Guaranteed-available resource names at this point.
        """
        from Infernux.renderstack.injection_point import InjectionPoint

        ip = InjectionPoint(
            name=name,
            display_name=display_name or name.replace("_", " ").title(),
            resource_state=resources if resources is not None else {"color", "depth"},
        )
        self._injection_points_list.append(ip)
        self._topology.append(("ip", ip.display_name))

        if self._injection_callback is not None:
            self._injection_callback(name)

    # ---- Convenience: ScreenUI + post-process section ----

    def screen_ui_section(self, *, resources: "set | None" = None) -> None:
        """Insert the standard ScreenUI + post-process injection points.

        This is a convenience shortcut that emits::

            _ScreenUI_Camera          (draw_screen_ui list="camera")
            before_post_process       (injection point)
            after_post_process        (injection point)
            _ScreenUI_Overlay         (draw_screen_ui list="overlay")

        Custom pipelines can call this at the desired topology position.
        This method is **explicit opt-in**: if a pipeline does not call
        ``screen_ui_section()``, no ScreenUI section is added automatically.

        Override behavior: each element is only inserted when missing, so
        users may pre-declare one or more reserved names and let this method
        fill the rest without duplication.

        Args:
            resources: Resource set advertised to injection points.
                       Defaults to ``{"color"}``.
        """
        res = resources or {"color"}

        if not self.has_pass("_ScreenUI_Camera"):
            with self.add_pass("_ScreenUI_Camera") as p:
                p.write_color("color")
                p.draw_screen_ui(list="camera")

        if not self.has_injection_point("before_post_process"):
            self.injection_point("before_post_process", resources=res)
        if not self.has_injection_point("after_post_process"):
            self.injection_point("after_post_process", resources=res)

        if not self.has_pass("_ScreenUI_Overlay"):
            with self.add_pass("_ScreenUI_Overlay") as p:
                p.write_color("color")
                p.draw_screen_ui(list="overlay")

    # ---- Pass management ----

    def add_pass(self, name: str) -> RenderPassBuilder:
        """Add a render pass to the graph.

        Returns a ``RenderPassBuilder`` (also a context manager) that you
        use to configure the pass::

            with graph.add_pass("OpaquePass") as p:
                p.write_color("color")
                p.write_depth("depth")
                p.draw_renderers(queue_range=(0, 2500))

        The pass is appended to the topology sequence automatically.
        """
        builder = RenderPassBuilder(name, graph=self, pass_type="raster")
        self._passes.append(builder)
        self._topology.append(("pass", name))
        return builder

    def _add_typed_pass(self, name: str, pass_type: str) -> RenderPassBuilder:
        builder = RenderPassBuilder(name, graph=self, pass_type=pass_type)
        self._passes.append(builder)
        self._topology.append(("pass", name))
        return builder

    def add_compute_pass(self, name: str) -> RenderPassBuilder:
        """Add a compute-domain pass for typed storage-buffer accesses."""
        return self._add_typed_pass(name, "compute")

    def add_copy_pass(self, name: str) -> RenderPassBuilder:
        """Add a transfer-domain texture or buffer copy pass."""
        return self._add_typed_pass(name, "copy")

    def add_present_pass(self, name: str) -> RenderPassBuilder:
        """Add the final graph export pass."""
        return self._add_typed_pass(name, "present")

    def remove_pass(self, name: str) -> "RenderPassBuilder | None":
        """Remove a pass by name and return it, or ``None`` if not found.

        Also removes the corresponding topology entry.
        """
        removed = None
        for i, p in enumerate(self._passes):
            if p._name == name:
                removed = self._passes.pop(i)
                break
        if removed is not None:
            for i, (kind, label) in enumerate(self._topology):
                if kind == "pass" and label == name:
                    self._topology.pop(i)
                    break
        return removed

    def append_pass(self, builder: "RenderPassBuilder") -> None:
        """Re-append a previously removed pass at the end of the topology."""
        self._passes.append(builder)
        self._topology.append(("pass", builder._name))

    # ---- Output ----

    def set_output(self, texture) -> None:
        """Mark a texture as the final graph output.

        Args:
            texture: Texture alias (``str``) or ``TextureHandle``.
        """
        if isinstance(texture, str):
            handle = self.get_texture(texture)
            if handle is None:
                raise ValueError(
                    f"set_output: Texture '{texture}' not found in graph "
                    f"'{self._name}'"
                )
            self._output = handle.name
        else:
            self._output = texture.name

    # ---- Validation & finalization ----

    def validate_no_ip_before_first_pass(self) -> None:
        """Raise ``ValueError`` if an injection point precedes the first pass.

        The new API forbids IPs before the very first pass (use
        ``after_opaque`` etc. instead).
        """
        for kind, _label in self._topology:
            if kind == "pass":
                return  # first entry is a pass — OK
            if kind == "ip":
                raise ValueError(
                    f"Graph '{self._name}': injection point declared before "
                    "the first pass. The new API does not allow IPs before "
                    "the first pass."
                )

    def _validate_graph(self) -> None:
        self.validate_no_ip_before_first_pass()

        texture_map = {tex.name: tex for tex in self._textures}
        buffer_map = {buffer.name: buffer for buffer in self._buffers}
        pass_names = set()

        # Warn if multiple textures claim camera_target — they alias to the
        # same physical swapchain image, which is almost never intended.
        camera_targets = [t.name for t in self._textures if t.is_camera_target]
        if len(camera_targets) > 1:
            warnings.warn(
                f"[RenderGraph '{self._name}'] Multiple camera_target textures "
                f"({', '.join(camera_targets)}). All camera_target textures "
                f"alias to the same swapchain image — only one should be "
                f"camera_target=True.",
                stacklevel=3,
            )

        for tex in self._textures:
            if tex.size is not None and tex.size_divisor > 0:
                raise ValueError(
                    f"Texture '{tex.name}' cannot use both size and size_divisor"
                )
            if tex.size_divisor == 1:
                raise ValueError(
                    f"Texture '{tex.name}' size_divisor=1 has no effect; use 0 or >1"
                )

        for p in self._passes:
            if p._name in pass_names:
                raise ValueError(
                    f"Graph '{self._name}' contains duplicate pass name '{p._name}'"
                )
            pass_names.add(p._name)
            self._validate_pass(p, texture_map, buffer_map)

        if self._output is not None and self._output not in texture_map:
            raise ValueError(
                f"Graph '{self._name}' output '{self._output}' does not exist"
            )

    def _validate_pass(
        self,
        p: RenderPassBuilder,
        texture_map: Dict[str, TextureHandle],
        buffer_map: Dict[str, BufferHandle],
    ) -> None:
        raster_actions = {
            "none", "draw_renderers", "draw_skybox", "custom",
            "draw_shadow_casters", "draw_screen_ui", "fullscreen_quad",
        }
        allowed_actions = {
            "raster": raster_actions,
            "compute": {"none"},
            "copy": {"copy_texture", "copy_buffer"},
            "present": {"present"},
        }
        if p._pass_type not in allowed_actions or p._action not in allowed_actions[p._pass_type]:
            raise ValueError(
                f"Pass '{p._name}' action '{p._action}' is invalid for {p._pass_type} pass"
            )
        if p._pass_type != "raster" and (
            p._write_colors or p._write_depth is not None
            or p._clear_color is not None or p._clear_depth is not None
            or (p._pass_type != "compute" and p._reads)
        ):
            raise ValueError(
                f"Pass '{p._name}' uses raster texture attachments in a {p._pass_type} pass"
            )
        if p._pass_type == "compute" and not p._buffer_accesses and not p._side_effect:
            raise ValueError(
                f"Compute pass '{p._name}' must declare buffer access or a side effect"
            )
        if p._action == "draw_shadow_casters" and p._write_colors:
            raise ValueError(
                f"Pass '{p._name}' is depth-only and cannot write color targets"
            )

        if p._clear_depth is not None and p._write_depth is None:
            raise ValueError(
                f"Pass '{p._name}' clears depth but has no depth output"
            )

        if p._action == "draw_renderers":
            slots = sorted(p._write_colors)
            if slots != list(range(len(slots))):
                raise ValueError(
                    f"Pass '{p._name}' color slots must be contiguous from zero"
                )
            if p._material_pass == "depth":
                if p._write_colors or p._write_depth is None:
                    raise ValueError(
                        f"Depth pass '{p._name}' requires one depth output and no color outputs"
                    )
            elif p._material_pass == "picking":
                colors = [texture_map[name] for _, name in sorted(p._write_colors.items())]
                if (
                    len(colors) != 1
                    or colors[0].format != Format.RG32_UINT
                    or p._write_depth is None
                ):
                    raise ValueError(
                        f"Picking pass '{p._name}' requires RG32_UINT color[0] and a depth output"
                    )
            elif not p._write_colors:
                raise ValueError(
                    f"Material pass '{p._name}' requires at least one color output"
                )

        for buffer_name, _access in p._buffer_accesses:
            buffer = buffer_map.get(buffer_name)
            if buffer is None:
                raise ValueError(
                    f"Pass '{p._name}' references unknown buffer '{buffer_name}'"
                )
            required_usage = {
                "storage_read": int(GraphBufferUsage.STORAGE),
                "storage_write": int(GraphBufferUsage.STORAGE),
                "indirect_read": int(GraphBufferUsage.INDIRECT),
                "transfer_read": int(GraphBufferUsage.TRANSFER_SOURCE),
                "transfer_write": int(GraphBufferUsage.TRANSFER_DESTINATION),
            }[_access]
            if not buffer.usage & required_usage:
                raise ValueError(
                    f"Pass '{p._name}' buffer '{buffer_name}' does not declare "
                    f"the usage required by '{_access}'"
                )

        if p._action == "copy_texture":
            if p._source_resource not in texture_map or p._destination_resource not in texture_map:
                raise ValueError(f"Copy pass '{p._name}' references an unknown texture")
            if p._source_resource == p._destination_resource:
                raise ValueError(f"Copy pass '{p._name}' requires distinct textures")
            source = texture_map[p._source_resource]
            destination = texture_map[p._destination_resource]
            if source.is_camera_target or destination.is_camera_target:
                raise ValueError(
                    f"Copy pass '{p._name}' requires transient textures; use present() "
                    "or a fullscreen pass for a camera target"
                )
            if source.format != destination.format:
                raise ValueError(f"Copy pass '{p._name}' requires matching texture formats")
        elif p._action == "copy_buffer":
            if p._source_resource not in buffer_map or p._destination_resource not in buffer_map:
                raise ValueError(f"Copy pass '{p._name}' references an unknown buffer")
            if p._source_resource == p._destination_resource:
                raise ValueError(f"Copy pass '{p._name}' requires distinct buffers")
            maximum = min(
                buffer_map[p._source_resource].byte_size,
                buffer_map[p._destination_resource].byte_size,
            )
            if p._copy_bytes > maximum:
                raise ValueError(f"Copy pass '{p._name}' exceeds the smaller buffer")
        elif p._action == "present" and p._source_resource not in texture_map:
            raise ValueError(f"Present pass '{p._name}' references an unknown texture")
        elif p._action == "present" and texture_map[p._source_resource].is_depth:
            raise ValueError(f"Present pass '{p._name}' cannot export a depth texture")

        for read_name in p._reads:
            if read_name not in texture_map:
                raise ValueError(
                    f"Pass '{p._name}' reads unknown texture '{read_name}'"
                )

        for slot, tex_name in p._write_colors.items():
            tex = texture_map.get(tex_name)
            if tex is None:
                raise ValueError(
                    f"Pass '{p._name}' writes unknown color target '{tex_name}'"
                )
            if tex.is_depth:
                raise ValueError(
                    f"Pass '{p._name}' writes depth texture '{tex_name}' as color[{slot}]"
                )

        if p._write_depth is not None:
            tex = texture_map.get(p._write_depth)
            if tex is None:
                raise ValueError(
                    f"Pass '{p._name}' writes unknown depth target '{p._write_depth}'"
                )
            if not tex.is_depth:
                raise ValueError(
                    f"Pass '{p._name}' writes color texture '{p._write_depth}' as depth"
                )

        for sampler_name, tex_name in p._input_bindings.items():
            if tex_name not in texture_map:
                raise ValueError(
                    f"Pass '{p._name}' input '{sampler_name}' references unknown texture '{tex_name}'"
                )

        if p._action == "draw_shadow_casters" and p._write_depth is None:
            raise ValueError(
                f"Pass '{p._name}' is a shadow caster pass and requires a depth output"
            )

    # ---- Build ----

    def build(self) -> "RenderGraphDescription":
        """Build the graph into a C++ RenderGraphDescription.

        Validates the graph topology and produces the POD structure that
        C++ expects. Raises ValueError if there are validation issues.

        ``before_post_process`` and ``after_post_process`` injection points
        are **always** auto-injected at the end of the topology if the
        pipeline did not already declare them (via ``screen_ui_section()``
        or explicit ``injection_point()`` calls).  This guarantees that
        user passes targeting those slots always have somewhere to attach,
        regardless of the pipeline implementation.

        Returns:
            RenderGraphDescription (C++ POD) ready for
            ``SceneRenderGraph.apply_python_graph()``.
        """
        if not self._passes:
            raise ValueError(f"Graph '{self._name}' has no passes")

        self._validate_graph()

        # Auto-inject before/after_post_process injection points when the
        # pipeline didn't declare them.  Uses has_injection_point() so
        # pipelines that already define these (e.g. via screen_ui_section)
        # are not affected.
        _auto_res = {"color"}
        if not self.has_injection_point("before_post_process"):
            self.injection_point("before_post_process", resources=_auto_res)
        if not self.has_injection_point("after_post_process"):
            self.injection_point("after_post_process", resources=_auto_res)

        if self._output is None:
            # Auto-set output to camera_target if exists
            for tex in self._textures:
                if tex.is_camera_target:
                    self._output = tex.name
                    break

        if self._output is None:
            raise ValueError(
                f"Graph '{self._name}' has no output. "
                "Call graph.set_output(texture)."
            )

        if _HAS_NATIVE:
            return self._build_native()
        else:
            return self._build_dict()

    def _build_native(self):
        """Build using native C++ types."""
        desc = RenderGraphDescription()
        desc.name = self._name
        desc.source_revision = next(_SOURCE_REVISION_COUNTER)

        # Build texture list — construct full list then assign (pybind11
        # vectors return copies, so append() on a property doesn't work).
        tex_list = []
        for tex in self._textures:
            td = GraphTextureDesc()
            td.name = tex.name
            td.format = tex.format
            td.is_backbuffer = tex.is_camera_target
            td.is_depth = tex.is_depth
            if tex.size is not None:
                td.width = tex.size[0]
                td.height = tex.size[1]
            if tex.size_divisor > 0:
                td.size_divisor = tex.size_divisor
            tex_list.append(td)
        desc.textures = tex_list

        buffer_list = []
        for buffer in self._buffers:
            bd = GraphBufferDesc()
            bd.name = buffer.name
            bd.byte_size = buffer.byte_size
            bd.usage = buffer.usage
            buffer_list.append(bd)
        desc.buffers = buffer_list

        # Build pass list
        _command_map = {
            "draw_renderers": GraphCommandType.DRAW_RENDERERS,
            "draw_skybox": GraphCommandType.DRAW_SKYBOX,
            "draw_shadow_casters": GraphCommandType.DRAW_SHADOW_CASTERS,
            "draw_screen_ui": GraphCommandType.DRAW_SCREEN_UI,
            "fullscreen_quad": GraphCommandType.FULLSCREEN_QUAD,
            "copy_texture": GraphCommandType.COPY_TEXTURE,
            "copy_buffer": GraphCommandType.COPY_BUFFER,
            "present": GraphCommandType.PRESENT,
        }
        _pass_type_map = {
            "raster": GraphPassType.RASTER,
            "compute": GraphPassType.COMPUTE,
            "copy": GraphPassType.COPY,
            "present": GraphPassType.PRESENT,
        }
        _buffer_access_map = {
            "storage_read": GraphBufferAccessType.STORAGE_READ,
            "storage_write": GraphBufferAccessType.STORAGE_WRITE,
            "indirect_read": GraphBufferAccessType.INDIRECT_READ,
            "transfer_read": GraphBufferAccessType.TRANSFER_READ,
            "transfer_write": GraphBufferAccessType.TRANSFER_WRITE,
        }
        _material_pass_map = {
            "forward": MaterialPassType.FORWARD,
            "gbuffer": MaterialPassType.GBUFFER,
            "depth": MaterialPassType.DEPTH,
            "picking": MaterialPassType.PICKING,
            "motion": MaterialPassType.MOTION,
        }

        pass_list = []
        for p in self._passes:
            pd = GraphPassDesc()
            pd.name = p._name
            pd.type = _pass_type_map[p._pass_type]
            pd.read_textures = list(p._reads)
            # MRT support: serialize write_colors as list of (slot, name) pairs
            pd.write_colors = list(p._write_colors.items())
            pd.write_depth = p._write_depth or ""
            accesses = []
            for resource, access_type in p._buffer_accesses:
                access = GraphBufferAccessDesc()
                access.resource = resource
                access.type = _buffer_access_map[access_type]
                accesses.append(access)
            pd.buffer_accesses = accesses
            pd.side_effect = p._side_effect

            if p._clear_color is not None:
                pd.clear_color = True
                pd.clear_color_r = p._clear_color[0]
                pd.clear_color_g = p._clear_color[1]
                pd.clear_color_b = p._clear_color[2]
                pd.clear_color_a = p._clear_color[3]
            else:
                pd.clear_color = False

            if p._clear_depth is not None:
                pd.clear_depth = True
                pd.clear_depth_value = p._clear_depth
            else:
                pd.clear_depth = False

            command_type = _command_map.get(p._action)
            if command_type is not None:
                command = GraphCommandDesc()
                command.type = command_type
                command.material_pass = _material_pass_map[p._material_pass]
                command.queue_min = p._queue_min
                command.queue_max = p._queue_max
                command.sort_mode = p._sort_mode
                command.pass_tag = p._pass_tag
                command.override_material = p._override_material
                command.input_bindings = list(p._input_bindings.items())
                command.light_index = p._light_index
                command.screen_ui_list = p._screen_ui_list
                command.shader_name = p._shader_name
                command.push_constants = list(p._push_constants.items())
                command.source_resource = p._source_resource
                command.destination_resource = p._destination_resource
                command.copy_bytes = p._copy_bytes
                pd.commands = [command]

            pass_list.append(pd)
        desc.passes = pass_list

        desc.output_texture = self._output
        desc.msaa_samples = self._msaa_samples
        return desc

    def _build_dict(self):
        """Build as a dictionary (for testing without native module)."""
        return {
            "name": self._name,
            "source_revision": next(_SOURCE_REVISION_COUNTER),
            "textures": [
                {
                    "name": tex.name,
                    "format": int(tex.format),
                    "is_backbuffer": tex.is_camera_target,
                    "is_depth": tex.is_depth,
                }
                for tex in self._textures
            ],
            "buffers": [
                {
                    "name": buffer.name,
                    "byte_size": buffer.byte_size,
                    "usage": buffer.usage,
                }
                for buffer in self._buffers
            ],
            "passes": [
                {
                    "name": p._name,
                    "type": p._pass_type,
                    "reads": list(p._reads),
                    "buffer_accesses": list(p._buffer_accesses),
                    "write_colors": dict(p._write_colors),
                    "write_depth": p._write_depth or "",
                    "clear_color": p._clear_color,
                    "clear_depth": p._clear_depth,
                    "action": p._action,
                    "material_pass": p._material_pass,
                    "queue_min": p._queue_min,
                    "queue_max": p._queue_max,
                    "sort_mode": p._sort_mode,
                    "input_bindings": dict(p._input_bindings),
                    "source_resource": p._source_resource,
                    "destination_resource": p._destination_resource,
                    "copy_bytes": p._copy_bytes,
                    "side_effect": p._side_effect,
                }
                for p in self._passes
            ],
            "output_texture": self._output,
        }

    # ---- Debug ----

    def get_debug_string(self) -> str:
        """Get a human-readable representation of the graph."""
        lines = [f"RenderGraph '{self._name}':"]
        lines.append(f"  Textures ({len(self._textures)}):")
        for tex in self._textures:
            tag = " [camera_target]" if tex.is_camera_target else ""
            lines.append(f"    - {tex.name} ({tex.format.name}){tag}")

        lines.append(f"  Buffers ({len(self._buffers)}):")
        for buffer in self._buffers:
            lines.append(f"    - {buffer.name} ({buffer.byte_size} bytes, usage={buffer.usage})")

        lines.append(f"  Passes ({len(self._passes)}):")
        for i, p in enumerate(self._passes):
            lines.append(f"    [{i}] {p._name} [{p._pass_type}] -> {p._action}")
            if p._reads:
                lines.append(f"          reads: {', '.join(p._reads)}")
            if p._write_colors:
                for slot, name in sorted(p._write_colors.items()):
                    lines.append(f"          writes color[{slot}]: {name}")
            if p._write_depth:
                lines.append(f"          writes depth: {p._write_depth}")
            if p._buffer_accesses:
                accesses = ", ".join(f"{name}:{access}" for name, access in p._buffer_accesses)
                lines.append(f"          buffers: {accesses}")

        if self._output:
            lines.append(f"  Output: {self._output}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (f"<RenderGraph '{self._name}' "
                f"passes={len(self._passes)} textures={len(self._textures)} "
                f"buffers={len(self._buffers)}>")
