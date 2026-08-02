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
        project = self.project_panel
        scene_view = self.scene_view

        project.on_selection_changed = self._on_project_selection_changed
        scene_view.set_on_object_picked(self._on_scene_view_picked)
        scene_view.set_on_box_select(self._on_box_select_done)
        hierarchy.on_double_click_focus = (
            lambda oid: self._fly_to_object_by_id(oid)
        )

        selection = SelectionService.instance()
        previous_service = getattr(self, "_selection_projection_service", None)
        previous_listener = getattr(self, "_selection_projection_listener", None)
        if previous_service is not None and callable(previous_listener):
            previous_service.remove_listener(previous_listener)
        self._selection_projection_service = selection
        self._selection_projection_listener = self._on_global_selection_changed
        selection.add_listener(self._selection_projection_listener)
        self._prev_selection_snapshot = selection.snapshot
        self._present_selection_snapshot(selection.snapshot)
        self.undo_manager.set_context_hooks(
            self.interaction_core.capture_context,
            self._restore_editor_context,
        )

    def _restore_editor_context(self, context, phase: str) -> None:
        from Infernux.engine.interaction import SelectionService

        if SelectionService.instance().snapshot != context.selection:
            self._apply_selection_snapshot(context.selection)

        panel_id = context.focus.active_panel_id
        if not panel_id or self.window_manager is None:
            return
        if not self.window_manager.is_window_open(panel_id):
            registered = self.window_manager.get_registered_types()
            if panel_id in registered:
                self.window_manager.open_window(panel_id)
        if self.window_manager.is_window_open(panel_id):
            self.window_manager.focus_window(panel_id)

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

    def _on_global_selection_changed(self, change) -> None:
        self._present_selection_snapshot(change.after)
        self._record_selection_snapshot(
            change.after,
            previous_snapshot=change.before,
            record=change.record_history,
        )

    def _present_selection_snapshot(self, snapshot) -> None:
        """Project the one authoritative selection into editor views."""
        from Infernux.engine.interaction import SelectionDomain

        primary = snapshot.primary
        domain = snapshot.domain
        if domain in (SelectionDomain.ASSET, SelectionDomain.ASSET_SUBRESOURCE):
            paths = [
                (
                    target.target_id
                    if target.domain is SelectionDomain.ASSET
                    else target.document_id
                )
                for target in snapshot.targets
                if target.domain in (
                    SelectionDomain.ASSET,
                    SelectionDomain.ASSET_SUBRESOURCE,
                )
            ]
            paths = list(dict.fromkeys(path for path in paths if path))
            primary_path = ""
            if primary is not None:
                primary_path = (
                    primary.target_id
                    if primary.domain is SelectionDomain.ASSET
                    else primary.document_id
                )
            self.project_panel.set_selected_files(paths, primary_path, False)
            self._inspector_set_selected_file(primary_path)
            self._set_outline(0, [])
            self.event_bus.emit(EditorEvent.FILE_SELECTED, primary_path)
            return

        if domain in (SelectionDomain.SCENE_OBJECT, SelectionDomain.COMPONENT):
            if domain is SelectionDomain.COMPONENT:
                object_ids = list(dict.fromkeys(
                    object_id
                    for object_id, _component_id in (
                        target.component_ids() for target in snapshot.targets
                    )
                    if object_id
                ))
                primary_id = (
                    primary.component_ids()[0] if primary is not None else 0
                )
            else:
                object_ids = [
                    target.scene_object_id()
                    for target in snapshot.targets
                    if target.domain is SelectionDomain.SCENE_OBJECT
                ]
                primary_id = primary.scene_object_id() if primary is not None else 0

            obj = None
            if primary_id:
                from Infernux.lib import SceneManager

                scene = SceneManager.instance().get_active_scene()
                obj = scene.find_by_id(primary_id) if scene else None
            self.inspector_panel.set_selected_object_id(primary_id or 0)
            self.project_panel.clear_selection(False)
            self._set_outline(primary_id, object_ids)
            self.event_bus.emit(EditorEvent.SELECTION_CHANGED, obj)
            return

        self.project_panel.clear_selection(False)
        self.inspector_panel.clear_selected_object()
        self._inspector_set_selected_file("")
        self._set_outline(0, [])
        self.event_bus.emit(EditorEvent.SELECTION_CHANGED, None)

    def _fly_to_object_by_id(self, object_id: int):
        """Resolve object ID and fly scene view to it."""
        if not object_id:
            return
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(object_id) if scene else None
        if obj:
            self.scene_view.fly_to_object(obj)

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
            record_history=True,
        )

    def _on_scene_view_picked(self, object_id: int, ctrl: bool = False):
        from Infernux.engine.interaction import SelectionService, SelectionTarget

        selection = SelectionService.instance()

        if ctrl and object_id:
            selection.toggle(
                SelectionTarget.scene_object(object_id),
                owner_id="scene_view",
                record_history=True,
            )
        elif object_id:
            selection.select(
                SelectionTarget.scene_object(object_id),
                owner_id="scene_view",
                record_history=True,
            )
        elif not ctrl:
            selection.clear(record_history=True)

        primary = selection.snapshot.primary
        if primary is not None:
            self.hierarchy.expand_to_object(primary.scene_object_id())

    def _on_box_select_done(self, _primary_obj):
        from Infernux.engine.interaction import SelectionService

        primary = SelectionService.instance().snapshot.primary
        if primary is not None:
            self.hierarchy.expand_to_object(primary.scene_object_id())

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

        from Infernux.engine.interaction import SelectionService, SelectionTarget

        SelectionService.instance().select(
            SelectionTarget.scene_object(object_id),
            owner_id="hierarchy",
            reason="console_navigate_to_object",
            record_history=True,
        )
        self.hierarchy.expand_to_object(object_id)

        return True

    def _record_selection_snapshot(
        self,
        next_snapshot,
        *,
        previous_snapshot=None,
        record: bool = True,
    ):
        from Infernux.engine.ui.asset_resource_preview import release_all_preview_authoring
        from Infernux.engine.interaction import SelectionSnapshot
        from Infernux.engine.undo import GlobalSelectionCommand, UndoManager

        release_all_preview_authoring()

        if previous_snapshot is None:
            previous_snapshot = getattr(self, "_prev_selection_snapshot", None)
        if previous_snapshot is None:
            previous_snapshot = SelectionSnapshot()
        self._prev_selection_snapshot = next_snapshot

        if not record or previous_snapshot == next_snapshot:
            return
        manager = UndoManager.instance()
        if manager is None or manager.is_executing:
            return
        manager.record(GlobalSelectionCommand(
            previous_snapshot,
            next_snapshot,
            self._apply_selection_snapshot,
        ))

    def _apply_selection_snapshot(self, snapshot):
        from Infernux.engine.interaction import SelectionService

        service = SelectionService.instance()
        service.apply_snapshot(snapshot, reason="undo", record_history=False)
        self._prev_selection_snapshot = snapshot
        window_manager = getattr(self, "window_manager", None)
        if window_manager is not None and snapshot.owner_id:
            window_manager.focus_window(snapshot.owner_id)

