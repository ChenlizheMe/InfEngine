"""BootstrapSelectionMixin — extracted from EditorBootstrap."""
from __future__ import annotations

"""
EditorBootstrap — structured editor initialization.

Breaks the monolithic ``release_engine()`` startup path into explicit
startup steps. Each step is a separate method, closures become instance
methods, and panel/manager references live on the bootstrap instance.
"""


_PROJECT_SUBRESOURCE_TOKENS = {
    "::submat:": "submaterial",
    "::subbone:": "subbone",
    "::subanim:": "subanimation",
}


def _project_selection_target(path: str):
    """Translate one Project row identity into a typed global target."""
    from Infernux.engine.interaction import SelectionTarget

    value = str(path or "")
    for token, sub_kind in _PROJECT_SUBRESOURCE_TOKENS.items():
        base, separator, identifier = value.partition(token)
        if separator and base and identifier:
            return SelectionTarget.asset_subresource(
                base,
                identifier,
                sub_kind=sub_kind,
            )
    return SelectionTarget.asset(value)


def _project_path_for_target(target) -> str:
    """Rebuild the Project row path from a typed selection target."""
    from Infernux.engine.interaction import SelectionDomain

    if target.domain is SelectionDomain.ASSET:
        return target.target_id
    if target.domain is not SelectionDomain.ASSET_SUBRESOURCE:
        return ""
    token = {
        "submaterial": "::submat:",
        "subbone": "::subbone:",
        "subanimation": "::subanim:",
    }.get(target.sub_kind)
    if not token:
        return target.document_id
    return f"{target.document_id}{token}{target.target_id}"


class BootstrapSelectionMixin:
    """BootstrapSelectionMixin method group for EditorBootstrap."""

    def _wire_selection_system(self):
        from Infernux.engine.interaction import (
            DocumentKind,
            SelectionService,
        )

        hierarchy = self.hierarchy
        project = self.project_panel
        scene_view = self.scene_view

        project.on_selection_changed = self._on_project_selection_changed
        scene_view.set_on_object_picked(self._on_scene_view_picked)
        selection = SelectionService.instance()
        previous_service = getattr(self, "_selection_projection_service", None)
        previous_listener = getattr(self, "_selection_projection_listener", None)
        if previous_service is not None and callable(previous_listener):
            previous_service.remove_listener(previous_listener)
        self._selection_projection_service = selection
        self._selection_projection_listener = self._on_global_selection_changed
        selection.add_listener(self._selection_projection_listener)
        previous_asset_bus = getattr(self, "_selection_asset_mutations", None)
        previous_asset_listener = getattr(
            self,
            "_selection_asset_event_listener",
            None,
        )
        if previous_asset_bus is not None and callable(previous_asset_listener):
            previous_asset_bus.remove_listener(previous_asset_listener)
        asset_bus = self.interaction_core.asset_mutations
        self._selection_asset_mutations = asset_bus
        self._selection_asset_event_listener = (
            self._on_asset_selection_source_changed
        )
        asset_bus.add_listener(self._selection_asset_event_listener)
        self._prev_selection_snapshot = selection.snapshot
        self._present_selection_snapshot(selection.snapshot)
        focus = self.interaction_core.focus
        previous_focus_service = getattr(self, "_focus_history_service", None)
        previous_focus_listener = getattr(self, "_focus_history_listener", None)
        if previous_focus_service is not None and callable(previous_focus_listener):
            previous_focus_service.remove_change_listener(previous_focus_listener)
        self._focus_history_service = focus
        self._focus_history_listener = self._on_global_focus_changed
        focus.add_change_listener(self._focus_history_listener)

        previous_projection_service = getattr(
            self, "_window_focus_projection_service", None
        )
        previous_projection_listener = getattr(
            self, "_window_focus_projection_listener", None
        )
        if previous_projection_service is not None and callable(
            previous_projection_listener
        ):
            previous_projection_service.remove_listener(
                previous_projection_listener
            )

        def project_window_focus(snapshot):
            if self.window_manager is not None:
                self.window_manager.project_interaction_focus(snapshot)

        self._window_focus_projection_service = focus
        self._window_focus_projection_listener = project_window_focus
        focus.add_listener(project_window_focus)
        project_window_focus(focus.snapshot)
        self.interaction_core.set_window_locator_provider(
            lambda snapshot: self.window_manager.locate_window(
                snapshot.active_view_id or snapshot.active_panel_id
            )
            if self.window_manager is not None
            else None
        )
        self.undo_manager.set_context_hooks(
            self.interaction_core.capture_context,
            self._restore_editor_context,
        )
        opener = self.interaction_core.document_open
        opener.register(
            DocumentKind.SCENE,
            lambda locator: self.scene_file_manager.restore_document_locator(locator),
            replace=True,
        )
        opener.register(
            DocumentKind.PREFAB,
            self._restore_prefab_document,
            replace=True,
        )
        opener.register(
            DocumentKind.PARTICLE_GRAPH,
            lambda locator: self._restore_panel_document(
                locator,
                panel_id="particle_graph_editor",
            ),
            replace=True,
        )
        opener.register(
            DocumentKind.ANIMATION_FSM,
            lambda locator: self._restore_panel_document(
                locator,
                panel_id="animfsm_editor",
            ),
            replace=True,
        )
        opener.register(
            DocumentKind.TIMELINE,
            lambda locator: self._restore_panel_document(
                locator,
                panel_id="animtimeline_editor",
            ),
            replace=True,
        )
        opener.register(
            DocumentKind.ANIMATION_CLIP,
            lambda locator: self._restore_panel_document(
                locator,
                panel_id="animclip2d_editor",
            ),
            replace=True,
        )
        from Infernux.engine.interaction import SelectionDomain

        navigation = self.interaction_core.navigation
        navigation.register(
            SelectionDomain.ASSET,
            self._present_asset_navigation_target,
            replace=True,
        )
        navigation.register(
            SelectionDomain.ASSET_SUBRESOURCE,
            self._present_asset_navigation_target,
            replace=True,
        )
        navigation.register(
            SelectionDomain.SCENE_OBJECT,
            self._present_scene_navigation_target,
            replace=True,
        )

    def _on_asset_selection_source_changed(self, change) -> None:
        """Invalidate stable subresource targets independently of Panel visibility."""
        import os

        from Infernux.engine.interaction import (
            AssetMutationKind,
            SelectionDomain,
            SelectionService,
            SelectionTarget,
            iter_asset_mutations,
        )
        from Infernux.engine.path_utils import same_path

        selection = SelectionService.instance()
        snapshot = selection.snapshot
        if snapshot.domain is not SelectionDomain.ASSET_SUBRESOURCE:
            return
        for mutation in iter_asset_mutations(change):
            changed_path = mutation.path
            asset_path = (
                changed_path[:-5]
                if changed_path.lower().endswith(".meta")
                else changed_path
            )
            scoped = tuple(
                target
                for target in selection.snapshot.targets
                if target.sub_kind == "sprite_frame"
                and same_path(target.document_id, asset_path)
            )
            if not scoped:
                continue

            valid_ids: set[str] = set()
            asset_exists = (
                os.path.isfile(asset_path)
                and mutation.kind is not AssetMutationKind.DELETED
            )
            if asset_exists:
                try:
                    from Infernux.core.asset_types import (
                        TextureType,
                        read_texture_import_settings,
                    )

                    settings = read_texture_import_settings(asset_path)
                    if settings.texture_type is TextureType.SPRITE:
                        valid_ids = {
                            frame.stable_id for frame in settings.sprite_frames
                        }
                except (OSError, TypeError, ValueError):
                    valid_ids = set()

            selection.reconcile(
                lambda target: not (
                    target.sub_kind == "sprite_frame"
                    and same_path(target.document_id, asset_path)
                    and target.target_id not in valid_ids
                ),
                fallback=(
                    SelectionTarget.asset(asset_path) if asset_exists else None
                ),
                fallback_owner_id="inspector",
                reason="asset_subresource_invalidated",
                record_history=False,
            )
    def _focus_navigation_panel(
        self,
        panel_id: str,
        *,
        record_history: bool = True,
    ) -> bool:
        if self.window_manager is None:
            return False
        try:
            if record_history:
                return self.window_manager.open_window_from_user(
                    panel_id,
                    reason="navigate_panel",
                ) is not None
            return self.window_manager.open_window(panel_id) is not None
        except (KeyError, RuntimeError, ValueError):
            return False

    def _present_asset_navigation_target(
        self,
        target,
        request,
    ) -> bool:
        import os

        from Infernux.engine.interaction import SelectionDomain, ViewCommandService
        from Infernux.engine.path_utils import is_path_within, lexical_path, same_path
        from Infernux.engine.project_context import get_project_root

        if self.project_panel is None:
            return False
        raw_path = (
            target.document_id
            if target.domain is SelectionDomain.ASSET_SUBRESOURCE
            else target.target_id
        )
        project_root = get_project_root()
        if project_root and not os.path.isabs(str(raw_path or "")):
            raw_path = os.path.join(project_root, str(raw_path))
        backing_path = lexical_path(raw_path)
        if not backing_path or not os.path.exists(backing_path):
            return False
        if project_root and not is_path_within(backing_path, project_root):
            return False
        parent = lexical_path(
            backing_path if os.path.isdir(backing_path) else os.path.dirname(backing_path)
        )
        if not parent or not self.project_panel.can_navigate_to_path(parent):
            return False
        if request.activate_panel:
            if not self._focus_navigation_panel(
                "project",
                record_history=request.record_history,
            ):
                return False

        current = lexical_path(self.project_panel.get_current_path())
        if same_path(current, parent):
            return True
        if not request.record_history:
            return bool(self.project_panel.set_current_path(parent))
        directory_history = getattr(self.interaction_core, "directory_navigation", None)
        if directory_history is None:
            return ViewCommandService.require().set_value(
                current,
                parent,
                self.project_panel.set_current_path,
                description="Navigate Project",
            )
        directory_history.sync(current)
        return directory_history.navigate(
            parent,
            lambda target: ViewCommandService.require().set_value(
                current,
                target,
                self.project_panel.set_current_path,
                description="Navigate Project",
            ),
        )

    def _present_scene_navigation_target(
        self,
        target,
        request,
    ) -> bool:
        object_id = target.scene_object_id()
        if object_id <= 0 or self.hierarchy is None:
            return False
        from Infernux.lib import SceneManager

        manager = SceneManager.instance()
        resolve_runtime = getattr(manager, "find_runtime_object_by_id", None)
        if callable(resolve_runtime):
            obj = resolve_runtime(object_id)
        else:
            scene = manager.get_active_scene()
            obj = scene.find_by_id(object_id) if scene is not None else None
        if obj is None:
            return False
        if request.activate_panel:
            if not self._focus_navigation_panel(
                "hierarchy",
                record_history=request.record_history,
            ):
                return False

        ancestor_ids: list[int] = []
        parent = obj.get_parent()
        while parent is not None:
            ancestor_ids.append(int(parent.id))
            parent = parent.get_parent()

        from Infernux.engine.interaction import TreeViewStateService

        return TreeViewStateService.require().reveal_path(
            self.hierarchy.get_expanded_object_ids(),
            ancestor_ids,
            self.hierarchy.set_expanded_object_ids,
            description="Reveal Hierarchy Object",
            record_history=request.record_history,
        )

    def _restore_prefab_document(self, locator):
        """Restore the scene-backed Prefab document without recording history."""
        import os

        from Infernux.engine.interaction import (
            DocumentOpenResult,
            DocumentOpenStatus,
        )
        from Infernux.engine.path_utils import same_path
        from Infernux.engine.scene_manager import SceneFileManager

        registry = self.interaction_core.documents
        resolved = registry.resolve_locator(locator)
        scene_files = SceneFileManager.instance()
        if scene_files is None:
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message="Prefab restore requires an active SceneFileManager",
            )
        path = str(locator.resource_path or "").strip()
        if not path or not os.path.isfile(path):
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message=f"Prefab resource is unavailable: {path or locator.title}",
            )
        if scene_files.is_prefab_mode:
            if not same_path(scene_files.prefab_mode_path or "", path):
                return DocumentOpenResult(
                    DocumentOpenStatus.FAILED,
                    message="another Prefab document is currently active",
                )
            if resolved is None:
                resolved = registry.resolve_locator(locator)
            return DocumentOpenResult(
                DocumentOpenStatus.READY if resolved is not None else DocumentOpenStatus.FAILED,
                resolved,
                "" if resolved is not None else "active Prefab document is not registered",
            )
        if not scene_files.open_prefab_mode(path, preserve_undo_history=True):
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message=f"Prefab document open was rejected: {path}",
            )
        resolved = registry.resolve_locator(locator)
        if resolved is None:
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message="Prefab Mode did not register the requested document",
            )
        return DocumentOpenResult(DocumentOpenStatus.READY, resolved)

    def _restore_panel_document(self, locator, *, panel_id: str):
        """Idempotently restore one singleton authoring panel document."""
        import os

        from Infernux.engine.interaction import (
            DocumentOpenResult,
            DocumentOpenStatus,
        )

        registry = self.interaction_core.documents
        resolved = registry.resolve_locator(locator)
        if resolved is not None:
            return DocumentOpenResult(DocumentOpenStatus.READY, resolved)

        path = str(locator.resource_path or "").strip()
        if not path:
            panel = self.window_manager.open_window(panel_id)
            restore_session = getattr(panel, "restore_dormant_document", None)
            if not callable(restore_session) or not restore_session(locator):
                return DocumentOpenResult(
                    DocumentOpenStatus.FAILED,
                    message=f"document session is unavailable: {locator.title}",
                )
            resolved = registry.resolve_locator(locator)
            if resolved is None:
                return DocumentOpenResult(
                    DocumentOpenStatus.FAILED,
                    message="session restore did not register the requested document",
                )
            return DocumentOpenResult(DocumentOpenStatus.READY, resolved)
        if not os.path.isfile(path):
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message=f"document resource is unavailable: {path or locator.title}",
            )

        requests = getattr(self, "_document_restore_requests", None)
        if requests is None:
            requests = {}
            self._document_restore_requests = requests
        request_key = locator.stable_id
        request = requests.setdefault(request_key, {"state": "new", "message": ""})
        if request["state"] == "failed":
            requests.pop(request_key, None)
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message=request["message"] or "document replacement was cancelled",
            )

        panel = self.window_manager.get_window_instance(panel_id)
        existing = registry.document_for_view(panel_id)
        if existing is not None and existing.is_dirty and request["state"] == "new":
            from Infernux.engine.ui.dirty_panel_confirmation import (
                DirtyPanelConfirmationCoordinator,
            )

            def _approved() -> None:
                request["state"] = "approved"

            def _cancelled() -> None:
                request["state"] = "failed"
                request["message"] = "document replacement was cancelled"

            coordinator = DirtyPanelConfirmationCoordinator.instance()
            accepted = coordinator.request_document_replace(
                existing.document_id,
                on_complete=_approved,
                on_cancel=_cancelled,
            )
            if accepted:
                request["state"] = "waiting"
            return DocumentOpenResult(DocumentOpenStatus.PENDING)
        if request["state"] == "waiting":
            return DocumentOpenResult(DocumentOpenStatus.PENDING)

        try:
            panel = self.window_manager.open_window(panel_id)
            controller = getattr(
                panel,
                "_authoring_document_controller",
                None,
            )
            loader = getattr(controller, "open_resource_immediate", None)
            if not callable(loader):
                raise RuntimeError(
                    f"editor panel '{panel_id}' has no authoring document loader"
                )
            loaded = loader(path)
            if loaded is False:
                raise RuntimeError(f"editor panel rejected document: {path}")
        except Exception as exc:
            requests.pop(request_key, None)
            return DocumentOpenResult(DocumentOpenStatus.FAILED, message=str(exc))

        resolved = registry.resolve_locator(locator)
        if resolved is None:
            requests.pop(request_key, None)
            return DocumentOpenResult(
                DocumentOpenStatus.FAILED,
                message="document loader did not register the requested resource",
            )
        requests.pop(request_key, None)
        return DocumentOpenResult(DocumentOpenStatus.READY, resolved)

    def _restore_editor_context(self, context, phase: str):
        from dataclasses import replace

        from Infernux.engine.interaction import (
            ContextRestoreStatus,
            DocumentOpenStatus,
            SelectionService,
        )

        # Old journal entries may contain the temporary child context emitted
        # by inline rename, popup, or drag capture. It is presentation state,
        # not a restorable editor location.
        transients = getattr(
            self.interaction_core,
            "transient_interactions",
            None,
        )
        focus_snapshot = (
            transients.persistent_focus_snapshot(context.focus)
            if transients is not None
            else context.focus
        )
        if context.scene is not None:
            scene_result = self.interaction_core.document_open.resolve_or_open(
                context.scene
            )
            if scene_result.status is DocumentOpenStatus.PENDING:
                return ContextRestoreStatus.PENDING
            if (
                scene_result.status is DocumentOpenStatus.FAILED
                or scene_result.document is None
            ):
                from Infernux.debug import Debug

                Debug.log_error(
                    "Undo/Redo could not restore Scene context "
                    f"'{context.scene.title}' ({context.scene.stable_id}): "
                    f"{scene_result.message or scene_result.status.value}"
                )
                return ContextRestoreStatus.FAILED
        if context.document is not None:
            result = self.interaction_core.document_open.resolve_or_open(context.document)
            if result.status is DocumentOpenStatus.PENDING:
                return ContextRestoreStatus.PENDING
            if result.status is DocumentOpenStatus.FAILED or result.document is None:
                if result.message.startswith("document restore is not supported for "):
                    return ContextRestoreStatus.DISCARD
                from Infernux.debug import Debug

                Debug.log_error(
                    "Undo/Redo could not restore document context "
                    f"'{context.document.title}' ({context.document.stable_id}): "
                    f"{result.message or result.status.value}"
                )
                return ContextRestoreStatus.FAILED
            document = result.document
            if focus_snapshot.active_document_id != document.document_id:
                focus_snapshot = replace(
                    focus_snapshot,
                    active_document_id=document.document_id,
                )
        window_locator = getattr(context, "window", None)
        window_already_visible = False
        if window_locator is not None and self.window_manager is not None:
            window_already_visible = self.window_manager.is_window_content_visible(
                window_locator.window_id
            )
            if (
                not window_already_visible
                and self.interaction_core.focus.snapshot != focus_snapshot
            ):
                self.interaction_core.focus.apply_snapshot(
                    focus_snapshot,
                    reason="undo_focus_intent",
                    record_history=False,
                )
            status = self.window_manager.restore_window(window_locator)
            if status is not ContextRestoreStatus.READY:
                if status is ContextRestoreStatus.FAILED:
                    from Infernux.debug import Debug

                    Debug.log_error(
                        "Undo/Redo could not restore window context "
                        f"'{window_locator.window_id}'"
                    )
                return status
        if (
            not window_already_visible
            and self.interaction_core.focus.snapshot != focus_snapshot
        ):
            self._apply_focus_snapshot(
                focus_snapshot,
                window_already_restored=window_locator is not None,
            )
        panel_id = focus_snapshot.active_view_id or focus_snapshot.active_panel_id
        child_context_id = focus_snapshot.child_context_id
        if panel_id and self.window_manager is not None:
            if not self.window_manager.restore_panel_child_context(
                panel_id,
                child_context_id,
            ):
                from Infernux.debug import Debug

                Debug.log_error(
                    "Undo/Redo could not restore panel child context "
                    f"'{child_context_id}' for '{panel_id}'"
                )
                return ContextRestoreStatus.FAILED
        if SelectionService.instance().snapshot != context.selection:
            self._apply_selection_snapshot(context.selection)
        return ContextRestoreStatus.READY

    def _apply_focus_snapshot(self, snapshot, *, window_already_restored=False) -> None:
        focus = self.interaction_core.focus
        focus.apply_snapshot(snapshot, reason="undo", record_history=False)
        window_id = snapshot.active_view_id or snapshot.active_panel_id
        if not window_id or self.window_manager is None:
            return
        if window_already_restored:
            return
        if not self.window_manager.is_window_open(window_id):
            registered = self.window_manager.get_registered_types()
            panel_type_id = snapshot.active_panel_id
            if panel_type_id in registered:
                self.window_manager.open_window(
                    panel_type_id,
                    instance_id=window_id,
                )
        if self.window_manager.is_window_open(window_id):
            self.window_manager.focus_window(window_id)

    def _on_global_focus_changed(self, change) -> None:
        if not change.record_history:
            return
        from Infernux.engine.interaction import SelectionService
        from Infernux.engine.undo import GlobalFocusCommand, UndoManager

        manager = UndoManager.instance()
        if manager is None or manager.is_executing:
            return
        # The focus producer owns the presentation decision. Re-checking dock
        # visibility here is racy: Python panels publish focus while ImGui is
        # revealing the new tab, so a second probe can observe the new state
        # and silently drop a transition already classified as user-visible.
        selection = SelectionService.instance().snapshot
        description = (
            f"Focus {change.after.active_panel_id}"
            if change.after.active_panel_id
            else f"Leave {change.before.active_panel_id}"
        )
        before_context = self.interaction_core.capture_context(
            focus=change.before,
            selection=selection,
        )
        presentation_before_view_id = str(
            getattr(change, "presentation_before_view_id", "") or ""
        )
        if presentation_before_view_id and self.window_manager is not None:
            from dataclasses import replace

            locator = self.window_manager.locate_window(
                presentation_before_view_id
            )
            if locator is not None:
                before_context = replace(before_context, window=locator)
        manager.record(
            GlobalFocusCommand(
                change.before,
                change.after,
                description=description,
            ),
            before_context=before_context,
            after_context=self.interaction_core.capture_context(
                focus=change.after,
                selection=selection,
            ),
        )

    def _set_outline(self, object_id: int, object_ids=None):
        native = self.engine.get_native_engine()
        if not native:
            return
        if object_ids is None:
            from Infernux.engine.interaction import SelectionService

            ids = SelectionService.instance().scene_object_ids()
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
        console_uid = 0
        if (
            domain is SelectionDomain.DIAGNOSTIC_ENTRY
            and primary is not None
            and primary.document_id == "console"
        ):
            try:
                console_uid = int(primary.target_id)
            except (TypeError, ValueError):
                console_uid = 0
        console = getattr(self, "console", None)
        if console is not None:
            console.set_selection_snapshot(console_uid)

        inspector = self.inspector_panel
        if domain is SelectionDomain.COMPONENT:
            component_ids = [
                target.component_ids()[1]
                for target in snapshot.targets
                if target.domain is SelectionDomain.COMPONENT
                and target.component_ids()[1] > 0
            ]
            if hasattr(inspector, "set_selected_component_ids"):
                inspector.set_selected_component_ids(component_ids)
        elif hasattr(inspector, "clear_selected_components"):
            inspector.clear_selected_components()

        if domain in (SelectionDomain.ASSET, SelectionDomain.ASSET_SUBRESOURCE):
            paths = [
                _project_path_for_target(target)
                for target in snapshot.targets
                if target.domain in (
                    SelectionDomain.ASSET,
                    SelectionDomain.ASSET_SUBRESOURCE,
                )
            ]
            paths = list(dict.fromkeys(path for path in paths if path))
            primary_path = ""
            if primary is not None:
                primary_path = _project_path_for_target(primary)
            self.project_panel.set_selected_files(paths, primary_path, False)
            from Infernux.engine.ui.inspector_snapshot import (
                InspectorSnapshotService,
                InspectorTarget,
            )
            InspectorSnapshotService.instance().set_active_target(
                InspectorTarget.asset(primary_path)
            )
            self._inspector_set_selected_file(primary_path)
            self._set_outline(0, [])
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

            from Infernux.engine.ui.inspector_snapshot import (
                InspectorSnapshotService,
                InspectorTarget,
            )
            InspectorSnapshotService.instance().set_active_target(
                InspectorTarget.scene_object(primary_id or 0)
            )
            inspector.set_selected_object_id(primary_id or 0)
            self.project_panel.clear_selection(False)
            self._set_outline(primary_id, object_ids)
            return

        self.project_panel.clear_selection(False)
        from Infernux.engine.ui.inspector_snapshot import (
            InspectorSnapshotService,
            InspectorTarget,
        )
        InspectorSnapshotService.instance().set_active_target(
            InspectorTarget.none()
        )
        self.inspector_panel.clear_selected_object()
        self._inspector_set_selected_file("")
        self._set_outline(0, [])

    def _on_project_selection_changed(self, paths, primary_path):
        from Infernux.engine.interaction import (
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )

        targets = tuple(_project_selection_target(path) for path in paths if path)
        primary = _project_selection_target(primary_path) if primary_path else None
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
        with self.interaction_core.scene_objects.user_action("Select Scene Object"):
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
                selection.clear(reason="scene_pick_clear", record_history=True)

            primary = selection.snapshot.primary
            if primary is not None:
                self.interaction_core.navigation.reveal(
                    primary,
                    record_history=True,
                    activate_panel=False,
                )

    def _navigate_console_entry_to_object(self, object_id: int) -> bool:
        """Reveal a console-targeted scene object in Hierarchy and Inspector."""
        if not object_id:
            return False

        from Infernux.lib import SceneManager

        manager = SceneManager.instance()
        resolve_runtime = getattr(manager, "find_runtime_object_by_id", None)
        if callable(resolve_runtime):
            obj = resolve_runtime(object_id)
        else:
            scene = manager.get_active_scene()
            obj = scene.find_by_id(object_id) if scene else None
        if obj is None:
            return False

        from Infernux.engine.interaction import SelectionTarget

        return self.interaction_core.navigation.locate(
            SelectionTarget.scene_object(object_id),
            owner_id="hierarchy",
            reason="console_navigate_to_object",
            record_history=True,
        )

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
        if manager is None or manager.is_executing or manager.is_user_action_active:
            return
        manager.record(GlobalSelectionCommand(
            previous_snapshot,
            next_snapshot,
        ))

    def _apply_selection_snapshot(self, snapshot):
        from Infernux.engine.interaction import SelectionService

        service = SelectionService.instance()
        service.apply_snapshot(snapshot, reason="undo", record_history=False)
        self._prev_selection_snapshot = snapshot

