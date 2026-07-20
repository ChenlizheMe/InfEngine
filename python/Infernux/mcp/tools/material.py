"""Material MCP tools."""

from __future__ import annotations

import os
from typing import Any

from Infernux.engine.path_utils import relative_path
from Infernux.mcp.tools.common import (
    main_thread,
    notify_asset_changed,
    register_tool_metadata,
    require_knowledge_token,
    resolve_project_path,
    serialize_value,
)


def register_material_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="material_create")
    def material_create(
        path: str,
        template: str = "lit",
        overwrite: bool = False,
        properties: dict[str, Any] | None = None,
        knowledge_token: str = "",
    ) -> dict:
        """Create a material asset and optionally set properties."""

        def _create():
            require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            from Infernux.core.material import Material
            file_path = resolve_project_path(project_path, path)
            if os.path.exists(file_path) and not overwrite:
                raise FileExistsError(f"Material already exists: {path}")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            name = os.path.splitext(os.path.basename(file_path))[0]
            mat = Material.create_unlit(name) if str(template).lower() == "unlit" else Material.create_lit(name)
            _set_properties(mat, properties or {})
            mat.save(file_path)
            notify_asset_changed(file_path, "created")
            return {"path": relative_path(file_path, project_path), **_material_info(mat)}

        return main_thread("material_create", _create, arguments={"path": path, "template": template, "overwrite": overwrite, "knowledge_token": knowledge_token})

    @mcp.tool(name="material_get_properties")
    def material_get_properties(path: str) -> dict:
        """Read material properties."""

        def _get():
            mat = _load_material(project_path, path)
            return {"path": relative_path(resolve_project_path(project_path, path), project_path), **_material_info(mat)}

        return main_thread("material_get_properties", _get)

    @mcp.tool(name="material_set_property")
    def material_set_property(path: str, name: str, value: Any, value_type: str = "auto", knowledge_token: str = "") -> dict:
        """Set one material property."""

        def _set():
            require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            file_path = resolve_project_path(project_path, path)
            mat = _load_material(project_path, path)
            _set_one(mat, name, value, value_type)
            mat.flush()
            mat.save(file_path)
            notify_asset_changed(file_path, "modified")
            return {"path": path, "name": name, "value": serialize_value(mat.get_property(name)), **_material_info(mat)}

        return main_thread("material_set_property", _set, arguments={"path": path, "name": name, "value_type": value_type, "knowledge_token": knowledge_token})

    @mcp.tool(name="material_set_shader")
    def material_set_shader(
        path: str,
        vertex: str = "",
        fragment: str = "",
        knowledge_token: str = "",
    ) -> dict:
        """Select validated vertex and/or fragment shader IDs for a material."""

        def _set():
            require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            file_path = resolve_project_path(project_path, path)
            mat = _load_material(project_path, path)
            if vertex:
                _require_shader_stage(vertex, "vertex")
                mat.vert_shader_name = str(vertex).strip()
            if fragment:
                _require_shader_stage(fragment, "fragment")
                mat.frag_shader_name = str(fragment).strip()
            if not vertex and not fragment:
                raise ValueError("At least one of vertex or fragment must be provided.")
            mat.flush()
            mat.save(file_path)
            notify_asset_changed(file_path, "modified")
            return {"path": path, **_material_info(mat)}

        return main_thread(
            "material_set_shader",
            _set,
            arguments={
                "path": path,
                "vertex": vertex,
                "fragment": fragment,
                "knowledge_token": knowledge_token,
            },
        )


def _load_material(project_path: str, path: str):
    from Infernux.core.material import Material
    file_path = resolve_project_path(project_path, path)
    mat = Material.load(file_path)
    if mat is None:
        raise FileNotFoundError(f"Material not found or failed to load: {path}")
    return mat


def _set_properties(mat, properties: dict[str, Any]) -> None:
    for name, value in properties.items():
        _set_one(mat, name, value, "auto")


def _set_one(mat, name: str, value: Any, value_type: str) -> None:
    kind = str(value_type or "auto").lower()
    if kind == "float" or (kind == "auto" and isinstance(value, float)):
        mat.set_float(name, float(value))
    elif kind == "int" or (kind == "auto" and isinstance(value, int) and not isinstance(value, bool)):
        mat.set_int(name, int(value))
    elif kind == "color" or (kind == "auto" and isinstance(value, (list, tuple)) and len(value) == 4):
        mat.set_color(name, float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    elif kind == "vector2" or (kind == "auto" and isinstance(value, (list, tuple)) and len(value) == 2):
        mat.set_vector2(name, float(value[0]), float(value[1]))
    elif kind == "vector3" or (kind == "auto" and isinstance(value, (list, tuple)) and len(value) == 3):
        mat.set_vector3(name, float(value[0]), float(value[1]), float(value[2]))
    elif kind == "texture":
        mat.set_texture(name, value)
    else:
        mat.set_param(name, value)


def _require_shader_stage(shader_id: str, expected_kind: str) -> None:
    from Infernux.mcp.tools.api import _scan_shaders

    normalized = str(shader_id or "").strip().lower()
    matches = [
        item
        for item in _scan_shaders()
        if str(item.get("shader_id") or "").lower() == normalized
    ]
    if any(item.get("kind") == expected_kind for item in matches):
        return
    actual = sorted({str(item.get("kind") or "") for item in matches if item.get("kind")})
    if actual:
        raise ValueError(
            f"Shader '{shader_id}' is not a {expected_kind} shader; available kind(s): {', '.join(actual)}."
        )
    raise FileNotFoundError(f"Shader '{shader_id}' was not found in the imported shader catalog.")


def _properties(mat) -> dict[str, Any]:
    try:
        return serialize_value(mat.get_all_properties())
    except Exception:
        return {}


def _material_info(mat) -> dict[str, Any]:
    return {
        "name": str(getattr(mat, "name", "")),
        "shader": {
            "shader_name": str(getattr(mat, "shader_name", "") or ""),
            "vertex": str(getattr(mat, "vert_shader_name", "") or ""),
            "fragment": str(getattr(mat, "frag_shader_name", "") or ""),
        },
        "render_queue": int(getattr(mat, "render_queue", 0) or 0),
        "properties": _properties(mat),
    }


def _register_metadata() -> None:
    for name, summary in {
        "material_create": "Create a material asset.",
        "material_get_properties": "Read material shader selection and properties.",
        "material_set_property": "Set a material shader property.",
        "material_set_shader": "Select validated vertex and fragment shader IDs for a material.",
    }.items():
        register_tool_metadata(
            name,
            summary=summary,
            category="assets/materials",
            tags=["material", "shader", "properties"],
            aliases=["shader selection", "fragment shader", "vertex shader", "材质", "着色器属性"],
            preconditions=["Requires a valid shader knowledge_token from shader_guide or api_get('shader')."],
            recovery=["Call shader_guide, read the guide, then retry with data.knowledge_lock.token as knowledge_token."],
            next_suggested_tools=["shader_describe", "shader_catalog", "api_get"],
        )
