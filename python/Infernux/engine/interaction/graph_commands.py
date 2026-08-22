"""Global command authority for graph editor interaction."""

from __future__ import annotations

from typing import Any, Optional


class GraphCommandService:
    """Route shared graph intents to the currently active graph view."""

    _instance: Optional["GraphCommandService"] = None

    _COMMANDS = (
        ("graph.center_view", "Center View"),
        ("graph.reset_zoom", "Reset Zoom"),
        ("graph.add_node", "Add Node"),
        ("graph.create_node", "Create Node"),
        ("graph.workspace.add", "Add Graph Workspace Item"),
    )

    def __init__(self, panels: Any) -> None:
        self._panels = panels
        GraphCommandService._instance = self

    @classmethod
    def instance(cls) -> Optional["GraphCommandService"]:
        return cls._instance

    def register_commands(self, registry: Any) -> None:
        from .commands import EditorCommand

        for command_id, display_name in self._COMMANDS:
            registry.register(
                EditorCommand(
                    command_id,
                    lambda context, identifier=command_id: (
                        self._panels.execute_active(context, identifier)
                    ),
                    display_name=display_name,
                    category="Graph",
                    can_execute=(
                        lambda context, identifier=command_id: (
                            self._panels.can_execute_active(context, identifier)
                        )
                    ),
                )
            )

    def shutdown(self) -> None:
        self._panels = None
        if GraphCommandService._instance is self:
            GraphCommandService._instance = None


__all__ = ["GraphCommandService"]
