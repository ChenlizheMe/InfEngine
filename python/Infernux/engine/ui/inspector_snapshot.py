"""Revision-driven data snapshots for the native Inspector.

The service deliberately stores no component values.  Runtime and authoring
models remain authoritative; this module only answers whether a target,
schema, value, or preview dependency changed since the previous draw plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Iterable


class InspectorRevisionLayer(str, Enum):
    TARGET = "target"
    SCHEMA = "schema"
    VALUE = "value"
    PREVIEW = "preview"


@dataclass(frozen=True, slots=True)
class InspectorTarget:
    kind: str
    identity: str

    @classmethod
    def none(cls) -> "InspectorTarget":
        return cls("none", "")

    @classmethod
    def scene_object(cls, object_id: int) -> "InspectorTarget":
        value = int(object_id or 0)
        return cls("scene_object", str(value)) if value > 0 else cls.none()

    @classmethod
    def asset(cls, path: str) -> "InspectorTarget":
        if not path:
            return cls.none()
        from Infernux.engine.path_utils import lexical_path_key

        return cls("asset", lexical_path_key(path))


@dataclass(frozen=True, slots=True)
class InspectorRevisionSnapshot:
    """Small immutable revision packet suitable for one native UI frame."""

    target: InspectorTarget
    target_revision: int
    schema_revision: int
    value_revision: int
    preview_revision: int

    def token(self) -> tuple[int, int, int, int]:
        return (
            self.target_revision,
            self.schema_revision,
            self.value_revision,
            self.preview_revision,
        )


def target_for_component(component) -> InspectorTarget:
    try:
        # Component.game_object is a lifecycle-checked public property.  A
        # snapshot lookup can legitimately race an unbind/retirement window,
        # so prefer the passive owner reference and only use the public shape
        # for lightweight test doubles and foreign component adapters.
        try:
            state = object.__getattribute__(component, "__dict__")
        except (AttributeError, TypeError):
            state = {}
        owner = state.get("_game_object")
        if owner is None:
            owner = state.get("game_object")
        object_id = int(getattr(owner, "id", 0) or 0)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        object_id = 0
    return InspectorTarget.scene_object(object_id)


def _component_instance_id(component) -> int:
    try:
        state = object.__getattribute__(component, "__dict__")
    except (AttributeError, TypeError):
        state = {}
    for key in ("_component_id", "component_id"):
        try:
            value = int(state.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value

    # Python component facades expose lifecycle-aware properties.  Never call
    # one after its native binding has been retired merely to identify a cache
    # entry.  Raw pybind components and lightweight adapters do not use a
    # Python property and may be queried while present in an authoritative
    # component listing.
    descriptor = vars(type(component)).get("component_id")
    if isinstance(descriptor, property):
        return id(component)
    try:
        return int(getattr(component, "component_id", 0) or id(component))
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return id(component)


class InspectorSnapshotService:
    """Monotonic typed invalidation authority for Inspector data consumers."""

    _instance: "InspectorSnapshotService | None" = None

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = 1
        self._active_target = InspectorTarget.none()
        self._global = {layer: 1 for layer in InspectorRevisionLayer}
        self._targets: dict[tuple[InspectorTarget, InspectorRevisionLayer], int] = {}
        self._components: dict[tuple[InspectorTarget, int, InspectorRevisionLayer], int] = {}
        self._fields: dict[tuple[InspectorTarget, int, str], int] = {}
        self._domains: dict[tuple[InspectorTarget, str, InspectorRevisionLayer], int] = {}
        self._component_targets: dict[int, InspectorTarget] = {}
        self._unbound_components: dict[tuple[int, InspectorRevisionLayer], int] = {}
        self._unbound_fields: dict[tuple[int, str], int] = {}
        self._consumed_journal_revision = 0

    @classmethod
    def instance(cls) -> "InspectorSnapshotService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def set_active_target(self, target: InspectorTarget) -> int:
        if not isinstance(target, InspectorTarget):
            raise TypeError("Inspector target must be an InspectorTarget")
        with self._lock:
            if target == self._active_target:
                return self._global[InspectorRevisionLayer.TARGET]
            self._active_target = target
            revision = self._next()
            self._global[InspectorRevisionLayer.TARGET] = revision
            return revision

    def active_target(self) -> InspectorTarget:
        with self._lock:
            return self._active_target

    def revision(self) -> int:
        """Return the latest published sequence without allocating a snapshot."""
        with self._lock:
            return self._sequence

    def consumed_journal_revision(self) -> int:
        with self._lock:
            return self._consumed_journal_revision

    def register_component(
        self,
        component,
        *,
        target: InspectorTarget | None = None,
    ) -> int:
        """Remember a component's owning Inspector target without reading values."""
        component_id = _component_instance_id(component)
        resolved_target = target or target_for_component(component)
        with self._lock:
            self._attach_component_locked(component_id, resolved_target)
        return component_id

    def _forget_component_locked(
        self,
        component_id: int,
        target: InspectorTarget,
    ) -> None:
        self._component_targets.pop(component_id, None)
        for key in tuple(self._components):
            if key[0] == target and key[1] == component_id:
                self._components.pop(key, None)
        for key in tuple(self._fields):
            if key[0] == target and key[1] == component_id:
                self._fields.pop(key, None)

    def _attach_component_locked(
        self,
        component_id: int,
        target: InspectorTarget,
    ) -> None:
        previous = self._component_targets.get(component_id)
        if previous is not None and previous != target:
            self._forget_component_locked(component_id, previous)
        self._component_targets[component_id] = target
        for layer in InspectorRevisionLayer:
            revision = self._unbound_components.pop((component_id, layer), 0)
            if revision:
                key = (target, component_id, layer)
                self._components[key] = max(self._components.get(key, 0), revision)
        for key in tuple(self._unbound_fields):
            if key[0] != component_id:
                continue
            revision = self._unbound_fields.pop(key)
            field_key = (target, component_id, key[1])
            self._fields[field_key] = max(self._fields.get(field_key, 0), revision)

    def _publish_unbound_component_locked(
        self,
        revision: int,
        layer: InspectorRevisionLayer,
        component_id: int,
        *,
        field_id: str = "",
    ) -> None:
        key = (component_id, layer)
        self._unbound_components[key] = max(
            self._unbound_components.get(key, 0), revision
        )
        if layer is InspectorRevisionLayer.VALUE:
            field_key = (component_id, str(field_id or ""))
            self._unbound_fields[field_key] = max(
                self._unbound_fields.get(field_key, 0), revision
            )

    def register_target_components(
        self,
        target: InspectorTarget,
        components: Iterable,
    ) -> None:
        """Replace target membership from an authoritative component listing."""
        if not isinstance(target, InspectorTarget):
            raise TypeError("Inspector target must be an InspectorTarget")
        component_ids = {_component_instance_id(component) for component in components}
        with self._lock:
            stale = tuple(
                component_id
                for component_id, owner in self._component_targets.items()
                if owner == target and component_id not in component_ids
            )
            for component_id in stale:
                self._forget_component_locked(component_id, target)
            for component_id in component_ids:
                self._attach_component_locked(component_id, target)

    def invalidate(
        self,
        layer: InspectorRevisionLayer,
        *,
        target: InspectorTarget | None = None,
        component_id: int = 0,
        field_id: str = "",
        domain: str = "",
    ) -> int:
        """Publish one typed change and update its aggregate dependency keys."""
        layer = InspectorRevisionLayer(layer)
        component_id = int(component_id or 0)
        field_id = str(field_id or "")
        domain = str(domain or "")
        with self._lock:
            revision = self._next()
            if target is None:
                self._global[layer] = revision
                return revision

            if component_id > 0:
                self._components[(target, component_id, layer)] = revision
            else:
                self._targets[(target, layer)] = revision
            if layer is InspectorRevisionLayer.VALUE and component_id > 0:
                # Empty field id is the component-wide value dependency.
                # A concrete field still advances the component aggregate so
                # its draw plan replays, but must not invalidate sibling cells.
                self._fields[(target, component_id, field_id)] = revision
            if domain:
                self._domains[(target, domain, layer)] = revision
            return revision

    def invalidate_target(self, target: InspectorTarget | None = None) -> int:
        return self.invalidate(InspectorRevisionLayer.TARGET, target=target)

    def invalidate_schema(
        self,
        target: InspectorTarget | None = None,
        *,
        component_id: int = 0,
        domain: str = "",
    ) -> int:
        return self.invalidate(
            InspectorRevisionLayer.SCHEMA,
            target=target,
            component_id=component_id,
            domain=domain,
        )

    def invalidate_value(
        self,
        target: InspectorTarget | None = None,
        *,
        component_id: int = 0,
        field_id: str = "",
        domain: str = "",
    ) -> int:
        return self.invalidate(
            InspectorRevisionLayer.VALUE,
            target=target,
            component_id=component_id,
            field_id=field_id,
            domain=domain,
        )

    def invalidate_preview(
        self,
        target: InspectorTarget | None = None,
        *,
        component_id: int = 0,
        domain: str = "",
    ) -> int:
        return self.invalidate(
            InspectorRevisionLayer.PREVIEW,
            target=target,
            component_id=component_id,
            domain=domain,
        )

    def snapshot(
        self,
        target: InspectorTarget | None = None,
        *,
        component_ids: Iterable[int] = (),
        domains: Iterable[str] = (),
    ) -> InspectorRevisionSnapshot:
        target = target or self.active_target()
        components = tuple(int(value) for value in component_ids if int(value) > 0)
        domain_keys = tuple(str(value) for value in domains if str(value))
        with self._lock:
            revisions: dict[InspectorRevisionLayer, int] = {}
            for layer in InspectorRevisionLayer:
                revision = max(
                    self._global[layer],
                    self._targets.get((target, layer), 0),
                )
                for component_id in components:
                    revision = max(
                        revision,
                        self._components.get((target, component_id, layer), 0),
                    )
                for domain in domain_keys:
                    revision = max(
                        revision,
                        self._domains.get((target, domain, layer), 0),
                    )
                revisions[layer] = revision
            return InspectorRevisionSnapshot(
                target=target,
                target_revision=revisions[InspectorRevisionLayer.TARGET],
                schema_revision=revisions[InspectorRevisionLayer.SCHEMA],
                value_revision=revisions[InspectorRevisionLayer.VALUE],
                preview_revision=revisions[InspectorRevisionLayer.PREVIEW],
            )

    def aggregate(self, targets: Iterable[InspectorTarget]) -> InspectorRevisionSnapshot:
        snapshots = tuple(self.snapshot(target) for target in targets)
        if not snapshots:
            return self.snapshot(InspectorTarget.none())
        return InspectorRevisionSnapshot(
            target=InspectorTarget("selection", "|".join(
                f"{item.target.kind}:{item.target.identity}" for item in snapshots
            )),
            target_revision=max(item.target_revision for item in snapshots),
            schema_revision=max(item.schema_revision for item in snapshots),
            value_revision=max(item.value_revision for item in snapshots),
            preview_revision=max(item.preview_revision for item in snapshots),
        )

    def component_snapshot(self, component) -> InspectorRevisionSnapshot:
        target = target_for_component(component)
        component_id = self.register_component(component, target=target)
        return self.snapshot(target, component_ids=(component_id,))

    def field_revision(self, component, field_id: str) -> int:
        target = target_for_component(component)
        component_id = _component_instance_id(component)
        field_id = str(field_id or "")
        with self._lock:
            self._attach_component_locked(component_id, target)
            return max(
                self._global[InspectorRevisionLayer.VALUE],
                self._targets.get((target, InspectorRevisionLayer.VALUE), 0),
                self._fields.get((target, component_id, ""), 0),
                self._fields.get((target, component_id, field_id), 0),
            )

    @staticmethod
    def _domain_name(domain) -> str:
        return str(getattr(domain, "value", domain) or "")

    @staticmethod
    def _component_id(stable_id) -> int:
        candidate = stable_id[0] if isinstance(stable_id, tuple) and stable_id else stable_id
        try:
            return int(candidate)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _asset_target(stable_id) -> InspectorTarget | None:
        if not isinstance(stable_id, str):
            return None
        value = stable_id.strip()
        if not value or not any(marker in value for marker in ("/", "\\", ".")):
            return None
        return InspectorTarget.asset(value)

    def _publish_projection(
        self,
        revision: int,
        layer: InspectorRevisionLayer,
        *,
        target: InspectorTarget | None = None,
        component_id: int = 0,
        field_id: str = "",
        domain: str = "",
    ) -> None:
        if target is None:
            self._global[layer] = max(self._global[layer], revision)
            return
        if component_id > 0:
            key = (target, component_id, layer)
            self._components[key] = max(self._components.get(key, 0), revision)
        else:
            self._targets[(target, layer)] = max(
                self._targets.get((target, layer), 0), revision
            )
        if layer is InspectorRevisionLayer.VALUE and component_id > 0:
            key = (target, component_id, field_id)
            self._fields[key] = max(self._fields.get(key, 0), revision)
        if domain:
            key = (target, domain, layer)
            self._domains[key] = max(self._domains.get(key, 0), revision)

    def consume_changes(self, change_set) -> bool:
        """Project one RuntimeChangeJournal change set into Inspector layers.

        The adapter intentionally uses duck typing and imports no journal
        module, keeping dependency direction Runtime -> Editor.  Journal
        revision remains the source cursor; the local sequence only maps it
        into the Inspector's compatibility revision space.
        """
        source_revision = int(getattr(change_set, "revision", 0) or 0)
        changes = getattr(change_set, "changes", None)
        if source_revision < 0 or changes is None:
            raise TypeError("Inspector change adapter requires a RuntimeChangeSet")
        if not bool(getattr(change_set, "changed", bool(changes))):
            with self._lock:
                self._consumed_journal_revision = max(
                    self._consumed_journal_revision, source_revision
                )
            return False

        with self._lock:
            if source_revision <= self._consumed_journal_revision:
                return False
            projected_revision = max(self._sequence + 1, source_revision)
            self._sequence = projected_revision
            self._consumed_journal_revision = source_revision

            if bool(getattr(change_set, "full_resync", False)) and not changes:
                for layer in InspectorRevisionLayer:
                    self._publish_projection(projected_revision, layer)
                return True

            for domain, delta in changes.items():
                name = self._domain_name(domain)
                broad = bool(getattr(delta, "broad", False))
                stable_ids = tuple(getattr(delta, "stable_ids", ()) or ())
                fields = tuple(getattr(delta, "fields", ()) or ())

                if name in {"selection", "inspector_target"}:
                    self._publish_projection(
                        projected_revision, InspectorRevisionLayer.TARGET
                    )
                    continue

                if name in {"scene_topology", "component_structure", "script_schema"}:
                    layer = InspectorRevisionLayer.SCHEMA
                elif name == "preview_source":
                    layer = InspectorRevisionLayer.PREVIEW
                else:
                    layer = InspectorRevisionLayer.VALUE

                if broad:
                    self._publish_projection(projected_revision, layer)

                if name == "component_field":
                    for field in fields:
                        component_id = self._component_id(
                            getattr(field, "component_id", 0)
                        )
                        target = self._component_targets.get(component_id)
                        if target is None:
                            self._publish_unbound_component_locked(
                                projected_revision,
                                InspectorRevisionLayer.VALUE,
                                component_id,
                                field_id=str(getattr(field, "field_id", "") or ""),
                            )
                            continue
                        self._publish_projection(
                            projected_revision,
                            InspectorRevisionLayer.VALUE,
                            target=target,
                            component_id=component_id,
                            field_id=str(getattr(field, "field_id", "") or ""),
                            domain=name,
                        )
                    continue

                for stable_id in stable_ids:
                    asset_target = self._asset_target(stable_id)
                    if asset_target is not None and name in {
                        "asset_content",
                        "asset_import_state",
                        "material",
                        "preview_source",
                    }:
                        self._publish_projection(
                            projected_revision,
                            layer,
                            target=asset_target,
                            domain=name,
                        )
                        if name in {"asset_content", "asset_import_state", "material"}:
                            self._publish_projection(
                                projected_revision,
                                InspectorRevisionLayer.PREVIEW,
                                target=asset_target,
                                domain=name,
                            )
                        continue

                    component_id = self._component_id(stable_id)
                    target = self._component_targets.get(component_id)
                    if target is not None:
                        self._publish_projection(
                            projected_revision,
                            layer,
                            target=target,
                            component_id=component_id,
                            domain=name,
                        )
                        if name in {
                            "scene_topology",
                            "component_structure",
                            "component_enabled",
                            "script_schema",
                        }:
                            # Native component headers are cached per object.
                            # Publish their infrequent structural changes to
                            # the owner packet while ordinary fields remain
                            # strictly component/field scoped.
                            self._publish_projection(
                                projected_revision,
                                InspectorRevisionLayer.SCHEMA,
                                target=target,
                                domain=name,
                            )
                    elif component_id > 0 and name in {
                        "transform_local",
                        "transform_world",
                        "scene_topology",
                    }:
                        self._publish_projection(
                            projected_revision,
                            layer,
                            target=InspectorTarget.scene_object(component_id),
                            domain=name,
                        )
                    elif component_id > 0 and name in {
                        "component_structure",
                        "component_enabled",
                        "component_field",
                        "script_value",
                    }:
                        self._publish_unbound_component_locked(
                            projected_revision,
                            layer,
                            component_id,
                        )
                    else:
                        self._publish_projection(projected_revision, layer)

                if name in {"asset_content", "asset_import_state", "material"} and not stable_ids:
                    self._publish_projection(
                        projected_revision, InspectorRevisionLayer.PREVIEW
                    )
            return True

    def reset_for_tests(self) -> None:
        with self._lock:
            self._sequence = 1
            self._active_target = InspectorTarget.none()
            self._global = {layer: 1 for layer in InspectorRevisionLayer}
            self._targets.clear()
            self._components.clear()
            self._fields.clear()
            self._domains.clear()
            self._component_targets.clear()
            self._unbound_components.clear()
            self._unbound_fields.clear()
            self._consumed_journal_revision = 0


def invalidate_component_field(component, field_id: str) -> int:
    service = InspectorSnapshotService.instance()
    return service.invalidate_value(
        target_for_component(component),
        component_id=service.register_component(component),
        field_id=field_id,
    )


def invalidate_component_schema(component) -> int:
    service = InspectorSnapshotService.instance()
    return service.invalidate_schema(
        target_for_component(component),
        component_id=service.register_component(component),
    )


def invalidate_scene_transforms(object_ids: Iterable[int]) -> int:
    """Invalidate live Transform projections for the supplied scene objects.

    Native Transform edits can happen outside Inspector-owned property
    transactions (scene gizmos, physics/editor handoff, and scene rebuilds).
    Keep those paths revision driven by publishing only the affected object
    targets instead of disabling the native Transform cache.
    """
    service = InspectorSnapshotService.instance()
    latest = service.revision()
    seen: set[int] = set()
    for value in object_ids:
        try:
            object_id = int(value)
        except (TypeError, ValueError):
            continue
        if object_id <= 0 or object_id in seen:
            continue
        seen.add(object_id)
        latest = service.invalidate_value(
            InspectorTarget.scene_object(object_id),
            domain="transform",
        )
    return latest


def invalidate_rebuilt_scene() -> int:
    """Invalidate every Inspector projection after replacing a scene graph."""
    service = InspectorSnapshotService.instance()
    service.invalidate_schema()
    return service.invalidate_value()


__all__ = [
    "InspectorRevisionLayer",
    "InspectorRevisionSnapshot",
    "InspectorSnapshotService",
    "InspectorTarget",
    "invalidate_component_field",
    "invalidate_component_schema",
    "invalidate_rebuilt_scene",
    "invalidate_scene_transforms",
    "target_for_component",
]
