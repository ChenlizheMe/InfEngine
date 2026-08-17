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
import weakref
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .fields import FieldType

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
        from .fields import FieldType as FT
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


@dataclass
class _RetiredLayoutState:
    """Native old-layout references retained until every owning epoch dies."""

    epoch_refs: list[weakref.ReferenceType] = field(default_factory=list)
    pending_slots: list[tuple[int, int]] = field(default_factory=list)
    sentinel_slot: Optional[tuple[int, int]] = None


_retired_layouts: dict[int, _RetiredLayoutState] = {}


def _layout_has_live_epoch(state: _RetiredLayoutState) -> bool:
    state.epoch_refs[:] = [
        reference for reference in state.epoch_refs if reference() is not None
    ]
    return bool(state.epoch_refs)


def _release_slot_now(class_id: int, slot: tuple[int, int]) -> None:
    lib = _get_lib()
    if lib._cds_is_alive(int(class_id), slot):
        lib._cds_free(int(class_id), slot)


def _drain_retired_layout(class_id: int) -> None:
    state = _retired_layouts.get(int(class_id))
    if state is None or _layout_has_live_epoch(state):
        return
    for slot in tuple(state.pending_slots):
        _release_slot_now(int(class_id), slot)
    if state.sentinel_slot is not None:
        _release_slot_now(int(class_id), state.sentinel_slot)
    _retired_layouts.pop(int(class_id), None)


def _retain_layout_epoch(class_id: int, component_type: type):
    """Pin only the old epoch object, never the class or its layout state."""
    try:
        from Infernux.engine.runtime_dispatch import current_runtime_epoch

        epoch = current_runtime_epoch()
        if epoch.descriptor_for(component_type) is None:
            return None
        state = _retired_layouts.setdefault(int(class_id), _RetiredLayoutState())
        if state.sentinel_slot is None:
            try:
                state.sentinel_slot = tuple(_get_lib()._cds_alloc(int(class_id)))
            except AttributeError:
                # Lightweight Python probes may not expose native allocation.
                # They still exercise epoch ordering without a native sentinel.
                pass

        def on_epoch_collected(_reference, retired_class_id=int(class_id)):
            try:
                _drain_retired_layout(retired_class_id)
            except Exception:
                # A weakref callback must not surface errors during GC.
                pass

        reference = weakref.ref(epoch, on_epoch_collected)
        if all(existing() is not epoch for existing in state.epoch_refs):
            state.epoch_refs.append(reference)
        return reference
    except (ImportError, AttributeError, TypeError):
        return None


def _cancel_layout_epoch(class_id: int, reference) -> None:
    state = _retired_layouts.get(int(class_id))
    if state is None:
        return
    state.epoch_refs[:] = [
        item for item in state.epoch_refs if item is not reference and item() is not None
    ]
    _drain_retired_layout(int(class_id))


def _class_key(cls) -> str:
    fields = getattr(cls, "_serialized_fields_", {})
    layout = []
    for name, metadata in sorted(fields.items()):
        field_type_value = getattr(metadata, "field_type", None)
        if field_type_value is None or not is_cds_backed(field_type_value):
            continue
        field_type = getattr(getattr(metadata, "field_type", None), "name", "")
        layout.append((name, field_type))
    revision = hashlib.sha256(repr(layout).encode("utf-8")).hexdigest()[:16]
    return f"{cls._get_type_guid()}@{revision}"


def _numeric_fields(cls) -> dict[str, object]:
    return {
        name: metadata
        for name, metadata in getattr(cls, "_serialized_fields_", {}).items()
        if is_cds_backed(metadata.field_type)
    }


def _descriptor(cls, name: str):
    for base in cls.__mro__:
        value = base.__dict__.get(name)
        if value is not None and hasattr(value, "metadata"):
            return value
    return None


@dataclass
class _PreparedClassPublication:
    component_type: type
    key: str
    candidate_id: Optional[int]
    class_id: Optional[int]
    field_map: dict[str, tuple[int, int]]
    reused: bool = False


class CDSSchemaPublication:
    """One owner-side, multi-class CDS schema transaction.

    Candidate classes and migrated slots remain private until ``commit``.
    Descriptor metadata and the Python class registry are published only after
    the native transaction has sealed successfully.  A caller may therefore
    prepare every live instance before exposing any part of the new layout.
    """

    def __init__(self, component_types: Iterable[type]) -> None:
        self._lib = _get_lib()
        self._types = tuple(dict.fromkeys(component_types))
        self._transaction_id: Optional[int] = None
        self._entries: dict[type, _PreparedClassPublication] = {}
        self._registry_before: dict[str, tuple[bool, object]] = {}
        self._descriptor_before: list[tuple[object, tuple[object, object, object]]] = []
        self._retired_keys: set[str] = set()
        self._retired_epoch_holds: list[tuple[int, object]] = []
        self._allocated_slots: list[tuple[_PreparedClassPublication, tuple[int, int]]] = []
        self._sealed = False
        self._native_committed = False
        self._native_finalized = False
        self._publish_started = False
        self._committed = False
        self._rolled_back = False
        try:
            self._prepare()
        except Exception:
            self.rollback()
            raise

    @property
    def committed(self) -> bool:
        return self._committed and not self._rolled_back

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    @property
    def finalized(self) -> bool:
        return self._native_finalized

    def _begin(self) -> int:
        if self._transaction_id is None:
            self._transaction_id = int(self._lib._cds_schema_begin())
        return self._transaction_id

    def _prepare(self) -> None:
        for component_type in self._types:
            key = _class_key(component_type)
            numeric_fields = _numeric_fields(component_type)
            self._registry_before.setdefault(
                key,
                (key in _class_registry, _class_registry.get(key)),
            )
            existing = _class_registry.get(key)
            if not numeric_fields:
                entry = _PreparedClassPublication(
                    component_type, key, None, None, {}, reused=True
                )
                self._entries[component_type] = entry
                continue
            if existing is not None:
                class_id, field_map = existing
                entry = _PreparedClassPublication(
                    component_type,
                    key,
                    None,
                    int(class_id),
                    dict(field_map),
                    reused=True,
                )
                self._entries[component_type] = entry
                continue

            transaction_id = self._begin()
            candidate_id = int(
                self._lib._cds_schema_prepare_class(transaction_id, key)
            )
            field_map: dict[str, tuple[int, int]] = {}
            for name, metadata in numeric_fields.items():
                type_code = cds_type_code(metadata.field_type)
                field_id = int(
                    self._lib._cds_schema_prepare_field(
                        transaction_id,
                        candidate_id,
                        name,
                        type_code,
                    )
                )
                field_map[name] = (field_id, type_code)
            self._entries[component_type] = _PreparedClassPublication(
                component_type,
                key,
                candidate_id,
                None,
                field_map,
            )

    def entry(self, component_type: type) -> _PreparedClassPublication:
        try:
            return self._entries[component_type]
        except KeyError as exc:
            raise KeyError(
                f"component type is not part of this CDS transaction: "
                f"{component_type.__qualname__}"
            ) from exc

    def retire_published_layout(self, component_type: type) -> None:
        """Stage an old layout retirement without releasing its native slots."""
        key = _class_key(component_type)
        if key in {entry.key for entry in self._entries.values()}:
            return
        self._registry_before.setdefault(
            key,
            (key in _class_registry, _class_registry.get(key)),
        )
        previous = _class_registry.get(key)
        if previous is not None:
            reference = _retain_layout_epoch(int(previous[0]), component_type)
            if reference is not None:
                self._retired_epoch_holds.append((int(previous[0]), reference))
        self._retired_keys.add(key)

    def reserve(self, component_type: type, capacity: int) -> None:
        entry = self.entry(component_type)
        if entry.candidate_id is None:
            return
        self._lib._cds_schema_reserve(
            self._transaction_id,
            entry.candidate_id,
            int(capacity),
        )

    def allocate_slot(self, component_type: type) -> Optional[tuple[int, int]]:
        entry = self.entry(component_type)
        if entry.class_id is not None and entry.reused:
            return None
        if entry.candidate_id is None:
            return None
        slot = tuple(
            self._lib._cds_schema_alloc(
                self._transaction_id,
                entry.candidate_id,
            )
        )
        self._allocated_slots.append((entry, slot))
        return slot

    def migrate_slot(
        self,
        component_type: type,
        source_class_id: int,
        source_slot: tuple[int, int],
        field_map: Iterable[tuple[int, int]],
    ) -> tuple[int, int]:
        entry = self.entry(component_type)
        if entry.candidate_id is None:
            raise RuntimeError("CDS slot migration requires a prepared numeric layout")
        slot = tuple(
            self._lib._cds_schema_migrate_slot(
                self._transaction_id,
                entry.candidate_id,
                int(source_class_id),
                source_slot,
                tuple(field_map),
            )
        )
        self._allocated_slots.append((entry, slot))
        return slot

    def set_value(
        self,
        component_type: type,
        field_name: str,
        slot: tuple[int, int],
        value: Any,
    ) -> None:
        entry = self.entry(component_type)
        if entry.candidate_id is None:
            raise RuntimeError("CDS prepared value write requires a candidate layout")
        field_id, type_code = entry.field_map[field_name]
        self._lib._cds_schema_set(
            self._transaction_id,
            entry.candidate_id,
            field_id,
            slot,
            type_code,
            value,
        )

    def seal(self) -> dict[type, Optional[int]]:
        if self._rolled_back:
            raise RuntimeError("CDS schema transaction has been rolled back")
        if self._sealed:
            return {owner: entry.class_id for owner, entry in self._entries.items()}
        if self._transaction_id is not None:
            final_ids = dict(self._lib._cds_schema_seal(self._transaction_id))
            for entry in self._entries.values():
                if entry.candidate_id is not None:
                    value = final_ids.get(entry.candidate_id)
                    if value is None:
                        value = self._lib._cds_schema_final_class_id(
                            self._transaction_id,
                            entry.candidate_id,
                        )
                    if value is None:
                        raise RuntimeError(
                            f"native CDS did not seal candidate class '{entry.key}'"
                        )
                    entry.class_id = int(value)
        self._sealed = True
        return {owner: entry.class_id for owner, entry in self._entries.items()}

    def commit(self) -> dict[type, tuple[Optional[int], dict[str, tuple[int, int]]]]:
        if self._rolled_back:
            raise RuntimeError("CDS schema transaction has been rolled back")
        if self._committed:
            return self.class_info()
        self.seal()
        try:
            if self._transaction_id is not None:
                committed_ids = dict(
                    self._lib._cds_schema_commit(self._transaction_id)
                )
                self._native_committed = True
                for entry in self._entries.values():
                    if entry.candidate_id is not None:
                        value = committed_ids.get(entry.candidate_id)
                        if value is None or int(value) != entry.class_id:
                            raise RuntimeError(
                                f"native CDS committed an unexpected class ID for '{entry.key}'"
                            )

            self._publish_started = True
            for entry in self._entries.values():
                if entry.class_id is not None:
                    _class_registry[entry.key] = (
                        entry.class_id,
                        dict(entry.field_map),
                    )
                for name, (field_id, type_code) in entry.field_map.items():
                    descriptor = _descriptor(entry.component_type, name)
                    if descriptor is None:
                        raise RuntimeError(
                            f"serialized descriptor '{entry.component_type.__qualname__}.{name}' "
                            "is unavailable during CDS publication"
                        )
                    self._descriptor_before.append(
                        (
                            descriptor,
                            (
                                descriptor._cds_class_id,
                                descriptor._cds_field_id,
                                descriptor._cds_type_code,
                            ),
                        )
                    )
                    descriptor._cds_class_id = entry.class_id
                    descriptor._cds_field_id = field_id
                    descriptor._cds_type_code = type_code
            for key in self._retired_keys:
                _class_registry.pop(key, None)
            self._committed = True
            return self.class_info()
        except Exception:
            self.rollback()
            raise

    def class_info(self) -> dict[type, tuple[Optional[int], dict[str, tuple[int, int]]]]:
        return {
            owner: (entry.class_id, dict(entry.field_map))
            for owner, entry in self._entries.items()
        }

    def finalize(self) -> None:
        """Make a committed native layout irreversible at the owner commit edge."""
        if self._rolled_back:
            raise RuntimeError("CDS schema transaction has been rolled back")
        if self._native_finalized:
            return
        if not self._committed:
            raise RuntimeError("CDS schema transaction must commit before finalization")
        if self._transaction_id is not None:
            if not self._native_committed:
                raise RuntimeError("CDS schema transaction must commit before finalization")
            if self._lib._cds_schema_finalize(self._transaction_id) is False:
                raise RuntimeError("native CDS rejected schema finalization")
        self._native_finalized = True

    def rollback(self) -> None:
        if self._rolled_back:
            return
        if self._native_finalized:
            raise RuntimeError("finalized CDS schema transaction cannot be rolled back")
        rollback_errors: list[str] = []
        if self._native_committed and not self._native_finalized:
            # Rolling back the committed native schema destroys its complete
            # candidate storage, including every migrated/allocated slot.  A
            # per-slot free here is redundant and creates a partial-failure
            # surface before the all-or-nothing native rollback.
            try:
                if self._lib._cds_schema_rollback(self._transaction_id) is False:
                    rollback_errors.append("native CDS rejected committed schema rollback")
            except Exception as exc:
                rollback_errors.append(f"native CDS committed schema rollback failed: {exc}")
        if self._publish_started:
            for descriptor, previous in reversed(self._descriptor_before):
                (
                    descriptor._cds_class_id,
                    descriptor._cds_field_id,
                    descriptor._cds_type_code,
                ) = previous
            for key, (present, value) in self._registry_before.items():
                if present:
                    _class_registry[key] = value
                else:
                    _class_registry.pop(key, None)
        for class_id, reference in self._retired_epoch_holds:
            _cancel_layout_epoch(class_id, reference)
        self._retired_epoch_holds.clear()
        if not self._native_committed and self._transaction_id is not None:
            try:
                if self._lib._cds_schema_active(self._transaction_id):
                    if self._lib._cds_schema_rollback(self._transaction_id) is False:
                        rollback_errors.append("native CDS rejected prepared schema rollback")
            except Exception as exc:
                rollback_errors.append(f"native CDS prepared schema rollback failed: {exc}")
        self._committed = False
        if rollback_errors:
            raise RuntimeError("; ".join(rollback_errors))
        self._rolled_back = True


def prepare_schema_publication(
    component_types: Iterable[type],
) -> CDSSchemaPublication:
    """Prepare one private multi-class schema transaction for an owner safe point."""
    return CDSSchemaPublication(component_types)


def register_class(cls) -> Optional[int]:
    """Register an InxComponent subclass with the C++ ComponentDataStore.

    Called from InxComponent.__init_subclass__.
    Only registers if the class has at least one CDS-backed numeric field.
    Returns the class_id or None if no numeric fields.
    """
    from ._component_registration import stage_candidate_component_type

    if stage_candidate_component_type(cls):
        return None
    return publish_class(cls)


def publish_class(cls) -> Optional[int]:
    """Register a class during owner commit, bypassing candidate deferral."""
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
    from ._component_registration import is_component_registration_pending

    if is_component_registration_pending(cls):
        return None
    return _class_registry.get(_class_key(cls))


# ── Slot management ─────────────────────────────────────────────────────

def allocate_slot(cls) -> Optional[tuple[int, int]]:
    """Allocate a CDS slot for a new component instance. Returns slot or None."""
    from ._component_registration import is_component_registration_pending

    if is_component_registration_pending(cls):
        return None
    info = _class_registry.get(_class_key(cls))
    if info is None:
        return None
    lib = _get_lib()
    return lib._cds_alloc(info[0])


def get_class_id(cls) -> Optional[int]:
    """Return the CDS class_id for *cls*, or None if not registered."""
    from ._component_registration import is_component_registration_pending

    if is_component_registration_pending(cls):
        return None
    info = _class_registry.get(_class_key(cls))
    if info is None:
        return None
    return info[0]


def reserve_class(cls, capacity: int) -> None:
    """Reserve numeric-field storage for at least *capacity* live instances."""
    if type(capacity) is not int or capacity < 0:
        raise ValueError("CDS reserve capacity must be a non-negative integer")
    from ._component_registration import is_component_registration_pending

    if is_component_registration_pending(cls):
        return
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
    class_id = int(class_id)
    _drain_retired_layout(class_id)
    state = _retired_layouts.get(class_id)
    if state is not None and _layout_has_live_epoch(state):
        state.pending_slots.append(tuple(slot))
        return
    _release_slot_now(class_id, tuple(slot))


def allocate_published_slot(class_id: int) -> tuple[int, int]:
    """Allocate from an already published layout for transactional rollback."""
    return tuple(_get_lib()._cds_alloc(int(class_id)))


def published_slot_is_alive(class_id: int, slot: tuple[int, int]) -> bool:
    return bool(_get_lib()._cds_is_alive(int(class_id), slot))


def set_published_value(
    class_id: int,
    field_id: int,
    type_code: int,
    slot: tuple[int, int],
    value: Any,
) -> None:
    _get_lib()._cds_set(
        int(class_id),
        int(field_id),
        slot,
        int(type_code),
        value,
    )


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
