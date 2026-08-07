"""Undo commands for the canonical RenderStack model."""

from __future__ import annotations

import copy
from typing import Any

from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import (
    _comp_type_name_of,
    _game_object_id_of,
    _resolve_target,
)
from Infernux.engine.undo._property_commands import SetPropertyCommand


class RenderStackFieldCommand(SetPropertyCommand):
    """Set one pipeline field and commit its owning RenderStack document."""

    def __init__(self, stack: Any, target: Any, field_name: str,
                 old_value: Any, new_value: Any, description: str = ""):
        super().__init__(target, field_name, old_value, new_value,
                         description or f"Set {field_name}")
        self._stack = stack
        self._stack_game_object_id = _game_object_id_of(stack)
        self._stack_type_name = _comp_type_name_of(stack)
        self._pipeline_class_name = str(stack.pipeline_class_name or "")

    def _live_stack(self):
        return _resolve_target(
            self._stack,
            self._stack_game_object_id,
            self._stack_type_name,
        )

    def _commit_projection(self, value) -> None:
        # The pipeline object is a live projection. The RenderStack parameter
        # document is the persistent authority and must move in the same undo
        # step, including after a pipeline/graph rebuild.
        stack = self._live_stack()
        if stack is None:
            raise RuntimeError("RenderStack field target is no longer available")
        stack.set_pipeline_parameter(
            self._prop_name,
            value,
            pipeline_class_name=self._pipeline_class_name,
        )

    def execute(self) -> None:
        self._commit_projection(self._new_value)

    def undo(self) -> None:
        self._commit_projection(self._old_value)

    def redo(self) -> None:
        self._commit_projection(self._new_value)


class RenderStackSetPipelineCommand(UndoCommand):
    _is_property_edit = True

    def __init__(self, stack, old_pipeline: str, new_pipeline: str,
                 description: str = "Set Render Pipeline"):
        super().__init__(description)
        self._stack = stack
        self._stack_game_object_id = _game_object_id_of(stack)
        self._stack_type_name = _comp_type_name_of(stack)
        self._old_pipeline = old_pipeline
        self._new_pipeline = new_pipeline

    def _live_stack(self):
        return _resolve_target(
            self._stack,
            self._stack_game_object_id,
            self._stack_type_name,
        )

    def _apply(self, pipeline_name: str) -> None:
        stack = self._live_stack()
        if stack is None:
            raise RuntimeError("RenderStack pipeline target is no longer available")
        stack.set_pipeline(pipeline_name)

    def execute(self) -> None:
        self._apply(self._new_pipeline)

    def undo(self) -> None:
        self._apply(self._old_pipeline)

    def redo(self) -> None:
        self._apply(self._new_pipeline)


class RenderStackEffectSlotsCommand(UndoCommand):
    """Replace one stable EffectStage slot list as one replayable edit."""

    _is_property_edit = True

    def __init__(
        self,
        stack: Any,
        stage_id: str,
        old_slots: Any,
        new_slots: Any,
        description: str = "Edit Render Effects",
    ) -> None:
        super().__init__(description)
        self._stack = stack
        self._stack_game_object_id = _game_object_id_of(stack)
        self._stack_type_name = _comp_type_name_of(stack)
        self._stage_id = str(stage_id or "")
        self._old_slots = copy.deepcopy(list(old_slots))
        self._new_slots = copy.deepcopy(list(new_slots))

    def _live_stack(self):
        return _resolve_target(
            self._stack,
            self._stack_game_object_id,
            self._stack_type_name,
        )

    def _apply(self, slots: Any) -> None:
        stack = self._live_stack()
        if stack is None:
            raise RuntimeError("RenderStack effect target is no longer available")
        stack.set_effect_stage_slots(self._stage_id, copy.deepcopy(list(slots)))

    def execute(self) -> None:
        self._apply(self._new_slots)

    def undo(self) -> None:
        self._apply(self._old_slots)

    def redo(self) -> None:
        self._apply(self._new_slots)
