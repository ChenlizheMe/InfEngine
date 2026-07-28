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
    "asset_ref": (0.48, 0.62, 0.90, 1.0),
    "exec": (0.76, 0.76, 0.78, 1.0),
    "event": (0.84, 0.44, 0.40, 1.0),
}

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
    property_enum_entries=None,
    display_name_override: str = "",
    hidden_property_ids=(),
    hidden_port_ids=(),
) -> NodeTypeDef:
    port_type_overrides = dict(port_type_overrides or {})
    property_enum_entries = dict(property_enum_entries or {})
    hidden_property_ids = frozenset(hidden_property_ids or ())
    hidden_port_ids = frozenset(hidden_port_ids or ())
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
                max_connections=(1 if port.direction is PortDirection.INPUT else -1),
                data_type=data_type,
                pin_category=(
                    PinCategory.DATA if port.kind is PortKind.VALUE else PinCategory.EXEC
                ),
            )
        )
    is_root = definition.type_id.startswith("particle.root.")
    is_mandatory_root = definition.type_id in {
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
            resolved_type = port_type_overrides.get(port.id) or port.value_type
            inline_fields.append(
                NodeInlineFieldDef(
                    port.id,
                    port.display_name or port.id.replace("_", " ").title(),
                    resolved_type.value_type.value if resolved_type is not None else "f32",
                    copy.deepcopy(port.default),
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
    return NodeTypeDef(
        type_id=definition.type_id,
        label=display_name_override or definition.display_name,
        header_color=root_header if is_root else (0.28, 0.31, 0.36, 1.0),
        pins=pins,
        min_width=248.0 if is_root else 210.0,
        deletable=not is_mandatory_root,
        body_bottom_pad=detached_fields * 24.0,
        visual_style="context" if is_root else "graph",
        # Node chrome shows only the display name — no MATH/COMMON chips.
        category_label="",
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

    def definition_for_type(self, type_id: str) -> NodeDef | None:
        return self._definitions.get(type_id)

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
        self._dynamic_type_cache: dict[str, NodeTypeDef] = {}
        self._documents = {stage: getattr(emitter, stage) for stage in self.STAGES}
        self._creatable_type_ids: list[str] = []
        self._allowed_stages: dict[str, set[str]] = {}
        self._authoring_stage = "init"
        self._pending_creation_stage = ""

        for definition in registry.definitions():
            stages = {
                stage
                for stage in self.STAGES
                if particle_stage_definition_filter(f"particle.{stage}")(definition)
            }
            if (
                definition_set is not None
                and definition.type_id in definition_set.event_source_by_type_id
            ):
                source_emitter_id, source_stage = (
                    definition_set.event_source_by_type_id[definition.type_id]
                )
                stages = (
                    {source_stage}
                    if source_emitter_id == emitter.stable_id and source_stage in stages
                    else set()
                )
            if (
                definition_set is not None
                and definition.type_id in definition_set.event_target_by_type_id
            ):
                target_emitter_id = definition_set.event_target_by_type_id[
                    definition.type_id
                ]
                stages = {"init"} if target_emitter_id == emitter.stable_id else set()
            if not stages:
                continue
            self.register_type(_canvas_definition(definition))
            self._allowed_stages[definition.type_id] = stages
            if (
                not definition.type_id.startswith("particle.root.")
                or definition.type_id in _PARTICLE_COLLISION_ROOT_TYPES
            ):
                self._creatable_type_ids.append(definition.type_id)

        for stage in self.STAGES:
            document = self._documents[stage]
            if document is None:
                continue
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

    def prepare_node_creation(self, stage: str) -> None:
        self._pending_creation_stage = stage if stage in self.STAGES else ""

    def set_collision_enabled(self, enabled: bool) -> None:
        self._collision_enabled = bool(enabled)
        self._dynamic_type_cache.clear()

    def _mark_attribute_catalog_dirty(self) -> None:
        self._attribute_catalog_dirty = True
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
        if type_id not in _PARTICLE_COLLISION_ROOT_TYPES:
            return True, ""
        if not self._collision_enabled:
            return False, "Enable Collision in Emitter Settings first"
        if any(node.type_id == type_id for node in self.nodes):
            return False, "This collision lifecycle root already exists"
        return True, ""

    def stage_nearest_y(self, y: float) -> str:
        return min(self.STAGES, key=lambda stage: abs(float(y) - self._STAGE_Y[stage]))

    def registered_types(self) -> list[NodeTypeDef]:
        return [
            definition
            for type_id in self._creatable_type_ids
            if (definition := self.get_type(type_id)) is not None
        ]

    def definition_for_type(self, type_id: str) -> NodeDef | None:
        return self._definitions.get(type_id)

    def attribute_entries(self) -> tuple[tuple[str, str], ...]:
        self._ensure_attribute_catalog()
        return tuple(
            (item.name.replace("_", " ").title(), item.stable_id)
            for item in self._attribute_catalog.values()
        )

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

    def get_node_type(self, node) -> NodeTypeDef | None:
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

        base = super().get_node_type(node)
        if base is None:
            return base
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
                    and (
                        node.type_id != "particle.attribute.cache"
                        or cache_value_connected
                    )
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
            "particle.parameter.get",
        }:
            return with_lifecycle_state(base)
        is_parameter = node.type_id == "particle.parameter.get"
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
        cache_key = f"{property_id}:{selected_id}:sampled={int(sampled)}"
        cached = self._dynamic_type_cache.get(cache_key)
        if cached is not None:
            return with_lifecycle_state(cached)
        selected = (
            self._parameter_catalog.get(selected_id)
            if is_parameter
            else self._attribute_catalog.get(selected_id)
        )
        definition = self._definitions.get(node.type_id)
        if selected is None or definition is None:
            return with_lifecycle_state(base)
        resolved = _canvas_definition(
            definition,
            port_type_overrides={"value": selected.value_type},
            property_enum_entries={
                property_id: (
                    self.parameter_entries()
                    if is_parameter
                    else self.attribute_entries()
                )
            },
            display_name_override=selected.name if is_parameter else "",
            hidden_property_ids={"parameter"} if is_parameter else (),
            hidden_port_ids={"out"} if not is_parameter and not sampled else (),
        )
        self._dynamic_type_cache[cache_key] = resolved
        return with_lifecycle_state(resolved)

    def remove_invalid_links_for_node(self, node_uid: str) -> tuple[str, ...]:
        self._mark_attribute_catalog_dirty()
        removed = []
        kept = []
        for link in self.links:
            if node_uid not in {link.source_node, link.target_node}:
                kept.append(link)
                continue
            if self.validate_link(
                link.source_node,
                link.source_pin,
                link.target_node,
                link.target_pin,
                ignore_link_uid=link.uid,
            ):
                kept.append(link)
            else:
                removed.append(link.uid)
        self.links = kept
        return tuple(removed)

    def _stage_for_new_node(self, type_id: str, y: float) -> str:
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
        enabled, reason = self.node_creation_state(type_id)
        if not enabled:
            raise ValueError(reason)
        stage = self._stage_for_new_node(type_id, float(y))
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
        if type_id == "particle.parameter.get" and self._parameter_catalog:
            properties["parameter"] = next(iter(self._parameter_catalog))
        properties.update(data)
        node = super().add_node(type_id, x, y, uid=canvas_uid, **properties)
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
        source_type = self._effective_port_type(source, source_port)
        target_type = self._effective_port_type(target, target_port)
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
                ValueType.TEXTURE2D,
            }:
                return LinkValidationResult(
                    False,
                    "unsupported_cache_type",
                    "Attribute Cache requires a GPU-storable value",
                )
            target_type = source_type
        if (
            source_port.kind is PortKind.VALUE
            and source_type is not None
            and target_type is not None
            and not PORTABLE_TYPE_SYSTEM.can_connect(source_type, target_type)
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
        from Infernux.particle.asset import particle_attribute_zero

        target.data["value_type"] = value_type.value_type.value
        target.data["value_space"] = value_type.space.value
        target.data["value"] = copy.deepcopy(particle_attribute_zero(value_type))
        if value_type.value_type is ValueType.TEXTURE2D:
            target.data["composition"] = "set"
        self._mark_attribute_catalog_dirty()

    def add_link(self, src_node, src_pin, dst_node, dst_pin, uid=None, **data):
        stage = self.stage_for_uid(src_node)
        raw_uid = str(uid) if uid else uuid.uuid4().hex[:8]
        canvas_uid = raw_uid if self.stage_for_uid(raw_uid) else self._canvas_uid(stage, raw_uid)
        link = super().add_link(
            src_node, src_pin, dst_node, dst_pin, uid=canvas_uid, **data
        )
        if link is not None:
            self._infer_cache_type(src_node, src_pin, dst_node, dst_pin)
            self._mark_attribute_catalog_dirty()
        return link

    def replace_link(self, link_uid, src_node, src_pin, dst_node, dst_pin):
        target = self.find_node(dst_node)
        previous = copy.deepcopy(target.data) if target is not None else None
        self._infer_cache_type(src_node, src_pin, dst_node, dst_pin)
        link = super().replace_link(
            link_uid, src_node, src_pin, dst_node, dst_pin
        )
        if link is None and target is not None and previous is not None:
            target.data = previous
        self._mark_attribute_catalog_dirty()
        return link

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
    """Return the common/particle node palette allowed in one particle stage."""

    stage = str(domain).removeprefix("particle.")

    def _accept(definition: NodeDef) -> bool:
        type_id = definition.type_id
        if type_id.startswith("common."):
            return True
        if type_id.startswith("particle.event.output."):
            return type_id.startswith(f"particle.event.output.{stage}.")
        if type_id.startswith("particle.event.payload."):
            return stage == "init"
        if type_id == "particle.context.delta_time":
            return stage in {
                "update",
                "collision_enter",
                "collision_stay",
                "collision_exit",
            }
        if stage in {
            "init",
            "update",
            "collision_enter",
            "collision_stay",
            "collision_exit",
            "rendering",
        } and type_id.startswith(
            (
                "particle.attribute.",
                "particle.control.",
                "particle.context.",
                "particle.parameter.",
                "particle.vector_field.",
            )
        ):
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
