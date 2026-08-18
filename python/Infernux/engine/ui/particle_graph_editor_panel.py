"""Editor for strict ``.particlegraph`` assets and their AOT lifecycles."""

from __future__ import annotations

import copy
import json
import math
import os
import uuid
from dataclasses import replace
from typing import Optional

from Infernux.core.node_graph import (
    NodeGraph,
    NodeGraphAuthoringState,
    NodeGraphElementKind,
    NodeGraphMutation,
    NodeGraphMutationKind,
)
from Infernux.core.asset_reference_types import (
    AssetReferenceCodec,
    asset_type_registry,
    resolve_asset_reference_path,
)
from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.engine.interaction import (
    GraphActionDiff,
    GraphElementKind,
    GraphElementRef,
    GraphMutation,
    GraphMutationKind,
    PanelViewStateField,
    PanelViewStateSchema,
)
from Infernux.graph.registry import (
    COMMON_NODE_REGISTRY,
    PortDirection,
    PortKind,
)
from Infernux.graph.expression_ir import ExpressionCompiler
from Infernux.graph.parameters import (
    GraphParameterAuthoringPolicy,
    GraphParameterCollection,
)
from Infernux.graph.parameter_transactions import GraphParameterTransaction
from Infernux.graph.ramp import CURVE_WRAP_MODES, GRADIENT_MODES, MAX_RAMP_KEYS, Curve, Gradient
from Infernux.graph.types import (
    AssetReference,
    BUILTIN_MESH_NAMES,
    CoordinateSpace,
    TypeRef,
    ValueType,
    builtin_mesh_name,
    builtin_mesh_reference,
)
from Infernux.lib import InxGUIContext
from Infernux.particle.asset import (
    EmitterSettings,
    EmitterShape,
    EmitterShapeKind,
    MeshEmissionMode,
    SdfEmissionMode,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventFlow,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphSchemaError,
    ParticleParameter,
    SimulationSpace,
    default_event_graph,
)
from Infernux.particle.artifact import (
    ParticleArtifactRegistry,
    PreparedParticleGraphArtifact,
)
from Infernux.particle.data_interface import (
    SdfFilter,
    SdfVolume,
    VectorField,
    VectorFieldBoundary,
    VectorFieldFilter,
    particle_data_interface_from_dict,
)
from Infernux.particle.nodes import (
    PARTICLE_EVENT_ACTIVE_TYPE_ID,
    PARTICLE_EVENT_TRIGGER_TYPE_ID,
    particle_graph_node_definitions,
)

from .asset_save_dialog import AssetSaveAsDialog
from .graph_document_authoring import (
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from .graph_details import GraphDetailContributor
from .floating_workspace_panel import (
    render_compact_tab_bar,
    render_workspace_add_header,
)
from .inspector_utils import preserve_ui_float_precision, render_color_value_bar
from .inspector_shader_utils import get_shader_property_generation
from .node_graph_editor_panel import (
    GraphParameterDetailConfig,
    GraphWorkspaceAddAction,
    GraphWorkspaceDrag,
    GraphWorkspaceEntry,
    GraphWorkspaceSection,
    NODE_GRAPH_PANEL_INTERACTION,
    NodeGraphEditorPanel,
)
from .node_graph_view import NodeCreationEntry
from .panel_registry import editor_panel
from .theme import Theme
from ._inspector_references import (
    _asset_guid_from_path,
    _portable_asset_path_hint,
    _resolve_project_asset_path,
    render_asset_reference_field,
)

_STAGES = (
    "init",
    "update",
    "collision_enter",
    "collision_stay",
    "collision_exit",
    "rendering",
)
_EVENT_VALUE_TYPES = (
    ValueType.BOOL,
    ValueType.I32,
    ValueType.U32,
    ValueType.F32,
    ValueType.VEC2,
    ValueType.VEC3,
    ValueType.VEC4,
    ValueType.COLOR,
)
_PARAMETER_VALUE_TYPES = (
    *_EVENT_VALUE_TYPES,
    ValueType.CURVE,
    ValueType.GRADIENT,
    ValueType.TEXTURE2D,
    ValueType.MESH,
)
_PARAMETER_CREATE_TYPES = (
    ValueType.F32,
    ValueType.VEC2,
    ValueType.VEC3,
    ValueType.VEC4,
    ValueType.COLOR,
    ValueType.BOOL,
    ValueType.I32,
    ValueType.U32,
    ValueType.CURVE,
    ValueType.GRADIENT,
    ValueType.TEXTURE2D,
    ValueType.MESH,
)
_PARAMETER_TYPE_COLORS = {
    ValueType.BOOL: (0.78, 0.25, 0.31, 1.0),
    ValueType.F32: (0.34, 0.72, 0.42, 1.0),
    ValueType.I32: (0.30, 0.68, 0.52, 1.0),
    ValueType.U32: (0.30, 0.68, 0.52, 1.0),
    ValueType.VEC2: (0.30, 0.70, 0.86, 1.0),
    ValueType.VEC3: (0.90, 0.58, 0.24, 1.0),
    ValueType.VEC4: (0.72, 0.45, 0.88, 1.0),
    ValueType.COLOR: (0.88, 0.78, 0.28, 1.0),
    ValueType.CURVE: (0.42, 0.78, 0.62, 1.0),
    ValueType.GRADIENT: (0.86, 0.48, 0.68, 1.0),
    ValueType.TEXTURE2D: (0.62, 0.48, 0.86, 1.0),
    ValueType.MESH: (0.32, 0.66, 0.78, 1.0),
    ValueType.MAT3: (0.44, 0.62, 0.84, 1.0),
    ValueType.MAT4: (0.36, 0.54, 0.78, 1.0),
}
_PARAMETER_DRAG_PAYLOAD = "PARTICLE_PARAMETER"
_EVENT_DRAG_PAYLOAD = "PARTICLE_EVENT"

# Shared workspace list row chrome (Emitters / Parameters / Events tabs).
_WORKSPACE_EMITTER_ON = (0.34, 0.72, 0.42, 1.0)
_WORKSPACE_EMITTER_OFF = (0.42, 0.42, 0.45, 1.0)
_WORKSPACE_EVENT_TYPE = (0.72, 0.45, 0.88, 1.0)


def _mesh_reference_display(reference: AssetReference) -> str:
    builtin = builtin_mesh_name(reference)
    if builtin:
        return f"Built-in {builtin}"
    return os.path.basename(reference.path_hint) if reference.path_hint else t("igui.none")


def _builtin_mesh_picker_items(query: str) -> list[tuple[str, dict[str, str]]]:
    filter_text = str(query).strip().lower()
    return [
        (
            f"Built-in/{name}",
            {
                "asset_type": "Mesh",
                "builtin": name,
                "guid": "",
                "path_hint": "",
            },
        )
        for name in BUILTIN_MESH_NAMES
        if not filter_text or filter_text in name.lower() or filter_text in "built-in"
    ]


def _particle_reference_value(
    asset_type: str,
    reference: AssetReference,
) -> dict[str, str]:
    builtin = builtin_mesh_name(reference) if asset_type == "Mesh" else ""
    return {
        "asset_type": asset_type,
        "builtin": builtin,
        "guid": "" if builtin else reference.guid,
        "path_hint": "" if builtin else reference.path_hint,
    }


def _particle_asset_reference(asset_type: str, value) -> AssetReference:
    """Convert the shared editor reference into the particle AOT ABI value."""

    descriptor = asset_type_registry.require(asset_type)
    error = descriptor.incompatibility(value)
    if error:
        raise ValueError(error)
    payload = AssetReferenceCodec.normalize(descriptor.type_id, value)
    if payload["builtin"]:
        if descriptor.type_id != "Mesh":
            raise ValueError(f"{descriptor.display_name} does not support built-in values")
        return builtin_mesh_reference(payload["builtin"])
    path = resolve_asset_reference_path(descriptor.type_id, payload)
    guid = payload["guid"] or _asset_guid_from_path(path)
    if not guid:
        raise ValueError(
            f"{descriptor.display_name} must be a registered project asset"
        )
    return AssetReference(guid, _portable_asset_path_hint(path))


def _event_field_default(value_type: ValueType):
    if value_type is ValueType.CURVE:
        return Curve().to_dict()
    if value_type is ValueType.GRADIENT:
        return Gradient().to_dict()
    if value_type in {ValueType.TEXTURE2D, ValueType.MESH}:
        return AssetReference().to_dict()
    if value_type is ValueType.BOOL:
        return False
    if value_type in {ValueType.I32, ValueType.U32}:
        return 0
    if value_type is ValueType.F32:
        return 0.0
    size = {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        ValueType.MAT3: 9,
        ValueType.MAT4: 16,
    }[value_type]
    return [0.0] * size


_PARTICLE_PARAMETER_POLICY = GraphParameterAuthoringPolicy(
    ParticleParameter,
    _PARAMETER_VALUE_TYPES,
    _event_field_default,
    writable_types=frozenset(_EVENT_VALUE_TYPES),
    allowed_spaces={
        ValueType.VEC3: (CoordinateSpace.NONE, CoordinateSpace.WORLD),
    },
)


def _record_scalar_node_property_semantics(
    ctx: InxGUIContext,
    *,
    node_uid: str,
    key: str,
    label: str,
    value_type: ValueType,
    value,
) -> None:
    if not bool(getattr(ctx, "semantic_capture_enabled", True)):
        return
    semantic_id = f"particle_graph.node.{node_uid}.property.{key}"
    if value_type is ValueType.BOOL:
        ctx.record_semantic_item(
            "checkbox", label, True, semantic_id, bool_value=bool(value)
        )
    elif value_type in {ValueType.I32, ValueType.U32, ValueType.F32}:
        kind = "drag_float" if value_type is ValueType.F32 else "int_input"
        ctx.record_semantic_item(
            kind, label, True, semantic_id, numeric_value=float(value)
        )
    elif value_type is ValueType.STRING:
        kind = "combo" if key in {"sort", "uv_mode", "alignment"} else "text_input"
        ctx.record_semantic_item(
            kind, label, True, semantic_id, string_value=str(value)
        )


def _node_property_is_visible(node, key: str) -> bool:
    if key == "alignment_axis":
        return str(node.data.get("alignment", "camera_plane")) == "axis"
    return True


@editor_panel(
    "Particle Graph Editor",
    type_id="particle_graph_editor",
    title_key="panel.particle_graph_editor",
    menu_path="Rendering",
    interaction=NODE_GRAPH_PANEL_INTERACTION,
)
class ParticleGraphEditorPanel(NodeGraphEditorPanel):
    window_id = "particle_graph_editor"
    VIEW_STATE_SCHEMA = PanelViewStateSchema(
        "particle_graph_editor.view",
        (
            PanelViewStateField("emitter_index", "_emitter_index", int),
            PanelViewStateField("stage", "_stage", str),
            PanelViewStateField("workspace_tab", "_workspace_tab_index", int),
            PanelViewStateField("pan_x", "_view.pan_x", float),
            PanelViewStateField("pan_y", "_view.pan_y", float),
            PanelViewStateField("zoom", "_view.zoom", float),
        ),
    )
    _HIDDEN_INTERNAL_RESOURCE_NODE_TYPES = frozenset(
        {
            "particle.vector_field.sample",
            "particle.collision.sdf",
            "particle.sdf.sample_distance",
            "particle.sdf.sample_gradient",
        }
    )

    def __init__(self):
        super().__init__(
            title="Particle Graph Editor",
            window_id=self.window_id,
            semantic_namespace="particle_graph.canvas",
        )
        from Infernux.engine.interaction import AuthoringDocumentController

        self._authoring_document_controller = AuthoringDocumentController(self)
        self._asset = ParticleGraphAsset()
        self._file_path = ""
        self._emitter_index = 0
        self._stage = "init"
        self._focus_detail_name = ""
        self._workspace_tab_index = 0
        self._pending_save_ticket_id = ""
        self._draft_compile_error = ""
        self._save_as_dialog = AssetSaveAsDialog(
            "particle_graph.save_as",
            "particle graph",
            owner_id=self._window_id,
        )
        self._shader_definition_generation = get_shader_property_generation()
        self._model: ParticleEmitterGraphAuthoringModel | None = None
        self._install_graph_selection_controller(
            contains=self._contains_graph_element,
            element_from_view=self._graph_element_from_view,
            element_to_view=self._graph_element_to_view,
            on_changed=self._on_graph_selection_projected,
        )
        self._bind_stage()
        # Merely opening an authoring window is not an edit. The blank model
        # becomes dirty only after the first authored mutation (or an explicit
        # New command), so closing an untouched panel never prompts to save.
        self._replace_particle_document(resource_path="", dirty=False)

    @property
    def asset(self) -> ParticleGraphAsset:
        self._sync_model_to_asset()
        return self._asset

    @property
    def _selected_node_uid(self) -> str:
        selection = getattr(self, "_graph_selection", None)
        primary = selection.primary if selection is not None else None
        if primary is None or primary.kind is not GraphElementKind.NODE:
            return ""
        return self._graph_element_to_view(primary)

    @_selected_node_uid.setter
    def _selected_node_uid(self, node_uid: str) -> None:
        # Kept as a projection setter for existing editor integrations. The
        # controller remains the only stored selection state.
        selection = getattr(self, "_graph_selection", None)
        if selection is None:
            return
        self._select_canvas_node(str(node_uid), record_history=False)

    @property
    def _selected_parameter_id(self) -> str:
        selection = getattr(self, "_graph_selection", None)
        return (
            selection.primary_id(GraphElementKind.PARAMETER)
            if selection is not None
            else ""
        )

    @property
    def _selected_event_type_id(self) -> str:
        selection = getattr(self, "_graph_selection", None)
        return (
            selection.primary_id(GraphElementKind.EVENT_TYPE)
            if selection is not None
            else ""
        )

    @staticmethod
    def _particle_document_key(path: str):
        from Infernux.engine.interaction import DocumentKey, DocumentKind

        normalized = resolved_path(path)
        try:
            from Infernux.core.asset_types import read_meta_guid

            guid = read_meta_guid(normalized)
        except Exception:
            guid = ""
        if guid:
            return DocumentKey.asset(DocumentKind.PARTICLE_GRAPH, guid)
        return DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, normalized)

    def _replace_particle_document(self, *, resource_path: str, dirty: bool) -> None:
        from Infernux.engine.interaction import (
            DocumentCapability,
            DocumentKey,
            DocumentKind,
            DocumentRegistry,
        )

        path = resolved_path(resource_path) if resource_path else ""
        title = os.path.splitext(os.path.basename(path))[0] if path else self._asset.name
        registry = DocumentRegistry.instance()
        key = self._particle_document_key(path) if path else DocumentKey.session(
            DocumentKind.PARTICLE_GRAPH
        )
        document, _created = registry.open_or_create(
            key,
            title,
            resource_path=path,
            revision=1 if dirty else 0,
            saved_revision=0,
            capabilities=(
                DocumentCapability.SAVE
                | DocumentCapability.SAVE_AS
                | DocumentCapability.DISCARD
            ),
            controller=self._authoring_document_controller,
        )
        self._bind_replaced_document(document.document_id, dirty=dirty)
        self._graph_selection.refresh()

    def _particle_document(self):
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().get(self.document_id)

    def capture_document_restore_state(self, document_id: str) -> dict:
        if document_id != self.document_id:
            raise ValueError("Particle Graph restore capture targeted another document")
        self._sync_model_to_asset()
        return {
            "asset": self._asset.to_dict(),
            "file_path": self._file_path,
        }

    def restore_document_restore_state(self, state: dict) -> None:
        if not isinstance(state, dict) or set(state) != {"asset", "file_path"}:
            raise ValueError("Particle Graph document restore state is invalid")
        self._asset = ParticleGraphAsset.from_dict(copy.deepcopy(state["asset"]))
        self._file_path = (
            resolved_path(state["file_path"]) if state["file_path"] else ""
        )
        self._emitter_index = min(
            max(0, self._emitter_index), len(self._asset.emitters) - 1
        )
        self._bind_stage()
        self._graph_selection.clear(record_history=False)

    def recover_incompatible_document_restore_state(
        self,
        state,
        error: Exception,
    ) -> bool:
        del error
        path = str(state.get("file_path", "")) if isinstance(state, dict) else ""
        if path and os.path.isfile(path):
            return self.open_document_resource_immediate(path)
        self._asset = ParticleGraphAsset()
        self._file_path = ""
        self._emitter_index = 0
        self._stage = "init"
        self._bind_stage()
        self._replace_particle_document(resource_path="", dirty=True)
        return True

    def capture_graph_diff_checkpoint(self):
        """Capture an exception-only checkpoint for atomic diff replay."""
        self._sync_model_to_asset()
        return (self._asset, self._emitter_index, self._stage)

    def restore_graph_diff_checkpoint(self, checkpoint) -> None:
        self._asset, self._emitter_index, self._stage = checkpoint
        self._bind_stage()
        self._graph_selection.refresh()

    @staticmethod
    def _particle_element_id(emitter_id: str, canvas_id: str) -> str:
        return f"{emitter_id}/{canvas_id}"

    @staticmethod
    def _split_particle_element_id(stable_id: str) -> tuple[str, str]:
        emitter_id, separator, canvas_id = str(stable_id).partition("/")
        return (emitter_id, canvas_id) if separator else ("", "")

    def _graph_element_from_view(
        self, kind: GraphElementKind, view_id: str
    ) -> GraphElementRef:
        return GraphElementRef(
            kind,
            self._particle_element_id(self._selected_emitter().stable_id, view_id),
        )

    def _graph_element_to_view(self, element: GraphElementRef) -> str:
        if element.kind not in {GraphElementKind.NODE, GraphElementKind.LINK}:
            return element.stable_id
        emitter_id, canvas_id = self._split_particle_element_id(element.stable_id)
        return canvas_id if emitter_id == self._selected_emitter().stable_id else ""

    def _node_graph_authoring_identity(
        self, _kind: NodeGraphElementKind, stable_id: str
    ) -> str:
        return self._particle_element_id(
            self._selected_emitter().stable_id,
            stable_id,
        )

    def _node_graph_clipboard_node_identity(
        self, _old_id: str, payload: dict
    ) -> str:
        stage = str(payload.get("stage", ""))
        if not stage:
            raise ValueError("Particle Graph clipboard node has no lifecycle stage")
        return ParticleEmitterGraphAuthoringModel._canvas_uid(
            stage, uuid.uuid4().hex[:8]
        )

    def _node_graph_clipboard_link_identity(
        self, _old_id: str, payload: dict
    ) -> str:
        stage = ParticleEmitterGraphAuthoringModel.stage_for_uid(
            str(payload.get("source_node", ""))
        )
        if not stage:
            raise ValueError("Particle Graph clipboard link has no lifecycle stage")
        return ParticleEmitterGraphAuthoringModel._canvas_uid(
            stage, uuid.uuid4().hex[:8]
        )

    def _node_graph_remap_clipboard_node(
        self,
        _old_id: str,
        _new_id: str,
        payload: dict,
        node_id_map,
    ) -> dict:
        """Remap derived Attribute Cache/Capture IDs inside copied node data."""
        from Infernux.particle.asset import (
            particle_attribute_cache_id,
            particle_attribute_capture_id,
        )

        derived: dict[str, str] = {}
        for old_node_id, new_node_id in node_id_map.items():
            old_stage = ParticleEmitterGraphAuthoringModel.stage_for_uid(old_node_id)
            new_stage = ParticleEmitterGraphAuthoringModel.stage_for_uid(new_node_id)
            if not old_stage or not new_stage:
                continue
            old_raw = ParticleEmitterGraphAuthoringModel._document_uid(old_node_id)
            new_raw = ParticleEmitterGraphAuthoringModel._document_uid(new_node_id)
            derived[
                particle_attribute_cache_id(old_stage, old_raw)
            ] = particle_attribute_cache_id(new_stage, new_raw)
            derived[
                particle_attribute_capture_id(old_stage, old_raw)
            ] = particle_attribute_capture_id(new_stage, new_raw)

        def remap(value):
            if isinstance(value, str):
                return derived.get(value, value)
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, tuple):
                return tuple(remap(item) for item in value)
            if isinstance(value, dict):
                return {key: remap(item) for key, item in value.items()}
            return value

        return remap(payload)

    def _contains_graph_element(self, element: GraphElementRef) -> bool:
        if element.kind is GraphElementKind.GRAPH:
            return element.stable_id == self._asset.stable_id
        if element.kind is GraphElementKind.PARAMETER:
            return any(item.stable_id == element.stable_id for item in self._asset.parameters)
        if element.kind is GraphElementKind.EVENT_TYPE:
            return any(item.stable_id == element.stable_id for item in self._asset.event_types)
        if element.kind is GraphElementKind.EMITTER:
            return any(item.stable_id == element.stable_id for item in self._asset.emitters)
        if element.kind is GraphElementKind.EVENT_FLOW:
            emitter_id, flow_id = self._split_particle_element_id(element.stable_id)
            return any(
                emitter.stable_id == emitter_id
                and any(flow.stable_id == flow_id for flow in emitter.event_flows)
                for emitter in self._asset.emitters
            )
        if element.kind is GraphElementKind.DATA_INTERFACE:
            emitter_id, interface_id = self._split_particle_element_id(
                element.stable_id
            )
            return any(
                emitter.stable_id == emitter_id
                and any(
                    interface.stable_id == interface_id
                    for interface in emitter.data_interfaces
                )
                for emitter in self._asset.emitters
            )
        if element.kind in {GraphElementKind.NODE, GraphElementKind.LINK}:
            emitter_id, canvas_id = self._split_particle_element_id(element.stable_id)
            if not emitter_id or not canvas_id:
                return False
            emitter = next(
                (item for item in self._asset.emitters if item.stable_id == emitter_id),
                None,
            )
            if emitter is None:
                return False
            model = (
                self._model
                if emitter_id == self._selected_emitter().stable_id
                else ParticleEmitterGraphAuthoringModel(
                    emitter,
                    definition_set=particle_graph_node_definitions(self._asset),
                )
            )
            finder = model.find_node if element.kind is GraphElementKind.NODE else model.find_link
            return finder(canvas_id) is not None
        return False

    def _on_graph_selection_projected(
        self, elements: tuple[GraphElementRef, ...]
    ) -> None:
        primary = elements[-1] if elements else None
        if primary is None:
            return
        emitter_id = ""
        if primary.kind is GraphElementKind.EMITTER:
            emitter_id = primary.stable_id
        elif primary.kind in {
            GraphElementKind.NODE,
            GraphElementKind.LINK,
            GraphElementKind.EVENT_FLOW,
            GraphElementKind.DATA_INTERFACE,
        }:
            emitter_id, _local_id = self._split_particle_element_id(
                primary.stable_id
            )
        if not emitter_id:
            return
        index = next(
            (
                index
                for index, emitter in enumerate(self._asset.emitters)
                if emitter.stable_id == emitter_id
            ),
            -1,
        )
        if index >= 0 and index != self._emitter_index:
            self._sync_model_to_asset()
            self._emitter_index = index
            self._bind_stage()

    def _select_canvas_node(
        self,
        node_uid: str,
        *,
        reason: str = "particle_graph_node_selection",
        record_history: bool = False,
    ) -> bool:
        node_uid = str(node_uid or "")
        if not node_uid:
            return self._graph_selection.clear(
                reason=reason,
                record_history=record_history,
            )
        return self._graph_selection.select(
            (self._graph_element_from_view(GraphElementKind.NODE, node_uid),),
            reason=reason,
            record_history=record_history,
        )

    def _select_particle_element(
        self,
        kind: GraphElementKind,
        stable_id: str,
        *,
        reason: str,
        record_history: bool = False,
    ) -> bool:
        stable_id = str(stable_id or "")
        if not stable_id:
            return self._graph_selection.clear(
                reason=reason,
                record_history=record_history,
            )
        return self._graph_selection.select_one(
            kind,
            stable_id,
            reason=reason,
            record_history=record_history,
        )

    def _emitter_index_by_id(self, stable_id: str) -> int:
        return next(
            (
                index
                for index, emitter in enumerate(self._asset.emitters)
                if emitter.stable_id == stable_id
            ),
            -1,
        )

    def _replace_emitter_from_model(
        self,
        emitter_index: int,
        model: ParticleEmitterGraphAuthoringModel,
    ) -> None:
        emitter = self._asset.emitters[emitter_index]
        replacement = self._emitter_with_model_documents(emitter, model)
        if replacement == emitter:
            return
        emitters = list(self._asset.emitters)
        emitters[emitter_index] = replacement
        self._asset = replace(self._asset, emitters=tuple(emitters))

    @staticmethod
    def _emitter_with_model_documents(
        emitter: ParticleEmitterAsset,
        model: ParticleEmitterGraphAuthoringModel,
    ) -> ParticleEmitterAsset:
        documents = model.to_documents()
        updates = {
            stage: documents[stage]
            for stage in _STAGES
            if stage in documents and getattr(emitter, stage) != documents[stage]
        }
        event_flows = tuple(
            replace(flow, graph=documents.get(f"event.{flow.stable_id}", flow.graph))
            for flow in emitter.event_flows
        )
        if event_flows != emitter.event_flows:
            updates["event_flows"] = event_flows
        return replace(emitter, **updates) if updates else emitter

    def _resolve_dynamic_particle_definitions(self, model, _node):
        emitter = self._emitter_with_model_documents(
            self._selected_emitter(), model
        )
        emitters = list(self._asset.emitters)
        emitters[self._emitter_index] = emitter
        return particle_graph_node_definitions(
            replace(self._asset, emitters=tuple(emitters))
        )

    def _graph_state(self, asset: ParticleGraphAsset) -> NodeGraphAuthoringState:
        states: list[NodeGraphAuthoringState] = []
        definitions = particle_graph_node_definitions(asset)
        for emitter in asset.emitters:
            model = ParticleEmitterGraphAuthoringModel(
                emitter,
                definition_set=definitions,
            )
            emitter_id = emitter.stable_id
            states.append(
                model.capture_authoring_state(
                    identity=lambda _kind, canvas_id, emitter_id=emitter_id: (
                        self._particle_element_id(emitter_id, canvas_id)
                    )
                )
            )
        return NodeGraph.merge_authoring_states(states)

    def _graph_mutations_between(
        self,
        before_asset: ParticleGraphAsset,
        after_asset: ParticleGraphAsset,
    ) -> tuple[GraphMutation, ...]:
        core_mutations = NodeGraph.diff_authoring_states(
            self._graph_state(before_asset),
            self._graph_state(after_asset),
        )
        return tuple(
            GraphMutation(
                GraphMutationKind(mutation.kind.value),
                GraphElementRef(
                    GraphElementKind(mutation.element_kind.value),
                    mutation.stable_id,
                ),
                before=mutation.before,
                after=mutation.after,
                before_index=mutation.before_index,
                after_index=mutation.after_index,
            )
            for mutation in core_mutations
        )

    def apply_diff(self, diff: GraphActionDiff) -> None:
        """Apply one stable-identity Particle Graph authoring diff."""
        if diff.document_id != self.document_id:
            raise RuntimeError("graph diff targets a different Particle Graph document")
        rebind = False
        emitter_models: dict[int, ParticleEmitterGraphAuthoringModel] = {}

        def emitter_model(emitter_index: int) -> ParticleEmitterGraphAuthoringModel:
            model = emitter_models.get(emitter_index)
            if model is not None:
                return model
            model = (
                self._model
                if emitter_index == self._emitter_index and self._model is not None
                else ParticleEmitterGraphAuthoringModel(
                    self._asset.emitters[emitter_index],
                    definition_set=particle_graph_node_definitions(self._asset),
                )
            )
            emitter_models[emitter_index] = model
            return model

        def flush_emitter_models() -> None:
            for emitter_index, model in tuple(emitter_models.items()):
                self._replace_emitter_from_model(emitter_index, model)
            emitter_models.clear()

        for mutation in diff.mutations:
            element = mutation.element
            if element.kind is GraphElementKind.EVENT_TYPE:
                flush_emitter_models()
                index = next(
                    (
                        index
                        for index, event_type in enumerate(self._asset.event_types)
                        if event_type.stable_id == element.stable_id
                    ),
                    -1,
                )
                if mutation.kind is GraphMutationKind.INSERT:
                    if index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot insert Particle Graph event type {element.stable_id}"
                        )
                    event_type = ParticleEventType.from_dict(
                        mutation.after, "$.event_types[diff]"
                    )
                    event_types = list(self._asset.event_types)
                    event_types.insert(
                        max(0, min(int(mutation.after_index), len(event_types))),
                        event_type,
                    )
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot update Particle Graph event type {element.stable_id}"
                        )
                    event_type = ParticleEventType.from_dict(
                        mutation.after, "$.event_types[diff]"
                    )
                    event_types = list(self._asset.event_types)
                    event_types[index] = event_type
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if index < 0:
                        raise RuntimeError(
                            f"Particle Graph event type no longer exists: {element.stable_id}"
                        )
                    if any(
                        flow.event_id == element.stable_id
                        for emitter in self._asset.emitters
                        for flow in emitter.event_flows
                    ):
                        raise RuntimeError(
                            "Particle event type removal must remove or clear its flows "
                            "in the same diff"
                        )
                    event_type = self._asset.event_types[index]
                    event_types = list(self._asset.event_types)
                    event_types.pop(index)
                else:
                    raise RuntimeError(
                        f"unsupported Particle event mutation: {mutation.kind.value}"
                    )
                if event_type.stable_id != element.stable_id:
                    raise RuntimeError("Particle event mutation changed stable identity")
                self._asset = replace(self._asset, event_types=tuple(event_types))
                self._bind_stage()
                continue

            if element.kind is GraphElementKind.EVENT_FLOW:
                flush_emitter_models()
                emitter_id, flow_id = self._split_particle_element_id(
                    element.stable_id
                )
                emitter_index = self._emitter_index_by_id(emitter_id)
                if emitter_index < 0:
                    raise RuntimeError(
                        f"Particle Graph emitter no longer exists: {emitter_id}"
                    )
                emitter = self._asset.emitters[emitter_index]
                flow_index = next(
                    (
                        index
                        for index, flow in enumerate(emitter.event_flows)
                        if flow.stable_id == flow_id
                    ),
                    -1,
                )
                if mutation.kind is GraphMutationKind.INSERT:
                    if flow_index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot insert Particle Graph event flow {element.stable_id}"
                        )
                    flow = ParticleEventFlow.from_dict(
                        mutation.after, "$.emitters[diff].events[diff]"
                    )
                    flows = list(emitter.event_flows)
                    flows.insert(
                        max(0, min(int(mutation.after_index), len(flows))), flow
                    )
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if flow_index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot update Particle Graph event flow {element.stable_id}"
                        )
                    flow = ParticleEventFlow.from_dict(
                        mutation.after, "$.emitters[diff].events[diff]"
                    )
                    flows = list(emitter.event_flows)
                    flows[flow_index] = flow
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if flow_index < 0:
                        raise RuntimeError(
                            f"Particle Graph event flow no longer exists: {element.stable_id}"
                        )
                    flow = emitter.event_flows[flow_index]
                    flows = list(emitter.event_flows)
                    flows.pop(flow_index)
                else:
                    raise RuntimeError(
                        f"unsupported Particle event flow mutation: {mutation.kind.value}"
                    )
                if flow.stable_id != flow_id:
                    raise RuntimeError("Particle event flow mutation changed stable identity")
                emitters = list(self._asset.emitters)
                emitters[emitter_index] = replace(
                    emitter, event_flows=tuple(flows)
                )
                self._asset = replace(self._asset, emitters=tuple(emitters))
                self._emitter_index = emitter_index
                if mutation.kind is GraphMutationKind.INSERT:
                    self._stage = f"event.{flow_id}"
                valid_stages = set(_STAGES) | {
                    f"event.{item.stable_id}" for item in flows
                }
                if self._stage not in valid_stages:
                    self._stage = "init"
                self._bind_stage()
                continue

            if element.kind is GraphElementKind.EMITTER:
                flush_emitter_models()
                index = self._emitter_index_by_id(element.stable_id)
                requested_index: Optional[int] = None
                if mutation.kind is GraphMutationKind.INSERT:
                    if index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot insert Particle Graph emitter {element.stable_id}"
                        )
                    emitter = ParticleEmitterAsset.from_dict(
                        mutation.after,
                        "$.emitters[diff]",
                    )
                    if emitter.stable_id != element.stable_id:
                        raise RuntimeError("Particle emitter insertion changed stable identity")
                    emitters = list(self._asset.emitters)
                    target_index = max(
                        0,
                        min(int(mutation.after_index), len(emitters)),
                    )
                    emitters.insert(target_index, emitter)
                    requested_index = target_index
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if index < 0 or len(self._asset.emitters) <= 1:
                        raise RuntimeError(
                            f"cannot remove Particle Graph emitter {element.stable_id}"
                        )
                    emitters = list(self._asset.emitters)
                    emitters.pop(index)
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot update Particle Graph emitter {element.stable_id}"
                        )
                    current = self._asset.emitters[index]
                    if set(mutation.after) == {"name"}:
                        emitter = replace(current, name=str(mutation.after["name"]))
                    elif set(mutation.after) == {"settings"}:
                        emitter = replace(
                            current,
                            settings=EmitterSettings.from_dict(
                                mutation.after["settings"],
                                "$.emitters[diff].settings",
                            ),
                        )
                    else:
                        emitter = ParticleEmitterAsset.from_dict(
                            mutation.after,
                            "$.emitters[diff]",
                        )
                    if emitter.stable_id != element.stable_id:
                        raise RuntimeError("Particle emitter update changed stable identity")
                    emitters = list(self._asset.emitters)
                    emitters[index] = emitter
                else:
                    raise RuntimeError(
                        f"unsupported Particle emitter mutation: {mutation.kind.value}"
                    )
                active_id = self._selected_emitter().stable_id
                self._asset = replace(self._asset, emitters=tuple(emitters))
                active_index = self._emitter_index_by_id(active_id)
                self._emitter_index = (
                    requested_index
                    if requested_index is not None
                    else active_index
                    if active_index >= 0
                    else min(self._emitter_index, len(self._asset.emitters) - 1)
                )
                self._bind_stage()
                continue

            if element.kind is GraphElementKind.DATA_INTERFACE:
                flush_emitter_models()
                emitter_id, interface_id = self._split_particle_element_id(
                    element.stable_id
                )
                emitter_index = self._emitter_index_by_id(emitter_id)
                if emitter_index < 0:
                    raise RuntimeError(
                        f"Particle Graph emitter no longer exists: {emitter_id}"
                    )
                emitter = self._asset.emitters[emitter_index]
                interface_index = next(
                    (
                        index
                        for index, interface in enumerate(emitter.data_interfaces)
                        if interface.stable_id == interface_id
                    ),
                    -1,
                )
                interfaces = list(emitter.data_interfaces)
                if mutation.kind is GraphMutationKind.INSERT:
                    if interface_index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot insert Particle Data Interface {interface_id}"
                        )
                    interface = particle_data_interface_from_dict(mutation.after)
                    interfaces.insert(
                        max(0, min(int(mutation.after_index), len(interfaces))),
                        interface,
                    )
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if interface_index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot update Particle Data Interface {interface_id}"
                        )
                    interface = particle_data_interface_from_dict(mutation.after)
                    interfaces[interface_index] = interface
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if interface_index < 0:
                        raise RuntimeError(
                            f"Particle Data Interface no longer exists: {interface_id}"
                        )
                    interface = interfaces.pop(interface_index)
                else:
                    raise RuntimeError(
                        "unsupported Particle Data Interface mutation: "
                        f"{mutation.kind.value}"
                    )
                if interface.stable_id != interface_id:
                    raise RuntimeError(
                        "Particle Data Interface mutation changed stable identity"
                    )
                emitters = list(self._asset.emitters)
                emitters[emitter_index] = replace(
                    emitter,
                    data_interfaces=tuple(interfaces),
                )
                self._asset = replace(self._asset, emitters=tuple(emitters))
                self._emitter_index = emitter_index
                self._bind_stage()
                continue

            if element.kind is GraphElementKind.PARAMETER:
                flush_emitter_models()
                index = next(
                    (
                        index
                        for index, parameter in enumerate(self._asset.parameters)
                        if parameter.stable_id == element.stable_id
                    ),
                    -1,
                )
                if mutation.kind is GraphMutationKind.INSERT:
                    if index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot insert Particle Graph parameter {element.stable_id}"
                        )
                    parameter = ParticleParameter.from_dict(
                        mutation.after,
                        "$.parameters[diff]",
                    )
                    if parameter.stable_id != element.stable_id:
                        raise RuntimeError("Particle parameter insertion changed stable identity")
                    parameters = list(self._asset.parameters)
                    target_index = max(
                        0,
                        min(int(mutation.after_index), len(parameters)),
                    )
                    parameters.insert(target_index, parameter)
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(
                            f"cannot update Particle Graph parameter {element.stable_id}"
                        )
                    parameter = ParticleParameter.from_dict(
                        mutation.after,
                        "$.parameters[diff]",
                    )
                    if parameter.stable_id != element.stable_id:
                        raise RuntimeError("Particle parameter update changed stable identity")
                    parameters = list(self._asset.parameters)
                    parameters[index] = parameter
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if index < 0:
                        raise RuntimeError(
                            f"Particle Graph parameter no longer exists: {element.stable_id}"
                        )
                    if any(
                        node.properties.get("parameter") == element.stable_id
                        for emitter in self._asset.emitters
                        for document in (
                            emitter.init,
                            emitter.update,
                            emitter.collision_enter,
                            emitter.collision_stay,
                            emitter.collision_exit,
                            emitter.rendering,
                            *(flow.graph for flow in emitter.event_flows),
                        )
                        if document is not None
                        for node in document.nodes
                    ):
                        raise RuntimeError(
                            "Particle parameter removal must include its referencing nodes "
                            "in the same diff"
                        )
                    parameters = list(self._asset.parameters)
                    parameters.pop(index)
                elif mutation.kind is GraphMutationKind.MOVE:
                    if index < 0:
                        raise RuntimeError(
                            f"Particle Graph parameter no longer exists: "
                            f"{element.stable_id}"
                        )
                    target_index = int(mutation.after_index)
                    if target_index < 0 or target_index >= len(self._asset.parameters):
                        raise RuntimeError(
                            f"Particle Graph parameter move target is invalid: "
                            f"{target_index}"
                        )
                    parameters = list(self._asset.parameters)
                    parameter = parameters.pop(index)
                    parameters.insert(target_index, parameter)
                else:
                    raise RuntimeError(
                        f"unsupported Particle parameter mutation: {mutation.kind.value}"
                    )
                self._asset = replace(self._asset, parameters=tuple(parameters))
                self._bind_stage()
                continue

            if element.kind in {GraphElementKind.NODE, GraphElementKind.LINK}:
                emitter_id, canvas_id = self._split_particle_element_id(
                    element.stable_id
                )
                emitter_index = self._emitter_index_by_id(emitter_id)
                if emitter_index < 0:
                    raise RuntimeError(
                        f"Particle Graph emitter no longer exists: {emitter_id}"
                    )
                model = emitter_model(emitter_index)
                payload = mutation.after if isinstance(mutation.after, dict) else {}
                output_type = str(payload.get("type_id", ""))
                if not output_type and isinstance(mutation.before, dict):
                    output_type = str(mutation.before.get("type_id", ""))
                model.apply_authoring_mutation(
                    NodeGraphMutation(
                        NodeGraphMutationKind(mutation.kind.value),
                        NodeGraphElementKind(element.kind.value),
                        canvas_id,
                        before=mutation.before,
                        after=mutation.after,
                        before_index=mutation.before_index,
                        after_index=mutation.after_index,
                    )
                )
                if output_type.startswith("particle.output."):
                    rebind = True
                continue

            raise RuntimeError(
                "unsupported Particle Graph mutation: "
                f"{element.kind.value}/{mutation.kind.value}"
            )
        flush_emitter_models()
        if rebind:
            self._bind_stage()

    def on_graph_diff_applied(self, _diff: GraphActionDiff) -> None:
        self._graph_selection.refresh()

    def authoring_document_state(self) -> dict:
        """Return identity/dirty state without serializing the live graph model."""
        return {
            "file_path": str(self._file_path),
            "dirty": self._document_is_dirty(),
        }

    def authoring_snapshot(self, *, include_registered_types: bool = False) -> dict:
        """Return the currently open editor document, not a disk reparse."""
        self._sync_model_to_asset()
        nodes = []
        links = []
        if self._model is not None:
            for node in self._model.nodes:
                nodes.append(
                    {
                        "uid": str(node.uid),
                        "type_id": str(node.type_id),
                        "stage": str(self._model.stage_for_uid(node.uid) or ""),
                        "position": [float(node.pos_x), float(node.pos_y)],
                        "properties": copy.deepcopy(node.data),
                    }
                )
            for link in self._model.links:
                links.append(
                    {
                        "uid": str(link.uid),
                        "source_node": str(link.source_node),
                        "source_port": str(link.source_pin),
                        "target_node": str(link.target_node),
                        "target_port": str(link.target_pin),
                    }
                )
        return {
            "panel_id": self.window_id,
            "file_path": str(self._file_path),
            "dirty": self._document_is_dirty(),
            "emitter_index": int(self._emitter_index),
            "selected_node_uid": str(self._selected_node_uid),
            "emitters": [
                {
                    "stable_id": emitter.stable_id,
                    "name": emitter.name,
                    "settings": emitter.settings.to_dict(),
                    "data_interfaces": [
                        interface.to_dict() for interface in emitter.data_interfaces
                    ],
                }
                for emitter in self._asset.emitters
            ],
            "parameters": [value.to_dict() for value in self._asset.parameters],
            "event_types": [value.to_dict() for value in self._asset.event_types],
            "attribute_entries": (
                [
                    {"name": name, "stable_id": stable_id}
                    for name, stable_id in self._model.attribute_entries()
                ]
                if self._model is not None
                else []
            ),
            "registered_types": (
                [
                    self._authoring_definition_snapshot(
                        definition.type_id, canvas_definition=definition
                    )
                    for definition in (
                        self._model.registered_types()
                        if self._model is not None
                        else ()
                    )
                ]
                if include_registered_types
                else []
            ),
            "nodes": nodes,
            "links": links,
        }

    def authoring_type_catalog(
        self,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """Return a searchable page of node definitions for the selected emitter."""
        needle = str(query).strip().casefold()
        definitions = (
            self._model.registered_types() if self._model is not None else ()
        )
        matches = [
            definition
            for definition in definitions
            if definition.type_id not in self._HIDDEN_INTERNAL_RESOURCE_NODE_TYPES
            and (
                not needle
                or needle in str(definition.type_id).casefold()
                or needle in str(definition.label).casefold()
            )
        ]
        start = max(0, int(offset))
        page_size = min(200, max(1, int(limit)))
        end = min(len(matches), start + page_size)
        return {
            "query": str(query),
            "offset": start,
            "limit": page_size,
            "total": len(matches),
            "has_more": end < len(matches),
            "types": [
                self._authoring_definition_snapshot(
                    definition.type_id, canvas_definition=definition
                )
                for definition in matches[start:end]
            ],
        }

    def _authoring_definition_snapshot(
        self, type_id: str, *, canvas_definition=None
    ) -> dict:
        definition = self._definition_for_type(type_id)
        if definition is None:
            raise RuntimeError(
                f"Particle Graph node type is not registered: {type_id!r}"
            )
        canvas_fields = {
            field.id: field
            for field in getattr(canvas_definition, "inline_fields", ())
        }

        def property_choices(field):
            canvas_field = canvas_fields.get(field.id)
            values = tuple(getattr(canvas_field, "enum_values", ()))
            if not values:
                return field.choices
            labels = tuple(getattr(canvas_field, "enum_labels", ())) or values
            return tuple(zip(labels, values))

        return {
            "type_id": definition.type_id,
            "display_name": (
                str(canvas_definition.label)
                if canvas_definition is not None
                else definition.display_name
            ),
            "ports": [
                {
                    "id": port.id,
                    "display_name": port.display_name,
                    "direction": port.direction.value,
                    "kind": port.kind.value,
                    "type": (
                        port.value_type.to_dict()
                        if port.value_type is not None
                        else None
                    ),
                    "type_variable": port.type_variable,
                    "type_property": port.type_property,
                    "dimension_policy": port.dimension_policy.value,
                    "required": bool(port.required),
                    "default": copy.deepcopy(port.default),
                }
                for port in definition.ports
            ],
            "properties": [
                {
                    "id": field.id,
                    "type": field.value_type.to_dict(),
                    "default": copy.deepcopy(field.default),
                    "choices": [
                        {"label": label, "value": copy.deepcopy(value)}
                        for label, value in property_choices(field)
                    ],
                }
                for field in definition.properties
            ],
        }

    def _on_node_asset_reference(
        self, node_uid: str, field_id: str, asset_type: str, reference
    ) -> None:
        """View callback for asset-valued node inputs (tuned for the model)."""
        del asset_type
        try:
            self.set_node_asset_reference(node_uid, field_id, reference)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            Debug.log_error(f"Particle Graph asset input edit failed: {exc}")

    @staticmethod
    def _node_asset_reference_items(
        _node_uid: str,
        _field_id: str,
        asset_type: str,
        query: str,
    ):
        return _builtin_mesh_picker_items(query) if asset_type == "Mesh" else ()

    def set_node_asset_reference(
        self, node_uid: str, property_name: str, file_path
    ) -> dict:
        """Edit an asset-valued node input through the live authoring model."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        node = self._model.find_node(str(node_uid))
        if node is None:
            raise KeyError(f"Particle Graph node not found: {node_uid!r}")
        definition = self._definition_for_node(node)
        if definition is None:
            raise RuntimeError(
                f"Particle Graph node type is not registered: {node.type_id!r}"
            )
        key = str(property_name)
        field = next((item for item in definition.properties if item.id == key), None)
        asset_port = next(
            (
                item
                for item in definition.ports
                if item.id == key
                and item.direction is PortDirection.INPUT
                and item.kind is PortKind.VALUE
                and item.value_type is not None
                and item.value_type.value_type
                in {ValueType.TEXTURE2D, ValueType.MESH}
            ),
            None,
        )
        is_asset_reference = (
            field is not None
            and field.value_type.value_type is ValueType.ASSET_REF
        )
        if not is_asset_reference and asset_port is None:
            valid = [
                item.id
                for item in definition.properties
                if item.value_type.value_type is ValueType.ASSET_REF
            ]
            valid.extend(
                item.id
                for item in definition.ports
                if item.direction is PortDirection.INPUT
                and item.kind is PortKind.VALUE
                and item.value_type is not None
                and item.value_type.value_type
                in {ValueType.TEXTURE2D, ValueType.MESH}
            )
            raise KeyError(
                f"Particle Graph node {node_uid!r} has no asset property {key!r}; "
                f"valid properties: {valid}"
            )

        asset_type = (
            "Mesh"
            if key == "mesh"
            or (
                asset_port is not None
                and asset_port.value_type.value_type is ValueType.MESH
            )
            else "Texture"
            if asset_port is not None
            and asset_port.value_type.value_type is ValueType.TEXTURE2D
            else "Material"
        )
        candidate = file_path
        if candidate is None or (
            isinstance(candidate, dict)
            and not any(
                str(candidate.get(name) or "").strip()
                for name in ("guid", "path_hint", "path", "builtin", "built_in")
            )
        ):
            reference = AssetReference().to_dict()
            if node.data.get(key) == reference:
                return copy.deepcopy(reference)
            previous = copy.deepcopy(node.data.get(key))
            self._on_node_data_changed(node.uid, key, previous, reference)
            self._select_canvas_node(node.uid)
            stage = self._model.stage_for_uid(node.uid)
            if stage:
                self._select_stage(stage)
            return copy.deepcopy(reference)
        if asset_type == "Mesh":
            token = str(file_path).strip() if isinstance(file_path, str) else ""
            if token.startswith("builtin-mesh:"):
                candidate = {
                    "asset_type": "Mesh",
                    "builtin": token.removeprefix("builtin-mesh:"),
                }
            elif token in BUILTIN_MESH_NAMES:
                candidate = {"asset_type": "Mesh", "builtin": token}
        reference = _particle_asset_reference(asset_type, candidate).to_dict()
        if node.data.get(key) == reference:
            return copy.deepcopy(reference)

        previous = copy.deepcopy(node.data.get(key))
        self._on_node_data_changed(node.uid, key, previous, reference)
        self._select_canvas_node(node.uid)
        stage = self._model.stage_for_uid(node.uid)
        if stage:
            self._select_stage(stage)
        return copy.deepcopy(reference)

    def add_authoring_node(
        self, stage: str, type_id: str, x: float = 0.0, y: float = 0.0
    ) -> dict:
        """Create a node through the same model and Undo path as the canvas."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        stage = str(stage)
        type_id = str(type_id)
        if type_id in self._HIDDEN_INTERNAL_RESOURCE_NODE_TYPES:
            raise ValueError(
                "Vector Field and SDF authoring are not available in this release"
            )
        valid_stages = set(_STAGES) | {
            f"event.{flow.stable_id}" for flow in self._selected_emitter().event_flows
        }
        if stage not in valid_stages:
            raise ValueError(f"Unknown Particle Graph stage: {stage!r}")
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise ValueError("Particle Graph node position must be finite")
        definition = self._definition_for_type(type_id)
        collision_root = type_id in {
            "particle.root.collision_enter",
            "particle.root.collision_stay",
            "particle.root.collision_exit",
        }
        if definition is None or (
            type_id.startswith("particle.root.") and not collision_root
        ):
            raise ValueError(f"Particle Graph node type cannot be created: {type_id!r}")
        if collision_root and type_id != f"particle.root.{stage}":
            raise ValueError(
                "Collision lifecycle roots must be created in their matching lifecycle stage"
            )
        domain = "particle.event" if stage.startswith("event.") else f"particle.{stage}"
        if not particle_stage_definition_filter(domain)(definition):
            raise ValueError(
                f"Particle Graph node type {type_id!r} is not valid in {stage!r}"
            )

        self._stage = stage
        self._model.set_authoring_stage(stage)
        self._model.prepare_node_creation(stage)
        node = self._on_node_add(type_id, float(x), float(y))
        if node is None or self._model.stage_for_uid(node.uid) != stage:
            raise RuntimeError(f"Particle Graph could not create {type_id!r} in {stage!r}")
        self._select_canvas_node(node.uid)
        result = {
            "uid": str(node.uid),
            "type_id": str(node.type_id),
            "stage": stage,
            "properties": copy.deepcopy(node.data),
        }
        attribute_id = self._model.attribute_id_for_cache_node(node.uid)
        if attribute_id:
            result["attribute_id"] = attribute_id
        return result

    def set_node_property(self, node_uid: str, property_name: str, value) -> dict:
        """Set a typed field currently editable in the node Inspector."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        node = self._model.find_node(str(node_uid))
        if node is None:
            raise KeyError(f"Particle Graph node not found: {node_uid!r}")
        definition = self._definition_for_node(node)
        if definition is None:
            raise RuntimeError(
                f"Particle Graph node type is not registered: {node.type_id!r}"
        )
        key = str(property_name)
        field = next((item for item in definition.properties if item.id == key), None)
        field_value_type = field.value_type if field is not None else None
        if field is None:
            port = next(
                (
                    item
                    for item in definition.ports
                    if item.id == key
                    and item.direction is PortDirection.INPUT
                    and item.kind is PortKind.VALUE
                    and not item.required
                    and (item.value_type is not None or item.type_property)
                ),
                None,
            )
            if port is None:
                raise KeyError(
                    f"Particle Graph node {node_uid!r} has no editable property {key!r}"
                )
            if any(
                link.target_node == node.uid and link.target_pin == key
                for link in self._model.links
            ):
                raise ValueError(
                    f"Particle Graph node {node_uid!r}.{key} is driven by a value link"
                )
            field = port
            field_value_type = self._model._effective_port_type(node, port)
        if field_value_type is None:
            raise ValueError(
                f"Particle Graph node {node_uid!r}.{key} has no resolved value type"
            )
        if field_value_type.value_type in {
            ValueType.ASSET_REF,
            ValueType.TEXTURE2D,
            ValueType.MESH,
        }:
            raise ValueError(
                "Asset-valued inputs must use particle_graph_set_node_asset"
            )
        if key == "attribute":
            entries = self._model.attribute_entries()
            valid_attributes = {attribute_id for _label, attribute_id in entries}
            if str(value) not in valid_attributes:
                raise ValueError(
                    f"Particle Graph node {node_uid!r} references unknown attribute "
                    f"{value!r}"
                )
        if key == "event":
            valid_events = {event_id for _label, event_id in self._model.event_entries()}
            if str(value) not in valid_events:
                raise ValueError(
                    f"Particle Graph node {node_uid!r} references unknown event {value!r}"
                )
        if (
            node.type_id == "particle.output.ribbon"
            and key == "sort"
            and str(value) != "none"
        ):
            raise ValueError(
                "Ribbon Output uses stable strip topology ordering and requires sort='none'"
            )
        if key == "interface":
            expected = self._data_interface_type_for_node(node.type_id)
            if expected is not None and str(value):
                interface = next(
                    (
                        item
                        for item in self._selected_emitter().data_interfaces
                        if item.stable_id == str(value)
                    ),
                    None,
                )
                if not isinstance(interface, expected):
                    raise ValueError(
                        f"Particle Graph node {node_uid!r} requires a {expected.__name__} Data Interface"
                    )
        error = ExpressionCompiler._literal_error(field_value_type, value)
        if error:
            raise ValueError(
                f"Particle Graph node {node_uid!r}.{key} {error}"
            )
        previous = copy.deepcopy(node.data.get(key, field.default))
        next_value = copy.deepcopy(value)
        self._on_node_data_changed(node.uid, key, previous, next_value)
        self._select_canvas_node(node.uid)
        stage = self._model.stage_for_uid(node.uid)
        if stage:
            self._select_stage(stage)
        return {
            "node_uid": str(node.uid),
            "property_name": key,
            "value": copy.deepcopy(node.data.get(key)),
            "changed": previous != next_value,
        }

    def remove_authoring_node(self, node_uid: str) -> dict:
        """Delete one user node through the same model and Undo path as the canvas."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        node = self._model.find_node(str(node_uid))
        if node is None:
            raise KeyError(f"Particle Graph node not found: {node_uid!r}")
        stage = str(self._model.stage_for_uid(node.uid) or "")
        type_id = str(node.type_id)
        self._on_nodes_deleted((node.uid,))
        if self._model.find_node(node.uid) is not None:
            raise ValueError(f"Particle Graph node cannot be deleted: {node_uid!r}")
        return {
            "node_uid": str(node_uid),
            "type_id": type_id,
            "stage": stage,
            "changed": True,
        }

    @staticmethod
    def _data_interface_type_for_node(type_id: str):
        if type_id in {
            "particle.collision.sdf",
            "particle.sdf.sample_distance",
            "particle.sdf.sample_gradient",
        }:
            return SdfVolume
        if type_id == "particle.vector_field.sample":
            return VectorField
        return None

    def _emitter_index_for_id(self, emitter_id: str) -> int:
        emitter_id = str(emitter_id)
        index = next(
            (
                index
                for index, emitter in enumerate(self._asset.emitters)
                if emitter.stable_id == emitter_id
            ),
            -1,
        )
        if index < 0:
            raise KeyError(f"Particle emitter not found: {emitter_id!r}")
        return index

    @staticmethod
    def _new_data_interface(kind: str, name: str):
        del kind, name
        raise ValueError(
            "Vector Field and SDF authoring are not available in this release"
        )

    def add_authoring_data_interface(
        self, emitter_id: str, kind: str, name: str = ""
    ) -> dict:
        """Add a typed Data Interface through the live document and Undo stack."""
        self._sync_model_to_asset()
        index = self._emitter_index_for_id(emitter_id)
        interface = self._new_data_interface(kind, name)
        ref = GraphElementRef(
            GraphElementKind.DATA_INTERFACE,
            self._particle_element_id(emitter_id, interface.stable_id),
        )
        if not self._execute_graph_mutations(
            "Add Particle Graph Data Interface",
            (
                GraphMutation(
                    GraphMutationKind.INSERT,
                    ref,
                    after=interface.to_dict(),
                    after_index=len(self._asset.emitters[index].data_interfaces),
                ),
            ),
            selection_after=(ref,),
        ):
            raise RuntimeError("Particle Data Interface could not be created")
        return interface.to_dict()

    @staticmethod
    def _data_interface_reference(interface):
        return interface.texture

    @staticmethod
    def _data_interface_asset_contract(interface) -> str:
        if isinstance(interface, SdfVolume):
            return "Texture.SDF"
        if isinstance(interface, VectorField):
            return "Texture.VectorField"
        raise TypeError("unsupported Particle Data Interface")

    def _replace_authoring_data_interface(
        self,
        emitter_index: int,
        interface_id: str,
        replacement,
        description: str,
    ) -> bool:
        emitter = self._asset.emitters[emitter_index]
        interface_index = next(
            (
                index
                for index, interface in enumerate(emitter.data_interfaces)
                if interface.stable_id == str(interface_id)
            ),
            -1,
        )
        if interface_index < 0:
            raise KeyError(f"Particle Data Interface not found: {interface_id!r}")
        current = emitter.data_interfaces[interface_index]
        if replacement == current:
            return False
        ref = GraphElementRef(
            GraphElementKind.DATA_INTERFACE,
            self._particle_element_id(emitter.stable_id, interface_id),
        )
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    ref,
                    before=current.to_dict(),
                    after=replacement.to_dict(),
                    before_index=interface_index,
                    after_index=interface_index,
                ),
            ),
            selection_after=(ref,),
        )

    def set_authoring_data_interface_asset(
        self, emitter_id: str, interface_id: str, file_path: str
    ) -> dict:
        """Set an imported source asset on a typed Data Interface."""
        self._sync_model_to_asset()
        emitter_index = self._emitter_index_for_id(emitter_id)
        emitter = self._asset.emitters[emitter_index]
        interface = next(
            (
                item
                for item in emitter.data_interfaces
                if item.stable_id == str(interface_id)
            ),
            None,
        )
        if interface is None:
            raise KeyError(f"Particle Data Interface not found: {interface_id!r}")
        asset_type = self._data_interface_asset_contract(interface)
        reference = _particle_asset_reference(asset_type, file_path)
        replacement = replace(
            interface,
            texture=reference,
        )
        changed = self._replace_authoring_data_interface(
            emitter_index,
            interface_id,
            replacement,
            "Set Particle Graph Data Interface asset",
        )
        return {**replacement.to_dict(), "changed": changed}

    def patch_authoring_data_interface(
        self, emitter_id: str, interface_id: str, values: dict
    ) -> dict:
        """Patch non-identity Data Interface fields through the strict decoder."""
        if type(values) is not dict or not values:
            raise ValueError("Particle Data Interface patch must be a non-empty object")
        self._sync_model_to_asset()
        emitter_index = self._emitter_index_for_id(emitter_id)
        emitter = self._asset.emitters[emitter_index]
        interface = next(
            (
                item
                for item in emitter.data_interfaces
                if item.stable_id == str(interface_id)
            ),
            None,
        )
        if interface is None:
            raise KeyError(f"Particle Data Interface not found: {interface_id!r}")
        payload = interface.to_dict()
        immutable = {"kind", "stable_id", "texture", "mesh", "cache"}
        allowed = set(payload) - immutable
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unsupported Particle Data Interface fields: {unknown}")
        for key, value in values.items():
            payload[key] = copy.deepcopy(value)
        replacement = particle_data_interface_from_dict(payload)
        changed = self._replace_authoring_data_interface(
            emitter_index,
            interface_id,
            replacement,
            "Edit Particle Graph Data Interface",
        )
        return {**replacement.to_dict(), "changed": changed}

    def remove_authoring_data_interface(
        self, emitter_id: str, interface_id: str
    ) -> dict:
        """Remove an unreferenced Data Interface from one emitter."""
        self._sync_model_to_asset()
        emitter_index = self._emitter_index_for_id(emitter_id)
        emitter = self._asset.emitters[emitter_index]
        interface = next(
            (
                item
                for item in emitter.data_interfaces
                if item.stable_id == str(interface_id)
            ),
            None,
        )
        if interface is None:
            raise KeyError(f"Particle Data Interface not found: {interface_id!r}")
        referenced_by = [
            node.uid
            for stage in _STAGES
            for document in (getattr(emitter, stage),)
            if document is not None
            for node in document.nodes
            if node.properties.get("interface") == interface.stable_id
        ]
        if (
            emitter.settings.shape.kind is EmitterShapeKind.SDF
            and emitter.settings.shape.sdf_interface == interface.stable_id
        ):
            referenced_by.append("Emitter Settings / Emission Shape")
        if referenced_by:
            raise ValueError(
                f"Particle Data Interface {interface.stable_id!r} is still referenced by nodes {referenced_by}"
            )
        ref = GraphElementRef(
            GraphElementKind.DATA_INTERFACE,
            self._particle_element_id(emitter.stable_id, interface.stable_id),
        )
        if not self._execute_graph_mutations(
            "Remove Particle Graph Data Interface",
            (
                GraphMutation(
                    GraphMutationKind.REMOVE,
                    ref,
                    before=interface.to_dict(),
                    before_index=emitter.data_interfaces.index(interface),
                ),
            ),
            selection_after=(),
        ):
            raise RuntimeError("Particle Data Interface could not be removed")
        return {**interface.to_dict(), "changed": True}

    def connect_exec(
        self,
        source_node_uid: str,
        target_node_uid: str,
        source_port: str = "out",
        target_port: str = "in",
    ) -> dict:
        """Connect two Exec endpoints through the strict graph model."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        source_uid = str(source_node_uid)
        target_uid = str(target_node_uid)
        source_port = str(source_port)
        target_port = str(target_port)
        source = self._model.find_node(source_uid)
        target = self._model.find_node(target_uid)
        if source is None or target is None:
            raise KeyError(
                f"Particle Graph Exec endpoint not found: {source_uid!r} -> {target_uid!r}"
            )
        for link in self._model.links:
            if (
                link.source_node == source_uid
                and link.source_pin == source_port
                and link.target_node == target_uid
                and link.target_pin == target_port
            ):
                return {"link_uid": str(link.uid), "changed": False}
        validation = self._model.validate_link(
            source_uid, source_port, target_uid, target_port
        )
        if not validation:
            raise ValueError(
                f"Particle Graph Exec connection is invalid ({validation.code}): "
                f"{validation.message}"
            )
        self._on_link_created(source_uid, source_port, target_uid, target_port)
        created = next(
            (
                link
                for link in self._model.links
                if link.source_node == source_uid
                and link.source_pin == source_port
                and link.target_node == target_uid
                and link.target_pin == target_port
            ),
            None,
        )
        if created is None:
            raise RuntimeError(
                f"Particle Graph could not connect {source_uid!r} to {target_uid!r}"
            )
        self._select_canvas_node(target_uid)
        stage = self._model.stage_for_uid(target_uid)
        if stage:
            self._select_stage(stage)
        return {
            "link_uid": str(created.uid),
            "source_port": source_port,
            "target_port": target_port,
            "changed": True,
        }

    def disconnect_link(self, link_uid: str) -> dict:
        """Disconnect one Exec or value link through the strict graph model."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        link_uid = str(link_uid)
        link = next(
            (item for item in self._model.links if str(item.uid) == link_uid),
            None,
        )
        if link is None:
            raise KeyError(f"Particle Graph link not found: {link_uid!r}")
        self._on_link_deleted(link_uid)
        if self._model.find_link(link_uid) is not None:
            raise RuntimeError(
                f"Particle Graph could not disconnect link {link_uid!r}"
            )
        return {
            "link_uid": link_uid,
            "source_node_uid": str(link.source_node),
            "source_port": str(link.source_pin),
            "target_node_uid": str(link.target_node),
            "target_port": str(link.target_pin),
            "changed": True,
        }

    def connect_value(
        self,
        source_node_uid: str,
        source_port: str,
        target_node_uid: str,
        target_port: str,
    ) -> dict:
        """Connect one typed value through the same replacement path as the canvas."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        endpoints = (str(source_node_uid), str(target_node_uid))
        if any(self._model.find_node(uid) is None for uid in endpoints):
            raise KeyError(f"Particle Graph value endpoint not found: {endpoints!r}")
        source_port = str(source_port)
        target_port = str(target_port)
        existing = next(
            (
                link
                for link in self._model.links
                if link.target_node == endpoints[1]
                and link.target_pin == target_port
            ),
            None,
        )
        if (
            existing is not None
            and existing.source_node == endpoints[0]
            and existing.source_pin == source_port
        ):
            return {"link_uid": str(existing.uid), "changed": False}
        validation = self._model.validate_link(
            endpoints[0],
            source_port,
            endpoints[1],
            target_port,
            ignore_link_uid=existing.uid if existing is not None else "",
        )
        if not validation:
            raise ValueError(
                f"Particle Graph value connection is invalid ({validation.code}): "
                f"{validation.message}"
            )
        if existing is not None:
            self._on_link_replaced(
                existing.uid,
                endpoints[0],
                source_port,
                endpoints[1],
                target_port,
            )
        else:
            self._on_link_created(
                endpoints[0], source_port, endpoints[1], target_port
            )
        created = next(
            (
                link
                for link in self._model.links
                if link.source_node == endpoints[0]
                and link.source_pin == source_port
                and link.target_node == endpoints[1]
                and link.target_pin == target_port
            ),
            None,
        )
        if created is None:
            raise RuntimeError("Particle Graph could not connect the value ports")
        self._select_canvas_node(endpoints[1])
        stage = self._model.stage_for_uid(endpoints[1])
        if stage:
            self._select_stage(stage)
        return {"link_uid": str(created.uid), "changed": True}

    def select_authoring_emitter(self, emitter_id: str) -> dict:
        emitter_id = str(emitter_id)
        index = next(
            (
                index
                for index, emitter in enumerate(self._asset.emitters)
                if emitter.stable_id == emitter_id
            ),
            -1,
        )
        if index < 0:
            raise KeyError(f"Particle emitter not found: {emitter_id!r}")
        self._select_emitter(index)
        return {"stable_id": emitter_id, "index": index}

    def add_authoring_emitter(self, name: str) -> dict:
        """Add an emitter through one undoable live-document transaction."""
        name = str(name).strip()
        if not name:
            raise ValueError("Particle emitter name cannot be empty")
        if any(emitter.name == name for emitter in self._asset.emitters):
            raise ValueError(f"Particle emitter name already exists: {name!r}")
        emitter = ParticleEmitterAsset(name=name)
        selected = GraphElementRef(GraphElementKind.EMITTER, emitter.stable_id)
        if not self._execute_graph_mutations(
            "Add particle emitter",
            (
                GraphMutation(
                    GraphMutationKind.INSERT,
                    selected,
                    after=emitter.to_dict(),
                    after_index=len(self._asset.emitters),
                ),
            ),
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle emitter could not be created")
        self._stage = "init"
        return {
            "stable_id": emitter.stable_id,
            "name": emitter.name,
            "settings": emitter.settings.to_dict(),
        }

    def remove_authoring_emitter(self, emitter_id: str) -> dict:
        """Remove one emitter and its private lifecycle/event implementations."""
        if len(self._asset.emitters) <= 1:
            raise ValueError("Particle Graph must keep at least one emitter")
        emitter_id = str(emitter_id)
        emitter_index = next(
            (
                index
                for index, emitter in enumerate(self._asset.emitters)
                if emitter.stable_id == emitter_id
            ),
            -1,
        )
        if emitter_index < 0:
            raise KeyError(f"Particle emitter not found: {emitter_id!r}")

        self._sync_model_to_asset()
        removed_emitter = self._asset.emitters[emitter_index]
        if not self._execute_graph_mutations(
            "Remove particle emitter",
            (
                GraphMutation(
                    GraphMutationKind.REMOVE,
                    GraphElementRef(GraphElementKind.EMITTER, emitter_id),
                    before=removed_emitter.to_dict(),
                    before_index=emitter_index,
                ),
            ),
            selection_after=(),
        ):
            raise RuntimeError("Particle emitter could not be removed")
        return {
            "emitter": removed_emitter.to_dict(),
            "changed": True,
        }

    def add_authoring_parameter(
        self,
        name: str,
        value_type: str = "f32",
        default=None,
        *,
        exposed: bool = True,
        space: str = CoordinateSpace.NONE.value,
    ) -> dict:
        """Add one graph-level Blackboard parameter through the Undo path."""
        name = str(name).strip()
        if not name:
            raise ValueError("Particle parameter name cannot be empty")
        kind = ValueType(str(value_type))
        if kind not in _PARAMETER_VALUE_TYPES:
            raise ValueError(f"Unsupported Particle parameter type: {kind.value!r}")
        if default is None:
            default = _event_field_default(kind)
        edit = _PARTICLE_PARAMETER_POLICY.create(
            GraphParameterCollection(self._asset.parameters),
            name=name,
            value_type=TypeRef(kind, CoordinateSpace(str(space))),
            default=default,
            exposed=bool(exposed),
            writable=False,
        )
        parameter = edit.after
        if not isinstance(parameter, ParticleParameter):
            raise RuntimeError("Particle parameter policy returned an invalid value")
        selected = GraphElementRef(GraphElementKind.PARAMETER, parameter.stable_id)
        transaction = GraphParameterTransaction.begin(
            GraphParameterCollection(self._asset.parameters)
        ).create(parameter)
        if not self._execute_graph_parameter_transaction(
            "Add Particle Graph parameter",
            transaction,
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle Graph parameter could not be created")
        return parameter.to_dict()

    def add_authoring_parameter_node(
        self,
        parameter_id: str,
        x: float,
        y: float,
        *,
        stage: str = "",
    ) -> dict:
        """Create a typed parameter node at a canvas position in one transaction."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        parameter_id = str(parameter_id)
        parameter = GraphParameterCollection(self._asset.parameters).find(parameter_id)
        if parameter is None:
            raise KeyError(f"Particle parameter not found: {parameter_id!r}")
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise ValueError("Particle Graph node position must be finite")
        if stage:
            target_stage = str(stage)
        else:
            # Use the same lane picker as add_node. stage_nearest_y used to
            # include inactive collision bands, so a drop on empty canvas
            # requested collision_enter and then failed the stage check.
            self._model.prepare_node_creation("")
            target_stage = self._model.stage_for_new_node(
                "particle.parameter", float(y)
            )
        valid_stages = set(_STAGES) | {
            f"event.{flow.stable_id}" for flow in self._selected_emitter().event_flows
        }
        if target_stage not in valid_stages:
            raise ValueError(f"Unknown Particle Graph stage: {target_stage!r}")

        before = self._model.capture_authoring_state()
        self._stage = target_stage
        self._model.set_authoring_stage(target_stage)
        self._model.prepare_node_creation(target_stage)
        node = self._model.add_node(
            "particle.parameter",
            float(x),
            float(y),
            parameter=parameter_id,
        )
        if self._model.stage_for_uid(node.uid) != target_stage:
            self._model.remove_node(node.uid)
            raise RuntimeError("Particle Graph created the parameter node in the wrong stage")
        selected = self._graph_element_from_view(GraphElementKind.NODE, node.uid)
        if not self._commit_node_graph_change(
            "Add Particle Graph parameter node",
            before,
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle Graph parameter node could not be created")
        return {
            "uid": str(node.uid),
            "type_id": str(node.type_id),
            "stage": target_stage,
            "properties": copy.deepcopy(node.data),
        }

    def update_authoring_parameter(self, parameter_id: str, values: dict) -> dict:
        """Update a complete typed Blackboard field without changing its identity."""
        parameter_id = str(parameter_id)
        edit = _PARTICLE_PARAMETER_POLICY.update(
            GraphParameterCollection(self._asset.parameters),
            parameter_id,
            values,
        )
        current = edit.before
        updated = edit.after
        index = edit.index
        if not isinstance(current, ParticleParameter) or not isinstance(
            updated, ParticleParameter
        ):
            raise RuntimeError("Particle parameter policy returned an invalid value")
        if not edit.changed:
            return {**current.to_dict(), "changed": False}
        if (
            updated.value_type == current.value_type
            and not (current.writable and not updated.writable)
        ):
            selected = GraphElementRef(GraphElementKind.PARAMETER, parameter_id)
            transaction = GraphParameterTransaction.begin(
                GraphParameterCollection(self._asset.parameters)
            ).update(updated)
            if not self._execute_graph_parameter_transaction(
                "Edit Particle Graph parameter",
                transaction,
                merge_key=f"particle_parameter:{parameter_id}",
                selection_after=(selected,),
            ):
                raise RuntimeError("Particle Graph parameter could not be updated")
            return {**updated.to_dict(), "changed": True}
        parameters = list(self._asset.parameters)
        parameters[index] = updated
        parameter_asset = replace(self._asset, parameters=tuple(parameters))
        emitters = self._asset.emitters
        if updated.value_type != current.value_type:
            emitters = self._rebuild_parameter_dependencies(
                parameter_asset,
                parameter_id,
            )
        if current.writable and not updated.writable:
            emitters = tuple(
                self._remove_parameter_store_nodes(emitter, parameter_id)
                for emitter in emitters
            )
        before_asset = self._asset
        after_asset = replace(
            self._asset,
            parameters=tuple(parameters),
            emitters=emitters,
        )
        graph_mutations = self._graph_mutations_between(before_asset, after_asset)
        selected = GraphElementRef(GraphElementKind.PARAMETER, parameter_id)
        transaction = GraphParameterTransaction.begin(
            GraphParameterCollection(self._asset.parameters)
        ).update(updated)
        if not self._execute_graph_parameter_transaction(
            "Edit Particle Graph parameter",
            transaction,
            mutations_before=graph_mutations,
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle Graph parameter could not be updated")
        return {**updated.to_dict(), "changed": True}

    def remove_authoring_parameter(self, parameter_id: str) -> dict:
        """Remove a Blackboard field and every node that references it."""
        parameter_id = str(parameter_id)
        parameters = GraphParameterCollection(self._asset.parameters)
        removed = parameters.require(parameter_id)
        if not isinstance(removed, ParticleParameter):
            raise RuntimeError("Particle parameter collection contains an invalid value")
        before_asset = self._asset
        emitters = tuple(
            self._remove_parameter_nodes(emitter, parameter_id)
            for emitter in self._asset.emitters
        )
        after_asset = replace(
            self._asset,
            parameters=tuple(
                parameter
                for parameter in self._asset.parameters
                if parameter.stable_id != parameter_id
            ),
            emitters=emitters,
        )
        graph_mutations = self._graph_mutations_between(before_asset, after_asset)
        transaction = GraphParameterTransaction.begin(parameters).delete(parameter_id)
        if not self._execute_graph_parameter_transaction(
            "Remove Particle Graph parameter",
            transaction,
            mutations_before=graph_mutations,
            selection_after=(),
        ):
            raise RuntimeError("Particle Graph parameter could not be removed")
        return {**removed.to_dict(), "changed": True}

    def _rebuild_parameter_dependencies(
        self,
        parameter_asset: ParticleGraphAsset,
        parameter_id: str,
    ) -> tuple[ParticleEmitterAsset, ...]:
        """Re-resolve every node whose schema depends on one parameter."""
        old_definitions = particle_graph_node_definitions(self._asset)
        new_definitions = particle_graph_node_definitions(parameter_asset)
        rebuilt_emitters = []
        for emitter in self._asset.emitters:
            model = ParticleEmitterGraphAuthoringModel(
                emitter,
                definition_set=old_definitions,
            )
            affected = [
                node.uid
                for node in model.nodes
                if node.type_id in {"particle.parameter", "particle.parameter.set"}
                and str(node.data.get("parameter", "")) == parameter_id
            ]
            if affected:
                model.set_definition_set_resolver(
                    lambda _model, _node, definitions=new_definitions: definitions
                )
                model.rebuild_nodes({}, affected_node_uids=affected)
                emitter = self._emitter_with_model_documents(emitter, model)
            rebuilt_emitters.append(emitter)
        return tuple(rebuilt_emitters)

    @staticmethod
    def _remove_parameter_nodes(
        emitter: ParticleEmitterAsset, parameter_id: str
    ) -> ParticleEmitterAsset:
        updates = {}
        for stage in _STAGES:
            document = getattr(emitter, stage)
            if document is None:
                continue
            node_ids = {
                node.uid
                for node in document.nodes
                if node.type_id in {"particle.parameter", "particle.parameter.set"}
                and node.properties.get("parameter") == parameter_id
            }
            if node_ids:
                updates[stage] = replace(
                    document,
                    nodes=tuple(node for node in document.nodes if node.uid not in node_ids),
                    links=tuple(
                        link
                        for link in document.links
                        if link.source_node not in node_ids and link.target_node not in node_ids
                    ),
                )
        return replace(emitter, **updates) if updates else emitter

    @staticmethod
    def _remove_parameter_store_nodes(
        emitter: ParticleEmitterAsset, parameter_id: str
    ) -> ParticleEmitterAsset:
        updates = {}
        for stage in _STAGES:
            document = getattr(emitter, stage)
            if document is None:
                continue
            node_ids = {
                node.uid
                for node in document.nodes
                if node.type_id == "particle.parameter.set"
                and node.properties.get("parameter") == parameter_id
            }
            if node_ids:
                updates[stage] = replace(
                    document,
                    nodes=tuple(
                        node for node in document.nodes if node.uid not in node_ids
                    ),
                    links=tuple(
                        link
                        for link in document.links
                        if link.source_node not in node_ids
                        and link.target_node not in node_ids
                    ),
                )
        return replace(emitter, **updates) if updates else emitter

    @staticmethod
    def _decode_event_fields(
        fields: list[dict], *, require_stable_ids: bool
    ) -> tuple[ParticleEventField, ...]:
        decoded_fields = []
        expected = {"stable_id", "name", "type", "default"} if require_stable_ids else {
            "name",
            "type",
            "default",
        }
        for index, field in enumerate(fields):
            if type(field) is not dict or set(field) != expected:
                required = ", ".join(sorted(expected))
                raise ValueError(f"event field {index} requires {required}")
            field_name = str(field["name"]).strip()
            if not field_name:
                raise ValueError(f"event field {index} name cannot be empty")
            stable_id = str(field.get("stable_id", "")).strip()
            if not stable_id:
                stable_id = uuid.uuid4().hex
            decoded_fields.append(
                ParticleEventField(
                    stable_id,
                    field_name,
                    TypeRef.from_dict(field["type"]),
                    copy.deepcopy(field["default"]),
                )
            )
        if len({field.stable_id for field in decoded_fields}) != len(decoded_fields):
            raise ValueError("event field stable IDs must be unique")
        return tuple(decoded_fields)

    def set_authoring_emitter_settings(
        self, emitter_id: str, settings: dict
    ) -> dict:
        """Replace the complete current EmitterSettings schema atomically."""
        emitter_id = str(emitter_id)
        index = next(
            (
                index
                for index, emitter in enumerate(self._asset.emitters)
                if emitter.stable_id == emitter_id
            ),
            -1,
        )
        if index < 0:
            raise KeyError(f"Particle emitter not found: {emitter_id!r}")
        decoded = EmitterSettings.from_dict(settings, "$.settings")
        emitter = self._asset.emitters[index]
        if (
            decoded.shape.kind is EmitterShapeKind.SDF
            and emitter.settings.shape.kind is not EmitterShapeKind.SDF
        ):
            raise ValueError("SDF authoring is not available in this release")
        if decoded == emitter.settings:
            return {
                "stable_id": emitter_id,
                "settings": emitter.settings.to_dict(),
                "changed": False,
            }
        selected = GraphElementRef(GraphElementKind.EMITTER, emitter_id)
        if not self._execute_graph_mutations(
            "Edit emitter settings",
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    selected,
                    before={"settings": emitter.settings.to_dict()},
                    after={"settings": decoded.to_dict()},
                ),
            ),
            merge_key=f"particle_emitter_settings:{emitter_id}",
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle emitter settings could not be updated")
        return {
            "stable_id": emitter_id,
            "settings": decoded.to_dict(),
            "changed": True,
        }

    def patch_authoring_emitter_settings(
        self, emitter_id: str, values: dict
    ) -> dict:
        """Patch current emitter settings through the same strict decoder."""
        if type(values) is not dict or not values:
            raise ValueError("emitter settings patch must be a non-empty object")
        emitter_id = str(emitter_id)
        emitter = next(
            (
                value
                for value in self._asset.emitters
                if value.stable_id == emitter_id
            ),
            None,
        )
        if emitter is None:
            raise KeyError(f"Particle emitter not found: {emitter_id!r}")
        settings = emitter.settings.to_dict()
        unknown = sorted(set(values) - set(settings))
        if unknown:
            raise ValueError(f"unknown emitter settings: {unknown}")
        for key, value in values.items():
            settings[key] = copy.deepcopy(value)
        return self.set_authoring_emitter_settings(emitter_id, settings)

    def add_event_type(
        self,
        name: str,
        queue_capacity: int,
        fields: list[dict],
    ) -> dict:
        """Add one typed event schema through the live document and Undo stack."""
        self._sync_model_to_asset()
        decoded_fields = self._decode_event_fields(
            fields, require_stable_ids=False
        )
        event_type = ParticleEventType(
            uuid.uuid4().hex,
            str(name).strip(),
            int(queue_capacity),
            decoded_fields,
        )
        selected = GraphElementRef(
            GraphElementKind.EVENT_TYPE, event_type.stable_id
        )
        if not self._execute_graph_mutations(
            "Add Particle Graph event type",
            (
                GraphMutation(
                    GraphMutationKind.INSERT,
                    selected,
                    after=event_type.to_dict(),
                    after_index=len(self._asset.event_types),
                ),
            ),
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle Graph event type could not be created")
        return event_type.to_dict()

    def add_authoring_event_flow(
        self,
        event_type_id: str,
        x: float | None = None,
        y: float | None = None,
    ) -> dict:
        """Create one empty Active Event flow on the active emitter."""
        self._sync_model_to_asset()
        event_type_id = str(event_type_id)
        if (x is None) != (y is None):
            raise ValueError("Particle event root position requires both x and y")
        if x is not None and (
            not math.isfinite(float(x)) or not math.isfinite(float(y))
        ):
            raise ValueError("Particle event root position must be finite")
        event_type = next(
            (
                value
                for value in self._asset.event_types
                if value.stable_id == event_type_id
            ),
            None,
        )
        if event_type is None:
            raise KeyError(f"Particle event not found: {event_type_id!r}")
        emitter = self._selected_emitter()
        flow_id = uuid.uuid4().hex
        stage = f"event.{flow_id}"
        graph = default_event_graph(event_type_id)
        if x is not None:
            event_origin_y = ParticleEmitterGraphAuthoringModel.event_stage_canvas_origin(
                len(emitter.event_flows)
            )
            graph = replace(
                graph,
                nodes=tuple(
                    replace(
                        node,
                        position=(float(x), float(y) - event_origin_y),
                    )
                    if node.uid == "root.event"
                    else node
                    for node in graph.nodes
                ),
            )
        flow = ParticleEventFlow(flow_id, graph)
        root_uid = f"{stage}::root.event"
        selected = GraphElementRef(
            GraphElementKind.NODE,
            self._particle_element_id(emitter.stable_id, root_uid),
        )
        flow_ref = GraphElementRef(
            GraphElementKind.EVENT_FLOW,
            self._particle_element_id(emitter.stable_id, flow_id),
        )
        self._stage = stage
        if not self._execute_graph_mutations(
            "Add Particle Graph Active Event",
            (
                GraphMutation(
                    GraphMutationKind.INSERT,
                    flow_ref,
                    after=flow.to_dict(),
                    after_index=len(emitter.event_flows),
                ),
            ),
            selection_after=(selected,),
        ):
            self._stage = "init"
            raise RuntimeError("Particle Graph Active Event flow could not be created")
        return {"event_id": event_type_id, "flow_id": flow_id, "created": True}

    def _rebuild_event_dependencies(
        self,
        event_asset: ParticleGraphAsset,
        event_id: str,
        *,
        clear_event: bool = False,
    ) -> tuple[ParticleEmitterAsset, ...]:
        """Re-resolve every Active/Trigger node that uses one event schema."""
        old_definitions = particle_graph_node_definitions(self._asset)
        new_definitions = particle_graph_node_definitions(event_asset)
        rebuilt_emitters = []
        for emitter in self._asset.emitters:
            model = ParticleEmitterGraphAuthoringModel(
                emitter,
                definition_set=old_definitions,
            )
            affected = [
                node.uid
                for node in model.nodes
                if node.type_id in {
                    PARTICLE_EVENT_ACTIVE_TYPE_ID,
                    PARTICLE_EVENT_TRIGGER_TYPE_ID,
                }
                and str(node.data.get("event", "")) == event_id
            ]
            if affected:
                model.set_definition_set_resolver(
                    lambda _model, _node, definitions=new_definitions: definitions
                )
                model.rebuild_nodes(
                    (
                        {node_uid: {"event": ""} for node_uid in affected}
                        if clear_event
                        else {}
                    ),
                    affected_node_uids=affected,
                )
                emitter = self._emitter_with_model_documents(emitter, model)
            rebuilt_emitters.append(emitter)
        return tuple(rebuilt_emitters)

    def update_event_type(
        self,
        event_type_id: str,
        name: str,
        queue_capacity: int,
        fields: list[dict],
    ) -> dict:
        """Update one event schema without changing its stable identity."""
        self._sync_model_to_asset()
        event_type_id = str(event_type_id)
        index = next(
            (
                index
                for index, value in enumerate(self._asset.event_types)
                if value.stable_id == event_type_id
            ),
            -1,
        )
        if index < 0:
            raise KeyError(f"Particle event type not found: {event_type_id!r}")
        previous = self._asset.event_types[index]
        decoded_fields = self._decode_event_fields(fields, require_stable_ids=True)
        updated = ParticleEventType(
            previous.stable_id,
            str(name).strip(),
            int(queue_capacity),
            decoded_fields,
        )
        if updated == previous:
            return {**updated.to_dict(), "changed": False}

        event_types = list(self._asset.event_types)
        event_types[index] = updated
        event_asset = replace(
            self._asset,
            event_types=tuple(event_types),
        )
        emitters = self._rebuild_event_dependencies(
            event_asset,
            previous.stable_id,
        )
        before_asset = self._asset
        after_asset = replace(
            self._asset, event_types=tuple(event_types), emitters=emitters
        )
        graph_mutations = self._graph_mutations_between(before_asset, after_asset)
        selected = GraphElementRef(GraphElementKind.EVENT_TYPE, event_type_id)
        if not self._execute_graph_mutations(
            "Edit Particle Graph event type",
            (
                *graph_mutations,
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    selected,
                    before=previous.to_dict(),
                    after=updated.to_dict(),
                    before_index=index,
                    after_index=index,
                ),
            ),
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle Graph event type could not be updated")
        return {**updated.to_dict(), "changed": True}

    def remove_event_type(self, event_type_id: str) -> dict:
        """Remove an event schema while preserving every reusable event node."""
        self._sync_model_to_asset()
        event_type = next(
            (
                value
                for value in self._asset.event_types
                if value.stable_id == str(event_type_id)
            ),
            None,
        )
        if event_type is None:
            raise KeyError(f"Particle event type not found: {event_type_id!r}")
        before_asset = self._asset
        remaining_event_types = tuple(
            value
            for value in self._asset.event_types
            if value.stable_id != event_type.stable_id
        )
        # Keep a field-less schema only while rebuilding nodes. The strict
        # asset cannot temporarily reference a removed event from an Active
        # Event flow, so nodes are first migrated to event="" and the event is
        # removed from the final asset immediately afterwards.
        rebuild_event_types = tuple(
            ParticleEventType(
                value.stable_id,
                value.name,
                value.queue_capacity,
                (),
            )
            if value.stable_id == event_type.stable_id
            else value
            for value in self._asset.event_types
        )
        event_asset = replace(self._asset, event_types=rebuild_event_types)
        emitters = self._rebuild_event_dependencies(
            event_asset,
            event_type.stable_id,
            clear_event=True,
        )
        after_asset = replace(
            self._asset,
            emitters=tuple(emitters),
            event_types=remaining_event_types,
        )
        graph_mutations = self._graph_mutations_between(before_asset, after_asset)
        if not self._execute_graph_mutations(
            "Remove Particle Graph event type",
            (
                *graph_mutations,
                GraphMutation(
                    GraphMutationKind.REMOVE,
                    GraphElementRef(
                        GraphElementKind.EVENT_TYPE, event_type.stable_id
                    ),
                    before=event_type.to_dict(),
                    before_index=self._asset.event_types.index(event_type),
                ),
            ),
            selection_after=(),
        ):
            raise RuntimeError("Particle Graph event type could not be removed")
        return event_type.to_dict()

    def set_rendering_output(self, node_uid: str) -> dict:
        """Route the Rendering root Exec output through the authoring model."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        node = self._model.find_node(str(node_uid))
        if node is None:
            raise KeyError(f"Particle Graph node not found: {node_uid!r}")
        if (
            self._model.stage_for_uid(node.uid) != "rendering"
            or not str(node.type_id).startswith("particle.output.")
        ):
            raise ValueError(
                f"Particle Graph node {node_uid!r} is not a Rendering output"
            )

        root_uid = "rendering::root.rendering"
        output_links = [
            link
            for link in self._model.links
            if link.source_node == root_uid and link.source_pin == "out"
        ]
        if (
            len(output_links) == 1
            and output_links[0].target_node == node.uid
            and output_links[0].target_pin == "in"
        ):
            return {
                "node_uid": node.uid,
                "link_uid": output_links[0].uid,
                "changed": False,
            }

        before = self._model.capture_authoring_state()
        for link in output_links:
            self._model.remove_link(link.uid)
        created = self._model.add_link(root_uid, "out", node.uid, "in")
        if created is None:
            self._model.apply_authoring_mutations(
                NodeGraph.invert_authoring_mutations(
                    NodeGraph.diff_authoring_states(
                        before,
                        self._model.capture_authoring_state(),
                    )
                )
            )
            raise RuntimeError(
                f"Particle Graph could not route Rendering to output {node_uid!r}"
            )
        self._select_stage("rendering")
        selected = self._graph_element_from_view(GraphElementKind.NODE, node.uid)
        if not self._commit_node_graph_change(
            "Set Particle Graph rendering output",
            before,
            selection_after=(selected,),
        ):
            raise RuntimeError("Particle Graph Rendering output could not be changed")
        return {"node_uid": node.uid, "link_uid": created.uid, "changed": True}

    def reload_from_disk(self) -> bool:
        """Reload the current document after it has been saved cleanly."""
        document = self._particle_document()
        if document is not None and document.is_dirty:
            raise RuntimeError("Particle Graph must be saved before it can be reloaded")
        if not self._file_path:
            raise RuntimeError("Particle Graph has no source file to reload")
        reloaded = self.open_document_resource_immediate(self._file_path)
        if reloaded:
            self._persist_panel_state()
        return reloaded

    def discard_unsaved_changes(self) -> dict:
        """Explicitly discard the live document through the Editor contract."""
        from Infernux.engine.interaction import (
            DocumentActionStatus,
            DocumentRegistry,
        )

        previous_path = self._file_path
        document = self._particle_document()
        was_dirty = bool(document and document.is_dirty)
        if was_dirty:
            result = DocumentRegistry.instance().request_discard(document.document_id)
            if result.status not in {
                DocumentActionStatus.APPLIED,
                DocumentActionStatus.NO_OP,
            }:
                raise RuntimeError(
                    result.message or "Particle Graph could not discard unsaved changes"
                )
        document = self._particle_document()
        return {
            "discarded": bool(was_dirty),
            "previous_file_path": previous_path,
            "file_path": self._file_path,
            "dirty": bool(document and document.is_dirty),
        }

    def _selected_emitter(self) -> ParticleEmitterAsset:
        return self._asset.emitters[self._emitter_index]

    def _definition_for_type(self, type_id: str):
        if self._model is not None:
            return self._model.definition_for_type(type_id)
        return COMMON_NODE_REGISTRY.get(type_id)

    def _definition_for_node(self, node):
        if self._model is not None:
            return self._model.definition_for_node(node)
        return COMMON_NODE_REGISTRY.get(node.type_id) if node is not None else None

    def _replace_emitter(self, emitter: ParticleEmitterAsset) -> None:
        emitters = list(self._asset.emitters)
        emitters[self._emitter_index] = emitter
        self._asset = replace(self._asset, emitters=tuple(emitters))

    def _bind_stage(self) -> None:
        self._model = ParticleEmitterGraphAuthoringModel(
            self._selected_emitter(),
            definition_set=particle_graph_node_definitions(self._asset),
        )
        self._model.set_definition_set_resolver(
            self._resolve_dynamic_particle_definitions
        )
        self._model.set_authoring_stage(self._stage)
        self._bind_node_graph_model(self._model)

    def _sync_model_to_asset(self) -> None:
        if self._model is None:
            return
        emitter = self._selected_emitter()
        documents = self._model.to_documents()
        updates = {
            stage: documents[stage]
            for stage in _STAGES
            if stage in documents and getattr(emitter, stage) != documents[stage]
        }
        event_flows = tuple(
            replace(flow, graph=documents.get(f"event.{flow.stable_id}", flow.graph))
            for flow in emitter.event_flows
        )
        if event_flows != emitter.event_flows:
            updates["event_flows"] = event_flows
        if updates:
            self._replace_emitter(replace(emitter, **updates))

    def _select_stage(self, stage: str) -> None:
        valid = set(_STAGES) | {
            f"event.{flow.stable_id}" for flow in self._selected_emitter().event_flows
        }
        if stage not in valid or stage == self._stage:
            return
        self._stage = stage
        if self._model is not None:
            self._model.set_authoring_stage(stage)

    def _select_emitter(self, index: int) -> None:
        if not 0 <= index < len(self._asset.emitters):
            return
        target_id = self._asset.emitters[index].stable_id
        if index != self._emitter_index:
            self._sync_model_to_asset()
        self._select_particle_element(
            GraphElementKind.EMITTER,
            target_id,
            reason="particle_graph_emitter_selected",
            record_history=True,
        )
        # SelectionService is authoritative. Its projection normally performs
        # the rebind synchronously; retain this fallback for an already-current
        # global selection projected into a newly opened panel.
        if index != self._emitter_index:
            self._emitter_index = index
            self._bind_stage()

    def _select_event_type(self, event_type_id: str, *, focus_name: bool = False) -> None:
        if not any(
            item.stable_id == str(event_type_id) for item in self._asset.event_types
        ):
            return
        self._select_particle_element(
            GraphElementKind.EVENT_TYPE,
            str(event_type_id),
            reason="particle_graph_event_type_selected",
            record_history=True,
        )
        if focus_name:
            self._focus_detail_name = "event_type"

    def _refresh_shader_definitions_if_needed(self) -> None:
        generation = get_shader_property_generation()
        if generation == self._shader_definition_generation:
            return
        self._sync_model_to_asset()
        self._shader_definition_generation = generation
        self._bind_stage()

    def open_document_resource_immediate(self, file_path: str) -> bool:
        try:
            asset = ParticleGraphAsset.load(file_path)
        except (OSError, json.JSONDecodeError, ParticleGraphSchemaError, ValueError) as exc:
            Debug.log_error(f"Failed to open Particle Graph '{file_path}': {exc}")
            return False
        self._asset = asset
        self._file_path = resolved_path(file_path)
        self._emitter_index = 0
        self._stage = "init"
        self._draft_compile_error = ""
        self._bind_stage()
        self._replace_particle_document(resource_path=self._file_path, dirty=False)
        self._graph_selection.clear(record_history=False)
        return True

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del guid
        if document_id != self.document_id:
            return
        if self._file_path and same_path(self._file_path, source_path):
            self._file_path = resolved_path(destination_path)
            self._persist_panel_state()

    def _save_to(self, file_path: str, *, ticket_id: str = "") -> bool:
        active_ticket_id = ticket_id or self._pending_save_ticket_id
        if not active_ticket_id:
            raise RuntimeError(
                "Particle Graph saves require a DocumentRegistry SaveTicket"
            )
        result = self._authoring_document_controller.continue_save_to_resource(
            active_ticket_id,
            file_path,
        )
        if result.accepted:
            self._pending_save_ticket_id = ""
        return result.accepted

    def _show_save_as_dialog(self) -> bool:
        safe_name = (self._asset.name or "ParticleGraph").replace(" ", "_")
        if not self._save_as_dialog.request(
            title="Save Particle Graph",
            extension="particlegraph",
            default_name=safe_name,
            current_path=self._file_path,
            save_callback=self._save_to,
            cancel_callback=self._cancel_pending_save,
        ):
            Debug.log_warning(
                "[ParticleGraphEditor] No project root set - cannot save Particle Graph."
            )
            return False
        return True

    def _do_save(self) -> bool:
        return self._request_document_save(save_as=False)

    def _request_document_save(self, *, save_as: bool) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        document = self._particle_document()
        if document is None:
            return False
        return DocumentRegistry.instance().request_save(
            document.document_id,
            save_as=save_as,
        ).accepted

    def request_authoring_save_as(self, ticket):
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
        )

        self._pending_save_ticket_id = ticket.ticket_id
        if self._show_save_as_dialog():
            return DocumentActionResult(DocumentActionStatus.PENDING)
        self._pending_save_ticket_id = ""
        return DocumentActionResult(
            DocumentActionStatus.REJECTED,
            "no project root is available",
        )

    def capture_authoring_save_snapshot(self, target: str):
        from Infernux.engine.interaction import (
            AuthoringAssetSnapshot,
            document_content_token,
        )

        self._sync_model_to_asset()
        normalized = resolved_path(target)
        if not normalized:
            raise ValueError("Particle Graph save target is invalid")
        title = os.path.splitext(os.path.basename(normalized))[0]
        document = copy.deepcopy(self._asset.to_dict())
        document["name"] = title
        asset = ParticleGraphAsset.from_dict(document)
        from Infernux.core.asset_types import read_meta_guid

        prepared = ParticleArtifactRegistry.prepare_graph_asset(
            asset, normalized, guid=read_meta_guid(normalized)
        )
        return AuthoringAssetSnapshot(
            normalized,
            prepared.source_text,
            document_content_token(asset.to_dict()),
            title,
            prepared,
        )

    def publish_authoring_save_snapshot(self, snapshot) -> str:
        prepared = snapshot.payload
        if not isinstance(prepared, PreparedParticleGraphArtifact):
            raise TypeError("Particle Graph save snapshot has no prepared AOT artifact")

        ParticleArtifactRegistry.publish_prepared_graph(prepared)
        self._asset = replace(self._asset, name=snapshot.title)
        self._file_path = snapshot.target_path
        self._draft_compile_error = ""
        self._persist_panel_state()

        publication_error = ""
        try:
            from Infernux.core.assets import AssetManager

            result = AssetManager.reimport_asset(snapshot.target_path)
            if not result:
                result = AssetManager.import_asset(snapshot.target_path)
            if not result:
                publication_error = str(
                    getattr(result, "error", "")
                    or f"Particle Graph publication failed: {snapshot.target_path}"
                )
        except Exception as exc:
            publication_error = str(exc)
        return publication_error

    def current_authoring_content_token(self) -> str:
        from Infernux.engine.interaction import document_content_token

        self._sync_model_to_asset()
        return document_content_token(self._asset.to_dict())

    def discard(self, *, document_id: str) -> bool:
        if document_id != self.document_id:
            return False
        return self._discard_unsaved_changes()

    def _cancel_pending_save(self) -> None:
        ticket_id = self._pending_save_ticket_id
        self._pending_save_ticket_id = ""
        if not ticket_id:
            return
        from Infernux.engine.interaction import DocumentRegistry

        DocumentRegistry.instance().complete_save(
            ticket_id,
            success=False,
            cancelled=True,
            message="save was cancelled",
        )

    def _discard_unsaved_changes(self) -> bool:
        if self._file_path:
            try:
                asset = ParticleGraphAsset.load(self._file_path)
            except (
                OSError,
                json.JSONDecodeError,
                ParticleGraphSchemaError,
                ValueError,
            ) as exc:
                Debug.log_error(
                    f"Failed to discard Particle Graph '{self._file_path}': {exc}"
                )
                return False
            self._asset = asset
            self._emitter_index = 0
            self._stage = "init"
            self._draft_compile_error = ""
            self._bind_stage()
            self._graph_selection.clear(record_history=False)
            self._persist_panel_state()
            return True
        self._asset = ParticleGraphAsset()
        self._emitter_index = 0
        self._stage = "init"
        self._draft_compile_error = ""
        self._bind_stage()
        # The registry commits revision rollback after this model restore.
        self._graph_selection.clear(record_history=False)
        self._persist_panel_state()
        return True

    def _snapshot(self) -> dict:
        self._sync_model_to_asset()
        return {
            "asset": copy.deepcopy(self._asset.to_dict()),
            "emitter_index": self._emitter_index,
            "stage": self._stage,
            "selected_parameter_id": self._selected_parameter_id,
            "document_revision": (
                self._particle_document().revision
                if self._particle_document() is not None
                else 0
            ),
        }

    def _on_node_graph_selection_accepted(
        self,
        node_ids: tuple[str, ...],
        _link_id: str,
    ) -> None:
        node_uid = node_ids[-1] if node_ids else ""
        if self._model is not None and node_uid:
            stage = self._model.stage_for_uid(node_uid)
            if stage:
                self._select_stage(stage)

    def _on_canvas_drop(self, payload_type, payload, x: float, y: float) -> None:
        payload_type = str(payload_type)
        if not isinstance(payload, str):
            return
        try:
            if payload_type == _PARAMETER_DRAG_PAYLOAD:
                self.add_authoring_parameter_node(payload, float(x), float(y))
            elif payload_type == _EVENT_DRAG_PAYLOAD:
                self.add_authoring_event_flow(payload, float(x), float(y))
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            Debug.log_warning(f"Particle workspace drop rejected: {exc}")

    def _on_node_creation_requested(self, request: dict) -> None:
        if self._model is None:
            return
        stage = self._model.stage_for_uid(str(request.get("source_node", "")))
        if not stage:
            stage = self._model.stage_nearest_y(float(request.get("gy", 0.0)))
        self._stage = stage
        self._model.set_authoring_stage(stage)
        self._model.prepare_node_creation(stage)

    def _node_creation_entries(self, request: dict):
        if self._model is None:
            return ()
        entries = []
        collision_roots = {
            "particle.root.collision_enter",
            "particle.root.collision_stay",
            "particle.root.collision_exit",
        }
        for definition in self._model.registered_types():
            if definition.type_id in self._HIDDEN_INTERNAL_RESOURCE_NODE_TYPES:
                continue
            if request.get("source_node") and self._view._compatible_pin_for_type(
                definition, request
            ) is None:
                continue
            enabled, reason = self._model.node_creation_state(definition.type_id)
            label = definition.label
            if not enabled and definition.type_id in collision_roots:
                label = f"{label} ({t('particle_graph_editor.unavailable')})"
            entries.append(
                NodeCreationEntry(
                    key=definition.type_id,
                    label=label,
                    category=(
                        t("particle_graph_editor.collision_lifecycle")
                        if definition.type_id in collision_roots
                        else definition.category_label
                        or definition.type_id.split(".", 1)[0].upper()
                    ),
                    type_id=definition.type_id,
                    enabled=enabled,
                    disabled_reason=reason,
                )
            )
        return entries

    def _on_node_add(self, type_id: str, x: float, y: float):
        if self._model is None or self._model.get_type(type_id) is None:
            return
        before = self._model.capture_authoring_state()
        try:
            node = self._model.add_node(type_id, x, y)
        except ValueError as exc:
            Debug.log_warning(f"Particle Graph node creation rejected: {exc}")
            return
        self._stage = self._model.stage_for_uid(node.uid) or self._stage
        node_uid = node.uid
        selected = self._graph_element_from_view(GraphElementKind.NODE, node_uid)
        if not self._commit_node_graph_change(
            "Add Particle Graph node",
            before,
            selection_after=(selected,),
        ):
            return None
        return self._model.find_node(node_uid) if self._model is not None else None

    def _on_node_data_changed(self, node_uid: str, key: str, old_value, new_value) -> None:
        if self._model is None:
            return
        node = self._model.find_node(node_uid)
        if node is None or old_value == new_value:
            return
        rebuilt = self._model.rebuild_node(node.uid, {key: new_value})
        selected = self._graph_element_from_view(GraphElementKind.NODE, node.uid)
        self._commit_node_graph_change(
            f"Edit Particle Graph {key}",
            rebuilt.before,
            merge_key=f"particle_node:{selected.stable_id}:{key}",
            selection_after=(selected,),
        )
        if rebuilt.removed_link_ids:
            Debug.log_warning(
                "Particle Graph disconnected "
                f"{len(rebuilt.removed_link_ids)} incompatible link(s) "
                "after the node definition changed"
            )

    def _on_nodes_deleted(self, node_uids) -> None:
        if self._model is None:
            return
        before = self._model.capture_authoring_state()
        changed = False
        for uid in node_uids:
            changed = self._model.remove_node(uid) or changed
        if changed:
            selected = set(self._graph_selection.selected_ids(GraphElementKind.NODE))
            deleted = {
                self._particle_element_id(self._selected_emitter().stable_id, str(uid))
                for uid in node_uids
            }
            self._commit_node_graph_change(
                "Delete Particle Graph node" if len(node_uids) == 1 else "Delete Particle Graph nodes",
                before,
                selection_after=() if selected & deleted else None,
            )

    def _on_link_created(self, src_node, src_pin, dst_node, dst_pin) -> None:
        if self._model is None:
            return
        before = self._model.capture_authoring_state()
        if self._model.add_link(src_node, src_pin, dst_node, dst_pin) is not None:
            self._commit_node_graph_change(
                "Connect Particle Graph nodes",
                before,
            )

    def _on_link_deleted(self, link_uid: str) -> None:
        if self._model is None:
            return
        before = self._model.capture_authoring_state()
        if self._model.remove_link(link_uid):
            self._commit_node_graph_change(
                "Disconnect Particle Graph nodes",
                before,
            )

    def _on_link_replaced(
        self, link_uid: str, src_node: str, src_pin: str, dst_node: str, dst_pin: str
    ) -> None:
        if self._model is None:
            return
        before = self._model.capture_authoring_state()
        if self._model.replace_link(
            link_uid, src_node, src_pin, dst_node, dst_pin
        ) is not None:
            self._commit_node_graph_change(
                "Replace Particle Graph connection",
                before,
            )

    def _node_graph_drag_description(self, _stable_id: str) -> str:
        return "Move Particle Graph node"

    def _node_graph_drag_merge_key(self, _stable_id: str) -> str:
        return "particle_node:selection:position"

    def _add_emitter(self) -> None:
        names = {emitter.name for emitter in self._asset.emitters}
        index = len(self._asset.emitters) + 1
        name = f"Emitter {index}"
        while name in names:
            index += 1
            name = f"Emitter {index}"
        self.add_authoring_emitter(name)

    def _remove_selected_emitter(self) -> None:
        if len(self._asset.emitters) <= 1:
            return
        self.remove_authoring_emitter(self._selected_emitter().stable_id)

    def _update_emitter(self, emitter: ParticleEmitterAsset, description: str) -> None:
        current = self._selected_emitter()
        if emitter == current:
            return
        if emitter.stable_id != current.stable_id:
            raise ValueError("Particle emitter update cannot change stable identity")
        if emitter == replace(current, name=emitter.name):
            before_payload = {"name": current.name}
            after_payload = {"name": emitter.name}
        elif emitter == replace(current, settings=emitter.settings):
            before_payload = {"settings": current.settings.to_dict()}
            after_payload = {"settings": emitter.settings.to_dict()}
        else:
            raise RuntimeError("Particle emitter update must target one authoring field group")
        self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.EMITTER, emitter.stable_id),
                    before=before_payload,
                    after=after_payload,
                ),
            ),
            merge_key=f"particle_emitter:{emitter.stable_id}:{next(iter(after_payload))}",
        )

    def _update_settings(self, settings: EmitterSettings) -> None:
        emitter = self._selected_emitter()
        self._update_emitter(replace(emitter, settings=settings), "Edit emitter settings")
        if self._model is not None:
            self._model.set_collision_enabled(settings.collision_enabled)

    def _initial_size(self):
        return (1120, 700)

    def _empty_state_drop_types(self):
        return ["PARTICLE_GRAPH_FILE"]

    def _on_empty_state_drop(self, payload_type, payload):
        if payload_type == "PARTICLE_GRAPH_FILE" and payload:
            from Infernux.engine.interaction import DocumentKind

            self.request_document_resource_open(DocumentKind.PARTICLE_GRAPH, payload)

    def save_state(self) -> dict:
        return super().save_state()

    def load_state(self, data: dict) -> None:
        super().load_state(data)
        self._emitter_index = min(
            max(0, self._emitter_index), len(self._asset.emitters) - 1
        )
        if self._stage not in _STAGES:
            raise ValueError(f"invalid Particle Graph stage View State: {self._stage}")
        self._workspace_tab_index = max(0, min(self._workspace_tab_index, 3))
        self._bind_stage()
        self._graph_selection.refresh()

    def _record_document_semantics(self, ctx: InxGUIContext) -> None:
        if not bool(getattr(ctx, "semantic_capture_enabled", True)):
            return
        document = self._particle_document()
        document_title = document.title if document is not None else self._asset.name
        ctx.record_semantic_item(
            "status", document_title, False, "particle_graph.document.name",
            string_value=document_title,
        )
        ctx.record_semantic_item(
            "status", "Particle Graph Asset Path", False, "particle_graph.document.path",
            string_value=self._file_path,
        )
        ctx.record_semantic_item(
            "status", "Unsaved Changes", False, "particle_graph.document.dirty",
            bool_value=self._document_is_dirty(),
        )
        ctx.record_semantic_item(
            "status",
            "Draft Compile Error",
            False,
            "particle_graph.document.compile_error",
            string_value=self._draft_compile_error,
        )

    @staticmethod
    def _render_event_field_default(
        ctx: InxGUIContext, field_index: int, value_type: ValueType, value
    ):
        label = t("particle_graph_editor.event_default")
        suffix = f"##particle_event_field_{field_index}_default"
        if value_type is ValueType.BOOL:
            return bool(ctx.checkbox(f"{label}{suffix}", bool(value)))
        if value_type is ValueType.I32:
            return int(ctx.input_int(f"{label}{suffix}", int(value)))
        if value_type is ValueType.U32:
            return max(0, int(ctx.input_uint(f"{label}{suffix}", int(value))))
        if value_type is ValueType.F32:
            return float(ctx.drag_float(f"{label}{suffix}", float(value), 0.05, -1.0e7, 1.0e7))
        if value_type is ValueType.COLOR:
            ctx.label(label)
            return render_color_value_bar(
                ctx,
                suffix,
                value,
                allow_hdr=True,
                default_hdr_enabled=True,
            )
        return [
            float(
                ctx.drag_float(
                    f"{label} {axis}##particle_event_field_{field_index}_default_{axis}",
                    float(component),
                    0.05,
                    -1.0e7,
                    1.0e7,
                )
            )
            for axis, component in zip("XYZW", value)
        ]

    def _add_default_event_type(self) -> None:
        existing = {item.name for item in self._asset.event_types}
        index = len(existing) + 1
        name = "Event" if "Event" not in existing else f"Event {index}"
        while name in existing:
            index += 1
            name = f"Event {index}"
        created = self.add_event_type(name, 32, [])
        self._select_event_type(created["stable_id"], focus_name=True)

    def _node_graph_workspace_add(
        self, section_id: str, action_id: str
    ) -> bool:
        if section_id == "particle_emitter" and action_id == "default":
            self._add_emitter()
            return True
        if section_id == "particle_event_type" and action_id == "default":
            self._add_default_event_type()
            return True
        if section_id == "particle_parameter":
            value_type = ValueType(action_id)
            if value_type not in _PARAMETER_CREATE_TYPES:
                raise ValueError(
                    f"Unsupported Particle parameter type: {action_id!r}"
                )
            self.add_authoring_parameter(
                self._next_parameter_name(value_type), value_type.value
            )
            return True
        return super()._node_graph_workspace_add(section_id, action_id)

    def _node_graph_workspace_rename(
        self, element: GraphElementRef, name: str
    ) -> bool:
        name = str(name).strip()
        if not name:
            return False
        if element.kind is GraphElementKind.PARAMETER:
            self.update_authoring_parameter(element.stable_id, {"name": name})
            return True
        if element.kind is GraphElementKind.EVENT_TYPE:
            event_type = next(
                (
                    item
                    for item in self._asset.event_types
                    if item.stable_id == element.stable_id
                ),
                None,
            )
            if event_type is None:
                raise KeyError(f"Particle event type not found: {element.stable_id!r}")
            self.update_event_type(
                element.stable_id,
                name,
                event_type.queue_capacity,
                [field.to_dict() for field in event_type.fields],
            )
            return True
        if element.kind is GraphElementKind.EMITTER:
            index = self._emitter_index_for_id(element.stable_id)
            if index != self._emitter_index:
                self._select_emitter(index)
            self._update_emitter(
                replace(self._selected_emitter(), name=name),
                "Rename particle emitter",
            )
            return True
        return super()._node_graph_workspace_rename(element, name)

    def _node_graph_workspace_delete(self, element: GraphElementRef) -> bool:
        if element.kind is GraphElementKind.PARAMETER:
            self.remove_authoring_parameter(element.stable_id)
            return True
        if element.kind is GraphElementKind.EVENT_TYPE:
            self.remove_event_type(element.stable_id)
            return True
        if element.kind is GraphElementKind.EMITTER:
            self.remove_authoring_emitter(element.stable_id)
            return True
        return super()._node_graph_workspace_delete(element)

    def _render_event_page(self, ctx: InxGUIContext) -> None:
        entries = tuple(
            GraphWorkspaceEntry(
                GraphElementRef(GraphElementKind.EVENT_TYPE, event_type.stable_id),
                event_type.name,
                str(event_type.queue_capacity),
                _WORKSPACE_EVENT_TYPE,
                semantic_kind="particle_event_type",
                semantic_id=(
                    f"particle_graph.event_type.{event_type.stable_id}"
                ),
                semantic_numeric_value=float(event_type.queue_capacity),
                can_rename=True,
                can_delete=True,
                drag=GraphWorkspaceDrag(
                    _EVENT_DRAG_PAYLOAD,
                    event_type.stable_id,
                    event_type.name,
                ),
            )
            for event_type in self._asset.event_types
        )
        self._render_graph_workspace_section(
            ctx,
            GraphWorkspaceSection(
                t("particle_graph_editor.event_types"),
                "particle_event_type",
                entries,
                add_actions=(GraphWorkspaceAddAction("default", "Event"),),
                add_semantic_id="particle_graph.workspace.events.add",
                rename_label=t("particle_graph_editor.rename"),
                delete_label=t("particle_graph_editor.delete"),
            ),
        )

    def _next_parameter_name(self, value_type: ValueType) -> str:
        base = t(f"particle_graph_editor.type_{value_type.value}")
        existing = {parameter.name for parameter in self._asset.parameters}
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def _render_parameter_page(self, ctx: InxGUIContext) -> None:
        entries = tuple(
            GraphWorkspaceEntry(
                GraphElementRef(GraphElementKind.PARAMETER, parameter.stable_id),
                parameter.name,
                t(
                    f"particle_graph_editor.type_{parameter.value_type.value_type.value}"
                ),
                _PARAMETER_TYPE_COLORS[parameter.value_type.value_type],
                semantic_kind="particle_parameter",
                semantic_id=f"particle_graph.parameter.{parameter.stable_id}",
                semantic_string_value=parameter.value_type.value_type.value,
                can_rename=True,
                can_delete=True,
                drag=GraphWorkspaceDrag(
                    _PARAMETER_DRAG_PAYLOAD,
                    parameter.stable_id,
                    parameter.name,
                ),
            )
            for parameter in self._asset.parameters
        )
        self._render_graph_workspace_section(
            ctx,
            GraphWorkspaceSection(
                t("particle_graph_editor.parameters"),
                "particle_parameter",
                entries,
                add_actions=tuple(
                    GraphWorkspaceAddAction(
                        value_type.value,
                        t(f"particle_graph_editor.type_{value_type.value}"),
                    )
                    for value_type in _PARAMETER_CREATE_TYPES
                ),
                add_semantic_id="particle_graph.workspace.parameters.add",
                rename_label=t("particle_graph_editor.rename_parameter"),
                delete_label=t("particle_graph_editor.remove_parameter"),
            ),
        )

    def _render_emitter_page(self, ctx: InxGUIContext) -> None:
        entries = tuple(
            GraphWorkspaceEntry(
                GraphElementRef(GraphElementKind.EMITTER, emitter.stable_id),
                emitter.name,
                dot_color=_WORKSPACE_EMITTER_ON,
                semantic_kind="particle_emitter",
                semantic_id=f"particle_graph.emitter.{index}",
                selected=index == self._emitter_index,
                can_rename=True,
                can_delete=len(self._asset.emitters) > 1,
            )
            for index, emitter in enumerate(self._asset.emitters)
        )
        self._render_graph_workspace_section(
            ctx,
            GraphWorkspaceSection(
                t("particle_graph_editor.emitters"),
                "particle_emitter",
                entries,
                add_actions=(GraphWorkspaceAddAction("default", "Emitter"),),
                add_semantic_id="particle_graph.workspace.emitters.add",
                rename_label=t("particle_graph_editor.rename"),
                delete_label=t("particle_graph_editor.delete"),
            ),
        )

    def _render_emitter_list(self, ctx: InxGUIContext) -> None:
        tabs = (
            (t("particle_graph_editor.emitters"), "emitters"),
            (t("particle_graph_editor.parameters"), "parameters"),
            (t("particle_graph_editor.events"), "events"),
        )
        previous_tab = self._workspace_tab_index
        self._workspace_tab_index = render_compact_tab_bar(
            ctx,
            "particle_graph_workspace",
            tabs,
            self._workspace_tab_index,
            semantic_prefix="particle_graph.workspace",
        )
        if self._workspace_tab_index != previous_tab:
            self.publish_child_context(
                self.current_child_context_id(),
                reason="particle_graph_workspace_tab",
                record_history=True,
            )
        if self._workspace_tab_index == 0:
            self._render_emitter_page(ctx)
        elif self._workspace_tab_index == 1:
            self._render_parameter_page(ctx)
        else:
            self._render_event_page(ctx)

    def current_child_context_id(self) -> str:
        pages = ("emitters", "parameters", "events")
        index = max(0, min(int(self._workspace_tab_index), len(pages) - 1))
        return f"particle_graph.workspace.{pages[index]}"

    def restore_child_context(self, context_id: str) -> bool:
        context_id = str(context_id or "")
        if not context_id:
            return True
        pages = {
            "particle_graph.workspace.emitters": 0,
            "particle_graph.workspace.parameters": 1,
            "particle_graph.workspace.events": 2,
        }
        index = pages.get(context_id)
        if index is None:
            return False
        self._workspace_tab_index = index
        return True

    def _node_graph_parameter_policy(self):
        return _PARTICLE_PARAMETER_POLICY

    def _node_graph_parameter_collection(self):
        return GraphParameterCollection(self._asset.parameters)

    def _node_graph_parameter_detail_config(self) -> GraphParameterDetailConfig:
        return GraphParameterDetailConfig(
            title=t("particle_graph_editor.parameter_settings"),
            name_label=t("particle_graph_editor.name"),
            type_label=t("particle_graph_editor.parameter_type"),
            space_label=t("particle_graph_editor.parameter_space"),
            default_label=t("particle_graph_editor.parameter_default"),
            exposed_label=t("particle_graph_editor.parameter_exposed"),
            writable_label=t("particle_graph_editor.parameter_writable"),
            tooltip_label=t("particle_graph_editor.parameter_tooltip"),
            hdr_label=t("particle_graph_editor.parameter_hdr"),
            semantic_prefix="particle_graph.parameter",
            value_type_label=lambda kind: t(
                f"particle_graph_editor.type_{kind.value}"
            ),
            coordinate_space_label=lambda space: t(
                "particle_graph_editor.space_world"
                if space is CoordinateSpace.WORLD
                else "particle_graph_editor.space_none"
            ),
        )

    def _node_graph_commit_parameter_changes(
        self,
        stable_id: str,
        changes: dict,
    ) -> bool:
        self.update_authoring_parameter(stable_id, changes)
        return True

    def _render_node_graph_parameter_default(
        self,
        ctx: InxGUIContext,
        parameter: ParticleParameter,
        value_type: ValueType,
        value,
        *,
        label: str,
        semantic_prefix: str,
        allow_hdr: bool | None = None,
    ):
        if value_type in {ValueType.TEXTURE2D, ValueType.MESH}:
            default = value
            kind = value_type
            reference = AssetReference.from_dict(default)
            selected_references: list[AssetReference] = []
            is_mesh = kind is ValueType.MESH
            asset_type = "Mesh" if is_mesh else "Texture"
            descriptor = asset_type_registry.require(asset_type)

            def _select_resource(value):
                try:
                    selected_references.append(
                        _particle_asset_reference(asset_type, value)
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    Debug.log_error(
                        f"Particle {kind.value} parameter assignment rejected: {exc}"
                    )

            ping_path = str(reference.path_hint or "").strip()
            if ping_path:
                try:
                    ping_path = resolved_path(ping_path) or ping_path
                except Exception:
                    pass
            render_asset_reference_field(
                ctx,
                f"particle_parameter_default_{kind.value}",
                _mesh_reference_display(reference)
                if is_mesh
                else os.path.basename(reference.path_hint)
                if reference.path_hint
                else t("igui.none"),
                descriptor.display_name,
                asset_type=descriptor.type_id,
                accept_drag_type=descriptor.drag_types,
                on_assign=_select_resource,
                additional_asset_items=_builtin_mesh_picker_items if is_mesh else None,
                on_clear=lambda: selected_references.append(AssetReference()),
                ping_path=ping_path or None,
                has_value=bool(reference.guid or reference.path_hint or builtin_mesh_name(reference)),
                reference_value=_particle_reference_value(asset_type, reference),
                semantic_id=semantic_prefix,
            )
            return (
                selected_references[-1].to_dict()
                if selected_references
                else reference.to_dict()
            )
        if value_type is ValueType.CURVE:
            return self._render_curve_property(
                ctx,
                f"parameter_{parameter.stable_id}",
                "default",
                value,
                semantic_prefix=semantic_prefix,
            )
        if value_type is ValueType.GRADIENT:
            return self._render_gradient_property(
                ctx,
                f"parameter_{parameter.stable_id}",
                "default",
                value,
                semantic_prefix=semantic_prefix,
            )
        return super()._render_node_graph_parameter_default(
            ctx,
            parameter,
            value_type,
            value,
            label=label,
            semantic_prefix=semantic_prefix,
            allow_hdr=allow_hdr,
        )

    def _render_event_type_properties(self, ctx: InxGUIContext) -> None:
        event_type = next(
            (
                item
                for item in self._asset.event_types
                if item.stable_id == self._selected_event_type_id
            ),
            None,
        )
        if event_type is None:
            self._graph_selection.clear(
                reason="particle_graph_drop_missing_event_type",
                record_history=False,
            )
            return

        ctx.label(t("particle_graph_editor.edit_event_type_title"))
        ctx.separator()
        if self._focus_detail_name == "event_type":
            ctx.set_keyboard_focus_here()
            self._focus_detail_name = ""
        name = ctx.text_input(
            f"{t('particle_graph_editor.event_name')}##particle_event_detail_name",
            event_type.name,
            128,
        ).strip()
        capacity = min(
            64,
            max(
                1,
            int(
                ctx.input_uint(
                    f"{t('particle_graph_editor.event_capacity')}##particle_event_detail_capacity",
                    event_type.queue_capacity,
                )
            ),
            ),
        )
        fields = [
            {
                "stable_id": field.stable_id,
                "name": field.name,
                "value_type": field.value_type.value_type,
                "default": copy.deepcopy(field.default),
            }
            for field in event_type.fields
        ]

        def _add_field() -> None:
            fields.append(
                {
                    "stable_id": uuid.uuid4().hex,
                    "name": f"Value {len(fields) + 1}",
                    "value_type": ValueType.F32,
                    "default": 0.0,
                }
            )

        render_workspace_add_header(
            ctx,
            t("particle_graph_editor.event_payload"),
            "##particle_event_detail_field_add",
            on_add=_add_field,
        )
        remove_index = -1
        for index, field in enumerate(fields):
            ctx.label(f"{t('particle_graph_editor.event_field')} {index + 1}")
            field["name"] = ctx.text_input(
                f"{t('particle_graph_editor.event_field_name')}##particle_event_detail_field_{index}_name",
                str(field["name"]),
                128,
            ).strip()
            current_type = field["value_type"]
            type_index = ctx.combo(
                f"{t('particle_graph_editor.event_field_type')}##particle_event_detail_field_{index}_type",
                _EVENT_VALUE_TYPES.index(current_type),
                [
                    t(f"particle_graph_editor.event_type_{value.value}")
                    for value in _EVENT_VALUE_TYPES
                ],
                -1,
            )
            selected_type = _EVENT_VALUE_TYPES[
                max(0, min(type_index, len(_EVENT_VALUE_TYPES) - 1))
            ]
            if selected_type is not current_type:
                field["value_type"] = selected_type
                field["default"] = _event_field_default(selected_type)
            field["default"] = self._render_event_field_default(
                ctx, index, selected_type, field["default"]
            )
            if ctx.button(
                f"{t('particle_graph_editor.remove_event_field')}##particle_event_detail_field_{index}_remove"
            ):
                remove_index = index
            ctx.separator()
        if remove_index >= 0:
            del fields[remove_index]

        encoded_fields = [
            {
                "stable_id": str(field["stable_id"]),
                "name": str(field["name"]),
                "type": TypeRef(field["value_type"]).to_dict(),
                "default": copy.deepcopy(field["default"]),
            }
            for field in fields
        ]
        current_fields = [field.to_dict() for field in event_type.fields]
        if (
            name
            and (
                name != event_type.name
                or capacity != event_type.queue_capacity
                or encoded_fields != current_fields
            )
        ):
            try:
                self.update_event_type(
                    event_type.stable_id, name, capacity, encoded_fields
                )
            except (ParticleGraphSchemaError, TypeError, ValueError) as exc:
                Debug.log_warning(f"Particle event edit rejected: {exc}")

    def _render_emitter_settings(self, ctx: InxGUIContext) -> None:
        ctx.label(t("particle_graph_editor.emitter_settings"))
        ctx.separator()
        emitter = self._selected_emitter()
        if self._focus_detail_name == "emitter":
            ctx.set_keyboard_focus_here()
            self._focus_detail_name = ""
        name = ctx.text_input(
            f"{t('particle_graph_editor.name')}##particle_emitter_name", emitter.name, 128
        ).strip()
        if name and name != emitter.name:
            self._update_emitter(replace(emitter, name=name), "Rename particle emitter")
            emitter = self._selected_emitter()

        settings = emitter.settings
        values = {}
        values["capacity"] = max(
            1,
            int(
                ctx.input_int(
                    f"{t('particle_graph_editor.capacity')}##particle_capacity",
                    settings.capacity,
                )
            ),
        )
        spaces = list(SimulationSpace)
        space_index = spaces.index(settings.simulation_space)
        space_index = ctx.combo(
            f"{t('particle_graph_editor.simulation_space')}##particle_space",
            space_index,
            [t(f"particle_graph_editor.space_{item.value}") for item in spaces],
            -1,
        )
        values["simulation_space"] = spaces[max(0, min(space_index, len(spaces) - 1))]
        values["seed"] = max(
            0,
            int(
                ctx.input_uint(
                    f"{t('particle_graph_editor.seed')}##particle_seed", settings.seed
                )
            ),
        )
        values["spawn_rate"] = max(
            0.0,
            float(
                ctx.drag_float(
                    f"{t('particle_graph_editor.spawn_rate')}##particle_spawn_rate",
                    settings.spawn_rate,
                    0.1,
                    0.0,
                    1.0e7,
                )
            ),
        )
        values["spawn_rate_over_distance"] = max(
            0.0,
            float(
                ctx.drag_float(
                    f"{t('particle_graph_editor.spawn_rate_over_distance')}##particle_spawn_rate_over_distance",
                    settings.spawn_rate_over_distance,
                    0.1,
                    0.0,
                    1.0e7,
                )
            ),
        )
        values["duration"] = max(
            0.001,
            float(
                ctx.drag_float(
                    f"{t('particle_graph_editor.duration')}##particle_duration",
                    settings.duration,
                    0.05,
                    0.001,
                    1.0e7,
                )
            ),
        )
        values["loop"] = bool(
            ctx.checkbox(
                f"{t('particle_graph_editor.loop')}##particle_loop",
                settings.loop,
            )
        )
        values["start_delay"] = max(
            0.0,
            float(
                ctx.drag_float(
                    f"{t('particle_graph_editor.start_delay')}##particle_start_delay",
                    settings.start_delay,
                    0.05,
                    0.0,
                    1.0e7,
                )
            ),
        )
        values["collision_enabled"] = bool(
            ctx.checkbox(
                f"{t('particle_graph_editor.collision')}##particle_collision_enabled",
                settings.collision_enabled,
            )
        )
        if values["collision_enabled"]:
            values["collision_layer_mask"] = self._render_collision_layer_mask(
                ctx, settings.collision_layer_mask
            )
            values["collision_include_triggers"] = bool(
                ctx.checkbox(
                    f"{t('particle_graph_editor.include_triggers')}##particle_collision_include_triggers",
                    settings.collision_include_triggers,
                )
            )
            values["collision_bounce_scale"] = max(
                0.0,
                float(
                    ctx.drag_float(
                        f"{t('particle_graph_editor.bounce_scale')}##particle_collision_bounce_scale",
                        settings.collision_bounce_scale,
                        0.01,
                        0.0,
                        100.0,
                    )
                ),
            )
            values["collision_friction_scale"] = max(
                0.0,
                float(
                    ctx.drag_float(
                        f"{t('particle_graph_editor.friction_scale')}##particle_collision_friction_scale",
                        settings.collision_friction_scale,
                        0.01,
                        0.0,
                        100.0,
                    )
                ),
            )

        ctx.separator()
        ctx.label(t("particle_graph_editor.emission_shape"))
        shape = settings.shape
        shape_kinds = [
            item
            for item in EmitterShapeKind
            if item is not EmitterShapeKind.SDF
            or shape.kind is EmitterShapeKind.SDF
        ]
        kind_index = ctx.combo(
            f"{t('particle_graph_editor.shape')}##particle_shape",
            shape_kinds.index(shape.kind),
            [t(f"particle_graph_editor.shape_{item.value}") for item in shape_kinds],
            -1,
        )
        requested_kind = shape_kinds[max(0, min(kind_index, len(shape_kinds) - 1))]
        sdf_interfaces = tuple(
            interface
            for interface in self._selected_emitter().data_interfaces
            if isinstance(interface, SdfVolume)
        )
        kind = requested_kind
        if requested_kind is EmitterShapeKind.SDF and not sdf_interfaces:
            ctx.label(t("particle_graph_editor.sdf_shape_requires_interface"))
            kind = shape.kind
        shape_spaces = [CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.WORLD]
        shape_space = shape.space
        if kind is not EmitterShapeKind.SDF:
            shape_space_index = ctx.combo(
                f"{t('particle_graph_editor.shape_space')}##particle_shape_space",
                shape_spaces.index(shape.space),
                [t(f"particle_graph_editor.shape_space_{item.value}") for item in shape_spaces],
                -1,
            )
            shape_space = shape_spaces[max(0, min(shape_space_index, len(shape_spaces) - 1))]
        radius = shape.radius
        angle = shape.angle_degrees
        dimensions = shape.dimensions
        mesh = shape.mesh if kind is EmitterShapeKind.MESH else AssetReference()
        mesh_mode = shape.mesh_mode
        sdf_interface = shape.sdf_interface if kind is EmitterShapeKind.SDF else ""
        sdf_mode = shape.sdf_mode
        if kind in {EmitterShapeKind.SPHERE, EmitterShapeKind.CONE}:
            radius = max(
                0.0,
                float(ctx.drag_float(f"{t('particle_graph_editor.radius')}##particle_shape_radius", shape.radius, 0.05, 0.0, 1.0e7)),
            )
        if kind is EmitterShapeKind.CONE:
            angle = min(
                180.0,
                max(0.0, float(ctx.drag_float(f"{t('particle_graph_editor.angle')}##particle_shape_angle", shape.angle_degrees, 0.2, 0.0, 180.0))),
            )
        if kind is EmitterShapeKind.BOX:
            dimensions = tuple(
                max(0.0, float(ctx.drag_float(f"{t('particle_graph_editor.size')} {axis}##particle_shape_{axis}", value, 0.05, 0.0, 1.0e7)))
                for axis, value in zip("XYZ", shape.dimensions)
            )
        if kind is EmitterShapeKind.MESH:
            selected_meshes: list[AssetReference] = []
            descriptor = asset_type_registry.require("Mesh")

            def _select_mesh(value):
                try:
                    selected_meshes.append(_particle_asset_reference("Mesh", value))
                except (KeyError, TypeError, ValueError) as exc:
                    Debug.log_error(f"Particle emitter shape assignment rejected: {exc}")

            _mesh_ping = str(mesh.path_hint or "").strip()
            if _mesh_ping:
                try:
                    _mesh_ping = resolved_path(_mesh_ping) or _mesh_ping
                except Exception:
                    pass
            render_asset_reference_field(
                ctx,
                "particle_emitter_shape_mesh",
                _mesh_reference_display(mesh),
                descriptor.display_name,
                asset_type=descriptor.type_id,
                accept_drag_type=descriptor.drag_types,
                on_assign=_select_mesh,
                additional_asset_items=_builtin_mesh_picker_items,
                on_clear=lambda: selected_meshes.append(AssetReference()),
                ping_path=_mesh_ping or None,
                has_value=bool(mesh.guid or mesh.path_hint or builtin_mesh_name(mesh)),
                reference_value=_particle_reference_value("Mesh", mesh),
                semantic_id="particle_graph.emitter.shape.mesh",
            )
            if selected_meshes:
                mesh = selected_meshes[-1]
            mesh_modes = list(MeshEmissionMode)
            mesh_mode_index = ctx.combo(
                f"{t('particle_graph_editor.mesh_sampling')}##particle_shape_mesh_mode",
                mesh_modes.index(mesh_mode),
                [t(f"particle_graph_editor.mesh_sampling_{item.value}") for item in mesh_modes],
                -1,
            )
            mesh_mode = mesh_modes[
                max(0, min(mesh_mode_index, len(mesh_modes) - 1))
            ]
        if kind is EmitterShapeKind.SDF:
            sdf_ids = [interface.stable_id for interface in sdf_interfaces]
            if sdf_interface not in sdf_ids:
                sdf_interface = sdf_ids[0]
            sdf_index = ctx.combo(
                f"{t('particle_graph_editor.sdf_volume')}##particle_shape_sdf_interface",
                sdf_ids.index(sdf_interface),
                [interface.name for interface in sdf_interfaces],
                -1,
            )
            sdf_interface = sdf_ids[max(0, min(sdf_index, len(sdf_ids) - 1))]
            sdf_modes = list(SdfEmissionMode)
            sdf_mode_index = ctx.combo(
                f"{t('particle_graph_editor.sdf_emission')}##particle_shape_sdf_mode",
                sdf_modes.index(sdf_mode),
                [
                    t(f"particle_graph_editor.sdf_emission_{item.value}")
                    for item in sdf_modes
                ],
                -1,
            )
            sdf_mode = sdf_modes[max(0, min(sdf_mode_index, len(sdf_modes) - 1))]
        values["shape"] = replace(
            shape,
            kind=kind,
            space=shape_space,
            radius=radius,
            angle_degrees=angle,
            dimensions=dimensions,
            mesh=mesh,
            mesh_mode=mesh_mode,
            sdf_interface=sdf_interface,
            sdf_mode=sdf_mode,
        )

        new_settings = preserve_ui_float_precision(
            replace(settings, **values), settings
        )
        if new_settings != settings:
            self._update_settings(new_settings)

        ctx.separator()
        ctx.label(t("particle_graph_editor.bursts"))
        bursts = list(self._selected_emitter().settings.bursts)
        changed = False
        remove_index = -1
        for index, burst in enumerate(bursts):
            ctx.label(f"{t('particle_graph_editor.burst')} {index + 1}")
            time_value = max(0.0, float(ctx.drag_float(f"{t('particle_graph_editor.burst_time')}##burst_time_{index}", burst.time, 0.05, 0.0, 1.0e7)))
            count = max(0, int(ctx.input_int(f"{t('particle_graph_editor.burst_count')}##burst_count_{index}", burst.count)))
            cycles = max(1, int(ctx.input_int(f"{t('particle_graph_editor.burst_cycles')}##burst_cycles_{index}", burst.cycles)))
            interval = max(0.0, float(ctx.drag_float(f"{t('particle_graph_editor.burst_interval')}##burst_interval_{index}", burst.interval, 0.05, 0.0, 1.0e7)))
            probability = min(
                1.0,
                max(
                    0.0,
                    float(
                        ctx.drag_float(
                            f"{t('particle_graph_editor.burst_probability')}##burst_probability_{index}",
                            burst.probability,
                            0.01,
                            0.0,
                            1.0,
                        )
                    ),
                ),
            )
            updated = preserve_ui_float_precision(
                ParticleBurst(time_value, count, cycles, interval, probability), burst
            )
            if updated != burst:
                bursts[index] = updated
                changed = True
            if ctx.button(f"{t('particle_graph_editor.remove_burst')}##particle_burst_remove_{index}"):
                remove_index = index
        if remove_index >= 0:
            del bursts[remove_index]
            changed = True
        if ctx.button(t("particle_graph_editor.add_burst")):
            bursts.append(ParticleBurst(0.0, 10))
            changed = True
        if changed:
            self._update_settings(
                replace(self._selected_emitter().settings, bursts=tuple(bursts))
            )

    @staticmethod
    def _render_collision_layer_mask(ctx: InxGUIContext, value: int) -> int:
        mask = int(value) & 0xFFFFFFFF
        if mask == 0xFFFFFFFF:
            summary = t("particle_graph_editor.layers_everything")
        elif mask == 0:
            summary = t("particle_graph_editor.layers_nothing")
        else:
            summary = f"{mask.bit_count()}/32"

        popup_id = "particle_collision_layers_popup"
        if ctx.button(
            f"{t('particle_graph_editor.collision_layers')}: {summary}##particle_collision_layers"
        ):
            ctx.open_popup(popup_id)
        if not ctx.begin_popup(popup_id):
            return mask

        if ctx.button(
            f"{t('particle_graph_editor.layers_everything')}##particle_collision_layers_all"
        ):
            mask = 0xFFFFFFFF
        if ctx.button(
            f"{t('particle_graph_editor.layers_nothing')}##particle_collision_layers_none"
        ):
            mask = 0
        ctx.separator()

        try:
            from Infernux.lib import TagLayerManager

            layer_names = list(TagLayerManager.instance().get_all_layers())
        except (AttributeError, ReferenceError, RuntimeError):
            layer_names = []
        for index, name in enumerate(layer_names[:32]):
            if not name:
                continue
            bit = 1 << index
            selected = bool(mask & bit)
            next_selected = bool(
                ctx.checkbox(f"{name}##particle_collision_layer_{index}", selected)
            )
            if next_selected != selected:
                mask = (mask | bit) if next_selected else (mask & ~bit)

        ctx.end_popup()
        return mask & 0xFFFFFFFF

    def _render_data_interfaces(self, ctx: InxGUIContext) -> None:
        ctx.separator()
        ctx.label(t("particle_graph_editor.data_interfaces"))
        emitter = self._selected_emitter()
        interfaces = list(emitter.data_interfaces)
        changed = False
        remove_index = -1

        for index, interface in enumerate(interfaces):
            ctx.separator()
            kind_label = t(f"particle_graph_editor.interface_{interface.kind}")
            ctx.label(f"{kind_label} {index + 1}")
            replacement = interface

            name = ctx.text_input(
                f"{t('particle_graph_editor.name')}##particle_interface_name_{interface.stable_id}",
                interface.name,
                128,
            ).strip()
            if name and name != replacement.name:
                replacement = replace(replacement, name=name)

            reference = self._data_interface_reference(replacement)
            path_hint = str(reference.path_hint or "")
            selected_references: list[AssetReference] = []
            asset_type = self._data_interface_asset_contract(replacement)
            descriptor = asset_type_registry.require(asset_type)

            def _select_asset(value):
                try:
                    selected_references.append(
                        _particle_asset_reference(asset_type, value)
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    Debug.log_error(
                        f"Particle Data Interface assignment rejected: {exc}"
                    )

            _iface_ping = str(path_hint or "").strip()
            if _iface_ping:
                try:
                    _iface_ping = resolved_path(_iface_ping) or _iface_ping
                except Exception:
                    pass
            render_asset_reference_field(
                ctx,
                f"particle_interface_asset_{interface.stable_id}",
                os.path.basename(path_hint) if path_hint else t("igui.none"),
                descriptor.display_name,
                accept_drag_type=descriptor.drag_types,
                on_assign=_select_asset,
                on_clear=lambda: selected_references.append(AssetReference()),
                ping_path=_iface_ping or None,
                has_value=bool(reference.guid or reference.path_hint),
                asset_type=descriptor.type_id,
                reference_value=_particle_reference_value(asset_type, reference),
                semantic_id=f"particle_graph.interface.{interface.stable_id}.asset",
            )
            if selected_references:
                selected = selected_references[-1]
                replacement = replace(
                    replacement,
                    texture=selected,
                )

            spaces = [CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.WORLD]
            space_index = ctx.combo(
                f"{t('particle_graph_editor.interface_space')}##particle_interface_space_{interface.stable_id}",
                spaces.index(replacement.space),
                [t(f"particle_graph_editor.shape_space_{item.value}") for item in spaces],
                -1,
            )
            replacement = replace(
                replacement,
                space=spaces[max(0, min(space_index, len(spaces) - 1))],
            )

            matrix_name = "field_to_space"
            matrix = list(getattr(replacement, matrix_name))
            for row in range(4):
                values = []
                for column in range(4):
                    offset = row * 4 + column
                    values.append(
                        float(
                            ctx.drag_float(
                                f"{t('particle_graph_editor.interface_transform')} {row + 1}.{column + 1}"
                                f"##particle_interface_matrix_{interface.stable_id}_{offset}",
                                matrix[offset],
                                0.02,
                                -1.0e7,
                                1.0e7,
                            )
                        )
                    )
                matrix[row * 4 : row * 4 + 4] = values
            replacement = replace(replacement, **{matrix_name: tuple(matrix)})

            if isinstance(replacement, SdfVolume):
                distance_scale = max(
                    1.0e-6,
                    float(
                        ctx.drag_float(
                            f"{t('particle_graph_editor.distance_scale')}##particle_interface_distance_{interface.stable_id}",
                            replacement.distance_scale,
                            0.02,
                            1.0e-6,
                            1.0e7,
                        )
                    ),
                )
                filters = list(SdfFilter)
                filter_index = ctx.combo(
                    f"{t('particle_graph_editor.filtering')}##particle_interface_filter_{interface.stable_id}",
                    filters.index(replacement.filtering),
                    [t(f"particle_graph_editor.filter_{item.value}") for item in filters],
                    -1,
                )
                replacement = replace(
                    replacement,
                    distance_scale=distance_scale,
                    filtering=filters[max(0, min(filter_index, len(filters) - 1))],
                )
            elif isinstance(replacement, VectorField):
                boundaries = list(VectorFieldBoundary)
                boundary_index = ctx.combo(
                    f"{t('particle_graph_editor.boundary')}##particle_interface_boundary_{interface.stable_id}",
                    boundaries.index(replacement.boundary),
                    [t(f"particle_graph_editor.boundary_{item.value}") for item in boundaries],
                    -1,
                )
                filters = list(VectorFieldFilter)
                filter_index = ctx.combo(
                    f"{t('particle_graph_editor.filtering')}##particle_interface_filter_{interface.stable_id}",
                    filters.index(replacement.filtering),
                    [t(f"particle_graph_editor.filter_{item.value}") for item in filters],
                    -1,
                )
                replacement = replace(
                    replacement,
                    vector_scale=float(
                        ctx.drag_float(
                            f"{t('particle_graph_editor.vector_scale')}##particle_interface_scale_{interface.stable_id}",
                            replacement.vector_scale,
                            0.02,
                            -1.0e7,
                            1.0e7,
                        )
                    ),
                    boundary=boundaries[
                        max(0, min(boundary_index, len(boundaries) - 1))
                    ],
                    filtering=filters[max(0, min(filter_index, len(filters) - 1))],
                )
            replacement = preserve_ui_float_precision(replacement, interface)
            if replacement != interface:
                interfaces[index] = replacement
                changed = True
            if ctx.button(
                f"{t('particle_graph_editor.remove_interface')}##particle_interface_remove_{interface.stable_id}"
            ):
                remove_index = index

        if remove_index >= 0:
            interface = interfaces[remove_index]
            referenced = any(
                node.properties.get("interface") == interface.stable_id
                for stage in _STAGES
                for document in (getattr(emitter, stage),)
                if document is not None
                for node in document.nodes
            )
            if referenced:
                Debug.log_warning(
                    f"Particle Data Interface {interface.name!r} is still used by a node"
                )
            else:
                del interfaces[remove_index]
                changed = True

        if changed:
            emitter_id = emitter.stable_id
            old_by_id = {
                item.stable_id: (index, item)
                for index, item in enumerate(emitter.data_interfaces)
            }
            new_by_id = {
                item.stable_id: (index, item)
                for index, item in enumerate(interfaces)
            }
            mutations = []
            for stable_id, (index, item) in old_by_id.items():
                ref = GraphElementRef(
                    GraphElementKind.DATA_INTERFACE,
                    self._particle_element_id(emitter_id, stable_id),
                )
                if stable_id not in new_by_id:
                    mutations.append(
                        GraphMutation(
                            GraphMutationKind.REMOVE,
                            ref,
                            before=item.to_dict(),
                            before_index=index,
                        )
                    )
                    continue
                new_index, replacement = new_by_id[stable_id]
                if replacement != item or new_index != index:
                    mutations.append(
                        GraphMutation(
                            GraphMutationKind.UPDATE,
                            ref,
                            before=item.to_dict(),
                            after=replacement.to_dict(),
                            before_index=index,
                            after_index=new_index,
                        )
                    )
            if mutations:
                self._execute_graph_mutations(
                    "Edit Particle Graph Data Interfaces",
                    tuple(mutations),
                )

        if ctx.button(t("particle_graph_editor.add_vector_field")):
            self.add_authoring_data_interface(
                self._selected_emitter().stable_id, VectorField.kind
            )
    def _render_node_properties(self, ctx: InxGUIContext) -> None:
        if self._model is None or not self._selected_node_uid:
            return
        node = self._model.find_node(self._selected_node_uid)
        definition = self._definition_for_node(node)
        if node is None or definition is None:
            return
        ctx.label(t("particle_graph_editor.node_settings"))
        ctx.separator()
        canvas_definition = self._model.get_node_type(node)
        ctx.label(
            canvas_definition.label
            if canvas_definition is not None
            else definition.display_name
        )
        property_ids = {item.id for item in definition.properties}
        editable_fields = [
            (item.id, item.value_type.value_type, item.default)
            for item in definition.properties
        ]
        editable_fields.extend(
            (port.id, port.value_type.value_type, port.default)
            for port in definition.ports
            if port.direction is PortDirection.INPUT
            and port.kind is PortKind.VALUE
            and not port.required
            and port.value_type is not None
            and port.id not in property_ids
            and not any(
                link.target_node == node.uid and link.target_pin == port.id
                for link in self._model.links
            )
        )
        for key, value_type, default in editable_fields:
            if not _node_property_is_visible(node, key):
                continue
            value = copy.deepcopy(node.data.get(key, default))
            label_key = f"particle_graph_editor.property_{key}"
            label = t(label_key)
            if label == label_key:
                label = (
                    key.removeprefix("shader.")
                    if key.startswith("shader.")
                    else key.replace("_", " ").title()
                )
            new_value = value
            semantic_id = f"particle_graph.node.{node.uid}.property.{key}"
            semantic_recorded_by_widget = False
            if value_type is ValueType.BOOL:
                new_value = bool(ctx.checkbox(f"{label}##particle_node_{key}", bool(value)))
            elif value_type in {ValueType.I32, ValueType.U32}:
                semantic_method = getattr(
                    ctx,
                    "input_uint_semantic"
                    if value_type is ValueType.U32
                    else "input_int_semantic",
                    None,
                )
                if callable(semantic_method):
                    new_value = int(
                        semantic_method(
                            f"{label}##particle_node_{key}",
                            int(value),
                            semantic_id,
                        )
                    )
                    semantic_recorded_by_widget = True
                else:
                    input_method = (
                        ctx.input_uint
                        if value_type is ValueType.U32
                        else ctx.input_int
                    )
                    new_value = int(
                        input_method(f"{label}##particle_node_{key}", int(value))
                    )
            elif value_type is ValueType.F32:
                semantic_drag = getattr(ctx, "drag_float_semantic", None)
                if callable(semantic_drag):
                    new_value = float(
                        semantic_drag(
                            f"{label}##particle_node_{key}",
                            float(value),
                            0.05,
                            -1.0e7,
                            1.0e7,
                            semantic_id,
                        )
                    )
                    semantic_recorded_by_widget = True
                else:
                    new_value = float(
                        ctx.drag_float(
                            f"{label}##particle_node_{key}",
                            float(value),
                            0.05,
                            -1.0e7,
                            1.0e7,
                        )
                    )
            elif value_type is ValueType.COLOR:
                ctx.label(label)
                new_value = render_color_value_bar(
                    ctx,
                    f"##particle_node_{node.uid}_{key}",
                    value,
                    allow_hdr=True,
                    default_hdr_enabled=True,
                )
            elif value_type in {ValueType.VEC2, ValueType.VEC3, ValueType.VEC4}:
                new_value = [
                    float(ctx.drag_float(f"{label} {axis}##particle_node_{key}_{axis}", float(component), 0.05, -1.0e7, 1.0e7))
                    for axis, component in zip("XYZW", value)
                ]
            elif value_type is ValueType.TEXTURE2D:
                reference = AssetReference.from_dict(value)
                descriptor = asset_type_registry.require("Texture")
                reference_committed = False

                def _select_texture(candidate):
                    nonlocal reference_committed
                    try:
                        self.set_node_asset_reference(node.uid, key, candidate)
                        reference_committed = True
                    except (KeyError, TypeError, ValueError) as exc:
                        Debug.log_error(
                            f"Particle Output texture assignment rejected: {exc}"
                        )

                def _clear_texture():
                    nonlocal reference_committed
                    self.set_node_asset_reference(node.uid, key, None)
                    reference_committed = True

                ping_path = str(reference.path_hint or "").strip()
                if ping_path:
                    try:
                        ping_path = _resolve_project_asset_path(ping_path) or ping_path
                    except Exception:
                        pass
                render_asset_reference_field(
                    ctx,
                    f"particle_node_{key}",
                    os.path.basename(reference.path_hint)
                    if reference.path_hint
                    else (reference.guid or t("igui.none")),
                    descriptor.display_name,
                    asset_type=descriptor.type_id,
                    accept_drag_type=descriptor.drag_types,
                    on_assign=_select_texture,
                    on_clear=_clear_texture,
                    ping_path=ping_path or None,
                    has_value=bool(reference.guid or reference.path_hint),
                    reference_value=_particle_reference_value("Texture", reference),
                    semantic_id=semantic_id,
                )
                if reference_committed:
                    return
            elif value_type in {ValueType.ASSET_REF, ValueType.MESH}:
                reference = dict(value)
                mesh_reference = (
                    AssetReference.from_dict(reference)
                    if value_type is ValueType.MESH or key == "mesh"
                    else None
                )
                path_hint = str(reference.get("path_hint", "") or "")
                reference_committed = False

                is_mesh = value_type is ValueType.MESH or key == "mesh"
                display = (
                    _mesh_reference_display(mesh_reference)
                    if is_mesh
                    else os.path.basename(path_hint) if path_hint else t("igui.none")
                )
                asset_kind = "Mesh" if is_mesh else "Material"
                descriptor = asset_type_registry.require(asset_kind)

                def _select_asset(candidate):
                    nonlocal reference_committed
                    try:
                        self.set_node_asset_reference(node.uid, key, candidate)
                        reference_committed = True
                    except (KeyError, TypeError, ValueError) as exc:
                        Debug.log_error(
                            f"Particle node {asset_kind} assignment rejected: {exc}"
                        )

                def _clear_asset():
                    nonlocal reference_committed
                    self.set_node_asset_reference(node.uid, key, None)
                    reference_committed = True

                _node_ping = str(path_hint or "").strip()
                if _node_ping:
                    try:
                        _node_ping = resolved_path(_node_ping) or _node_ping
                    except Exception:
                        pass
                render_asset_reference_field(
                    ctx,
                    f"particle_node_{key}",
                    display,
                    descriptor.display_name,
                    asset_type=descriptor.type_id,
                    accept_drag_type=descriptor.drag_types,
                    on_assign=_select_asset,
                    additional_asset_items=(
                        _builtin_mesh_picker_items if is_mesh else None
                    ),
                    on_clear=_clear_asset,
                    ping_path=_node_ping or None,
                    has_value=bool(reference.get("guid") or path_hint),
                    reference_value=(
                        _particle_reference_value(asset_kind, mesh_reference)
                        if mesh_reference is not None
                        else AssetReferenceCodec.normalize(asset_kind, reference)
                    ),
                    semantic_id=f"particle_graph.node.{node.uid}.property.{key}",
                )
                if reference_committed:
                    return
            elif value_type is ValueType.STRING and key == "interface":
                expected = self._data_interface_type_for_node(node.type_id)
                matching = [
                    interface
                    for interface in self._selected_emitter().data_interfaces
                    if expected is None or isinstance(interface, expected)
                ]
                values = [""] + [interface.stable_id for interface in matching]
                labels = [t("igui.none")] + [interface.name for interface in matching]
                current = values.index(str(value)) if str(value) in values else 0
                current = ctx.combo(
                    f"{label}##particle_node_{key}",
                    current,
                    labels,
                    -1,
                )
                new_value = values[max(0, min(current, len(values) - 1))]
            elif value_type is ValueType.STRING and key == "attribute":
                entries = self._model.attribute_entries()
                labels = [entry[0] for entry in entries]
                values = [entry[1] for entry in entries]
                if values:
                    current = values.index(str(value)) if str(value) in values else 0
                    current = ctx.combo(
                        f"{label}##particle_node_{key}",
                        current,
                        labels,
                        -1,
                    )
                    new_value = values[max(0, min(current, len(values) - 1))]
            elif value_type is ValueType.STRING and key == "parameter":
                entries = self._model.parameter_entries()
                labels = [entry[0] for entry in entries]
                values = [entry[1] for entry in entries]
                if values:
                    current = values.index(str(value)) if str(value) in values else 0
                    current = ctx.combo(
                        f"{label}##particle_node_{key}",
                        current,
                        labels,
                        -1,
                    )
                    new_value = values[max(0, min(current, len(values) - 1))]
            elif value_type is ValueType.STRING and (
                property_def := definition.property(key)
            ) is not None and property_def.choices:
                choices = property_def.choices
                labels = [label for label, _value in choices]
                values = [value for _label, value in choices]
                current = values.index(value) if value in values else 0
                current = ctx.combo(
                    f"{label}##particle_node_{key}",
                    current,
                    labels,
                    -1,
                )
                new_value = values[max(0, min(current, len(values) - 1))]
            elif value_type is ValueType.STRING and key == "sort":
                options = (
                    ["none"]
                    if node.type_id == "particle.output.ribbon"
                    else ["none", "back_to_front", "front_to_back"]
                )
                current = options.index(value) if value in options else 0
                current = ctx.combo(
                    f"{label}##particle_node_{key}",
                    current,
                    [t(f"particle_graph_editor.sort_{option}") for option in options],
                    -1,
                )
                new_value = options[max(0, min(current, len(options) - 1))]
            elif value_type is ValueType.STRING and key == "uv_mode":
                options = ["stretch", "repeat"]
                current = options.index(value) if value in options else 0
                current = ctx.combo(
                    f"{label}##particle_node_{key}",
                    current,
                    [t(f"particle_graph_editor.uv_{option}") for option in options],
                    -1,
                )
                new_value = options[max(0, min(current, len(options) - 1))]
            elif value_type is ValueType.STRING and key == "alignment":
                options = ["camera_plane", "camera_position", "axis", "velocity"]
                current = options.index(value) if value in options else 0
                current = ctx.combo(
                    f"{label}##particle_node_{key}",
                    current,
                    [t(f"particle_graph_editor.alignment_{option}") for option in options],
                    -1,
                )
                new_value = options[max(0, min(current, len(options) - 1))]
            elif value_type is ValueType.STRING:
                new_value = ctx.text_input(f"{label}##particle_node_{key}", str(value), 512)
            elif value_type is ValueType.CURVE:
                new_value = self._render_curve_property(ctx, node.uid, key, value)
            elif value_type is ValueType.GRADIENT:
                new_value = self._render_gradient_property(ctx, node.uid, key, value)
            if not semantic_recorded_by_widget and value_type in {
                ValueType.BOOL,
                ValueType.I32,
                ValueType.U32,
                ValueType.F32,
                ValueType.STRING,
            }:
                _record_scalar_node_property_semantics(
                    ctx,
                    node_uid=node.uid,
                    key=key,
                    label=label,
                    value_type=value_type,
                    value=new_value,
                )
            new_value = preserve_ui_float_precision(new_value, value)
            if new_value != value:
                self._on_node_data_changed(node.uid, key, value, new_value)
                return

    @staticmethod
    def _render_curve_property(
        ctx: InxGUIContext,
        node_uid: str,
        key: str,
        value,
        *,
        semantic_prefix: str = "",
    ):
        curve = Curve.from_dict(value)
        keys = [item.to_dict() for item in curve.keys]
        pre_index = CURVE_WRAP_MODES.index(curve.pre_wrap)
        post_index = CURVE_WRAP_MODES.index(curve.post_wrap)
        pre_index = ctx.combo(
            f"{t('particle_graph_editor.pre_wrap')}##{node_uid}_{key}_pre",
            pre_index,
            list(CURVE_WRAP_MODES),
            -1,
        )
        post_index = ctx.combo(
            f"{t('particle_graph_editor.post_wrap')}##{node_uid}_{key}_post",
            post_index,
            list(CURVE_WRAP_MODES),
            -1,
        )
        remove_index = -1
        for index, item in enumerate(keys):
            ctx.separator()
            ctx.label(f"{t('particle_graph_editor.key')} {index + 1}")
            minimum = keys[index - 1]["time"] + 1.0e-4 if index else -1.0e7
            maximum = (
                keys[index + 1]["time"] - 1.0e-4
                if index + 1 < len(keys)
                else 1.0e7
            )
            item["time"] = float(
                ctx.drag_float(
                    f"{t('particle_graph_editor.time')}##{node_uid}_{key}_{index}_time",
                    item["time"],
                    0.01,
                    minimum,
                    maximum,
                )
            )
            ctx.record_semantic_item(
                "drag_float",
                t("particle_graph_editor.time"),
                True,
                f"{semantic_prefix or f'particle_graph.node.{node_uid}.property.{key}'}.key.{index}.time",
                numeric_value=item["time"],
            )
            for tangent_key in ("value", "in_tangent", "out_tangent"):
                item[tangent_key] = float(
                    ctx.drag_float(
                        f"{t(f'particle_graph_editor.{tangent_key}')}##{node_uid}_{key}_{index}_{tangent_key}",
                        item[tangent_key],
                        0.02,
                        -1.0e7,
                        1.0e7,
                    )
                )
                ctx.record_semantic_item(
                    "drag_float",
                    t(f"particle_graph_editor.{tangent_key}"),
                    True,
                    f"{semantic_prefix or f'particle_graph.node.{node_uid}.property.{key}'}.key.{index}.{tangent_key}",
                    numeric_value=item[tangent_key],
                )
            if len(keys) > 1 and ctx.button(
                f"{t('particle_graph_editor.remove_key')}##{node_uid}_{key}_{index}_remove"
            ):
                remove_index = index
        if remove_index >= 0:
            del keys[remove_index]
        if len(keys) < MAX_RAMP_KEYS and ctx.button(
            f"{t('particle_graph_editor.add_key')}##{node_uid}_{key}_add"
        ):
            last = keys[-1]
            keys.append(
                {
                    "time": last["time"] + 1.0,
                    "value": last["value"],
                    "in_tangent": last["in_tangent"],
                    "out_tangent": last["out_tangent"],
                }
            )
        return Curve.from_dict(
            {
                "keys": keys,
                "pre_wrap": CURVE_WRAP_MODES[pre_index],
                "post_wrap": CURVE_WRAP_MODES[post_index],
            }
        ).to_dict()

    @staticmethod
    def _render_gradient_property(
        ctx: InxGUIContext,
        node_uid: str,
        key: str,
        value,
        *,
        semantic_prefix: str = "",
    ):
        gradient = Gradient.from_dict(value)
        keys = [item.to_dict() for item in gradient.keys]
        mode_index = GRADIENT_MODES.index(gradient.mode)
        mode_index = ctx.combo(
            f"{t('particle_graph_editor.gradient_mode')}##{node_uid}_{key}_mode",
            mode_index,
            list(GRADIENT_MODES),
            -1,
        )
        remove_index = -1
        for index, item in enumerate(keys):
            ctx.separator()
            ctx.label(f"{t('particle_graph_editor.key')} {index + 1}")
            minimum = keys[index - 1]["time"] + 1.0e-4 if index else 0.0
            maximum = (
                keys[index + 1]["time"] - 1.0e-4
                if index + 1 < len(keys)
                else 1.0
            )
            item["time"] = float(
                ctx.drag_float(
                    f"{t('particle_graph_editor.time')}##{node_uid}_{key}_{index}_time",
                    item["time"],
                    0.01,
                    minimum,
                    maximum,
                )
            )
            ctx.record_semantic_item(
                "drag_float",
                t("particle_graph_editor.time"),
                True,
                f"{semantic_prefix or f'particle_graph.node.{node_uid}.property.{key}'}.key.{index}.time",
                numeric_value=item["time"],
            )
            color = list(
                ctx.color_edit(
                    f"{t('particle_graph_editor.color')}##{node_uid}_{key}_{index}_color",
                    *item["color"],
                    hdr=True,
                )
            )
            for channel_index, channel in enumerate(("r", "g", "b", "a")):
                maximum = 1.0 if channel == "a" else 64.0
                color[channel_index] = float(
                    ctx.drag_float(
                        f"{channel.upper()}##{node_uid}_{key}_{index}_{channel}",
                        color[channel_index],
                        0.01,
                        0.0,
                        maximum,
                    )
                )
                ctx.record_semantic_item(
                    "drag_float",
                    channel.upper(),
                    True,
                    f"{semantic_prefix or f'particle_graph.node.{node_uid}.property.{key}'}.key.{index}.color.{channel}",
                    numeric_value=color[channel_index],
                )
            item["color"] = color
            if len(keys) > 1 and ctx.button(
                f"{t('particle_graph_editor.remove_key')}##{node_uid}_{key}_{index}_remove"
            ):
                remove_index = index
        if remove_index >= 0:
            del keys[remove_index]
        if len(keys) < MAX_RAMP_KEYS and ctx.button(
            f"{t('particle_graph_editor.add_key')}##{node_uid}_{key}_add"
        ):
            if len(keys) == 1:
                new_time = 1.0 if keys[0]["time"] < 1.0 else 0.0
                keys.append({"time": new_time, "color": list(keys[0]["color"])})
                keys.sort(key=lambda item: item["time"])
            else:
                gap_index = max(
                    range(len(keys) - 1),
                    key=lambda index: keys[index + 1]["time"] - keys[index]["time"],
                )
                left = keys[gap_index]
                right = keys[gap_index + 1]
                keys.insert(
                    gap_index + 1,
                    {
                        "time": (left["time"] + right["time"]) * 0.5,
                        "color": [
                            a + (b - a) * 0.5
                            for a, b in zip(left["color"], right["color"])
                        ],
                    },
                )
        return Gradient.from_dict(
            {"keys": keys, "mode": GRADIENT_MODES[mode_index]}
        ).to_dict()

    def _before_node_graph_render(self, ctx: InxGUIContext) -> None:
        del ctx
        self._refresh_shader_definitions_if_needed()

    def _render_node_graph_toolbar(self, ctx: InxGUIContext) -> None:
        save_label = t("particle_graph_editor.save")
        if ctx.button(save_label):
            from Infernux.engine.interaction import CommandSource

            self._execute_node_graph_command("file.save", source=CommandSource.TOOLBAR)
        if bool(getattr(ctx, "semantic_capture_enabled", True)):
            ctx.record_semantic_item("button", save_label, True, "particle_graph.toolbar.save")
        ctx.same_line(0, 12)
        ctx.label(self._asset.name)
        if self._draft_compile_error:
            ctx.same_line(0, 12)
            ctx.label(t("particle_graph_editor.draft_invalid"))
            if ctx.is_item_hovered():
                ctx.set_tooltip(self._draft_compile_error)
        self._record_document_semantics(ctx)

    def _render_node_graph_left_panel(self, ctx: InxGUIContext) -> None:
        self._render_emitter_list(ctx)

    def _node_graph_detail_contributors(
        self,
    ) -> tuple[GraphDetailContributor, ...]:
        return (
            GraphDetailContributor(
                "particle.node",
                400,
                lambda: bool(self._selected_node_uid),
                self._render_node_properties,
            ),
            GraphDetailContributor(
                "parameter",
                300,
                self._is_node_graph_parameter_detail_active,
                lambda ctx: self._render_node_graph_parameter_detail(ctx),
            ),
            GraphDetailContributor(
                "particle.event",
                200,
                lambda: bool(self._selected_event_type_id),
                self._render_event_type_properties,
            ),
            GraphDetailContributor(
                "particle.emitter",
                0,
                lambda: True,
                self._render_emitter_settings,
            ),
        )

    def _node_graph_defer_canvas_drop_target(self) -> bool:
        return True

__all__ = ["ParticleGraphEditorPanel"]
