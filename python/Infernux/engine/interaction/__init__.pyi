from .descriptors import SelectionDomain, SelectionSnapshot, SelectionTarget
from .contexts import FocusService, FocusSnapshot, InputContext, InputContextStack
from .close_coordinator import CloseCoordinator, CloseIntent, CloseIntentKind, CloseIssue, CloseState
from .documents import DocumentActionResult, DocumentActionStatus, DocumentCapability, DocumentController, DocumentIdentityKind, DocumentKey, DocumentKind, DocumentRegistry, DocumentState, EditorDocument, SaveTicket, SaveTicketStatus
from .action_journal import ActionOrigin, EditorActionJournal, EditorContextSnapshot, JournalEntry, JournalPushResult
from .selection import SelectionChange, SelectionService
from .commands import CommandContext, CommandResult, CommandSource, CommandStatus, EditorCommand, EditorCommandRegistry
from .shortcuts import KeyChord, ShortcutBinding, ShortcutEvent, ShortcutModifier, ShortcutPhase, ShortcutRouteResult, ShortcutRouteStatus, ShortcutRouter, ShortcutScope
from .session import EditorInteractionCore
from .transactions import EditorTransaction

__all__: list[str]
