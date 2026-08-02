"""Editor for strict ``.particlegraph`` assets and their AOT lifecycles."""

from __future__ import annotations

import copy
import json
import math
import os
import uuid
from dataclasses import replace
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.graph.registry import (
    COMMON_NODE_REGISTRY,
    PortDirection,
    PortKind,
)
from Infernux.graph.expression_ir import ExpressionCompiler
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
from Infernux.particle.artifact import ParticleArtifactRegistry
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
    particle_event_payload_port_id,
    particle_graph_node_definitions,
)

from .asset_save_dialog import AssetSaveAsDialog
from .editor_panel import EditorPanel
from .graph_document_authoring import (
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from .floating_workspace_panel import (
    FloatingOverlayState,
    begin_workspace_entry,
    finish_workspace_entry,
    paint_workspace_entry,
    render_compact_tab_bar,
    render_floating_overlay,
    render_workspace_add_header,
    update_overlay_resize_drag,
)
from .imgui_keys import KEY_DELETE, KEY_ESCAPE, KEY_F2
from .inspector_utils import preserve_ui_float_precision
from .inspector_shader_utils import get_shader_property_generation
from .node_graph_view import NodeCreationEntry, NodeGraphView
from .panel_registry import editor_panel
from .theme import Theme
from ._inspector_references import (
    _asset_guid_from_path,
    _picker_assets,
    _picker_texture_assets,
    _portable_asset_path_hint,
    _project_texture_guid_and_path,
    _resolve_project_asset_path,
    render_object_field,
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
        (f"Built-in/{name}", builtin_mesh_reference(name).to_dict())
        for name in BUILTIN_MESH_NAMES
        if not filter_text or filter_text in name.lower() or filter_text in "built-in"
    ]


def _selected_builtin_mesh(value) -> AssetReference | None:
    if type(value) is not dict:
        return None
    try:
        reference = AssetReference.from_dict(value)
        return reference if builtin_mesh_name(reference) else None
    except (TypeError, ValueError):
        return None


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
)
class ParticleGraphEditorPanel(EditorPanel):
    window_id = "particle_graph_editor"
    _HIDDEN_INTERNAL_RESOURCE_NODE_TYPES = frozenset(
        {
            "particle.vector_field.sample",
            "particle.collision.sdf",
            "particle.sdf.sample_distance",
            "particle.sdf.sample_gradient",
        }
    )

    def __init__(self):
        super().__init__(title="Particle Graph Editor", window_id=self.window_id)
        self._asset = ParticleGraphAsset()
        self._file_path = ""
        self._emitter_index = 0
        self._stage = "init"
        self._dirty = True
        self._selected_node_uid = ""
        self._selected_parameter_id = ""
        self._selected_event_type_id = ""
        self._focus_detail_name = ""
        self._renaming_parameter_id = ""
        self._parameter_rename_buffer = ""
        self._focus_parameter_rename = False
        self._workspace_tab_index = 0
        self._drag_snapshot: Optional[dict] = None
        self._draft_compile_error = ""
        self._event_type_dialog_requested = False
        self._event_type_dialog_open = False
        self._editing_event_type_id = ""
        self._event_dialog_error = ""
        self._event_type_draft = self._new_event_type_draft()
        self._save_as_dialog = AssetSaveAsDialog(
            "particle_graph.save_as", "particle graph"
        )
        self._left_overlay = FloatingOverlayState()
        self._right_overlay = FloatingOverlayState()
        self._shader_definition_generation = get_shader_property_generation()

        self._view = NodeGraphView()
        self._view.semantic_namespace = "particle_graph.canvas"
        self._view.on_node_add_request = self._on_node_add
        self._view.on_node_creation_entries = self._node_creation_entries
        self._view.on_node_creation_requested = self._on_node_creation_requested
        self._view.on_nodes_deleted = self._on_nodes_deleted
        self._view.on_link_created = self._on_link_created
        self._view.on_link_deleted = self._on_link_deleted
        self._view.on_link_replaced = self._on_link_replaced
        self._view.on_node_drag_start = self._on_node_drag_start
        self._view.on_node_drag_end = self._on_node_drag_end
        self._view.on_node_selected = self._on_node_selected
        self._view.on_node_data_changed = self._on_node_data_changed
        self._view.on_canvas_drop = self._on_canvas_drop
        self._model: ParticleEmitterGraphAuthoringModel | None = None
        self._bind_stage()

    @property
    def asset(self) -> ParticleGraphAsset:
        self._sync_model_to_asset()
        return self._asset

    def authoring_document_state(self) -> dict:
        """Return identity/dirty state without serializing the live graph model."""
        return {
            "file_path": str(self._file_path),
            "dirty": bool(self._dirty),
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
            "dirty": bool(self._dirty),
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

        builtin_reference = _selected_builtin_mesh(file_path) if key == "mesh" else None
        if builtin_reference is None and key == "mesh":
            token = str(file_path).strip()
            if token.startswith("builtin-mesh:"):
                builtin_reference = builtin_mesh_reference(
                    token.removeprefix("builtin-mesh:")
                )
            elif token in BUILTIN_MESH_NAMES:
                builtin_reference = builtin_mesh_reference(token)
        if builtin_reference is not None:
            reference = builtin_reference.to_dict()
            if node.data.get(key) == reference:
                return copy.deepcopy(reference)
            before = self._snapshot()
            node.data[key] = copy.deepcopy(reference)
            self._selected_node_uid = node.uid
            self._view.selected_nodes = [node.uid]
            stage = self._model.stage_for_uid(node.uid)
            if stage:
                self._select_stage(stage)
            self._sync_model_to_asset()
            self._mark_changed()
            self._record(f"Set Particle Graph {key}", before)
            return copy.deepcopy(reference)

        target = resolved_path(file_path)
        if not os.path.isfile(target):
            raise FileNotFoundError(f"Particle Graph asset reference not found: {file_path}")
        extension = os.path.splitext(target)[1].lower()
        if key == "mesh":
            from Infernux.core.asset_types import MESH_EXTENSIONS

            if extension not in MESH_EXTENSIONS:
                raise ValueError(
                    f"Particle Graph Mesh property requires a model asset; got {extension!r}"
                )
        elif (
            asset_port is not None
            and asset_port.value_type.value_type is ValueType.TEXTURE2D
        ):
            from Infernux.core.asset_types import IMAGE_EXTENSIONS

            if extension not in IMAGE_EXTENSIONS:
                raise ValueError(
                    "Particle Output shader Texture2D property requires an image "
                    f"asset; got {extension!r}"
                )

        guid = _asset_guid_from_path(target)
        if not guid:
            raise RuntimeError(
                f"Particle Graph asset reference is not imported and has no GUID: {file_path}"
            )
        reference = {
            "guid": guid,
            "path_hint": _portable_asset_path_hint(target),
        }
        if node.data.get(key) == reference:
            return copy.deepcopy(reference)

        before = self._snapshot()
        node.data[key] = copy.deepcopy(reference)
        self._selected_node_uid = node.uid
        self._view.selected_nodes = [node.uid]
        stage = self._model.stage_for_uid(node.uid)
        if stage:
            self._select_stage(stage)
        self._sync_model_to_asset()
        self._mark_changed()
        self._record(f"Set Particle Graph {key}", before)
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
        self._selected_node_uid = node.uid
        self._view.selected_nodes = [node.uid]
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
        self._selected_node_uid = node.uid
        self._view.selected_nodes = [node.uid]
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
        before = self._snapshot()
        if not self._model.remove_node(node.uid):
            raise ValueError(f"Particle Graph node cannot be deleted: {node_uid!r}")
        if self._selected_node_uid == node.uid:
            self._selected_node_uid = ""
            self._view.selected_nodes = []
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Delete Particle Graph node", before)
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
        before = self._snapshot()
        emitters = list(self._asset.emitters)
        emitters[index] = replace(
            emitters[index],
            data_interfaces=(*emitters[index].data_interfaces, interface),
        )
        self._asset = replace(self._asset, emitters=tuple(emitters))
        self._emitter_index = index
        self._bind_stage()
        self._mark_changed()
        self._record("Add Particle Graph Data Interface", before)
        return interface.to_dict()

    @staticmethod
    def _data_interface_reference(interface):
        return interface.texture

    @staticmethod
    def _data_interface_asset_contract(interface) -> tuple[tuple[str, ...], str]:
        if isinstance(interface, SdfVolume):
            return (".inxsdf",), "Signed Distance Field"
        if isinstance(interface, VectorField):
            return (".inxvfield",), "Vector Field"
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
        if replacement == emitter.data_interfaces[interface_index]:
            return False
        before = self._snapshot()
        interfaces = list(emitter.data_interfaces)
        interfaces[interface_index] = replacement
        emitters = list(self._asset.emitters)
        emitters[emitter_index] = replace(
            emitter, data_interfaces=tuple(interfaces)
        )
        self._asset = replace(self._asset, emitters=tuple(emitters))
        self._emitter_index = emitter_index
        self._bind_stage()
        self._mark_changed()
        self._record(description, before)
        return True

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
        target = resolved_path(file_path)
        if not os.path.isfile(target):
            raise FileNotFoundError(f"Particle Data Interface asset not found: {file_path}")
        required_extensions, asset_label = self._data_interface_asset_contract(interface)
        extension = os.path.splitext(target)[1].lower()
        if extension not in required_extensions:
            raise ValueError(
                f"{asset_label} Data Interface requires one of {required_extensions}; got {extension!r}"
            )
        guid = _asset_guid_from_path(target)
        if not guid:
            raise RuntimeError(
                f"Particle Data Interface asset is not imported and has no GUID: {file_path}"
            )
        reference = AssetReference(
            guid=guid,
            path_hint=_portable_asset_path_hint(target),
        )
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
        before = self._snapshot()
        emitters = list(self._asset.emitters)
        emitters[emitter_index] = replace(
            emitter,
            data_interfaces=tuple(
                item
                for item in emitter.data_interfaces
                if item.stable_id != interface.stable_id
            ),
        )
        self._asset = replace(self._asset, emitters=tuple(emitters))
        self._emitter_index = emitter_index
        self._bind_stage()
        self._mark_changed()
        self._record("Remove Particle Graph Data Interface", before)
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
        before = self._snapshot()
        created = self._model.add_link(
            source_uid, source_port, target_uid, target_port
        )
        if created is None:
            raise RuntimeError(
                f"Particle Graph could not connect {source_uid!r} to {target_uid!r}"
            )
        self._selected_node_uid = target_uid
        self._view.selected_nodes = [target_uid]
        stage = self._model.stage_for_uid(target_uid)
        if stage:
            self._select_stage(stage)
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Connect Particle Graph Exec", before)
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
        before = self._snapshot()
        if not self._model.remove_link(link_uid):
            raise RuntimeError(
                f"Particle Graph could not disconnect link {link_uid!r}"
            )
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Disconnect Particle Graph link", before)
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
        before = self._snapshot()
        if existing is not None:
            created = self._model.replace_link(
                existing.uid,
                endpoints[0],
                source_port,
                endpoints[1],
                target_port,
            )
        else:
            created = self._model.add_link(
                endpoints[0], source_port, endpoints[1], target_port
            )
        if created is None:
            self._apply_snapshot(before)
            raise RuntimeError("Particle Graph could not connect the value ports")
        self._selected_node_uid = endpoints[1]
        self._view.selected_nodes = [endpoints[1]]
        stage = self._model.stage_for_uid(endpoints[1])
        if stage:
            self._select_stage(stage)
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Connect Particle Graph value", before)
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
        before = self._snapshot()
        self._asset = replace(
            self._asset,
            emitters=(*self._asset.emitters, emitter),
        )
        self._emitter_index = len(self._asset.emitters) - 1
        self._stage = "init"
        self._bind_stage()
        self._mark_changed()
        self._record("Add particle emitter", before)
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

        self.select_authoring_emitter(emitter_id)
        self._sync_model_to_asset()
        before = self._snapshot()
        removed_emitter = self._asset.emitters[emitter_index]
        remaining_emitters = list(self._asset.emitters)
        del remaining_emitters[emitter_index]
        self._asset = replace(
            self._asset,
            emitters=tuple(remaining_emitters),
        )
        self._emitter_index = min(emitter_index, len(remaining_emitters) - 1)
        self._bind_stage()
        self._mark_changed()
        self._record("Remove particle emitter", before)
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
        if any(parameter.name == name for parameter in self._asset.parameters):
            raise ValueError(f"Particle parameter name already exists: {name!r}")
        kind = ValueType(str(value_type))
        if kind not in _PARAMETER_VALUE_TYPES:
            raise ValueError(f"Unsupported Particle parameter type: {kind.value!r}")
        if default is None:
            default = _event_field_default(kind)
        parameter = ParticleParameter(
            stable_id=uuid.uuid4().hex,
            name=name,
            value_type=TypeRef(kind, CoordinateSpace(str(space))),
            default=copy.deepcopy(default),
            exposed=bool(exposed),
            writable=False,
        )
        before = self._snapshot()
        self._asset = replace(
            self._asset,
            parameters=(*self._asset.parameters, parameter),
        )
        self._selected_parameter_id = parameter.stable_id
        self._selected_node_uid = ""
        self._view.selected_nodes = []
        self._bind_stage()
        self._mark_changed()
        self._record("Add Particle Graph parameter", before)
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
        parameter = next(
            (
                item
                for item in self._asset.parameters
                if item.stable_id == parameter_id
            ),
            None,
        )
        if parameter is None:
            raise KeyError(f"Particle parameter not found: {parameter_id!r}")
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise ValueError("Particle Graph node position must be finite")
        target_stage = str(stage) if stage else self._model.stage_nearest_y(float(y))
        valid_stages = set(_STAGES) | {
            f"event.{flow.stable_id}" for flow in self._selected_emitter().event_flows
        }
        if target_stage not in valid_stages:
            raise ValueError(f"Unknown Particle Graph stage: {target_stage!r}")

        before = self._snapshot()
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
            self._apply_snapshot(before)
            raise RuntimeError("Particle Graph created the parameter node in the wrong stage")
        self._selected_parameter_id = ""
        self._selected_node_uid = node.uid
        self._view.selected_nodes = [node.uid]
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Add Particle Graph parameter node", before)
        return {
            "uid": str(node.uid),
            "type_id": str(node.type_id),
            "stage": target_stage,
            "properties": copy.deepcopy(node.data),
        }

    def update_authoring_parameter(self, parameter_id: str, values: dict) -> dict:
        """Update a complete typed Blackboard field without changing its identity."""
        if type(values) is not dict or not values:
            raise ValueError("Particle parameter update must be a non-empty object")
        parameter_id = str(parameter_id)
        index = next(
            (
                index
                for index, parameter in enumerate(self._asset.parameters)
                if parameter.stable_id == parameter_id
            ),
            -1,
        )
        if index < 0:
            raise KeyError(f"Particle parameter not found: {parameter_id!r}")
        current = self._asset.parameters[index]
        allowed = {
            "name",
            "type",
            "default",
            "exposed",
            "writable",
            "category",
            "tooltip",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown Particle parameter fields: {sorted(unknown)}")
        name = str(values.get("name", current.name)).strip()
        if not name:
            raise ValueError("Particle parameter name cannot be empty")
        if any(
            item.stable_id != parameter_id and item.name == name
            for item in self._asset.parameters
        ):
            raise ValueError(f"Particle parameter name already exists: {name!r}")
        encoded_type = values.get("type", current.value_type.to_dict())
        value_type = (
            TypeRef.from_dict(encoded_type)
            if type(encoded_type) is dict
            else TypeRef(ValueType(str(encoded_type)))
        )
        writable = bool(values.get("writable", current.writable))
        if value_type.value_type in {
            ValueType.CURVE,
            ValueType.GRADIENT,
            ValueType.TEXTURE2D,
            ValueType.MESH,
        }:
            writable = False
        default = copy.deepcopy(values.get("default", current.default))
        if value_type != current.value_type and "default" not in values:
            default = _event_field_default(value_type.value_type)
        updated = ParticleParameter(
            stable_id=current.stable_id,
            name=name,
            value_type=value_type,
            default=default,
            exposed=values.get("exposed", current.exposed),
            writable=writable,
            category=str(values.get("category", current.category)),
            tooltip=str(values.get("tooltip", current.tooltip)),
        )
        if updated == current:
            return {**current.to_dict(), "changed": False}
        before = self._snapshot()
        parameters = list(self._asset.parameters)
        parameters[index] = updated
        emitters = self._asset.emitters
        if updated.value_type != current.value_type:
            emitters = tuple(
                self._disconnect_parameter_outputs(emitter, parameter_id)
                for emitter in emitters
            )
        if current.writable and not updated.writable:
            emitters = tuple(
                self._remove_parameter_store_nodes(emitter, parameter_id)
                for emitter in emitters
            )
        self._asset = replace(
            self._asset,
            parameters=tuple(parameters),
            emitters=emitters,
        )
        self._selected_parameter_id = parameter_id
        self._bind_stage()
        self._mark_changed()
        self._record("Edit Particle Graph parameter", before)
        return {**updated.to_dict(), "changed": True}

    def remove_authoring_parameter(self, parameter_id: str) -> dict:
        """Remove a Blackboard field and every node that references it."""
        parameter_id = str(parameter_id)
        removed = next(
            (
                parameter
                for parameter in self._asset.parameters
                if parameter.stable_id == parameter_id
            ),
            None,
        )
        if removed is None:
            raise KeyError(f"Particle parameter not found: {parameter_id!r}")
        before = self._snapshot()
        emitters = tuple(
            self._remove_parameter_nodes(emitter, parameter_id)
            for emitter in self._asset.emitters
        )
        self._asset = replace(
            self._asset,
            parameters=tuple(
                parameter
                for parameter in self._asset.parameters
                if parameter.stable_id != parameter_id
            ),
            emitters=emitters,
        )
        self._selected_parameter_id = ""
        self._bind_stage()
        self._mark_changed()
        self._record("Remove Particle Graph parameter", before)
        return {**removed.to_dict(), "changed": True}

    @staticmethod
    def _disconnect_parameter_outputs(
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
                    links=tuple(
                        link
                        for link in document.links
                        if not (
                            link.source_node in node_ids
                            or (
                                link.target_node in node_ids
                                and link.target_pin == "value"
                            )
                        )
                    ),
                )
        return replace(emitter, **updates) if updates else emitter

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
    def _new_event_type_draft() -> dict:
        return {"name": "Event", "capacity": 32, "fields": []}

    @staticmethod
    def _draft_from_event_type(event_type: ParticleEventType) -> dict:
        return {
            "name": event_type.name,
            "capacity": event_type.queue_capacity,
            "fields": [
                {
                    "stable_id": field.stable_id,
                    "name": field.name,
                    "value_type": field.value_type.value_type.value,
                    "default": copy.deepcopy(field.default),
                }
                for field in event_type.fields
            ],
        }

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
        before = self._snapshot()
        emitters = list(self._asset.emitters)
        emitters[index] = replace(emitter, settings=decoded)
        self._asset = replace(self._asset, emitters=tuple(emitters))
        self._emitter_index = index
        self._bind_stage()
        self._mark_changed()
        self._record("Edit emitter settings", before)
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
        before = self._snapshot()
        self._asset = replace(
            self._asset,
            event_types=(*self._asset.event_types, event_type),
        )
        self._bind_stage()
        self._mark_changed()
        self._record("Add Particle Graph event type", before)
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
        before = self._snapshot()
        self._replace_emitter(
            replace(
                emitter,
                event_flows=(
                    *emitter.event_flows,
                    ParticleEventFlow(flow_id, default_event_graph(event_type_id)),
                ),
            )
        )
        self._stage = stage
        self._bind_stage()
        if x is not None and self._model is not None:
            root = self._model.find_node(f"{stage}::root.event")
            if root is None:
                self._apply_snapshot(before)
                raise RuntimeError("Particle Graph did not create the Event root")
            root.pos_x = float(x)
            root.pos_y = float(y)
            self._sync_model_to_asset()
        root_uid = f"{stage}::root.event"
        self._selected_event_type_id = ""
        self._selected_parameter_id = ""
        self._selected_node_uid = root_uid
        self._view.selected_nodes = [root_uid]
        self._mark_changed()
        self._record("Add Particle Graph Active Event", before)
        return {"event_id": event_type_id, "flow_id": flow_id, "created": True}

    @staticmethod
    def _prune_changed_event_field_links(
        emitter: ParticleEmitterAsset,
        event_id: str,
        changed_port_ids: set[str],
    ) -> ParticleEmitterAsset:
        if not changed_port_ids:
            return emitter
        event_types = {
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
        }

        def prune_document(document: GraphDocument | None):
            if document is None:
                return None
            event_node_ids = {
                node.uid
                for node in document.nodes
                if node.type_id in event_types
                and str(node.properties.get("event", "")) == event_id
            }
            links = tuple(
                link
                for link in document.links
                if not (
                    link.source_node in event_node_ids
                    and link.source_port in changed_port_ids
                )
                and not (
                    link.target_node in event_node_ids
                    and link.target_port in changed_port_ids
                )
            )
            return replace(document, links=links) if links != document.links else document

        replacements = {}
        for stage in _STAGES:
            document = getattr(emitter, stage)
            pruned = prune_document(document)
            if pruned != document:
                replacements[stage] = pruned
        event_flows = tuple(
            replace(flow, graph=prune_document(flow.graph))
            for flow in emitter.event_flows
        )
        if event_flows != emitter.event_flows:
            replacements["event_flows"] = event_flows
        return replace(emitter, **replacements) if replacements else emitter

    @staticmethod
    def _migrate_event_trigger_literals(
        emitter: ParticleEmitterAsset,
        previous: ParticleEventType,
        updated: ParticleEventType,
    ) -> ParticleEmitterAsset:
        """Keep every Trigger Event literal valid across one schema edit."""

        previous_by_id = {field.stable_id: field for field in previous.fields}
        updated_ports = {
            particle_event_payload_port_id(field.stable_id): field
            for field in updated.fields
        }

        def migrate_document(document: GraphDocument | None):
            if document is None:
                return None
            nodes = []
            changed = False
            for node in document.nodes:
                if (
                    node.type_id != PARTICLE_EVENT_TRIGGER_TYPE_ID
                    or str(node.properties.get("event", "")) != previous.stable_id
                ):
                    nodes.append(node)
                    continue
                properties = dict(node.properties)
                for key in tuple(properties):
                    if key.startswith("payload.") and key not in updated_ports:
                        del properties[key]
                for port_id, field in updated_ports.items():
                    previous_field = previous_by_id.get(field.stable_id)
                    if (
                        previous_field is None
                        or previous_field.value_type != field.value_type
                        or port_id not in properties
                    ):
                        properties[port_id] = copy.deepcopy(field.default)
                replacement = replace(node, properties=properties)
                nodes.append(replacement)
                changed = changed or replacement != node
            return replace(document, nodes=tuple(nodes)) if changed else document

        replacements = {}
        for stage in _STAGES:
            document = getattr(emitter, stage)
            migrated = migrate_document(document)
            if migrated != document:
                replacements[stage] = migrated
        event_flows = tuple(
            replace(flow, graph=migrate_document(flow.graph))
            for flow in emitter.event_flows
        )
        if event_flows != emitter.event_flows:
            replacements["event_flows"] = event_flows
        return replace(emitter, **replacements) if replacements else emitter

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

        previous_by_id = {field.stable_id: field for field in previous.fields}
        updated_by_id = {field.stable_id: field for field in updated.fields}
        changed_field_ids = {
            stable_id
            for stable_id in set(previous_by_id) | set(updated_by_id)
            if stable_id not in previous_by_id
            or stable_id not in updated_by_id
            or previous_by_id[stable_id].value_type
            != updated_by_id[stable_id].value_type
        }
        changed_port_ids = {
            particle_event_payload_port_id(stable_id)
            for stable_id in changed_field_ids
        }
        before = self._snapshot()
        event_types = list(self._asset.event_types)
        event_types[index] = updated
        emitters = tuple(
            self._migrate_event_trigger_literals(
                self._prune_changed_event_field_links(
                    emitter, previous.stable_id, changed_port_ids
                ),
                previous,
                updated,
            )
            for emitter in self._asset.emitters
        )
        self._asset = replace(
            self._asset, event_types=tuple(event_types), emitters=emitters
        )
        self._bind_stage()
        self._mark_changed()
        self._record("Edit Particle Graph event type", before)
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
        before = self._snapshot()
        def clear_document(document: GraphDocument | None):
            if document is None:
                return None
            cleared_ids = set()
            nodes = []
            changed = False
            for node in document.nodes:
                if (
                    node.type_id not in {
                        PARTICLE_EVENT_ACTIVE_TYPE_ID,
                        PARTICLE_EVENT_TRIGGER_TYPE_ID,
                    }
                    or str(node.properties.get("event", "")) != event_type.stable_id
                ):
                    nodes.append(node)
                    continue
                properties = {
                    key: copy.deepcopy(value)
                    for key, value in node.properties.items()
                    if not str(key).startswith("payload.")
                }
                properties["event"] = ""
                replacement = replace(node, properties=properties)
                nodes.append(replacement)
                cleared_ids.add(node.uid)
                changed = changed or replacement != node
            links = tuple(
                link
                for link in document.links
                if not (
                    link.source_node in cleared_ids
                    and str(link.source_port).startswith("payload.")
                )
                and not (
                    link.target_node in cleared_ids
                    and str(link.target_port).startswith("payload.")
                )
            )
            changed = changed or links != document.links
            return (
                replace(document, nodes=tuple(nodes), links=links)
                if changed
                else document
            )

        emitters = []
        for emitter in self._asset.emitters:
            updates = {
                stage: (
                    clear_document(document)
                    if (document := getattr(emitter, stage)) is not None
                    else None
                )
                for stage in _STAGES
            }
            updates["event_flows"] = tuple(
                replace(flow, graph=clear_document(flow.graph))
                for flow in emitter.event_flows
            )
            emitters.append(replace(emitter, **updates))
        self._asset = replace(
            self._asset,
            emitters=tuple(emitters),
            event_types=tuple(
                value
                for value in self._asset.event_types
                if value.stable_id != event_type.stable_id
            ),
        )
        self._selected_node_uid = ""
        self._view.selected_nodes = []
        self._bind_stage()
        self._mark_changed()
        self._record("Remove Particle Graph event type", before)
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

        before = self._snapshot()
        for link in output_links:
            self._model.remove_link(link.uid)
        created = self._model.add_link(root_uid, "out", node.uid, "in")
        if created is None:
            self._apply_snapshot(before)
            raise RuntimeError(
                f"Particle Graph could not route Rendering to output {node_uid!r}"
            )
        self._selected_node_uid = node.uid
        self._view.selected_nodes = [node.uid]
        self._select_stage("rendering")
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Set Particle Graph rendering output", before)
        return {"node_uid": node.uid, "link_uid": created.uid, "changed": True}

    def reload_from_disk(self) -> bool:
        """Reload the current document after it has been saved cleanly."""
        if self._dirty:
            raise RuntimeError("Particle Graph must be saved before it can be reloaded")
        if not self._file_path:
            raise RuntimeError("Particle Graph has no source file to reload")
        reloaded = self._open_particlegraph(self._file_path)
        if reloaded:
            self._persist_panel_state()
        return reloaded

    def discard_unsaved_changes(self) -> dict:
        """Explicitly discard the live document through the Editor contract."""
        previous_path = self._file_path
        was_dirty = self._dirty
        if was_dirty and not self._discard_unsaved_changes():
            raise RuntimeError("Particle Graph could not discard unsaved changes")
        return {
            "discarded": bool(was_dirty),
            "previous_file_path": previous_path,
            "file_path": self._file_path,
            "dirty": self._dirty,
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
        self._model.set_authoring_stage(self._stage)
        self._view.graph = self._model
        self._view.reset_interaction_state()
        self._selected_node_uid = ""

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
        self._selected_parameter_id = ""
        self._selected_event_type_id = ""
        if index == self._emitter_index:
            self._selected_node_uid = ""
            self._view.selected_nodes = []
            return
        self._sync_model_to_asset()
        self._emitter_index = index
        self._bind_stage()

    def _select_event_type(self, event_type_id: str, *, focus_name: bool = False) -> None:
        if not any(
            item.stable_id == str(event_type_id) for item in self._asset.event_types
        ):
            return
        self._selected_event_type_id = str(event_type_id)
        self._selected_parameter_id = ""
        self._selected_node_uid = ""
        self._view.selected_nodes = []
        if focus_name:
            self._focus_detail_name = "event_type"

    def _refresh_shader_definitions_if_needed(self) -> None:
        generation = get_shader_property_generation()
        if generation == self._shader_definition_generation:
            return
        selected_uid = self._selected_node_uid
        self._sync_model_to_asset()
        self._shader_definition_generation = generation
        self._bind_stage()
        if selected_uid and self._model.find_node(selected_uid) is not None:
            self._selected_node_uid = selected_uid
            self._view.selected_nodes = [selected_uid]

    def _open_particlegraph(self, file_path: str) -> bool:
        try:
            asset = ParticleGraphAsset.load(file_path)
        except (OSError, json.JSONDecodeError, ParticleGraphSchemaError, ValueError) as exc:
            Debug.log_error(f"Failed to open Particle Graph '{file_path}': {exc}")
            return False
        self._asset = asset
        self._file_path = resolved_path(file_path)
        self._emitter_index = 0
        self._stage = "init"
        self._dirty = False
        self._draft_compile_error = ""
        self._bind_stage()
        self._sync_project_dirty_flag()
        return True

    def _save_to(self, file_path: str) -> bool:
        self._sync_model_to_asset()
        target = resolved_path(file_path)
        current = resolved_path(self._file_path) if self._file_path else ""
        if not current or not same_path(target, current):
            self._asset = replace(
                self._asset,
                name=os.path.splitext(os.path.basename(target))[0],
            )
        try:
            self._asset.save(target)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._draft_compile_error = str(exc)
            Debug.log_error(f"Failed to save Particle Graph '{target}': {exc}")
            return False

        self._file_path = target
        self._dirty = False
        self._draft_compile_error = ""
        self._sync_project_dirty_flag()
        self._persist_panel_state()
        try:
            from Infernux.core.assets import AssetManager

            AssetManager.reimport_asset(target)
        except Exception as exc:
            Debug.log_suppressed("particle_graph_editor.reimport", exc)
        return True

    def _show_save_as_dialog(self) -> None:
        safe_name = (self._asset.name or "ParticleGraph").replace(" ", "_")
        if not self._save_as_dialog.request(
            title="Save Particle Graph",
            extension="particlegraph",
            default_name=safe_name,
            current_path=self._file_path,
        ):
            Debug.log_warning(
                "[ParticleGraphEditor] No project root set - cannot save Particle Graph."
            )

    def _do_save(self) -> bool:
        if not self._file_path:
            self._show_save_as_dialog()
            return False
        return self._save_to(self._file_path)

    def handle_save_command(self, save_as: bool = False) -> bool:
        if save_as:
            self._show_save_as_dialog()
        else:
            self._do_save()
        return True

    def _discard_unsaved_changes(self) -> bool:
        if self._file_path:
            discarded = self._open_particlegraph(self._file_path)
            if discarded:
                self._persist_panel_state()
            return discarded
        self._asset = ParticleGraphAsset()
        self._emitter_index = 0
        self._stage = "init"
        self._dirty = False
        self._draft_compile_error = ""
        self._bind_stage()
        self._sync_project_dirty_flag()
        self._persist_panel_state()
        return True

    def _snapshot(self) -> dict:
        self._sync_model_to_asset()
        return {
            "asset": copy.deepcopy(self._asset.to_dict()),
            "emitter_index": self._emitter_index,
            "stage": self._stage,
            "selected_parameter_id": self._selected_parameter_id,
        }

    def _apply_snapshot(self, snapshot: dict) -> None:
        self._asset = ParticleGraphAsset.from_dict(snapshot["asset"])
        self._emitter_index = min(
            int(snapshot.get("emitter_index", 0)), len(self._asset.emitters) - 1
        )
        stage = str(snapshot.get("stage", "init"))
        valid = set(_STAGES) | {
            f"event.{flow.stable_id}"
            for flow in self._asset.emitters[self._emitter_index].event_flows
        }
        self._stage = stage if stage in valid else "init"
        selected_parameter_id = str(snapshot.get("selected_parameter_id", ""))
        self._selected_parameter_id = (
            selected_parameter_id
            if any(
                parameter.stable_id == selected_parameter_id
                for parameter in self._asset.parameters
            )
            else ""
        )
        self._dirty = True
        self._bind_stage()
        self._sync_project_dirty_flag()

    def _record(self, description: str, before: dict) -> None:
        from Infernux.engine.undo import record_node_graph_snapshot

        record_node_graph_snapshot(
            description=description,
            before_snapshot=before,
            after_snapshot=self._snapshot(),
            apply_snapshot=self._apply_snapshot,
        )

    def _mark_changed(self) -> None:
        self._dirty = True
        self._sync_project_dirty_flag()

    def _on_node_selected(self, node_uid: str) -> None:
        self._selected_node_uid = node_uid
        if node_uid:
            self._selected_parameter_id = ""
            self._selected_event_type_id = ""
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
        before = self._snapshot()
        try:
            node = self._model.add_node(type_id, x, y)
        except ValueError as exc:
            Debug.log_warning(f"Particle Graph node creation rejected: {exc}")
            return
        self._stage = self._model.stage_for_uid(node.uid) or self._stage
        output_identity = None
        if node.type_id.startswith("particle.output."):
            output_identity = (self._stage, self._model._document_uid(node.uid))
        self._sync_model_to_asset()
        if output_identity is not None:
            stage, document_uid = output_identity
            self._bind_stage()
            node = self._model.find_node(self._model._canvas_uid(stage, document_uid))
            if node is not None:
                self._selected_node_uid = node.uid
                self._view.selected_nodes = [node.uid]
        self._mark_changed()
        self._record("Add Particle Graph node", before)
        return node

    def _on_node_data_changed(self, node_uid: str, key: str, old_value, new_value) -> None:
        if self._model is None:
            return
        node = self._model.find_node(node_uid)
        if node is None or old_value == new_value:
            return
        before = self._snapshot()
        node.data[key] = copy.deepcopy(new_value)
        if key == "shader" and node.type_id.startswith("particle.output."):
            stage = self._model.stage_for_uid(node.uid)
            document_uid = self._model._document_uid(node.uid)
            self._sync_model_to_asset()
            self._bind_stage()
            replacement = self._model.find_node(
                self._model._canvas_uid(stage, document_uid)
            )
            if replacement is not None:
                definition = self._definition_for_node(replacement)
                valid_ports = {
                    port.id: port
                    for port in (definition.ports if definition is not None else ())
                    if port.direction is PortDirection.INPUT
                    and port.kind is PortKind.VALUE
                    and str(port.id).startswith("shader.")
                }
                for property_id in tuple(replacement.data):
                    property_id = str(property_id)
                    if not property_id.startswith("shader."):
                        continue
                    port = valid_ports.get(property_id)
                    value_type = (
                        self._model._effective_port_type(replacement, port)
                        if port is not None
                        else None
                    )
                    if value_type is None or ExpressionCompiler._literal_error(
                        value_type, replacement.data[property_id]
                    ):
                        del replacement.data[property_id]
                self._model.remove_invalid_links_for_node(replacement.uid)
                self._selected_node_uid = replacement.uid
                self._view.selected_nodes = [replacement.uid]
                self._sync_model_to_asset()
            self._mark_changed()
            self._record(f"Edit Particle Graph {key}", before)
            return
        if key == "event" and node.type_id in {
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
        }:
            for property_id in tuple(node.data):
                if str(property_id).startswith("payload."):
                    del node.data[property_id]
            definition = self._model.definition_for_node(node)
            if definition is not None and node.type_id == PARTICLE_EVENT_TRIGGER_TYPE_ID:
                for port in definition.ports:
                    if (
                        port.direction is PortDirection.INPUT
                        and port.kind is PortKind.VALUE
                        and str(port.id).startswith("payload.")
                    ):
                        node.data[port.id] = copy.deepcopy(port.default)
        removed_links = self._model.remove_invalid_links_for_node(node.uid)
        self._sync_model_to_asset()
        self._mark_changed()
        self._record(f"Edit Particle Graph {key}", before)
        if removed_links:
            Debug.log_warning(
                f"Particle Graph disconnected {len(removed_links)} incompatible link(s) "
                f"after {key} changed"
            )

    def _on_nodes_deleted(self, node_uids) -> None:
        if self._model is None:
            return
        before = self._snapshot()
        changed = any(self._model.remove_node(uid) for uid in node_uids)
        if changed:
            self._sync_model_to_asset()
            self._mark_changed()
            self._record("Delete Particle Graph nodes", before)

    def _on_link_created(self, src_node, src_pin, dst_node, dst_pin) -> None:
        if self._model is None:
            return
        before = self._snapshot()
        if self._model.add_link(src_node, src_pin, dst_node, dst_pin) is not None:
            self._sync_model_to_asset()
            self._mark_changed()
            self._record("Connect Particle Graph nodes", before)

    def _on_link_deleted(self, link_uid: str) -> None:
        if self._model is None:
            return
        before = self._snapshot()
        if self._model.remove_link(link_uid):
            self._sync_model_to_asset()
            self._mark_changed()
            self._record("Disconnect Particle Graph nodes", before)

    def _on_link_replaced(
        self, link_uid: str, src_node: str, src_pin: str, dst_node: str, dst_pin: str
    ) -> None:
        if self._model is None:
            return
        before = self._snapshot()
        if self._model.replace_link(
            link_uid, src_node, src_pin, dst_node, dst_pin
        ) is not None:
            self._sync_model_to_asset()
            self._mark_changed()
            self._record("Replace Particle Graph connection", before)

    def _on_node_drag_start(self, _node_uid: str) -> None:
        self._drag_snapshot = self._snapshot()

    def _on_node_drag_end(self, _node_uid: str) -> None:
        before = self._drag_snapshot
        self._drag_snapshot = None
        self._sync_model_to_asset()
        if before is not None and before != self._snapshot():
            self._mark_changed()
            self._record("Move Particle Graph node", before)

    def _add_emitter(self) -> None:
        before = self._snapshot()
        names = {emitter.name for emitter in self._asset.emitters}
        index = len(self._asset.emitters) + 1
        name = f"Emitter {index}"
        while name in names:
            index += 1
            name = f"Emitter {index}"
        self._asset = replace(
            self._asset,
            emitters=(*self._asset.emitters, ParticleEmitterAsset(name=name)),
        )
        self._emitter_index = len(self._asset.emitters) - 1
        self._stage = "init"
        self._bind_stage()
        self._mark_changed()
        self._record("Add particle emitter", before)

    def _remove_selected_emitter(self) -> None:
        if len(self._asset.emitters) <= 1:
            return
        self.remove_authoring_emitter(self._selected_emitter().stable_id)

    def _update_emitter(self, emitter: ParticleEmitterAsset, description: str) -> None:
        if emitter == self._selected_emitter():
            return
        before = self._snapshot()
        self._replace_emitter(emitter)
        self._mark_changed()
        self._record(description, before)

    def _update_settings(self, settings: EmitterSettings) -> None:
        emitter = self._selected_emitter()
        self._update_emitter(replace(emitter, settings=settings), "Edit emitter settings")
        if self._model is not None:
            self._model.set_collision_enabled(settings.collision_enabled)

    def _sync_project_dirty_flag(self) -> None:
        try:
            from Infernux.engine.project_context import set_panel_dirty

            set_panel_dirty(self.window_id, self._dirty)
        except Exception:
            pass

    def _window_title_suffix(self) -> str:
        return " *" if self._dirty else ""

    def _initial_size(self):
        return (1120, 700)

    def _empty_state_drop_types(self):
        return ["PARTICLE_GRAPH_FILE"]

    def _on_empty_state_drop(self, payload_type, payload):
        if payload_type == "PARTICLE_GRAPH_FILE" and payload:
            self._open_particlegraph(payload)

    def save_state(self) -> dict:
        data = {
            "file_path": self._file_path,
            "emitter_index": self._emitter_index,
            "stage": self._stage,
            "pan_x": self._view.pan_x,
            "pan_y": self._view.pan_y,
            "zoom": self._view.zoom,
            "dirty": bool(self._dirty),
        }
        if self._dirty:
            data["draft"] = self._snapshot()["asset"]
        return data

    def load_state(self, data: dict) -> None:
        path = str(data.get("file_path", ""))
        draft = data.get("draft")
        if bool(data.get("dirty")) and isinstance(draft, dict):
            try:
                self._asset = ParticleGraphAsset.from_dict(draft)
                self._file_path = resolved_path(path) if path else ""
                self._dirty = True
            except ParticleGraphSchemaError:
                # Drafts are transient editor state, not versioned assets. A
                # schema-breaking engine update discards them and restores the
                # authoritative saved graph instead of retaining legacy data.
                self._dirty = False
                if path and os.path.isfile(path):
                    self._open_particlegraph(path)
        elif path and os.path.isfile(path):
            self._open_particlegraph(path)
        self._emitter_index = min(
            int(data.get("emitter_index", 0)), len(self._asset.emitters) - 1
        )
        stage = str(data.get("stage", "init"))
        self._stage = stage if stage in _STAGES else "init"
        self._bind_stage()
        self._view.pan_x = float(data.get("pan_x", self._view.pan_x))
        self._view.pan_y = float(data.get("pan_y", self._view.pan_y))
        self._view.zoom = float(data.get("zoom", self._view.zoom))
        self._sync_project_dirty_flag()

    def on_disable(self) -> None:
        try:
            from Infernux.engine.project_context import set_panel_dirty

            set_panel_dirty(self.window_id, False)
        except Exception:
            pass

    def _record_document_semantics(self, ctx: InxGUIContext) -> None:
        if not bool(getattr(ctx, "semantic_capture_enabled", True)):
            return
        ctx.record_semantic_item(
            "status", self._asset.name, False, "particle_graph.document.name",
            string_value=self._asset.name,
        )
        ctx.record_semantic_item(
            "status", "Particle Graph Asset Path", False, "particle_graph.document.path",
            string_value=self._file_path,
        )
        ctx.record_semantic_item(
            "status", "Unsaved Changes", False, "particle_graph.document.dirty",
            bool_value=self._dirty,
        )
        ctx.record_semantic_item(
            "status",
            "Draft Compile Error",
            False,
            "particle_graph.document.compile_error",
            string_value=self._draft_compile_error,
        )

    def _request_event_type_dialog(self, event_type_id: str = "") -> None:
        self._editing_event_type_id = str(event_type_id)
        if self._editing_event_type_id:
            event_type = next(
                value
                for value in self._asset.event_types
                if value.stable_id == self._editing_event_type_id
            )
            self._event_type_draft = self._draft_from_event_type(event_type)
        else:
            self._event_type_draft = self._new_event_type_draft()
        self._event_dialog_error = ""
        self._event_type_dialog_open = True
        self._event_type_dialog_requested = True

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
            return list(ctx.color_edit(f"{label}{suffix}", *value, hdr=True))
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

    def _render_event_type_dialog(self, ctx: InxGUIContext) -> None:
        from .editor_modal import (
            EditorModalAction,
            begin_editor_modal,
            end_editor_modal,
            render_editor_modal_actions,
        )

        if not self._event_type_dialog_open and not self._event_type_dialog_requested:
            return
        editing = bool(self._editing_event_type_id)
        title_key = (
            "particle_graph_editor.edit_event_type_title"
            if editing
            else "particle_graph_editor.add_event_type_title"
        )
        popup_id = f"{t(title_key)}###particle_graph_event_type"
        request_open = self._event_type_dialog_requested
        self._event_type_dialog_requested = False
        if not begin_editor_modal(
            ctx,
            popup_id=popup_id,
            title=t(title_key),
            semantic_id="particle_graph.event_type.dialog",
            request_open=request_open,
            width=620.0,
            height=560.0,
        ):
            if not request_open:
                self._event_type_dialog_open = False
            return

        draft = self._event_type_draft
        draft["name"] = ctx.text_input(
            f"{t('particle_graph_editor.event_name')}##particle_event_type_name",
            str(draft["name"]),
            128,
        )
        draft["capacity"] = min(
            64,
            max(
                1,
            int(
                ctx.input_uint(
                    f"{t('particle_graph_editor.event_capacity')}##particle_event_capacity",
                    int(draft["capacity"]),
                )
            ),
            ),
        )
        ctx.separator()
        ctx.label(t("particle_graph_editor.event_payload"))
        fields_visible = ctx.begin_child("##particle_event_fields", 0.0, 320.0, True)
        try:
            if fields_visible:
                remove_index = -1
                for index, field in enumerate(draft["fields"]):
                    ctx.label(f"{t('particle_graph_editor.event_field')} {index + 1}")
                    field["name"] = ctx.text_input(
                        f"{t('particle_graph_editor.event_field_name')}##particle_event_field_{index}_name",
                        str(field["name"]),
                        128,
                    )
                    current_type = ValueType(field["value_type"])
                    type_index = ctx.combo(
                        f"{t('particle_graph_editor.event_field_type')}##particle_event_field_{index}_type",
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
                        field["value_type"] = selected_type.value
                        field["default"] = _event_field_default(selected_type)
                    field["default"] = self._render_event_field_default(
                        ctx, index, selected_type, field["default"]
                    )
                    if ctx.button(
                        f"{t('particle_graph_editor.remove_event_field')}##particle_event_field_{index}_remove"
                    ):
                        remove_index = index
                    ctx.separator()
                if remove_index >= 0:
                    del draft["fields"][remove_index]
                if ctx.button(t("particle_graph_editor.add_event_field")):
                    draft["fields"].append(
                        {
                            "stable_id": uuid.uuid4().hex,
                            "name": f"Value {len(draft['fields']) + 1}",
                            "value_type": ValueType.F32.value,
                            "default": 0.0,
                        }
                    )
        finally:
            ctx.end_child()

        if self._event_dialog_error:
            ctx.text_wrapped(self._event_dialog_error)

        def _apply() -> None:
            try:
                fields = [
                    {
                        **(
                            {"stable_id": str(field["stable_id"])}
                            if editing
                            else {}
                        ),
                        "name": str(field["name"]),
                        "type": TypeRef(
                            ValueType(field["value_type"])
                        ).to_dict(),
                        "default": copy.deepcopy(field["default"]),
                    }
                    for field in draft["fields"]
                ]
                if editing:
                    self.update_event_type(
                        self._editing_event_type_id,
                        str(draft["name"]),
                        int(draft["capacity"]),
                        fields,
                    )
                else:
                    self.add_event_type(
                        str(draft["name"]),
                        int(draft["capacity"]),
                        fields,
                    )
            except (ParticleGraphSchemaError, TypeError, ValueError) as exc:
                self._event_dialog_error = str(exc)
                return
            self._event_dialog_error = ""
            self._event_type_dialog_open = False
            self._editing_event_type_id = ""
            ctx.close_current_popup()

        def _cancel() -> None:
            self._event_type_dialog_open = False
            self._editing_event_type_id = ""
            ctx.close_current_popup()

        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction(
                    t(
                        "particle_graph_editor.save_changes"
                        if editing
                        else "particle_graph_editor.create"
                    ),
                    "save" if editing else "create",
                    _apply,
                ),
                EditorModalAction(
                    t("editor.unsaved.cancel"),
                    "cancel",
                    _cancel,
                ),
            ],
            semantic_prefix="particle_graph.event_type",
        )
        end_editor_modal(ctx)

    def _add_default_event_type(self) -> None:
        existing = {item.name for item in self._asset.event_types}
        index = len(existing) + 1
        name = "Event" if "Event" not in existing else f"Event {index}"
        while name in existing:
            index += 1
            name = f"Event {index}"
        created = self.add_event_type(name, 32, [])
        self._select_event_type(created["stable_id"], focus_name=True)

    def _render_event_page(self, ctx: InxGUIContext) -> None:
        render_workspace_add_header(
            ctx,
            t("particle_graph_editor.event_types"),
            "##particle_event_type_add",
            on_add=self._add_default_event_type,
        )
        remove_type_id = ""
        for event_type in self._asset.event_types:
            selected = event_type.stable_id == self._selected_event_type_id
            clicked, rect = begin_workspace_entry(
                ctx, f"particle_event_type_{event_type.stable_id}", selected
            )
            paint_workspace_entry(
                ctx,
                rect,
                primary=event_type.name,
                secondary=str(event_type.queue_capacity),
                dot_color=_WORKSPACE_EVENT_TYPE,
                selected=selected,
            )
            if bool(getattr(ctx, "semantic_capture_enabled", True)):
                ctx.record_semantic_item(
                    "particle_event_type",
                    event_type.name,
                    True,
                    f"particle_graph.event_type.{event_type.stable_id}",
                    numeric_value=float(event_type.queue_capacity),
                )
            if clicked:
                self._select_event_type(event_type.stable_id)
            # Drag sources bind to ImGui's last submitted item. Keep this
            # immediately after the workspace row; opening the context menu
            # first makes the source attach to a popup item instead.
            if ctx.begin_drag_drop_source():
                ctx.set_drag_drop_payload_str(
                    _EVENT_DRAG_PAYLOAD, event_type.stable_id
                )
                ctx.label(event_type.name)
                ctx.end_drag_drop_source()
            action = ""
            if ctx.begin_popup_context_item(
                f"##particle_event_type_context_{event_type.stable_id}"
            ):
                if ctx.menu_item(t("particle_graph_editor.rename")):
                    action = "rename"
                if ctx.menu_item(t("particle_graph_editor.delete")):
                    action = "remove"
                ctx.end_popup()
            if action == "rename":
                self._select_event_type(event_type.stable_id, focus_name=True)
            if action == "remove":
                remove_type_id = event_type.stable_id
            finish_workspace_entry(ctx)
        if remove_type_id:
            self.remove_event_type(remove_type_id)

        if (
            self._workspace_shortcut_pressed(ctx, KEY_DELETE)
        ):
            if self._selected_event_type_id:
                event_type_id = self._selected_event_type_id
                self._selected_event_type_id = ""
                self.remove_event_type(event_type_id)
        elif (
            self._selected_event_type_id
            and self._workspace_shortcut_pressed(ctx, KEY_F2)
        ):
            self._focus_detail_name = "event_type"

    def _next_parameter_name(self, value_type: ValueType) -> str:
        base = t(f"particle_graph_editor.type_{value_type.value}")
        existing = {parameter.name for parameter in self._asset.parameters}
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def _request_parameter_rename(self, parameter_id: str) -> None:
        parameter_id = str(parameter_id)
        parameter = next(
            (
                item
                for item in self._asset.parameters
                if item.stable_id == parameter_id
            ),
            None,
        )
        if parameter is None:
            return
        self._selected_parameter_id = parameter_id
        self._renaming_parameter_id = parameter_id
        self._parameter_rename_buffer = parameter.name
        self._focus_parameter_rename = True

    def _commit_parameter_rename(self) -> bool:
        parameter_id = self._renaming_parameter_id
        if not parameter_id:
            return False
        name = self._parameter_rename_buffer.strip()
        if not name:
            return False
        try:
            self.update_authoring_parameter(parameter_id, {"name": name})
        except (KeyError, TypeError, ValueError) as exc:
            Debug.log_warning(f"Particle parameter rename rejected: {exc}")
            return False
        self._renaming_parameter_id = ""
        self._parameter_rename_buffer = ""
        self._focus_parameter_rename = False
        return True

    def _cancel_parameter_rename(self) -> None:
        self._renaming_parameter_id = ""
        self._parameter_rename_buffer = ""
        self._focus_parameter_rename = False

    def _render_parameter_create_menu(self, ctx: InxGUIContext) -> None:
        popup_id = "##particle_parameter_create"

        def _build_popup(popup_ctx: InxGUIContext) -> None:
            for value_type in _PARAMETER_CREATE_TYPES:
                label = t(f"particle_graph_editor.type_{value_type.value}")
                if popup_ctx.menu_item(label):
                    self.add_authoring_parameter(
                        self._next_parameter_name(value_type), value_type.value
                    )
                    popup_ctx.close_current_popup()

        render_workspace_add_header(
            ctx,
            t("particle_graph_editor.parameters"),
            "##particle_parameter_add",
            popup_id=popup_id,
            build_popup=_build_popup,
        )

    @staticmethod
    def _workspace_shortcut_pressed(ctx: InxGUIContext, key: int) -> bool:
        is_focused = getattr(ctx, "is_window_focused", None)
        is_active = getattr(ctx, "is_any_item_active", None)
        is_pressed = getattr(ctx, "is_key_pressed", None)
        return bool(
            callable(is_focused)
            and callable(is_active)
            and callable(is_pressed)
            and is_focused(3)
            and not is_active()
            and is_pressed(key)
        )

    def _render_parameter_entry(
        self, ctx: InxGUIContext, parameter: ParticleParameter
    ) -> str:
        selected = parameter.stable_id == self._selected_parameter_id
        clicked, rect = begin_workspace_entry(
            ctx, f"particle_parameter_{parameter.stable_id}", selected
        )
        type_label = t(
            f"particle_graph_editor.type_{parameter.value_type.value_type.value}"
        )
        renaming = parameter.stable_id == self._renaming_parameter_id
        if renaming:
            paint_workspace_entry(
                ctx,
                rect,
                primary="",
                secondary="",
                dot_color=_PARAMETER_TYPE_COLORS[parameter.value_type.value_type],
                selected=selected,
            )
            cursor_x = ctx.get_cursor_pos_x()
            cursor_y = ctx.get_cursor_pos_y()
            ctx.set_cursor_pos_x(rect[0] - ctx.get_window_pos_x() + 18.0)
            ctx.set_cursor_pos_y(rect[1] - ctx.get_window_pos_y() + 2.0)
            ctx.set_next_item_width(max(24.0, rect[2] - rect[0] - 26.0))
            if self._focus_parameter_rename:
                ctx.set_keyboard_focus_here()
                self._focus_parameter_rename = False
            self._parameter_rename_buffer = ctx.input_text_with_hint(
                f"##particle_parameter_rename_{parameter.stable_id}",
                "",
                self._parameter_rename_buffer,
                256,
                1 << 6,
            )
            cancelled = ctx.is_key_pressed(KEY_ESCAPE)
            committed = ctx.is_item_deactivated_after_edit()
            ctx.set_cursor_pos_x(cursor_x)
            ctx.set_cursor_pos_y(cursor_y)
            if cancelled:
                self._cancel_parameter_rename()
            elif committed:
                self._commit_parameter_rename()
        else:
            paint_workspace_entry(
                ctx,
                rect,
                primary=parameter.name,
                secondary=type_label,
                dot_color=_PARAMETER_TYPE_COLORS[parameter.value_type.value_type],
                selected=selected,
            )
        if bool(getattr(ctx, "semantic_capture_enabled", True)):
            ctx.record_semantic_item(
                "particle_parameter",
                parameter.name,
                True,
                f"particle_graph.parameter.{parameter.stable_id}",
                string_value=parameter.value_type.value_type.value,
                bool_value=selected,
            )
        if clicked:
            self._selected_parameter_id = parameter.stable_id
            self._selected_node_uid = ""
            self._selected_event_type_id = ""
            self._view.selected_nodes = []
            self._workspace_tab_index = 1
        if not renaming and ctx.is_item_hovered() and ctx.is_mouse_double_clicked(0):
            self._request_parameter_rename(parameter.stable_id)
        if ctx.begin_drag_drop_source():
            ctx.set_drag_drop_payload_str(_PARAMETER_DRAG_PAYLOAD, parameter.stable_id)
            ctx.label(parameter.name)
            ctx.end_drag_drop_source()

        action = ""
        if ctx.begin_popup_context_item(
            f"##particle_parameter_context_{parameter.stable_id}"
        ):
            if ctx.menu_item(t("particle_graph_editor.rename_parameter")):
                action = "rename"
            if ctx.menu_item(t("particle_graph_editor.remove_parameter")):
                action = "remove"
            ctx.end_popup()
        finish_workspace_entry(ctx)
        return action

    def _render_parameter_page(self, ctx: InxGUIContext) -> None:
        self._render_parameter_create_menu(ctx)
        remove_id = ""
        for parameter in tuple(self._asset.parameters):
            action = self._render_parameter_entry(ctx, parameter)
            if action == "rename":
                self._request_parameter_rename(parameter.stable_id)
                break
            if action == "remove":
                remove_id = parameter.stable_id
                break
        if remove_id:
            self.remove_authoring_parameter(remove_id)
            return
        if (
            self._selected_parameter_id
            and self._workspace_shortcut_pressed(ctx, KEY_DELETE)
        ):
            self.remove_authoring_parameter(self._selected_parameter_id)
        elif (
            self._selected_parameter_id
            and self._workspace_shortcut_pressed(ctx, KEY_F2)
        ):
            self._request_parameter_rename(self._selected_parameter_id)

    def _render_emitter_page(self, ctx: InxGUIContext) -> None:
        render_workspace_add_header(
            ctx,
            t("particle_graph_editor.emitters"),
            "##particle_emitter_add",
            on_add=self._add_emitter,
        )
        remove_index = -1
        select_index = -1
        for index, emitter in enumerate(self._asset.emitters):
            selected = index == self._emitter_index
            clicked, rect = begin_workspace_entry(
                ctx, f"particle_emitter_{emitter.stable_id}", selected
            )
            paint_workspace_entry(
                ctx,
                rect,
                primary=emitter.name,
                secondary="",
                dot_color=_WORKSPACE_EMITTER_ON,
                selected=selected,
            )
            if bool(getattr(ctx, "semantic_capture_enabled", True)):
                ctx.record_semantic_item(
                    "particle_emitter",
                    emitter.name,
                    True,
                    f"particle_graph.emitter.{index}",
                    bool_value=selected,
                )
            if clicked:
                select_index = index
            action = ""
            if ctx.begin_popup_context_item(
                f"##particle_emitter_context_{emitter.stable_id}"
            ):
                if ctx.menu_item(t("particle_graph_editor.rename")):
                    action = "rename"
                if (
                    len(self._asset.emitters) > 1
                    and ctx.menu_item(t("particle_graph_editor.delete"))
                ):
                    action = "remove"
                ctx.end_popup()
            if action == "rename":
                select_index = index
                self._focus_detail_name = "emitter"
            elif action == "remove":
                remove_index = index
            finish_workspace_entry(ctx)
        # Rebinding replaces the complete authoring model. Defer it until the
        # active ImGui row and its context popup have both finished processing.
        if select_index >= 0:
            self._select_emitter(select_index)
        if remove_index >= 0:
            self._emitter_index = remove_index
            self._remove_selected_emitter()
            return
        if (
            self._workspace_shortcut_pressed(ctx, KEY_DELETE)
            and len(self._asset.emitters) > 1
        ):
            self._remove_selected_emitter()
        elif (
            self._workspace_shortcut_pressed(ctx, KEY_F2)
        ):
            self._select_emitter(self._emitter_index)
            self._focus_detail_name = "emitter"

    def _render_emitter_list(self, ctx: InxGUIContext) -> None:
        tabs = (
            (t("particle_graph_editor.emitters"), "emitters"),
            (t("particle_graph_editor.parameters"), "parameters"),
            (t("particle_graph_editor.events"), "events"),
        )
        self._workspace_tab_index = render_compact_tab_bar(
            ctx,
            "particle_graph_workspace",
            tabs,
            self._workspace_tab_index,
        )
        if self._workspace_tab_index == 0:
            self._render_emitter_page(ctx)
        elif self._workspace_tab_index == 1:
            self._render_parameter_page(ctx)
        else:
            self._render_event_page(ctx)

    def _render_parameter_properties(self, ctx: InxGUIContext) -> None:
        parameter = next(
            (
                item
                for item in self._asset.parameters
                if item.stable_id == self._selected_parameter_id
            ),
            None,
        )
        if parameter is None:
            self._selected_parameter_id = ""
            return
        ctx.label(t("particle_graph_editor.parameter_settings"))
        ctx.separator()
        changes = {}
        ctx.label(t("particle_graph_editor.name"))
        name = ctx.text_input(
            "##particle_parameter_name",
            parameter.name,
            128,
        ).strip()
        if name and name != parameter.name:
            changes["name"] = name
        type_index = _PARAMETER_VALUE_TYPES.index(parameter.value_type.value_type)
        selected_type_index = ctx.combo(
            f"{t('particle_graph_editor.parameter_type')}##particle_parameter_type",
            type_index,
            [t(f"particle_graph_editor.type_{kind.value}") for kind in _PARAMETER_VALUE_TYPES],
            -1,
        )
        selected_kind = _PARAMETER_VALUE_TYPES[
            max(0, min(selected_type_index, len(_PARAMETER_VALUE_TYPES) - 1))
        ]
        if selected_kind is not parameter.value_type.value_type:
            changes["type"] = TypeRef(selected_kind).to_dict()
            changes["default"] = _event_field_default(selected_kind)

        selected_space = (
            parameter.value_type.space
            if selected_kind is ValueType.VEC3
            and parameter.value_type.value_type is ValueType.VEC3
            else CoordinateSpace.NONE
        )
        if selected_kind is ValueType.VEC3:
            parameter_spaces = (CoordinateSpace.NONE, CoordinateSpace.WORLD)
            space_index = ctx.combo(
                f"{t('particle_graph_editor.parameter_space')}##particle_parameter_space",
                parameter_spaces.index(selected_space),
                [
                    t("particle_graph_editor.space_none"),
                    t("particle_graph_editor.space_world"),
                ],
                -1,
            )
            edited_space = parameter_spaces[
                max(0, min(space_index, len(parameter_spaces) - 1))
            ]
            if edited_space is not selected_space:
                changes["type"] = TypeRef(ValueType.VEC3, edited_space).to_dict()

        default = changes.get("default", copy.deepcopy(parameter.default))
        kind = selected_kind
        label = t("particle_graph_editor.parameter_default")
        ctx.label(label)
        if kind is ValueType.BOOL:
            edited_default = bool(
                ctx.checkbox("##particle_parameter_default_bool", bool(default))
            )
        elif kind in {ValueType.I32, ValueType.U32}:
            method = ctx.input_uint if kind is ValueType.U32 else ctx.input_int
            edited_default = int(method("##particle_parameter_default_int", int(default)))
        elif kind is ValueType.F32:
            edited_default = float(
                ctx.drag_float(
                    "##particle_parameter_default_float",
                    float(default),
                    0.05,
                    -1.0e7,
                    1.0e7,
                )
            )
        elif kind in {ValueType.TEXTURE2D, ValueType.MESH}:
            reference = AssetReference.from_dict(default)
            selected_references: list[AssetReference] = []
            is_mesh = kind is ValueType.MESH
            mesh_extensions = (".fbx", ".obj", ".gltf", ".glb", ".dae")

            def _select_resource(path):
                from Infernux.core.asset_types import IMAGE_EXTENSIONS

                if is_mesh and (builtin := _selected_builtin_mesh(path)) is not None:
                    selected_references.append(builtin)
                    return
                target = resolved_path(str(path)) if is_mesh else ""
                texture_guid = ""
                if not is_mesh:
                    texture_guid, target = _project_texture_guid_and_path(path)
                extensions = mesh_extensions if is_mesh else IMAGE_EXTENSIONS
                if os.path.splitext(target)[1].lower() not in extensions:
                    Debug.log_warning(
                        f"Particle {kind.value} parameter received an incompatible asset: {path}"
                    )
                    return
                if not is_mesh and not texture_guid:
                    Debug.log_warning(
                        f"Particle texture parameter must use a project asset: {path}"
                    )
                    return
                guid = _asset_guid_from_path(target) if is_mesh else texture_guid
                if not guid:
                    Debug.log_warning(
                        f"Particle {kind.value} parameter asset is not imported: {path}"
                    )
                    return
                selected_references.append(
                    AssetReference(guid, _portable_asset_path_hint(target))
                )

            def _resource_picker(query):
                from Infernux.core.asset_types import IMAGE_EXTENSIONS

                items = []
                extensions = mesh_extensions if is_mesh else IMAGE_EXTENSIONS
                if is_mesh:
                    items.extend(_builtin_mesh_picker_items(query))
                if is_mesh:
                    for extension in sorted(extensions):
                        items.extend(_picker_assets(query, f"*{extension}"))
                else:
                    items.extend(_picker_texture_assets(query))
                return items

            ping_path = str(reference.path_hint or "").strip()
            if ping_path:
                try:
                    ping_path = resolved_path(ping_path) or ping_path
                except Exception:
                    pass
            render_object_field(
                ctx,
                f"particle_parameter_default_{kind.value}",
                _mesh_reference_display(reference)
                if is_mesh
                else os.path.basename(reference.path_hint)
                if reference.path_hint
                else t("igui.none"),
                "Mesh" if is_mesh else "Texture",
                accept_drag_type=(
                    ("MODEL_GUID", "MODEL_FILE", "ASSET_FILE")
                    if is_mesh
                    else ("TEXTURE_GUID", "TEXTURE_FILE", "ASSET_FILE")
                ),
                on_drop_callback=_select_resource,
                picker_asset_items=_resource_picker,
                on_pick=_select_resource,
                on_clear=lambda: selected_references.append(AssetReference()),
                ping_path=ping_path or None,
                semantic_id=f"particle_graph.parameter.{parameter.stable_id}.default",
            )
            edited_default = (
                selected_references[-1].to_dict()
                if selected_references
                else reference.to_dict()
            )
        elif kind is ValueType.CURVE:
            edited_default = self._render_curve_property(
                ctx,
                f"parameter_{parameter.stable_id}",
                "default",
                default,
                semantic_prefix=(
                    f"particle_graph.parameter.{parameter.stable_id}.default"
                ),
            )
        elif kind is ValueType.GRADIENT:
            edited_default = self._render_gradient_property(
                ctx,
                f"parameter_{parameter.stable_id}",
                "default",
                default,
                semantic_prefix=(
                    f"particle_graph.parameter.{parameter.stable_id}.default"
                ),
            )
        else:
            edited_default = [
                float(
                    ctx.drag_float(
                        f"{label} {axis}##particle_parameter_default_{axis}",
                        float(component),
                        0.05,
                        -1.0e7,
                        1.0e7,
                    )
                )
                for axis, component in zip("XYZW", default)
            ]
        if kind not in {
            ValueType.TEXTURE2D,
            ValueType.MESH,
            ValueType.CURVE,
            ValueType.GRADIENT,
        }:
            edited_default = preserve_ui_float_precision(edited_default, default)
        if edited_default != default:
            changes["default"] = edited_default

        exposed = bool(
            ctx.checkbox(
                f"{t('particle_graph_editor.parameter_exposed')}##particle_parameter_exposed",
                parameter.exposed,
            )
        )
        if exposed != parameter.exposed:
            changes["exposed"] = exposed
        writable_supported = kind not in {
            ValueType.CURVE,
            ValueType.GRADIENT,
            ValueType.TEXTURE2D,
            ValueType.MESH,
        }
        writable = bool(
            ctx.checkbox(
                f"{t('particle_graph_editor.parameter_writable')}##particle_parameter_writable",
                parameter.writable if writable_supported else False,
            )
        )
        if writable_supported and writable != parameter.writable:
            changes["writable"] = writable
        elif not writable_supported and parameter.writable:
            changes["writable"] = False
        tooltip = ctx.text_input(
            f"{t('particle_graph_editor.parameter_tooltip')}##particle_parameter_tooltip",
            parameter.tooltip,
            512,
        )
        if tooltip != parameter.tooltip:
            changes["tooltip"] = tooltip
        if changes:
            try:
                self.update_authoring_parameter(parameter.stable_id, changes)
            except (TypeError, ValueError) as exc:
                Debug.log_warning(f"Particle parameter edit rejected: {exc}")

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
            self._selected_event_type_id = ""
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

            def _select_mesh(path):
                from Infernux.core.asset_types import MESH_EXTENSIONS

                if (builtin := _selected_builtin_mesh(path)) is not None:
                    selected_meshes.append(builtin)
                    return
                target = resolved_path(str(path))
                if os.path.splitext(target)[1].lower() not in MESH_EXTENSIONS:
                    Debug.log_warning(f"Particle emitter shape requires a model asset: {path}")
                    return
                guid = _asset_guid_from_path(target)
                if not guid:
                    Debug.log_warning(f"Particle emitter shape asset is not imported: {path}")
                    return
                selected_meshes.append(
                    AssetReference(guid, _portable_asset_path_hint(target))
                )

            def _mesh_picker(query):
                from Infernux.core.asset_types import MESH_EXTENSIONS

                items = []
                items.extend(_builtin_mesh_picker_items(query))
                for extension in sorted(MESH_EXTENSIONS):
                    items.extend(_picker_assets(query, f"*{extension}"))
                return items

            _mesh_ping = str(mesh.path_hint or "").strip()
            if _mesh_ping:
                try:
                    _mesh_ping = resolved_path(_mesh_ping) or _mesh_ping
                except Exception:
                    pass
            render_object_field(
                ctx,
                "particle_emitter_shape_mesh",
                _mesh_reference_display(mesh),
                "Mesh",
                accept_drag_type=("MODEL_GUID", "MODEL_FILE", "ASSET_FILE"),
                on_drop_callback=_select_mesh,
                picker_asset_items=_mesh_picker,
                on_pick=_select_mesh,
                on_clear=lambda: selected_meshes.append(AssetReference()),
                ping_path=_mesh_ping or None,
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
            required_extensions, asset_label = self._data_interface_asset_contract(
                replacement
            )

            def _select_asset(path, *, _extensions=required_extensions):
                target = resolved_path(str(path))
                if os.path.splitext(target)[1].lower() not in _extensions:
                    Debug.log_warning(
                        f"Particle Data Interface requires one of {_extensions}: {path}"
                    )
                    return
                guid = _asset_guid_from_path(target)
                if not guid:
                    Debug.log_warning(
                        f"Particle Data Interface asset is not imported: {path}"
                    )
                    return
                selected_references.append(
                    AssetReference(
                        guid=guid,
                        path_hint=_portable_asset_path_hint(target),
                    )
                )

            _iface_ping = str(path_hint or "").strip()
            if _iface_ping:
                try:
                    _iface_ping = resolved_path(_iface_ping) or _iface_ping
                except Exception:
                    pass
            render_object_field(
                ctx,
                f"particle_interface_asset_{interface.stable_id}",
                os.path.basename(path_hint) if path_hint else t("igui.none"),
                asset_label,
                accept_drag_type=("TEXTURE_GUID", "TEXTURE_FILE", "ASSET_FILE"),
                on_drop_callback=_select_asset,
                picker_asset_items=lambda query: [
                    item
                    for item in _picker_assets(query, "*.*")
                    if os.path.splitext(str(item[1]))[1].lower() in required_extensions
                ],
                on_pick=_select_asset,
                on_clear=lambda: selected_references.append(AssetReference()),
                ping_path=_iface_ping or None,
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
            before = self._snapshot()
            self._replace_emitter(
                replace(self._selected_emitter(), data_interfaces=tuple(interfaces))
            )
            self._bind_stage()
            self._mark_changed()
            self._record("Edit Particle Graph Data Interfaces", before)

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
        changed = False
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
            elif value_type in {ValueType.VEC2, ValueType.VEC3, ValueType.VEC4, ValueType.COLOR}:
                new_value = [
                    float(ctx.drag_float(f"{label} {axis}##particle_node_{key}_{axis}", float(component), 0.05, -1.0e7, 1.0e7))
                    for axis, component in zip("XYZW", value)
                ]
            elif value_type is ValueType.TEXTURE2D:
                reference = AssetReference.from_dict(value)
                selected_references: list[AssetReference] = []

                def _select_texture(path):
                    from Infernux.core.asset_types import IMAGE_EXTENSIONS

                    guid, target = _project_texture_guid_and_path(path)
                    if not guid:
                        Debug.log_warning(
                            f"Particle Output texture must use a project asset: {path}"
                        )
                        return
                    selected_references.append(
                        AssetReference(guid, _portable_asset_path_hint(target))
                    )

                def _texture_picker(query):
                    return _picker_texture_assets(query)

                ping_path = str(reference.path_hint or "").strip()
                if ping_path:
                    try:
                        ping_path = _resolve_project_asset_path(ping_path) or ping_path
                    except Exception:
                        pass
                render_object_field(
                    ctx,
                    f"particle_node_{key}",
                    os.path.basename(reference.path_hint)
                    if reference.path_hint
                    else (reference.guid or t("igui.none")),
                    "Texture",
                    accept_drag_type=("TEXTURE_GUID", "TEXTURE_FILE", "ASSET_FILE"),
                    on_drop_callback=_select_texture,
                    picker_asset_items=_texture_picker,
                    on_pick=_select_texture,
                    on_clear=lambda: selected_references.append(AssetReference()),
                    ping_path=ping_path or None,
                    semantic_id=semantic_id,
                )
                if selected_references:
                    new_value = selected_references[-1].to_dict()
            elif value_type in {ValueType.ASSET_REF, ValueType.MESH}:
                reference = dict(value)
                mesh_reference = (
                    AssetReference.from_dict(reference)
                    if value_type is ValueType.MESH or key == "mesh"
                    else None
                )
                path_hint = str(reference.get("path_hint", "") or "")
                selected_reference = []

                is_mesh = value_type is ValueType.MESH or key == "mesh"
                display = (
                    _mesh_reference_display(mesh_reference)
                    if is_mesh
                    else os.path.basename(path_hint) if path_hint else t("igui.none")
                )
                asset_kind = "Mesh" if is_mesh else "Material"
                drag_types = (
                    ("MODEL_GUID", "MODEL_FILE", "ASSET_FILE")
                    if is_mesh
                    else ("MATERIAL_FILE", "ASSET_FILE")
                )

                def _select_asset(path):
                    if is_mesh and (builtin := _selected_builtin_mesh(path)) is not None:
                        selected_reference.append(builtin.to_dict())
                        return
                    normalized = str(path).replace("\\", "/")
                    selected_reference.append(
                        {"guid": _asset_guid_from_path(str(path)), "path_hint": normalized}
                    )

                def _picker(query):
                    if not is_mesh:
                        return _picker_assets(query, "*.mat")
                    from Infernux.core.asset_types import MESH_EXTENSIONS

                    items = _builtin_mesh_picker_items(query)
                    for extension in sorted(MESH_EXTENSIONS):
                        items.extend(_picker_assets(query, f"*{extension}"))
                    return items

                _node_ping = str(path_hint or "").strip()
                if _node_ping:
                    try:
                        _node_ping = resolved_path(_node_ping) or _node_ping
                    except Exception:
                        pass
                render_object_field(
                    ctx,
                    f"particle_node_{key}",
                    display,
                    asset_kind,
                    accept_drag_type=drag_types,
                    on_drop_callback=_select_asset,
                    picker_asset_items=_picker,
                    on_pick=_select_asset,
                    on_clear=lambda: selected_reference.append({"guid": "", "path_hint": ""}),
                    ping_path=_node_ping or None,
                    semantic_id=f"particle_graph.node.{node.uid}.property.{key}",
                )
                if selected_reference:
                    new_value = selected_reference[-1]
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
                if key in {"attribute", "parameter"}:
                    self._on_node_data_changed(node.uid, key, value, new_value)
                    return
                else:
                    node.data[key] = new_value
                    changed = True
        if changed:
            self._sync_model_to_asset()
            self._mark_changed()

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

    def on_render_content(self, ctx: InxGUIContext):
        self._refresh_shader_definitions_if_needed()
        save_label = t("particle_graph_editor.save")
        if ctx.button(save_label):
            self._do_save()
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
        ctx.separator()

        available_w = ctx.get_content_region_avail_width()
        available_h = ctx.get_content_region_avail_height()
        # Floating overlays: keep the graph full-bleed, dock compact panels to
        # the top-left (emitters/events) and top-right (node/emitter params).
        sidebar_w = min(240.0, max(180.0, available_w * 0.18))
        detail_w = min(380.0, max(320.0, available_w * 0.30))
        margin = 8.0
        max_overlay_h = max(1.0, available_h - margin * 2.0)
        default_h = min(
            max(160.0, available_h * 0.52),
            max_overlay_h,
        )
        if self._left_overlay.height <= 0.0:
            self._left_overlay.height = default_h
        if self._right_overlay.height <= 0.0:
            self._right_overlay.height = default_h
        self._left_overlay.height = update_overlay_resize_drag(
            ctx,
            self._left_overlay,
            avail_h=available_h,
            margin=margin,
        )
        self._right_overlay.height = update_overlay_resize_drag(
            ctx,
            self._right_overlay,
            avail_h=available_h,
            margin=margin,
        )

        graph_visible = ctx.begin_child("##particle_graph", available_w, available_h, False)
        try:
            if graph_visible:
                self._view.render(ctx, defer_canvas_drop_target=True)
                render_floating_overlay(
                    ctx,
                    self._left_overlay,
                    child_id="##particle_emitters",
                    x=margin,
                    y=margin,
                    width=sidebar_w,
                    max_height=max_overlay_h,
                    render_fn=lambda: self._render_emitter_list(ctx),
                )
                render_floating_overlay(
                    ctx,
                    self._right_overlay,
                    child_id="##particle_details",
                    x=max(margin, available_w - detail_w - margin),
                    y=margin,
                    width=detail_w,
                    max_height=max_overlay_h,
                    render_fn=lambda: (
                        self._render_node_properties(ctx)
                        if self._selected_node_uid
                        else self._render_parameter_properties(ctx)
                        if self._selected_parameter_id
                        and self._workspace_tab_index == 1
                        else self._render_event_type_properties(ctx)
                        if self._selected_event_type_id
                        and self._workspace_tab_index == 2
                        else self._render_emitter_settings(ctx)
                    ),
                )
                self._view.render_canvas_drop_target(ctx)
        finally:
            ctx.end_child()

        self._save_as_dialog.render(ctx, self._save_to)


__all__ = ["ParticleGraphEditorPanel"]
