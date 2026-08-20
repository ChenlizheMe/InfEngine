from contextlib import AbstractContextManager
from enum import Enum
from typing import Hashable, Iterable, Mapping

class RuntimeChangeDomain(str, Enum):
    SCENE_TOPOLOGY: RuntimeChangeDomain
    TRANSFORM_LOCAL: RuntimeChangeDomain
    TRANSFORM_WORLD: RuntimeChangeDomain
    COMPONENT_STRUCTURE: RuntimeChangeDomain
    COMPONENT_ENABLED: RuntimeChangeDomain
    COMPONENT_FIELD: RuntimeChangeDomain
    SCRIPT_SCHEMA: RuntimeChangeDomain
    SCRIPT_VALUE: RuntimeChangeDomain
    SCRIPT_DIAGNOSTIC: RuntimeChangeDomain
    PHYSICS_SHAPE: RuntimeChangeDomain
    PHYSICS_BODY: RuntimeChangeDomain
    RENDER_STATE: RuntimeChangeDomain
    MATERIAL: RuntimeChangeDomain
    RENDER_STACK: RuntimeChangeDomain
    ASSET_CONTENT: RuntimeChangeDomain
    ASSET_IMPORT_STATE: RuntimeChangeDomain
    SELECTION: RuntimeChangeDomain
    INSPECTOR_TARGET: RuntimeChangeDomain
    PREVIEW_SOURCE: RuntimeChangeDomain
    RUNTIME_UI_DATA: RuntimeChangeDomain

class RuntimeFrameBarrier(str, Enum):
    SAFE_POINT: RuntimeFrameBarrier
    FIXED_SCRIPT: RuntimeFrameBarrier
    TRANSFORM_TO_PHYSICS: RuntimeFrameBarrier
    PHYSICS_SIMULATION: RuntimeFrameBarrier
    PHYSICS_TO_TRANSFORM: RuntimeFrameBarrier
    TRANSFORM_RESOLVE: RuntimeFrameBarrier
    UPDATE_SCRIPT: RuntimeFrameBarrier
    LATE_SCRIPT: RuntimeFrameBarrier
    FINAL_TRANSFORM_RESOLVE: RuntimeFrameBarrier
    ANIMATION_TIMELINE: RuntimeFrameBarrier
    RENDER_EXTRACTION: RuntimeFrameBarrier
    RENDER_GRAPH: RuntimeFrameBarrier
    SNAPSHOT_PUBLICATION: RuntimeFrameBarrier
    PENDING_DESTROY: RuntimeFrameBarrier
    RETIREMENT: RuntimeFrameBarrier

class RuntimeFieldKey:
    type_id: Hashable
    component_id: Hashable
    field_id: Hashable
    def __init__(self, type_id: Hashable, component_id: Hashable, field_id: Hashable) -> None: ...

class RuntimeDomainChanges:
    broad: bool
    stable_ids: frozenset[Hashable]
    fields: frozenset[RuntimeFieldKey]
    @property
    def changed(self) -> bool: ...

class RuntimeChangeSet:
    from_revision: int
    revision: int
    domain_revisions: Mapping[RuntimeChangeDomain, int]
    changes: Mapping[RuntimeChangeDomain, RuntimeDomainChanges]
    full_resync: bool
    @property
    def changed(self) -> bool: ...
    def affects(self, *domains: RuntimeChangeDomain | str) -> bool: ...
    def for_domain(self, domain: RuntimeChangeDomain | str) -> RuntimeDomainChanges: ...

class RuntimeChangeCursor:
    name: str
    domains: frozenset[RuntimeChangeDomain]
    revision: int
    domain_revisions: dict[RuntimeChangeDomain, int]

class RuntimeChangeJournal:
    def __init__(self, *, history_limit: int = ...) -> None: ...
    @property
    def revision(self) -> int: ...
    def domain_revision(self, domain: RuntimeChangeDomain | str) -> int: ...
    def transaction(self) -> AbstractContextManager[RuntimeChangeJournal]: ...
    def publish(self, domain: RuntimeChangeDomain | str, *, stable_id: Hashable | None = ..., stable_ids: Iterable[Hashable] = ..., field: RuntimeFieldKey | None = ..., fields: Iterable[RuntimeFieldKey] = ..., broad: bool = ...) -> None: ...
    def publish_component_field(self, type_id: Hashable, component_id: Hashable, field_id: Hashable) -> None: ...
    def flush(self) -> int: ...
    def create_cursor(self, name: str, *, domains: Iterable[RuntimeChangeDomain | str] | None = ..., start_at_current: bool = ...) -> RuntimeChangeCursor: ...
    def consume(self, cursor: RuntimeChangeCursor, *, flush: bool = ...) -> RuntimeChangeSet: ...
    def profiler_snapshot(self) -> dict[str, int]: ...

def runtime_change_journal() -> RuntimeChangeJournal: ...
