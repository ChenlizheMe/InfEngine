"""Undo/Redo system for Infernux editor.

All public symbols are re-exported here.
"""

from __future__ import annotations

# -- Base --
from Infernux.engine.undo._base import (
    UndoCommand,
    CompoundCommand,
    LambdaCommand,
    _snapshot_value,
)

# -- Helpers (private, but some are imported externally) --
from Infernux.engine.undo._helpers import (
    _bump_inspector_structure,
    _bump_inspector_values,
    _destroy_game_object_immediately,
    _get_active_scene,
    _resolve_target,
    _resolve_live_ref,
)

# -- Property commands --
from Infernux.engine.undo._property_commands import (
    SetPropertyCommand,
    BuiltinPropertyCommand,
    GenericComponentCommand,
    PythonComponentDocumentCommand,
    MaterialDocumentCommand,
    ResourceDocumentCommand,
    SetMaterialSlotCommand,
)

# -- Structural commands --
from Infernux.engine.undo._structural_commands import (
    CreateGameObjectCommand,
    DeleteGameObjectCommand,
    DeleteGameObjectsCommand,
    ReparentCommand,
    MoveGameObjectCommand,
    GlobalSelectionCommand,
    PrefabModeCommand,
    PrefabUnpackCommand,
    PrefabRevertCommand,
)

from Infernux.engine.undo._asset_commands import (
    ProjectAssetCopyCommand,
    ProjectAssetDeleteCommand,
    ProjectAssetMoveCommand,
    ProjectAssetPasteCommand,
    ProjectAssetRenameCommand,
)

from Infernux.engine.undo._timeline_commands import (
    TimelineInsertKeyframeCommand,
    TimelinePropertyCommand,
    TimelineRemoveKeyframeCommand,
)

# -- Component commands --
from Infernux.engine.undo._component_commands import (
    AddComponentTransactionCommand,
    AddNativeComponentCommand,
    RemoveNativeComponentCommand,
    AddPyComponentCommand,
    RemovePyComponentCommand,
    RemoveComponentsCommand,
    ReorderComponentsCommand,
)

# -- Manager --
from Infernux.engine.undo._manager import UndoManager

# -- Trackers --
from Infernux.engine.undo._trackers import (
    InspectorSnapshotCommand,
    InspectorUndoTracker,
    HierarchyUndoTracker,
)

# -- Snapshots --
from Infernux.engine.undo._snapshots import (
    _SNAPSHOT_REGISTRY,
    _resolve_and_snap,
    _resolve_and_restore,
    snapshot_live_game_object,
    restore_live_game_object,
    snapshot_live_transform,
    restore_live_transform,
    snapshot_live_native_component,
    restore_live_native_component,
    snapshot_live_py_component,
    restore_live_py_component,
    snapshot_live_renderstack_component,
    restore_live_renderstack_component,
    _get_live_game_object,
    _get_live_transform,
    _get_nth_live_native_component,
    _get_nth_live_py_component,
)

# -- RenderStack --
from Infernux.engine.undo._renderstack import (
    RenderStackFieldCommand,
    RenderStackSetPipelineCommand,
)

# -- Recreate --
from Infernux.engine.undo._recreate import (
    _recreate_game_object_from_document,
)

# -- AnimFSM --
from Infernux.engine.undo._animfsm_commands import (
    NodeGraphSnapshotCommand,
    record_node_graph_snapshot,
    AnimFSMSnapshotCommand,
    record_animfsm_snapshot,
)

__all__ = [
    "UndoCommand", "CompoundCommand", "LambdaCommand",
    "SetPropertyCommand", "BuiltinPropertyCommand",
    "GenericComponentCommand", "PythonComponentDocumentCommand", "MaterialDocumentCommand", "ResourceDocumentCommand", "SetMaterialSlotCommand",
    "CreateGameObjectCommand", "DeleteGameObjectCommand", "DeleteGameObjectsCommand",
    "ReparentCommand", "MoveGameObjectCommand",
    "GlobalSelectionCommand", "PrefabModeCommand", "PrefabUnpackCommand",
    "PrefabRevertCommand",
    "ProjectAssetRenameCommand",
    "ProjectAssetDeleteCommand",
    "ProjectAssetCopyCommand",
    "ProjectAssetMoveCommand",
    "ProjectAssetPasteCommand",
    "TimelineInsertKeyframeCommand",
    "TimelinePropertyCommand",
    "TimelineRemoveKeyframeCommand",
    "AddComponentTransactionCommand",
    "AddNativeComponentCommand", "RemoveNativeComponentCommand",
    "AddPyComponentCommand", "RemovePyComponentCommand",
    "RemoveComponentsCommand",
    "ReorderComponentsCommand",
    "UndoManager",
    "InspectorSnapshotCommand", "InspectorUndoTracker", "HierarchyUndoTracker",
    "RenderStackFieldCommand", "RenderStackSetPipelineCommand",
    "NodeGraphSnapshotCommand", "record_node_graph_snapshot",
    "AnimFSMSnapshotCommand", "record_animfsm_snapshot",
]
