"""Mutable authoring bridge for strict :mod:`Infernux.graph` documents.

The compiler-facing graph format is intentionally immutable.  The shared
node canvas still consumes the older mutable ``NodeGraph`` protocol, so this
adapter is the single conversion boundary used by modern graph editors.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterable

from Infernux.core.node_graph import (
    LinkValidationResult,
    NodeGraph,
    NodeInlineFieldDef,
    NodeTypeDef,
    PinCategory,
    PinDef as CanvasPinDef,
    PinKind as CanvasPinKind,
)
from Infernux.graph.document import (
    GraphDocument,
    GraphLinkRecord,
    GraphNodeRecord,
)
from Infernux.graph.registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    NodeDefinitionRegistry,
    PortDimensionPolicy,
    PortDirection,
    PortKind,
)
from Infernux.graph.parameters import graph_parameter_allows_hdr
from Infernux.graph.types import CoordinateSpace, PORTABLE_TYPE_SYSTEM, TypeRef, ValueType


_PIN_COLORS = {
    "bool": (0.78, 0.25, 0.31, 1.0),
    "f32": (0.34, 0.72, 0.42, 1.0),
    "i32": (0.30, 0.68, 0.52, 1.0),
    "u32": (0.30, 0.68, 0.52, 1.0),
    "vec2": (0.30, 0.70, 0.86, 1.0),
    "vec3": (0.90, 0.58, 0.24, 1.0),
    "vec4": (0.72, 0.45, 0.88, 1.0),
    "color": (0.88, 0.78, 0.28, 1.0),
    "curve": (0.42, 0.78, 0.62, 1.0),
    "gradient": (0.86, 0.48, 0.68, 1.0),
    "texture2d": (0.62, 0.48, 0.86, 1.0),
    "asset_ref": (0.48, 0.62, 0.90, 1.0),
    "exec": (0.76, 0.76, 0.78, 1.0),
    "event": (0.84, 0.44, 0.40, 1.0),
}


def _value_port_accepts(source_type: TypeRef, target_type: TypeRef, target_port) -> bool:
    """Mirror ExpressionCompiler numeric port coercion during authoring."""
    policy = target_port.dimension_policy
    if policy is PortDimensionPolicy.EXACT:
        return PORTABLE_TYPE_SYSTEM.can_connect(source_type, target_type)
    try:
        if policy is PortDimensionPolicy.FIXED:
            PORTABLE_TYPE_SYSTEM.fixed_numeric_target(source_type, target_type)
            return True
        if policy is PortDimensionPolicy.PROMOTE:
            return PORTABLE_TYPE_SYSTEM.can_resize_numeric(source_type, target_type)
    except TypeError:
        return False
    return False


def _compatible_creation_pin(
    model,
    type_id: str,
    request: dict,
    *,
    source_definition,
    source_port_type,
):
    """Return the first palette pin that a live drag source can feed."""
    if source_definition is None:
        return None
    source_port = source_definition.port(str(request.get("source_pin", "")))
    target_def = model.definition_for_type(str(type_id))
    canvas = model.get_type(str(type_id))
    if source_port is None or target_def is None or canvas is None:
        return None
    want_input = request.get("source_kind") != CanvasPinKind.INPUT
    for port in target_def.ports:
        if port.kind is not source_port.kind:
            continue
        if want_input and port.direction is not PortDirection.INPUT:
            continue
        if not want_input and port.direction is not PortDirection.OUTPUT:
            continue
        if (
            source_port.kind is PortKind.VALUE
            and source_port_type is not None
            and port.value_type is not None
            and not _value_port_accepts(source_port_type, port.value_type, port)
        ):
            continue
        pin = next((item for item in canvas.pins if item.id == port.id), None)
        if pin is not None:
            return pin
    return None

_PARTICLE_COLLISION_ROOT_TYPES = frozenset(
    {
        "particle.root.collision_enter",
        "particle.root.collision_stay",
        "particle.root.collision_exit",
    }
)

# Wait and Until are first-class lifecycle operations backed by independent
# Vulkan continuation lanes for Init, Update, Rendering, and Collision.
_PROPERTY_ENUM_VALUES = {
    "alignment": ("camera_plane", "camera_position", "axis", "velocity"),
    "sort": ("none", "back_to_front", "front_to_back"),
    "uv_mode": ("stretch", "repeat"),
}

_PROPERTY_VISIBILITY = {
    "alignment_axis": ("alignment", "axis"),
}


def _canvas_type_name(port) -> str:
    if port.kind is not PortKind.VALUE:
        return port.kind.value
    if port.value_type is not None:
        return port.value_type.value_type.value
    return "any"


def _canvas_definition(
    definition: NodeDef,
    *,
    port_type_overrides=None,
    inline_type_overrides=None,
    property_enum_entries=None,
    display_name_override: str = "",
    hidden_property_ids=(),
    hidden_port_ids=(),
    inline_field_hdr=None,
) -> NodeTypeDef:
    port_type_overrides = dict(port_type_overrides or {})
    inline_type_overrides = dict(inline_type_overrides or {})
    property_enum_entries = dict(property_enum_entries or {})
    hidden_property_ids = frozenset(hidden_property_ids or ())
    hidden_port_ids = frozenset(hidden_port_ids or ())
    inline_field_hdr = dict(inline_field_hdr or {})
    pins = []
    for port in definition.ports:
        if port.id in hidden_port_ids:
            continue
        resolved_type = port_type_overrides.get(port.id)
        data_type = (
            resolved_type.value_type.value
            if resolved_type is not None
            else _canvas_type_name(port)
        )
        pins.append(
            CanvasPinDef(
                id=port.id,
                label=port.display_name or port.id.replace("_", " ").title(),
                kind=(
                    CanvasPinKind.INPUT
                    if port.direction is PortDirection.INPUT
                    else CanvasPinKind.OUTPUT
                ),
                color=_PIN_COLORS.get(data_type, (0.72, 0.72, 0.74, 1.0)),
                max_connections=(
                    port.max_connections
                    if port.max_connections is not None
                    else 1
                    if port.direction is PortDirection.INPUT
                    else -1
                ),
                data_type=data_type,
                pin_category=(
                    PinCategory.DATA if port.kind is PortKind.VALUE else PinCategory.EXEC
                ),
            )
        )
    is_event_root = definition.type_id == "particle.event.active" or definition.type_id.startswith(
        "internal.particle.event.active."
    )
    is_root = definition.type_id.startswith("particle.root.") or is_event_root
    is_mandatory_root = is_event_root or definition.type_id in {
        "particle.root.init",
        "particle.root.update",
        "particle.root.rendering",
    }
    property_by_id = {item.id: item for item in definition.properties}
    inline_fields = [
        NodeInlineFieldDef(
            item.id,
            item.id.replace("_", " ").title(),
            item.value_type.value_type.value,
            copy.deepcopy(item.default),
            asset_type="Material" if item.id == "material" else "",
            enum_values=tuple(
                value
                for _label, value in property_enum_entries.get(
                    item.id,
                    item.choices
                    or tuple(
                        (value, value)
                        for value in _PROPERTY_ENUM_VALUES.get(item.id, ())
                    ),
                )
            ),
            enum_labels=tuple(
                label
                for label, _value in property_enum_entries.get(
                    item.id,
                    item.choices
                    or tuple(
                        (value, value)
                        for value in _PROPERTY_ENUM_VALUES.get(item.id, ())
                    ),
                )
            ),
            visible_when_field=_PROPERTY_VISIBILITY.get(item.id, ("", None))[0],
            visible_when_value=_PROPERTY_VISIBILITY.get(item.id, ("", None))[1],
            hdr=inline_field_hdr.get(
                item.id,
                item.value_type.value_type.value == "color",
            ),
        )
        for item in definition.properties
        if item.id not in hidden_property_ids | {"composition"}
        if item.value_type.value_type not in {ValueType.CURVE, ValueType.GRADIENT}
    ]
    for port in definition.ports:
        if (
            port.direction is PortDirection.INPUT
            and port.kind is PortKind.VALUE
            and not port.required
            and port.id not in property_by_id
        ):
            resolved_type = (
                inline_type_overrides.get(port.id)
                or port_type_overrides.get(port.id)
                or port.value_type
            )
            if (
                resolved_type is not None
                and resolved_type.value_type
                in {ValueType.CURVE, ValueType.GRADIENT}
            ):
                continue
            resolved_type_name = (
                resolved_type.value_type.value if resolved_type is not None else "f32"
            )
            inline_fields.append(
                NodeInlineFieldDef(
                    port.id,
                    port.display_name or port.id.replace("_", " ").title(),
                    resolved_type_name,
                    copy.deepcopy(port.default),
                    hdr=inline_field_hdr.get(
                        port.id,
                        resolved_type_name == "color",
                    ),
                )
            )
    input_ids = {
        port.id
        for port in definition.ports
        if port.direction is PortDirection.INPUT and port.kind is PortKind.VALUE
    }
    detached_fields = sum(1 for item in inline_fields if item.id not in input_ids)
    # Unity VFX Graph–style stage colours: Initialize / Update / Output.
    root_header = (0.22, 0.46, 0.38, 1.0)
    if definition.type_id.endswith(".init"):
        root_header = (0.18, 0.42, 0.55, 1.0)
    elif definition.type_id.endswith(".update"):
        root_header = (0.42, 0.34, 0.18, 1.0)
    elif definition.type_id.endswith(".rendering"):
        root_header = (0.46, 0.24, 0.28, 1.0)
    elif is_event_root:
        root_header = (0.48, 0.28, 0.54, 1.0)
    presentation = definition.presentation
    default_header = root_header if is_root else (0.28, 0.31, 0.36, 1.0)
    default_min_width = 248.0 if is_root else 210.0
    default_deletable = not is_mandatory_root
    default_body_bottom_pad = detached_fields * 24.0
    default_visual_style = "context" if is_root else "graph"
    return NodeTypeDef(
        type_id=definition.type_id,
        label=display_name_override or definition.display_name,
        header_color=presentation.header_color or default_header,
        pins=pins,
        min_width=(
            default_min_width
            if presentation.min_width is None
            else presentation.min_width
        ),
        deletable=(
            default_deletable
            if presentation.deletable is None
            else presentation.deletable
        ),
        body_bottom_pad=(
            default_body_bottom_pad
            if presentation.body_bottom_pad is None
            else presentation.body_bottom_pad
        ),
        visual_style=presentation.visual_style or default_visual_style,
        # Node chrome shows only the display name — no MATH/COMMON chips.
        category_label=presentation.category_label,
        show_header_color_swatch=(
            False
            if presentation.show_header_color_swatch is None
            else presentation.show_header_color_swatch
        ),
        inline_fields=inline_fields,
    )


def _authoring_defaults(definition: NodeDef) -> dict:
    values = {
        item.id: copy.deepcopy(item.default) for item in definition.properties
    }
    for port in definition.ports:
        if (
            port.direction is PortDirection.INPUT
            and port.kind is PortKind.VALUE
            and not port.required
            and port.id not in values
        ):
            values[port.id] = copy.deepcopy(port.default)
    return values


class GraphDocumentAuthoringModel(NodeGraph):
    """Editable canvas model that round-trips a strict ``GraphDocument``."""

    def __init__(
        self,
        document: GraphDocument,
        *,
        registry: NodeDefinitionRegistry = COMMON_NODE_REGISTRY,
        definition_filter: Callable[[NodeDef], bool] | None = None,
    ) -> None:
        super().__init__(graph_kind=document.domain)
        self._domain = document.domain
        self._metadata = copy.deepcopy(dict(document.metadata))
        self._definitions = registry
        self._creatable_type_ids: list[str] = []

        for definition in registry.definitions():
            if definition_filter is not None and not definition_filter(definition):
                continue
            self.register_type(_canvas_definition(definition))
            if not definition.type_id.startswith("particle.root."):
                self._creatable_type_ids.append(definition.type_id)

        for record in document.nodes:
            if self.get_type(record.type_id) is None:
                definition = registry.get(record.type_id)
                if definition is not None:
                    self.register_type(_canvas_definition(definition))
            super().add_node(
                record.type_id,
                record.position[0],
                record.position[1],
                uid=record.uid,
                **copy.deepcopy(dict(record.properties)),
            )
        for record in document.links:
            self.links.append(
                self._make_link(
                    record.source_node,
                    record.source_port,
                    record.target_node,
                    record.target_port,
                    record.uid,
                )
            )

    @staticmethod
    def _make_link(source_node, source_pin, target_node, target_pin, uid):
        from Infernux.core.node_graph import GraphLink

        return GraphLink(
            uid=uid,
            source_node=source_node,
            source_pin=source_pin,
            target_node=target_node,
            target_pin=target_pin,
        )

    def registered_types(self) -> list[NodeTypeDef]:
        return [
            definition
            for type_id in self._creatable_type_ids
            if (definition := self.get_type(type_id)) is not None
        ]

    def definition_for_type(self, type_id: str) -> NodeDef | None:
        return self._definitions.get(type_id)

    def _effective_port_type(self, node, port):
        if port is None or port.kind is not PortKind.VALUE:
            return None
        if port.value_type is not None:
            return port.value_type
        if port.type_property and node is not None:
            selected = str(node.data.get(port.type_property, "") or "")
            if port.type_property == "target_space":
                try:
                    return TypeRef(ValueType.VEC3, CoordinateSpace(selected or "world"))
                except ValueError:
                    return None
        return None

    def compatible_creation_pin(self, type_id: str, request: dict):
        """Return the first palette pin that the live drag source can feed."""
        if not request.get("source_node"):
            return None
        source_node = self.find_node(str(request.get("source_node", "")))
        source_def = (
            self._definitions.get(source_node.type_id) if source_node is not None else None
        )
        source_port = source_def.port(str(request.get("source_pin", ""))) if source_def else None
        return _compatible_creation_pin(
            self,
            type_id,
            request,
            source_definition=source_def,
            source_port_type=self._effective_port_type(source_node, source_port),
        )

    def add_node(
        self, type_id: str, canvas_x=0.0, canvas_y=0.0, uid=None, **data
    ):
        if type_id not in self._creatable_type_ids:
            raise ValueError(f"node type {type_id!r} cannot be created in {self._domain!r}")
        definition = self._definitions.get(type_id)
        if definition is None:
            raise ValueError(f"unknown graph node type {type_id!r}")
        properties = _authoring_defaults(definition)
        properties.update(data)
        return super().add_node(
            type_id, canvas_x, canvas_y, uid=uid, **properties
        )

    def remove_node(self, uid: str) -> bool:
        node = self.find_node(uid)
        definition = self.get_type(node.type_id) if node is not None else None
        if definition is not None and not definition.deletable:
            return False
        return super().remove_node(uid)

    def validate_link(
        self,
        src_node: str,
        src_pin: str,
        dst_node: str,
        dst_pin: str,
        *,
        ignore_link_uid: str = "",
    ) -> LinkValidationResult:
        basic = super().validate_link(
            src_node,
            src_pin,
            dst_node,
            dst_pin,
            ignore_link_uid=ignore_link_uid,
        )
        if not basic and basic.code != "type_mismatch":
            return basic

        source = self.find_node(src_node)
        target = self.find_node(dst_node)
        source_def = self._definitions.get(source.type_id) if source else None
        target_def = self._definitions.get(target.type_id) if target else None
        source_port = source_def.port(src_pin) if source_def else None
        target_port = target_def.port(dst_pin) if target_def else None
        if source_port is None or target_port is None:
            return LinkValidationResult(False, "missing_port", "Link endpoint port does not exist")
        if source_port.kind is not target_port.kind:
            return LinkValidationResult(False, "kind_mismatch", "Graph port kinds do not match")
        if (
            source_port.kind is PortKind.VALUE
            and source_port.value_type is not None
            and target_port.value_type is not None
        ):
            if _value_port_accepts(
                source_port.value_type, target_port.value_type, target_port
            ):
                return LinkValidationResult(True)
            return LinkValidationResult(False, "type_mismatch", "Graph value types do not match")
        if not basic:
            return basic
        return LinkValidationResult(True)

    def to_document(self) -> GraphDocument:
        records = tuple(
            GraphNodeRecord(
                node.uid,
                node.type_id,
                (node.pos_x, node.pos_y),
                copy.deepcopy(node.data),
            )
            for node in self.nodes
        )
        links = []
        for link in self.links:
            source = self.find_node(link.source_node)
            definition = self._definitions.get(source.type_id) if source else None
            port = definition.port(link.source_pin) if definition else None
            if port is None:
                raise ValueError(f"link {link.uid!r} has an unknown source port")
            links.append(
                GraphLinkRecord(
                    link.uid,
                    link.source_node,
                    link.source_pin,
                    link.target_node,
                    link.target_pin,
                    port.kind,
                )
            )
        return GraphDocument(self._domain, records, tuple(links), self._metadata)


class ParticleEmitterGraphAuthoringModel(NodeGraph):
    """One authoring canvas backed by an emitter's lifecycle documents.

    Stage documents remain the compiler contract.  This adapter namespaces their
    identities on the mutable canvas, keeps every context chain independent,
    and splits them back into strict documents when the editor saves.
    """

    STAGES = (
        "init",
        "update",
        "collision_enter",
        "collision_stay",
        "collision_exit",
        "rendering",
    )
    _STAGE_Y = {
        "init": 0.0,
        "update": 230.0,
        "collision_enter": 460.0,
        "collision_stay": 690.0,
        "collision_exit": 920.0,
        "rendering": 1150.0,
    }
    _UID_SEPARATOR = "::"

    def __init__(
        self,
        emitter,
        *,
        registry: NodeDefinitionRegistry = COMMON_NODE_REGISTRY,
        definition_set=None,
    ) -> None:
        super().__init__(graph_kind="particle.emitter")
        from Infernux.particle.asset import optional_particle_attributes

        if definition_set is not None:
            registry = definition_set.registry
        self._definitions = registry
        self._definition_set = definition_set
        self._definition_set_resolver = None
        self._emitter_id = str(emitter.stable_id)
        self._collision_enabled = bool(emitter.settings.collision_enabled)
        self._base_attribute_catalog = {
            attribute.stable_id: attribute
            for attribute in (*emitter.attributes, *optional_particle_attributes())
        }
        self._attribute_catalog = dict(self._base_attribute_catalog)
        self._attribute_catalog_dirty = True
        self._parameter_catalog = (
            dict(definition_set.parameter_by_id)
            if definition_set is not None
            else {}
        )
        self._event_catalog = (
            dict(definition_set.event_type_by_id)
            if definition_set is not None
            else {}
        )
        self._dynamic_type_cache: dict[str, NodeTypeDef] = {}
        event_stages = tuple(
            f"event.{flow.stable_id}" for flow in emitter.event_flows
        )
        self._stages = (*self.STAGES, *event_stages)
        self._stage_y = dict(self._STAGE_Y)
        next_y = max(self._STAGE_Y.values()) + 230.0
        for index, stage in enumerate(event_stages):
            self._stage_y[stage] = next_y + index * 230.0
        self._documents = {stage: getattr(emitter, stage) for stage in self.STAGES}
        self._documents.update(
            {
                f"event.{flow.stable_id}": flow.graph
                for flow in emitter.event_flows
            }
        )
        self._creatable_type_ids: list[str] = []
        self._allowed_stages: dict[str, set[str]] = {}
        self._authoring_stage = "init"
        self._pending_creation_stage = ""

        for definition in registry.definitions():
            stages = {
                stage
                for stage in self._stages
                if particle_stage_definition_filter(
                    "particle.event" if stage.startswith("event.") else f"particle.{stage}"
                )(definition)
            }
            if definition.type_id.startswith("internal.particle.event."):
                continue
            if definition.type_id == "particle.event.active":
                stages = set(event_stages)
            if not stages:
                continue
            self.register_type(_canvas_definition(definition))
            self._allowed_stages[definition.type_id] = stages
            if (
                definition.type_id != "particle.event.active"
                and not definition.type_id.startswith("particle.root.")
                or definition.type_id in _PARTICLE_COLLISION_ROOT_TYPES
            ):
                self._creatable_type_ids.append(definition.type_id)

        for stage in self._stages:
            document = self._documents[stage]
            if document is None:
                continue
            y_offset = self._stage_y[stage]
            for record in document.nodes:
                if self.get_type(record.type_id) is None:
                    definition = registry.get(record.type_id)
                    if definition is not None:
                        self.register_type(_canvas_definition(definition))
                super().add_node(
                    record.type_id,
                    record.position[0],
                    record.position[1] + y_offset,
                    uid=self._canvas_uid(stage, record.uid),
                    **copy.deepcopy(dict(record.properties)),
                )
            for record in document.links:
                self.links.append(
                    self._make_link(
                        self._canvas_uid(stage, record.source_node),
                        record.source_port,
                        self._canvas_uid(stage, record.target_node),
                        record.target_port,
                        self._canvas_uid(stage, record.uid),
                    )
                )

        self.set_node_definition_resolver(
            self._resolve_particle_node_type,
            invalidator=self._invalidate_particle_node_definition,
        )

    @staticmethod
    def _make_link(source_node, source_pin, target_node, target_pin, uid):
        return GraphDocumentAuthoringModel._make_link(
            source_node, source_pin, target_node, target_pin, uid
        )

    @classmethod
    def _canvas_uid(cls, stage: str, uid: str) -> str:
        return f"{stage}{cls._UID_SEPARATOR}{uid}"

    @classmethod
    def _document_uid(cls, uid: str) -> str:
        return uid.split(cls._UID_SEPARATOR, 1)[1]

    @classmethod
    def stage_for_uid(cls, uid: str) -> str:
        stage = str(uid).split(cls._UID_SEPARATOR, 1)[0]
        return stage if stage in cls.STAGES or stage.startswith("event.") else ""

    @staticmethod
    def _is_portable_operator(node) -> bool:
        """Return whether a node is a stage-neutral authoring expression."""
        return bool(node is not None and str(node.type_id).startswith("common."))

    def _portable_component(self, node_uid: str) -> set[str]:
        """Collect the connected pure-operator island containing *node_uid*."""
        first = self.find_node(node_uid)
        if not self._is_portable_operator(first):
            return set()
        pending = [str(node_uid)]
        result: set[str] = set()
        while pending:
            current = pending.pop()
            if current in result:
                continue
            node = self.find_node(current)
            if not self._is_portable_operator(node):
                continue
            result.add(current)
            for link in self.links:
                if link.source_node == current:
                    other = link.target_node
                elif link.target_node == current:
                    other = link.source_node
                else:
                    continue
                if other not in result and self._is_portable_operator(
                    self.find_node(other)
                ):
                    pending.append(other)
        return result

    def _portable_component_anchors(self, component: set[str]) -> set[str]:
        anchors: set[str] = set()
        for link in self.links:
            if link.source_node in component and link.target_node not in component:
                other = link.target_node
            elif link.target_node in component and link.source_node not in component:
                other = link.source_node
            else:
                continue
            stage = self.stage_for_uid(other)
            if stage:
                anchors.add(stage)
        return anchors

    def _portable_rehome_plan(
        self, src_node: str, dst_node: str
    ) -> dict[str, str] | None:
        """Plan a stage move that lets pure operators join one lifecycle chain.

        Particle lifecycle nodes remain hard stage boundaries. Common math,
        random and vector operators are compiler-local expressions, so their
        stage is inferred from the chain they feed instead of becoming a
        permanent authoring trap based on where they were first created.
        """
        source_stage = self.stage_for_uid(src_node)
        target_stage = self.stage_for_uid(dst_node)
        if not source_stage or not target_stage:
            return None
        if source_stage == target_stage:
            return {}

        source_component = self._portable_component(src_node)
        target_component = self._portable_component(dst_node)
        if not source_component and not target_component:
            return None

        moving = source_component | target_component
        anchors = self._portable_component_anchors(source_component)
        anchors.update(self._portable_component_anchors(target_component))
        if not source_component:
            anchors.add(source_stage)
        if not target_component:
            anchors.add(target_stage)
        if len(anchors) > 1:
            return None
        destination = next(iter(anchors), source_stage)
        if any(
            destination not in self._allowed_stages.get(
                self.find_node(uid).type_id, set()
            )
            for uid in moving
        ):
            return None
        return {
            uid: destination
            for uid in moving
            if self.stage_for_uid(uid) != destination
        }

    def _apply_portable_rehome(self, plan: dict[str, str]) -> dict[str, str]:
        """Apply inferred compiler placement without moving authoring nodes.

        The stage prefix is only the persistence/compilation owner for a pure
        expression. It must never change the node's visible canvas position or
        behave as an authoring-stage classification.
        """
        if not plan:
            return {}
        node_ids = {
            old_uid: self._canvas_uid(stage, self._document_uid(old_uid))
            for old_uid, stage in plan.items()
        }
        occupied_nodes = {node.uid for node in self.nodes} - set(node_ids)
        if occupied_nodes.intersection(node_ids.values()):
            raise ValueError("portable node stage inference produced a duplicate node id")

        for node in self.nodes:
            old_uid = node.uid
            destination = plan.get(old_uid)
            if destination:
                node.uid = node_ids[old_uid]
        for link in self.links:
            link.source_node = node_ids.get(link.source_node, link.source_node)
            link.target_node = node_ids.get(link.target_node, link.target_node)

        occupied_links: set[str] = set()
        for link in self.links:
            source_stage = self.stage_for_uid(link.source_node)
            new_uid = self._canvas_uid(source_stage, self._document_uid(link.uid))
            if new_uid in occupied_links:
                raise ValueError("portable node stage inference produced a duplicate link id")
            link.uid = new_uid
            occupied_links.add(new_uid)
        self._mark_attribute_catalog_dirty()
        return node_ids

    @classmethod
    def event_stage_canvas_origin(cls, event_index: int) -> float:
        """Return the canvas Y origin for an emitter's indexed Event flow."""
        return max(cls._STAGE_Y.values()) + 230.0 + max(0, int(event_index)) * 230.0

    @property
    def authoring_stage(self) -> str:
        return self._authoring_stage

    def set_authoring_stage(self, stage: str) -> None:
        if stage in self._stages:
            self._authoring_stage = stage

    def prepare_node_creation(self, stage: str) -> None:
        self._pending_creation_stage = stage if stage in self._stages else ""

    def set_collision_enabled(self, enabled: bool) -> None:
        self._collision_enabled = bool(enabled)
        self._dynamic_type_cache.clear()

    def _mark_attribute_catalog_dirty(self) -> None:
        self._attribute_catalog_dirty = True
        self._dynamic_type_cache.clear()

    def set_definition_set_resolver(self, resolver) -> None:
        """Supply refreshed compiler definitions after particle data changes."""
        self._definition_set_resolver = resolver

    def authoring_node_payload(self, node) -> dict:
        payload = super().authoring_node_payload(node)
        payload["stage"] = self.stage_for_uid(node.uid)
        return payload

    def prepare_authoring_node_restore(self, payload: dict) -> None:
        stage = str(payload.get("stage", ""))
        if not stage:
            raise RuntimeError("Particle Graph node restore requires a lifecycle stage")
        self.set_authoring_stage(stage)
        self.prepare_node_creation(stage)

    def on_authoring_node_restored(self, node) -> None:
        self._mark_attribute_catalog_dirty()

    def on_authoring_link_restored(self, link) -> None:
        self._mark_attribute_catalog_dirty()

    def _invalidate_particle_node_definition(self, node) -> None:
        self._mark_attribute_catalog_dirty()
        if self._definition_set_resolver is None:
            return
        definition_set = self._definition_set_resolver(self, node)
        if definition_set is None:
            return
        self._definition_set = definition_set
        self._definitions = definition_set.registry
        self._parameter_catalog = dict(definition_set.parameter_by_id)
        self._event_catalog = dict(definition_set.event_type_by_id)
        self._dynamic_type_cache.clear()

    def _ensure_attribute_catalog(self) -> None:
        if not self._attribute_catalog_dirty:
            return
        from Infernux.particle.asset import (
            ParticleAttribute,
            particle_attribute_cache_id,
            particle_attribute_capture_id,
            particle_attribute_zero,
        )

        attributes = dict(self._base_attribute_catalog)
        for node in self.nodes:
            if node.type_id != "particle.attribute.cache":
                continue
            stage = self.stage_for_uid(node.uid)
            try:
                value_type = TypeRef(
                    ValueType(str(node.data.get("value_type", "f32"))),
                    CoordinateSpace(str(node.data.get("value_space", "none"))),
                )
                name = str(node.data.get("name", "Attribute Cache")).strip()
                attribute = ParticleAttribute(
                    particle_attribute_cache_id(
                        stage, self._document_uid(node.uid)
                    ),
                    name or "Attribute Cache",
                    value_type,
                    particle_attribute_zero(value_type),
                )
            except (TypeError, ValueError):
                continue
            attributes[attribute.stable_id] = attribute

        pending = [
            node
            for node in self.nodes
            if node.type_id == "particle.attribute.get"
            and any(
                link.target_node == node.uid and link.target_pin == "in"
                for link in self.links
            )
        ]
        while pending:
            progressed = False
            deferred = []
            for node in pending:
                source = attributes.get(
                    str(node.data.get("attribute", "builtin.position"))
                )
                if source is None:
                    deferred.append(node)
                    continue
                stage = self.stage_for_uid(node.uid)
                capture = ParticleAttribute(
                    particle_attribute_capture_id(
                        stage, self._document_uid(node.uid)
                    ),
                    f"{self._document_uid(node.uid)}_sample",
                    source.value_type,
                    particle_attribute_zero(source.value_type),
                )
                attributes[capture.stable_id] = capture
                progressed = True
            if not progressed:
                break
            pending = deferred

        self._attribute_catalog = attributes
        self._attribute_catalog_dirty = False

    def _cache_attribute_for_node(self, node):
        if node is None or node.type_id != "particle.attribute.cache":
            return None
        from Infernux.particle.asset import particle_attribute_cache_id

        self._ensure_attribute_catalog()
        return self._attribute_catalog.get(
            particle_attribute_cache_id(
                self.stage_for_uid(node.uid), self._document_uid(node.uid)
            )
        )

    def node_creation_state(self, type_id: str) -> tuple[bool, str]:
        if type_id == "particle.emitter.playing":
            definition = self._definitions.get(type_id)
            choices = (
                tuple(definition.properties[0].choices)
                if definition is not None and definition.properties
                else ()
            )
            if not any(value != self._emitter_id for _label, value in choices):
                return False, "Set Emitter Playing requires another emitter"
        if type_id not in _PARTICLE_COLLISION_ROOT_TYPES:
            return True, ""
        if not self._collision_enabled:
            return False, "Enable Collision in Emitter Settings first"
        if any(node.type_id == type_id for node in self.nodes):
            return False, "This collision lifecycle root already exists"
        return True, ""

    def _visible_stage_ids(self) -> tuple[str, ...]:
        collision_stages = {"collision_enter", "collision_stay", "collision_exit"}
        visible = []
        for stage in self._stages:
            if stage in collision_stages and (
                not self._collision_enabled
                or not any(
                    node.type_id == f"particle.root.{stage}" for node in self.nodes
                )
            ):
                continue
            visible.append(stage)
        return tuple(visible) or tuple(self._stages)

    def stage_nearest_y(self, y: float) -> str:
        return min(
            self._visible_stage_ids(),
            key=lambda stage: abs(float(y) - self._stage_y[stage]),
        )

    def stage_for_new_node(self, type_id: str, canvas_y: float) -> str:
        return self._stage_for_new_node(type_id, float(canvas_y))

    def registered_types(self) -> list[NodeTypeDef]:
        definitions = []
        for type_id in self._creatable_type_ids:
            if type_id == "particle.emitter.playing":
                definition = self._emitter_playing_type()
            else:
                definition = self.get_type(type_id)
            if definition is not None:
                definitions.append(definition)
        return definitions

    def definition_for_type(self, type_id: str) -> NodeDef | None:
        return self._definitions.get(type_id)

    def compatible_creation_pin(self, type_id: str, request: dict):
        """Return the first palette pin that the live drag source can feed."""
        if not request.get("source_node"):
            return None
        source_node = self.find_node(str(request.get("source_node", "")))
        source_def = self.definition_for_node(source_node)
        source_port = source_def.port(str(request.get("source_pin", ""))) if source_def else None
        return _compatible_creation_pin(
            self,
            type_id,
            request,
            source_definition=source_def,
            source_port_type=self._effective_port_type(source_node, source_port),
        )

    def event_entries(self) -> tuple[tuple[str, str], ...]:
        return (("None", ""),) + tuple(
            (item.name, item.stable_id) for item in self._event_catalog.values()
        )

    def _emitter_playing_type(self) -> NodeTypeDef | None:
        definition = self._definitions.get("particle.emitter.playing")
        if definition is None:
            return None
        cache_key = f"emitter-playing-catalog:{self._emitter_id}"
        cached = self._dynamic_type_cache.get(cache_key)
        if cached is None:
            entries = tuple(
                (label, value)
                for label, value in definition.properties[0].choices
                if value != self._emitter_id
            )
            cached = _canvas_definition(
                definition,
                property_enum_entries={"emitter": entries},
            )
            self._dynamic_type_cache[cache_key] = cached
        return cached

    def definition_for_node(self, node) -> NodeDef | None:
        if node is None:
            return None
        from Infernux.particle.nodes import (
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
            particle_event_node_definition,
            particle_output_node_definition,
        )

        if node.type_id in {
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
        }:
            if self._definition_set is None:
                return self._definitions.get(node.type_id)
            return particle_event_node_definition(
                self._definition_set,
                node.type_id,
                str(node.data.get("event", "")),
            )
        if node.type_id.startswith("particle.output.") and self._definition_set is not None:
            return particle_output_node_definition(
                self._definition_set,
                self._emitter_id,
                node,
            )
        return self._definitions.get(node.type_id)

    def attribute_entries(self) -> tuple[tuple[str, str], ...]:
        self._ensure_attribute_catalog()
        return tuple(
            (item.name.replace("_", " ").title(), item.stable_id)
            for item in self._attribute_catalog.values()
            if not item.stable_id.startswith("internal.")
        )

    def attribute_id_for_cache_node(self, node_uid: str) -> str:
        """Return the stable attribute owned by an Attribute Cache node."""
        attribute = self._cache_attribute_for_node(self.find_node(str(node_uid)))
        return str(attribute.stable_id) if attribute is not None else ""

    def attribute_cache_entries(self) -> tuple[tuple[str, str], ...]:
        self._ensure_attribute_catalog()
        return tuple(
            (item.name.replace("_", " ").title(), item.stable_id)
            for item in self._attribute_catalog.values()
            if item.stable_id.startswith("cache.")
        )

    def parameter_entries(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (item.name, item.stable_id)
            for item in self._parameter_catalog.values()
        )

    def writable_parameter_entries(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (item.name, item.stable_id)
            for item in self._parameter_catalog.values()
            if bool(getattr(item, "writable", False))
        )

    def _resolve_particle_node_type(self, node) -> NodeTypeDef | None:
        self._ensure_attribute_catalog()

        def with_lifecycle_state(value):
            if value is None:
                return None
            if (
                self.stage_for_uid(node.uid)
                not in {"collision_enter", "collision_stay", "collision_exit"}
                or self._collision_enabled
            ):
                return value
            unavailable = copy.copy(value)
            unavailable.label = f"{value.label} (Unavailable)"
            unavailable.header_color = (0.62, 0.18, 0.16, 1.0)
            return unavailable

        from Infernux.particle.nodes import (
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
        )

        if node.type_id in {
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
        }:
            event_id = str(node.data.get("event", ""))
            event_type = self._event_catalog.get(event_id)
            event_name = event_type.name if event_type is not None else "None"
            cache_key = f"event:{node.type_id}:{event_id}"
            cached = self._dynamic_type_cache.get(cache_key)
            if cached is None:
                definition = self.definition_for_node(node)
                if definition is None:
                    return None
                prefix = (
                    "Active Event"
                    if node.type_id == PARTICLE_EVENT_ACTIVE_TYPE_ID
                    else "Trigger Event"
                )
                cached = _canvas_definition(
                    definition,
                    property_enum_entries={"event": self.event_entries()},
                    display_name_override=f"{prefix}: {event_name}",
                )
                # Compiler-only definitions shape the ports; assets keep one stable type.
                cached.type_id = node.type_id
                self._dynamic_type_cache[cache_key] = cached
            return with_lifecycle_state(cached)

        if node.type_id.startswith("particle.output."):
            shader_id = str(node.data.get("shader", "Particle Unlit"))
            cache_key = f"particle-output:{node.uid}:{shader_id}"
            cached = self._dynamic_type_cache.get(cache_key)
            if cached is None:
                definition = self.definition_for_node(node)
                if definition is None:
                    return None
                cached = _canvas_definition(
                    definition,
                    property_enum_entries={
                        "shader": (
                            self._definition_set.fragment_shader_choices
                            if self._definition_set is not None
                            else ()
                        )
                    },
                )
                cached.type_id = node.type_id
                self._dynamic_type_cache[cache_key] = cached
            return with_lifecycle_state(cached)

        if node.type_id == "particle.emitter.playing":
            definition = self._definitions.get(node.type_id)
            if definition is None:
                return with_lifecycle_state(self.get_type(node.type_id))
            entries = tuple(
                (label, value)
                for label, value in definition.properties[0].choices
                if value != self._emitter_id
            )
            selected_id = str(node.data.get("emitter", ""))
            selected_name = next(
                (label for label, value in entries if value == selected_id),
                "None",
            )
            cache_key = f"emitter-playing:{self._emitter_id}:{selected_id}"
            cached = self._dynamic_type_cache.get(cache_key)
            if cached is None:
                cached = _canvas_definition(
                    definition,
                    property_enum_entries={"emitter": entries},
                    display_name_override=f"Set Emitter Playing: {selected_name}",
                )
                self._dynamic_type_cache[cache_key] = cached
            return with_lifecycle_state(cached)

        base = self.get_type(node.type_id)
        if base is None:
            return base
        if node.type_id in {
            "common.space.transform_position",
            "common.space.transform_direction",
        }:
            try:
                target_type = TypeRef(
                    ValueType.VEC3,
                    CoordinateSpace(str(node.data.get("target_space", "world"))),
                )
            except ValueError:
                return with_lifecycle_state(base)
            cache_key = f"space-transform:{node.type_id}:{target_type.space.value}"
            cached = self._dynamic_type_cache.get(cache_key)
            if cached is None:
                definition = self._definitions.get(node.type_id)
                if definition is None:
                    return with_lifecycle_state(base)
                cached = _canvas_definition(
                    definition,
                    port_type_overrides={
                        "input": TypeRef(ValueType.VEC3),
                        "value": target_type,
                    },
                )
                self._dynamic_type_cache[cache_key] = cached
            return with_lifecycle_state(cached)
        from Infernux.particle.nodes import ATTRIBUTE_NODE_NAMES

        attribute_name = ATTRIBUTE_NODE_NAMES.get(node.type_id)
        if attribute_name is not None:
            composition = str(node.data.get("composition", "set"))
            selected_cache = self._cache_attribute_for_node(node)
            cache_value_connected = (
                node.type_id == "particle.attribute.cache"
                and any(
                    link.target_node == node.uid and link.target_pin == "value"
                    for link in self.links
                )
            )
            cache_key = (
                f"attribute-write:{node.type_id}:{composition}:"
                f"{selected_cache.stable_id if selected_cache else ''}:"
                f"{selected_cache.name if selected_cache else ''}:"
                f"{selected_cache.value_type if selected_cache else ''}:"
                f"connected={int(cache_value_connected)}"
            )
            cached = self._dynamic_type_cache.get(cache_key)
            if cached is not None:
                return with_lifecycle_state(cached)
            definition = self._definitions.get(node.type_id)
            if definition is None:
                return with_lifecycle_state(base)
            resolved = _canvas_definition(
                definition,
                port_type_overrides=(
                    {"value": selected_cache.value_type}
                    if selected_cache is not None
                    else {}
                ),
                inline_type_overrides=(
                    {"value": selected_cache.value_type}
                    if selected_cache is not None
                    else {}
                ),
                display_name_override=(
                    f"{composition.replace('_', ' ').title()} "
                    f"{selected_cache.name if selected_cache else attribute_name}"
                ),
                hidden_property_ids=(
                    {"value_type", "value_space"}
                    if node.type_id == "particle.attribute.cache"
                    else ()
                ),
            )
            self._dynamic_type_cache[cache_key] = resolved
            return with_lifecycle_state(resolved)
        if node.type_id not in {
            "particle.attribute.get",
            "particle.parameter",
            "particle.parameter.set",
        }:
            return with_lifecycle_state(base)
        is_parameter = node.type_id in {"particle.parameter", "particle.parameter.set"}
        is_parameter_store = node.type_id == "particle.parameter.set"
        property_id = "parameter" if is_parameter else "attribute"
        selected_id = str(
            node.data.get(property_id, "" if is_parameter else "builtin.position")
        )
        sampled = (
            not is_parameter
            and any(
                link.target_node == node.uid
                and link.target_pin == "in"
                for link in self.links
            )
        )
        selected = (
            self._parameter_catalog.get(selected_id)
            if is_parameter
            else self._attribute_catalog.get(selected_id)
        )
        parameter_hdr = bool(
            is_parameter and selected is not None and graph_parameter_allows_hdr(selected)
        )
        cache_key = (
            f"{node.type_id}:{property_id}:{selected_id}:sampled={int(sampled)}"
            f":hdr={int(parameter_hdr)}"
        )
        cached = self._dynamic_type_cache.get(cache_key)
        if cached is not None:
            return with_lifecycle_state(cached)
        definition = self._definitions.get(node.type_id)
        if selected is None or definition is None:
            return with_lifecycle_state(base)
        resolved = _canvas_definition(
            definition,
            port_type_overrides={"value": selected.value_type},
            property_enum_entries={
                property_id: (
                    self.writable_parameter_entries()
                    if is_parameter_store
                    else self.parameter_entries()
                    if is_parameter
                    else self.attribute_entries()
                )
            },
            display_name_override=(
                f"Set Parameter: {selected.name}"
                if is_parameter_store
                else selected.name
                if is_parameter
                else ""
            ),
            hidden_property_ids=(
                {"parameter"} if is_parameter and not is_parameter_store else ()
            ),
            hidden_port_ids={"out"} if not is_parameter and not sampled else (),
            inline_field_hdr=(
                {"value": parameter_hdr}
                if is_parameter_store
                and selected.value_type.value_type is ValueType.COLOR
                else None
            ),
        )
        self._dynamic_type_cache[cache_key] = resolved
        return with_lifecycle_state(resolved)

    def remove_invalid_links_for_node(self, node_uid: str) -> tuple[str, ...]:
        self._mark_attribute_catalog_dirty()
        return super().remove_invalid_links_for_node(node_uid)

    def _stage_for_new_node(self, type_id: str, canvas_y: float) -> str:
        allowed = set(self._allowed_stages.get(type_id, set()))
        collision_stages = {"collision_enter", "collision_stay", "collision_exit"}
        if type_id not in _PARTICLE_COLLISION_ROOT_TYPES:
            allowed = {
                stage
                for stage in allowed
                if stage not in collision_stages
                or (
                    self._collision_enabled
                    and any(
                        node.type_id == f"particle.root.{stage}"
                        for node in self.nodes
                    )
                )
            }
        if not allowed:
            raise ValueError(f"node type {type_id!r} has no available lifecycle flow")
        if len(allowed) == 1:
            return next(iter(allowed))
        if self._pending_creation_stage in allowed:
            return self._pending_creation_stage
        if self._authoring_stage in allowed:
            selected_y = self._stage_y[self._authoring_stage]
            nearest = min(
                allowed,
                key=lambda stage: abs(float(canvas_y) - self._stage_y[stage]),
            )
            if abs(float(canvas_y) - self._stage_y[nearest]) + 40.0 < abs(
                float(canvas_y) - selected_y
            ):
                return nearest
            return self._authoring_stage
        return min(
            allowed,
            key=lambda stage: abs(float(canvas_y) - self._stage_y[stage]),
        )

    def add_node(
        self, type_id: str, canvas_x=0.0, canvas_y=0.0, uid=None, **data
    ):
        if type_id not in self._creatable_type_ids:
            raise ValueError(f"node type {type_id!r} cannot be created in a particle emitter")
        definition = self._definitions.get(type_id)
        if definition is None:
            raise ValueError(f"unknown graph node type {type_id!r}")
        enabled, reason = self.node_creation_state(type_id)
        if not enabled:
            raise ValueError(reason)
        stage = self._stage_for_new_node(type_id, float(canvas_y))
        self._pending_creation_stage = ""
        raw_uid = (
            f"root.{stage}"
            if type_id == f"particle.root.{stage}"
            else str(uid) if uid else uuid.uuid4().hex[:8]
        )
        canvas_uid = raw_uid if self.stage_for_uid(raw_uid) else self._canvas_uid(stage, raw_uid)
        properties = _authoring_defaults(definition)
        if type_id == "particle.attribute.cache":
            existing_names = {
                str(item.data.get("name", ""))
                for item in self.nodes
                if item.type_id == "particle.attribute.cache"
            }
            index = 1
            cache_name = "Attribute Cache"
            while cache_name in existing_names:
                index += 1
                cache_name = f"Attribute Cache {index}"
            properties.update(
                {
                    "name": cache_name,
                    "value_type": ValueType.F32.value,
                    "value_space": CoordinateSpace.NONE.value,
                    "value": 0.0,
                }
            )
        if type_id in {"particle.parameter", "particle.parameter.set"}:
            catalog = (
                {
                    stable_id: parameter
                    for stable_id, parameter in self._parameter_catalog.items()
                    if bool(getattr(parameter, "writable", False))
                }
                if type_id == "particle.parameter.set"
                else self._parameter_catalog
            )
            if catalog:
                properties["parameter"] = next(iter(catalog))
        if type_id == "particle.emitter.playing":
            choices = tuple(
                (label, value)
                for label, value in definition.properties[0].choices
                if value != self._emitter_id
            )
            if choices:
                properties["emitter"] = choices[0][1]
        properties.update(data)
        node = super().add_node(
            type_id, canvas_x, canvas_y, uid=canvas_uid, **properties
        )
        self._mark_attribute_catalog_dirty()
        self._authoring_stage = stage
        return node

    def remove_node(self, uid: str) -> bool:
        node = self.find_node(uid)
        definition = self.get_type(node.type_id) if node is not None else None
        if definition is not None and not definition.deletable:
            return False
        dependent_gets = []
        if node is not None and node.type_id == "particle.attribute.cache":
            attribute = self._cache_attribute_for_node(node)
            if attribute is not None:
                dependent_gets = [
                    item.uid
                    for item in self.nodes
                    if item.type_id == "particle.attribute.get"
                    and str(item.data.get("attribute", "")) == attribute.stable_id
                ]
        for dependent_uid in dependent_gets:
            super().remove_node(dependent_uid)
        removed = super().remove_node(uid)
        if removed:
            self._mark_attribute_catalog_dirty()
        return removed

    def _effective_port_type(self, node, port):
        self._ensure_attribute_catalog()
        if port is None or port.kind is not PortKind.VALUE:
            return None
        if node is not None and node.type_id in {
            "common.space.transform_position",
            "common.space.transform_direction",
        }:
            if port.id == "input":
                return TypeRef(ValueType.VEC3)
            if port.id == "value":
                try:
                    return TypeRef(
                        ValueType.VEC3,
                        CoordinateSpace(str(node.data.get("target_space", "world"))),
                    )
                except ValueError:
                    return None
        if port.value_type is not None:
            return port.value_type
        if port.type_property == "attribute":
            attribute = self._attribute_catalog.get(
                str(node.data.get("attribute", "builtin.position"))
            )
            return attribute.value_type if attribute is not None else None
        if port.type_property == "parameter":
            parameter = self._parameter_catalog.get(
                str(node.data.get("parameter", ""))
            )
            return parameter.value_type if parameter is not None else None
        if port.type_property == "value_type":
            try:
                return TypeRef(
                    ValueType(str(node.data.get("value_type", "f32"))),
                    CoordinateSpace(str(node.data.get("value_space", "none"))),
                )
            except ValueError:
                return None
        return None

    def validate_link(
        self,
        src_node: str,
        src_pin: str,
        dst_node: str,
        dst_pin: str,
        *,
        ignore_link_uid: str = "",
    ) -> LinkValidationResult:
        source_stage = self.stage_for_uid(src_node)
        target_stage = self.stage_for_uid(dst_node)
        rehome_plan = self._portable_rehome_plan(src_node, dst_node)
        if (
            not source_stage
            or not target_stage
            or (source_stage != target_stage and rehome_plan is None)
        ):
            return LinkValidationResult(
                False, "cross_stage", "Particle stage chains cannot be connected"
            )
        basic = super().validate_link(
            src_node,
            src_pin,
            dst_node,
            dst_pin,
            ignore_link_uid=ignore_link_uid,
        )
        inference_target = self.find_node(dst_node)
        cache_type_inference = (
            basic.code == "type_mismatch"
            and inference_target is not None
            and inference_target.type_id == "particle.attribute.cache"
            and dst_pin == "value"
        )
        if not basic and basic.code != "type_mismatch" and not cache_type_inference:
            return basic
        source = self.find_node(src_node)
        target = self.find_node(dst_node)
        source_def = self.definition_for_node(source)
        target_def = self.definition_for_node(target)
        source_port = source_def.port(src_pin) if source_def else None
        target_port = target_def.port(dst_pin) if target_def else None
        if source_port is None or target_port is None:
            return LinkValidationResult(False, "missing_port", "Link endpoint port does not exist")
        if source_port.kind is not target_port.kind:
            return LinkValidationResult(False, "kind_mismatch", "Graph port kinds do not match")
        source_type = self._effective_port_type(source, source_port)
        target_type = self._effective_port_type(target, target_port)
        if (
            target is not None
            and target.type_id
            in {
                "common.space.transform_position",
                "common.space.transform_direction",
            }
            and dst_pin == "input"
        ):
            supported_spaces = {
                CoordinateSpace.NONE,
                CoordinateSpace.EMITTER_LOCAL,
                CoordinateSpace.SIMULATION,
                CoordinateSpace.WORLD,
            }
            if source_type is not None and source_type.space not in supported_spaces:
                return LinkValidationResult(
                    False,
                    "type_mismatch",
                    "Space transforms require a spatial Vector3 input",
                )
        cache_type_inference = (
            target is not None
            and target.type_id == "particle.attribute.cache"
            and dst_pin == "value"
        )
        if cache_type_inference and source_type is not None:
            if source_type.value_type not in {
                ValueType.BOOL,
                ValueType.I32,
                ValueType.U32,
                ValueType.F32,
                ValueType.VEC2,
                ValueType.VEC3,
                ValueType.VEC4,
                ValueType.COLOR,
                ValueType.MAT3,
                ValueType.MAT4,
            }:
                return LinkValidationResult(
                    False,
                    "unsupported_cache_type",
                    "Attribute Cache requires per-particle numeric or boolean data; resources must remain graph parameters",
                )
            target_type = source_type
        if (
            source_port.kind is PortKind.VALUE
            and source_type is not None
            and target_type is not None
            and not _value_port_accepts(source_type, target_type, target_port)
        ):
            return LinkValidationResult(False, "type_mismatch", "Graph value types do not match")
        return LinkValidationResult(True)

    def _infer_cache_type(self, src_node: str, src_pin: str, dst_node: str, dst_pin: str) -> None:
        target = self.find_node(dst_node)
        if target is None or target.type_id != "particle.attribute.cache" or dst_pin != "value":
            return
        source = self.find_node(src_node)
        definition = self._definitions.get(source.type_id) if source else None
        port = definition.port(src_pin) if definition else None
        value_type = self._effective_port_type(source, port)
        if value_type is None:
            return
        from Infernux.particle.asset import (
            particle_attribute_cache_id,
            particle_attribute_zero,
        )

        cache_id = particle_attribute_cache_id(
            self.stage_for_uid(target.uid), self._document_uid(target.uid)
        )
        dependents = [
            node.uid
            for node in self.nodes
            if node.type_id == "particle.attribute.get"
            and str(node.data.get("attribute", "")) == cache_id
        ]
        self.rebuild_nodes(
            {
                target.uid: {
                    "value_type": value_type.value_type.value,
                    "value_space": value_type.space.value,
                    "value": copy.deepcopy(particle_attribute_zero(value_type)),
                }
            },
            affected_node_uids=dependents,
        )

    def add_link(self, src_node, src_pin, dst_node, dst_pin, uid=None, **data):
        checkpoint = self.capture_authoring_state()
        try:
            rehome_plan = self._portable_rehome_plan(src_node, dst_node)
            if self.stage_for_uid(src_node) != self.stage_for_uid(dst_node):
                if rehome_plan is None:
                    return None
                node_ids = self._apply_portable_rehome(rehome_plan)
                src_node = node_ids.get(src_node, src_node)
                dst_node = node_ids.get(dst_node, dst_node)
            stage = self.stage_for_uid(src_node)
            raw_uid = str(uid) if uid else uuid.uuid4().hex[:8]
            canvas_uid = (
                raw_uid
                if self.stage_for_uid(raw_uid)
                else self._canvas_uid(stage, raw_uid)
            )
            link = super().add_link(
                src_node, src_pin, dst_node, dst_pin, uid=canvas_uid, **data
            )
            if link is not None:
                self._infer_cache_type(src_node, src_pin, dst_node, dst_pin)
                self._mark_attribute_catalog_dirty()
            return self.find_link(canvas_uid) if link is not None else None
        except Exception:
            self.restore_authoring_state(checkpoint)
            raise

    def replace_link(self, link_uid, src_node, src_pin, dst_node, dst_pin):
        checkpoint = self.capture_authoring_state()
        try:
            raw_link_uid = self._document_uid(link_uid)
            rehome_plan = self._portable_rehome_plan(src_node, dst_node)
            if self.stage_for_uid(src_node) != self.stage_for_uid(dst_node):
                if rehome_plan is None:
                    return None
                node_ids = self._apply_portable_rehome(rehome_plan)
                src_node = node_ids.get(src_node, src_node)
                dst_node = node_ids.get(dst_node, dst_node)
                link_uid = self._canvas_uid(
                    self.stage_for_uid(src_node), raw_link_uid
                )
            link = super().replace_link(
                link_uid, src_node, src_pin, dst_node, dst_pin
            )
            if link is not None:
                self._infer_cache_type(src_node, src_pin, dst_node, dst_pin)
                self._mark_attribute_catalog_dirty()
            return self.find_link(link_uid) if link is not None else None
        except Exception:
            self.restore_authoring_state(checkpoint)
            raise

    def remove_link(self, uid: str) -> bool:
        link = self.find_link(uid)
        if link is None:
            return False
        sampled_node_uid = (
            link.target_node
            if link.target_pin == "in"
            and (node := self.find_node(link.target_node)) is not None
            and node.type_id == "particle.attribute.get"
            else ""
        )
        removed = super().remove_link(uid)
        if removed and sampled_node_uid:
            self.links = [
                item
                for item in self.links
                if not (
                    item.source_node == sampled_node_uid
                    and item.source_pin == "out"
                )
            ]
        if removed:
            self._mark_attribute_catalog_dirty()
        return removed

    def to_documents(self) -> dict[str, GraphDocument | None]:
        result = {}
        for stage in self._stages:
            nodes = tuple(
                GraphNodeRecord(
                    self._document_uid(node.uid),
                    node.type_id,
                    (node.pos_x, node.pos_y - self._stage_y[stage]),
                    copy.deepcopy(node.data),
                )
                for node in self.nodes
                if self.stage_for_uid(node.uid) == stage
            )
            links = []
            for link in self.links:
                if self.stage_for_uid(link.uid) != stage:
                    continue
                source = self.find_node(link.source_node)
                definition = self.definition_for_node(source)
                port = definition.port(link.source_pin) if definition else None
                if port is None:
                    raise ValueError(f"link {link.uid!r} has an unknown source port")
                links.append(
                    GraphLinkRecord(
                        self._document_uid(link.uid),
                        self._document_uid(link.source_node),
                        link.source_pin,
                        self._document_uid(link.target_node),
                        link.target_pin,
                        port.kind,
                    )
                )
            original = self._documents[stage]
            if not nodes and stage in {
                "collision_enter",
                "collision_stay",
                "collision_exit",
            }:
                result[stage] = None
                continue
            result[stage] = GraphDocument(
                (
                    original.domain
                    if original is not None
                    else f"particle.{stage}"
                ),
                nodes,
                tuple(links),
                copy.deepcopy(dict(original.metadata)) if original is not None else {},
            )
        return result


def particle_stage_definition_filter(domain: str) -> Callable[[NodeDef], bool]:
    """Return the node palette for one lifecycle flow.

    Ordinary nodes describe calculations or executable behavior and are valid
    in every lifecycle. Only structural roots, event entry points, and render
    outputs retain lifecycle ownership.
    """

    stage = str(domain).removeprefix("particle.")

    def _accept(definition: NodeDef) -> bool:
        type_id = definition.type_id
        if type_id.startswith("common."):
            return True
        if type_id.startswith("particle.root."):
            return type_id == f"particle.root.{stage}"
        if type_id == "particle.event.active":
            return stage == "event"
        if type_id.startswith("particle.output."):
            return stage == "rendering"
        return type_id.startswith("particle.")

    return _accept


__all__ = [
    "GraphDocumentAuthoringModel",
    "ParticleEmitterGraphAuthoringModel",
    "particle_stage_definition_filter",
]
