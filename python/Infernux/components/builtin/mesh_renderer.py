"""
MeshRenderer — Python InxComponent wrapper for the C++ MeshRenderer component.

Exposes shadow settings and material access as CppProperty descriptors.
Mesh data access (vertices, normals, UVs, indices) is provided via
delegate methods.

Example::

    from Infernux.components.builtin import MeshRenderer

    class MyShadowToggle(InxComponent):
        def start(self):
            mr = self.game_object.get_component(MeshRenderer)
            mr.casts_shadows = False
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from Infernux.components.builtin_component import BuiltinComponent, CppProperty
from Infernux.components.fields import FieldType


def _to_native_material(value):
    """Unwrap a Python Material wrapper to native InxMaterial.

    Passes through strings (GUIDs) and None unchanged.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    native = getattr(value, "_native", None) or getattr(value, "native", None)
    if native is not None:
        return native
    return value


class MeshRenderer(BuiltinComponent):
    """Python wrapper for the C++ MeshRenderer component.

    Properties delegate to the C++ ``MeshRenderer`` via CppProperty.

    Material properties follow Unity naming conventions:

    * ``material`` / ``sharedMaterial`` \u2014 slot 0
    * ``materials`` / ``sharedMaterials`` \u2014 all slots

    Accepts any of: Python ``Material`` wrapper, native ``InxMaterial``,
    GUID string, or ``None``.
    """

    _cpp_type_name = "MeshRenderer"
    _component_category_ = "Rendering"

    # ---- Shadow settings ----
    casts_shadows = CppProperty(
        "casts_shadows",
        FieldType.BOOL,
        default=True,
        tooltip="Whether this renderer casts shadows",
    )
    receives_shadows = CppProperty(
        "receives_shadows",
        FieldType.BOOL,
        default=True,
        tooltip="Whether this renderer receives shadows",
    )

    # ------------------------------------------------------------------
    # Material — Unity-style API
    # ------------------------------------------------------------------

    @property
    def material(self):
        """The material for slot 0 (Unity-style)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_material(0)
        return None

    @material.setter
    def material(self, value) -> None:
        cpp = self._cpp_component
        if cpp is not None:
            cpp.set_material(0, _to_native_material(value))

    @property
    def sharedMaterial(self):
        """Alias for ``material`` (Unity compatibility)."""
        return self.material

    @sharedMaterial.setter
    def sharedMaterial(self, value) -> None:
        self.material = value

    @property
    def materials(self) -> list:
        """All materials across every slot (Unity-style)."""
        cpp = self._cpp_component
        if cpp is None:
            return []
        return [cpp.get_material(i) for i in range(cpp.material_count)]

    @materials.setter
    def materials(self, value: list) -> None:
        cpp = self._cpp_component
        if cpp is None:
            return
        for i, mat in enumerate(value):
            cpp.set_material(i, _to_native_material(mat))

    @property
    def sharedMaterials(self) -> list:
        """Alias for ``materials`` (Unity compatibility)."""
        return self.materials

    @sharedMaterials.setter
    def sharedMaterials(self, value: list) -> None:
        self.materials = value

    @property
    def material_guid(self) -> str:
        """The material GUID for slot 0 (empty string if none)."""
        cpp = self._cpp_component
        if cpp is not None:
            guids = cpp.get_material_guids()
            return guids[0] if guids else ""
        return ""

    @material_guid.setter
    def material_guid(self, value: str) -> None:
        cpp = self._cpp_component
        if cpp is not None:
            cpp.set_material(0, value)

    def has_render_material(self) -> bool:
        """Check if a custom material is assigned at slot 0."""
        return self.material is not None

    def get_effective_material(self, slot: int = 0):
        """Get the effective material at slot (custom or default)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_effective_material(slot)
        return None

    # ------------------------------------------------------------------
    # Multi-material API  (Unity Renderer alignment)
    # ------------------------------------------------------------------

    @property
    def material_count(self) -> int:
        """Number of material slots."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.material_count
        return 0

    materialCount = material_count  # Unity PascalCase alias

    def get_material(self, slot: int):
        """Get the material at a given slot index."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_material(slot)
        return None

    def set_material(self, slot_or_material, material=None) -> None:
        """Set a material.  Two calling conventions::

            mr.set_material(0, some_material)   # slot + material
            mr.set_material(some_material)       # slot 0 shorthand

        Accepts Material wrapper, InxMaterial, GUID string, or None.
        """
        cpp = self._cpp_component
        if cpp is None:
            return
        if material is None and not isinstance(slot_or_material, int):
            cpp.set_material(0, _to_native_material(slot_or_material))
        else:
            cpp.set_material(slot_or_material, _to_native_material(material))

    def get_material_guids(self) -> List[str]:
        """Get all material slot GUIDs as a list."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_material_guids()
        return []

    # ---- Unity Renderer.GetMaterials / GetSharedMaterials ----

    def get_materials(self, result: Optional[list] = None) -> list:
        """Return all materials as a list.

        If *result* is provided it is cleared and filled in-place
        (Unity non-alloc pattern); otherwise a new list is returned.

        Matches Unity ``Renderer.GetMaterials``.
        """
        mats = self.materials
        if result is not None:
            result.clear()
            result.extend(mats)
            return result
        return mats

    GetMaterials = get_materials  # Unity PascalCase alias

    def get_shared_materials(self, result: Optional[list] = None) -> list:
        """Return all shared materials (equivalent to ``get_materials``).

        Matches Unity ``Renderer.GetSharedMaterials``.
        """
        return self.get_materials(result)

    GetSharedMaterials = get_shared_materials  # Unity PascalCase alias

    # ---- Unity Renderer.SetMaterials / SetSharedMaterials ----

    def set_materials(self, materials_list: list) -> None:
        """Set all material slots from a list.

        Accepts Material wrappers, InxMaterial objects, GUID strings, or None.
        Matches Unity ``Renderer.SetMaterials``.
        """
        cpp = self._cpp_component
        if cpp is None:
            return
        for i, mat in enumerate(materials_list):
            native = _to_native_material(mat)
            if isinstance(native, str):
                cpp.set_material(i, native)
            else:
                cpp.set_material(i, native)

    SetMaterials = set_materials  # Unity PascalCase alias

    def set_shared_materials(self, materials_list: list) -> None:
        """Alias for ``set_materials`` (Unity compatibility).

        Matches Unity ``Renderer.SetSharedMaterials``.
        """
        self.set_materials(materials_list)

    SetSharedMaterials = set_shared_materials  # Unity PascalCase alias

    def set_material_slot_count(self, count: int) -> None:
        """Set the number of material slots."""
        cpp = self._cpp_component
        if cpp is not None:
            cpp.set_material_slot_count(count)

    # ------------------------------------------------------------------
    # Mesh data access (read-only, for AI / CV / inspection)
    # ------------------------------------------------------------------

    def has_inline_mesh(self) -> bool:
        """Check if the renderer has inline mesh data."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.has_inline_mesh()
        return False

    @property
    def has_mesh_asset(self) -> bool:
        """Check if the renderer references an asset-managed mesh."""
        cpp = self._cpp_component
        if cpp is not None:
            return bool(cpp.has_mesh_asset)
        return False

    @property
    def mesh_asset_guid(self) -> str:
        """GUID of the referenced mesh asset, if any."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.mesh_asset_guid
        return ""

    @property
    def mesh_name(self) -> str:
        """Display name of the referenced mesh asset, if any."""
        cpp = self._cpp_component
        if cpp is not None:
            return getattr(cpp, "mesh_name", "")
        return ""

    def get_mesh_asset(self):
        """Get the InxMesh asset object (None if no asset mesh)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_mesh_asset()
        return None

    def get_material_slot_names(self) -> List[str]:
        """Material slot names from the model file (e.g. 'Body', 'Glass')."""
        mesh = self.get_mesh_asset()
        if mesh is not None:
            names = list(mesh.material_slot_names)
            cpp = self._cpp_component
            submesh_index = getattr(cpp, "submesh_index", -1) if cpp is not None else -1
            if submesh_index >= 0 and submesh_index < mesh.submesh_count:
                info = mesh.get_submesh_info(submesh_index)
                slot = int(info.get("material_slot", 0))
                if 0 <= slot < len(names):
                    return [names[slot]]
                sub_name = info.get("name", "")
                return [sub_name] if sub_name else []
            return names
        return []

    def get_submesh_infos(self) -> List[dict]:
        """Get info dicts for each submesh: name, vertex/index counts, material slot."""
        mesh = self.get_mesh_asset()
        if mesh is None:
            return []
        cpp = self._cpp_component
        submesh_index = getattr(cpp, "submesh_index", -1) if cpp is not None else -1
        if submesh_index >= 0 and submesh_index < mesh.submesh_count:
            return [mesh.get_submesh_info(submesh_index)]
        result = []
        for i in range(mesh.submesh_count):
            result.append(mesh.get_submesh_info(i))
        return result

    @property
    def vertex_count(self) -> int:
        """Number of vertices in inline mesh (0 if using resource mesh)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.vertex_count
        return 0

    @property
    def index_count(self) -> int:
        """Number of indices in inline mesh (0 if using resource mesh)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.index_count
        return 0

    def get_positions(self) -> List[Tuple[float, float, float]]:
        """Get all vertex positions as (x, y, z) tuples."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_positions()
        return []

    def get_normals(self) -> List[Tuple[float, float, float]]:
        """Get all vertex normals as (x, y, z) tuples."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_normals()
        return []

    def get_uvs(self) -> List[Tuple[float, float]]:
        """Get all vertex UVs as (u, v) tuples."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_uvs()
        return []

    def get_indices(self) -> List[int]:
        """Get all indices as a flat list."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_indices()
        return []

    def set_primitive_mesh(self, primitive_type) -> None:
        """Assign one of the built-in primitive meshes."""
        cpp = self._cpp_component
        if cpp is not None:
            cpp.set_primitive_mesh(primitive_type)

    def set_mesh_asset_guid(self, guid: str) -> None:
        """Assign a model/mesh asset by GUID."""
        cpp = self._cpp_component
        if cpp is not None and hasattr(cpp, "set_mesh_asset_guid"):
            cpp.set_mesh_asset_guid(guid or "")

    def clear_mesh_asset(self) -> None:
        """Clear the assigned asset mesh."""
        cpp = self._cpp_component
        if cpp is not None and hasattr(cpp, "clear_mesh_asset"):
            cpp.clear_mesh_asset()

    def serialize(self) -> str:
        """Serialize MeshRenderer to JSON string (delegates to C++)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.serialize()
        return "{}"
