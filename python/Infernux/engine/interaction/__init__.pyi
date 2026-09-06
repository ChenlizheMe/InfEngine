from .descriptors import SelectionDomain, SelectionSnapshot, SelectionTarget
from .contexts import FocusChange, FocusService, FocusSnapshot, InputContext, InputContextStack
from .close_coordinator import CloseCoordinator, CloseIntent, CloseIntentKind, CloseIssue, CloseState
from .documents import DocumentActionResult, DocumentActionStatus, DocumentCapability, DocumentController, ExplicitResourceSaveController, DocumentIdentityKind, DocumentKey, DocumentKind, DocumentLocator, DocumentRegistry, DocumentState, EditorDocument, SaveTicket, SaveTicketStatus, document_content_token
from .document_open import DocumentOpenAdapter, DocumentOpenResult, DocumentOpenService, DocumentOpenStatus
from .external_conflicts import ExternalDocumentConflict, ExternalDocumentConflictService
from .action_journal import ActionOrigin, ContextRestoreStatus, EditorActionJournal, EditorContextSnapshot, JournalEntry, JournalPushResult, action_origin_scope, current_action_origin
from .windows import PanelViewStateField, PanelViewStateSchema, WindowLocator
from .selection import SelectionChange, SelectionService
from .clipboard import ClipboardChange, ClipboardDomain, ClipboardItem, ClipboardOperation, ClipboardPayload, ClipboardService
from .commands import CommandContext, CommandResult, CommandSource, CommandStatus, EditorCommand, EditorCommandRegistry
from .shortcuts import KeyChord, ShortcutBinding, ShortcutEvent, ShortcutModifier, ShortcutPhase, ShortcutRouteResult, ShortcutRouteStatus, ShortcutRouter, ShortcutScope
from .command_palette import COMMAND_PALETTE_CONTEXT_ID, COMMAND_PALETTE_MODAL_ID, CommandPaletteEntry, CommandPaletteService
from .history import HistoryEntrySnapshot, HistoryModel, HistorySnapshot
from .shortcut_profiles import DEFAULT_PROFILE_ID, DEFAULT_PROFILE_NAME, SHORTCUT_PROFILES_SCHEMA, ShortcutBindingSnapshot, ShortcutOverrideSnapshot, ShortcutProfileDiff, ShortcutProfileDiffKind, ShortcutProfileModel, ShortcutProfileSnapshot, ShortcutProfilesSnapshot
from .session import EditorInteractionCore
from .continuous_edits import ContinuousEditService, ContinuousEditSession
from .authoring_mutations import AuthoringMutationService
from .project_assets import ProjectAssetCommandService, ProjectAssetInteractionService
from .prefabs import PrefabCommandService
from .render_stacks import RenderStackCommandService, submit_renderstack_command
from .components import ComponentCommandService, ComponentDocumentEditResult
from .saving import EditorSaveService, FocusedSaveResult
from .view_commands import ViewCommandService
from .tree_views import TreeViewStateService
from .preferences import PreferencesCommandService, SUPPORTED_IDES, SUPPORTED_LOCALES, submit_preferences_command
from .graph_commands import GraphCommandService
from .authoring_documents import AuthoringAssetSnapshot, AuthoringDocumentController
from .transient_interactions import TransientInteraction, TransientInteractionService
from .navigation import DirectoryNavigationHistory, DirectoryNavigationSnapshot, NavigationAdapter, NavigationRequest, NavigationService
from .resource_documents import EditableResourceDocumentController, ensure_editable_resource_document
from .project_settings import BUILD_SETTINGS_DEFAULTS, ProjectSettingsDocumentController, ensure_project_settings_document, normalize_build_settings
from .transactions import EditorTransaction
from .panels import BoundPanelCommand, ExternalDropKind, PanelCommandAdapter, PanelCommandSpec, PanelInteractionDescriptor, PanelInteractionRegistry, PanelShortcutSpec
from .external_drops import ExternalDropDecision, ExternalDropStatus, ExternalDropTargetService
from .scene_objects import SceneObjectCommandService
from .asset_mutations import AssetContentChange, AssetMutation, AssetMutationChange, AssetMutationKind, AssetMutationNotification, AssetMutationService, AssetRelocationChange, AssetRelocationPlan, iter_asset_mutations
from .asset_content import (
    AssetReferenceContentPatch,
    AssetReferenceRelocationPlanner,
    AssetRenameContentRegistry,
    AssetRenameTransform,
)
from .graph_authoring import GraphActionDiff, GraphDomainAdapter, GraphElementKind, GraphElementRef, GraphMutation, GraphMutationKind, GraphSelectionController
from .modals import ActiveModal, ModalRegistration, ModalService
from .search import SearchQueryModel, SearchToken, normalize_search_text
from .collection_models import CollectionInsertion, CollectionInteractionModel, CollectionRenameSession, CollectionSelection, CollectionViewport, TreeProjectionModel, TreeProjectionRow
from .context_menus import ContextMenuBuilder, ContextMenuCommand, ContextMenuEntry, ContextMenuRenderResult, ContextMenuSubmenu, ResolvedContextMenuCommand
from .object_fields import ASSET_REFERENCE_CLEAR_COMMAND, ASSET_REFERENCE_COPY_COMMAND, ASSET_REFERENCE_OPEN_COMMAND, ASSET_REFERENCE_PASTE_COMMAND, ASSET_REFERENCE_REVEAL_COMMAND, AssetReferenceCatalog, AssetReferenceCommandTarget, AssetReferenceFieldModel, ObjectFieldGesture, ObjectPickerModel, ObjectReferenceFieldModel, asset_reference_catalog, asset_reference_command_payload, object_picker_model, register_asset_reference_commands
from .serialized_properties import FieldSchema, PropertyDrawerRegistry, PropertyTransaction, PropertyTransactionStatus, SerializedObjectView, SerializedPropertyBinding, SerializedPropertyHandle, SnapshotPropertyTransaction, make_attribute_property_transaction, make_native_document_property_transaction, make_python_component_property_transaction, property_drawer_registry

__all__: list[str]
