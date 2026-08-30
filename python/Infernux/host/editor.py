"""Stable editor-automation capability boundary for transport plugins."""

from __future__ import annotations

from typing import Any, Iterable

from Infernux.debug import DebugConsole

from .operations import OperationError


_LEVEL_ALIASES = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "LOG": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ASSERT": "ERROR",
    "EXCEPTION": "ERROR",
    "FATAL": "FATAL",
}


class EditorAutomationHost:
    """JSON-oriented access to editor capabilities used by automation plugins.

    This class is the supported boundary. Transport plugins should not import
    editor managers or native bindings directly. Tests may replace the process
    provider with :meth:`set_provider`.
    """

    _provider: "EditorAutomationHost | None" = None

    @classmethod
    def instance(cls) -> "EditorAutomationHost":
        if cls._provider is None:
            cls._provider = cls()
        return cls._provider

    @classmethod
    def set_provider(cls, provider: "EditorAutomationHost | None") -> None:
        cls._provider = provider

    def interaction_core(self):
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise OperationError(
                "editor.unavailable", "Editor interaction services are unavailable."
            )
        return core

    def plugin_manager(self):
        from Infernux.plugins import PluginManager

        manager = PluginManager.instance()
        if manager is None:
            raise OperationError(
                "editor.unavailable", "Plugin project session is unavailable."
            )
        return manager

    def asset_database(self):
        manager = self.plugin_manager()
        engine = getattr(manager, "engine", None)
        database = getattr(engine, "get_asset_database", lambda: None)()
        if database is None:
            database = getattr(
                self.interaction_core().project_assets, "asset_database", None
            )
        if database is None:
            raise OperationError("editor.unavailable", "AssetDatabase is unavailable.")
        return database

    def active_scene(self):
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            raise OperationError("editor.unavailable", "No active scene is available.")
        return scene

    def project_info(self, project_root: str) -> dict[str, object]:
        from Infernux.engine.play_mode import PlayModeManager
        from Infernux.engine.scene_manager import SceneFileManager

        scene_files = SceneFileManager.instance()
        play_mode = PlayModeManager.instance()
        scene = self.active_scene()
        return {
            "project_root": str(project_root),
            "active_scene": {
                "name": str(getattr(scene, "name", "")),
                "path": str(getattr(scene_files, "current_scene_path", ""))
                if scene_files
                else "",
                "dirty": bool(getattr(scene_files, "is_dirty", False))
                if scene_files
                else False,
            },
            "play_state": str(
                getattr(getattr(play_mode, "state", None), "name", "edit")
            ).lower(),
        }

    def request_editor_close(self) -> None:
        from Infernux.engine.scene_manager import SceneFileManager

        manager = SceneFileManager.instance()
        if manager is None:
            raise OperationError(
                "editor.unavailable",
                "Scene lifecycle service is unavailable for normal shutdown.",
            )
        manager.request_close()

    def runtime_status(self) -> dict[str, object]:
        manager = self._play_mode_manager()
        return self._runtime_status_value(manager)

    def runtime_transition(self, method: str) -> dict[str, object]:
        manager = self._play_mode_manager()
        result = getattr(manager, str(method))()
        return {
            "accepted": True if result is None else bool(result),
            "runtime": self._runtime_status_value(manager),
        }

    def set_time_scale(self, value: float) -> dict[str, object]:
        manager = self._play_mode_manager()
        manager.time_scale = float(value)
        return {"runtime": self._runtime_status_value(manager)}

    def editor_camera_state(self) -> dict[str, object]:
        return self._camera_state(self._editor_camera())

    def restore_editor_camera(
        self,
        position: Iterable[float],
        focus: Iterable[float],
        distance: float,
        yaw: float,
        pitch: float,
    ) -> dict[str, object]:
        camera = self._editor_camera()
        camera.restore_state(
            *[float(value) for value in position],
            *[float(value) for value in focus],
            float(distance),
            float(yaw),
            float(pitch),
        )
        return self._camera_state(camera)

    def focus_editor_camera(
        self, point: Iterable[float], distance: float
    ) -> dict[str, object]:
        camera = self._editor_camera()
        camera.focus_on(*[float(value) for value in point], float(distance))
        return self._camera_state(camera)

    def game_camera_state(self) -> dict[str, object] | None:
        camera = self.active_scene().effective_game_camera
        if camera is None:
            return None
        owner = getattr(camera, "game_object", None)
        serializer = getattr(camera, "serialize_document", None)
        return {
            "object_id": int(getattr(owner, "id", 0) or 0),
            "component_id": int(getattr(camera, "component_id", 0) or 0),
            "document": serializer() if callable(serializer) else {},
        }

    def queue_input(self, kind: str, **arguments: object) -> dict[str, int]:
        native = self._native_engine()
        native.request_full_speed_frame()
        handlers = {
            "key": lambda: native.queue_synthetic_key_input(
                self.resolve_scancode(arguments["key"]),
                bool(arguments.get("pressed", True)),
                bool(arguments.get("repeat", False)),
            ),
            "pointer_move": lambda: native.queue_synthetic_mouse_motion_input(
                float(arguments["x"]),
                float(arguments["y"]),
                float(arguments.get("delta_x", 0.0)),
                float(arguments.get("delta_y", 0.0)),
            ),
            "pointer_button": lambda: native.queue_synthetic_mouse_button_input(
                int(arguments["button"]),
                bool(arguments["pressed"]),
                float(arguments["x"]),
                float(arguments["y"]),
            ),
            "wheel": lambda: native.queue_synthetic_mouse_wheel_input(
                float(arguments.get("horizontal", 0.0)),
                float(arguments.get("vertical", 0.0)),
            ),
            "text": lambda: native.queue_synthetic_text_input(
                str(arguments.get("text", ""))
            ),
            "close": native.queue_synthetic_close_request,
        }
        callback = handlers.get(str(kind))
        if callback is None:
            raise OperationError(
                "operation.invalid_arguments", f"Unknown input event kind: {kind}"
            )
        sequence = int(callback() or 0)
        if sequence <= 0:
            raise OperationError("input.rejected", "Synthetic input was rejected.")
        return {"sequence": sequence, **self.input_status()}

    def input_status(self) -> dict[str, int]:
        native = self._native_engine()
        return {
            "last_processed_sequence": int(
                native.last_processed_synthetic_input_sequence
            ),
            "pending_event_count": int(native.pending_synthetic_input_count),
        }

    def resolve_scancode(self, key: object) -> int:
        from Infernux.lib import InputManager

        aliases = {
            "ctrl": 224,
            "control": 224,
            "shift": 225,
            "alt": 226,
            "option": 226,
            "cmd": 227,
            "command": 227,
            "super": 227,
            "win": 227,
            "windows": 227,
            "esc": 41,
        }
        if isinstance(key, bool):
            raise OperationError(
                "operation.invalid_arguments", "key cannot be a boolean"
            )
        if isinstance(key, int):
            result = key
        else:
            text = str(key).strip()
            candidate = aliases.get(text.casefold(), text)
            result = (
                int(candidate)
                if isinstance(candidate, int)
                else int(InputManager.name_to_scancode(candidate))
            )
        if result <= 0:
            raise OperationError("operation.invalid_arguments", f"Unknown key: {key}")
        return result

    def semantic_capture_enabled(self, enabled: bool) -> bool:
        from Infernux.lib import set_gui_semantic_capture_enabled

        set_gui_semantic_capture_enabled(bool(enabled))
        return bool(enabled)

    def request_semantic_snapshot(self) -> int:
        from Infernux.lib import request_gui_semantic_snapshot

        return int(request_gui_semantic_snapshot() or 0)

    def semantic_snapshot(self) -> dict[str, object]:
        from Infernux.lib import get_gui_semantic_snapshot

        return dict(get_gui_semantic_snapshot() or {})

    def request_capture(self, source: str, output_path: str) -> int:
        return int(self._native_engine().request_capture(str(source), str(output_path)))

    def capture_status(self, capture_id: int) -> dict[str, object]:
        return dict(self._native_engine().query_capture(int(capture_id)))

    def cancel_capture(self, capture_id: int) -> bool:
        return bool(self._native_engine().cancel_capture(int(capture_id)))

    def request_scene_pick(
        self, x: float, y: float, width: float, height: float
    ) -> int:
        return int(
            self._native_engine().request_scene_object_pick(x, y, width, height)
        )

    def scene_pick_status(self, request_id: int) -> dict[str, object]:
        return dict(self._native_engine().query_scene_object_pick(int(request_id)))

    def console_read(
        self, limit: int = 100, levels: Iterable[str] = ()
    ) -> dict[str, object]:
        allowed = {self._canonical_level(level) for level in levels}
        panel = self._native_console()
        reader = getattr(panel, "_get_visible_log_snapshot", None)
        if callable(reader):
            entries = [dict(item) for item in reader(max(1, int(limit)))]
            entries = [
                item
                for item in entries
                if not allowed
                or self._canonical_level(item.get("level", "")) in allowed
            ]
            status_bar = None
            try:
                message, level, info, warnings, errors, uid = panel._get_status_snapshot()
                status_bar = {
                    "surface": "status_bar",
                    "message": message,
                    "level": self._canonical_level(level),
                    "counts": {
                        "info": info,
                        "warnings": warnings,
                        "errors": errors,
                    },
                    "mirrors_console_uid": uid or None,
                }
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
            return {
                "entries": entries,
                "source": "native_console",
                "surface": "console",
                "status_bar": status_bar,
            }
        entries = []
        for entry in DebugConsole.instance().get_entries()[-max(1, int(limit)) :]:
            value = {
                "time": entry.get_formatted_time(),
                "level": self._canonical_level(entry.log_type),
                "message": entry.message,
                "source_file": entry.source_file,
                "source_line": entry.source_line,
                "stack_trace": entry.stack_trace,
                "count": 1,
            }
            if not allowed or value["level"] in allowed:
                entries.append(value)
        return {
            "entries": entries,
            "source": "python_debug_fallback",
            "surface": "console",
            "status_bar": None,
        }

    def create_project_asset(
        self,
        kind: str,
        directory: str,
        name: str,
        extension: str,
        variant: str = "",
    ) -> str:
        from Infernux.engine.ui import project_file_ops

        core = self.interaction_core()
        service = core.project_asset_interactions
        if service.configured:
            return str(service.create(kind, directory, name, extension, variant) or "")
        unique_name = project_file_ops.get_unique_name(directory, name, extension)

        def create_file():
            creators = {
                "folder": (project_file_ops.create_folder, (directory, unique_name)),
                "script": (project_file_ops.create_script, (directory, unique_name, core.project_assets.asset_database)),
                "shader": (project_file_ops.create_shader, (directory, unique_name, variant, core.project_assets.asset_database)),
                "material": (project_file_ops.create_material, (directory, unique_name, core.project_assets.asset_database)),
                "physic_material": (project_file_ops.create_physic_material, (directory, unique_name, core.project_assets.asset_database)),
                "scene": (project_file_ops.create_scene, (directory, unique_name, core.project_assets.asset_database)),
                "animation_clip": (project_file_ops.create_animclip, (directory, unique_name, core.project_assets.asset_database)),
                "animation_clip3d": (project_file_ops.create_animclip3d, (directory, unique_name, core.project_assets.asset_database)),
                "animation_fsm": (project_file_ops.create_animfsm, (directory, unique_name, core.project_assets.asset_database)),
                "particle_graph": (project_file_ops.create_particlegraph, (directory, unique_name, core.project_assets.asset_database)),
                "render_effect": (project_file_ops.create_render_effect, (directory, unique_name, variant, core.project_assets.asset_database)),
                "render_effect_group": (project_file_ops.create_render_effect_group, (directory, unique_name, core.project_assets.asset_database)),
                "animation_timeline": (project_file_ops.create_animtimeline, (directory, unique_name, core.project_assets.asset_database)),
                "timeline_fsm": (project_file_ops.create_timelinefsm, (directory, unique_name, core.project_assets.asset_database)),
            }
            callback, arguments = creators[kind]
            return callback(*arguments)

        return str(
            core.project_assets.create_with_path(
                directory,
                create_file,
                description=f"Create {kind.replace('_', ' ').title()}",
            )
            or ""
        )

    def material_document(self, path: str) -> tuple[Any, dict[str, object]]:
        from Infernux.core.material import Material

        material = Material.load(path)
        if material is None:
            raise OperationError("material.load_failed", f"Material could not be loaded: {path}")
        return material, dict(material.serialize_document())

    def publish_material_document(
        self,
        path: str,
        guid: str,
        document: dict[str, object],
        *,
        edit_key: str,
        description: str,
    ) -> None:
        from Infernux.engine.interaction import (
            DocumentKind,
            ensure_editable_resource_document,
        )

        material, _before = self.material_document(path)
        controller = ensure_editable_resource_document(
            category="material",
            document_kind=DocumentKind.MATERIAL,
            file_path=path,
            resource=material,
            guid=guid,
            title=material.name,
            view_id="automation",
        )
        changed = controller.apply_document(
            document,
            view_id="automation",
            edit_key=edit_key,
            description=description,
        )
        if not changed:
            raise OperationError(
                "material.edit_rejected", "Material edit was rejected or unchanged."
            )
        controller.flush_autosave(force=True)
        native = getattr(
            getattr(self.plugin_manager(), "engine", None),
            "get_native_engine",
            lambda: None,
        )()
        refresh = getattr(native, "refresh_material_pipeline", None)
        if callable(refresh):
            refresh(material.native)

    def particle_graph_document(self, path: str) -> tuple[Any, dict[str, object]]:
        from Infernux.particle.asset import ParticleGraphAsset

        graph = ParticleGraphAsset.load(path)
        return graph, dict(graph.to_dict())

    def particle_graph_from_document(self, document: dict[str, object]):
        from Infernux.particle.asset import ParticleGraphAsset

        return ParticleGraphAsset.from_dict(document)

    def publish_particle_graph(
        self, path: str, guid: str, before: Any, after: Any, description: str
    ) -> None:
        from Infernux.core.assets import AssetManager
        from Infernux.engine.undo import UndoCommand, UndoManager
        from Infernux.particle.artifact import ParticleArtifactRegistry

        if before.to_dict() == after.to_dict():
            raise OperationError(
                "particle.edit_rejected", "Particle Graph edit is unchanged."
            )

        class ParticleGraphCommand(UndoCommand):
            marks_dirty = False

            def __init__(command_self):
                super().__init__(description)

            @staticmethod
            def _publish(graph):
                ParticleArtifactRegistry.save_graph_asset(graph, path, guid=guid)
                result = AssetManager.reimport_asset(path)
                if not result:
                    result = AssetManager.import_asset(path)
                if not result:
                    raise RuntimeError(
                        str(getattr(result, "error", "Particle Graph import failed"))
                    )

            def _apply(command_self, graph, rollback):
                try:
                    command_self._publish(graph)
                except Exception:
                    try:
                        command_self._publish(rollback)
                    except Exception:
                        pass
                    raise

            def execute(command_self):
                command_self._apply(after, before)

            def undo(command_self):
                command_self._apply(before, after)

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            raise OperationError(
                "particle.edit_rejected",
                "Editor history cannot accept Particle Graph edits.",
            )
        if not manager.execute(ParticleGraphCommand()):
            raise OperationError(
                "particle.edit_rejected",
                "Particle Graph edit failed validation or publication.",
            )

    def hierarchy_create_kinds(self) -> list[dict[str, object]]:
        from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

        return list(HierarchyCreationService.instance().list_create_kinds())

    def create_scene_object(self, kind: str, parent_id: int, name: str):
        from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

        service = HierarchyCreationService.instance()
        if not service.can_create(kind, parent_id=int(parent_id)):
            raise OperationError(
                "scene.create_rejected", f"Cannot create object kind {kind!r}."
            )
        return service.create(
            kind,
            parent_id=int(parent_id),
            name=str(name or "") or None,
            select=False,
            selection_owner_id="automation",
            selection_reason="host_create_game_object",
        )

    def scene_object(self, object_id: int):
        """Resolve one live scene object without exposing SceneManager lookup rules."""
        normalized = int(object_id)
        value = self.active_scene().find_by_id(normalized)
        if value is None:
            raise OperationError(
                "scene.object_not_found", f"GameObject was not found: {normalized}"
            )
        return value

    def scene_components(self, object_id: int) -> list[Any]:
        """Return each logical component once, preferring Python API wrappers."""
        from Infernux.components.builtin_component import BuiltinComponent

        owner = self.scene_object(object_id)
        result: list[Any] = []
        seen: set[int] = set()
        for value in list(owner.get_py_components()) + list(owner.get_components()):
            component_id = int(getattr(value, "component_id", 0) or 0)
            if component_id > 0 and component_id in seen:
                continue
            if not bool(getattr(value, "_is_builtin_component_wrapper", False)):
                wrapper_type = BuiltinComponent._builtin_registry.get(
                    str(getattr(value, "type_name", "") or "")
                )
                if wrapper_type is not None:
                    value = wrapper_type._get_or_create_wrapper(value, owner)
            if component_id > 0:
                seen.add(component_id)
            result.append(value)
        return result

    def scene_component(self, object_id: int, component_id: int):
        target_id = int(component_id)
        for value in self.scene_components(object_id):
            if int(getattr(value, "component_id", 0) or 0) == target_id:
                return value
        raise OperationError(
            "scene.component_not_found",
            f"Component {target_id} was not found on GameObject {int(object_id)}.",
        )

    def scene_component_schema(
        self, object_id: int, component_id: int
    ) -> dict[str, object]:
        """Describe writable component fields using authoritative serializer metadata."""
        from Infernux.components.fields import get_serialized_fields

        value = self.scene_component(object_id, component_id)
        fields: list[dict[str, object]] = []
        for name, metadata in get_serialized_fields(type(value)).items():
            enum_type = getattr(metadata, "enum_type", None)
            if isinstance(enum_type, str):
                try:
                    import Infernux.lib as native

                    enum_type = getattr(native, enum_type, None)
                except (ImportError, AttributeError):
                    enum_type = None
            members = getattr(enum_type, "__members__", {}) or {}
            field_type = getattr(getattr(metadata, "field_type", None), "name", "unknown")
            entry: dict[str, object] = {
                "name": str(name),
                "type": str(field_type).lower(),
                "readonly": bool(getattr(metadata, "readonly", False)),
                "hidden": bool(getattr(metadata, "hidden", False)),
            }
            value_range = getattr(metadata, "range", None)
            if value_range is not None:
                entry["range"] = [float(item) for item in value_range]
            if members:
                entry["enum"] = [
                    {
                        "name": str(member_name),
                        "value": int(getattr(member, "value", member)),
                    }
                    for member_name, member in members.items()
                ]
            fields.append(entry)
        if not fields:
            serializer = getattr(value, "serialize_document", None)
            document = serializer() if callable(serializer) else {}
            if isinstance(document, dict):
                fields = [
                    {
                        "name": str(name),
                        "type": self._json_type(field_value),
                        "readonly": False,
                        "hidden": False,
                    }
                    for name, field_value in document.items()
                ]
        return {
            "object_id": int(object_id),
            "component_id": int(component_id),
            "component_type": str(
                getattr(value, "type_name", "") or type(value).__name__
            ),
            "python_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": fields,
        }

    def delete_scene_objects(self, object_ids: Iterable[int]) -> list[int]:
        values = [int(value) for value in object_ids]
        if not self.interaction_core().scene_objects.delete_ids(values):
            raise OperationError("scene.edit_rejected", "No GameObjects were deleted.")
        return values

    def set_scene_object_property(
        self, object_id: int, property_name: str, value: object
    ) -> None:
        if not self.interaction_core().scene_objects.set_object_property(
            int(object_id), str(property_name), value
        ):
            raise OperationError(
                "scene.edit_rejected", "GameObject property edit was rejected or unchanged."
            )

    def set_scene_transforms(self, object_id: int, transform: dict[str, object]) -> None:
        if not self.interaction_core().scene_objects.set_transforms(
            [int(object_id)], [dict(transform)]
        ):
            raise OperationError(
                "scene.edit_rejected", "Transform edit was rejected or unchanged."
            )

    def set_scene_parent(self, object_ids: Iterable[int], parent_id: int = 0) -> None:
        values = [int(value) for value in object_ids]
        target = int(parent_id)
        mode = "parent" if target else "root"
        if not self.interaction_core().scene_objects.move_hierarchy(values, mode, target):
            raise OperationError(
                "scene.edit_rejected", "Hierarchy edit was rejected or unchanged."
            )

    def add_scene_component(
        self, object_id: int, component_type: str, *, script_guid: str = ""
    ):
        return self.interaction_core().components.add(
            self.scene_object(object_id),
            str(component_type),
            script_guid=str(script_guid or ""),
        )

    def remove_scene_component(self, object_id: int, component_id: int) -> None:
        owner = self.scene_object(object_id)
        value = self.scene_component(object_id, component_id)
        if not self.interaction_core().components.remove(owner, value):
            raise OperationError(
                "scene.edit_rejected", "Component removal was rejected or unchanged."
            )

    def set_scene_component_field(
        self, object_id: int, component_id: int, field: str, value: object
    ):
        target = self.scene_component(object_id, component_id)
        if not self.interaction_core().components.set_field(target, str(field), value):
            raise OperationError(
                "scene.edit_rejected", "Component field edit was rejected or unchanged."
            )
        return target

    def open_scene(self, path: str) -> bool:
        from Infernux.engine.scene_manager import SceneFileManager

        manager = SceneFileManager.instance()
        return bool(manager is not None and manager.open_scene(path))

    def save_scene(self) -> str:
        from Infernux.engine.scene_manager import SceneFileManager

        manager = SceneFileManager.instance()
        if manager is None or not manager.save_current_scene():
            raise OperationError(
                "scene.save_rejected", "The active scene could not be saved synchronously."
            )
        return str(manager.current_scene_path or "")

    def reload_scene(self, *, discard_changes: bool = False) -> dict[str, object]:
        """Reload the active scene through the normal deferred scene swap."""
        from Infernux.engine.scene_manager import SceneFileManager

        manager = SceneFileManager.instance()
        path = str(manager.current_scene_path or "") if manager is not None else ""
        if manager is None or not manager.reload_current_scene(
            discard_changes=bool(discard_changes)
        ):
            raise OperationError(
                "scene.reload_rejected",
                "The active scene could not be reloaded. Stop Play Mode and pass "
                "discard_changes=true when the document is dirty or conflicted.",
            )
        return {
            "scheduled": True,
            "path": path,
            "discarded_changes": bool(discard_changes),
        }

    @staticmethod
    def _json_type(value: object) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (list, tuple)):
            return "array"
        if isinstance(value, dict):
            return "object"
        return type(value).__name__

    def player_build_targets(self) -> dict[str, object]:
        """List targets currently owned by enabled platform plugins."""
        from dataclasses import asdict

        from Infernux.engine.build import (
            current_host_player_target,
            exporter_registry,
        )

        targets = exporter_registry.targets()
        desktop = current_host_player_target(targets)
        def _capabilities(item) -> dict[str, object]:
            result = asdict(item.capabilities)
            result["features"] = sorted(result["features"])
            return result

        return {
            "current_host_target": str(desktop.id) if desktop is not None else "",
            "targets": [
                {
                    "id": str(item.id),
                    "display_name": item.display_name,
                    "platform": item.platform,
                    "architecture": item.architecture,
                    "capabilities": _capabilities(item),
                }
                for item in targets
            ],
        }

    def build_player(
        self,
        project_root: str,
        *,
        target: str = "",
        output_dir: str = "",
        game_name: str = "",
        debug_mode: bool | None = None,
        lto: bool | None = None,
        enable_jit: bool | None = None,
        android_artifact: str = "",
        compress_resources: bool | None = None,
        persist_settings: bool = True,
    ) -> dict[str, object]:
        """Build one registered Player target through the shared build service."""
        from dataclasses import replace
        import json
        import os
        import tempfile

        from Infernux.engine.build import (
            BuildConfiguration,
            BuildProfile,
            BuildRequest,
            BuildService,
            BuildUnavailableError,
            build_progress_fraction,
            current_host_player_target,
            exporter_registry,
        )
        from Infernux.engine.interaction import normalize_build_settings
        from Infernux.engine.path_utils import resolved_path
        from Infernux.engine.player_build_preflight import (
            publish_player_asset_catalog_for_host,
        )

        root = resolved_path(project_root)
        settings_path = os.path.join(root, "ProjectSettings", "BuildSettings.json")
        try:
            with open(settings_path, "r", encoding="utf-8") as stream:
                settings = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError(
                "player.build_settings",
                f"Build Settings could not be read: {settings_path}",
            ) from exc
        if not isinstance(settings, dict):
            raise OperationError("player.build_settings", "Build Settings must be an object.")
        try:
            settings = normalize_build_settings(settings)
        except (TypeError, ValueError) as exc:
            raise OperationError("player.build_settings", str(exc)) from exc

        raw_output = str(output_dir or settings.get("output_dir", "") or "").strip()
        final_output = resolved_path(raw_output) if raw_output else ""
        final_name = str(game_name or settings.get("game_name", "") or "").strip() or os.path.basename(root)
        if not final_output:
            raise OperationError(
                "player.build_settings",
                "Player build requires output_dir.",
            )
        final_debug = bool(settings.get("debug_mode", False)) if debug_mode is None else bool(debug_mode)
        final_lto = bool(settings.get("lto", True)) if lto is None else bool(lto)
        final_jit = bool(settings.get("enable_jit", False)) if enable_jit is None else bool(enable_jit)
        final_artifact = str(
            android_artifact or settings.get("android_artifact", "apk") or "apk"
        ).strip().casefold()
        if final_artifact not in {"apk", "aab"}:
            raise OperationError(
                "player.build_settings",
                "android_artifact must be apk or aab.",
            )
        available_targets = exporter_registry.targets()
        desktop = current_host_player_target(available_targets)
        final_target = str(
            target
            or settings.get("build_target", "")
            or (desktop.id if desktop is not None else "")
        ).strip()
        if not final_target or all(
            item.id != final_target for item in available_targets
        ):
            raise OperationError(
                "player.target_unavailable",
                f"Player build target is not installed or enabled: {final_target or '<none>'}",
                details={
                    "requested_target": final_target,
                    "available_targets": [
                        {
                            "id": str(item.id),
                            "display_name": item.display_name,
                            "platform": item.platform,
                            "architecture": item.architecture,
                        }
                        for item in available_targets
                    ],
                },
            )
        final_compress = (
            not final_debug
            if compress_resources is None
            else bool(compress_resources)
        )
        settings.update(
            {
                "build_target": final_target,
                "android_artifact": final_artifact,
                "output_dir": final_output,
                "game_name": final_name,
                "debug_mode": final_debug,
                "lto": final_lto,
                "enable_jit": final_jit,
            }
        )
        progress: list[dict[str, object]] = []
        phase_counts: dict[str, int] = {}
        omitted_verbose = 0
        last_fraction = 0.0

        def record(item) -> None:
            nonlocal last_fraction, omitted_verbose
            phase = str(item.phase)
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            source = str(item.detail.get("source", "")).casefold()
            if source in {"cmake", "gradle", "web-toolchain"}:
                omitted_verbose += 1
                return
            last_fraction = max(last_fraction, build_progress_fraction(item))
            progress.append(
                {
                    "phase": phase,
                    "completed": item.completed,
                    "total": item.total,
                    "message": item.message,
                    "progress": last_fraction,
                    "detail": dict(item.detail),
                }
            )
            if len(progress) > 128:
                del progress[:-128]

        request = BuildRequest(
            root,
            final_target,
            final_output,
            BuildProfile(
                configuration=(
                    BuildConfiguration.DEVELOPMENT
                    if final_debug
                    else BuildConfiguration.RELEASE
                ),
                debug_symbols=final_debug,
                compress_resources=final_compress,
                options={
                    "android_artifact": final_artifact,
                    "build_settings": settings,
                },
            ),
            progress=record,
        )
        service = BuildService(exporter_registry)
        try:
            plan = service.create_plan(request)
            catalog = publish_player_asset_catalog_for_host(root)
            request = replace(
                request,
                asset_catalog_entries=tuple(catalog["entries"]),
            )
            result = service.execute(request, plan)
        except BuildUnavailableError as exc:
            raise OperationError(
                "player.target_unavailable",
                "; ".join(item.message for item in exc.diagnostics),
                details={
                    "target": final_target,
                    "diagnostics": [
                        {
                            "severity": item.severity.value,
                            "code": item.code,
                            "message": item.message,
                            "source": item.source,
                            "detail": dict(item.detail),
                        }
                        for item in exc.diagnostics
                    ],
                },
            ) from exc
        diagnostics = [
            {
                "severity": item.severity.value,
                "code": item.code,
                "message": item.message,
                "source": item.source,
                "detail": dict(item.detail),
            }
            for item in result.diagnostics
        ]
        artifacts = [
            {
                "path": item.path,
                "kind": item.kind,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in result.artifacts
        ]
        if not result.success:
            raise OperationError(
                "player.build_failed",
                "; ".join(item["message"] for item in diagnostics)
                or "The platform exporter did not produce a Player artifact.",
                details={
                    "target": final_target,
                    "diagnostics": diagnostics,
                    "manifest": dict(result.manifest),
                    "logs": list(result.logs[-100:]),
                },
            )

        if persist_settings:
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".build-settings-",
                suffix=".tmp",
                dir=os.path.dirname(settings_path),
                text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(settings, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, settings_path)
            except Exception:
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
                raise

        is_desktop = desktop is not None and final_target == desktop.id
        desktop_output = str(result.manifest.get("output_dir", final_output))
        executable = (
            os.path.join(
                desktop_output,
                final_name + (".exe" if os.name == "nt" else ""),
            )
            if is_desktop
            else ""
        )
        return {
            "target": final_target,
            "output_dir": final_output,
            "game_name": final_name,
            "executable_path": executable,
            "executable_exists": bool(executable and os.path.isfile(executable)),
            "debug_mode": final_debug,
            "lto": final_lto,
            "enable_jit": final_jit,
            "compress_resources": final_compress,
            "android_artifact": final_artifact,
            "elapsed_seconds": result.elapsed_seconds,
            "artifacts": artifacts,
            "diagnostics": diagnostics,
            "manifest": dict(result.manifest),
            "progress": progress,
            "progress_summary": {
                "event_count": sum(phase_counts.values()),
                "retained_count": len(progress),
                "omitted_verbose_count": omitted_verbose,
                "phase_counts": dict(sorted(phase_counts.items())),
            },
        }

    @staticmethod
    def _canonical_level(value: object) -> str:
        raw = getattr(value, "name", value)
        return _LEVEL_ALIASES.get(str(raw).upper(), str(raw).upper())

    @staticmethod
    def _runtime_status_value(manager: Any) -> dict[str, object]:
        return {
            "state": str(manager.state.name).lower(),
            "playing": bool(manager.is_playing),
            "paused": bool(manager.is_paused),
            "time_scale": float(manager.time_scale),
            "delta_time": float(manager.delta_time),
            "total_play_time": float(manager.total_play_time),
            "step_sequence": int(manager.step_sequence),
            "transition_timings_ms": dict(manager.last_transition_timings_ms),
        }

    @staticmethod
    def _camera_state(camera: Any) -> dict[str, object]:
        return {
            "position": list(camera.position),
            "rotation": list(camera.rotation),
            "focus": list(camera.focus_point),
            "distance": float(camera.focus_distance),
            "fov": float(camera.fov),
            "near_clip": float(camera.near_clip),
            "far_clip": float(camera.far_clip),
            "orthographic": bool(camera.orthographic),
            "orthographic_size": float(camera.orthographic_size),
        }

    @staticmethod
    def _play_mode_manager():
        from Infernux.engine.play_mode import PlayModeManager

        manager = PlayModeManager.instance()
        if manager is None:
            raise OperationError("editor.unavailable", "PlayModeManager is unavailable.")
        return manager

    def _editor_camera(self):
        camera = getattr(getattr(self.plugin_manager(), "engine", None), "editor_camera", None)
        if camera is None:
            raise OperationError("editor.unavailable", "Editor camera is unavailable.")
        return camera

    @staticmethod
    def _native_console():
        try:
            from Infernux.engine.bootstrap import EditorBootstrap

            bootstrap = EditorBootstrap.instance()
            return getattr(bootstrap, "console", None) if bootstrap else None
        except (AttributeError, ImportError, RuntimeError):
            return None

    @staticmethod
    def _native_engine():
        from Infernux.engine.bootstrap import EditorBootstrap

        bootstrap = EditorBootstrap.instance()
        engine = bootstrap.engine if bootstrap is not None else None
        native = engine.get_native_engine() if engine is not None else None
        if native is None:
            raise OperationError(
                "editor.unavailable",
                "A running graphical Editor session is required.",
            )
        return native


__all__ = ["EditorAutomationHost"]
