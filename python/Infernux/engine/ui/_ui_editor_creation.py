"""UIEditorCreationMixin — extracted from UIEditorPanel."""
from __future__ import annotations

"""UI Editor panel — Figma-style 2D canvas editor for screen-space UI layout.

Displays the selected UICanvas at its reference resolution and lets users
visually position UI elements via drag.  Max zoom is 100% (1:1 pixels).

Docked alongside Scene / Game views.
"""

import configparser
import math
import os
from time import perf_counter as _pc

from typing import Optional
from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from Infernux.engine.project_context import get_project_root
from Infernux.ui.enums import TextResizeMode
from Infernux.ui.inx_ui_screen_component import clear_rect_cache
from Infernux.ui.ui_texture_cache import get_shared_cache as _get_tex_cache
from Infernux.ui.ui_render_dispatch import dispatch as _ui_dispatch
from Infernux.ui.ui_canvas_utils import collect_canvases_with_go
from .editor_panel import EditorPanel
from .panel_registry import editor_panel
from .editor_icons import EditorIcons
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiMouseCursor
from .ui_editor_shortcuts import UIEditorInput
from Infernux.debug import Debug
from .imgui_keys import (
    KEY_LEFT_ARROW, KEY_RIGHT_ARROW, KEY_UP_ARROW, KEY_DOWN_ARROW,
)


class UIEditorCreationMixin:
    """UIEditorCreationMixin method group for UIEditorPanel."""

    def _select_element(self, elem_comp):
        """Select a UI element and sync with hierarchy/inspector."""
        self._selected_element_comp = elem_comp
        if self._on_selection_changed:
            if elem_comp is not None:
                go = elem_comp.game_object
                self._focus_canvas_for_object(go)
                self._on_selection_changed(go)
                # Auto-expand hierarchy to reveal this object
                if self._hierarchy_panel and go is not None:
                    self._hierarchy_panel.expand_to_object(go.id)
            else:
                self._on_selection_changed(None)

    def _select_canvas(self, canvas_go):
        """Select a canvas GameObject and sync with hierarchy/inspector."""
        self._clear_interaction_state()
        if canvas_go is None:
            if self._on_selection_changed:
                self._on_selection_changed(None)
            return

        self._focused_canvas_id = canvas_go.id
        if self._on_selection_changed:
            self._on_selection_changed(canvas_go)
        if self._hierarchy_panel is not None:
            self._hierarchy_panel.expand_to_object(canvas_go.id)

    def _delete_selected_element(self):
        """Delete the currently selected UI element's GameObject via undo system."""
        elem = self._selected_element_comp
        if elem is None:
            return
        go = elem.game_object
        self._selected_element_comp = None
        self._dragging = False
        if go is not None:
            from Infernux.engine.undo import UndoManager, DeleteGameObjectCommand
            mgr = UndoManager.instance()
            if mgr:
                mgr.execute(DeleteGameObjectCommand(go.id, "Delete UI Element"))
            else:
                from Infernux.lib import SceneManager
                scene = SceneManager.instance().get_active_scene()
                if scene is not None:
                    scene.destroy_game_object(go)
                from Infernux.engine.interaction import SelectionService

                SelectionService.instance().clear(
                    reason="delete_ui_element",
                    record_history=False,
                )

    def _create_canvas(self):
        """Create a Canvas through the shared hierarchy transaction."""
        from Infernux.lib import SceneManager
        from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            return
        try:
            created = HierarchyCreationService.instance().create(
                "ui.canvas",
                selection_owner_id="ui_editor",
                selection_reason="ui_editor_create_canvas",
            )
        except Exception as exc:
            Debug.log_error(f"[UIEditor] Failed to create Canvas: {exc}")
            return
        go = scene.find_by_id(int(created["id"]))
        if go is None:
            return
        self._clear_interaction_state()
        self._focused_canvas_id = go.id

    def _create_ui_element(self, canvas_go, kind: str, component_cls, go_name: str,
                           default_size=None, default_pos=None):
        """Create a UI element through the shared hierarchy transaction.

        Args:
            canvas_go: Parent canvas game-object.
            kind: Shared hierarchy creation kind.
            component_cls: UI component class to instantiate.
            go_name: Name for the new game-object.
            default_size: Optional (w, h) to set before adding the component.
            default_pos: Optional (x, y) centered-anchor offset.
        """
        from Infernux.lib import SceneManager
        from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            return
        created_component = []

        def configure_created(go):
            comp = go.get_py_component(component_cls)
            if comp is None:
                raise RuntimeError(
                    f"{go_name} creation did not attach {component_cls.__name__}"
                )
            if default_size is not None:
                comp.width = float(default_size[0])
                comp.height = float(default_size[1])
            if default_pos is not None:
                from Infernux.ui import UICanvas

                canvas_comp = next(
                    (
                        candidate
                        for candidate in canvas_go.get_py_components()
                        if isinstance(candidate, UICanvas)
                    ),
                    None,
                )
                if canvas_comp is not None:
                    from Infernux.ui.enums import ScreenAlignH, ScreenAlignV

                    comp.align_h = ScreenAlignH.Center
                    comp.align_v = ScreenAlignV.Center
                    comp.x = float(default_pos[0])
                    comp.y = float(default_pos[1])
            created_component.append(comp)

        try:
            created = HierarchyCreationService.instance().create(
                kind,
                parent_id=int(canvas_go.id),
                name=go_name,
                selection_owner_id="ui_editor",
                selection_reason=f"ui_editor_create_{kind.rsplit('.', 1)[-1]}",
                configure_created=configure_created,
            )
        except Exception as exc:
            Debug.log_error(f"[UIEditor] Failed to create {go_name}: {exc}")
            return

        go = scene.find_by_id(int(created["id"]))
        if go is None or not created_component:
            return
        self._focused_canvas_id = int(canvas_go.id)
        self._selected_element_comp = created_component[0]

    def _create_text_element(self, canvas_go):
        """Create a UIText child under the given canvas GameObject."""
        from Infernux.ui import UIText as UITextCls
        self._create_ui_element(
            canvas_go, "ui.text", UITextCls, "Text",
            default_pos=Theme.UI_EDITOR_NEW_TEXT_POS,
        )

    def _create_image_element(self, canvas_go):
        """Create a UIImage child under the given canvas GameObject."""
        from Infernux.ui import UIImage as UIImageCls
        self._create_ui_element(
            canvas_go, "ui.image", UIImageCls, "Image",
            default_size=Theme.UI_EDITOR_NEW_IMAGE_SIZE,
            default_pos=Theme.UI_EDITOR_NEW_IMAGE_POS,
        )

    def _create_button_element(self, canvas_go):
        """Create a UIButton child under the given canvas GameObject."""
        from Infernux.ui import UIButton as UIButtonCls
        self._create_ui_element(
            canvas_go, "ui.button", UIButtonCls, "Button",
            default_size=Theme.UI_EDITOR_NEW_BUTTON_SIZE,
            default_pos=Theme.UI_EDITOR_NEW_BUTTON_POS,
        )

