"""Panel-independent scene-object editing commands."""

from __future__ import annotations

import copy
import math
from contextlib import contextmanager
from collections.abc import Callable, Mapping, Sequence

from Infernux.debug import Debug

from .clipboard import (
    ClipboardDomain,
    ClipboardItem,
    ClipboardOperation,
    ClipboardService,
)
from .commands import CommandContext
from .descriptors import SelectionDomain
from .selection import SelectionService
from .action_journal import ActionOrigin


class SceneObjectCommandService:
    """Own scene-object mutations independently of editor panels."""

    def __init__(
        self,
        selection: SelectionService,
        clipboard: ClipboardService,
    ) -> None:
        if not isinstance(selection, SelectionService):
            raise TypeError("scene object commands require SelectionService")
        if not isinstance(clipboard, ClipboardService):
            raise TypeError("scene object commands require ClipboardService")
        self._selection = selection
        self._clipboard = clipboard
        self._change_publisher: Callable[[], None] | None = None

    def set_change_publisher(self, publisher: Callable[[], None] | None) -> None:
        """Publish scene-object value changes without coupling the core to a panel."""
        if publisher is not None and not callable(publisher):
            raise TypeError("scene object change publisher must be callable")
        self._change_publisher = publisher

    def _publish_change(self) -> None:
        if self._change_publisher is not None:
            self._change_publisher()

    @contextmanager
    def user_action(self, description: str):
        """Group one semantic scene edit without exposing Undo to its caller."""
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            raise RuntimeError("Global editor history is unavailable.")
        with manager.user_action(str(description or "Edit Scene")):
            yield

    @staticmethod
    def environment_command(
        old_values,
        new_values,
        description: str = "Edit Scene Environment",
    ):
        """Create the canonical command used by property-field transactions."""
        from Infernux.engine.undo import SceneEnvironmentCommand

        return SceneEnvironmentCommand(
            copy.deepcopy(dict(old_values)),
            copy.deepcopy(dict(new_values)),
            str(description or "Edit Scene Environment"),
        )

    def set_environment(
        self,
        changes,
        *,
        description: str = "Edit Scene Environment",
        origin: ActionOrigin = ActionOrigin.USER,
    ) -> bool:
        """Apply scene-wide lighting/environment state through global history."""
        scene = self._active_scene()
        if scene is None:
            raise RuntimeError("Scene environment edit requires an active scene")
        updates = copy.deepcopy(dict(changes))
        if not updates:
            return False
        environment = scene.get_environment()
        old_values = {key: copy.deepcopy(environment.get(key)) for key in updates}
        if old_values == updates:
            return False

        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            raise RuntimeError("Scene environment edit requires the global Action Journal")
        command = self.environment_command(old_values, updates, description)
        if not manager.execute(command, origin=ActionOrigin(origin)):
            raise RuntimeError(f"Scene environment edit was rejected: {description}")
        return True

    @staticmethod
    def _context_object_ids(context: CommandContext) -> tuple[int, ...]:
        selection_ids = tuple(
            target.scene_object_id()
            for target in context.selection.targets
            if target.domain is SelectionDomain.SCENE_OBJECT
            and target.scene_object_id() > 0
        )
        payload = getattr(context, "payload", {}) or {}
        raw_explicit = payload.get("object_ids", ())
        if isinstance(raw_explicit, (str, int)):
            raw_explicit = (raw_explicit,)
        explicit: list[int] = []
        for value in raw_explicit:
            try:
                object_id = int(value)
            except (TypeError, ValueError):
                continue
            if object_id > 0 and object_id not in explicit:
                explicit.append(object_id)
        if not explicit:
            for key in ("object_id", "target_id"):
                try:
                    object_id = int(payload.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if object_id > 0:
                    explicit.append(object_id)
                    break
        if not explicit:
            return selection_ids
        if selection_ids and all(object_id in selection_ids for object_id in explicit):
            return selection_ids
        return tuple(explicit)

    @classmethod
    def has_selection(cls, context: CommandContext) -> bool:
        return bool(cls._context_object_ids(context))

    @staticmethod
    def _active_scene():
        from Infernux.lib import SceneManager

        return SceneManager.instance().get_active_scene()

    def _selected_roots(self, context: CommandContext):
        scene = self._active_scene()
        if scene is None:
            return []
        object_ids = self._context_object_ids(context)
        selected_ids = set(object_ids)
        roots = []
        for object_id in object_ids:
            obj = scene.find_by_id(object_id)
            if obj is None:
                continue
            parent = obj.get_parent()
            while parent is not None and parent.id not in selected_ids:
                parent = parent.get_parent()
            if parent is None:
                roots.append(obj)
        return roots

    def can_copy(self, context: CommandContext) -> bool:
        return bool(self.has_selection(context) and self._selected_roots(context))

    def copy(self, context: CommandContext, *, cut: bool) -> bool:
        from Infernux.engine.component_restore import (
            serialize_game_object_document_authoritatively,
        )
        from Infernux.engine.undo import DeleteGameObjectsCommand, UndoManager

        roots = self._selected_roots(context)
        if not roots:
            return False
        manager = UndoManager.instance()
        if cut and (
            manager is None or not manager.enabled or manager.is_executing
        ):
            return False

        entries = []
        for obj in roots:
            parent = obj.get_parent()
            transform = getattr(obj, "transform", None)
            entries.append(
                {
                    "document": serialize_game_object_document_authoritatively(obj),
                    "source_parent_id": parent.id if parent else None,
                    "source_sibling_index": (
                        transform.get_sibling_index() if transform else 0
                    ),
                    "source_world_position": (
                        transform.position.to_tuple() if transform else None
                    ),
                    "source_world_rotation": (
                        transform.rotation.to_tuple() if transform else None
                    ),
                }
            )

        published = self._clipboard.write(
            ClipboardDomain.SCENE_OBJECT,
            (
                ClipboardItem(str(obj.id), data=entry)
                for obj, entry in zip(roots, entries)
            ),
            operation=(ClipboardOperation.CUT if cut else ClipboardOperation.COPY),
            source_owner_id="scene_objects",
            reason="cut_scene_objects" if cut else "copy_scene_objects",
        )
        if not cut:
            return True
        if manager.execute(
            DeleteGameObjectsCommand([obj.id for obj in roots], "Cut GameObjects")
        ):
            return True
        self._clipboard.clear(
            expected_revision=published.revision,
            reason="cut_scene_objects_rejected",
        )
        return False

    def can_paste(self, _context: CommandContext) -> bool:
        return bool(
            self._active_scene() is not None
            and self._clipboard.has_payload(ClipboardDomain.SCENE_OBJECT)
        )

    def duplicate(self, context: CommandContext) -> bool:
        """Duplicate the current scene selection through the existing atomic paste path."""
        if not self.can_copy(context):
            return False
        return bool(self.copy(context, cut=False) and self.paste(context))

    def can_external_drop(
        self,
        reference: str,
        parent_id: int = 0,
        is_guid: bool = False,
    ) -> bool:
        """Return whether a project asset can be committed into the scene.

        The asset kind is deliberately not part of this predicate: Prefab and
        Model drops share the same scene/Undo/parent preconditions and perform
        their domain-specific validation when the command executes.
        """
        del is_guid
        from Infernux.engine.undo import UndoManager

        scene = self._active_scene()
        manager = UndoManager.instance()
        try:
            resolved_parent_id = int(parent_id or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            scene is not None
            and str(reference or "").strip()
            and resolved_parent_id >= 0
            and (
                resolved_parent_id == 0
                or scene.find_by_id(resolved_parent_id) is not None
            )
            and manager is not None
            and manager.enabled
            and not manager.is_executing
        )

    def paste(self, context: CommandContext) -> bool:
        from Infernux.engine.component_restore import (
            instantiate_prepared_game_object_documents,
            preflight_game_object_python_components,
        )
        from Infernux.engine.scene_manager import SceneFileManager
        from Infernux.engine.undo import (
            CompoundCommand,
            CreateGameObjectCommand,
            UndoManager,
        )
        from Infernux.lib import Vector3, quatf

        payload = self._clipboard.peek(ClipboardDomain.SCENE_OBJECT)
        if payload is None:
            return False
        entries = [item.data for item in payload.items if isinstance(item.data, dict)]
        scene = self._active_scene()
        manager = UndoManager.instance()
        if (
            scene is None
            or not entries
            or manager is None
            or not manager.enabled
            or manager.is_executing
        ):
            return False

        before_selection = self._selection.snapshot
        anchor_id = self._selection.primary_scene_object_id()
        anchor = scene.find_by_id(anchor_id) if anchor_id else None
        anchor_parent = anchor.get_parent() if anchor else None
        anchor_transform = getattr(anchor, "transform", None)
        anchor_index = (
            anchor_transform.get_sibling_index() if anchor_transform else -1
        )
        anchor_insert_index = anchor_index + 1 if anchor_index >= 0 else None
        scene_files = SceneFileManager.instance()
        asset_database = (
            getattr(scene_files, "_asset_database", None) if scene_files else None
        )
        prepared_entries = []
        try:
            for entry in entries:
                document = copy.deepcopy(entry["document"])
                prepared = preflight_game_object_python_components(
                    document,
                    asset_database=asset_database,
                    preserve_document_ids=False,
                    prefer_loaded_types=True,
                    reference_scene=scene,
                )
                prepared_entries.append((entry, document, prepared))
        except (KeyError, RuntimeError) as exc:
            for _entry, _document, prepared in prepared_entries:
                prepared.discard()
            Debug.log_error(f"Paste preflight failed: {exc}")
            return False

        created = []
        try:
            batch_entries = []
            for entry, document, prepared in prepared_entries:
                source_parent_id = entry.get("source_parent_id")
                source_parent = (
                    scene.find_by_id(source_parent_id)
                    if source_parent_id is not None
                    else None
                )
                parent = anchor_parent if anchor is not None else source_parent
                batch_entries.append((document, prepared, parent))
            created = instantiate_prepared_game_object_documents(scene, batch_entries)

            parent_offsets = {}
            for (entry, _document, _prepared), new_obj in zip(
                prepared_entries, created
            ):
                transform = getattr(new_obj, "transform", None)
                if transform is None:
                    continue
                world_position = entry.get("source_world_position")
                if world_position and len(world_position) == 3:
                    transform.position = Vector3(*(float(v) for v in world_position))
                world_rotation = entry.get("source_world_rotation")
                if world_rotation and len(world_rotation) == 4:
                    transform.rotation = quatf(*(float(v) for v in world_rotation))
                base_index = (
                    anchor_insert_index
                    if anchor_insert_index is not None
                    else int(entry.get("source_sibling_index", 0)) + 1
                )
                parent = new_obj.get_parent()
                parent_id = parent.id if parent else 0
                offset = parent_offsets.get(parent_id, 0)
                transform.set_sibling_index(max(0, base_index + offset))
                parent_offsets[parent_id] = offset + 1
        except Exception as exc:
            for instance in reversed(created):
                try:
                    scene.destroy_game_object(instance)
                except Exception as cleanup_exc:
                    Debug.log_suppressed(
                        "scene_objects.paste_cleanup",
                        cleanup_exc,
                    )
            if created:
                try:
                    scene.process_pending_destroys()
                except Exception as cleanup_exc:
                    Debug.log_suppressed(
                        "scene_objects.paste_cleanup_flush",
                        cleanup_exc,
                    )
            Debug.log_error(f"Paste commit failed: {exc}")
            return False
        finally:
            for _entry, _document, prepared in prepared_entries:
                prepared.discard()

        if not created:
            return False
        created_ids = [obj.id for obj in created]
        owner_id = context.focus.active_view_id or context.focus.active_panel_id
        self._selection.replace_scene_objects(
            created_ids,
            owner_id=owner_id or "hierarchy",
            reason="paste_scene_objects",
            record_history=False,
        )
        after_selection = self._selection.snapshot
        commands = [
            CreateGameObjectCommand(
                object_id,
                "Paste GameObject",
                before_selection=before_selection if index == 0 else None,
                after_selection=after_selection if index == 0 else None,
            )
            for index, object_id in enumerate(created_ids)
        ]
        command = (
            commands[0]
            if len(commands) == 1
            else CompoundCommand(commands, "Paste GameObjects")
        )
        manager.record(command)
        if payload.operation is ClipboardOperation.CUT:
            self._clipboard.consume_cut(payload.revision)
        return True

    def instantiate_prefab(
        self,
        reference: str,
        parent_id: int = 0,
        is_guid: bool = False,
    ) -> bool:
        """Instantiate one Prefab through the global scene mutation path."""
        from Infernux.engine.prefab_manager import (
            instantiate_prefab,
            read_prefab_source_canvas,
        )
        from Infernux.engine.undo import (
            CompoundCommand,
            CreateGameObjectCommand,
            UndoManager,
        )
        from Infernux.lib import AssetRegistry
        from Infernux.ui import UICanvas
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache

        ref = str(reference or "").strip()
        if not self.can_external_drop(ref, parent_id, is_guid):
            return None
        scene = self._active_scene()
        manager = UndoManager.instance()
        if scene is None or manager is None:
            return None

        before_selection = self._selection.snapshot
        parent = scene.find_by_id(int(parent_id)) if int(parent_id or 0) else None
        registry = AssetRegistry.instance()
        asset_database = registry.get_asset_database() if registry else None
        created_canvas = None
        new_object = None

        def rollback() -> None:
            for value in (new_object, created_canvas):
                if value is None:
                    continue
                try:
                    if scene.find_by_id(int(value.id)) is not None:
                        scene.destroy_game_object(value)
                except Exception as exc:
                    Debug.log_suppressed("scene_objects.prefab_drop.rollback", exc)
            try:
                scene.process_pending_destroys()
                invalidate_canvas_cache()
            except Exception as exc:
                Debug.log_suppressed("scene_objects.prefab_drop.rollback_flush", exc)
            self._selection.apply_snapshot(
                before_selection,
                reason="instantiate_prefab_rejected",
                record_history=False,
            )

        try:
            if parent is None:
                canvas_name = read_prefab_source_canvas(
                    file_path=None if is_guid else ref,
                    guid=ref if is_guid else None,
                    asset_database=asset_database,
                )
                if canvas_name:
                    for root in scene.get_root_objects():
                        if str(root.name) != str(canvas_name):
                            continue
                        if root.get_py_component(UICanvas) is not None:
                            parent = root
                            break
                    if parent is None:
                        created_canvas = scene.create_game_object(str(canvas_name))
                        if created_canvas is None:
                            raise RuntimeError(
                                f"Failed to create Canvas '{canvas_name}' for Prefab"
                            )
                        if created_canvas.add_py_component(UICanvas()) is None:
                            raise RuntimeError(
                                f"Failed to attach UICanvas to '{canvas_name}'"
                            )
                        invalidate_canvas_cache()
                        parent = created_canvas

            new_object = instantiate_prefab(
                guid=ref if is_guid else None,
                file_path=None if is_guid else ref,
                scene=scene,
                parent=parent,
                asset_database=asset_database,
            )
            if new_object is None:
                raise RuntimeError("Prefab instantiation returned no GameObject")
        except Exception as exc:
            rollback()
            Debug.log_error(f"Prefab instantiation failed: {exc}")
            return False

        self._selection.select_scene_object(
            int(new_object.id),
            owner_id="hierarchy",
            reason="instantiate_prefab",
            record_history=False,
        )
        after_selection = self._selection.snapshot
        commands = []
        if created_canvas is not None:
            commands.append(
                CreateGameObjectCommand(
                    int(created_canvas.id),
                    "Instantiate Prefab",
                )
            )
        commands.append(
            CreateGameObjectCommand(
                int(new_object.id),
                "Instantiate Prefab",
                before_selection=before_selection,
                after_selection=after_selection,
            )
        )
        command = (
            commands[0]
            if len(commands) == 1
            else CompoundCommand(commands, "Instantiate Prefab")
        )
        if manager.record(command):
            return True
        rollback()
        return False

    def create_model(
        self,
        reference: str,
        parent_id: int = 0,
        is_guid: bool = False,
    ) -> bool:
        """Create one imported-model GameObject through the global journal."""
        return self.create_model_object(
            reference,
            parent_id=parent_id,
            is_guid=is_guid,
        ) is not None

    def create_model_object(
        self,
        reference: str,
        *,
        parent_id: int = 0,
        is_guid: bool = False,
        name: str = "",
        select: bool = True,
        selection_owner_id: str = "hierarchy",
        selection_reason: str = "create_model",
    ):
        """Create an imported model and return the committed object."""
        from Infernux.engine.undo import CreateGameObjectCommand, UndoManager
        from Infernux.lib import AssetRegistry

        ref = str(reference or "").strip()
        if not self.can_external_drop(ref, parent_id, is_guid):
            return False
        scene = self._active_scene()
        manager = UndoManager.instance()
        if scene is None or manager is None:
            return False

        guid = ref if is_guid else ""
        if not guid:
            registry = AssetRegistry.instance()
            asset_database = registry.get_asset_database() if registry else None
            guid = asset_database.get_guid_from_path(ref) if asset_database else ""
        if not guid:
            return None

        before_selection = self._selection.snapshot
        new_object = scene.create_from_model(guid)
        if new_object is None:
            return None
        parent = scene.find_by_id(int(parent_id)) if int(parent_id or 0) else None
        if int(parent_id or 0) and parent is None:
            scene.destroy_game_object(new_object)
            scene.process_pending_destroys()
            return None
        if parent is not None:
            new_object.set_parent(parent)
        if name:
            new_object.name = str(name)
        if select:
            self._selection.select_scene_object(
                int(new_object.id),
                owner_id=str(selection_owner_id or "hierarchy"),
                reason=str(selection_reason or "create_model"),
                record_history=False,
            )
        command = CreateGameObjectCommand(
            int(new_object.id),
            "Create Model",
            before_selection=before_selection,
            after_selection=self._selection.snapshot,
        )
        if manager.record(command):
            return new_object
        try:
            scene.destroy_game_object(new_object)
            scene.process_pending_destroys()
        finally:
            self._selection.apply_snapshot(
                before_selection,
                reason="create_model_rejected",
                record_history=False,
            )
        return None

    def delete(self, context: CommandContext) -> bool:
        roots = self._selected_roots(context)
        return self.delete_ids(
            (obj.id for obj in roots),
            description="Delete GameObjects",
        )

    def delete_ids(
        self,
        object_ids,
        *,
        description: str = "Delete GameObjects",
    ) -> bool:
        """Delete explicit objects through the same atomic hierarchy command."""
        from Infernux.engine.undo import DeleteGameObjectsCommand, UndoManager

        ids = []
        for value in object_ids:
            try:
                object_id = int(value)
            except (TypeError, ValueError):
                return False
            if object_id > 0 and object_id not in ids:
                ids.append(object_id)
        manager = UndoManager.instance()
        if (
            not ids
            or manager is None
            or not manager.enabled
            or manager.is_executing
        ):
            return False
        return bool(
            manager.execute(DeleteGameObjectsCommand(ids, str(description)))
        )

    def duplicate_object(
        self,
        object_id: int,
        *,
        parent_id: int = 0,
        name: str = "",
        select: bool = True,
        selection_owner_id: str = "hierarchy",
        selection_reason: str = "duplicate_game_object",
    ):
        """Duplicate one object and publish exactly one global history entry."""
        from Infernux.engine.component_restore import clone_game_object_transactionally
        from Infernux.engine.undo import CreateGameObjectCommand, UndoManager

        scene = self._active_scene()
        manager = UndoManager.instance()
        if scene is None or manager is None or not manager.enabled or manager.is_executing:
            return None
        source = scene.find_by_id(int(object_id))
        parent = scene.find_by_id(int(parent_id)) if int(parent_id or 0) else None
        if source is None or (int(parent_id or 0) and parent is None):
            return None

        before_selection = self._selection.snapshot
        duplicate = clone_game_object_transactionally(scene, source, parent)
        if duplicate is None:
            return None
        try:
            if name:
                duplicate.name = str(name)
            if select:
                self._selection.select_scene_object(
                    int(duplicate.id),
                    owner_id=str(selection_owner_id or "hierarchy"),
                    reason=str(selection_reason or "duplicate_game_object"),
                    record_history=False,
                )
            after_selection = self._selection.snapshot
            command = CreateGameObjectCommand(
                int(duplicate.id),
                "Duplicate GameObject",
                before_selection=before_selection,
                after_selection=after_selection,
            )
            if manager.record(command):
                return duplicate
        except Exception as exc:
            Debug.log_suppressed("scene_objects.duplicate.commit", exc)

        try:
            scene.destroy_game_object(duplicate)
            scene.process_pending_destroys()
        finally:
            self._selection.apply_snapshot(
                before_selection,
                reason="duplicate_game_object_rejected",
                record_history=False,
            )
        return None

    def rename(self, object_id: int, new_name: str) -> bool:
        """Rename one object through the global journal."""
        from Infernux.engine.undo import SetPropertyCommand, UndoManager

        try:
            normalized_id = int(object_id)
        except (TypeError, ValueError):
            return False
        scene = self._active_scene()
        obj = scene.find_by_id(normalized_id) if scene is not None else None
        value = str(new_name or "").strip()
        if obj is None or not value or obj.name == value:
            return False
        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return False
        return bool(
            manager.execute(
                SetPropertyCommand(
                    obj,
                    "name",
                    str(obj.name),
                    value,
                    "Rename GameObject",
                )
            )
        )

    @staticmethod
    def _normalize_object_property(property_name: str, value):
        name = str(property_name or "").strip()
        if name == "active":
            if isinstance(value, bool):
                return name, value
            if isinstance(value, (int, float)) and value in (0, 1):
                return name, bool(value)
            if isinstance(value, str) and value.strip().lower() in {
                "true",
                "false",
                "1",
                "0",
            }:
                return name, value.strip().lower() in {"true", "1"}
            raise ValueError("GameObject.active requires a boolean")
        if name in {"name", "tag"}:
            if not isinstance(value, str):
                raise ValueError(f"GameObject.{name} requires a string")
            return name, value
        if name == "layer":
            if isinstance(value, bool):
                raise ValueError("GameObject.layer requires an integer")
            try:
                layer = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("GameObject.layer requires an integer") from exc
            if layer < 0 or layer > 31:
                raise ValueError("GameObject.layer must be between 0 and 31")
            return name, layer
        raise ValueError(f"unsupported GameObject property: {name or '<empty>'}")

    def can_set_object_property(
        self,
        object_id: int,
        property_name: str,
        value,
    ) -> bool:
        try:
            normalized_id = int(object_id)
        except (TypeError, ValueError):
            return False
        scene = self._active_scene()
        obj = scene.find_by_id(normalized_id) if scene is not None else None
        if obj is None:
            return False
        try:
            name, normalized = self._normalize_object_property(property_name, value)
        except (TypeError, ValueError):
            return False
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        return bool(
            manager is not None
            and manager.enabled
            and not manager.is_executing
            and getattr(obj, name, None) != normalized
        )

    def set_object_property(
        self,
        object_id: int,
        property_name: str,
        value,
    ) -> bool:
        """Set an Inspector-visible GameObject property through global history."""
        try:
            normalized_id = int(object_id)
        except (TypeError, ValueError):
            return False
        scene = self._active_scene()
        obj = scene.find_by_id(normalized_id) if scene is not None else None
        if obj is None:
            return False
        try:
            name, normalized = self._normalize_object_property(property_name, value)
        except (TypeError, ValueError):
            return False

        from .serialized_properties import (
            PropertyTransactionStatus,
            make_attribute_property_transaction,
        )

        transaction = make_attribute_property_transaction(
            (obj,),
            name,
            property_path=f"GameObject.{name}",
            description={
                "active": "Set GameObject Active",
                "name": "Rename GameObject",
                "tag": "Set GameObject Tag",
                "layer": "Set GameObject Layer",
            }[name],
            publish=self._publish_change,
        )
        return transaction.commit(normalized) is PropertyTransactionStatus.APPLIED

    @staticmethod
    def _normalize_transform_values(values: Mapping[str, object]) -> dict[str, list[float]]:
        if not isinstance(values, Mapping):
            raise ValueError("transform value must be an object")
        result: dict[str, list[float]] = {}
        for field in ("position", "rotation", "scale"):
            raw = values.get(field)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or len(raw) != 3:
                raise ValueError(f"transform {field} must contain exactly three values")
            vector = [float(component) for component in raw]
            if not all(math.isfinite(component) for component in vector):
                raise ValueError(f"transform {field} must contain finite values")
            result[field] = vector
        return result

    @classmethod
    def _transform_snapshot_with_values(cls, snapshot, values):
        result = copy.deepcopy(snapshot)
        result.update(cls._normalize_transform_values(values))
        return result

    def _prepare_transform_edit(self, object_ids, transform_values):
        try:
            ids = tuple(int(object_id) for object_id in object_ids)
            values = tuple(transform_values)
        except (TypeError, ValueError):
            return None
        if (
            not ids
            or len(ids) != len(values)
            or any(object_id <= 0 for object_id in ids)
            or len(set(ids)) != len(ids)
        ):
            return None

        from Infernux.engine.undo import snapshot_live_transform

        try:
            before = tuple(snapshot_live_transform(object_id) for object_id in ids)
            if any(snapshot is None for snapshot in before):
                return None
            after = tuple(
                self._transform_snapshot_with_values(snapshot, value)
                for snapshot, value in zip(before, values)
            )
        except (TypeError, ValueError, RuntimeError):
            return None
        return ids, before, after

    def can_set_transforms(self, object_ids, transform_values, gesture_id="") -> bool:
        prepared = self._prepare_transform_edit(object_ids, transform_values)
        if prepared is None:
            return False
        _ids, before, after = prepared
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        return bool(
            before != after
            and manager is not None
            and manager.enabled
            and not manager.is_executing
        )

    def set_transforms(self, object_ids, transform_values, gesture_id="") -> bool:
        """Commit one single- or multi-object Transform edit atomically."""
        prepared = self._prepare_transform_edit(object_ids, transform_values)
        if prepared is None:
            return False
        ids, before, after = prepared
        if before == after:
            return False

        from .serialized_properties import (
            PropertyTransactionStatus,
            SnapshotPropertyTransaction,
        )
        from Infernux.engine.undo import restore_live_transform, snapshot_live_transform

        def capture():
            snapshots = tuple(snapshot_live_transform(object_id) for object_id in ids)
            if any(snapshot is None for snapshot in snapshots):
                raise RuntimeError("transform target is no longer available")
            return snapshots

        def restore(snapshots):
            if len(snapshots) != len(ids):
                raise RuntimeError("transform batch identity count changed")
            for object_id, snapshot in zip(ids, snapshots):
                restore_live_transform(object_id, snapshot)
            self._publish_change()

        transaction = SnapshotPropertyTransaction(
            "Transform:" + ",".join(str(object_id) for object_id in ids),
            capture,
            restore,
            "Edit Transform" if len(ids) == 1 else "Edit Transforms",
            gesture_id=str(gesture_id or "").strip(),
        )
        return transaction.commit(after) is PropertyTransactionStatus.APPLIED

    @staticmethod
    def _parent_id(obj):
        parent = obj.get_parent()
        return int(parent.id) if parent is not None else None

    @staticmethod
    def _children_ids(scene, parent_id):
        if parent_id is None:
            children = scene.get_root_objects()
        else:
            parent = scene.find_by_id(parent_id)
            if parent is None:
                raise RuntimeError(f"hierarchy parent is unavailable: {parent_id}")
            children = parent.get_children()
        return [int(child.id) for child in children]

    @staticmethod
    def _is_descendant(candidate, ancestor) -> bool:
        current = candidate
        while current is not None:
            if current is ancestor:
                return True
            current = current.get_parent()
        return False

    def move_hierarchy(
        self,
        object_ids,
        mode: str,
        target_id: int = 0,
        after: bool = False,
    ) -> bool:
        """Apply one atomic multi-object hierarchy gesture.

        ``mode`` is ``parent``, ``adjacent``, or ``root``. The native tree is
        responsible only for hit testing and UI-domain filtering; this service
        resolves the authoritative before/after hierarchy layout and owns the
        single Undo entry.
        """
        from Infernux.engine.undo import SceneHierarchyLayoutCommand, UndoManager

        scene = self._active_scene()
        manager = UndoManager.instance()
        normalized_ids = []
        for value in object_ids:
            try:
                object_id = int(value)
            except (TypeError, ValueError):
                return False
            if object_id > 0 and object_id not in normalized_ids:
                normalized_ids.append(object_id)
        ids = tuple(normalized_ids)
        operation = str(mode or "").strip().lower()
        if (
            scene is None
            or not ids
            or operation not in {"parent", "adjacent", "root"}
            or manager is None
            or not manager.enabled
            or manager.is_executing
        ):
            return False

        objects = []
        for object_id in ids:
            obj = scene.find_by_id(object_id)
            if obj is None:
                return False
            objects.append(obj)

        selected = set(ids)
        top_level_objects = []
        for obj in objects:
            parent = obj.get_parent()
            has_selected_ancestor = False
            while parent is not None:
                if int(parent.id) in selected:
                    has_selected_ancestor = True
                    break
                parent = parent.get_parent()
            if not has_selected_ancestor:
                top_level_objects.append(obj)
        objects = top_level_objects
        ids = tuple(int(obj.id) for obj in objects)
        selected = set(ids)
        if not ids:
            return False

        target = scene.find_by_id(int(target_id)) if int(target_id or 0) else None
        if operation == "parent":
            if target is None or int(target.id) in selected:
                return False
            if any(self._is_descendant(target, obj) for obj in objects):
                return False
            destination_parent_id = int(target.id)
        elif operation == "adjacent":
            if target is None or int(target.id) in selected:
                return False
            destination_parent_id = self._parent_id(target)
        else:
            destination_parent_id = None

        affected_parent_ids = {
            self._parent_id(obj) for obj in objects
        } | {destination_parent_id}
        before = {
            parent_id: tuple(self._children_ids(scene, parent_id))
            for parent_id in affected_parent_ids
        }
        after_layout = {
            parent_id: [
                object_id
                for object_id in object_ids
                if object_id not in selected
            ]
            for parent_id, object_ids in before.items()
        }

        destination = after_layout[destination_parent_id]
        if operation == "adjacent":
            anchor_id = int(target.id)
            try:
                insert_index = destination.index(anchor_id)
            except ValueError:
                return False
            if after:
                insert_index += 1
            destination[insert_index:insert_index] = ids
        else:
            destination.extend(ids)

        after_snapshot = {
            parent_id: tuple(object_ids)
            for parent_id, object_ids in after_layout.items()
        }
        if before == after_snapshot:
            return False
        description = "Move GameObject" if len(ids) == 1 else "Move GameObjects"
        return bool(
            manager.execute(
                SceneHierarchyLayoutCommand(
                    before,
                    after_snapshot,
                    description,
                )
            )
        )

    def set_sibling_index(self, object_id: int, index: int) -> bool:
        """Move one object within its sibling list through one layout diff."""
        from Infernux.engine.undo import SceneHierarchyLayoutCommand, UndoManager

        scene = self._active_scene()
        manager = UndoManager.instance()
        obj = scene.find_by_id(int(object_id)) if scene is not None else None
        if obj is None or manager is None or not manager.enabled or manager.is_executing:
            return False
        parent_id = self._parent_id(obj)
        before_ids = self._children_ids(scene, parent_id)
        try:
            old_index = before_ids.index(int(object_id))
        except ValueError:
            return False
        after_ids = list(before_ids)
        after_ids.pop(old_index)
        target_index = max(0, min(int(index), len(after_ids)))
        after_ids.insert(target_index, int(object_id))
        if after_ids == before_ids:
            return False
        return bool(
            manager.execute(
                SceneHierarchyLayoutCommand(
                    {parent_id: tuple(before_ids)},
                    {parent_id: tuple(after_ids)},
                    "Move GameObject",
                )
            )
        )

    def copy_selected(self, cut: bool = False) -> bool:
        return self.copy(self._command_context(), cut=bool(cut))

    def paste_clipboard(self) -> bool:
        return self.paste(self._command_context())

    def delete_selected(self) -> bool:
        return self.delete(self._command_context())

    def has_clipboard_data(self) -> bool:
        return self._clipboard.has_payload(ClipboardDomain.SCENE_OBJECT)

    @staticmethod
    def _command_context() -> CommandContext:
        from .commands import CommandSource, EditorCommandRegistry

        return EditorCommandRegistry.instance().context(CommandSource.API)
