"""Field-schema migration contracts shared by script reload and CDS storage.

This module is deliberately mutation-free.  It validates and prepares an
entire class migration before the owner transaction allocates native slots or
changes a live class body.  Native allocation, descriptor publication and old
slot retirement are separate commit-edge operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .fields import (
    FieldMetadata,
    FieldType,
    SerializedFieldDescriptor,
    copy_serialized_field_default,
    get_serialized_fields,
)


class FieldSchemaMigrationError(RuntimeError):
    """Raised before publication when a schema transition is ambiguous or unsafe."""


@dataclass(frozen=True)
class FieldMigration:
    target_name: str
    target: FieldMetadata
    source_name: str | None = None
    source: FieldMetadata | None = None
    conversion: str = "default"


@dataclass(frozen=True)
class ClassSchemaMigration:
    target_type: type
    candidate_type: type
    fields: tuple[FieldMigration, ...]
    removed_fields: tuple[str, ...]

    @property
    def changed(self) -> bool:
        old_fields = get_serialized_fields(self.target_type)
        new_fields = get_serialized_fields(self.candidate_type)
        if tuple(old_fields) != tuple(new_fields):
            return True
        return any(
            _storage_signature(old_fields[name]) != _storage_signature(new_fields[name])
            for name in old_fields
        )


def _type_identity(value: Any) -> tuple[str, str]:
    return (
        str(getattr(value, "__module__", "")),
        str(getattr(value, "__qualname__", "")),
    )


def _storage_signature(metadata: FieldMetadata) -> tuple[Any, ...]:
    """Return only data-layout semantics; Inspector presentation is excluded."""
    return (
        metadata.field_type,
        metadata.element_type,
        _type_identity(metadata.element_class),
        _type_identity(metadata.serializable_class),
        _type_identity(metadata.enum_type),
        str(metadata.component_type or ""),
        str(metadata.asset_type or ""),
    )


def _conversion(source: FieldMetadata, target: FieldMetadata) -> str:
    if _storage_signature(source) == _storage_signature(target):
        return "identity"
    if source.field_type is FieldType.INT and target.field_type is FieldType.FLOAT:
        return "int_to_float"
    raise FieldSchemaMigrationError(
        f"serialized field type change is not supported: "
        f"{source.name} ({source.field_type.name}) -> "
        f"{target.name} ({target.field_type.name})"
    )


def build_class_schema_migration(
    target_type: type,
    candidate_type: type,
) -> ClassSchemaMigration:
    """Validate one stable-class schema transition without mutating either class."""
    old_fields = get_serialized_fields(target_type)
    new_fields = get_serialized_fields(candidate_type)
    claimed_sources: set[str] = set()
    migrations: list[FieldMigration] = []

    for target_name, target in new_fields.items():
        source_name: str | None = target_name if target_name in old_fields else None
        if source_name is None:
            declared_matches = [
                name for name in target.former_names
                if name in old_fields
            ]
            if len(declared_matches) > 1:
                raise FieldSchemaMigrationError(
                    f"field '{target_name}' has multiple live migration sources: {declared_matches}"
                )
            source_name = declared_matches[0] if declared_matches else None

        if source_name is None:
            migrations.append(FieldMigration(target_name, target))
            continue
        if source_name in claimed_sources:
            raise FieldSchemaMigrationError(
                f"live field '{source_name}' is claimed by more than one candidate field"
            )
        claimed_sources.add(source_name)
        source = old_fields[source_name]
        migrations.append(
            FieldMigration(
                target_name,
                target,
                source_name,
                source,
                _conversion(source, target),
            )
        )

    removed = tuple(name for name in old_fields if name not in claimed_sources)
    return ClassSchemaMigration(target_type, candidate_type, tuple(migrations), removed)


def collect_live_instances(component_type: type) -> tuple[object, ...]:
    """Return each currently registered live instance of exactly ``component_type`` once."""
    registry = getattr(component_type, "_active_instances", {})
    found: list[object] = []
    seen: set[int] = set()
    for values in tuple(registry.values()):
        for instance in tuple(values):
            token = id(instance)
            if token in seen or type(instance) is not component_type:
                continue
            if bool(getattr(instance, "_is_destroyed", False)):
                continue
            seen.add(token)
            found.append(instance)
    return tuple(found)


def _descriptor(owner: type, name: str) -> SerializedFieldDescriptor | None:
    for base in owner.__mro__:
        value = base.__dict__.get(name)
        if isinstance(value, SerializedFieldDescriptor):
            return value
    return None


def _convert_value(value: Any, conversion: str) -> Any:
    if conversion == "int_to_float":
        return float(value)
    if conversion == "identity":
        return value
    raise FieldSchemaMigrationError(f"unknown field conversion: {conversion}")


def prepare_instance_values(
    migration: ClassSchemaMigration,
    instances: Iterable[object] | None = None,
) -> Mapping[object, dict[str, Any]]:
    """Capture all instance values before any live descriptor is replaced.

    New defaults are copied separately for every instance.  Existing reference,
    list and SerializableObject values retain their authored identity for an
    identity migration; the eventual descriptor normalization remains the
    single authority for storage representation.
    """
    selected = collect_live_instances(migration.target_type) if instances is None else tuple(instances)
    prepared: dict[object, dict[str, Any]] = {}
    for instance in selected:
        if type(instance) is not migration.target_type:
            raise FieldSchemaMigrationError(
                f"migration instance has unexpected type: {type(instance).__qualname__}"
            )
        values: dict[str, Any] = {}
        for field in migration.fields:
            if field.source_name is None:
                values[field.target_name] = copy_serialized_field_default(field.target)
                continue
            descriptor = _descriptor(migration.target_type, field.source_name)
            if descriptor is None:
                raise FieldSchemaMigrationError(
                    f"live field descriptor is unavailable: {field.source_name}"
                )
            values[field.target_name] = _convert_value(
                descriptor.get_raw(instance),
                field.conversion,
            )
        prepared[instance] = values
    return prepared
