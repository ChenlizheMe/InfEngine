"""BootstrapSelectionMixin — extracted from EditorBootstrap."""
from __future__ import annotations

"""
EditorBootstrap — structured editor initialization.

Breaks the monolithic ``release_engine()`` startup path into explicit
startup steps. Each step is a separate method, closures become instance
methods, and panel/manager references live on the bootstrap instance.
"""


from Infernux.engine.ui.event_bus import EditorEvent


class BootstrapSelectionMixin:
    """BootstrapSelectionMixin method group for EditorBootstrap."""

    def _wire_selection_system(self):
        hierarchy = self.hierarchy
        inspector = self.inspector_panel
        project = self.project_panel
        scene_view = self.scene_view
        event_bus = self.event_bus

        hierarchy.on_selection_changed = self._on_hierarchy_selected
        project.on_file_selected = self._on_project_selected
        project.on_empty_area_clicked = self._on_project_panel_empty_clicked
        scene_view.set_on_object_picked(self._on_scene_view_picked)
        scene_view.set_on_box_select(self._on_box_select_done)
        hierarchy.on_double_click_focus = (
            lambda oid: self._fly_to_object_by_id(oid)
        )

        # Let structural undo commands restore selection via the same
        # pipeline as SelectionCommand (updates inspector, outline, etc.).
        from Infernux.engine.undo import (
            CreateGameObjectCommand,
            DeleteGameObjectCommand,
            DeleteGameObjectsCommand,
        )
        CreateGameObjectCommand._selection_restore_fn = self._apply_selection_undo
        DeleteGameObjectCommand._selection_restore_fn = self._apply_selection_undo
        DeleteGameObjectsCommand._selection_restore_fn = self._apply_selection_undo

    def _set_outline(self, object_id: int, object_ids=None):
        native = self.engine.get_native_engine()
        if not native:
            return
        if object_ids is None:
            from Infernux.engine.ui.selection_manager import SelectionManager
            ids = SelectionManager.instance().get_ids()
        else:
            ids = list(object_ids)
        ids = [int(selected_id) for selected_id in ids if selected_id]
        if ids:
            native.set_selection_outlines(ids)
        elif object_id:
            native.set_selection_outlines([int(object_id)])
        else:
            native.clear_selection_outline()

    def _fly_to_object_by_id(self, object_id: int):
        """Resolve object ID and fly scene view to it."""
        if not object_id:
            return
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(object_id) if scene else None
        if obj:
            self.scene_view.fly_to_object(obj)

    def _on_hierarchy_selected(self, object_id: int):
        """C++ HierarchyPanel calls this with uint64_t primary ID (0 = none)."""
        from Infernux.engine.ui.selection_manager import SelectionManager
        sel = SelectionManager.instance()
        new_ids = sel.get_ids()
        primary_id = sel.get_primary()

        # Track selection state for structural command restoration. Pure
        # navigation does not occupy the scene-edit Undo stack.
        self._record_editor_selection_change(new_ids, "")

        # Resolve ID → game object for inspector & event bus
        obj = None
        if primary_id:
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            obj = scene.find_by_id(primary_id) if scene else None

        self.inspector_panel.set_selected_object_id(primary_id or 0)
        if primary_id:
            self.project_panel.clear_selection()
        self._set_outline(primary_id, new_ids)
        self.event_bus.emit(EditorEvent.SELECTION_CHANGED, obj)

    def _on_project_selected(self, path):
        self._record_editor_selection_change([], path or "")
        self._inspector_set_selected_file(path)
        if path:
            self.hierarchy.clear_selection_and_notify()
        self.event_bus.emit(EditorEvent.FILE_SELECTED, path)

    def _on_project_panel_empty_clicked(self):
        self._record_editor_selection_change([], "")
        self.project_panel.clear_selection()
        self.hierarchy.clear_selection_and_notify()

    def _on_scene_view_picked(self, object_id: int, ctrl: bool = False):
        from Infernux.engine.ui.selection_manager import SelectionManager
        sel = SelectionManager.instance()

        if ctrl and object_id:
            sel.toggle(object_id)
        elif object_id:
            sel.select(object_id)
        elif not ctrl:
            sel.clear()

        new_ids = sel.get_ids()
        primary = sel.get_primary()

        # Track selection without adding a scene-edit Undo entry.
        self._record_editor_selection_change(new_ids, "")

        self._set_outline(primary, new_ids)

        if primary:
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            obj = scene.find_by_id(primary) if scene else None
            self.inspector_panel.set_selected_object_id(primary)
            self.project_panel.clear_selection()
            # Expand hierarchy to reveal the picked object
            if obj:
                self.hierarchy.expand_to_object(obj.id)
            self.event_bus.emit(EditorEvent.SELECTION_CHANGED, obj)
        else:
            self.project_panel.clear_selection()
            self.inspector_panel.set_selected_object_id(0)
            self.event_bus.emit(EditorEvent.SELECTION_CHANGED, None)

    def _on_box_select_done(self, primary_obj):
        from Infernux.engine.ui.selection_manager import SelectionManager
        sel = SelectionManager.instance()
        new_ids = sel.get_ids()
        self._record_editor_selection_change(new_ids, "")

        self.inspector_panel.set_selected_object_id(primary_obj.id if primary_obj else 0)
        if primary_obj:
            self.project_panel.clear_selection()
            self.hierarchy.expand_to_object(primary_obj.id)
        else:
            self.project_panel.clear_selection()
        self._set_outline(primary_obj.id if primary_obj else 0, new_ids)
        self.event_bus.emit(EditorEvent.SELECTION_CHANGED, primary_obj)

    def _navigate_console_entry_to_object(self, object_id: int) -> bool:
        """Reveal a console-targeted scene object in Hierarchy and Inspector."""
        if not object_id:
            return False

        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(object_id) if scene else None
        if obj is None:
            return False

        if self.window_manager is not None:
            if not self.window_manager.is_window_open("hierarchy"):
                self.window_manager.open_window("hierarchy")
            if not self.window_manager.is_window_open("inspector"):
                self.window_manager.open_window("inspector")

        self.hierarchy.set_selected_object_by_id(object_id, clear_search=True)

        if not self.hierarchy.get_ui_mode():
            self.inspector_panel.set_selected_object_id(object_id)
            self.project_panel.clear_selection()
            self._set_outline(object_id, [object_id])
            self.event_bus.emit(EditorEvent.SELECTION_CHANGED, obj)

        return True

    def _record_editor_selection_change(self, new_ids: list, file_path: str):
        """Record hierarchy/project navigation as a non-dirty Undo step."""
        from Infernux.engine.ui.asset_resource_preview import release_all_preview_authoring
        from Infernux.engine.undo import EditorSelectionCommand, UndoManager

        release_all_preview_authoring()
        previous_ids = list(self._prev_selection_ids)
        previous_file = self._prev_selected_file or ""
        next_ids = list(new_ids)
        next_file = file_path or ""
        self._prev_selection_ids = next_ids
        self._prev_selected_file = next_file

        if previous_ids == next_ids and previous_file == next_file:
            return
        manager = UndoManager.instance()
        if manager is None or manager.is_executing:
            return
        manager.record(EditorSelectionCommand(
            previous_ids,
            previous_file,
            next_ids,
            next_file,
            self._apply_editor_selection_undo,
        ))

    def _record_selection_change(self, new_ids: list):
        self._record_editor_selection_change(new_ids, "")

    def _apply_editor_selection_undo(self, ids: list, file_path: str):
        from Infernux.engine.ui.selection_manager import SelectionManager
        sel = SelectionManager.instance()
        file_path = file_path or ""

        if file_path:
            sel.clear()
            self._prev_selection_ids = []
            self._prev_selected_file = file_path
            self._set_outline(0)
            self.hierarchy.clear_selection_and_notify()
            self.project_panel.set_selected_file(file_path)
            self._inspector_set_selected_file(file_path)
            self.event_bus.emit(EditorEvent.FILE_SELECTED, file_path)
            return

        sel.set_ids(ids)
        self._prev_selection_ids = list(ids)
        self._prev_selected_file = ""

        primary = sel.get_primary()
        self._set_outline(primary, ids)
        self.project_panel.clear_selection()
        self._inspector_set_selected_file("")

        if primary:
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            obj = scene.find_by_id(primary) if scene else None
            self.inspector_panel.set_selected_object_id(primary)
            if obj:
                self.hierarchy.expand_to_object(obj.id)
            self.event_bus.emit(EditorEvent.SELECTION_CHANGED, obj)
        else:
            self.inspector_panel.set_selected_object_id(0)
            self.event_bus.emit(EditorEvent.SELECTION_CHANGED, None)

    def _apply_selection_undo(self, ids: list):
        """Restore a selection state during undo/redo."""
        self._apply_editor_selection_undo(ids, "")

