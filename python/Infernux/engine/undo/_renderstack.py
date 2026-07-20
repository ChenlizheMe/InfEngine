"""Undo commands for the canonical RenderStack model."""

from __future__ import annotations

from typing import Any

from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._property_commands import SetPropertyCommand


class RenderStackFieldCommand(SetPropertyCommand):
    """Set a pipeline or EffectStage field and invalidate the graph."""

    def __init__(self, stack: Any, target: Any, field_name: str,
                 old_value: Any, new_value: Any, description: str = ""):
        super().__init__(target, field_name, old_value, new_value,
                         description or f"Set {field_name}")
        self._stack = stack

    def execute(self) -> None:
        super().execute()
        self._stack.invalidate_graph()

    def undo(self) -> None:
        super().undo()
        self._stack.invalidate_graph()

    def redo(self) -> None:
        super().redo()
        self._stack.invalidate_graph()


class RenderStackSetPipelineCommand(UndoCommand):
    _is_property_edit = True

    def __init__(self, stack, old_pipeline: str, new_pipeline: str,
                 description: str = "Set Render Pipeline"):
        super().__init__(description)
        self._stack = stack
        self._old_pipeline = old_pipeline
        self._new_pipeline = new_pipeline

    def execute(self) -> None:
        self._stack.set_pipeline(self._new_pipeline)

    def undo(self) -> None:
        self._stack.set_pipeline(self._old_pipeline)

    def redo(self) -> None:
        self._stack.set_pipeline(self._new_pipeline)
