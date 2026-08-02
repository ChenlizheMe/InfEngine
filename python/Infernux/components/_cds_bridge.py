"""
Component Data Store — Python bridge to C++ ComponentDataStore.

Provides a mapping from FieldType → CDS DataType and convenience functions
for registering InxComponent classes, allocating/releasing slots, and
single-element get/set through the C++ SoA store.

Numeric fields (INT, FLOAT, BOOL, VEC2, VEC3, VEC4) are backed by C++
SoA arrays for cache-friendly batch access.  Non-numeric fields (STRING,
GAME_OBJECT, MATERIAL, …) remain in the Python-side dict.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .serialized_field import FieldType

# Lazy-loaded C++ module reference.
_lib = None


def _get_lib():
    global _lib
    if _lib is None:
        from Infernux import lib as _l
        _lib = _l
    return _lib


# ── FieldType → CDS DataType code mapping ──────────────────────────────

# Must match ComponentDataStore::DataType enum order.
_CDS_FLOAT64 = 0
_CDS_INT64 = 1
_CDS_BOOL = 2
_CDS_VEC2 = 3
_CDS_VEC3 = 4
_CDS_VEC4 = 5

_FIELD_TYPE_TO_CDS: Dict[str, int] = {}  # populated lazily


def _get_field_type_map() -> Dict[str, int]:
    """Return FieldType.name → CDS type code mapping (lazy init)."""
    if not _FIELD_TYPE_TO_CDS:
        from .serialized_field import FieldType as FT
        _FIELD_TYPE_TO_CDS.update({
            FT.INT.name: _CDS_INT64,
            FT.FLOAT.name: _CDS_FLOAT64,
            FT.BOOL.name: _CDS_BOOL,
            FT.VEC2.name: _CDS_VEC2,
            FT.VEC3.name: _CDS_VEC3,
            FT.VEC4.name: _CDS_VEC4,
        })
    return _FIELD_TYPE_TO_CDS


def is_cds_backed(field_type) -> bool:
    """Return True if the given FieldType is stored in the C++ SoA store."""
    m = _get_field_type_map()
    return field_type.name in m


def cds_type_code(field_type) -> int:
    """Return the C++ DataType code for a FieldType, or raise."""
    m = _get_field_type_map()
    code = m.get(field_type.name)
    if code is None:
        raise TypeError(f"FieldType.{field_type.name} is not CDS-backed")
    return code


# ── Per-class registry ──────────────────────────────────────────────────

# stable component type GUID + layout revision -> CDS field registration
_class_registry: Dict[str, tuple] = {}


def _class_key(cls) -> str:
    fields = getattr(cls, "_serialized_fields_", {})
    layout = []
    for name, metadata in sorted(fields.items()):
        field_type = getattr(getattr(metadata, "field_type", None), "name", "")
        element_type = getattr(getattr(metadata, "element_type", None), "name", "")
        enum_type = getattr(metadata, "enum_type", None)
        serializable_type = getattr(metadata, "serializable_class", None)
        layout.append((
            name,
            field_type,
            element_type,
            f"{getattr(enum_type, '__module__', '')}:{getattr(enum_type, '__qualname__', '')}",
            f"{getattr(serializable_type, '__module__', '')}:{getattr(serializable_type, '__qualname__', '')}",
        ))
    revision = hashlib.sha256(repr(layout).encode("utf-8")).hexdigest()[:16]
    return f"{cls._get_type_guid()}@{revision}"


def register_class(cls) -> Optional[int]:
    """Register an InxComponent subclass with the C++ ComponentDataStore.

    Called from InxComponent.__init_subclass__.
    Only registers if the class has at least one CDS-backed numeric field.
    Returns the class_id or None if no numeric fields.
    """
    key = _class_key(cls)
    if key in _class_registry:
        return _class_registry[key][0]

    fields_meta = getattr(cls, '_serialized_fields_', {})
    if not fields_meta:
        return None

    numeric_fields = {}
    for fname, meta in fields_meta.items():
        if is_cds_backed(meta.field_type):
            numeric_fields[fname] = meta

    if not numeric_fields:
        return None

    lib = _get_lib()
    class_id = lib._cds_register_class(key)
    field_map = {}
    for fname, meta in numeric_fields.items():
        tc = cds_type_code(meta.field_type)
        fid = lib._cds_register_field(class_id, fname, tc)
        field_map[fname] = (fid, tc)

    _class_registry[key] = (class_id, field_map)

    # Stamp CDS metadata on each numeric descriptor for fast __get__/__set__.
    for fname, (fid, tc) in field_map.items():
        descriptor = cls.__dict__.get(fname)
        if descriptor is not None and hasattr(descriptor, 'metadata'):
            descriptor._cds_class_id = class_id
            descriptor._cds_field_id = fid
            descriptor._cds_type_code = tc

    return class_id


def get_class_info(cls):
    """Return (class_id, field_map) or None."""
    return _class_registry.get(_class_key(cls))


# ── Slot management ─────────────────────────────────────────────────────

def allocate_slot(cls) -> Optional[tuple[int, int]]:
    """Allocate a CDS slot for a new component instance. Returns slot or None."""
    info = _class_registry.get(_class_key(cls))
    if info is None:
        return None
    lib = _get_lib()
    return lib._cds_alloc(info[0])


def get_class_id(cls) -> Optional[int]:
    """Return the CDS class_id for *cls*, or None if not registered."""
    info = _class_registry.get(_class_key(cls))
    if info is None:
        return None
    return info[0]


def reserve_class(cls, capacity: int) -> None:
    """Reserve numeric-field storage for at least *capacity* live instances."""
    if type(capacity) is not int or capacity < 0:
        raise ValueError("CDS reserve capacity must be a non-negative integer")
    info = _class_registry.get(_class_key(cls))
    if info is None:
        register_class(cls)
        info = _class_registry.get(_class_key(cls))
    if info is None:
        raise TypeError(f"{cls.__qualname__} has no CDS-backed numeric fields")
    _get_lib()._cds_reserve(info[0], capacity)


def release_slot(cls, slot: tuple[int, int], class_id: Optional[int] = None) -> None:
    """Release a CDS slot once, using the instance's allocation class ID."""
    if class_id is None:
        info = _class_registry.get(_class_key(cls))
        if info is None:
            return
        class_id = info[0]
    if slot is None:
        return
    lib = _get_lib()
    if lib._cds_is_alive(class_id, slot):
        lib._cds_free(class_id, slot)


# ── Single-element access (called from SerializedFieldDescriptor) ───────

def cds_get(class_id: int, field_id: int, type_code: int, slot: tuple[int, int]) -> Any:
    """Read one value from the C++ store."""
    lib = _get_lib()
    raw = lib._cds_get(class_id, field_id, slot, type_code)
    # For vector types, _cds_get returns a tuple — wrap in the engine type.
    if type_code == _CDS_VEC2:
        return lib.Vector2(raw[0], raw[1])
    if type_code == _CDS_VEC3:
        return lib.Vector3(raw[0], raw[1], raw[2])
    if type_code == _CDS_VEC4:
        return lib.vec4f(raw[0], raw[1], raw[2], raw[3])
    return raw


def cds_set(class_id: int, field_id: int, type_code: int, slot: tuple[int, int], value: Any) -> None:
    """Write one value to the C++ store."""
    lib = _get_lib()
    lib._cds_set(class_id, field_id, slot, type_code, value)
