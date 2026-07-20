"""Editor for strict ``.particlegraph`` assets and their three AOT stages."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.graph.registry import COMMON_NODE_REGISTRY
from Infernux.graph.types import ValueType
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

from .asset_save_dialog import AssetSaveAsDialog
from .editor_panel import EditorPanel
from .graph_document_authoring import (
    GraphDocumentAuthoringModel,
    particle_stage_definition_filter,
)
from .node_graph_view import NodeGraphView
from .panel_registry import editor_panel


_STAGES = ("init", "update", "rendering")


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
        self._save_as_dialog = AssetSaveAsDialog(
            "particle_graph.save_as", "particle graph"
        )

        self._view = NodeGraphView()
        self._view.semantic_namespace = "particle_graph.canvas"
        self._view.on_node_add_request = self._on_node_add
        self._view.on_nodes_deleted = self._on_nodes_deleted
        self._view.on_link_created = self._on_link_created
        self._view.on_link_deleted = self._on_link_deleted
        self._view.on_node_drag_start = self._on_node_drag_start
        self._view.on_node_drag_end = self._on_node_drag_end
        self._view.on_node_selected = self._on_node_selected
        self._model: GraphDocumentAuthoringModel | None = None
        self._bind_stage()

    @property
    def asset(self) -> ParticleGraphAsset:
        self._sync_model_to_asset()
        return self._asset

    def _selected_emitter(self) -> ParticleEmitterAsset:
        return self._asset.emitters[self._emitter_index]

    def _replace_emitter(self, emitter: ParticleEmitterAsset) -> None:
        emitters = list(self._asset.emitters)
        emitters[self._emitter_index] = emitter
        self._asset = replace(self._asset, emitters=tuple(emitters))

    def _bind_stage(self) -> None:
        document = getattr(self._selected_emitter(), self._stage)
        self._model = GraphDocumentAuthoringModel(
            document,
            definition_filter=particle_stage_definition_filter(document.domain),
        )
        self._view.graph = self._model
        self._view.reset_interaction_state()
        self._selected_node_uid = ""

    def _sync_model_to_asset(self) -> None:
        if self._model is None:
            return
        emitter = self._selected_emitter()
        document = self._model.to_document()
        if getattr(emitter, self._stage) != document:
            self._replace_emitter(replace(emitter, **{self._stage: document}))

    def _select_stage(self, stage: str) -> None:
        if stage not in _STAGES or stage == self._stage:
            return
        self._sync_model_to_asset()
        self._stage = stage
        self._bind_stage()

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
            return self._open_particlegraph(self._file_path)
        self._asset = ParticleGraphAsset()
        self._emitter_index = 0
        self._stage = "init"
        self._dirty = False
        self._bind_stage()
        self._sync_project_dirty_flag()
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
        self._sync_project_dirty_flag()

    def _on_node_selected(self, node_uid: str) -> None:
        self._selected_node_uid = node_uid

    def _on_node_add(self, type_id: str, x: float, y: float) -> None:
        if self._model is None or self._model.get_type(type_id) is None:
            return
        before = self._snapshot()
        self._model.add_node(type_id, x, y)
        self._sync_model_to_asset()
        self._mark_changed()
        self._record("Add Particle Graph node", before)

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

    def _render_stage_tabs(self, ctx: InxGUIContext) -> None:
        if not ctx.begin_tab_bar("##particle_graph_stages"):
            return
        for stage in _STAGES:
            label = t(f"particle_graph_editor.stage_{stage}")
            if ctx.begin_tab_item(label):
                self._select_stage(stage)
                ctx.end_tab_item()
        ctx.end_tab_bar()

    def _render_emitter_settings(self, ctx: InxGUIContext) -> None:
        emitter = self._selected_emitter()
        name = ctx.text_input("Name##particle_emitter_name", emitter.name, 128).strip()
        if name and name != emitter.name:
            self._update_emitter(replace(emitter, name=name), "Rename particle emitter")
            emitter = self._selected_emitter()

        settings = emitter.settings
        values = {}
        values["capacity"] = max(
            1, int(ctx.input_int("Capacity##particle_capacity", settings.capacity))
        )
        targets = list(ExecutionTarget)
        target_index = targets.index(settings.target)
        target_index = ctx.combo(
            "Target##particle_target", target_index,
            [item.value.upper() for item in targets], -1,
        )
        values["target"] = targets[max(0, min(target_index, len(targets) - 1))]

        spaces = list(SimulationSpace)
        space_index = spaces.index(settings.simulation_space)
        space_index = ctx.combo(
            "Simulation Space##particle_space", space_index,
            [item.value.title() for item in spaces], -1,
        )
        values["simulation_space"] = spaces[max(0, min(space_index, len(spaces) - 1))]
        values["seed"] = max(0, int(ctx.input_int("Seed##particle_seed", settings.seed)))
        values["spawn_rate"] = max(
            0.0,
            float(ctx.drag_float("Spawn Rate##particle_spawn_rate", settings.spawn_rate, 0.1, 0.0, 1.0e7)),
        )

        life_min = max(
            0.0,
            float(ctx.drag_float("Lifetime Min##particle_life_min", settings.lifetime.minimum, 0.05, 0.0, 1.0e7)),
        )
        life_max = max(
            life_min,
            float(ctx.drag_float("Lifetime Max##particle_life_max", settings.lifetime.maximum, 0.05, life_min, 1.0e7)),
        )
        values["lifetime"] = ScalarRange(life_min, life_max)

        speed_min = float(
            ctx.drag_float("Speed Min##particle_speed_min", settings.initial_speed.minimum, 0.05, -1.0e7, 1.0e7)
        )
        speed_max = max(
            speed_min,
            float(ctx.drag_float("Speed Max##particle_speed_max", settings.initial_speed.maximum, 0.05, speed_min, 1.0e7)),
        )
        values["initial_speed"] = ScalarRange(speed_min, speed_max)
        gravity = tuple(
            float(ctx.drag_float(f"Gravity {axis}##particle_gravity_{axis}", value, 0.05, -1.0e7, 1.0e7))
            for axis, value in zip("XYZ", settings.gravity)
        )
        values["gravity"] = gravity

        shape = settings.shape
        shape_kinds = list(EmitterShapeKind)
        kind_index = ctx.combo(
            "Shape##particle_shape", shape_kinds.index(shape.kind),
            [item.value.title() for item in shape_kinds], -1,
        )
        kind = shape_kinds[max(0, min(kind_index, len(shape_kinds) - 1))]
        radius = max(
            0.0,
            float(ctx.drag_float("Radius##particle_shape_radius", shape.radius, 0.05, 0.0, 1.0e7)),
        )
        angle = min(
            180.0,
            max(0.0, float(ctx.drag_float("Angle##particle_shape_angle", shape.angle_degrees, 0.2, 0.0, 180.0))),
        )
        dimensions = tuple(
            max(0.0, float(ctx.drag_float(f"Size {axis}##particle_shape_{axis}", value, 0.05, 0.0, 1.0e7)))
            for axis, value in zip("XYZ", shape.dimensions)
        )
        values["shape"] = replace(
            shape, kind=kind, radius=radius, angle_degrees=angle, dimensions=dimensions
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
            ctx.label(f"Burst {index + 1}")
            time_value = max(0.0, float(ctx.drag_float(f"Time##burst_time_{index}", burst.time, 0.05, 0.0, 1.0e7)))
            count = max(0, int(ctx.input_int(f"Count##burst_count_{index}", burst.count)))
            cycles = max(1, int(ctx.input_int(f"Cycles##burst_cycles_{index}", burst.cycles)))
            interval = max(0.0, float(ctx.drag_float(f"Interval##burst_interval_{index}", burst.interval, 0.05, 0.0, 1.0e7)))
            updated = ParticleBurst(time_value, count, cycles, interval)
            if updated != burst:
                bursts[index] = updated
                changed = True
            if ctx.button(f"Remove##particle_burst_remove_{index}"):
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
        definition = COMMON_NODE_REGISTRY.get(node.type_id) if node else None
        if node is None or definition is None:
            return
        ctx.separator()
        ctx.label(definition.display_name)
        changed = False
        for property_def in definition.properties:
            key = property_def.id
            value = copy.deepcopy(node.data.get(key, property_def.default))
            value_type = property_def.value_type.value_type
            label = key.replace("_", " ").title()
            new_value = value
            if value_type is ValueType.BOOL:
                new_value = bool(ctx.checkbox(f"{label}##particle_node_{key}", bool(value)))
            elif value_type in {ValueType.I32, ValueType.U32}:
                new_value = int(ctx.input_int(f"{label}##particle_node_{key}", int(value)))
                if value_type is ValueType.U32:
                    new_value = max(0, new_value)
            elif value_type is ValueType.F32:
                new_value = float(ctx.drag_float(f"{label}##particle_node_{key}", float(value), 0.05, -1.0e7, 1.0e7))
            elif value_type in {ValueType.VEC2, ValueType.VEC3, ValueType.VEC4, ValueType.COLOR}:
                new_value = [
                    float(ctx.drag_float(f"{label} {axis}##particle_node_{key}_{axis}", float(component), 0.05, -1.0e7, 1.0e7))
                    for axis, component in zip("XYZW", value)
                ]
            elif value_type is ValueType.ASSET_REF:
                reference = dict(value)
                reference["path_hint"] = ctx.text_input(
                    f"{label}##particle_node_{key}", reference.get("path_hint", ""), 512
                ).replace("\\", "/")
                new_value = reference
            elif value_type is ValueType.STRING and key == "sort":
                options = ["none", "back_to_front", "front_to_back"]
                current = options.index(value) if value in options else 0
                current = ctx.combo(f"{label}##particle_node_{key}", current, options, -1)
                new_value = options[max(0, min(current, len(options) - 1))]
            elif value_type is ValueType.STRING:
                new_value = ctx.text_input(f"{label}##particle_node_{key}", str(value), 512)
            if new_value != value:
                node.data[key] = new_value
                changed = True
        if changed:
            self._sync_model_to_asset()
            self._mark_changed()

    def on_render_content(self, ctx: InxGUIContext):
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

        if ctx.begin_child("##particle_emitters", sidebar_w, available_h, True):
            self._render_emitter_list(ctx)
        ctx.end_child()
        ctx.same_line()
        if ctx.begin_child("##particle_graph", graph_w, available_h, False):
            self._render_stage_tabs(ctx)
            self._view.render(ctx)
        ctx.end_child()
        ctx.same_line()
        if ctx.begin_child("##particle_details", detail_w, available_h, True):
            self._render_emitter_settings(ctx)
            self._render_node_properties(ctx)
        ctx.end_child()

        self._save_as_dialog.render(ctx, self._save_to)


__all__ = ["ParticleGraphEditorPanel"]
