"""Type stubs for the authoritative Python component registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from .component import InxComponent


@dataclass(frozen=True)
class ComponentRegistration:
    type_name: str
    category: str
    menu_path: str
    script_path: str
    component_type: Optional[Type[InxComponent]]
    project_script: bool
    allow_multiple: bool = True
    required_types: tuple[object, ...] = ()
    incompatible_types: tuple[object, ...] = ()


def register_component_type(component_type: Type[InxComponent], *, script_path: str = "") -> None: ...
def ensure_engine_component_catalog_loaded() -> None: ...
def register_component_script(file_path: str) -> bool: ...
def unregister_component_script(file_path: str) -> None: ...
def get_component_registrations(*, project_root: str = "") -> tuple[ComponentRegistration, ...]: ...
def get_component_registration_revision() -> int: ...
def get_component_constraints(component_type: type) -> ComponentRegistration: ...
def get_python_attachment_blockers(game_object: object, component_type: type) -> tuple[str, ...]: ...


def get_type(name: str) -> Optional[Type[InxComponent]]:
    """Get the latest registered component class by name.

    Example::

        TestCache = get_type("testcache")
        if TestCache:
            comp = self.get_component(TestCache)
    """
    ...


def get_type_by_identity(
    name: str,
    script_guid: str,
    type_guid: str,
) -> Optional[Type[InxComponent]]: ...


def get_all_types() -> Dict[str, Type[InxComponent]]:
    """Get all known InxComponent subclass types as ``{name: class}``."""
    ...


class _TypeAccessor:
    """Dynamic attribute accessor for component types.

    Example::

        T.TestCache   # -> get_type("TestCache")
        T.Movement    # -> get_type("Movement")
    """
    def __getattr__(self, name: str) -> Optional[Type[InxComponent]]: ...
    def __repr__(self) -> str: ...


T: _TypeAccessor
