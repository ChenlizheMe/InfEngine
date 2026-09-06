"""
Material wrapper.

Wraps the C++ InxMaterial with context manager support, property caching,
and a clean scripting API.

Usage::

    # Create via factory
    mat = Material.create_lit("MyPBR")
    mat.set_color("baseColor", 1.0, 0.5, 0.0)
    mat.set_float("metallic", 0.9)
    mat.set_float("smoothness", 0.9)

    # Context manager for scoped lifecycle
    with Material.create_lit("Temp") as mat:
        mat.set_float("smoothness", 0.5)
        renderer.material = mat
    # mat is disposed on exit

    # Load from file
    mat = Material.load("materials/gold.mat")

    # Assign to a MeshRenderer
    mesh_renderer.material = mat.native
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from Infernux.debug import Debug
from Infernux.lib import InxMaterial

_EMBEDDED_MODEL_MAT_TOKEN = "::submat:"

# ── Vulkan blend factor constants (VkBlendFactor) ──
_VK_BLEND_SRC_ALPHA: int = 6
_VK_BLEND_ONE_MINUS_SRC_ALPHA: int = 7
_VK_BLEND_OP_ADD: int = 0

# ── Render queue constants ──
_RENDER_QUEUE_OPAQUE: int = 2000
_RENDER_QUEUE_TRANSPARENT: int = 3000

# ── Default alpha clip threshold ──
_DEFAULT_ALPHA_CLIP_THRESHOLD: float = 0.5


class Material:
    """Pythonic wrapper around C++ InxMaterial.

    Provides:
    - Context manager for scoped lifecycle
    - Clean property setters/getters
    - Factory methods for common material workflows
    - Serialization to/from dict
    """

    # Minimum interval (seconds) between disk writes for the same material.
    _AUTOSAVE_MIN_INTERVAL: float = 0.5

    # Class-level set of Material instances with pending throttled saves.
    _pending_saves: dict[int, "Material"] = {}

    # When True, auto-save is suppressed (e.g. during Play mode).
    # Runtime material changes should be transient like Unity.
    _suppress_auto_save: bool = False

    def __init__(self, native: "InxMaterial"):
        """Wrap an existing native InxMaterial.

        Prefer using factory methods (create_lit, create_unlit, load) instead.
        """
        if native is None:
            raise ValueError("Cannot wrap a None InxMaterial")
        self._native = native
        self._disposed = False
        self._last_save_time: float = 0.0
        self._save_pending: bool = False

    # ==========================================================================
    # Factory Methods
    # ==========================================================================

    @staticmethod
    def create_lit(name: str = "New Material") -> "Material":
        """Create a new PBR lit material (default_lit shader)."""
        native = InxMaterial.create_default_lit()
        if name != "New Material":
            native.name = name
        return Material(native)

    @staticmethod
    def create_unlit(name: str = "Unlit Material") -> "Material":
        """Create a new unlit material (default_unlit shader)."""
        native = InxMaterial.create_default_unlit()
        if name != "Unlit Material":
            native.name = name
        return Material(native)

    @staticmethod
    def from_native(native: "InxMaterial") -> "Material":
        """Wrap an existing C++ InxMaterial."""
        return Material(native)

    def to_dict(self) -> dict:
        """Return the material document without encoding JSON text."""
        return self._native.serialize_document()

    def serialize_document(self) -> dict:
        """Return the canonical editor document for this material."""
        return self.to_dict()

    def apply_dict(self, document: dict) -> bool:
        """Transactionally apply a material document."""
        return bool(self._native.deserialize_document(document))

    def deserialize_document(self, document: dict) -> bool:
        """Apply the canonical editor document transactionally."""
        return self.apply_dict(document)

    @staticmethod
    def _parse_embedded_model_material_path(file_path: str) -> Optional[Tuple[str, int]]:
        if _EMBEDDED_MODEL_MAT_TOKEN not in file_path:
            return None
        base, _, idx_s = file_path.partition(_EMBEDDED_MODEL_MAT_TOKEN)
        if not base or not idx_s:
            return None
        try:
            slot = int(idx_s)
        except ValueError:
            return None
        if slot < 0:
            return None
        return (base, slot)

    @staticmethod
    def _load_embedded_model_material_slot(model_path: str, slot: int, virtual_path: str) -> Optional["Material"]:
        """Build a DefaultLit material from Assimp-extracted slot data (FBX inline materials)."""
        from Infernux.lib import AssetRegistry, InxMaterial

        mesh = AssetRegistry.instance().load_mesh(model_path)
        if mesh is None:
            return None
        slots = mesh.get_material_slot_data()
        if slot >= len(slots):
            return None
        slot_data = slots[slot]
        native = InxMaterial.create_default_lit()
        native.is_builtin = False
        base_color = slot_data["base_color"]
        emission_color = slot_data["emission_color"]
        native.set_color("baseColor", tuple(float(value) for value in base_color))
        native.set_color(
            "emissionColor", tuple(float(value) for value in emission_color)
        )
        native.set_float("metallic", float(slot_data["metallic"]))
        native.set_float("smoothness", float(slot_data["smoothness"]))
        names = mesh.material_slot_names
        native.name = (
            str(names[slot])
            if slot < len(names) and names[slot]
            else f"EmbeddedMaterial_{slot}"
        )
        native.file_path = virtual_path
        return Material.from_native(native)

    @staticmethod
    def load(file_path: str) -> Optional["Material"]:
        """Load a material from a .mat file.

        Uses AssetRegistry.load_material() so the returned instance is the
        same shared object used by the renderer and Inspector.  This ensures
        that property changes via set_color / set_float are immediately
        visible everywhere (Unity-like behaviour).
        """
        embedded = Material._parse_embedded_model_material_path(file_path)
        if embedded:
            model_path, slot = embedded
            return Material._load_embedded_model_material_slot(model_path, slot, file_path)

        from Infernux.lib import AssetRegistry

        native = AssetRegistry.instance().load_material(file_path)
        return Material(native) if native is not None else None

    @staticmethod
    def get(name: str) -> Optional["Material"]:
        """Look up a built-in material by name.

        Queries AssetRegistry's builtin material map (keyed by name,
        e.g. 'DefaultLit', 'ErrorMaterial').  Returns None if not found.
        """
        from Infernux.lib import AssetRegistry

        native = AssetRegistry.instance().get_builtin_material(name)
        return Material(native) if native is not None else None

    # ==========================================================================
    # Context Manager (scoped lifecycle)
    # ==========================================================================

    def __enter__(self) -> "Material":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.dispose()
        return False

    def dispose(self):
        """Release this material reference."""
        if not self._disposed and self._native is not None:
            self._save_pending = False
            Material._pending_saves.pop(id(self), None)
            self._disposed = True
            # Note: actual GPU resource cleanup happens in C++ destructor
            # when the last shared_ptr reference is released.

    # ==========================================================================
    # Properties
    # ==========================================================================

    @property
    def native(self) -> "InxMaterial":
        """Access the underlying C++ InxMaterial (for passing to C++ APIs)."""
        return self._native

    @property
    def name(self) -> str:
        return self._native.name

    @name.setter
    def name(self, value: str):
        self._native.name = value

    @property
    def guid(self) -> str:
        return self._native.guid

    @property
    def render_queue(self) -> int:
        return self._native.get_render_queue()

    @render_queue.setter
    def render_queue(self, value: int):
        from Infernux.lib import RenderStateOverride

        self._native.set_render_queue(value)
        self._native.mark_override(RenderStateOverride.RENDER_QUEUE)

    @property
    def shader_name(self) -> str:
        return self._native.shader_name

    @shader_name.setter
    def shader_name(self, name: str):
        self._native.shader_name = name

    @property
    def vert_shader_name(self) -> str:
        return self._native.vert_shader_name

    @vert_shader_name.setter
    def vert_shader_name(self, name: str):
        self._native.vert_shader_name = name

    @property
    def frag_shader_name(self) -> str:
        return self._native.frag_shader_name

    @frag_shader_name.setter
    def frag_shader_name(self, name: str):
        self._native.frag_shader_name = name

    def set_shader(self, shader_name: str):
        """Set the material's shader by name (sets both vert and frag)."""
        self._native.set_shader(shader_name)

    @property
    def is_builtin(self) -> bool:
        return self._native.is_builtin

    # ==========================================================================
    # Render State Convenience Properties
    # ==========================================================================

    def _commit_render_state(self, state, *override_names: str) -> None:
        """Write back a mutated RenderState, claiming only the given overrides.

        The native set_render_state marks *every* field as authored (a full
        explicit assignment). A single-field Inspector/Python edit must only
        claim its own field so the untouched ones keep following the shader's
        annotation defaults, so restore the previous override mask and add
        just the edited bits.
        """
        from Infernux.lib import RenderStateOverride
        overrides = self._native.render_state_overrides
        self._native.set_render_state(state)
        for name in override_names:
            overrides |= int(getattr(RenderStateOverride, name))
        self._native.render_state_overrides = overrides

    def _set_render_state_field(self, field: str, value, override_name: str) -> None:
        """Set one render-state field, commit it back, and mark the override."""
        state = self._native.get_render_state()
        setattr(state, field, value)
        self._commit_render_state(state, override_name)

    @property
    def render_state_overrides(self) -> int:
        """Bitmask of user-overridden RenderState fields."""
        return self._native.render_state_overrides

    @render_state_overrides.setter
    def render_state_overrides(self, value: int):
        self._native.render_state_overrides = value

    @property
    def cull_mode(self) -> int:
        """VkCullModeFlags: 0=None, 1=Front, 2=Back."""
        return self._native.get_render_state().cull_mode

    @cull_mode.setter
    def cull_mode(self, value: int):
        self._set_render_state_field("cull_mode", value, "CULL_MODE")

    @property
    def depth_write_enable(self) -> bool:
        return self._native.get_render_state().depth_write_enable

    @depth_write_enable.setter
    def depth_write_enable(self, value: bool):
        self._set_render_state_field("depth_write_enable", value, "DEPTH_WRITE")

    @property
    def depth_test_enable(self) -> bool:
        return self._native.get_render_state().depth_test_enable

    @depth_test_enable.setter
    def depth_test_enable(self, value: bool):
        self._set_render_state_field("depth_test_enable", value, "DEPTH_TEST")

    @property
    def depth_compare_op(self) -> int:
        """VkCompareOp: 0=Never, 1=Less, 2=Equal, 3=LessOrEqual, etc."""
        return self._native.get_render_state().depth_compare_op

    @depth_compare_op.setter
    def depth_compare_op(self, value: int):
        self._set_render_state_field("depth_compare_op", value, "DEPTH_COMPARE_OP")

    @property
    def blend_enable(self) -> bool:
        return self._native.get_render_state().blend_enable

    @blend_enable.setter
    def blend_enable(self, value: bool):
        self._set_render_state_field("blend_enable", value, "BLEND_ENABLE")

    @property
    def surface_type(self) -> str:
        """'opaque' or 'transparent' (derived from blend_enable)."""
        return "transparent" if self.blend_enable else "opaque"

    @surface_type.setter
    def surface_type(self, value: str):
        state = self._native.get_render_state()
        if value == "transparent":
            state.blend_enable = True
            state.src_color_blend_factor = _VK_BLEND_SRC_ALPHA
            state.dst_color_blend_factor = _VK_BLEND_ONE_MINUS_SRC_ALPHA
            state.color_blend_op = _VK_BLEND_OP_ADD
            state.depth_write_enable = False
            state.render_queue = _RENDER_QUEUE_TRANSPARENT
        else:
            state.blend_enable = False
            state.depth_write_enable = True
            state.render_queue = _RENDER_QUEUE_OPAQUE
        self._commit_render_state(
            state, "SURFACE_TYPE", "BLEND_ENABLE", "BLEND_MODE", "DEPTH_WRITE", "RENDER_QUEUE"
        )
        # Older tooling could accidentally create shader properties with the
        # same names as render-state fields. Keep a single source of truth.
        self._native.remove_property("blend_enable")
        self._native.remove_property("depth_write_enable")

    @property
    def alpha_clip_enabled(self) -> bool:
        """Whether alpha clipping is enabled."""
        return self._native.get_render_state().alpha_clip_enabled

    @alpha_clip_enabled.setter
    def alpha_clip_enabled(self, value: bool):
        state = self._native.get_render_state()
        state.alpha_clip_enabled = value
        if value and state.alpha_clip_threshold <= 0.0:
            state.alpha_clip_threshold = _DEFAULT_ALPHA_CLIP_THRESHOLD
        self._commit_render_state(state, "ALPHA_CLIP")
        self._native.sync_alpha_clip_property()

    @property
    def alpha_clip_threshold(self) -> float:
        """Alpha clip threshold (0.0–1.0)."""
        return self._native.get_render_state().alpha_clip_threshold

    @alpha_clip_threshold.setter
    def alpha_clip_threshold(self, value: float):
        state = self._native.get_render_state()
        state.alpha_clip_threshold = max(0.0, min(1.0, value))
        state.alpha_clip_enabled = True
        self._commit_render_state(state, "ALPHA_CLIP")
        self._native.sync_alpha_clip_property()

    # ==========================================================================
    # Auto-save (Unity-like dirty-flag persistence)
    # ==========================================================================

    def _auto_save(self):
        """Auto-save material to its .mat file after property changes.

        Mirrors Unity: every material property change persists to disk,
        both in edit mode and at runtime.  Saves are throttled so that
        rapid-fire changes (e.g. every-frame set_texture) don't hammer
        the file system — pending writes are flushed via flush().
        """
        if Material._suppress_auto_save or self._cannot_persist():
            return

        file_path = getattr(self._native, "file_path", None)
        if not file_path:
            return

        now = time.monotonic()
        if now - self._last_save_time < self._AUTOSAVE_MIN_INTERVAL:
            self._save_pending = True
            Material._pending_saves[id(self)] = self
            return

        self._flush_save()

    def _flush_save(self):
        """Execute the actual disk write."""
        if self._cannot_persist():
            self._save_pending = False
            Material._pending_saves.pop(id(self), None)
            return
        file_path = getattr(self._native, "file_path", None)
        if not file_path:
            return
        try:
            ok = self._native.save()
            if ok:
                self._last_save_time = time.monotonic()
                self._save_pending = False
                Material._pending_saves.pop(id(self), None)
            else:
                self._save_pending = True
                Material._pending_saves[id(self)] = self
                from Infernux.debug import Debug
                Debug.log_warning(
                    f"Material._auto_save: save() returned False for '{self.name}' "
                    f"(path={file_path})"
                )
        except Exception as e:
            self._save_pending = True
            Material._pending_saves[id(self)] = self
            from Infernux.debug import Debug
            Debug.log_warning(f"Material._auto_save: exception for '{self.name}': {e}")

    def _cannot_persist(self) -> bool:
        """Return whether this wrapper no longer owns a writable material."""
        if self._disposed or self._native is None:
            return True
        try:
            return bool(self._native.is_deleted())
        except AttributeError:
            return False
        except RuntimeError:
            return True

    def flush(self):
        """Force-write any pending changes to disk."""
        if self._save_pending:
            self._flush_save()

    @staticmethod
    def flush_all_pending():
        """Flush all materials that have throttled pending saves.

        Called by the engine at end-of-frame to ensure changes persist
        without blocking every property setter with synchronous I/O.
        """
        for mat in list(Material._pending_saves.values()):
            mat._flush_save()

    # ==========================================================================
    # Shader Property Setters (Unity-compatible naming)
    # ==========================================================================

    def set_float(self, name: str, value: float):
        """Set a float shader property."""
        self._native.set_float(name, value)
        self._auto_save()

    def set_int(self, name: str, value: int):
        """Set an integer shader property."""
        self._native.set_int(name, value)
        self._auto_save()

    def set_color(self, name: str, r: float, g: float, b: float, a: float = 1.0):
        """Set a color shader property (RGBA)."""
        self._native.set_color(name, (r, g, b, a))
        self._auto_save()

    def set_vector2(self, name: str, x: float, y: float):
        """Set a vec2 shader property."""
        self._native.set_vector2(name, (x, y))
        self._auto_save()

    def set_vector3(self, name: str, x: float, y: float, z: float):
        """Set a vec3 shader property."""
        self._native.set_vector3(name, (x, y, z))
        self._auto_save()

    def set_vector4(self, name: str, x: float, y: float, z: float, w: float):
        """Set a vec4 shader property."""
        self._native.set_vector4(name, (x, y, z, w))
        self._auto_save()

    def set_texture_guid(self, name: str, texture_guid: str):
        """Set a texture shader property by asset GUID."""
        self._native.set_texture_guid(name, texture_guid)
        self._auto_save()

    def set_param(self, name: str, value):
        """Set a non-texture material property using type/shape dispatch.

        Examples:
            material.set_param("metallic", 0.9)
            material.set_param("baseColor", (1.0, 0.5, 0.2, 1.0))
            material.set_param("tiling", (2.0, 2.0))
        """
        self._native.set_param(name, value)
        self._auto_save()

    def set_texture(self, name: str, value):
        """Set a texture property from a GUID, builtin token, texture object, or None.

        Supported values:
            - ``None``: clears the texture
            - texture asset GUID string or builtin texture token
            - object with a non-empty ``guid``
        """
        self._native.set_texture(name, value)
        self._auto_save()

    def clear_texture(self, name: str):
        """Clear a texture shader property (remove texture reference)."""
        self._native.clear_texture(name)
        self._auto_save()

    def remove_property(self, name: str) -> bool:
        """Remove a shader property and any asset dependency it owns."""
        removed = bool(self._native.remove_property(name))
        if removed:
            self._auto_save()
        return removed

    # ==========================================================================
    # Shader Property Getters
    # ==========================================================================

    def get_float(self, name: str, default: float = 0.0) -> float:
        """Get a float shader property."""
        val = self._native.get_property(name)
        return float(val) if val is not None else default

    def get_int(self, name: str, default: int = 0) -> int:
        """Get an integer shader property."""
        val = self._native.get_property(name)
        return int(val) if val is not None else default

    def get_color(self, name: str) -> Tuple[float, float, float, float]:
        """Get a color shader property as (r, g, b, a)."""
        val = self._native.get_property(name)
        if val is None:
            return (0.0, 0.0, 0.0, 1.0)
        try:
            return (float(val[0]), float(val[1]), float(val[2]), float(val[3]))
        except (TypeError, IndexError):
            return (0.0, 0.0, 0.0, 1.0)

    def get_vector2(self, name: str) -> Tuple[float, float]:
        """Get a vec2 shader property."""
        val = self._native.get_property(name)
        if val is None:
            return (0.0, 0.0)
        try:
            return (float(val[0]), float(val[1]))
        except (TypeError, IndexError):
            return (0.0, 0.0)

    def get_vector3(self, name: str) -> Tuple[float, float, float]:
        """Get a vec3 shader property."""
        val = self._native.get_property(name)
        if val is None:
            return (0.0, 0.0, 0.0)
        try:
            return (float(val[0]), float(val[1]), float(val[2]))
        except (TypeError, IndexError):
            return (0.0, 0.0, 0.0)

    def get_vector4(self, name: str) -> Tuple[float, float, float, float]:
        """Get a vec4 shader property."""
        val = self._native.get_property(name)
        if val is None:
            return (0.0, 0.0, 0.0, 0.0)
        try:
            return (float(val[0]), float(val[1]), float(val[2]), float(val[3]))
        except (TypeError, IndexError):
            return (0.0, 0.0, 0.0, 0.0)

    def get_texture(self, name: str) -> Optional[str]:
        """Get a texture shader property GUID (or None if not set)."""
        val = self._native.get_property(name)
        return str(val) if val is not None else None

    def has_property(self, name: str) -> bool:
        """Check if the material has a property with the given name."""
        return self._native.has_property(name)

    def get_property(self, name: str):
        """Get a property value by name (generic). Returns None if not found."""
        return self._native.get_property(name)

    def get_all_properties(self) -> dict:
        """Get all properties as a dictionary."""
        return self._native.get_all_properties()

    def save(self, file_path: str) -> bool:
        """Save material to a .mat file."""
        try:
            self._native.save_to(file_path)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    # ==========================================================================
    # Dunder methods
    # ==========================================================================

    def __repr__(self):
        return f"Material(name='{self.name}', queue={self.render_queue})"

    def __eq__(self, other):
        if isinstance(other, Material):
            return self.guid == other.guid
        return NotImplemented

    def __hash__(self):
        return hash(self.guid)

    # ==========================================================================
    # Clone / Instantiate (Unity-style)
    # ==========================================================================

    def clone(self) -> "Material":
        """Create a deep copy of this material (Unity: ``new Material(original)``).

        All properties, shader names, and render state are copied.
        Texture references remain shared (same as Unity).
        The clone is a runtime-only instance with no GUID or file path.
        """
        cloned_native = self._native.clone()
        return Material(cloned_native)
