"""Transactional Python-component preflight and publication."""

from __future__ import annotations

from typing import Any, Optional


class PythonComponentRestoreError(RuntimeError): ...


class PreparedPythonComponent:
    game_object_id: Optional[int]
    source_object_id: int
    document_path: str
    type_name: str
    script_guid: str
    type_guid: str
    enabled: bool
    fields_document: dict[str, Any]
    instance: Any


class PreparedPythonComponentGraph:
    components: list[PreparedPythonComponent]
    def require_open(self) -> None: ...
    def consume(self) -> None: ...
    def discard(self) -> None: ...


def preflight_scene_python_components(
    document: dict[str, Any], asset_database: Any = None
) -> PreparedPythonComponentGraph: ...
def preflight_game_object_python_components(
    document: dict[str, Any],
    asset_database: Any = None,
    *,
    preserve_document_ids: bool,
    prefer_loaded_types: bool = False,
    reference_scene: Any = None,
) -> PreparedPythonComponentGraph: ...
def publish_prepared_scene_python_components(
    scene: Any,
    prepared: PreparedPythonComponentGraph,
    *,
    clear_registries: bool,
    object_id_map: Optional[dict[int, int]] = None,
) -> None: ...
def deserialize_scene_document_transactionally(
    scene: Any,
    document: dict[str, Any],
    asset_database: Any = None,
    *,
    clear_registries: bool = True,
    after_publish: Any = None,
) -> bool: ...
def deserialize_game_object_document_transactionally(
    game_object: Any,
    document: dict[str, Any],
    asset_database: Any = None,
    *,
    preserve_document_ids: bool = True,
) -> bool: ...
def commit_prepared_game_object_document(
    game_object: Any,
    document: dict[str, Any],
    prepared: PreparedPythonComponentGraph,
) -> bool: ...
def instantiate_game_object_document_transactionally(
    scene: Any,
    document: dict[str, Any],
    parent: Any = None,
    asset_database: Any = None,
) -> Any: ...
def instantiate_prepared_game_object_document(
    scene: Any,
    document: dict[str, Any],
    prepared: PreparedPythonComponentGraph,
    parent: Any = None,
) -> Any: ...
def instantiate_prepared_game_object_documents(
    scene: Any,
    entries: list[tuple[dict[str, Any], PreparedPythonComponentGraph, Any]],
) -> list[Any]: ...
def clone_game_object_transactionally(
    scene: Any,
    source: Any,
    parent: Any = None,
    asset_database: Any = None,
    *,
    instantiate_in_world_space: bool = False,
    configure_created: Any = None,
) -> Any: ...
def resolve_script_from_guid(script_guid: str, asset_database: Any = None) -> Optional[str]: ...
def create_component_instance(
    script_guid: str,
    type_guid: str,
    type_name: str,
    asset_database: Any = None,
    *,
    prefer_loaded_type: bool = False,
) -> tuple[Optional[Any], Optional[str]]: ...
