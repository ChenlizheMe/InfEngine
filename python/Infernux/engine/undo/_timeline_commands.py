"""Precise undo commands for Animation Timeline authoring."""

from __future__ import annotations

import copy
from typing import Any

from Infernux.core.animation_timeline import AnimationTimeline, TimelineKeyframe
from Infernux.engine.interaction import DocumentRegistry
from Infernux.engine.undo._base import UndoCommand


class _TimelineCommand(UndoCommand):
    def __init__(
        self,
        timeline: AnimationTimeline,
        document_id: str,
        before_revision: int,
        after_revision: int,
        description: str,
    ) -> None:
        super().__init__(description)
        self._timeline = timeline
        self._document_id = str(document_id or "")
        self._before_revision = int(before_revision)
        self._after_revision = int(after_revision)

    def _live_timeline(self) -> AnimationTimeline:
        document = DocumentRegistry.instance().require(self._document_id)
        controller = document.controller
        candidate = getattr(controller, "_timeline", None)
        if isinstance(candidate, AnimationTimeline):
            return candidate
        return self._timeline

    def _finish(self, revision: int) -> None:
        registry = DocumentRegistry.instance()
        registry.restore_content_revision(self._document_id, revision)
        document = registry.require(self._document_id)
        controller = document.controller
        callback = getattr(controller, "_on_timeline_command_applied", None)
        if callable(callback):
            callback()


class TimelinePropertyCommand(_TimelineCommand):
    """Set one timeline or keyframe property through stable element identity."""

    _is_property_edit = True
    MERGE_WINDOW = 0.3

    def __init__(
        self,
        timeline: AnimationTimeline,
        document_id: str,
        target_id: str,
        field_name: str,
        old_value: Any,
        new_value: Any,
        before_revision: int,
        after_revision: int,
        description: str = "Edit Timeline",
    ) -> None:
        super().__init__(
            timeline,
            document_id,
            before_revision,
            after_revision,
            description,
        )
        self._target_id = str(target_id or "")
        self._field_name = str(field_name or "")
        if not self._field_name:
            raise ValueError("timeline property command requires a field name")
        self._old_value = copy.deepcopy(old_value)
        self._new_value = copy.deepcopy(new_value)

    def _target(self):
        timeline = self._live_timeline()
        if not self._target_id:
            return timeline
        key = timeline.find_keyframe(self._target_id)
        if key is None:
            raise RuntimeError(f"timeline keyframe no longer exists: {self._target_id}")
        return key

    def _apply(self, value: Any, revision: int) -> None:
        setattr(self._target(), self._field_name, copy.deepcopy(value))
        self._finish(revision)

    def execute(self) -> None:
        self._apply(self._new_value, self._after_revision)

    def undo(self) -> None:
        self._apply(self._old_value, self._before_revision)

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        return (
            isinstance(other, TimelinePropertyCommand)
            and self._document_id == other._document_id
            and self._target_id == other._target_id
            and self._field_name == other._field_name
            and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW
        )

    def merge(self, other: "TimelinePropertyCommand") -> None:
        self._new_value = copy.deepcopy(other._new_value)
        self._after_revision = other._after_revision
        self.timestamp = other.timestamp


class TimelineInsertKeyframeCommand(_TimelineCommand):
    """Insert one serialized keyframe at a stable list position."""

    def __init__(
        self,
        timeline: AnimationTimeline,
        document_id: str,
        keyframe: TimelineKeyframe,
        index: int,
        before_revision: int,
        after_revision: int,
        description: str = "Add Timeline Keyframe",
    ) -> None:
        super().__init__(
            timeline,
            document_id,
            before_revision,
            after_revision,
            description,
        )
        self._keyframe_document = copy.deepcopy(keyframe.to_dict())
        self._index = max(0, int(index))

    @property
    def stable_id(self) -> str:
        return str(self._keyframe_document["stable_id"])

    def execute(self) -> None:
        timeline = self._live_timeline()
        if timeline.find_keyframe(self.stable_id) is not None:
            raise RuntimeError(f"timeline keyframe already exists: {self.stable_id}")
        index = min(self._index, len(timeline.keyframes))
        timeline.keyframes.insert(index, TimelineKeyframe.from_dict(self._keyframe_document))
        self._finish(self._after_revision)

    def undo(self) -> None:
        timeline = self._live_timeline()
        key = timeline.find_keyframe(self.stable_id)
        if key is None:
            raise RuntimeError(f"timeline keyframe no longer exists: {self.stable_id}")
        timeline.keyframes.remove(key)
        self._finish(self._before_revision)


class TimelineRemoveKeyframeCommand(_TimelineCommand):
    """Remove one keyframe while retaining enough data for exact restoration."""

    def __init__(
        self,
        timeline: AnimationTimeline,
        document_id: str,
        keyframe: TimelineKeyframe,
        index: int,
        before_revision: int,
        after_revision: int,
        description: str = "Delete Timeline Keyframe",
    ) -> None:
        super().__init__(
            timeline,
            document_id,
            before_revision,
            after_revision,
            description,
        )
        self._keyframe_document = copy.deepcopy(keyframe.to_dict())
        self._index = max(0, int(index))

    @property
    def stable_id(self) -> str:
        return str(self._keyframe_document["stable_id"])

    def execute(self) -> None:
        timeline = self._live_timeline()
        key = timeline.find_keyframe(self.stable_id)
        if key is None:
            raise RuntimeError(f"timeline keyframe no longer exists: {self.stable_id}")
        timeline.keyframes.remove(key)
        self._finish(self._after_revision)

    def undo(self) -> None:
        timeline = self._live_timeline()
        if timeline.find_keyframe(self.stable_id) is not None:
            raise RuntimeError(f"timeline keyframe already exists: {self.stable_id}")
        index = min(self._index, len(timeline.keyframes))
        timeline.keyframes.insert(index, TimelineKeyframe.from_dict(self._keyframe_document))
        self._finish(self._before_revision)
