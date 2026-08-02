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
        from Infernux.engine.interaction import SelectionService

        hierarchy = self.hierarchy
        inspector = self.inspector_panel
        project = self.project_panel
        scene_view = self.scene_view
        event_bus = self.event_bus

        hierarchy.on_selection_changed = self._on_hierarchy_selected
        project.on_file_selected = None
        project.on_selection_changed = self._on_project_selection_changed
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
        self._prev_selection_snapshot = SelectionService.instance().snapshot

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
        self._synchronize_object_selection(record=True, owner_id="hierarchy")

    def _synchronize_object_selection(
        self,
        *,
        record: bool,
        reveal_primary: bool = False,
        owner_id: str = "hierarchy",
    ):
        """Publish the global object selection to every editor surface."""
        from Infernux.engine.ui.selection_manager import SelectionManager
        sel = SelectionManager.instance()
        new_ids = sel.get_ids()
        primary_id = sel.get_primary()

        if record:
            self._record_editor_selection_change(new_ids, "", owner_id=owner_id)
        else:
            self._prev_selection_ids = list(new_ids)
            self._prev_selected_file = ""

        # Resolve ID → game object for inspector & event bus
        obj = None
        if primary_id:
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            obj = scene.find_by_id(primary_id) if scene else None

        self.inspector_panel.set_selected_object_id(primary_id or 0)
        self.project_panel.clear_selection(False)
        self._set_outline(primary_id, new_ids)
        if reveal_primary and obj:
            self.hierarchy.expand_to_object(obj.id)
        self.event_bus.emit(EditorEvent.SELECTION_CHANGED, obj)

    def _on_project_selected(self, path):
        from Infernux.engine.interaction import SelectionService, SelectionTarget

        if path:
            SelectionService.instance().select(
                SelectionTarget.asset(path),
                owner_id="project",
                reason="project_select",
                record_history=False,
            )
        else:
            SelectionService.instance().clear(
                reason="project_clear",
                record_history=False,
            )
        self._record_editor_selection_change([], path or "")
        self._set_outline(0, [])
        self._inspector_set_selected_file(path)
        self.event_bus.emit(EditorEvent.FILE_SELECTED, path)

    def _on_project_selection_changed(self, paths, primary_path):
        from Infernux.engine.interaction import (
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )

        targets = tuple(SelectionTarget.asset(path) for path in paths if path)
        primary = SelectionTarget.asset(primary_path) if primary_path else None
        snapshot = SelectionSnapshot.create(
            targets,
            owner_id="project" if targets else "",
            primary=primary,
        )
        SelectionService.instance().apply_snapshot(
            snapshot,
            reason="project_select",
            record_history=False,
        )
        self._record_selection_snapshot(snapshot)
        self._set_outline(0, [])
        self._inspector_set_selected_file(primary_path or "")
        self.event_bus.emit(EditorEvent.FILE_SELECTED, primary_path or "")

    def _on_project_panel_empty_clicked(self):
        from Infernux.engine.ui.selection_manager import SelectionManager

        SelectionManager.instance().clear()
        self._synchronize_object_selection(record=True, owner_id="project")

    def _on_scene_view_picked(self, object_id: int, ctrl: bool = False):
        from Infernux.engine.ui.selection_manager import SelectionManager
        sel = SelectionManager.instance()

        if ctrl and object_id:
            sel.toggle(object_id)
        elif object_id:
            sel.select(object_id)
        elif not ctrl:
            sel.clear()

        self._synchronize_object_selection(
            record=True,
            reveal_primary=True,
            owner_id="scene_view",
        )

    def _on_box_select_done(self, primary_obj):
        self._synchronize_object_selection(
            record=True,
            reveal_primary=True,
            owner_id="scene_view",
        )

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
            self._synchronize_object_selection(
                record=True,
                reveal_primary=True,
                owner_id="hierarchy",
            )

        return True

    def _record_editor_selection_change(
        self,
        new_ids: list,
        file_path: str,
        *,
        owner_id: str = "hierarchy",
    ):
        """Record hierarchy/project navigation as a non-dirty Undo step."""
        from Infernux.engine.interaction import (
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )
        next_ids = list(new_ids)
        next_file = file_path or ""
        if next_file:
            next_snapshot = SelectionSnapshot.create(
                (SelectionTarget.asset(next_file),),
                owner_id="project",
            )
        else:
            targets = tuple(
                SelectionTarget.scene_object(object_id)
                for object_id in next_ids
                if int(object_id) > 0
            )
            next_snapshot = SelectionSnapshot.create(
                targets,
                owner_id=owner_id if targets else "",
            )

        SelectionService.instance().apply_snapshot(
            next_snapshot,
            reason="editor_surface",
            record_history=False,
        )
        self._record_selection_snapshot(next_snapshot)

    def _record_selection_snapshot(self, next_snapshot):
        from Infernux.engine.ui.asset_resource_preview import release_all_preview_authoring
        from Infernux.engine.interaction import (
            SelectionDomain,
            SelectionSnapshot,
            SelectionTarget,
        )
        from Infernux.engine.undo import GlobalSelectionCommand, UndoManager

        release_all_preview_authoring()

        primary = next_snapshot.primary
        next_file = (
            primary.target_id
            if primary is not None and primary.domain is SelectionDomain.ASSET
            else ""
        )
        next_ids = [
            target.scene_object_id()
            for target in next_snapshot.targets
            if target.domain is SelectionDomain.SCENE_OBJECT
        ]
        previous_snapshot = getattr(self, "_prev_selection_snapshot", None)
        if previous_snapshot is None:
            previous_file = getattr(self, "_prev_selected_file", "") or ""
            previous_ids = list(getattr(self, "_prev_selection_ids", []) or [])
            if previous_file:
                previous_snapshot = SelectionSnapshot.create(
                    (SelectionTarget.asset(previous_file),),
                    owner_id="project",
                )
            else:
                previous_targets = tuple(
                    SelectionTarget.scene_object(object_id)
                    for object_id in previous_ids
                    if int(object_id) > 0
                )
                previous_snapshot = SelectionSnapshot.create(
                    previous_targets,
                    owner_id="hierarchy" if previous_targets else "",
                )
        self._prev_selection_snapshot = next_snapshot
        self._prev_selection_ids = next_ids
        self._prev_selected_file = next_file

        if previous_snapshot == next_snapshot:
            return
        manager = UndoManager.instance()
        if manager is None or manager.is_executing:
            return
        manager.record(GlobalSelectionCommand(
            previous_snapshot,
            next_snapshot,
            self._apply_selection_snapshot_compat,
        ))

    def _record_selection_change(self, new_ids: list):
        self._record_editor_selection_change(new_ids, "")

    def _apply_editor_selection_undo(self, ids: list, file_path: str):
        from Infernux.engine.interaction import SelectionSnapshot, SelectionTarget

        file_path = file_path or ""
        if file_path:
            snapshot = SelectionSnapshot.create(
                (SelectionTarget.asset(file_path),),
                owner_id="project",
            )
        else:
            targets = tuple(
                SelectionTarget.scene_object(object_id)
                for object_id in ids
                if int(object_id) > 0
            )
            snapshot = SelectionSnapshot.create(
                targets,
                owner_id="hierarchy" if targets else "",
            )
        self._apply_selection_snapshot(snapshot)

    def _apply_selection_snapshot_compat(self, snapshot):
        from Infernux.engine.interaction import SelectionDomain

        primary = snapshot.primary
        if primary is not None and primary.domain is SelectionDomain.ASSET:
            self._apply_editor_selection_undo([], primary.target_id)
            return
        ids = [
            target.scene_object_id()
            for target in snapshot.targets
            if target.domain is SelectionDomain.SCENE_OBJECT
        ]
        self._apply_editor_selection_undo(ids, "")

    def _apply_selection_snapshot(self, snapshot):
        from Infernux.engine.interaction import SelectionDomain, SelectionService

        service = SelectionService.instance()
        service.apply_snapshot(snapshot, reason="undo", record_history=False)
        self._prev_selection_snapshot = snapshot
        primary = snapshot.primary

        if primary is not None and primary.domain is SelectionDomain.ASSET:
            file_path = primary.target_id
            self._prev_selection_ids = []
            self._prev_selected_file = file_path
            self._set_outline(0, [])
            self.project_panel.set_selected_file(file_path, False)
            self._inspector_set_selected_file(file_path)
            self.event_bus.emit(EditorEvent.FILE_SELECTED, file_path)
            if self.window_manager is not None:
                self.window_manager.focus_window("project")
            return

        ids = [
            target.scene_object_id()
            for target in snapshot.targets
            if target.domain is SelectionDomain.SCENE_OBJECT
        ]
        self._prev_selection_ids = list(ids)
        self._prev_selected_file = ""

        self._inspector_set_selected_file("")
        self._synchronize_object_selection(record=False, reveal_primary=True)
        if self.window_manager is not None and snapshot.owner_id:
            self.window_manager.focus_window(snapshot.owner_id)

    def _apply_selection_undo(self, ids: list):
        """Restore a selection state during undo/redo."""
        self._apply_editor_selection_undo(ids, "")

