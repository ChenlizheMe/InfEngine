"""Type stubs for Infernux.components.script_loader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Type
from types import CodeType

from .component import InxComponent


class ScriptLoadError(Exception):
    """Raised when a script cannot be loaded or contains no valid components."""
    ...


def set_script_error(file_path: str, message: str) -> None:
    """Record an error message for a script (no exception object needed)."""
    ...

def get_script_errors() -> Dict[str, str]:
    """Return a snapshot of all currently broken scripts {path: traceback}."""
    ...

def has_script_errors() -> bool:
    """Return True if any loaded script has unresolved errors."""
    ...

def get_script_error_revision() -> int:
    """Return a monotonic revision changed only when diagnostics change."""
    ...

def get_script_error_by_path(file_path: str) -> Optional[str]:
    """Return the error string for *file_path*, or ``None`` if it loaded OK."""
    ...


def load_component_from_file(file_path: str) -> Type[InxComponent]:
    """Load the first InxComponent subclass from a Python file.

    Raises:
        ScriptLoadError: If file doesn't exist, can't be imported,
                         or contains no components.
    """
    ...


def load_component_class_from_file(file_path: str, type_name: str = ...) -> Optional[Type[InxComponent]]:
    """Load a specific component class from a Python file.

    Prefers an exact ``type_name`` match. If missing and the file defines
    exactly one ``InxComponent`` subclass, returns that class so one-file
    class renames keep scene references alive.
    """
    ...


def load_all_components_from_file(
    file_path: str,
    *,
    preserve_classes: Iterable[type] = ...,
    register: bool = ...,
    source_only: bool = ...,
    source: bytes | str | None = ...,
    code: CodeType | None = ...,
) -> List[Type[InxComponent]]:
    """Load all InxComponent subclasses from a Python file.

    Raises:
        ScriptLoadError: If file doesn't exist or can't be imported.
    """
    ...


class ScriptReloadRejected(ScriptLoadError): ...


@dataclass(frozen=True)
class ComponentBodyReloadRequest:
    file_path: str
    target_types: tuple[type, ...] = ...
    instances_by_type: dict[type, tuple[object, ...]] | None = ...
    script_guid: str = ...
    source: bytes | str | None = ...
    code: CodeType | None = ...
    retire_script_paths: tuple[str, ...] = ...


class ComponentBodyReloadTransaction:
    requests: tuple[ComponentBodyReloadRequest, ...]
    plans: tuple[tuple[type, tuple[tuple[str, bool, object], ...]], ...]
    registry_entries: tuple[tuple[str, tuple[type, ...]], ...]
    diagnostic_snapshot: tuple[dict[str, str], int]
    had_live_targets: bool
    member_status: tuple[tuple[str, bool, int], ...]
    committed: bool
    rolled_back: bool
    finalized: bool
    def commit(self) -> dict[type, tuple[str, ...]]: ...
    def finalize(self) -> None: ...
    def rollback(self) -> None: ...


def stage_component_body_reload_batch(
    requests: Iterable[ComponentBodyReloadRequest],
) -> ComponentBodyReloadTransaction: ...


def rollback_component_body_reload(transaction: ComponentBodyReloadTransaction) -> None: ...


def reload_component_bodies(
    file_path: str,
    target_types: Iterable[Type[InxComponent]],
    *,
    script_guid: str = ...,
    instances_by_type: dict[Type[InxComponent], tuple[object, ...]] | None = ...,
    source: bytes | str | None = ...,
    code: CodeType | None = ...,
) -> dict[Type[InxComponent], tuple[str, ...]]:
    """Reload compatible live component bodies using an optional frontend code object."""
    ...


def create_component_instance(component_class: Type[InxComponent]) -> InxComponent:
    """Create an instance of a component class.

    Raises:
        ScriptLoadError: If instantiation fails.
    """
    ...


def load_and_create_component(
    file_path: str, asset_database: Optional[object] = ...
) -> Optional[InxComponent]:
    """Load first component from file and create an instance.

    Returns ``None`` if the script has errors (already logged).

    Raises:
        ScriptLoadError: If AssetDatabase is missing or GUID cannot be resolved.
    """
    ...


def get_component_info(component_class: Type[InxComponent]) -> dict:
    """Extract metadata from a component class.

    Returns:
        Dict with keys ``name``, ``module``, ``docstring``, ``fields``.
    """
    ...
