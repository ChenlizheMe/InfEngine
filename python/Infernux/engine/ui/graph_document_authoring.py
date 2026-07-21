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
    PortDirection,
    PortKind,
)
from Infernux.graph.types import PORTABLE_TYPE_SYSTEM


_PIN_COLORS = {
    "bool": (0.78, 0.25, 0.31, 1.0),
    "f32": (0.34, 0.72, 0.42, 1.0),
    "i32": (0.30, 0.68, 0.52, 1.0),
    "u32": (0.30, 0.68, 0.52, 1.0),
    "vec2": (0.90, 0.58, 0.24, 1.0),
    "vec3": (0.90, 0.58, 0.24, 1.0),
    "vec4": (0.90, 0.58, 0.24, 1.0),
    "color": (0.88, 0.78, 0.28, 1.0),
    "asset_ref": (0.48, 0.62, 0.90, 1.0),
    "stream": (0.76, 0.76, 0.78, 1.0),
    "event": (0.84, 0.44, 0.40, 1.0),
}


def _canvas_type_name(port) -> str:
    if port.kind is not PortKind.VALUE:
        return port.kind.value
    if port.value_type is not None:
        return port.value_type.value_type.value
    return "any"


def _canvas_definition(definition: NodeDef) -> NodeTypeDef:
    pins = []
    for port in definition.ports:
        data_type = _canvas_type_name(port)
        pins.append(
            CanvasPinDef(
                id=port.id,
                label=port.id.replace("_", " ").title(),
                kind=(
                    CanvasPinKind.INPUT
                    if port.direction is PortDirection.INPUT
                    else CanvasPinKind.OUTPUT
                ),
                color=_PIN_COLORS.get(data_type, (0.72, 0.72, 0.74, 1.0)),
                max_connections=(1 if port.direction is PortDirection.INPUT else -1),
                data_type=data_type,
                pin_category=(
                    PinCategory.DATA if port.kind is PortKind.VALUE else PinCategory.EXEC
                ),
            )
        )
    is_root = definition.type_id.startswith("particle.root.")
    property_by_id = {item.id: item for item in definition.properties}
    inline_fields = [
        NodeInlineFieldDef(
            item.id,
            item.id.replace("_", " ").title(),
            item.value_type.value_type.value,
            copy.deepcopy(item.default),
            asset_type="Material" if item.id == "material" else "",
            enum_values=("none", "back_to_front", "front_to_back")
            if item.id == "sort"
            else (),
        )
        for item in definition.properties
    ]
    for port in definition.ports:
        if (
            port.direction is PortDirection.INPUT
            and port.kind is PortKind.VALUE
            and not port.required
            and port.id not in property_by_id
        ):
            inline_fields.append(
                NodeInlineFieldDef(
                    port.id,
                    port.id.replace("_", " ").title(),
                    port.value_type.value_type.value if port.value_type is not None else "f32",
                    copy.deepcopy(port.default),
                )
            )
    input_ids = {
        port.id
        for port in definition.ports
        if port.direction is PortDirection.INPUT and port.kind is PortKind.VALUE
    }
    detached_fields = sum(1 for item in inline_fields if item.id not in input_ids)
    category = definition.type_id.split(".", 1)[0].upper()
    if definition.type_id.startswith("common.math."):
        category = "MATH"
    elif definition.type_id.startswith("common.vector."):
        category = "VECTOR"
    elif definition.type_id.startswith("common.constant."):
        category = "CONSTANT"
    elif is_root:
        category = "CONTEXT"
    return NodeTypeDef(
        type_id=definition.type_id,
        label=definition.display_name,
        header_color=(0.22, 0.46, 0.38, 1.0) if is_root else (0.28, 0.31, 0.36, 1.0),
        pins=pins,
        min_width=196.0 if is_root else 190.0,
        deletable=not is_root,
        body_bottom_pad=detached_fields * 24.0,
        visual_style="context" if is_root else "graph",
        category_label=category,
        show_header_color_swatch=False,
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

    def add_node(self, type_id: str, x=0.0, y=0.0, uid=None, **data):
        if type_id not in self._creatable_type_ids:
            raise ValueError(f"node type {type_id!r} cannot be created in {self._domain!r}")
        definition = self._definitions.get(type_id)
        if definition is None:
            raise ValueError(f"unknown graph node type {type_id!r}")
        properties = _authoring_defaults(definition)
        properties.update(data)
        return super().add_node(type_id, x, y, uid=uid, **properties)

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
        if not basic:
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
            and not PORTABLE_TYPE_SYSTEM.can_connect(source_port.value_type, target_port.value_type)
        ):
            return LinkValidationResult(False, "type_mismatch", "Graph value types do not match")
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
    """One authoring canvas backed by an emitter's three strict stage documents.

    Stage documents remain the compiler contract.  This adapter namespaces their
    identities on the mutable canvas, keeps the three context chains independent,
    and splits them back into strict documents when the editor saves.
    """

    STAGES = ("init", "update", "rendering")
    _STAGE_Y = {"init": 0.0, "update": 230.0, "rendering": 460.0}
    _UID_SEPARATOR = "::"

    def __init__(
        self,
        emitter,
        *,
        registry: NodeDefinitionRegistry = COMMON_NODE_REGISTRY,
    ) -> None:
        super().__init__(graph_kind="particle.emitter")
        self._definitions = registry
        self._documents = {stage: getattr(emitter, stage) for stage in self.STAGES}
        self._creatable_type_ids: list[str] = []
        self._allowed_stages: dict[str, set[str]] = {}
        self._authoring_stage = "init"

        for definition in registry.definitions():
            stages = {
                stage
                for stage in self.STAGES
                if particle_stage_definition_filter(f"particle.{stage}")(definition)
            }
            if not stages:
                continue
            self.register_type(_canvas_definition(definition))
            self._allowed_stages[definition.type_id] = stages
            if not definition.type_id.startswith("particle.root."):
                self._creatable_type_ids.append(definition.type_id)

        for stage in self.STAGES:
            document = self._documents[stage]
            y_offset = self._STAGE_Y[stage]
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
        return stage if stage in cls.STAGES else ""

    @property
    def authoring_stage(self) -> str:
        return self._authoring_stage

    def set_authoring_stage(self, stage: str) -> None:
        if stage in self.STAGES:
            self._authoring_stage = stage

    def registered_types(self) -> list[NodeTypeDef]:
        return [
            definition
            for type_id in self._creatable_type_ids
            if (definition := self.get_type(type_id)) is not None
        ]

    def _stage_for_new_node(self, type_id: str, y: float) -> str:
        allowed = self._allowed_stages.get(type_id, set())
        if len(allowed) == 1:
            return next(iter(allowed))
        if self._authoring_stage in allowed:
            selected_y = self._STAGE_Y[self._authoring_stage]
            nearest = min(allowed, key=lambda stage: abs(float(y) - self._STAGE_Y[stage]))
            if abs(float(y) - self._STAGE_Y[nearest]) + 40.0 < abs(float(y) - selected_y):
                return nearest
            return self._authoring_stage
        return min(allowed, key=lambda stage: abs(float(y) - self._STAGE_Y[stage]))

    def add_node(self, type_id: str, x=0.0, y=0.0, uid=None, **data):
        if type_id not in self._creatable_type_ids:
            raise ValueError(f"node type {type_id!r} cannot be created in a particle emitter")
        definition = self._definitions.get(type_id)
        if definition is None:
            raise ValueError(f"unknown graph node type {type_id!r}")
        stage = self._stage_for_new_node(type_id, float(y))
        properties = _authoring_defaults(definition)
        properties.update(data)
        raw_uid = str(uid) if uid else uuid.uuid4().hex[:8]
        canvas_uid = raw_uid if self.stage_for_uid(raw_uid) else self._canvas_uid(stage, raw_uid)
        node = super().add_node(type_id, x, y, uid=canvas_uid, **properties)
        self._authoring_stage = stage
        return node

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
        source_stage = self.stage_for_uid(src_node)
        target_stage = self.stage_for_uid(dst_node)
        if not source_stage or source_stage != target_stage:
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
        if not basic:
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
            and not PORTABLE_TYPE_SYSTEM.can_connect(source_port.value_type, target_port.value_type)
        ):
            return LinkValidationResult(False, "type_mismatch", "Graph value types do not match")
        return LinkValidationResult(True)

    def add_link(self, src_node, src_pin, dst_node, dst_pin, uid=None, **data):
        stage = self.stage_for_uid(src_node)
        raw_uid = str(uid) if uid else uuid.uuid4().hex[:8]
        canvas_uid = raw_uid if self.stage_for_uid(raw_uid) else self._canvas_uid(stage, raw_uid)
        return super().add_link(
            src_node, src_pin, dst_node, dst_pin, uid=canvas_uid, **data
        )

    def to_documents(self) -> dict[str, GraphDocument]:
        result = {}
        for stage in self.STAGES:
            nodes = tuple(
                GraphNodeRecord(
                    self._document_uid(node.uid),
                    node.type_id,
                    (node.pos_x, node.pos_y - self._STAGE_Y[stage]),
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
                definition = self._definitions.get(source.type_id) if source else None
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
            result[stage] = GraphDocument(
                original.domain, nodes, tuple(links), copy.deepcopy(dict(original.metadata))
            )
        return result


def particle_stage_definition_filter(domain: str) -> Callable[[NodeDef], bool]:
    """Return the common/particle node palette allowed in one particle stage."""

    stage = str(domain).removeprefix("particle.")

    def _accept(definition: NodeDef) -> bool:
        type_id = definition.type_id
        if type_id.startswith("common."):
            return True
        if type_id == f"particle.root.{stage}":
            return True
        if stage == "rendering":
            return type_id.startswith("particle.output.")
        return type_id.startswith(f"particle.{stage}.")

    return _accept


__all__ = [
    "GraphDocumentAuthoringModel",
    "ParticleEmitterGraphAuthoringModel",
    "particle_stage_definition_filter",
]
