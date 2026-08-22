"""Prefab property overrides — compute, apply, and revert per-instance changes.

Compares a prefab instance in the scene against its source ``.prefab`` file
to determine which properties have been modified.

Example::

    from Infernux.engine.prefab_overrides import compute_overrides, apply_overrides_to_prefab

    overrides = compute_overrides(instance_obj, "/path/to/my.prefab")
    apply_overrides_to_prefab(instance_obj, "/path/to/my.prefab")
"""

from __future__ import annotations

from typing import Any, List, Optional


class Override:
    """A single property difference between a prefab instance and its source."""

    node_path: str
    key: str
    prefab_value: Any
    instance_value: Any

    def __init__(
        self,
        node_path: str,
        key: str,
        prefab_value: Any,
        instance_value: Any,
    ) -> None: ...

    def __repr__(self) -> str: ...


def resolve_prefab_instance_root(instance_obj: Any) -> Any:
    """Return the root object of the linked prefab instance, if any."""
    ...


def compute_overrides(
    instance_obj: Any,
    prefab_path: str,
    asset_database: Any = None,
) -> List[Override]:
    """Return a list of property overrides for *instance_obj* vs. its prefab.

    Args:
        instance_obj: The in-scene ``GameObject`` instance.
        prefab_path: Path to the source ``.prefab`` file.
        asset_database: Optional C++ ``AssetDatabase``.
    """
    ...

def apply_overrides_to_prefab(
    instance_obj: Any,
    prefab_path: str,
    asset_database: Any = None,
) -> bool:
    """Write the instance's current state back into the prefab file.

    Args:
        instance_obj: The modified in-scene instance.
        prefab_path: Path to the source ``.prefab`` file.
        asset_database: Optional C++ ``AssetDatabase``.

    Returns:
        ``True`` on success.
    """
    ...

def build_prefab_apply_command(
    instance_obj: Any,
    prefab_path: str,
    asset_database: Any = None,
) -> Any: ...

def revert_overrides(
    instance_obj: Any,
    prefab_path: str,
    asset_database: Any = None,
) -> bool:
    """Reset the instance to match the prefab source.

    Args:
        instance_obj: The in-scene instance to revert.
        prefab_path: Path to the source ``.prefab`` file.
        asset_database: Optional C++ ``AssetDatabase``.
    """
    ...

def build_prefab_revert_command(
    instance_obj: Any,
    prefab_path: str,
    asset_database: Any = None,
) -> Any: ...
