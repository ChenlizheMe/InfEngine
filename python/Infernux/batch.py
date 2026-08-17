"""
Batch read/write API for high-performance data transfer between
engine objects and numpy arrays.

Usage::

    from Infernux.batch import batch_read, batch_write

    # Read world positions from a list of transforms → numpy (N, 3)
    positions = batch_read(transforms, 'position')

    # Run a JIT kernel
    @njit(parallel=True)
    def gravity(pos, dt):
        for i in prange(len(pos)):
            pos[i, 1] -= 9.8 * dt
    gravity(positions, delta_time)

    # Write back
    batch_write(transforms, positions, 'position')

Supported Transform properties:
    'position', 'local_position', 'local_scale',
    'euler_angles', 'local_euler_angles',
    'rotation', 'local_rotation'

For InxComponent subclass fields (int, float, Vector3, etc.):
    batch_read(components, 'velocity')
    batch_read(components, MyComponent.velocity)   # descriptor form
"""

from __future__ import annotations

from typing import Any, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

# NumPy is a required engine runtime dependency. Import it on first batch API
# use so importing this module itself stays lightweight.
_np = None


def _get_np():
    global _np
    if _np is None:
        import numpy
        _np = numpy
    return _np

# Lazy import to avoid circular deps at module level.
_lib = None


def _get_lib():
    global _lib
    if _lib is None:
        from Infernux import lib as _l
        _lib = _l
    return _lib


# ── Property name resolution ────────────────────────────────────────────

def _resolve_property_name(prop: Any) -> str:
    """Accept either a string or a descriptor and return the property name."""
    if isinstance(prop, str):
        return prop
    # SerializedFieldDescriptor (InxComponent fields)
    if hasattr(prop, 'metadata') and hasattr(prop.metadata, 'name'):
        return prop.metadata.name
    # pybind11 property descriptors don't have a nice .name — fall back
    raise TypeError(
        f"Cannot resolve property from {type(prop).__name__!r}. "
        f"Pass a string like 'position' or a SerializedFieldDescriptor."
    )


# ── Shape/dtype helpers ─────────────────────────────────────────────────

# Transform properties backed by C++ fast path.
_TRANSFORM_VEC3_PROPS = frozenset({
    'position', 'local_position', 'local_scale',
    'euler_angles', 'local_euler_angles',
})
_TRANSFORM_QUAT_PROPS = frozenset({
    'rotation', 'local_rotation',
})
_TRANSFORM_ALL_PROPS = _TRANSFORM_VEC3_PROPS | _TRANSFORM_QUAT_PROPS


def _is_transform_list(targets: Sequence) -> bool:
    """Check if the first element is a Transform (C++ pybind11 type)."""
    if not targets:
        return False
    lib = _get_lib()
    return isinstance(targets[0], lib.Transform)


# ── ComponentDataStore fast path ────────────────────────────────────────

def _try_cds_gather(targets: Sequence, prop_name: str):
    """Attempt CDS C++ fast-path gather.  Returns ndarray or None."""
    from Infernux.components._cds_bridge import get_class_info
    cls = type(targets[0])
    info = get_class_info(cls)
    if info is None:
        return None
    class_id, field_map = info
    entry = field_map.get(prop_name)
    if entry is None:
        return None
    field_id, type_code = entry

    # Collect generational slot handles as an (N, 2) uint32 array.
    np = _get_np()
    slots = np.array([t._cds_slot for t in targets], dtype=np.uint32)
    lib = _get_lib()
    return np.asarray(lib._cds_batch_gather(class_id, field_id, type_code, slots))


def _try_cds_scatter(targets: Sequence, data: np.ndarray, prop_name: str) -> bool:
    """Attempt CDS C++ fast-path scatter.  Returns True if handled."""
    from Infernux.components._cds_bridge import get_class_info
    cls = type(targets[0])
    info = get_class_info(cls)
    if info is None:
        return False
    class_id, field_map = info
    entry = field_map.get(prop_name)
    if entry is None:
        return False
    field_id, type_code = entry

    np = _get_np()
    slots = np.array([t._cds_slot for t in targets], dtype=np.uint32)
    lib = _get_lib()
    lib._cds_batch_scatter(class_id, field_id, type_code, slots, data)
    return True


# ── FieldType → numpy dtype/shape mapping ───────────────────────────────

def _field_shape_dtype(field_type) -> tuple[tuple[int, ...], "np.dtype"]:
    """Return (per-element shape, dtype) for a FieldType enum."""
    from Infernux.components.fields import FieldType
    np = _get_np()
    _map = {
        FieldType.INT:   ((), np.dtype('int64')),
        FieldType.FLOAT: ((), np.dtype('float64')),
        FieldType.BOOL:  ((), np.dtype('bool')),
        FieldType.VEC2:  ((2,), np.dtype('float32')),
        FieldType.VEC3:  ((3,), np.dtype('float32')),
        FieldType.VEC4:  ((4,), np.dtype('float32')),
    }
    result = _map.get(field_type)
    if result is None:
        raise TypeError(
            f"batch_read/batch_write does not support FieldType.{field_type.name}. "
            f"Only numeric types (INT, FLOAT, BOOL, VEC2, VEC3, VEC4) are supported."
        )
    return result


# ── Component (Python) slow path ────────────────────────────────────────

def _component_gather(targets: Sequence, prop_name: str) -> np.ndarray:
    """Gather a named attribute from a sequence of InxComponent instances."""
    from Infernux.components.fields import FieldType

    # Look up metadata from the class
    cls = type(targets[0])
    meta = cls._serialized_fields_.get(prop_name)  # type: ignore[attr-defined]
    if meta is None:
        raise AttributeError(
            f"{cls.__name__} has no serialized field '{prop_name}'"
        )

    elem_shape, dtype = _field_shape_dtype(meta.field_type)
    np = _get_np()
    n = len(targets)
    full_shape = (n, *elem_shape) if elem_shape else (n,)
    out = np.empty(full_shape, dtype=dtype)

    for i, obj in enumerate(targets):
        val = getattr(obj, prop_name)
        if elem_shape:
            # Vector type — extract components
            if meta.field_type == FieldType.VEC2:
                out[i, 0] = val.x
                out[i, 1] = val.y
            elif meta.field_type == FieldType.VEC3:
                out[i, 0] = val.x
                out[i, 1] = val.y
                out[i, 2] = val.z
            elif meta.field_type == FieldType.VEC4:
                out[i, 0] = val.x
                out[i, 1] = val.y
                out[i, 2] = val.z
                out[i, 3] = val.w
        else:
            out[i] = val

    return out


def _component_scatter(targets: Sequence, data: np.ndarray, prop_name: str) -> None:
    """Scatter a numpy array back into named attributes of InxComponent instances."""
    from Infernux.components.fields import FieldType

    cls = type(targets[0])
    meta = cls._serialized_fields_.get(prop_name)  # type: ignore[attr-defined]
    if meta is None:
        raise AttributeError(
            f"{cls.__name__} has no serialized field '{prop_name}'"
        )

    n = len(targets)
    lib = _get_lib()

    for i, obj in enumerate(targets):
        if meta.field_type == FieldType.VEC2:
            setattr(obj, prop_name, lib.Vector2(float(data[i, 0]), float(data[i, 1])))
        elif meta.field_type == FieldType.VEC3:
            setattr(obj, prop_name, lib.Vector3(float(data[i, 0]), float(data[i, 1]), float(data[i, 2])))
        elif meta.field_type == FieldType.VEC4:
            setattr(obj, prop_name, lib.vec4f(float(data[i, 0]), float(data[i, 1]), float(data[i, 2]), float(data[i, 3])))
        else:
            setattr(obj, prop_name, data[i].item())


# ── Public API ──────────────────────────────────────────────────────────

def batch_read(targets: Sequence, prop: Any) -> NDArray:
    """Read a property from all *targets* into a numpy array.

    Parameters
    ----------
    targets : list[Transform] | list[InxComponent] | TransformBatchHandle
        Homogeneous list of engine objects, or a pre-built
        ``TransformBatchHandle`` for zero-overhead repeated reads.
    prop : str | descriptor
        Property name (``'position'``, ``'velocity'``) or a class-level
        descriptor (``MyComponent.velocity``).

    Returns
    -------
    numpy.ndarray
        Shape depends on the property type:
        - ``int`` / ``float`` / ``bool`` → ``(N,)``
        - ``Vector2`` → ``(N, 2)``
        - ``Vector3`` → ``(N, 3)``
        - ``quatf`` / ``vec4f`` → ``(N, 4)``
    """
    prop_name = _resolve_property_name(prop)

    # Handle-based fast path (cached Transform pointers).
    lib = _get_lib()
    if isinstance(targets, lib.TransformBatchHandle):
        if prop_name in _TRANSFORM_ALL_PROPS:
            return lib._transform_batch_read(targets, prop_name)
        raise ValueError(
            f"Unknown Transform property '{prop_name}'. "
            f"Supported: {sorted(_TRANSFORM_ALL_PROPS)}"
        )

    if _is_transform_list(targets):
        if prop_name in _TRANSFORM_ALL_PROPS:
            return lib._transform_batch_read(targets, prop_name)
        raise ValueError(
            f"Unknown Transform property '{prop_name}'. "
            f"Supported: {sorted(_TRANSFORM_ALL_PROPS)}"
        )

    # Try CDS C++ fast path for InxComponent numeric fields.
    result = _try_cds_gather(targets, prop_name)
    if result is not None:
        return result

    # Fallback: Python getattr loop.
    return _component_gather(targets, prop_name)


def batch_write(targets: Sequence, data: NDArray, prop: Any):
    """Write a numpy array back to a property on all *targets*.

    Parameters
    ----------
    targets : list[Transform] | list[InxComponent] | TransformBatchHandle
        Same list used for the preceding ``batch_read``, or a
        ``TransformBatchHandle``.
    data : numpy.ndarray
        Array with ``data.shape[0] >= len(targets)``.
    prop : str | descriptor
        Same property specifier used for ``batch_read``.
    """
    prop_name = _resolve_property_name(prop)

    # Handle-based fast path.
    lib = _get_lib()
    if isinstance(targets, lib.TransformBatchHandle):
        if prop_name in _TRANSFORM_ALL_PROPS:
            return lib._transform_batch_write(targets, data, prop_name)
        raise ValueError(
            f"Unknown Transform property '{prop_name}'. "
            f"Supported: {sorted(_TRANSFORM_ALL_PROPS)}"
        )

    if _is_transform_list(targets):
        if prop_name in _TRANSFORM_ALL_PROPS:
            lib._transform_batch_write(targets, data, prop_name)
            return
        raise ValueError(
            f"Unknown Transform property '{prop_name}'. "
            f"Supported: {sorted(_TRANSFORM_ALL_PROPS)}"
        )

    # Try CDS C++ fast path for InxComponent numeric fields.
    if _try_cds_scatter(targets, data, prop_name):
        return

    # Fallback: Python setattr loop.
    _component_scatter(targets, data, prop_name)


def create_batch_handle(targets: list, *, mode: str = "strict") -> "TransformBatchHandle":
    """Create a validated ``TransformBatchHandle`` for *targets*.

    Re-use the handle across ``batch_read`` / ``batch_write`` calls to avoid
    repeated pybind11 extraction overhead. Every operation validates the
    stored ECS generations and scene world IDs. ``mode="strict"`` rejects any
    stale transform. ``mode="compact"`` skips stale entries and makes reads
    return ``(values, valid_mask)`` while writes return ``valid_mask``.
    """
    lib = _get_lib()
    modes = {
        "strict": lib.TransformBatchMode.STRICT,
        "compact": lib.TransformBatchMode.COMPACT,
    }
    try:
        validation_mode = modes[mode]
    except KeyError as exc:
        raise ValueError("batch handle mode must be 'strict' or 'compact'") from exc
    return lib.TransformBatchHandle(targets, validation_mode)


def create_scene_batch_handle(scene, *, name_prefix: str = "", mode: str = "strict") -> "TransformBatchHandle":
    """Create a Transform batch from native Scene filtering.

    This avoids materializing every matching GameObject and Transform in Python,
    which is important for large authored scenes and Play Mode startup.
    """
    if scene is None:
        raise ValueError("scene is required")
    if not isinstance(name_prefix, str):
        raise TypeError("name_prefix must be a string")
    lib = _get_lib()
    modes = {
        "strict": lib.TransformBatchMode.STRICT,
        "compact": lib.TransformBatchMode.COMPACT,
    }
    try:
        validation_mode = modes[mode]
    except KeyError as exc:
        raise ValueError("batch handle mode must be 'strict' or 'compact'") from exc
    return lib._create_scene_transform_batch_handle(scene, name_prefix, validation_mode)
