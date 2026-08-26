"""Transport-neutral automation host contracts."""

from .commands import CommandFuture, MainThreadCommandQueue
from .editor import EditorAutomationHost
from .operations import (
    Operation,
    OperationError,
    OperationJobRegistry,
    OperationKind,
    OperationRegistry,
    OperationSchema,
)

__all__ = [
    "CommandFuture",
    "EditorAutomationHost",
    "MainThreadCommandQueue",
    "Operation",
    "OperationError",
    "OperationJobRegistry",
    "OperationKind",
    "OperationRegistry",
    "OperationSchema",
]
