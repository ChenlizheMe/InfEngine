"""Editor for strict ``.particlegraph`` assets and their three AOT stages."""

from __future__ import annotations

import copy
import json
import math
import os
import time
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
from Infernux.graph.types import CoordinateSpace, ValueType
from Infernux.lib import InxGUIContext
from Infernux.particle.asset import (
    EmitterSettings,
    EmitterShape,
    EmitterShapeKind,
    ExecutionTarget,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ParticleGraphSchemaError,
    ScalarRange,
    SimulationSpace,
)
from Infernux.particle.artifact import ParticleArtifactRegistry
from Infernux.particle.nodes import particle_graph_node_definitions

from .asset_save_dialog import AssetSaveAsDialog
from .editor_panel import EditorPanel
from .graph_document_authoring import (
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from .node_graph_view import NodeGraphView
from .panel_registry import editor_panel
from ._inspector_references import (
    _asset_guid_from_path,
    _picker_assets,
    _portable_asset_path_hint,
    render_object_field,
)


_STAGES = ("init", "update", "rendering")


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
        kind = "combo" if key == "sort" else "text_input"
        ctx.record_semantic_item(
            kind, label, True, semantic_id, string_value=str(value)
        )


@editor_panel(
    "Particle Graph Editor",
    type_id="particle_graph_editor",
    title_key="panel.particle_graph_editor",
    menu_path="Rendering",
)
class ParticleGraphEditorPanel(EditorPanel):
    window_id = "particle_graph_editor"

    def __init__(self):
        super().__init__(title="Particle Graph Editor", window_id=self.window_id)
        self._asset = ParticleGraphAsset()
        self._file_path = ""
        self._emitter_index = 0
        self._stage = "init"
        self._dirty = True
        self._selected_node_uid = ""
        self._drag_snapshot: Optional[dict] = None
        self._draft_compile_due_at = 0.0
        self._save_as_dialog = AssetSaveAsDialog(
            "particle_graph.save_as", "particle graph"
        )

        self._view = NodeGraphView()
        self._view.semantic_namespace = "particle_graph.canvas"
        self._view.on_node_add_request = self._on_node_add
        self._view.on_node_creation_requested = self._on_node_creation_requested
        self._view.on_nodes_deleted = self._on_nodes_deleted
        self._view.on_link_created = self._on_link_created
        self._view.on_link_deleted = self._on_link_deleted
        self._view.on_link_replaced = self._on_link_replaced
        self._view.on_node_drag_start = self._on_node_drag_start
        self._view.on_node_drag_end = self._on_node_drag_end
        self._view.on_node_selected = self._on_node_selected
        self._view.on_node_data_changed = self._on_node_data_changed
        self._model: ParticleEmitterGraphAuthoringModel | None = None
        self._bind_stage()

    @property
    def asset(self) -> ParticleGraphAsset:
        self._sync_model_to_asset()
        return self._asset

    def authoring_snapshot(self) -> dict:
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
            "nodes": nodes,
            "links": links,
        }

    def set_node_asset_reference(
        self, node_uid: str, property_name: str, file_path: str
    ) -> dict:
        """Edit an AssetRef through the live authoring model and undo stack."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        node = self._model.find_node(str(node_uid))
        if node is None:
            raise KeyError(f"Particle Graph node not found: {node_uid!r}")
        definition = self._definition_for_type(node.type_id)
        if definition is None:
            raise RuntimeError(
                f"Particle Graph node type is not registered: {node.type_id!r}"
            )
        key = str(property_name)
        field = next((item for item in definition.properties if item.id == key), None)
        if field is None or field.value_type.value_type is not ValueType.ASSET_REF:
            valid = [
                item.id
                for item in definition.properties
                if item.value_type.value_type is ValueType.ASSET_REF
            ]
            raise KeyError(
                f"Particle Graph node {node_uid!r} has no AssetRef property {key!r}; "
                f"valid properties: {valid}"
            )

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
        elif key == "material" and extension != ".mat":
            raise ValueError(
                f"Particle Graph Material property requires a .mat asset; got {extension!r}"
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
        if stage not in _STAGES:
            raise ValueError(f"Unknown Particle Graph stage: {stage!r}")
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise ValueError("Particle Graph node position must be finite")
        definition = self._definition_for_type(type_id)
        if definition is None or type_id.startswith("particle.root."):
            raise ValueError(f"Particle Graph node type cannot be created: {type_id!r}")
        if not particle_stage_definition_filter(f"particle.{stage}")(definition):
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
        return {
            "uid": str(node.uid),
            "type_id": str(node.type_id),
            "stage": stage,
            "properties": copy.deepcopy(node.data),
        }

    def set_node_property(self, node_uid: str, property_name: str, value) -> dict:
        """Set a typed non-asset node property through the canvas edit path."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        node = self._model.find_node(str(node_uid))
        if node is None:
            raise KeyError(f"Particle Graph node not found: {node_uid!r}")
        definition = self._definition_for_type(node.type_id)
        if definition is None:
            raise RuntimeError(
                f"Particle Graph node type is not registered: {node.type_id!r}"
            )
        key = str(property_name)
        field = next((item for item in definition.properties if item.id == key), None)
        if field is None:
            raise KeyError(
                f"Particle Graph node {node_uid!r} has no editable property {key!r}"
            )
        if field.value_type.value_type is ValueType.ASSET_REF:
            raise ValueError(
                "AssetRef properties must use particle_graph_set_node_asset"
            )
        error = ExpressionCompiler._literal_error(field.value_type, value)
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

    def connect_stream(self, source_node_uid: str, target_node_uid: str) -> dict:
        """Connect two stream endpoints through the strict graph model."""
        if self._model is None:
            raise RuntimeError("Particle Graph editor has no active authoring model")
        source_uid = str(source_node_uid)
        target_uid = str(target_node_uid)
        source = self._model.find_node(source_uid)
        target = self._model.find_node(target_uid)
        if source is None or target is None:
            raise KeyError(
                f"Particle Graph stream endpoint not found: {source_uid!r} -> {target_uid!r}"
            )
        for link in self._model.links:
            if (
                link.source_node == source_uid
                and link.source_pin == "out"
                and link.target_node == target_uid
                and link.target_pin == "in"
            ):
                return {"link_uid": str(link.uid), "changed": False}
        validation = self._model.validate_link(source_uid, "out", target_uid, "in")
        if not validation:
            raise ValueError(
                f"Particle Graph stream connection is invalid ({validation.code}): "
                f"{validation.message}"
            )
        before = self._snapshot()
        created = self._model.add_link(source_uid, "out", target_uid, "in")
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
        self._record("Connect Particle Graph stream", before)
        return {"link_uid": str(created.uid), "changed": True}

    def set_rendering_output(self, node_uid: str) -> dict:
        """Route the Rendering root stream to one output through the authoring model."""
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

    def _selected_emitter(self) -> ParticleEmitterAsset:
        return self._asset.emitters[self._emitter_index]

    def _definition_for_type(self, type_id: str):
        if self._model is not None:
            return self._model.definition_for_type(type_id)
        return COMMON_NODE_REGISTRY.get(type_id)

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
            stage: document
            for stage, document in documents.items()
            if getattr(emitter, stage) != document
        }
        if updates:
            self._replace_emitter(replace(emitter, **updates))

    def _select_stage(self, stage: str) -> None:
        if stage not in _STAGES or stage == self._stage:
            return
        self._stage = stage
        if self._model is not None:
            self._model.set_authoring_stage(stage)

    def _select_emitter(self, index: int) -> None:
        if not 0 <= index < len(self._asset.emitters) or index == self._emitter_index:
            return
        self._sync_model_to_asset()
        self._emitter_index = index
        self._bind_stage()

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
            Debug.log_error(f"Failed to save Particle Graph '{target}': {exc}")
            return False

        self._file_path = target
        self._dirty = False
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
        }

    def _apply_snapshot(self, snapshot: dict) -> None:
        self._asset = ParticleGraphAsset.from_dict(snapshot["asset"])
        self._emitter_index = min(
            int(snapshot.get("emitter_index", 0)), len(self._asset.emitters) - 1
        )
        stage = str(snapshot.get("stage", "init"))
        self._stage = stage if stage in _STAGES else "init"
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
        if self._file_path:
            self._draft_compile_due_at = time.monotonic() + 0.18
        self._sync_project_dirty_flag()

    def _publish_live_draft_if_due(self) -> None:
        if not self._file_path or self._draft_compile_due_at <= 0.0:
            return
        if time.monotonic() < self._draft_compile_due_at:
            return
        self._draft_compile_due_at = 0.0
        self._sync_model_to_asset()
        try:
            ParticleArtifactRegistry.publish_graph_asset(self._asset, self._file_path)
        except (RuntimeError, TypeError, ValueError) as exc:
            Debug.log_error(f"Particle Graph draft compile failed: {exc}")

    def _on_node_selected(self, node_uid: str) -> None:
        self._selected_node_uid = node_uid
        if self._model is not None and node_uid:
            stage = self._model.stage_for_uid(node_uid)
            if stage:
                self._select_stage(stage)

    def _on_node_creation_requested(self, request: dict) -> None:
        if self._model is None:
            return
        stage = self._model.stage_for_uid(str(request.get("source_node", "")))
        if not stage:
            stage = self._model.stage_nearest_y(float(request.get("gy", 0.0)))
        self._stage = stage
        self._model.set_authoring_stage(stage)
        self._model.prepare_node_creation(stage)

    def _on_node_add(self, type_id: str, x: float, y: float):
        if self._model is None or self._model.get_type(type_id) is None:
            return
        before = self._snapshot()
        node = self._model.add_node(type_id, x, y)
        self._stage = self._model.stage_for_uid(node.uid) or self._stage
        self._sync_model_to_asset()
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
        self._sync_model_to_asset()
        self._mark_changed()
        self._record(f"Edit Particle Graph {key}", before)

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
        before = self._snapshot()
        emitters = list(self._asset.emitters)
        del emitters[self._emitter_index]
        self._asset = replace(self._asset, emitters=tuple(emitters))
        self._emitter_index = min(self._emitter_index, len(emitters) - 1)
        self._bind_stage()
        self._mark_changed()
        self._record("Remove particle emitter", before)

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
            except ParticleGraphSchemaError as exc:
                Debug.log_warning(f"Failed to restore Particle Graph draft: {exc}")
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

    def _render_emitter_list(self, ctx: InxGUIContext) -> None:
        ctx.label(t("particle_graph_editor.emitters"))
        for index, emitter in enumerate(self._asset.emitters):
            selected = index == self._emitter_index
            if ctx.selectable(
                f"{emitter.name}##particle_emitter_{emitter.stable_id}", selected
            ):
                self._select_emitter(index)
            if bool(getattr(ctx, "semantic_capture_enabled", True)):
                ctx.record_semantic_item(
                    "particle_emitter", emitter.name, True,
                    f"particle_graph.emitter.{index}", bool_value=selected,
                )
        if ctx.button(t("particle_graph_editor.add_emitter")):
            self._add_emitter()
        if len(self._asset.emitters) > 1:
            ctx.same_line()
            if ctx.button(t("particle_graph_editor.remove_emitter")):
                self._remove_selected_emitter()

    def _render_emitter_settings(self, ctx: InxGUIContext) -> None:
        ctx.label(t("particle_graph_editor.emitter_settings"))
        ctx.separator()
        emitter = self._selected_emitter()
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
        targets = list(ExecutionTarget)
        target_index = targets.index(settings.target)
        target_index = ctx.combo(
            f"{t('particle_graph_editor.target')}##particle_target",
            target_index,
            [t(f"particle_graph_editor.target_{item.value}") for item in targets],
            -1,
        )
        values["target"] = targets[max(0, min(target_index, len(targets) - 1))]

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

        ctx.separator()
        ctx.label(t("particle_graph_editor.initial_state"))
        life_min = max(
            0.0,
            float(ctx.drag_float(f"{t('particle_graph_editor.lifetime_min')}##particle_life_min", settings.lifetime.minimum, 0.05, 0.0, 1.0e7)),
        )
        life_max = max(
            life_min,
            float(ctx.drag_float(f"{t('particle_graph_editor.lifetime_max')}##particle_life_max", settings.lifetime.maximum, 0.05, life_min, 1.0e7)),
        )
        values["lifetime"] = ScalarRange(life_min, life_max)

        speed_min = float(
            ctx.drag_float(f"{t('particle_graph_editor.speed_min')}##particle_speed_min", settings.initial_speed.minimum, 0.05, -1.0e7, 1.0e7)
        )
        speed_max = max(
            speed_min,
            float(ctx.drag_float(f"{t('particle_graph_editor.speed_max')}##particle_speed_max", settings.initial_speed.maximum, 0.05, speed_min, 1.0e7)),
        )
        values["initial_speed"] = ScalarRange(speed_min, speed_max)
        gravity = tuple(
            float(ctx.drag_float(f"{t('particle_graph_editor.gravity')} {axis}##particle_gravity_{axis}", value, 0.05, -1.0e7, 1.0e7))
            for axis, value in zip("XYZ", settings.gravity)
        )
        values["gravity"] = gravity

        ctx.separator()
        ctx.label(t("particle_graph_editor.emission_shape"))
        shape = settings.shape
        shape_kinds = list(EmitterShapeKind)
        kind_index = ctx.combo(
            f"{t('particle_graph_editor.shape')}##particle_shape",
            shape_kinds.index(shape.kind),
            [t(f"particle_graph_editor.shape_{item.value}") for item in shape_kinds],
            -1,
        )
        kind = shape_kinds[max(0, min(kind_index, len(shape_kinds) - 1))]
        shape_spaces = [CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.WORLD]
        shape_space_index = ctx.combo(
            f"{t('particle_graph_editor.shape_space')}##particle_shape_space",
            shape_spaces.index(shape.space),
            [t(f"particle_graph_editor.shape_space_{item.value}") for item in shape_spaces],
            -1,
        )
        shape_space = shape_spaces[max(0, min(shape_space_index, len(shape_spaces) - 1))]
        radius = max(
            0.0,
            float(ctx.drag_float(f"{t('particle_graph_editor.radius')}##particle_shape_radius", shape.radius, 0.05, 0.0, 1.0e7)),
        )
        angle = min(
            180.0,
            max(0.0, float(ctx.drag_float(f"{t('particle_graph_editor.angle')}##particle_shape_angle", shape.angle_degrees, 0.2, 0.0, 180.0))),
        )
        dimensions = tuple(
            max(0.0, float(ctx.drag_float(f"{t('particle_graph_editor.size')} {axis}##particle_shape_{axis}", value, 0.05, 0.0, 1.0e7)))
            for axis, value in zip("XYZ", shape.dimensions)
        )
        values["shape"] = replace(
            shape,
            kind=kind,
            space=shape_space,
            radius=radius,
            angle_degrees=angle,
            dimensions=dimensions,
        )

        new_settings = replace(settings, **values)
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
            updated = ParticleBurst(time_value, count, cycles, interval)
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

    def _render_node_properties(self, ctx: InxGUIContext) -> None:
        if self._model is None or not self._selected_node_uid:
            return
        node = self._model.find_node(self._selected_node_uid)
        definition = self._definition_for_type(node.type_id) if node else None
        if node is None or definition is None:
            return
        ctx.label(t("particle_graph_editor.node_settings"))
        ctx.separator()
        ctx.label(definition.display_name)
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
            value = copy.deepcopy(node.data.get(key, default))
            label_key = f"particle_graph_editor.property_{key}"
            label = t(label_key)
            if label == label_key:
                label = key.replace("_", " ").title()
            new_value = value
            if value_type is ValueType.BOOL:
                new_value = bool(ctx.checkbox(f"{label}##particle_node_{key}", bool(value)))
            elif value_type in {ValueType.I32, ValueType.U32}:
                input_method = (
                    ctx.input_uint if value_type is ValueType.U32 else ctx.input_int
                )
                new_value = int(input_method(f"{label}##particle_node_{key}", int(value)))
            elif value_type is ValueType.F32:
                new_value = float(ctx.drag_float(f"{label}##particle_node_{key}", float(value), 0.05, -1.0e7, 1.0e7))
            elif value_type in {ValueType.VEC2, ValueType.VEC3, ValueType.VEC4, ValueType.COLOR}:
                new_value = [
                    float(ctx.drag_float(f"{label} {axis}##particle_node_{key}_{axis}", float(component), 0.05, -1.0e7, 1.0e7))
                    for axis, component in zip("XYZW", value)
                ]
            elif value_type is ValueType.ASSET_REF:
                reference = dict(value)
                path_hint = str(reference.get("path_hint", "") or "")
                display = os.path.basename(path_hint) if path_hint else t("igui.none")
                selected_reference = []

                is_mesh = key == "mesh"
                asset_kind = "Mesh" if is_mesh else "Material"
                drag_types = (
                    ("MODEL_GUID", "MODEL_FILE") if is_mesh else "MATERIAL_FILE"
                )

                def _select_asset(path):
                    normalized = str(path).replace("\\", "/")
                    selected_reference.append(
                        {"guid": _asset_guid_from_path(str(path)), "path_hint": normalized}
                    )

                def _picker(query):
                    if not is_mesh:
                        return _picker_assets(query, "*.mat")
                    from Infernux.core.asset_types import MESH_EXTENSIONS

                    items = []
                    for extension in sorted(MESH_EXTENSIONS):
                        items.extend(_picker_assets(query, f"*{extension}"))
                    return items

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
                    semantic_id=f"particle_graph.node.{node.uid}.property.{key}",
                )
                if selected_reference:
                    new_value = selected_reference[-1]
            elif value_type is ValueType.STRING and key == "sort":
                options = ["none", "back_to_front", "front_to_back"]
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
            elif value_type is ValueType.STRING:
                new_value = ctx.text_input(f"{label}##particle_node_{key}", str(value), 512)
            elif value_type is ValueType.CURVE:
                new_value = self._render_curve_property(ctx, node.uid, key, value)
            elif value_type is ValueType.GRADIENT:
                new_value = self._render_gradient_property(ctx, node.uid, key, value)
            if value_type in {
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
            if new_value != value:
                node.data[key] = new_value
                changed = True
        if changed:
            self._sync_model_to_asset()
            self._mark_changed()

    @staticmethod
    def _render_curve_property(ctx: InxGUIContext, node_uid: str, key: str, value):
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
                f"particle_graph.node.{node_uid}.property.{key}.key.{index}.time",
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
                    f"particle_graph.node.{node_uid}.property.{key}.key.{index}.{tangent_key}",
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
    def _render_gradient_property(ctx: InxGUIContext, node_uid: str, key: str, value):
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
                f"particle_graph.node.{node_uid}.property.{key}.key.{index}.time",
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
                    f"particle_graph.node.{node_uid}.property.{key}.key.{index}.color.{channel}",
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
        self._publish_live_draft_if_due()
        save_label = t("particle_graph_editor.save")
        if ctx.button(save_label):
            self._do_save()
        if bool(getattr(ctx, "semantic_capture_enabled", True)):
            ctx.record_semantic_item("button", save_label, True, "particle_graph.toolbar.save")
        ctx.same_line(0, 12)
        ctx.label(self._asset.name)
        self._record_document_semantics(ctx)
        ctx.separator()

        available_w = ctx.get_content_region_avail_width()
        available_h = ctx.get_content_region_avail_height()
        sidebar_w = min(230.0, max(170.0, available_w * 0.20))
        detail_w = min(280.0, max(210.0, available_w * 0.24))
        graph_w = max(1.0, available_w - sidebar_w - detail_w - 16.0)

        emitter_visible = ctx.begin_child("##particle_emitters", sidebar_w, available_h, True)
        try:
            if emitter_visible:
                self._render_emitter_list(ctx)
        finally:
            ctx.end_child()
        ctx.same_line()
        graph_visible = ctx.begin_child("##particle_graph", graph_w, available_h, False)
        try:
            if graph_visible:
                self._view.render(ctx)
        finally:
            ctx.end_child()
        ctx.same_line()
        details_visible = ctx.begin_child("##particle_details", detail_w, available_h, True)
        try:
            if details_visible:
                if self._selected_node_uid:
                    self._render_node_properties(ctx)
                else:
                    self._render_emitter_settings(ctx)
        finally:
            ctx.end_child()

        self._save_as_dialog.render(ctx, self._save_to)


__all__ = ["ParticleGraphEditorPanel"]
