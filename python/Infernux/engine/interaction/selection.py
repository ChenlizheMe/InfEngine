"""Single-authority selection service for all editor surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .descriptors import SelectionDomain, SelectionSnapshot, SelectionTarget


@dataclass(frozen=True, slots=True)
class SelectionChange:
    before: SelectionSnapshot
    after: SelectionSnapshot
    reason: str
    record_history: bool


class SelectionService:
    """Own the editor's one active selection domain and ordered targets.

    ``owner_id`` identifies the view that most recently authored the selection;
    it is not a second selection domain and never partitions same-domain targets.
    """

    _instance: Optional["SelectionService"] = None

    def __init__(self) -> None:
        self._snapshot = SelectionSnapshot()
        self._revision = 0
        self._listeners: list[Callable[[SelectionChange], None]] = []
        self._ordered_targets: dict[str, tuple[SelectionTarget, ...]] = {}
        self._owner_domain_validator: Optional[
            Callable[[str, SelectionDomain], bool]
        ] = None
        SelectionService._instance = self

    @classmethod
    def instance(cls) -> "SelectionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def install(cls, service: "SelectionService") -> None:
        if not isinstance(service, cls):
            raise TypeError("selection service must be a SelectionService")
        cls._instance = service

    @property
    def snapshot(self) -> SelectionSnapshot:
        return self._snapshot

    @property
    def revision(self) -> int:
        return self._revision

    def add_listener(self, callback: Callable[[SelectionChange], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SelectionChange], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    @staticmethod
    def reconciled_snapshot(
        snapshot: SelectionSnapshot,
        is_valid: Callable[[SelectionTarget], bool],
        *,
        fallback: Optional[SelectionTarget] = None,
        fallback_owner_id: str = "",
    ) -> SelectionSnapshot:
        """Project a selection onto currently valid stable targets.

        This is the shared invalidation primitive for collections and asset
        subresources. It never records history by itself, so external asset
        refreshes can reconcile selection without becoming user actions while
        document commands can embed the returned before/after snapshots.
        """
        if not callable(is_valid):
            raise TypeError("selection reconciliation requires a validator")
        retained = tuple(target for target in snapshot.targets if is_valid(target))
        if retained == snapshot.targets:
            return snapshot
        if not retained:
            if fallback is None:
                return SelectionSnapshot()
            owner_id = str(fallback_owner_id or snapshot.owner_id or "").strip()
            if not owner_id:
                raise ValueError("selection reconciliation fallback requires an owner")
            return SelectionSnapshot.create(
                (fallback,),
                owner_id=owner_id,
                primary=fallback,
                anchor=fallback,
            )
        primary = snapshot.primary if snapshot.primary in retained else retained[-1]
        anchor = snapshot.anchor if snapshot.anchor in retained else retained[0]
        return SelectionSnapshot.create(
            retained,
            owner_id=snapshot.owner_id,
            primary=primary,
            anchor=anchor,
        )

    def reconcile(
        self,
        is_valid: Callable[[SelectionTarget], bool],
        *,
        fallback: Optional[SelectionTarget] = None,
        fallback_owner_id: str = "",
        reason: str = "reconcile",
        record_history: bool = False,
    ) -> bool:
        projected = self.reconciled_snapshot(
            self._snapshot,
            is_valid,
            fallback=fallback,
            fallback_owner_id=fallback_owner_id,
        )
        if projected == self._snapshot:
            return False
        return self.apply_snapshot(
            projected,
            reason=reason,
            record_history=record_history,
        )

    def set_owner_domain_validator(
        self,
        validator: Optional[Callable[[str, SelectionDomain], bool]],
    ) -> None:
        if validator is not None and not callable(validator):
            raise TypeError("selection owner-domain validator must be callable")
        self._owner_domain_validator = validator

    def scene_object_ids(self) -> tuple[int, ...]:
        """Project the active typed selection onto owning scene objects."""
        snapshot = self._snapshot
        if snapshot.domain is SelectionDomain.SCENE_OBJECT:
            return tuple(
                target.scene_object_id()
                for target in snapshot.targets
                if target.scene_object_id() > 0
            )
        if snapshot.domain is SelectionDomain.COMPONENT:
            return tuple(dict.fromkeys(
                object_id
                for object_id, _component_id in (
                    target.component_ids() for target in snapshot.targets
                )
                if object_id > 0
            ))
        return ()

    def primary_scene_object_id(self) -> int:
        """Return the selected GameObject or the owner of a selected component."""
        primary = self._snapshot.primary
        if primary is None:
            return 0
        if primary.domain is SelectionDomain.SCENE_OBJECT:
            return primary.scene_object_id()
        if primary.domain is SelectionDomain.COMPONENT:
            return primary.component_ids()[0]
        return 0

    def is_scene_object_selected(self, object_id: int) -> bool:
        return int(object_id) in self.scene_object_ids()

    def set_ordered_scene_objects(
        self,
        owner_id: str,
        object_ids: Sequence[int],
    ) -> None:
        self.set_ordered_targets(
            owner_id,
            tuple(
                SelectionTarget.scene_object(object_id)
                for object_id in object_ids
                if int(object_id) > 0
            ),
        )

    def select_scene_object(
        self,
        object_id: int,
        *,
        owner_id: str,
        reason: str = "select_scene_object",
        record_history: bool = True,
    ) -> bool:
        object_id = int(object_id)
        if object_id <= 0:
            return self.clear(reason=reason, record_history=record_history)
        return self.select(
            SelectionTarget.scene_object(object_id),
            owner_id=owner_id,
            reason=reason,
            record_history=record_history,
        )

    def toggle_scene_object(
        self,
        object_id: int,
        *,
        owner_id: str,
        reason: str = "toggle_scene_object",
        record_history: bool = True,
    ) -> bool:
        object_id = int(object_id)
        if object_id <= 0:
            return False
        return self.toggle(
            SelectionTarget.scene_object(object_id),
            owner_id=owner_id,
            reason=reason,
            record_history=record_history,
        )

    def range_select_scene_object(
        self,
        object_id: int,
        *,
        owner_id: str,
        reason: str = "range_select_scene_object",
        record_history: bool = True,
    ) -> bool:
        object_id = int(object_id)
        if object_id <= 0:
            return self.clear(reason=reason, record_history=record_history)
        return self.range_select(
            SelectionTarget.scene_object(object_id),
            owner_id=owner_id,
            reason=reason,
            record_history=record_history,
        )

    def replace_scene_objects(
        self,
        object_ids: Iterable[int],
        *,
        owner_id: str,
        primary_object_id: int = 0,
        anchor_object_id: int = 0,
        reason: str = "replace_scene_objects",
        record_history: bool = True,
    ) -> bool:
        targets = tuple(
            SelectionTarget.scene_object(object_id)
            for object_id in dict.fromkeys(int(value) for value in object_ids)
            if object_id > 0
        )
        primary = next(
            (target for target in targets if target.scene_object_id() == int(primary_object_id)),
            None,
        )
        anchor = next(
            (target for target in targets if target.scene_object_id() == int(anchor_object_id)),
            None,
        )
        return self.replace(
            targets,
            owner_id=owner_id if targets else "",
            primary=primary,
            anchor=anchor,
            reason=reason,
            record_history=record_history,
        )

    def box_select_scene_objects(
        self,
        object_ids: Iterable[int],
        *,
        additive: bool,
        owner_id: str,
        reason: str = "box_select_scene_objects",
        record_history: bool = True,
    ) -> bool:
        targets = tuple(
            SelectionTarget.scene_object(object_id)
            for object_id in dict.fromkeys(int(value) for value in object_ids)
            if object_id > 0
        )
        before = self._snapshot
        if additive and before.domain is SelectionDomain.SCENE_OBJECT:
            targets = tuple(dict.fromkeys(before.targets + targets))
            anchor = before.anchor if before.anchor in targets else None
        else:
            anchor = None
        primary = targets[-1] if targets else None
        return self.replace(
            targets,
            owner_id=owner_id if targets else "",
            primary=primary,
            anchor=anchor,
            reason=reason,
            record_history=record_history,
        )

    def set_ordered_targets(
        self,
        owner_id: str,
        targets: Sequence[SelectionTarget],
    ) -> None:
        owner_id = str(owner_id or "")
        ordered = tuple(dict.fromkeys(targets))
        if ordered and not owner_id:
            raise ValueError("ordered selection targets require an owner")
        if len({target.domain for target in ordered}) > 1:
            raise ValueError("ordered selection targets cannot mix domains")
        self._ordered_targets[owner_id] = ordered

    def replace(
        self,
        targets: Iterable[SelectionTarget],
        *,
        owner_id: str,
        primary: Optional[SelectionTarget] = None,
        anchor: Optional[SelectionTarget] = None,
        reason: str = "replace",
        record_history: bool = True,
    ) -> bool:
        snapshot = SelectionSnapshot.create(
            targets,
            owner_id=owner_id,
            primary=primary,
            anchor=anchor,
        )
        return self.apply_snapshot(
            snapshot,
            reason=reason,
            record_history=record_history,
        )

    def select(
        self,
        target: SelectionTarget,
        *,
        owner_id: str,
        reason: str = "select",
        record_history: bool = True,
    ) -> bool:
        return self.replace(
            (target,),
            owner_id=owner_id,
            primary=target,
            anchor=target,
            reason=reason,
            record_history=record_history,
        )

    def toggle(
        self,
        target: SelectionTarget,
        *,
        owner_id: str,
        reason: str = "toggle",
        record_history: bool = True,
    ) -> bool:
        before = self._snapshot
        owner_id = str(owner_id or "")
        if before.domain is not None and before.domain is not target.domain:
            return self.select(
                target,
                owner_id=owner_id,
                reason=reason,
                record_history=record_history,
            )

        targets = list(before.targets)
        if target in targets:
            targets.remove(target)
            primary = targets[-1] if targets else None
            anchor = before.anchor if before.anchor in targets else primary
        else:
            targets.append(target)
            primary = target
            anchor = before.anchor or target
        return self.replace(
            targets,
            owner_id=owner_id,
            primary=primary,
            anchor=anchor,
            reason=reason,
            record_history=record_history,
        )

    def range_select(
        self,
        target: SelectionTarget,
        *,
        owner_id: str,
        reason: str = "range_select",
        record_history: bool = True,
    ) -> bool:
        owner_id = str(owner_id or "")
        ordered = self._ordered_targets.get(owner_id, ())
        before = self._snapshot
        anchor = before.anchor if before.domain in {None, target.domain} else None
        if not ordered or anchor not in ordered or target not in ordered:
            return self.select(
                target,
                owner_id=owner_id,
                reason=reason,
                record_history=record_history,
            )
        start, end = ordered.index(anchor), ordered.index(target)
        lo, hi = min(start, end), max(start, end)
        return self.replace(
            ordered[lo : hi + 1],
            owner_id=owner_id,
            primary=target,
            anchor=anchor,
            reason=reason,
            record_history=record_history,
        )

    def clear(
        self,
        *,
        reason: str = "clear",
        record_history: bool = True,
    ) -> bool:
        return self.apply_snapshot(
            SelectionSnapshot(),
            reason=reason,
            record_history=record_history,
        )

    def apply_snapshot(
        self,
        snapshot: SelectionSnapshot,
        *,
        reason: str = "restore",
        record_history: bool = False,
    ) -> bool:
        if not isinstance(snapshot, SelectionSnapshot):
            raise TypeError("selection snapshot must be a SelectionSnapshot")
        if (
            snapshot.targets
            and self._owner_domain_validator is not None
            and not self._owner_domain_validator(snapshot.owner_id, snapshot.domain)
        ):
            raise ValueError(
                f"selection owner '{snapshot.owner_id}' does not declare "
                f"the '{snapshot.domain.value}' domain"
            )
        before = self._snapshot
        if before == snapshot:
            return False
        self._snapshot = snapshot
        self._revision += 1
        change = SelectionChange(before, snapshot, str(reason), bool(record_history))
        for callback in tuple(self._listeners):
            try:
                callback(change)
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("SelectionService.listener", exc)
        return True

    def remap_asset_path(
        self,
        source_path: str,
        destination_path: str,
        *,
        reason: str = "asset_moved",
    ) -> bool:
        """Remap asset selections without creating a second user action."""
        from Infernux.engine.path_utils import lexical_path, path_key

        source_key = path_key(source_path)
        destination = lexical_path(destination_path)
        if not source_key or not destination:
            raise ValueError("asset selection remap requires source and destination")

        def remap(target: SelectionTarget) -> SelectionTarget:
            if (
                target.domain is SelectionDomain.ASSET
                and path_key(target.target_id) == source_key
            ):
                return SelectionTarget.asset(destination)
            if (
                target.domain is SelectionDomain.ASSET_SUBRESOURCE
                and path_key(target.document_id) == source_key
            ):
                return SelectionTarget.asset_subresource(
                    destination,
                    target.target_id,
                    sub_kind=target.sub_kind,
                )
            return target

        before = self._snapshot
        mapping = {target: remap(target) for target in before.targets}
        next_snapshot = (
            SelectionSnapshot.create(
                (mapping[target] for target in before.targets),
                owner_id=before.owner_id,
                primary=(
                    mapping.get(before.primary)
                    if before.primary is not None
                    else None
                ),
                anchor=(
                    mapping.get(before.anchor)
                    if before.anchor is not None
                    else None
                ),
            )
            if before.targets
            else before
        )

        for owner_id, targets in tuple(self._ordered_targets.items()):
            remapped = tuple(remap(target) for target in targets)
            if remapped != targets:
                self._ordered_targets[owner_id] = remapped

        return self.apply_snapshot(
            next_snapshot,
            reason=reason,
            record_history=False,
        )
