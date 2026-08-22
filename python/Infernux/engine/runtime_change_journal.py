"""Typed runtime invalidation journal shared by Editor and Player.

The journal is deliberately not a data model and not an undo history.  It
coalesces stable identities between deterministic runtime barriers so each
consumer can update derived state from a monotonic revision cursor.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import threading
from types import MappingProxyType
from typing import Hashable, Iterable, Iterator, Mapping


class RuntimeChangeDomain(str, Enum):
    SCENE_TOPOLOGY = "scene_topology"
    TRANSFORM_LOCAL = "transform_local"
    TRANSFORM_WORLD = "transform_world"
    COMPONENT_STRUCTURE = "component_structure"
    COMPONENT_ENABLED = "component_enabled"
    COMPONENT_FIELD = "component_field"
    SCRIPT_SCHEMA = "script_schema"
    SCRIPT_VALUE = "script_value"
    SCRIPT_DIAGNOSTIC = "script_diagnostic"
    PHYSICS_SHAPE = "physics_shape"
    PHYSICS_BODY = "physics_body"
    RENDER_STATE = "render_state"
    MATERIAL = "material"
    RENDER_STACK = "render_stack"
    ASSET_CONTENT = "asset_content"
    ASSET_IMPORT_STATE = "asset_import_state"
    SELECTION = "selection"
    INSPECTOR_TARGET = "inspector_target"
    PREVIEW_SOURCE = "preview_source"
    RUNTIME_UI_DATA = "runtime_ui_data"


class RuntimeFrameBarrier(str, Enum):
    SAFE_POINT = "safe_point"
    FIXED_SCRIPT = "fixed_script"
    TRANSFORM_TO_PHYSICS = "transform_to_physics"
    PHYSICS_SIMULATION = "physics_simulation"
    PHYSICS_TO_TRANSFORM = "physics_to_transform"
    TRANSFORM_RESOLVE = "transform_resolve"
    UPDATE_SCRIPT = "update_script"
    LATE_SCRIPT = "late_script"
    FINAL_TRANSFORM_RESOLVE = "final_transform_resolve"
    ANIMATION_TIMELINE = "animation_timeline"
    RENDER_EXTRACTION = "render_extraction"
    RENDER_GRAPH = "render_graph"
    SNAPSHOT_PUBLICATION = "snapshot_publication"
    PENDING_DESTROY = "pending_destroy"
    RETIREMENT = "retirement"


@dataclass(frozen=True, slots=True)
class RuntimeFieldKey:
    type_id: Hashable
    component_id: Hashable
    field_id: Hashable


@dataclass(frozen=True, slots=True)
class RuntimeDomainChanges:
    broad: bool = False
    stable_ids: frozenset[Hashable] = frozenset()
    fields: frozenset[RuntimeFieldKey] = frozenset()

    @property
    def changed(self) -> bool:
        return self.broad or bool(self.stable_ids) or bool(self.fields)


@dataclass(frozen=True, slots=True)
class RuntimeChangeSet:
    from_revision: int
    revision: int
    domain_revisions: Mapping[RuntimeChangeDomain, int]
    changes: Mapping[RuntimeChangeDomain, RuntimeDomainChanges]
    full_resync: bool = False

    @property
    def changed(self) -> bool:
        return self.full_resync or bool(self.changes)

    def affects(self, *domains: RuntimeChangeDomain | str) -> bool:
        return any(RuntimeChangeDomain(domain) in self.changes for domain in domains)

    def for_domain(
        self, domain: RuntimeChangeDomain | str
    ) -> RuntimeDomainChanges:
        return self.changes.get(
            RuntimeChangeDomain(domain),
            RuntimeDomainChanges(),
        )


@dataclass(slots=True)
class RuntimeChangeCursor:
    name: str
    domains: frozenset[RuntimeChangeDomain]
    revision: int = 0
    domain_revisions: dict[RuntimeChangeDomain, int] = field(default_factory=dict)
    _empty_change_set: RuntimeChangeSet | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(slots=True)
class _MutableDomainChanges:
    broad: bool = False
    stable_ids: set[Hashable] = field(default_factory=set)
    fields: set[RuntimeFieldKey] = field(default_factory=set)

    def merge(self, other: "_MutableDomainChanges") -> None:
        self.broad = self.broad or other.broad
        self.stable_ids.update(other.stable_ids)
        self.fields.update(other.fields)

    def freeze(self) -> RuntimeDomainChanges:
        return RuntimeDomainChanges(
            broad=self.broad,
            stable_ids=frozenset(self.stable_ids),
            fields=frozenset(self.fields),
        )


@dataclass(frozen=True, slots=True)
class _CommittedBatch:
    revision: int
    changes: Mapping[RuntimeChangeDomain, RuntimeDomainChanges]


def _all_domains() -> frozenset[RuntimeChangeDomain]:
    return frozenset(RuntimeChangeDomain)


class RuntimeChangeJournal:
    """Coalescing typed revision stream with independent consumer cursors."""

    def __init__(self, *, history_limit: int = 256) -> None:
        if int(history_limit) < 2:
            raise ValueError("runtime change history_limit must be at least 2")
        self._lock = threading.RLock()
        self._pending: dict[RuntimeChangeDomain, _MutableDomainChanges] = {}
        self._history: deque[_CommittedBatch] = deque(maxlen=int(history_limit))
        self._revision = 0
        self._domain_revisions = {domain: 0 for domain in RuntimeChangeDomain}
        self._local = threading.local()
        self._publish_count = 0
        self._coalesced_count = 0
        self._flush_count = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def domain_revision(self, domain: RuntimeChangeDomain | str) -> int:
        with self._lock:
            return self._domain_revisions[RuntimeChangeDomain(domain)]

    @staticmethod
    def _require_hashable(value: Hashable, label: str) -> Hashable:
        try:
            hash(value)
        except TypeError as exc:
            raise TypeError(f"runtime change {label} must be hashable") from exc
        return value

    def _transaction_stack(
        self,
    ) -> list[dict[RuntimeChangeDomain, _MutableDomainChanges]]:
        stack = getattr(self._local, "transactions", None)
        if stack is None:
            stack = []
            self._local.transactions = stack
        return stack

    @staticmethod
    def _merge_changes(
        target: dict[RuntimeChangeDomain, _MutableDomainChanges],
        source: Mapping[RuntimeChangeDomain, _MutableDomainChanges],
    ) -> None:
        for domain, delta in source.items():
            target.setdefault(domain, _MutableDomainChanges()).merge(delta)

    @contextmanager
    def transaction(self) -> Iterator["RuntimeChangeJournal"]:
        """Merge all changes in the block, or publish none if it raises."""
        stack = self._transaction_stack()
        local: dict[RuntimeChangeDomain, _MutableDomainChanges] = {}
        stack.append(local)
        try:
            yield self
        except BaseException:
            stack.pop()
            raise
        else:
            stack.pop()
            if stack:
                self._merge_changes(stack[-1], local)
            else:
                with self._lock:
                    self._merge_changes(self._pending, local)

    def publish(
        self,
        domain: RuntimeChangeDomain | str,
        *,
        stable_id: Hashable | None = None,
        stable_ids: Iterable[Hashable] = (),
        field: RuntimeFieldKey | None = None,
        fields: Iterable[RuntimeFieldKey] = (),
        broad: bool = False,
    ) -> None:
        """Queue one typed mutation for coalescing at the next barrier."""
        typed_domain = RuntimeChangeDomain(domain)
        ids = set(stable_ids)
        if stable_id is not None:
            ids.add(stable_id)
        ids = {self._require_hashable(value, "stable_id") for value in ids}

        field_set = set(fields)
        if field is not None:
            field_set.add(field)
        for item in field_set:
            if not isinstance(item, RuntimeFieldKey):
                raise TypeError("runtime field changes require RuntimeFieldKey values")
            self._require_hashable(item.type_id, "type_id")
            self._require_hashable(item.component_id, "component_id")
            self._require_hashable(item.field_id, "field_id")

        broad = bool(broad or (not ids and not field_set))
        delta = _MutableDomainChanges(broad=broad, stable_ids=ids, fields=field_set)
        stack = self._transaction_stack()
        if stack:
            target = stack[-1]
            existing = target.get(typed_domain)
            if existing is not None:
                self._coalesced_count += 1
                existing.merge(delta)
            else:
                target[typed_domain] = delta
            self._publish_count += 1
            return

        with self._lock:
            existing = self._pending.get(typed_domain)
            if existing is not None:
                self._coalesced_count += 1
                existing.merge(delta)
            else:
                self._pending[typed_domain] = delta
            self._publish_count += 1

    def publish_component_field(
        self,
        type_id: Hashable,
        component_id: Hashable,
        field_id: Hashable,
    ) -> None:
        self.publish(
            RuntimeChangeDomain.COMPONENT_FIELD,
            field=RuntimeFieldKey(type_id, component_id, field_id),
        )

    def _flush_locked(self) -> int:
        if not self._pending:
            return self._revision
        self._revision += 1
        revision = self._revision
        frozen = {
            domain: delta.freeze()
            for domain, delta in self._pending.items()
            if delta.broad or delta.stable_ids or delta.fields
        }
        self._pending = {}
        for domain in frozen:
            self._domain_revisions[domain] = revision
        self._history.append(_CommittedBatch(revision, frozen))
        self._flush_count += 1
        return revision

    def flush(self) -> int:
        """Commit the coalesced pending set and return its revision."""
        if self._transaction_stack():
            raise RuntimeError("cannot flush a runtime change transaction before it exits")
        with self._lock:
            return self._flush_locked()

    def create_cursor(
        self,
        name: str,
        *,
        domains: Iterable[RuntimeChangeDomain | str] | None = None,
        start_at_current: bool = True,
    ) -> RuntimeChangeCursor:
        selected = (
            _all_domains()
            if domains is None
            else frozenset(RuntimeChangeDomain(domain) for domain in domains)
        )
        with self._lock:
            revision = self._revision if start_at_current else 0
            revisions = {
                domain: self._domain_revisions[domain]
                for domain in selected
                if self._domain_revisions[domain] <= revision
            }
        domain_snapshot = MappingProxyType(dict(revisions))
        empty_change_set = RuntimeChangeSet(
            from_revision=revision,
            revision=revision,
            domain_revisions=domain_snapshot,
            changes=MappingProxyType({}),
        )
        return RuntimeChangeCursor(
            str(name),
            selected,
            revision,
            revisions,
            empty_change_set,
        )

    def consume(
        self,
        cursor: RuntimeChangeCursor,
        *,
        flush: bool = True,
    ) -> RuntimeChangeSet:
        if not isinstance(cursor, RuntimeChangeCursor):
            raise TypeError("runtime change consumer requires a RuntimeChangeCursor")
        if flush and self._transaction_stack():
            raise RuntimeError("cannot flush a runtime change transaction before it exits")

        with self._lock:
            if flush:
                self._flush_locked()
            start_revision = int(cursor.revision)
            current_revision = self._revision
            if start_revision > current_revision:
                raise ValueError("runtime change cursor is ahead of its journal")

            if start_revision == current_revision:
                cached = cursor._empty_change_set
                if cached is not None and cached.revision == current_revision:
                    return cached
                revisions = {
                    domain: self._domain_revisions[domain]
                    for domain in cursor.domains
                }
                cursor.domain_revisions.clear()
                cursor.domain_revisions.update(revisions)
                cached = RuntimeChangeSet(
                    from_revision=current_revision,
                    revision=current_revision,
                    domain_revisions=MappingProxyType(revisions),
                    changes=MappingProxyType({}),
                )
                cursor._empty_change_set = cached
                return cached

            oldest = self._history[0].revision if self._history else current_revision + 1
            history_lost = start_revision < oldest - 1
            merged: dict[RuntimeChangeDomain, _MutableDomainChanges] = {}
            if history_lost:
                for domain in cursor.domains:
                    if self._domain_revisions[domain] > start_revision:
                        merged[domain] = _MutableDomainChanges(broad=True)
            else:
                for batch in self._history:
                    if batch.revision <= start_revision:
                        continue
                    for domain, delta in batch.changes.items():
                        if domain not in cursor.domains:
                            continue
                        target = merged.setdefault(domain, _MutableDomainChanges())
                        target.broad = target.broad or delta.broad
                        target.stable_ids.update(delta.stable_ids)
                        target.fields.update(delta.fields)

            changes = {domain: delta.freeze() for domain, delta in merged.items()}
            full_resync = history_lost and bool(changes)
            revisions = {
                domain: self._domain_revisions[domain]
                for domain in cursor.domains
            }
            cursor.revision = current_revision
            cursor.domain_revisions.clear()
            cursor.domain_revisions.update(revisions)
            result = RuntimeChangeSet(
                from_revision=start_revision,
                revision=current_revision,
                domain_revisions=MappingProxyType(revisions),
                changes=MappingProxyType(changes),
                full_resync=full_resync,
            )
            cursor._empty_change_set = RuntimeChangeSet(
                from_revision=current_revision,
                revision=current_revision,
                domain_revisions=result.domain_revisions,
                changes=MappingProxyType({}),
            )
            return result

    def profiler_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "revision": self._revision,
                "pending_domains": len(self._pending),
                "history_batches": len(self._history),
                "publish_count": self._publish_count,
                "coalesced_count": self._coalesced_count,
                "flush_count": self._flush_count,
            }


_DEFAULT_RUNTIME_CHANGE_JOURNAL = RuntimeChangeJournal()


def runtime_change_journal() -> RuntimeChangeJournal:
    return _DEFAULT_RUNTIME_CHANGE_JOURNAL


__all__ = [
    "RuntimeChangeCursor",
    "RuntimeChangeDomain",
    "RuntimeChangeJournal",
    "RuntimeChangeSet",
    "RuntimeDomainChanges",
    "RuntimeFieldKey",
    "RuntimeFrameBarrier",
    "runtime_change_journal",
]
