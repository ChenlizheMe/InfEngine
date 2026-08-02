"""
Collider — Abstract base class for all collider BuiltinComponent wrappers.

Mirrors Unity's ``Collider`` base class. Concrete subclasses
(``BoxCollider``, ``SphereCollider``, ``CapsuleCollider``) inherit
the shared ``center``, ``is_trigger`` properties and category.

Example::

    from Infernux.components.builtin import BoxCollider

    class MyScript(InxComponent):
        def start(self):
            col = self.game_object.get_component(BoxCollider)
            if isinstance(col, Collider):
                print("It's a collider!")
"""

from __future__ import annotations

from enum import IntEnum

from Infernux.components.builtin_component import BuiltinComponent, CppProperty
from Infernux.components.serialized_field import FieldType
from Infernux.core.asset_ref import PhysicMaterialRef


class PhysicsMaterialCombine(IntEnum):
    """How two collider material values are combined for a contact."""

    Average = 0
    Minimum = 1
    Multiply = 2
    Maximum = 3


def _wrap_physic_material(native) -> PhysicMaterialRef:
    if native is None:
        return PhysicMaterialRef()
    ref = PhysicMaterialRef(guid=native.guid, path_hint=native.file_path)
    from Infernux.core.physic_material import PhysicMaterial
    ref._cached = PhysicMaterial(native)
    return ref


def _unwrap_physic_material(value):
    if value is None:
        return None
    if isinstance(value, PhysicMaterialRef):
        if not value.guid and not value.path_hint:
            return None
        resolved = value.resolve()
        if resolved is None:
            raise ValueError(f"PhysicMaterial reference cannot be resolved: {value.guid}")
        return resolved.native
    from Infernux.core.physic_material import PhysicMaterial
    if isinstance(value, PhysicMaterial):
        return value.native
    from Infernux.lib import InxPhysicMaterial as NativePhysicMaterial
    if isinstance(value, NativePhysicMaterial):
        return value
    raise TypeError("physic_material must be PhysicMaterial, PhysicMaterialRef, or None")


class Collider(BuiltinComponent):
    """Abstract Python base for all collider wrappers (mirrors Unity's Collider).

    Subclasses must still set ``_cpp_type_name`` to their concrete C++ type
    (e.g. ``"BoxCollider"``).  This class itself is **not** registered in
    ``_builtin_registry`` because ``_cpp_type_name`` is left empty.
    """

    # Not a concrete component — don't register
    _cpp_type_name = ""

    _component_category_ = "Physics"

    # Always-draw flag inherited by subclasses
    _always_show = False

    # ---- Shared properties (common to all collider types) ----
    center = CppProperty(
        "center",
        FieldType.VEC3,
        default=None,
        tooltip="Center offset in local space",
    )
    is_trigger = CppProperty(
        "is_trigger",
        FieldType.BOOL,
        default=False,
        tooltip="Is this collider a trigger volume?",
    )
    physic_material = CppProperty(
        "physic_material",
        FieldType.ASSET,
        default=PhysicMaterialRef(),
        asset_type="PhysicMaterial",
        tooltip="Shared physics surface material.",
        get_converter=_wrap_physic_material,
        set_converter=_unwrap_physic_material,
    )
