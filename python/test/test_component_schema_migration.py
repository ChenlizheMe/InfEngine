from __future__ import annotations

from typing import Annotated

import pytest

from Infernux.components import FormerlySerializedAs, InxComponent
from Infernux.components._cds_migration import (
    FieldSchemaMigrationError,
    build_class_schema_migration,
    prepare_instance_values,
)
from Infernux.components._component_registration import (
    candidate_component_registration_scope,
)


def _component(name: str, fields: dict[str, object]) -> type:
    namespace = {"__module__": f"schema_migration_{name}", **fields}
    with candidate_component_registration_scope():
        return type(name, (InxComponent,), namespace)


def test_schema_migration_adds_independent_defaults_and_preserves_values():
    target = _component(
        "AddFieldProbe",
        {"__annotations__": {"speed": float}, "speed": 2.0},
    )
    candidate = _component(
        "AddFieldProbe",
        {
            "__annotations__": {"speed": float, "tags": list[str]},
            "speed": 5.0,
            "tags": ["new"],
        },
    )
    first = target()
    second = target()
    first.speed = 9.0

    values = prepare_instance_values(
        build_class_schema_migration(target, candidate),
        (first, second),
    )

    assert values[first]["speed"] == pytest.approx(9.0)
    assert values[second]["speed"] == pytest.approx(2.0)
    assert values[first]["tags"] == ["new"]
    assert values[first]["tags"] is not values[second]["tags"]


def test_schema_migration_uses_explicit_previous_name_without_runtime_alias():
    target = _component(
        "RenameFieldProbe",
        {"__annotations__": {"speed": float}, "speed": 1.0},
    )
    candidate = _component(
        "RenameFieldProbe",
        {
            "__annotations__": {
                "velocity": Annotated[float, FormerlySerializedAs("speed")],
            },
            "velocity": 3.0,
        },
    )
    instance = target()
    instance.speed = 7.5

    migration = build_class_schema_migration(target, candidate)
    values = prepare_instance_values(migration, (instance,))

    assert migration.removed_fields == ()
    assert values[instance] == {"velocity": pytest.approx(7.5)}


def test_schema_migration_allows_int_to_float_widening_only():
    int_type = _component(
        "NumericWidenProbe",
        {"__annotations__": {"value": int}, "value": 2},
    )
    float_type = _component(
        "NumericWidenProbe",
        {"__annotations__": {"value": float}, "value": 2.0},
    )
    instance = int_type()
    instance.value = 13

    values = prepare_instance_values(
        build_class_schema_migration(int_type, float_type),
        (instance,),
    )
    assert values[instance]["value"] == pytest.approx(13.0)

    with pytest.raises(FieldSchemaMigrationError, match="not supported"):
        build_class_schema_migration(float_type, int_type)


def test_schema_migration_rejects_one_source_claimed_twice():
    target = _component(
        "AmbiguousRenameProbe",
        {"__annotations__": {"value": float}, "value": 1.0},
    )
    candidate = _component(
        "AmbiguousRenameProbe",
        {
            "__annotations__": {
                "first": Annotated[float, FormerlySerializedAs("value")],
                "second": Annotated[float, FormerlySerializedAs("value")],
            },
            "first": 1.0,
            "second": 2.0,
        },
    )

    with pytest.raises(FieldSchemaMigrationError, match="claimed"):
        build_class_schema_migration(target, candidate)


def test_retired_cds_slot_waits_for_old_runtime_epoch_then_drains(monkeypatch):
    import gc

    from Infernux.components import _cds_bridge
    from Infernux.engine.runtime_dispatch import (
        current_runtime_epoch,
        ensure_runtime_dispatch_types,
        publish_runtime_dispatch_epoch,
    )

    target = _component(
        "EpochRetirementProbe",
        {"__annotations__": {"value": float}, "value": 1.0},
    )
    ensure_runtime_dispatch_types((target,))
    old_epoch = current_runtime_epoch()
    class_id = 9001
    slot = (3, 7)
    freed = []

    class _Probe:
        def __init__(self):
            self.alive = {slot}

        def _cds_alloc(self, candidate_class_id):
            sentinel = (9, 11)
            self.alive.add(sentinel)
            return sentinel

        def _cds_is_alive(self, candidate_class_id, candidate_slot):
            return candidate_class_id == class_id and tuple(candidate_slot) in self.alive

        def _cds_free(self, candidate_class_id, candidate_slot):
            candidate_slot = tuple(candidate_slot)
            self.alive.discard(candidate_slot)
            freed.append((candidate_class_id, candidate_slot))

    key = _cds_bridge._class_key(target)
    previous = _cds_bridge._class_registry.get(key)
    _cds_bridge._class_registry[key] = (class_id, {})
    monkeypatch.setattr(_cds_bridge, "_lib", _Probe())
    try:
        reference = _cds_bridge._retain_layout_epoch(class_id, target)
        assert reference is not None
        _cds_bridge.release_slot(target, slot, class_id)
        assert freed == []

        publication = publish_runtime_dispatch_epoch((), retired_types=(target,))
        publication.commit()
        del publication
        del old_epoch
        gc.collect()
        assert freed == [(class_id, slot), (class_id, (9, 11))]
    finally:
        _cds_bridge._retired_layouts.pop(class_id, None)
        if previous is None:
            _cds_bridge._class_registry.pop(key, None)
        else:
            _cds_bridge._class_registry[key] = previous
