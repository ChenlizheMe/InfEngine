"""Lifetime owner for editor interaction services."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Optional

from .contexts import FocusService
from .action_journal import EditorActionJournal, EditorContextSnapshot
from .documents import DocumentRegistry
from .document_open import DocumentOpenService
from .close_coordinator import CloseCoordinator
from .commands import EditorCommandRegistry
from .selection import SelectionService
from .clipboard import ClipboardService
from .shortcuts import ShortcutRouter
from .asset_mutations import AssetMutationService
from .asset_content import AssetRenameContentRegistry
from .windows import WindowLocator
from .continuous_edits import ContinuousEditService
from .navigation import NavigationService
from .transient_interactions import TransientInteractionService
from .panels import PanelInteractionRegistry
from .external_drops import ExternalDropTargetService
from .scene_objects import SceneObjectCommandService
from .modals import ModalService
from .authoring_mutations import AuthoringMutationService
from .project_assets import (
    ProjectAssetCommandService,
    ProjectAssetInteractionService,
)
from .prefabs import PrefabCommandService
from .render_stacks import RenderStackCommandService
from .components import ComponentCommandService
from .saving import EditorSaveService
from .view_commands import ViewCommandService
from .tree_views import TreeViewStateService
from .preferences import PreferencesCommandService
from .graph_commands import GraphCommandService
from .command_palette import CommandPaletteService
from .history import HistoryModel
from .external_conflicts import ExternalDocumentConflictService


class EditorInteractionCore:
    """Project-session owner for shared editor interaction state."""

    _instance: Optional["EditorInteractionCore"] = None

    def __init__(self) -> None:
        self.selection = SelectionService()
        self.navigation = NavigationService(self.selection)
        self.clipboard = ClipboardService()
        self.scene_objects = SceneObjectCommandService(
            self.selection,
            self.clipboard,
        )
        self.focus = FocusService()
        self.panels = PanelInteractionRegistry()
        from .descriptors import SelectionDomain

        self.panels.register_selection_authority(
            "automation",
            (SelectionDomain.SCENE_OBJECT,),
        )
        self.selection.set_owner_domain_validator(self.panels.allows_selection)
        self.transient_interactions = TransientInteractionService(self.focus)
        self.continuous_edits = ContinuousEditService()
        self.documents = DocumentRegistry()
        self.external_conflicts = ExternalDocumentConflictService(self.documents)
        self.authoring_mutations = AuthoringMutationService(self.documents)
        self.modals = ModalService()
        self.external_drops = ExternalDropTargetService(
            self.focus,
            self.modals,
            self.panels,
        )
        self.document_open = DocumentOpenService(self.documents)
        self.asset_content = AssetRenameContentRegistry()
        self.asset_mutations = AssetMutationService(self.documents, self.selection)
        self.project_assets = ProjectAssetCommandService(self.selection)
        self.project_asset_interactions = ProjectAssetInteractionService(
            self.project_assets,
            self.clipboard,
        )
        self.prefabs = PrefabCommandService(
            self.selection,
            self.navigation,
            self.document_open,
            self.project_assets,
            lambda: self.capture_context(),
        )
        self.panels.register_selection_authority(
            "prefab",
            (SelectionDomain.SCENE_OBJECT, SelectionDomain.ASSET),
        )
        self.render_stacks = RenderStackCommandService()
        self.components = ComponentCommandService()
        self.view_commands = ViewCommandService()
        self.tree_views = TreeViewStateService(self.view_commands)
        self.preferences = PreferencesCommandService()
        self.saving = EditorSaveService(self.documents)
        self.close_coordinator = CloseCoordinator(self.documents)
        self.action_journal = EditorActionJournal()
        self.history = HistoryModel(self.action_journal)
        self.commands = EditorCommandRegistry(
            focus=self.focus,
            selection=self.selection,
        )
        self.graph_commands = GraphCommandService(self.panels)
        self.graph_commands.register_commands(self.commands)
        self.render_stacks.register_commands(self.commands)
        self.preferences.register_commands(self.commands)
        self.shortcuts = ShortcutRouter(self.commands, self.focus, self.modals)
        self.command_palette = CommandPaletteService(
            self.commands,
            self.shortcuts,
            self.focus,
            self.modals,
        )
        self.command_palette.register_commands()
        self.selection.add_listener(self._on_selection_changed)
        self.focus.add_change_listener(self._on_focus_changed)
        self._window_locator_provider: Optional[
            Callable[[object], Optional[WindowLocator]]
        ] = None
        EditorInteractionCore._instance = self

    @classmethod
    def instance(cls) -> Optional["EditorInteractionCore"]:
        return cls._instance

    def shutdown(self) -> None:
        self.continuous_edits.clear(commit=True)
        self.transient_interactions.clear()
        self.command_palette.shutdown()
        self.modals.clear(cancel=True)
        self.selection.remove_listener(self._on_selection_changed)
        self.selection.set_owner_domain_validator(None)
        self.focus.remove_change_listener(self._on_focus_changed)
        self.close_coordinator.cancel()
        self.selection.clear(reason="session_shutdown", record_history=False)
        self.clipboard.clear(reason="session_shutdown")
        self.documents.clear()
        self.external_conflicts.clear()
        self.authoring_mutations.shutdown()
        self.document_open.clear()
        self.navigation.clear()
        self.asset_mutations.shutdown()
        self.prefabs.shutdown()
        self.project_asset_interactions.shutdown()
        self.project_assets.shutdown()
        self.render_stacks.shutdown()
        self.components.shutdown()
        self.tree_views.shutdown()
        self.view_commands.shutdown()
        self.preferences.shutdown()
        self.graph_commands.shutdown()
        self.saving.shutdown()
        self.asset_content.shutdown()
        self.action_journal.clear()
        self.shortcuts.clear()
        self.commands.clear()
        self.panels.clear()
        self._window_locator_provider = None
        active_panel_id = self.focus.snapshot.active_panel_id
        if active_panel_id:
            self.focus.deactivate_panel(
                active_panel_id,
                reason="session_shutdown",
                record_history=False,
            )
        if EditorInteractionCore._instance is self:
            EditorInteractionCore._instance = None

    @property
    def can_cancel_active_interaction(self) -> bool:
        return bool(
            self.modals.active_modal_id
            or self.transient_interactions.can_cancel
        )

    def cancel_active_interaction(self) -> bool:
        """Cancel the top-most modal or transient through one Escape command."""
        if self.modals.active_modal_id:
            return self.modals.cancel_active()
        return self.transient_interactions.cancel_active()

    @contextmanager
    def user_action(self, description: str):
        """Aggregate one UI intent without exposing UndoManager to panels."""
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            raise RuntimeError("global editor history is unavailable")
        if manager.is_user_action_active:
            yield
            return
        with manager.user_action(str(description or "Editor Action")):
            yield

    def _on_selection_changed(self, _change) -> None:
        self.continuous_edits.commit_all()

    def _on_focus_changed(self, change) -> None:
        before_owner = change.before.active_view_id or change.before.active_panel_id
        after_owner = change.after.active_view_id or change.after.active_panel_id
        if before_owner and before_owner != after_owner:
            self.transient_interactions.cancel_owner(before_owner)
            self.continuous_edits.commit_owner(before_owner)
        else:
            self.transient_interactions.refresh_context()

    def capture_context(
        self,
        *,
        focus=None,
        selection=None,
    ) -> EditorContextSnapshot:
        focus_snapshot = self.transient_interactions.persistent_focus_snapshot(
            focus or self.focus.snapshot
        )
        selection_snapshot = selection or self.selection.snapshot
        locator = self.documents.locate(focus_snapshot.active_document_id)
        scene_locator = None
        try:
            from Infernux.engine.scene_manager import SceneFileManager

            scene_files = SceneFileManager.instance()
            if scene_files is not None:
                scene_locator = self.documents.locate(scene_files.document_id)
        except (AttributeError, ImportError, RuntimeError):
            scene_locator = None
        window = None
        if self._window_locator_provider is not None:
            window = self._window_locator_provider(focus_snapshot)
        return EditorContextSnapshot(
            focus_snapshot,
            selection_snapshot,
            locator,
            window,
            scene_locator,
        )

    def set_window_locator_provider(
        self,
        provider: Optional[Callable[[object], Optional[WindowLocator]]],
    ) -> None:
        self._window_locator_provider = provider
