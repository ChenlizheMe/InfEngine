"""Command authority for RenderStack component authoring."""

from __future__ import annotations

import copy
from typing import Any, Optional

from .action_journal import ActionOrigin


_UNSET = object()


class RenderStackCommandService:
    """Route Inspector and automation RenderStack edits through one history."""

    _instance: Optional["RenderStackCommandService"] = None

    def __init__(self) -> None:
        RenderStackCommandService._instance = self

    @classmethod
    def instance(cls) -> Optional["RenderStackCommandService"]:
        return cls._instance

    def shutdown(self) -> None:
        if RenderStackCommandService._instance is self:
            RenderStackCommandService._instance = None

    def register_commands(self, registry) -> None:
        """Register the only public mutation entry points for RenderStack authoring."""
        from .commands import EditorCommand

        def stack_from(context):
            return context.payload.get("stack")

        def origin_from(context):
            from .commands import CommandSource

            return (
                ActionOrigin.AUTOMATION
                if context.source is CommandSource.AUTOMATION
                else ActionOrigin.USER
            )

        registry.register(
            EditorCommand(
                "renderstack.set_pipeline",
                lambda context: self.set_pipeline(
                    stack_from(context),
                    context.payload.get("pipeline", ""),
                    origin=origin_from(context),
                ),
                display_name="Set Render Pipeline",
                category="Rendering",
                can_execute=lambda context: stack_from(context) is not None,
            )
        )
        registry.register(
            EditorCommand(
                "renderstack.set_parameter",
                lambda context: self.set_pipeline_parameter(
                    stack_from(context),
                    context.payload.get("field", ""),
                    context.payload.get("value"),
                    origin=origin_from(context),
                    description=str(context.payload.get("description", "") or ""),
                    old_value=context.payload.get("old_value", _UNSET),
                ),
                display_name="Set Render Pipeline Parameter",
                category="Rendering",
                can_execute=lambda context: (
                    stack_from(context) is not None
                    and bool(str(context.payload.get("field", "") or "").strip())
                ),
            )
        )
        registry.register(
            EditorCommand(
                "renderstack.set_effect_slots",
                lambda context: self.set_effect_stage_slots(
                    stack_from(context),
                    context.payload.get("stage_id", ""),
                    context.payload.get("slots", ()),
                    origin=origin_from(context),
                    description=str(
                        context.payload.get("description", "Edit Render Effects")
                        or "Edit Render Effects"
                    ),
                ),
                display_name="Edit Render Effects",
                category="Rendering",
                can_execute=lambda context: (
                    stack_from(context) is not None
                    and bool(str(context.payload.get("stage_id", "") or "").strip())
                    and "slots" in context.payload
                ),
            )
        )

    def set_pipeline(
        self,
        stack: Any,
        pipeline_name: str,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> bool:
        from Infernux.engine.undo import RenderStackSetPipelineCommand

        old_value = str(stack.pipeline_class_name)
        new_value = str(pipeline_name or "").strip() or stack.DEFAULT_PIPELINE_NAME
        if old_value == new_value:
            return False
        self._execute(
            RenderStackSetPipelineCommand(stack, old_value, new_value),
            origin,
        )
        return True

    def set_pipeline_parameter(
        self,
        stack: Any,
        field_name: str,
        value: Any,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
        description: str = "",
        old_value: Any = _UNSET,
    ) -> bool:
        from Infernux.engine.undo import RenderStackFieldCommand

        name = str(field_name or "").strip()
        if not name:
            raise ValueError("RenderStack parameter name must not be empty")
        pipeline = stack.pipeline
        previous = copy.deepcopy(
            getattr(pipeline, name) if old_value is _UNSET else old_value
        )
        new_value = copy.deepcopy(value)
        if previous == new_value:
            return False
        self._execute(
            RenderStackFieldCommand(
                stack,
                pipeline,
                name,
                previous,
                new_value,
                description or f"Set {name}",
            ),
            origin,
        )
        return True

    def set_effect_stage_slots(
        self,
        stack: Any,
        stage_id: str,
        slots: Any,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
        description: str = "Edit Render Effects",
    ) -> bool:
        from Infernux.engine.undo import RenderStackEffectSlotsCommand

        stage = str(stage_id or "").strip()
        old_slots = list(stack.get_effect_stage_slots(stage))
        new_slots = list(slots)
        if old_slots == new_slots:
            return False
        self._execute(
            RenderStackEffectSlotsCommand(
                stack,
                stage,
                old_slots,
                new_slots,
                description,
            ),
            origin,
        )
        return True

    @staticmethod
    def _execute(command: Any, origin: ActionOrigin) -> None:
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            command.dispose()
            raise RuntimeError("RenderStack edit requires the global Action Journal")
        if not manager.execute(command, origin=ActionOrigin(origin)):
            raise RuntimeError(f"RenderStack edit was rejected: {command.description}")


def submit_renderstack_command(
    command_id: str,
    *,
    source=None,
    **payload,
) -> bool:
    """Submit one RenderStack intent through the global command registry."""
    from .commands import CommandSource
    from .session import EditorInteractionCore

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("RenderStack edit requires the editor interaction core")
    result = core.commands.execute(
        command_id,
        source=CommandSource.API if source is None else CommandSource(source),
        payload=payload,
    )
    if not result.accepted:
        raise RuntimeError(
            result.message or f"RenderStack command was rejected: {command_id}"
        )
    return True
