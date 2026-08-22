"""Semantic UI creation MCP tools."""

from __future__ import annotations

from typing import Any

from Infernux.mcp.tools.common import main_thread, register_tool_metadata, serialize_value


def register_ui_tools(mcp) -> None:
    _register_metadata()

    @mcp.tool(name="ui_create_canvas")
    def ui_create_canvas(name: str = "Canvas", reference_width: int = 1920, reference_height: int = 1080, select: bool = False) -> dict:
        """Create a UI Canvas."""

        def _create():
            obj, comp = _create_ui_object(
                "canvas",
                name,
                0,
                initial_values={
                    "reference_width": int(reference_width),
                    "reference_height": int(reference_height),
                },
                select=bool(select),
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_create_canvas", _create)

    @mcp.tool(name="ui_create_text")
    def ui_create_text(name: str = "Text", parent_id: int = 0, text: str = "New Text", rect: dict[str, Any] | None = None) -> dict:
        """Create a UIText element."""

        def _create():
            obj, comp = _create_ui_object(
                "text",
                name,
                int(parent_id or 0),
                initial_values={"text": str(text), **_rect_values(rect or {})},
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_create_text", _create)

    @mcp.tool(name="ui_create_button")
    def ui_create_button(name: str = "Button", parent_id: int = 0, label: str = "Button", rect: dict[str, Any] | None = None) -> dict:
        """Create a UIButton element."""

        def _create():
            obj, comp = _create_ui_object(
                "button",
                name,
                int(parent_id or 0),
                initial_values={"label": str(label), **_rect_values(rect or {})},
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_create_button", _create)

    @mcp.tool(name="ui_create_image")
    def ui_create_image(name: str = "Image", parent_id: int = 0, texture_path: str = "", rect: dict[str, Any] | None = None) -> dict:
        """Create a UIImage element."""

        def _create():
            obj, comp = _create_ui_object(
                "image",
                name,
                int(parent_id or 0),
                initial_values={
                    "texture_path": str(texture_path or ""),
                    **_rect_values(rect or {}),
                },
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_create_image", _create)

    @mcp.tool(name="ui_create_panel")
    def ui_create_panel(name: str = "Panel", parent_id: int = 0, color: list | None = None, rect: dict[str, Any] | None = None) -> dict:
        """Create a solid-color panel using UIImage."""

        def _create():
            obj, comp = _create_ui_object(
                "image",
                name,
                int(parent_id or 0),
                initial_values={
                    "texture_path": "",
                    "color": color or [0.1, 0.1, 0.1, 0.85],
                    **_rect_values(rect or {}),
                },
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_create_panel", _create)

    @mcp.tool(name="ui_set_rect")
    def ui_set_rect(object_id: int, rect: dict[str, Any]) -> dict:
        """Set x/y/width/height/rotation on a UI screen component."""

        def _set_rect():
            obj = _find_game_object(object_id)
            comp = _find_ui_screen_component(obj)
            if comp is None:
                raise FileNotFoundError(f"GameObject {object_id} has no screen UI component.")
            _commit_python_component_fields(
                comp,
                _rect_values(rect or {}),
                "Set UI rectangle",
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_set_rect", _set_rect)

    @mcp.tool(name="ui_set_text")
    def ui_set_text(object_id: int, text: str) -> dict:
        """Set text/label on UIText or UIButton."""

        def _set_text():
            obj = _find_game_object(object_id)
            comp = _find_named_component(obj, {"UIText", "UIButton"})
            if comp is None:
                raise FileNotFoundError(f"GameObject {object_id} has no UIText or UIButton.")
            field = "label" if type(comp).__name__ == "UIButton" else "text"
            _commit_python_component_fields(
                comp,
                {field: str(text)},
                f"Set {type(comp).__name__} {field}",
            )
            return _ui_snapshot(obj, comp)

        return main_thread("ui_set_text", _set_text)

    @mcp.tool(name="ui_inspect")
    def ui_inspect() -> dict:
        """Return a compact snapshot of UI canvases and elements."""

        def _inspect():
            from Infernux.lib import SceneManager
            scene_manager = SceneManager.instance()
            scene = scene_manager.get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            elements = []
            for runtime_scene in (
                scene,
                scene_manager.get_runtime_persistent_scene(),
            ):
                if runtime_scene is None:
                    continue
                for obj in runtime_scene.get_all_objects() or []:
                    comp = _find_ui_component(obj)
                    if comp is not None:
                        elements.append(_ui_snapshot(obj, comp))
            return {"elements": elements}

        return main_thread("ui_inspect", _inspect)

    @mcp.tool(name="ui_find_by_text")
    def ui_find_by_text(text: str) -> dict:
        """Find UIText/UIButton elements by visible text."""

        def _find():
            from Infernux.lib import SceneManager
            scene_manager = SceneManager.instance()
            scene = scene_manager.get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            needle = str(text).lower()
            matches = []
            for runtime_scene in (
                scene,
                scene_manager.get_runtime_persistent_scene(),
            ):
                if runtime_scene is None:
                    continue
                for obj in runtime_scene.get_all_objects() or []:
                    comp = _find_named_component(obj, {"UIText", "UIButton"})
                    if comp is None:
                        continue
                    visible = str(getattr(comp, "label", getattr(comp, "text", "")))
                    if needle in visible.lower():
                        matches.append(_ui_snapshot(obj, comp))
            return {"matches": matches}

        return main_thread("ui_find_by_text", _find)

    @mcp.tool(name="ui_bind_click")
    def ui_bind_click(
        button_id: int,
        target_id: int,
        component_name: str,
        method_name: str,
        replace: bool = True,
    ) -> dict:
        """Bind UIButton to GameObject -> attached component -> public method.

        The persistent event stores a scene-object reference. It never stores
        or invokes a script asset path directly.
        """

        def _bind():
            button_obj = _find_game_object(button_id)
            button = _find_named_component(button_obj, {"UIButton"})
            if button is None:
                raise FileNotFoundError(f"GameObject {button_id} has no UIButton component.")

            target_obj = _find_game_object(target_id)
            target_component = _find_named_component(target_obj, {str(component_name)})
            if target_component is None:
                raise FileNotFoundError(
                    f"Component '{component_name}' was not found on GameObject {target_id}."
                )

            from Infernux.ui.ui_event_entry import (
                UIEventEntry,
                get_callable_methods,
                get_method_parameter_specs,
                normalize_event_arguments,
            )

            method = str(method_name or "").strip()
            if method not in get_callable_methods(target_component):
                raise ValueError(
                    f"Method '{method}' is not a public persistent-event method on "
                    f"component '{component_name}'."
                )

            from Infernux.components import GameObjectRef

            entry = UIEventEntry(
                target=GameObjectRef(target_obj),
                component_name=type(target_component).__name__,
                method_name=method,
                arguments=normalize_event_arguments(
                    [], get_method_parameter_specs(target_component, method)
                ),
            )
            old_entries = list(button.on_click_entries or [])
            new_entries = [entry] if replace else [*old_entries, entry]
            _commit_python_component_fields(
                button,
                {"on_click_entries": new_entries},
                "Bind UIButton on_click",
            )
            return {
                "button": _ui_snapshot(button_obj, button),
                "binding": _event_entry_snapshot(entry),
                "binding_count": len(new_entries),
            }

        return main_thread(
            "ui_bind_click",
            _bind,
            arguments={
                "button_id": button_id,
                "target_id": target_id,
                "component_name": component_name,
                "method_name": method_name,
                "replace": replace,
            },
        )


def _create_ui_object(
    kind: str,
    name: str,
    parent_id: int,
    *,
    initial_values: dict[str, Any] | None = None,
    select: bool = False,
):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.lib import SceneManager

    scene = SceneManager.instance().get_active_scene()
    if not scene:
        raise RuntimeError("No active scene.")

    values = dict(initial_values or {})

    def _configure(obj) -> None:
        comp = _find_ui_component(obj)
        if comp is None:
            raise RuntimeError(f"Created ui.{kind} has no UI component.")
        for field, value in values.items():
            setattr(comp, field, value)
        _publish_ui_component(comp)

    creation = HierarchyCreationService.instance()
    with _scene_object_service().user_action(f"Create UI {kind.title()}"):
        if kind != "canvas" and not parent_id:
            canvas = _find_first_canvas(scene)
            if canvas is None:
                canvas_entry = creation.create(
                    "ui.canvas",
                    name="Canvas",
                    select=False,
                    selection_owner_id="automation",
                    selection_reason="mcp_ui_create_canvas",
                )
                parent_id = int(canvas_entry["id"])
            else:
                parent_id = int(canvas.id)
        created = creation.create(
            f"ui.{kind}",
            parent_id=int(parent_id or 0),
            name=str(name or kind.title()),
            select=bool(select),
            selection_owner_id="automation",
            selection_reason="mcp_ui_create",
            configure_created=_configure,
        )
    obj = scene.find_by_id(int(created["id"]))
    if obj is None:
        raise RuntimeError(f"Created ui.{kind} could not be resolved.")
    comp = _find_ui_component(obj)
    if comp is None:
        raise RuntimeError(f"Created ui.{kind} has no UI component.")
    return obj, comp


def _find_first_canvas(scene):
    try:
        for obj in scene.get_all_objects() or []:
            if _find_named_component(obj, {"UICanvas"}) is not None:
                return obj
    except Exception:
        pass
    return None


def _rect_values(rect: dict[str, Any]) -> dict[str, float]:
    return {
        key: float(rect[key])
        for key in (
            "x",
            "y",
            "width",
            "height",
            "rotation",
            "opacity",
            "corner_radius",
        )
        if key in rect
    }


def _commit_python_component_fields(
    comp,
    values: dict[str, Any],
    description: str,
) -> None:
    from Infernux.engine.interaction import (
        PropertyTransactionStatus,
        make_python_component_property_transaction,
    )

    applied = False
    with _scene_object_service().user_action(description):
        for field, value in values.items():
            status = make_python_component_property_transaction(
                (comp,),
                field,
                description=str(description),
            ).commit_or_raise(value)
            applied = applied or status is PropertyTransactionStatus.APPLIED
    if applied:
        _invalidate_ui_cache()


def _publish_ui_component(comp) -> None:
    callback = getattr(comp, "_call_on_validate", None)
    if callable(callback):
        callback()
    _invalidate_ui_cache()


def _invalidate_ui_cache() -> None:
    from Infernux.ui.ui_canvas_utils import invalidate_canvas_cache

    invalidate_canvas_cache()


def _scene_object_service():
    from Infernux.engine.interaction import EditorInteractionCore

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("EditorInteractionCore is unavailable.")
    return core.scene_objects


def _ui_snapshot(obj, comp) -> dict[str, Any]:
    data = {
        "object_id": int(obj.id),
        "name": str(obj.name),
        "type": type(comp).__name__,
        "parent_id": int(getattr(obj.get_parent(), "id", 0) or 0),
        "fields": {},
    }
    for key in ("text", "label", "x", "y", "width", "height", "rotation", "opacity", "corner_radius", "reference_width", "reference_height", "texture_path", "color"):
        if hasattr(comp, key):
            data["fields"][key] = serialize_value(getattr(comp, key))
    return data


def _event_entry_snapshot(entry) -> dict[str, Any]:
    from Infernux.ui.ui_event_entry import _get_serializable_raw_field

    target = _get_serializable_raw_field(entry, "target")
    return {
        "target_id": int(getattr(target, "persistent_id", 0) or 0),
        "component_name": str(getattr(entry, "component_name", "") or ""),
        "method_name": str(getattr(entry, "method_name", "") or ""),
        "argument_count": len(getattr(entry, "arguments", None) or []),
    }


def _find_game_object(object_id: int):
    from Infernux.mcp.tools.common import find_game_object
    return find_game_object(object_id)


def _find_named_component(obj, names: set[str]):
    try:
        for comp in obj.get_py_components() or []:
            if type(comp).__name__ in names:
                return comp
    except Exception:
        pass
    return None


def _find_ui_component(obj):
    return _find_named_component(obj, {"UICanvas", "UIText", "UIButton", "UIImage"})


def _find_ui_screen_component(obj):
    try:
        from Infernux.ui.inx_ui_screen_component import InxUIScreenComponent
        for comp in obj.get_py_components() or []:
            if isinstance(comp, InxUIScreenComponent):
                return comp
    except Exception:
        pass
    return None


def _register_metadata() -> None:
    for name, summary in {
        "ui_create_canvas": "Create a UICanvas root.",
        "ui_create_text": "Create a UIText element.",
        "ui_create_button": "Create a UIButton element.",
        "ui_create_panel": "Create a solid-color panel.",
        "ui_create_image": "Create a UIImage element.",
        "ui_set_rect": "Set UI element rectangle.",
        "ui_set_text": "Set text/label on a UI element.",
        "ui_inspect": "Inspect UI elements.",
        "ui_find_by_text": "Find UI elements by visible text.",
        "ui_bind_click": "Bind a persistent UIButton click event.",
    }.items():
        register_tool_metadata(name, summary=summary)
    register_tool_metadata(
        "ui_bind_click",
        summary="Bind a persistent UIButton click through an attached component.",
        concepts={
            "Persistent Click": (
                "A binding is GameObject -> component attached to that GameObject "
                "-> public method, matching the Unity Inspector interaction model."
            ),
            "Script Asset": (
                "Script files are component definitions, not click targets; this tool "
                "does not accept or persist a script path."
            ),
        },
        invariants=[
            "The target component must already be attached to target_id.",
            "The selected method must be public and excluded from lifecycle callbacks.",
            "No script asset path is stored in on_click_entries.",
        ],
        side_effects=["Updates UIButton.on_click_entries and marks the active scene dirty."],
    )
