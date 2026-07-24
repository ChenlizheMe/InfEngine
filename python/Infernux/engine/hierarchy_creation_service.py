"""Shared Hierarchy object creation service.

Both the C++ HierarchyPanel callbacks and MCP tools use this service so editor
UI creation and agent-driven creation stay behaviorally identical.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from Infernux.debug import Debug


PRIMITIVE_INDEX = {
    0: "primitive.cube",
    1: "primitive.sphere",
    2: "primitive.capsule",
    3: "primitive.cylinder",
    4: "primitive.plane",
    5: "primitive.quad",
}

LIGHT_INDEX = {
    0: "light.directional",
    1: "light.point",
    2: "light.spot",
}


class HierarchyCreationService:
    _instance: Optional["HierarchyCreationService"] = None
    _kind_registry: dict[str, dict[str, Any]] = {}
    _kind_factories: dict[str, Callable[[Any, int], Any]] = {}
    _defaults_registered: bool = False

    def __init__(self) -> None:
        self._selection_manager = None
        self._undo_tracker = None
        self._hierarchy_panel = None
        self._ensure_default_kinds()

    @classmethod
    def instance(cls) -> "HierarchyCreationService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def configure(self, *, selection_manager=None, undo_tracker=None, hierarchy_panel=None) -> None:
        self._selection_manager = selection_manager
        self._undo_tracker = undo_tracker
        self._hierarchy_panel = hierarchy_panel

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

    def create(
        self,
        kind: str,
        *,
        parent_id: int = 0,
        name: str | None = None,
        select: bool = True,
        record_undo: bool = True,
    ) -> dict[str, Any]:
        from Infernux.lib import SceneManager

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

        obj = self._create_raw(scene, kind, parent_id)
        if obj is None:
            raise RuntimeError(f"Failed to create hierarchy object kind '{kind}'.")

        if name:
            obj.name = str(name)
        else:
            obj.name = _unique_scene_object_name(scene, str(obj.name), exclude_id=int(getattr(obj, "id", 0) or 0))

        self._finalize(obj, effective_parent_id, self._description_for(kind), select=select, record_undo=record_undo)
        return self._serialize_created(obj, kind, selected=select)

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
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Text")
        if obj:
            obj.add_py_component(UITextCls())
            invalidate_canvas_cache()
        return obj

    def _create_ui_image(self, scene, parent_id: int):
        from Infernux.ui import UIImage as UIImageCls
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Image")
        if obj:
            image = UIImageCls()
            image.width = 100.0
            image.height = 100.0
            obj.add_py_component(image)
            invalidate_canvas_cache()
        return obj

    def _create_ui_button(self, scene, parent_id: int):
        from Infernux.ui import UIButton as UIButtonCls
        from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache
        obj = scene.create_game_object("Button")
        if obj:
            button = UIButtonCls()
            button.width = 160.0
            button.height = 40.0
            obj.add_py_component(button)
            invalidate_canvas_cache()
        return obj

    def _find_canvas_parent_id(self, scene, parent_id: int) -> int:
        from Infernux.ui import UICanvas

        candidate_ids = []
        if parent_id:
            candidate_ids.append(int(parent_id))
        selection = self._selection_manager
        if selection is not None and hasattr(selection, "get_primary"):
            selected_id = int(selection.get_primary() or 0)
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

    def _finalize(self, obj, parent_id: int, description: str, *, select: bool, record_undo: bool) -> None:
        if parent_id:
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            parent = scene.find_by_id(parent_id) if scene else None
            if parent:
                obj.set_parent(parent)

        if select and self._selection_manager:
            self._selection_manager.select(obj.id)

        if record_undo and self._undo_tracker:
            self._undo_tracker.record_create(obj.id, description)

        hp = self._hierarchy_panel
        if select and hp is not None:
            ensure_visible = getattr(hp, "set_selected_object_by_id", None)
            if callable(ensure_visible):
                ensure_visible(obj.id)
            # The native panel skips its selection notification when the
            # selection manager already holds the new id (we selected it
            # right above), so the Inspector would never hear about the new
            # object. Always fire the Python-side selection callback — it is
            # idempotent and syncs Inspector, outline and event bus.
            callback = getattr(hp, "on_selection_changed", None)
            if callable(callback):
                callback(obj.id)

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
        parent = None
        try:
            parent = obj.get_parent()
        except Exception as exc:
            Debug.log_suppressed("HierarchyCreationService.serialize.parent", exc)
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
    try:
        for comp in obj.get_components() or []:
            names.append(str(getattr(comp, "type_name", type(comp).__name__)))
    except Exception as exc:
        Debug.log_suppressed("HierarchyCreationService.components.native", exc)
    try:
        for comp in obj.get_py_components() or []:
            names.append(str(getattr(comp, "type_name", type(comp).__name__)))
    except Exception as exc:
        Debug.log_suppressed("HierarchyCreationService.components.py", exc)
    return names


def _unique_scene_object_name(scene, base_name: str, *, exclude_id: int = 0) -> str:
    """Return a Unity-style default name that does not collide in the scene."""
    base = str(base_name or "GameObject")
    existing: set[str] = set()
    try:
        for obj in scene.get_all_objects() or []:
            if int(getattr(obj, "id", 0) or 0) == int(exclude_id or 0):
                continue
            existing.add(str(getattr(obj, "name", "")))
    except Exception as exc:
        Debug.log_suppressed("HierarchyCreationService.unique_name", exc)
        return base

    if base not in existing:
        return base

    suffix = 1
    while f"{base} ({suffix})" in existing:
        suffix += 1
    return f"{base} ({suffix})"


def _get_py_components_safe(obj) -> list[Any]:
    if obj is None or not hasattr(obj, "get_py_components"):
        return []
    try:
        return list(obj.get_py_components() or [])
    except Exception as exc:
        Debug.log_suppressed("HierarchyCreationService.get_py_components", exc)
        return []
