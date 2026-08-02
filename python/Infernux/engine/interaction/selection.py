"""Single-authority selection service for all editor surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .descriptors import SelectionSnapshot, SelectionTarget


@dataclass(frozen=True, slots=True)
class SelectionChange:
    before: SelectionSnapshot
    after: SelectionSnapshot
    reason: str
    record_history: bool


class SelectionService:
    """Own the editor's one active selection domain and ordered targets."""

    _instance: Optional["SelectionService"] = None

    def __init__(self) -> None:
        self._snapshot = SelectionSnapshot()
        self._revision = 0
        self._listeners: list[Callable[[SelectionChange], None]] = []
        self._ordered_targets: dict[str, tuple[SelectionTarget, ...]] = {}
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

    def set_ordered_targets(
        self,
        owner_id: str,
        targets: Sequence[SelectionTarget],
    ) -> None:
        owner_id = str(owner_id or "")
        self._ordered_targets[owner_id] = tuple(dict.fromkeys(targets))

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
        if before.owner_id != owner_id or (
            before.domain is not None and before.domain is not target.domain
        ):
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
        anchor = before.anchor if before.owner_id == owner_id else None
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
