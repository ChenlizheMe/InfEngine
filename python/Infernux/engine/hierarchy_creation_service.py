"""Shared Hierarchy object creation service.

Both the C++ HierarchyPanel callbacks and MCP tools use this service so editor
UI creation and agent-driven creation stay behaviorally identical.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable, Optional

from Infernux.debug import Debug


class HierarchyCreationService:
    _instance: Optional["HierarchyCreationService"] = None
    _kind_registry: dict[str, dict[str, Any]] = {}
    _kind_factories: dict[str, Callable[[Any, int], Any]] = {}
    _defaults_registered: bool = False

    def __init__(self) -> None:
        self._selection_service = None
        self._navigation_service = None
        self._ensure_default_kinds()

    @classmethod
    def instance(cls) -> "HierarchyCreationService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def configure(self, *, selection_service=None, navigation_service=None) -> None:
        self._selection_service = selection_service
        self._navigation_service = navigation_service

    def _reveal_created_object(self, object_id: int, *, record_history: bool) -> None:
        navigation = self._navigation_service
        if navigation is None or int(object_id or 0) <= 0:
            return
        from Infernux.engine.interaction import SelectionTarget
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        # Commands execute inside one global user_action. Direct service calls
        # still reveal the object, but cannot manufacture a second history
        # entry after their creation command has already been recorded.
        record_reveal = bool(
            record_history
            and manager is not None
            and manager.is_user_action_active
        )
        navigation.reveal(
            SelectionTarget.scene_object(int(object_id)),
            record_history=record_reveal,
            activate_panel=False,
        )

    @classmethod
    def register_create_kind(
        cls,
        kind: str,
        label: str,
        *,
        category: str = "",
        description: str = "",
        factory: Callable[[Any, int], Any] | None = None,
    ) -> None:
        """Register a Hierarchy creation kind.

        This is intentionally shared by UI and MCP. Future editor modules can
        add objects (including UI widgets) without editing MCP code.
        """
        normalized = str(kind).strip()
        if not normalized:
            raise ValueError("Create kind cannot be empty.")
        cls._kind_registry[normalized] = {
            "kind": normalized,
            "label": str(label or normalized),
            "category": str(category or ""),
            "description": str(description or ""),
        }
        if factory is not None:
            cls._kind_factories[normalized] = factory

    @classmethod
    def unregister_create_kind(cls, kind: str) -> None:
        normalized = str(kind).strip()
        cls._kind_registry.pop(normalized, None)
        cls._kind_factories.pop(normalized, None)

    @classmethod
    def _ensure_default_kinds(cls) -> None:
        if cls._defaults_registered:
            return
        cls._defaults_registered = True
        defaults = [
            ("empty", "Empty", "General"),
            ("primitive.cube", "Cube", "3D Object"),
            ("primitive.sphere", "Sphere", "3D Object"),
            ("primitive.capsule", "Capsule", "3D Object"),
            ("primitive.cylinder", "Cylinder", "3D Object"),
            ("primitive.plane", "Plane", "3D Object"),
            ("primitive.quad", "Quad", "3D Object"),
            ("light.directional", "Directional Light", "Light"),
            ("light.point", "Point Light", "Light"),
            ("light.spot", "Spot Light", "Light"),
            ("rendering.camera", "Camera", "Rendering"),
            ("rendering.render_stack", "RenderStack", "Rendering"),
            ("rendering.sprite_renderer", "Sprite Renderer", "Rendering"),
            ("effect.particle_system", "Particle System", "Effect"),
            ("ui.canvas", "Canvas", "UI"),
            ("ui.image", "Image", "UI"),
            ("ui.text", "Text", "UI"),
            ("ui.button", "Button", "UI"),
        ]
        for kind, label, category in defaults:
            cls.register_create_kind(kind, label, category=category)

    def list_create_kinds(self) -> list[dict[str, str]]:
        self._ensure_default_kinds()
        return [
            dict(value)
            for _kind, value in sorted(self._kind_registry.items(), key=lambda item: (item[1].get("category", ""), item[1].get("label", "")))
        ]

    def can_create(self, kind: str, *, parent_id: int = 0) -> bool:
        self._ensure_default_kinds()
        normalized = str(kind or "").strip()
        if normalized not in self._kind_registry:
            return False
        try:
            from Infernux.lib import SceneManager

            scene = SceneManager.instance().get_active_scene()
            if scene is None:
                return False
            resolved_parent = int(parent_id or 0)
            return not resolved_parent or scene.find_by_id(resolved_parent) is not None
        except (RuntimeError, TypeError, ValueError):
            return False

    def can_create_empty_parent(self, object_ids: list[int] | None = None) -> bool:
        ids = object_ids
        if ids is None and self._selection_service is not None:
            ids = list(self._selection_service.scene_object_ids())
        return bool(ids and any(int(value or 0) > 0 for value in ids))

    def create(
        self,
        kind: str,
        *,
        parent_id: int = 0,
        name: str | None = None,
        select: bool = True,
        record_undo: bool = True,
        selection_owner_id: str = "hierarchy",
        selection_reason: str = "create_game_object",
        configure_created: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        from Infernux.lib import SceneManager
        from Infernux.engine.interaction import SelectionService

        scene = SceneManager.instance().get_active_scene()
        if not scene:
            raise RuntimeError("No active scene.")

        parent_id = int(parent_id or 0)
        effective_parent_id = parent_id
        if kind in {"ui.image", "ui.text", "ui.button"}:
            effective_parent_id = self._find_canvas_parent_id(scene, parent_id)

        if effective_parent_id:
            parent = scene.find_by_id(effective_parent_id)
            if parent is None:
                raise ValueError(f"Parent GameObject {effective_parent_id} was not found.")

        before_selection = SelectionService.instance().snapshot
        obj = None
        try:
            from Infernux.engine.undo import UndoManager

            manager = UndoManager.instance()
            initialization_scope = (
                manager.suppress_property_recording()
                if manager is not None
                else nullcontext()
            )
            with initialization_scope:
                obj = self._create_raw(scene, kind, effective_parent_id)
                if obj is None:
                    raise RuntimeError(
                        f"Failed to create hierarchy object kind '{kind}'."
                    )

                if name:
                    obj.name = str(name)
                else:
                    obj.name = _unique_scene_object_name(
                        scene,
                        str(obj.name),
                        exclude_id=int(getattr(obj, "id", 0) or 0),
                    )
                self._finalize(
                    obj,
                    effective_parent_id,
                    self._description_for(kind),
                    select=select,
                    record_undo=record_undo,
                    before_selection=before_selection,
                    selection_owner_id=selection_owner_id,
                    selection_reason=selection_reason,
                    configure_created=configure_created,
                )
        except Exception:
            if obj is not None:
                try:
                    scene.destroy_game_object(obj)
                    scene.process_pending_destroys()
                except Exception as cleanup_exc:
                    Debug.log_suppressed(
                        "hierarchy.create.rollback",
                        cleanup_exc,
                    )
            SelectionService.instance().apply_snapshot(
                before_selection,
                reason="hierarchy_create_rejected",
                record_history=False,
            )
            raise
        return self._serialize_created(obj, kind, selected=select)

    def create_empty_parent(self, object_ids: list[int] | None = None) -> dict[str, Any]:
        """Create an Empty that becomes the parent of the given selection.

        Supports multi-select: only topmost selected objects are reparented
        (descendants of other selected objects are left under those objects).
        World transforms of children are preserved.
        """
        from Infernux.lib import SceneManager, Vector3
        from Infernux.engine.undo import (
            CompoundCommand,
            CreateGameObjectCommand,
            MoveGameObjectCommand,
            UndoManager,
        )
        from Infernux.engine.interaction import SelectionService, SelectionTarget

        scene = SceneManager.instance().get_active_scene()
        if not scene:
            raise RuntimeError("No active scene.")
        mgr = UndoManager.instance()
        if mgr is None or not mgr.enabled or mgr.is_executing:
            raise RuntimeError("Global editor history is unavailable.")

        raw_ids = object_ids
        if raw_ids is None and self._selection_service is not None:
            raw_ids = list(self._selection_service.scene_object_ids())
        ids = [int(i) for i in (raw_ids or []) if int(i or 0)]
        if not ids:
            raise ValueError("No objects selected for Create Empty Parent.")

        selected_set = set(ids)
        objects = []
        for oid in ids:
            obj = scene.find_by_id(oid)
            if obj is not None:
                objects.append(obj)
        if not objects:
            raise ValueError("Selected GameObjects were not found.")

        topmost = []
        for obj in objects:
            ancestor = obj.get_parent()
            under_selected = False
            while ancestor is not None:
                if int(ancestor.id) in selected_set:
                    under_selected = True
                    break
                ancestor = ancestor.get_parent()
            if not under_selected:
                topmost.append(obj)
        if not topmost:
            raise ValueError("No valid objects for Create Empty Parent.")

        common_parent = _deepest_common_parent(topmost)
        insert_index = _min_sibling_index_under_parent(topmost, common_parent)

        selection = SelectionService.instance()
        before_selection = selection.snapshot
        parent_go = scene.create_game_object("GameObject")
        if parent_go is None:
            raise RuntimeError("Failed to create empty parent GameObject.")
        parent_go.name = _unique_scene_object_name(
            scene, str(parent_go.name), exclude_id=int(parent_go.id)
        )
        if common_parent is not None:
            parent_go.set_parent(common_parent, True)
        parent_tf = getattr(parent_go, "transform", None)
        if parent_tf is not None:
            parent_tf.set_sibling_index(max(0, int(insert_index)))

        avg = _average_world_position(topmost)
        if avg is not None and parent_tf is not None:
            parent_tf.position = Vector3(avg[0], avg[1], avg[2])

        move_cmds: list[MoveGameObjectCommand] = []
        for child in topmost:
            # Refuse parenting a selected ancestor onto the new empty if that
            # would create a cycle — topmost filtering already prevents this.
            if int(child.id) == int(parent_go.id):
                continue
            old_parent = child.get_parent()
            old_parent_id = int(old_parent.id) if old_parent is not None else None
            child_tf = getattr(child, "transform", None)
            old_sibling = int(child_tf.get_sibling_index()) if child_tf is not None else 0
            child.set_parent(parent_go, True)
            new_sibling = int(child_tf.get_sibling_index()) if child_tf is not None else 0
            move_cmds.append(
                MoveGameObjectCommand(
                    int(child.id),
                    old_parent_id,
                    int(parent_go.id),
                    old_sibling,
                    new_sibling,
                    "Create Empty Parent",
                )
            )

        selection.select(
            SelectionTarget.scene_object(parent_go.id),
            owner_id="hierarchy",
            reason="create_empty_parent",
            record_history=False,
        )
        after_selection = selection.snapshot

        cmds = [CreateGameObjectCommand(
            int(parent_go.id),
            "Create Empty Parent",
            before_selection=before_selection,
            after_selection=after_selection,
        )]
        cmds.extend(move_cmds)
        mgr.record(CompoundCommand(cmds, "Create Empty Parent"))

        self._reveal_created_object(parent_go.id, record_history=True)

        return self._serialize_created(parent_go, "empty", selected=True)

    def _create_raw(self, scene, kind: str, parent_id: int):
        self._ensure_default_kinds()
        factory = self._kind_factories.get(kind)
        if factory is not None:
            return factory(scene, parent_id)
        if kind == "empty":
            return scene.create_game_object("GameObject")
        if kind.startswith("primitive."):
            return self._create_primitive(scene, kind)
        if kind.startswith("light."):
            return self._create_light(scene, kind)
        if kind == "rendering.camera":
            obj = scene.create_game_object("Camera")
            if obj:
                obj.add_component("Camera")
            return obj
        if kind == "rendering.render_stack":
            from Infernux.renderstack import RenderStack as RenderStackCls
            obj = scene.create_game_object("RenderStack")
            if obj and obj.add_py_component(RenderStackCls()) is None:
                scene.destroy_game_object(obj)
                return None
            return obj
        if kind == "rendering.sprite_renderer":
            obj = scene.create_game_object("Sprite")
            if not obj:
                return None
            cpp_comp = obj.add_component("SpriteRenderer")
            if cpp_comp is None:
                scene.destroy_game_object(obj)
                return None
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer
            SpriteRenderer._get_or_create_wrapper(cpp_comp, obj)
            return obj
        if kind == "effect.particle_system":
            return self._create_particle_system(scene)
        if kind == "ui.canvas":
            return self._create_ui_canvas(scene)
        if kind == "ui.text":
            return self._create_ui_text(scene, parent_id)
        if kind == "ui.image":
            return self._create_ui_image(scene, parent_id)
        if kind == "ui.button":
            return self._create_ui_button(scene, parent_id)
        raise ValueError(f"Unknown hierarchy create kind: {kind}")

    def _create_primitive(self, scene, kind: str):
        from Infernux.lib import PrimitiveType
        primitive_types = {
            "primitive.cube": PrimitiveType.Cube,
            "primitive.sphere": PrimitiveType.Sphere,
            "primitive.capsule": PrimitiveType.Capsule,
            "primitive.cylinder": PrimitiveType.Cylinder,
            "primitive.plane": PrimitiveType.Plane,
            "primitive.quad": PrimitiveType.Quad,
        }
        primitive_type = primitive_types.get(kind)
        if primitive_type is None:
            raise ValueError(f"Unknown primitive kind: {kind}")
        return scene.create_primitive(primitive_type)

    def _create_light(self, scene, kind: str):
        from Infernux.lib import LightShadows, LightType, Vector3
        light_types = {
            "light.directional": ("Directional Light", LightType.Directional),
            "light.point": ("Point Light", LightType.Point),
            "light.spot": ("Spot Light", LightType.Spot),
        }
        entry = light_types.get(kind)
        if entry is None:
            raise ValueError(f"Unknown light kind: {kind}")
        name, light_type = entry
        obj = scene.create_game_object(name)
        if not obj:
            return None
        light_comp = obj.add_component("Light")
        if light_comp:
            light_comp.light_type = light_type
            light_comp.shadows = LightShadows.Hard
            if light_type == LightType.Directional and obj.transform:
                obj.transform.euler_angles = Vector3(50.0, -30.0, 0.0)
            elif light_type == LightType.Point:
                light_comp.range = 10.0
            elif light_type == LightType.Spot:
                light_comp.range = 10.0
                light_comp.outer_spot_angle = 45.0
                light_comp.spot_angle = 30.0
        return obj

    def _create_particle_system(self, scene):
        from Infernux.components.particle_system import ParticleSystem
        obj = scene.create_game_object("Particle System")
        if obj and obj.add_py_component(ParticleSystem()) is None:
            scene.destroy_game_object(obj)
            return None
        return obj

    def _create_ui_canvas(self, scene):
        from Infernux.ui import UICanvas as UICanvasCls
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Canvas")
        if obj:
            obj.add_py_component(UICanvasCls())
            invalidate_canvas_cache()
        return obj

    def _create_ui_text(self, scene, parent_id: int):
        from Infernux.ui import UIText as UITextCls
        from Infernux.ui.enums import ScreenAlignH, ScreenAlignV
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Text")
        if obj:
            text = UITextCls()
            text.align_h = ScreenAlignH.Center
            text.align_v = ScreenAlignV.Center
            text.x = -80.0
            text.y = -20.0
            obj.add_py_component(text)
            invalidate_canvas_cache()
        return obj

    def _create_ui_image(self, scene, parent_id: int):
        from Infernux.ui import UIImage as UIImageCls
        from Infernux.ui.enums import ScreenAlignH, ScreenAlignV
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Image")
        if obj:
            image = UIImageCls()
            image.width = 100.0
            image.height = 100.0
            image.align_h = ScreenAlignH.Center
            image.align_v = ScreenAlignV.Center
            image.x = -50.0
            image.y = -50.0
            obj.add_py_component(image)
            invalidate_canvas_cache()
        return obj

    def _create_ui_button(self, scene, parent_id: int):
        from Infernux.ui import UIButton as UIButtonCls
        from Infernux.ui.enums import ScreenAlignH, ScreenAlignV
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Button")
        if obj:
            button = UIButtonCls()
            button.width = 160.0
            button.height = 40.0
            button.align_h = ScreenAlignH.Center
            button.align_v = ScreenAlignV.Center
            button.x = -80.0
            button.y = -20.0
            obj.add_py_component(button)
            invalidate_canvas_cache()
        return obj

    def _find_canvas_parent_id(self, scene, parent_id: int) -> int:
        from Infernux.ui import UICanvas

        candidate_ids = []
        if parent_id:
            candidate_ids.append(int(parent_id))
        selection = self._selection_service
        if selection is not None:
            selected_id = int(selection.primary_scene_object_id() or 0)
            if selected_id and selected_id not in candidate_ids:
                candidate_ids.append(selected_id)

        for candidate_id in candidate_ids:
            current = scene.find_by_id(candidate_id)
            while current is not None:
                if any(isinstance(comp, UICanvas) for comp in _get_py_components_safe(current)):
                    return int(current.id)
                current = current.get_parent()

        canvases = [
            obj
            for obj in scene.get_all_objects()
            if any(isinstance(comp, UICanvas) for comp in _get_py_components_safe(obj))
        ]
        if len(canvases) == 1:
            return int(canvases[0].id)
        return 0

    def _finalize(
        self,
        obj,
        parent_id: int,
        description: str,
        *,
        select: bool,
        record_undo: bool,
        before_selection=None,
        selection_owner_id: str = "hierarchy",
        selection_reason: str = "create_game_object",
        configure_created: Callable[[Any], None] | None = None,
    ) -> None:
        if parent_id:
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            parent = scene.find_by_id(parent_id) if scene else None
            if parent:
                obj.set_parent(parent)

        if configure_created is not None:
            configure_created(obj)

        from Infernux.engine.interaction import SelectionService, SelectionTarget

        selection = SelectionService.instance()
        if before_selection is None:
            before_selection = selection.snapshot
        if select:
            selection.select(
                SelectionTarget.scene_object(obj.id),
                owner_id=str(selection_owner_id or "hierarchy"),
                reason=str(selection_reason or "create_game_object"),
                record_history=False,
            )
        after_selection = selection.snapshot

        if record_undo:
            from Infernux.engine.undo import CreateGameObjectCommand, UndoManager

            manager = UndoManager.instance()
            if manager is None or not manager.enabled or manager.is_executing:
                raise RuntimeError("Global editor history is unavailable.")
            recorded = manager.record(
                CreateGameObjectCommand(
                    obj.id,
                    description,
                    before_selection=before_selection,
                    after_selection=after_selection,
                )
            )
            if recorded is False:
                raise RuntimeError("Global editor history rejected hierarchy creation.")

        if select:
            self._reveal_created_object(obj.id, record_history=record_undo)

    def _description_for(self, kind: str) -> str:
        if kind.startswith("primitive."):
            return "Create Primitive"
        if kind.startswith("light."):
            return "Create Light"
        if kind == "empty":
            return "Create Empty"
        if kind == "rendering.camera":
            return "Create Camera"
        if kind == "rendering.render_stack":
            return "Create RenderStack"
        if kind == "rendering.sprite_renderer":
            return "Create Sprite Renderer"
        if kind == "effect.particle_system":
            return "Create Particle System"
        if kind == "ui.canvas":
            return "Create Canvas"
        if kind == "ui.text":
            return "Create Text"
        if kind == "ui.image":
            return "Create Image"
        if kind == "ui.button":
            return "Create Button"
        return "Create GameObject"

    def _serialize_created(self, obj, kind: str, *, selected: bool) -> dict[str, Any]:
        parent = obj.get_parent()
        return {
            "id": int(obj.id),
            "name": str(obj.name),
            "kind": kind,
            "parent_id": int(getattr(parent, "id", 0) or 0),
            "selected": bool(selected),
            "components": _component_names(obj),
        }


def _component_names(obj) -> list[str]:
    names: list[str] = []
    seen: set[tuple[str, int]] = set()

    def _append(comp) -> None:
        component_id = int(getattr(comp, "component_id", 0) or 0)
        key = (
            "component_id" if component_id else "object_id",
            component_id if component_id else id(comp),
        )
        if key in seen:
            return
        seen.add(key)
        names.append(str(getattr(comp, "type_name", type(comp).__name__)))

    for comp in obj.get_components() or []:
        _append(comp)
    for comp in obj.get_py_components() or []:
        _append(comp)
    return names


def _unique_scene_object_name(scene, base_name: str, *, exclude_id: int = 0) -> str:
    """Return a Unity-style default name that does not collide in the scene."""
    base = str(base_name or "GameObject")
    existing: set[str] = set()
    for obj in scene.get_all_objects() or []:
        if int(getattr(obj, "id", 0) or 0) == int(exclude_id or 0):
            continue
        existing.add(str(getattr(obj, "name", "")))

    if base not in existing:
        return base

    suffix = 1
    while f"{base} ({suffix})" in existing:
        suffix += 1
    return f"{base} ({suffix})"


def _get_py_components_safe(obj) -> list[Any]:
    if obj is None or not hasattr(obj, "get_py_components"):
        return []
    return list(obj.get_py_components() or [])


def _ancestor_parent_ids(obj) -> list[int]:
    """Immediate parent first, then ancestors, ending with 0 (scene root)."""
    ids: list[int] = []
    parent = obj.get_parent() if obj is not None else None
    while parent is not None:
        ids.append(int(parent.id))
        parent = parent.get_parent()
    ids.append(0)
    return ids


def _deepest_common_parent(objects: list[Any]):
    if not objects:
        return None
    candidates = _ancestor_parent_ids(objects[0])
    for obj in objects[1:]:
        allowed = set(_ancestor_parent_ids(obj))
        candidates = [cid for cid in candidates if cid in allowed]
    if not candidates:
        return None
    common_id = int(candidates[0])
    if common_id == 0:
        return None
    from Infernux.lib import SceneManager
    scene = SceneManager.instance().get_active_scene()
    return scene.find_by_id(common_id) if scene else None


def _min_sibling_index_under_parent(objects: list[Any], parent) -> int:
    parent_id = int(parent.id) if parent is not None else 0
    indices: list[int] = []
    for obj in objects:
        current = obj.get_parent() if obj is not None else None
        current_id = int(current.id) if current is not None else 0
        if current_id != parent_id:
            continue
        transform = getattr(obj, "transform", None)
        if transform is not None:
            indices.append(int(transform.get_sibling_index()))
    return min(indices) if indices else 0


def _average_world_position(objects: list[Any]) -> tuple[float, float, float] | None:
    sx = sy = sz = 0.0
    count = 0
    for obj in objects:
        transform = getattr(obj, "transform", None)
        if transform is None:
            continue
        pos = transform.position
        sx += float(pos.x)
        sy += float(pos.y)
        sz += float(pos.z)
        count += 1
    if count <= 0:
        return None
    return (sx / count, sy / count, sz / count)
