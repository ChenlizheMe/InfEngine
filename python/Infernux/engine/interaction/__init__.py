"""Shared editor interaction state and services."""

from .descriptors import SelectionDomain, SelectionSnapshot, SelectionTarget
from .contexts import FocusService, FocusSnapshot, InputContext, InputContextStack
from .documents import (
    DocumentActionResult,
    DocumentActionStatus,
    DocumentCapability,
    DocumentController,
    DocumentKind,
    DocumentRegistry,
    DocumentState,
    EditorDocument,
)
from .action_journal import (
    ActionOrigin,
    EditorActionJournal,
    EditorContextSnapshot,
    JournalEntry,
    JournalPushResult,
)
from .selection import SelectionChange, SelectionService
from .session import EditorInteractionCore
from .transactions import EditorTransaction

__all__ = [
    "EditorInteractionCore",
    "EditorTransaction",
    "DocumentActionResult",
    "DocumentActionStatus",
    "DocumentCapability",
    "DocumentController",
    "DocumentKind",
    "DocumentRegistry",
    "DocumentState",
    "EditorDocument",
    "ActionOrigin",
    "EditorActionJournal",
    "EditorContextSnapshot",
    "FocusService",
    "FocusSnapshot",
    "InputContext",
    "InputContextStack",
    "JournalEntry",
    "JournalPushResult",
    "SelectionChange",
    "SelectionDomain",
    "SelectionService",
    "SelectionSnapshot",
    "SelectionTarget",
]
