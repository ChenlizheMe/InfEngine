"""UIEditorCreationMixin — extracted from UIEditorPanel."""
from __future__ import annotations


class UIEditorCreationMixin:
    """UIEditorCreationMixin method group for UIEditorPanel."""

    def _select_element(self, elem_comp):
        """Select a UI element through typed navigation."""
        from Infernux.engine.interaction import (
            EditorInteractionCore,
            SelectionService,
            SelectionTarget,
        )

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("UI Editor selection requires Interaction Core history")
        with core.user_action("Select UI Element"):
            if elem_comp is None:
                SelectionService.instance().clear(
                    reason="ui_editor_clear",
                    record_history=True,
                )
                return
            go = elem_comp.game_object
            canvas_go, _canvas = self._find_canvas_for_object(go)
            if canvas_go is not None:
                self._set_focused_canvas_id(
                    canvas_go.id,
                    record_history=True,
                    description="Focus UI Canvas",
                )
            core.navigation.locate(
                SelectionTarget.scene_object(go.id),
                owner_id="ui_editor",
                reason="ui_editor_select",
                record_history=True,
                activate_panel=False,
            )

    def _select_canvas(self, canvas_go):
        """Select a canvas GameObject and sync with hierarchy/inspector."""
        self._clear_interaction_state()
        if canvas_go is None:
            from Infernux.engine.interaction import SelectionService

            SelectionService.instance().clear(
                reason="ui_editor_clear",
                record_history=True,
            )
            return

        from Infernux.engine.interaction import EditorInteractionCore, SelectionTarget

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("UI Editor selection requires Interaction Core history")
        with core.user_action("Select UI Canvas"):
            self._set_focused_canvas_id(
                canvas_go.id,
                record_history=True,
                description="Focus UI Canvas",
            )
            core.navigation.locate(
                SelectionTarget.scene_object(canvas_go.id),
                owner_id="ui_editor",
                reason="ui_editor_select_canvas",
                record_history=True,
                activate_panel=False,
            )

    def _delete_selected_element(self):
        """Compatibility entry point routed through the global command core."""
        from Infernux.engine.interaction import CommandSource, EditorCommandRegistry

        registry = EditorCommandRegistry.instance()
        if registry is None:
            return False
        return registry.execute(
            "edit.delete",
            source=CommandSource.API,
        ).accepted

    def _create_canvas(self):
        """Create a Canvas through the global editor command."""
        return self._submit_ui_creation("ui.canvas", 0)

    @staticmethod
    def _submit_ui_creation(kind: str, parent_id: int) -> bool:
        from Infernux.engine.interaction import CommandSource, EditorCommandRegistry

        registry = EditorCommandRegistry.instance()
        if registry is None:
            return False
        return registry.execute(
            "scene.create_object",
            source=CommandSource.POINTER,
            payload={"kind": str(kind), "parent_id": int(parent_id or 0)},
        ).accepted

    def _create_text_element(self, canvas_go):
        """Create a UIText child under the given canvas GameObject."""
        return self._submit_ui_creation("ui.text", int(canvas_go.id))

    def _create_image_element(self, canvas_go):
        """Create a UIImage child under the given canvas GameObject."""
        return self._submit_ui_creation("ui.image", int(canvas_go.id))

    def _create_button_element(self, canvas_go):
        """Create a UIButton child under the given canvas GameObject."""
        return self._submit_ui_creation("ui.button", int(canvas_go.id))

