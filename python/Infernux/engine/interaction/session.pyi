from contextlib import AbstractContextManager
from typing import Callable, Optional
from .contexts import FocusService
from .action_journal import EditorActionJournal, EditorContextSnapshot
from .history import HistoryModel
from .asset_mutations import AssetMutationService
from .asset_content import AssetRenameContentRegistry
from .documents import DocumentRegistry
from .document_open import DocumentOpenService
from .close_coordinator import CloseCoordinator
from .commands import EditorCommandRegistry
from .selection import SelectionService
from .clipboard import ClipboardService
from .shortcuts import ShortcutRouter
from .windows import WindowLocator
from .continuous_edits import ContinuousEditService
from .authoring_mutations import AuthoringMutationService
from .project_assets import ProjectAssetCommandService, ProjectAssetInteractionService
from .prefabs import PrefabCommandService
from .render_stacks import RenderStackCommandService
from .components import ComponentCommandService
from .saving import EditorSaveService
from .navigation import DirectoryNavigationHistory, NavigationService
from .transient_interactions import TransientInteractionService
from .panels import PanelInteractionRegistry
from .external_drops import ExternalDropTargetService
from .scene_objects import SceneObjectCommandService
from .modals import ModalService
from .view_commands import ViewCommandService
from .tree_views import TreeViewStateService
from .preferences import PreferencesCommandService
from .graph_commands import GraphCommandService
from .command_palette import CommandPaletteService

class EditorInteractionCore:
    selection: SelectionService
    navigation: NavigationService
    directory_navigation: DirectoryNavigationHistory
    clipboard: ClipboardService
    scene_objects: SceneObjectCommandService
    focus: FocusService
    panels: PanelInteractionRegistry
    transient_interactions: TransientInteractionService
    continuous_edits: ContinuousEditService
    documents: DocumentRegistry
    authoring_mutations: AuthoringMutationService
    modals: ModalService
    external_drops: ExternalDropTargetService
    document_open: DocumentOpenService
    close_coordinator: CloseCoordinator
    action_journal: EditorActionJournal
    history: HistoryModel
    asset_mutations: AssetMutationService
    project_assets: ProjectAssetCommandService
    project_asset_interactions: ProjectAssetInteractionService
    prefabs: PrefabCommandService
    render_stacks: RenderStackCommandService
    components: ComponentCommandService
    view_commands: ViewCommandService
    tree_views: TreeViewStateService
    preferences: PreferencesCommandService
    graph_commands: GraphCommandService
    saving: EditorSaveService
    asset_content: AssetRenameContentRegistry
    commands: EditorCommandRegistry
    shortcuts: ShortcutRouter
    command_palette: CommandPaletteService
    @property
    def can_cancel_active_interaction(self) -> bool: ...
    def cancel_active_interaction(self) -> bool: ...
    def user_action(self, description: str) -> AbstractContextManager[None]: ...
    def __init__(self) -> None: ...
    @classmethod
    def instance(cls) -> Optional[EditorInteractionCore]: ...
    def shutdown(self) -> None: ...
    def capture_context(self, *, focus: object = ..., selection: object = ...) -> EditorContextSnapshot: ...
    def set_window_locator_provider(self, provider: Optional[Callable[[object], Optional[WindowLocator]]]) -> None: ...
