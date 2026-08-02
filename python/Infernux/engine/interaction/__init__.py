"""Shared editor interaction state and services."""

from .descriptors import SelectionDomain, SelectionSnapshot, SelectionTarget
from .contexts import FocusService, FocusSnapshot, InputContext, InputContextStack
from .selection import SelectionChange, SelectionService
from .session import EditorInteractionCore

__all__ = [
    "EditorInteractionCore",
    "FocusService",
    "FocusSnapshot",
    "InputContext",
    "InputContextStack",
    "SelectionChange",
    "SelectionDomain",
    "SelectionService",
    "SelectionSnapshot",
    "SelectionTarget",
]
