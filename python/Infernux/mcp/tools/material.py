"""Material MCP tools."""

from __future__ import annotations

import copy
import os
from typing import Any

from Infernux.engine.path_utils import relative_path, same_path
from Infernux.mcp.tools.common import (
    get_asset_database,
    main_thread,
    notify_asset_changed,
    register_tool_metadata,
    require_knowledge_token,
    resolve_project_path,
    serialize_value,
)


_RESERVED_RENDER_STATE_PROPERTIES = frozenset(
    {
        "alpha_clip_enabled",
        "alpha_clip_threshold",
        "blend_enable",
        "color_blend_op",
        "cull_mode",
        "depth_compare_op",
        "depth_test_enable",
        "depth_write_enable",
        "dst_alpha_blend_factor",
        "dst_color_blend_factor",
        "render_queue",
        "src_alpha_blend_factor",
        "src_color_blend_factor",
        "stencil_enable",
        "surface_type",
    }
)

_BUILTIN_MATERIAL_URI_PREFIX = "builtin://"


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
            from Infernux.engine.interaction import ActionOrigin, ProjectAssetCommandService

            _require_writable_material_path(path)
            file_path = resolve_project_path(project_path, path)
            if os.path.exists(file_path) and not overwrite:
                raise FileExistsError(f"Material already exists: {path}")
            parent = os.path.dirname(file_path)
            if not os.path.isdir(parent):
                raise FileNotFoundError(f"Material directory does not exist: {relative_path(parent, project_path)}")
            name = os.path.splitext(os.path.basename(file_path))[0]
            mat = Material.create_unlit(name) if str(template).lower() == "unlit" else Material.create_lit(name)
            mat.native.is_builtin = False
            _set_properties(mat, properties or {})

            def _write_material():
                if not mat.save(file_path):
                    return False, f"Failed to save material: {path}"
                notify_asset_changed(file_path, "created")
                return True, ""

            service = ProjectAssetCommandService.instance()
            if service is None:
                raise RuntimeError("Editor Project asset command service is unavailable")
            if not service.configured:
                service.configure(project_path, get_asset_database())
            service.create(
                parent,
                _write_material,
                description="Create Material",
                origin=ActionOrigin.AUTOMATION,
                replace_path=file_path if overwrite and os.path.exists(file_path) else "",
            )
            return {"path": relative_path(file_path, project_path), **_material_info(mat)}

        return main_thread("material_create", _create, arguments={"path": path, "template": template, "overwrite": overwrite, "knowledge_token": knowledge_token})

    @mcp.tool(name="material_get_properties")
    def material_get_properties(path: str) -> dict:
        """Read material properties."""

        def _get():
            builtin_key = _builtin_material_key(path)
            mat = _load_material(project_path, path, allow_builtin=True)
            material_path = (
                f"{_BUILTIN_MATERIAL_URI_PREFIX}{builtin_key}"
                if builtin_key
                else relative_path(resolve_project_path(project_path, path), project_path)
            )
            return {"path": material_path, **_material_info(mat)}

        return main_thread("material_get_properties", _get)

    @mcp.tool(name="material_set_property")
    def material_set_property(path: str, name: str, value: Any, value_type: str = "auto", knowledge_token: str = "") -> dict:
        """Set one material property."""

        def _set():
            require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            _require_writable_material_path(path)
            mat = _apply_material_edit(
                project_path,
                path,
                lambda candidate: _set_one(candidate, name, value, value_type),
                edit_key=f"property:{name}",
                description=f"Set Material {name}",
            )
            return {"path": path, "name": name, "value": serialize_value(mat.get_property(name)), **_material_info(mat)}

        return main_thread("material_set_property", _set, arguments={"path": path, "name": name, "value_type": value_type, "knowledge_token": knowledge_token})

    @mcp.tool(name="material_set_render_queue")
    def material_set_render_queue(
        path: str,
        render_queue: int,
        knowledge_token: str = "",
    ) -> dict:
        """Set the material render queue used by pipeline route selectors."""

        def _set():
            require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            _require_writable_material_path(path)
            file_path = resolve_project_path(project_path, path)
            mat = _apply_material_edit(
                project_path,
                path,
                lambda candidate: setattr(candidate, "render_queue", int(render_queue)),
                edit_key="render_queue",
                description="Set Material Render Queue",
            )
            return {
                "path": relative_path(file_path, project_path),
                **_material_info(mat),
            }

        return main_thread(
            "material_set_render_queue",
            _set,
            arguments={
                "path": path,
                "render_queue": render_queue,
                "knowledge_token": knowledge_token,
            },
        )

    @mcp.tool(name="material_set_surface_type")
    def material_set_surface_type(
        path: str,
        surface_type: str,
        knowledge_token: str = "",
    ) -> dict:
        """Set opaque/transparent render state through the public Material API."""

        def _set():
            require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            _require_writable_material_path(path)
            normalized = str(surface_type or "").strip().lower()
            if normalized not in {"opaque", "transparent"}:
                raise ValueError("surface_type must be 'opaque' or 'transparent'.")
            file_path = resolve_project_path(project_path, path)
            mat = _apply_material_edit(
                project_path,
                path,
                lambda candidate: setattr(candidate, "surface_type", normalized),
                edit_key="surface_type",
                description="Set Material Surface Type",
            )
            return {
                "path": relative_path(file_path, project_path),
                **_material_info(mat),
            }

        return main_thread(
            "material_set_surface_type",
            _set,
            arguments={
                "path": path,
                "surface_type": surface_type,
                "knowledge_token": knowledge_token,
            },
        )

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
            _require_writable_material_path(path)
            file_path = resolve_project_path(project_path, path)
            if not vertex and not fragment:
                raise ValueError("At least one of vertex or fragment must be provided.")
            if vertex:
                _require_shader_stage(vertex, "vertex")
            if fragment:
                _require_shader_stage(fragment, "fragment")

            def _assign_shader(candidate):
                if vertex:
                    candidate.vert_shader_name = str(vertex).strip()
                if fragment:
                    candidate.frag_shader_name = str(fragment).strip()

            mat = _apply_material_edit(
                project_path,
                path,
                _assign_shader,
                edit_key="shader_program",
                description="Set Material Shader",
            )
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


def _builtin_material_key(path: str) -> str:
    identity = str(path or "").strip()
    if not identity.lower().startswith(_BUILTIN_MATERIAL_URI_PREFIX):
        return ""
    key = identity[len(_BUILTIN_MATERIAL_URI_PREFIX) :].strip()
    if not key or "/" in key or "\\" in key:
        raise ValueError(
            "Built-in material paths must use 'builtin://<material-key>' without nested paths."
        )
    return key


def _require_writable_material_path(path: str) -> None:
    if _builtin_material_key(path):
        raise PermissionError(
            "Built-in materials are read-only; clone one into Assets before editing it."
        )


def _load_material(project_path: str, path: str, *, allow_builtin: bool = False):
    from Infernux.core.material import Material

    builtin_key = _builtin_material_key(path)
    if builtin_key:
        if not allow_builtin:
            _require_writable_material_path(path)
        mat = Material.get(builtin_key)
        if mat is None:
            raise FileNotFoundError(f"Built-in material not found: {builtin_key}")
        return mat

    file_path = resolve_project_path(project_path, path)
    mat = Material.load(file_path)
    if mat is None:
        raise FileNotFoundError(f"Material not found or failed to load: {path}")
    return mat


def _editable_material(project_path: str, path: str):
    from Infernux.engine.interaction import (
        DocumentKind,
        DocumentRegistry,
        EditableResourceDocumentController,
        ensure_editable_resource_document,
    )
    from Infernux.engine.ui.inspector_material import notify_material_document_restored

    file_path = resolve_project_path(project_path, path)
    registry = DocumentRegistry.instance()
    for document in registry.documents:
        if (
            document.kind is DocumentKind.MATERIAL
            and document.resource_path
            and same_path(document.resource_path, file_path)
            and isinstance(document.controller, EditableResourceDocumentController)
        ):
            return document.controller.resource, document.controller

    material = _load_material(project_path, path)
    controller = ensure_editable_resource_document(
        category="material",
        document_kind=DocumentKind.MATERIAL,
        file_path=file_path,
        resource=material,
        guid=str(getattr(material, "guid", "") or ""),
        title=os.path.basename(file_path),
        on_restored=notify_material_document_restored,
    )
    return controller.resource, controller


def _apply_material_edit(
    project_path: str,
    path: str,
    mutate,
    *,
    edit_key: str,
    description: str,
):
    from Infernux.engine.interaction import ActionOrigin

    material, controller = _editable_material(project_path, path)
    original_document = material.serialize_document()
    candidate = material.clone()
    mutate(candidate)
    document = candidate.serialize_document()
    # clone() deliberately creates a unique runtime identity.  The candidate
    # only isolates mutations; its instance name/builtin flag must never leak
    # back into the durable asset document.
    for identity_field in ("name", "builtin"):
        if identity_field in original_document:
            document[identity_field] = copy.deepcopy(original_document[identity_field])
    if not controller.apply_document(
        document,
        view_id="automation",
        edit_key=edit_key,
        description=description,
        origin=ActionOrigin.AUTOMATION,
    ):
        return material
    return controller.resource


def _set_properties(mat, properties: dict[str, Any]) -> None:
    for name, value in properties.items():
        _set_one(mat, name, value, "auto")


def _set_one(mat, name: str, value: Any, value_type: str) -> None:
    normalized_name = str(name or "").strip().lower()
    if normalized_name in _RESERVED_RENDER_STATE_PROPERTIES:
        raise ValueError(
            f"'{name}' is render state, not a shader property; use the dedicated material tool."
        )
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
        "is_builtin": bool(getattr(mat, "is_builtin", False)),
        "shader": {
            "shader_name": str(getattr(mat, "shader_name", "") or ""),
            "vertex": str(getattr(mat, "vert_shader_name", "") or ""),
            "fragment": str(getattr(mat, "frag_shader_name", "") or ""),
        },
        "render_queue": int(getattr(mat, "render_queue", 0) or 0),
        "surface_type": str(getattr(mat, "surface_type", "opaque") or "opaque"),
        "blend_enable": bool(getattr(mat, "blend_enable", False)),
        "depth_write_enable": bool(getattr(mat, "depth_write_enable", True)),
        "properties": _properties(mat),
    }


def _register_metadata() -> None:
    for name, summary in {
        "material_create": "Create a material asset.",
        "material_get_properties": "Read project materials or read-only builtin://<material-key> properties.",
        "material_set_property": "Set a material shader property.",
        "material_set_render_queue": "Set the material render queue.",
        "material_set_surface_type": "Set opaque or transparent material render state.",
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
