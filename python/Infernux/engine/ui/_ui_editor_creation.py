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
from contextlib import nullcontext as _nullcontext
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

    def _record_ui_create(
        self,
        object_id: int,
        description: str = "Create UI Element",
        *,
        before_selection=None,
        after_selection=None,
    ):
        """Record a UI object creation through the undo system."""
        from Infernux.engine.undo import UndoManager, CreateGameObjectCommand
        mgr = UndoManager.instance()
        if mgr:
            mgr.record(CreateGameObjectCommand(
                object_id,
                description,
                before_selection=before_selection,
                after_selection=after_selection,
            ))

    def _create_canvas(self):
        """Create a new Canvas GameObject in the scene."""
        from Infernux.lib import SceneManager
        from Infernux.ui import UICanvas as UICanvasCls
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            return
        from Infernux.engine.interaction import SelectionService

        selection = SelectionService.instance()
        before_selection = selection.snapshot
        mgr = self._get_undo_mgr()
        ctx_mgr = mgr.suppress() if mgr else _nullcontext()
        go = None
        with ctx_mgr:
            go = scene.create_game_object("Canvas")
            if go:
                go.add_py_component(UICanvasCls())
                self._focused_canvas_id = go.id
                invalidate_canvas_cache()
                self._select_canvas(go)
        if go:
            self._record_ui_create(
                go.id,
                "Create Canvas",
                before_selection=before_selection,
                after_selection=selection.snapshot,
            )

    def _create_ui_element(self, canvas_go, component_cls, go_name: str,
                           default_size=None, default_pos=None,
                           undo_label: str = "Create UI Element"):
        """Generic helper to create a UI element under a canvas.

        Args:
            canvas_go: Parent canvas game-object.
            component_cls: UI component class to instantiate.
            go_name: Name for the new game-object.
            default_size: Optional (w, h) to set before adding the component.
            default_pos: Optional (x, y) centered-anchor offset.
            undo_label: Description for the undo system.
        """
        from Infernux.lib import SceneManager
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            return
        from Infernux.engine.interaction import SelectionService

        selection = SelectionService.instance()
        before_selection = selection.snapshot
        mgr = self._get_undo_mgr()
        ctx_mgr = mgr.suppress() if mgr else _nullcontext()
        go = None
        with ctx_mgr:
            go = scene.create_game_object(go_name)
            if go:
                go.set_parent(canvas_go)
                try:
                    comp = component_cls()
                except Exception as _exc:
                    Debug.log_error(f"[UIEditor] Failed to create {component_cls.__name__}: {_exc}")
                    return
                if default_size is not None:
                    comp.width = float(default_size[0])
                    comp.height = float(default_size[1])
                try:
                    go.add_py_component(comp)
                except Exception as _exc:
                    Debug.log_error(f"[UIEditor] add_py_component failed for {go_name}: {_exc}")
                    return
                if default_pos is not None:
                    # Find parent canvas component to set centered alignment
                    canvas_comp = None
                    for c in canvas_go.get_py_components():
                        from Infernux.ui import UICanvas
                        if isinstance(c, UICanvas):
                            canvas_comp = c
                            break
                    if canvas_comp:
                        from Infernux.ui.enums import ScreenAlignH, ScreenAlignV
                        comp.align_h = ScreenAlignH.Center
                        comp.align_v = ScreenAlignV.Center
                        comp.x = float(default_pos[0])
                        comp.y = float(default_pos[1])
                self._select_element(comp)
                invalidate_canvas_cache()
                if self._hierarchy_panel:
                    self._hierarchy_panel.set_pending_expand_id(canvas_go.id)
        if go:
            self._record_ui_create(
                go.id,
                undo_label,
                before_selection=before_selection,
                after_selection=selection.snapshot,
            )

    def _create_text_element(self, canvas_go):
        """Create a UIText child under the given canvas GameObject."""
        from Infernux.ui import UIText as UITextCls
        self._create_ui_element(
            canvas_go, UITextCls, "Text",
            default_pos=Theme.UI_EDITOR_NEW_TEXT_POS,
            undo_label="Create Text",
        )

    def _create_image_element(self, canvas_go):
        """Create a UIImage child under the given canvas GameObject."""
        from Infernux.ui import UIImage as UIImageCls
        self._create_ui_element(
            canvas_go, UIImageCls, "Image",
            default_size=Theme.UI_EDITOR_NEW_IMAGE_SIZE,
            default_pos=Theme.UI_EDITOR_NEW_IMAGE_POS,
            undo_label="Create Image",
        )

    def _create_button_element(self, canvas_go):
        """Create a UIButton child under the given canvas GameObject."""
        from Infernux.ui import UIButton as UIButtonCls
        self._create_ui_element(
            canvas_go, UIButtonCls, "Button",
            default_size=Theme.UI_EDITOR_NEW_BUTTON_SIZE,
            default_pos=Theme.UI_EDITOR_NEW_BUTTON_POS,
            undo_label="Create Button",
        )

