"""RenderStack MCP tools for pipeline and post-processing control."""

from __future__ import annotations

import os
from typing import Any

from Infernux.engine.path_utils import relative_path
from Infernux.mcp.tools.common import (
    get_asset_database,
    main_thread,
    register_tool_metadata,
    resolve_asset_path,
    serialize_value,
)


def register_renderstack_tools(mcp, project_path: str = "") -> None:
    _register_metadata()

    @mcp.tool(name="render_effect_create")
    def render_effect_create(path: str, feature_type: str) -> dict:
        """Create and import a strict reusable RenderEffect asset."""

        def _create_effect():
            from Infernux.engine.ui.project_file_ops import create_render_effect

            file_path = resolve_asset_path(project_path, path)
            if not file_path.lower().endswith(".effect"):
                file_path += ".effect"
            directory = os.path.dirname(file_path)
            os.makedirs(directory, exist_ok=True)
            name = os.path.splitext(os.path.basename(file_path))[0]
            created, error = create_render_effect(
                directory,
                name,
                str(feature_type),
                get_asset_database(),
            )
            if not created:
                if os.path.exists(file_path):
                    raise FileExistsError(error or f"Render Effect already exists: {path}")
                raise RuntimeError(error or f"Failed to create Render Effect: {path}")
            return {
                "path": relative_path(file_path, project_path),
                "feature_type": str(feature_type),
                "created": True,
            }

        return main_thread(
            "render_effect_create",
            _create_effect,
            arguments={"path": path, "feature_type": feature_type},
        )

    @mcp.tool(name="renderstack_find_or_create")
    def renderstack_find_or_create(name: str = "RenderStack", create_if_missing: bool = True) -> dict:
        """Find the active RenderStack or create one."""

        def _find_or_create():
            stack = _find_stack()
            created = False
            if stack is None and create_if_missing:
                from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
                entry = HierarchyCreationService.instance().create("rendering.render_stack", name=name, select=False)
                obj = _find_game_object(int(entry["id"]))
                stack = _find_stack_on_object(obj)
                created = True
            if stack is None:
                raise FileNotFoundError("No RenderStack found and create_if_missing is false.")
            return {"created": created, "stack": _stack_snapshot(stack)}

        return main_thread("renderstack_find_or_create", _find_or_create, arguments={"name": name, "create_if_missing": create_if_missing})

    @mcp.tool(name="renderstack_inspect")
    def renderstack_inspect() -> dict:
        """Inspect the active RenderStack pipeline and ordered EffectStage slots."""

        def _inspect():
            stack = _find_stack()
            if stack is None:
                return {
                    "exists": False,
                    "reason": "no_renderstack_in_active_scene",
                    "message": "No RenderStack exists in the active scene. Do not repeat renderstack_inspect; call renderstack_find_or_create if rendering stack control is needed.",
                    "next_suggested_tools": [
                        {
                            "tool": "renderstack_find_or_create",
                            "arguments": {"name": "RenderStack", "create_if_missing": True},
                        },
                        {
                            "tool": "hierarchy_create_object",
                            "arguments": {"kind": "rendering.render_stack", "name": "RenderStack"},
                        },
                    ],
                }
            return _stack_snapshot(stack, include_catalog=True)

        return main_thread("renderstack_inspect", _inspect)

    @mcp.tool(name="renderstack_list_pipelines")
    def renderstack_list_pipelines() -> dict:
        """List discovered RenderPipeline classes."""

        def _list():
            from Infernux.renderstack.discovery import discover_pipelines
            return {"pipelines": [_pipeline_entry(name, cls) for name, cls in sorted(discover_pipelines().items())]}

        return main_thread("renderstack_list_pipelines", _list)

    @mcp.tool(name="renderstack_set_pipeline")
    def renderstack_set_pipeline(pipeline: str = "") -> dict:
        """Set the active RenderStack pipeline. Empty string means default forward pipeline."""

        def _set():
            stack = _require_stack()
            stack.set_pipeline(str(pipeline or ""))
            _mark_scene_dirty()
            return _stack_snapshot(stack)

        return main_thread("renderstack_set_pipeline", _set, arguments={"pipeline": pipeline})

    @mcp.tool(name="renderstack_list_effect_stages")
    def renderstack_list_effect_stages() -> dict:
        """List pipeline EffectStages and their ordered asset slots."""

        def _list_effect_stages():
            return {
                "stages": _effect_stages(_require_stack()),
            }

        return main_thread("renderstack_list_effect_stages", _list_effect_stages)

    @mcp.tool(name="renderstack_add_effect")
    def renderstack_add_effect(stage_id: str, asset_path: str, enabled: bool = True) -> dict:
        """Mount a .effect or .effectgroup asset in one pipeline EffectStage."""

        def _add_effect():
            stack = _require_stack()
            reference = _render_effect_reference(asset_path)
            stack.add_effect_slot(str(stage_id), reference, enabled=bool(enabled))
            _mark_scene_dirty()
            return _stack_snapshot(stack)

        return main_thread(
            "renderstack_add_effect",
            _add_effect,
            arguments={"stage_id": stage_id, "asset_path": asset_path, "enabled": enabled},
        )

    @mcp.tool(name="renderstack_remove_effect")
    def renderstack_remove_effect(stage_id: str, index: int = 0) -> dict:
        """Remove one ordered EffectStage slot."""

        def _remove_effect():
            stack = _require_stack()
            slots = list(stack.get_effect_stage_slots(str(stage_id)))
            slot_index = int(index)
            if slot_index < 0 or slot_index >= len(slots):
                raise IndexError(
                    f"EffectStage '{stage_id}' has {len(slots)} slots; index {slot_index} is invalid."
                )
            slots.pop(slot_index)
            stack.set_effect_stage_slots(str(stage_id), slots)
            _mark_scene_dirty()
            return _stack_snapshot(stack)

        return main_thread(
            "renderstack_remove_effect",
            _remove_effect,
            arguments={"stage_id": stage_id, "index": index},
        )

    @mcp.tool(name="renderstack_set_effect_enabled")
    def renderstack_set_effect_enabled(stage_id: str, index: int, enabled: bool) -> dict:
        """Enable or disable one ordered EffectStage slot."""

        def _set_effect_enabled():
            stack = _require_stack()
            slots = list(stack.get_effect_stage_slots(str(stage_id)))
            slot_index = int(index)
            if slot_index < 0 or slot_index >= len(slots):
                raise IndexError(
                    f"EffectStage '{stage_id}' has {len(slots)} slots; index {slot_index} is invalid."
                )
            slots[slot_index].enabled = bool(enabled)
            stack.set_effect_stage_slots(str(stage_id), slots)
            _mark_scene_dirty()
            return _stack_snapshot(stack)

        return main_thread(
            "renderstack_set_effect_enabled",
            _set_effect_enabled,
            arguments={"stage_id": stage_id, "index": index, "enabled": enabled},
        )

def _find_stack():
    try:
        from Infernux.renderstack import RenderStack
        stack = RenderStack.instance()
        if stack is not None:
            return stack
    except Exception:
        pass
    try:
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        if not scene:
            return None
        for obj in scene.get_all_objects() or []:
            stack = _find_stack_on_object(obj)
            if stack is not None:
                return stack
    except Exception:
        return None
    return None


def _require_stack():
    stack = _find_stack()
    if stack is None:
        raise FileNotFoundError("No RenderStack exists in the active scene. Use renderstack.find_or_create.")
    return stack


def _find_stack_on_object(obj):
    try:
        from Infernux.renderstack import RenderStack
        for comp in obj.get_py_components() or []:
            if isinstance(comp, RenderStack):
                return comp
    except Exception:
        return None
    return None


def _find_game_object(object_id: int):
    from Infernux.mcp.tools.common import find_game_object
    return find_game_object(object_id)


def _stack_snapshot(stack, *, include_catalog: bool = False) -> dict[str, Any]:
    go = stack.game_object
    data = {
        "object_id": int(go.id),
        "object_name": str(go.name),
        "pipeline": str(getattr(stack, "pipeline_class_name", "") or ""),
        "active": bool(stack is type(stack).instance()),
        "enabled": bool(getattr(stack, "enabled", True)),
        "effect_stages": _effect_stages(stack),
        "effect_compile_errors": list(getattr(stack, "effect_compile_errors", ()) or ()),
        "build_failed": bool(getattr(stack, "_build_failed", False)),
    }
    if include_catalog:
        data["available_pipelines"] = _available_pipelines()
    return data


def _effect_stages(stack) -> list[dict[str, Any]]:
    stages = []
    for stage in list(getattr(stack, "effect_stages", ()) or ()):
        stage_id = str(getattr(stage, "stable_id", "") or "")
        slots = []
        for index, slot in enumerate(stack.get_effect_stage_slots(stage_id)):
            reference = getattr(slot, "effect_ref", None)
            slots.append({
                "index": index,
                "slot_id": str(getattr(slot, "slot_id", "") or ""),
                "enabled": bool(getattr(slot, "enabled", True)),
                "asset": {
                    "guid": str(getattr(reference, "guid", "") or ""),
                    "path_hint": str(getattr(reference, "path_hint", "") or ""),
                },
            })
        stages.append({
            "stable_id": stage_id,
            "display_name": str(getattr(stage, "display_name", "") or stage_id),
            "scope": str(getattr(getattr(stage, "scope", ""), "value", getattr(stage, "scope", ""))),
            "slots": slots,
        })
    return stages


def _render_effect_reference(asset_path: str):
    import os

    from Infernux.core.asset_ref import RenderEffectRef
    from Infernux.engine.path_utils import relative_path
    from Infernux.mcp import capabilities
    from Infernux.mcp.tools.common import get_asset_database, resolve_asset_path

    project_root = capabilities.project_path()
    resolved = resolve_asset_path(project_root, asset_path)
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"RenderEffect asset was not found: {asset_path}")
    extension = os.path.splitext(resolved)[1].lower()
    if extension not in {".effect", ".effectgroup"}:
        raise ValueError("RenderStack EffectStage assets must use .effect or .effectgroup")
    database = get_asset_database()
    guid = str(database.get_guid_from_path(resolved) or "") if database is not None else ""
    if not guid:
        raise FileNotFoundError(
            f"RenderEffect asset is not imported in the AssetDatabase: {asset_path}"
        )
    path_hint = relative_path(resolved, project_root, allow_root=True).replace("\\", "/")
    return RenderEffectRef(guid=guid, path_hint=path_hint)


def _available_pipelines() -> list[dict[str, Any]]:
    from Infernux.renderstack.discovery import discover_pipelines
    return [_pipeline_entry(name, cls) for name, cls in sorted(discover_pipelines().items())]


def _pipeline_entry(name: str, cls) -> dict[str, Any]:
    return {"name": str(name), "class_name": cls.__name__, "module": cls.__module__, "fields": _field_schema(cls)}


def _field_schema(cls) -> list[dict[str, Any]]:
    from Infernux.components.serialized_field import get_serialized_fields
    fields = []
    for name, meta in get_serialized_fields(cls).items():
        fields.append({
            "name": str(name),
            "type": getattr(getattr(meta, "field_type", None), "name", str(getattr(meta, "field_type", ""))),
            "default": serialize_value(getattr(meta, "default", None)),
            "range": serialize_value(getattr(meta, "range", None)),
            "tooltip": str(getattr(meta, "tooltip", "")),
            "readonly": bool(getattr(meta, "readonly", False)),
        })
    return fields


def _mark_scene_dirty() -> None:
    try:
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm:
            sfm.mark_dirty()
    except Exception:
        pass


def _register_metadata() -> None:
    for name, summary in {
        "renderstack_find_or_create": "Find or create the active scene RenderStack.",
        "render_effect_create": "Create and import a strict reusable RenderEffect asset.",
        "renderstack_inspect": "Inspect the active RenderStack pipeline and EffectStages.",
        "renderstack_list_pipelines": "List discovered RenderPipeline classes.",
        "renderstack_set_pipeline": "Switch the active RenderStack pipeline.",
        "renderstack_list_effect_stages": "List pipeline EffectStages and mounted Effect assets.",
        "renderstack_add_effect": "Mount a RenderEffect or EffectGroup asset in an EffectStage.",
        "renderstack_remove_effect": "Remove one mounted EffectStage asset slot.",
        "renderstack_set_effect_enabled": "Enable or disable one mounted EffectStage asset slot.",
    }.items():
        category = "renderstack/pipeline" if "pipeline" in name or name.endswith(("inspect", "find_or_create")) else "renderstack/effects"
        register_tool_metadata(
            name,
            summary=summary,
            category=category,
            tags=["renderstack", "rendering", "pipeline", "postprocess"],
            aliases=["render stack", "post processing", "effects"],
            recovery=[
                "If renderstack_inspect returns exists=false, do not repeat it.",
                "Call renderstack_find_or_create(name='RenderStack', create_if_missing=true) before editing the stack.",
            ],
            next_suggested_tools=["renderstack_find_or_create", "renderstack_inspect", "renderstack_list_effect_stages", "runtime_read_errors"],
        )
