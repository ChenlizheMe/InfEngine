"""Scene inspection and mutation MCP tools."""

from __future__ import annotations

import os
from typing import Any

from Infernux.engine.path_utils import (
    is_path_within,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
)
from Infernux.mcp.tools.common import (
    coerce_vector3,
    find_game_object,
    main_thread,
    require_knowledge_token,
    resolve_asset_path,
    serialize_component,
    serialize_value,
    serialize_vector,
    scene_status,
)


def _scene_object_service():
    """Return the same scene mutation authority used by visible editor panels."""
    from Infernux.engine.interaction import EditorInteractionCore

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("Editor interaction core is unavailable.")
    return core.scene_objects


def _component_command_service():
    """Return the component authority shared by Inspector and automation."""
    from Infernux.engine.interaction import EditorInteractionCore

    core = EditorInteractionCore.instance()
    if core is None:
        raise RuntimeError("Editor interaction core is unavailable.")
    return core.components


def _add_component_through_editor_transaction(
    obj,
    component_type: str,
    *,
    python_instance=None,
    fields: dict[str, Any] | None = None,
):
    """Use the same atomic add command as the visible Inspector."""
    initial_fields = dict(fields or {})

    def _initialize(component):
        for key, value in initial_fields.items():
            current_value = _component_field_value(component, key)
            setattr(
                component,
                key,
                _coerce_component_property_value(
                    component, key, value, current_value
                ),
            )

    return _component_command_service().add(
        obj,
        component_type,
        python_instance=python_instance,
        initializer=_initialize if initial_fields else None,
        description=f"Add {component_type}",
    )


def _remove_component_through_editor_transaction(obj, component) -> bool:
    """Use the same constraint-aware undo command as the visible Inspector."""
    return _component_command_service().remove(
        obj,
        component,
        description="MCP Remove Component",
    )


def _set_component_fields_through_editor_transaction(
    component,
    component_type: str,
    values: dict[str, Any] | None,
) -> dict[str, Any]:
    changes = []
    for field, value in (values or {}).items():
        old_value = _component_field_value(component, field)
        new_value = _coerce_component_property_value(
            component, field, value, old_value
        )
        if serialize_value(new_value) != serialize_value(old_value):
            changes.append((field, old_value, new_value))

    if changes:
        service = _component_command_service()
        service.execute_property_changes(
            [
                (
                    component,
                    field,
                    old_value,
                    new_value,
                    f"Set {component_type}.{field}",
                )
                for field, old_value, new_value in changes
            ],
            description=f"Set {component_type} Fields",
        )

    return {
        field: _serialize_component_field_value(component, field)
        for field, _old_value, _new_value in changes
    }


def register_scene_tools(mcp) -> None:
    @mcp.tool(name="scene_get_hierarchy")
    def scene_get_hierarchy(
        depth: int = 6,
        include_components: bool = True,
        include_inactive: bool = True,
    ) -> dict:
        """Return the active scene hierarchy."""

        def _read():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            roots = list(scene.get_root_objects() or [])
            return {
                "scene": getattr(scene, "name", ""),
                "roots": [
                    _serialize_object(
                        root,
                        depth=max(int(depth), 0),
                        include_components=bool(include_components),
                        include_inactive=bool(include_inactive),
                    )
                    for root in roots
                    if include_inactive or bool(getattr(root, "active", True))
                ],
            }

        return main_thread("scene_get_hierarchy", _read)

    @mcp.tool(name="scene_save")
    def scene_save(path: str = "") -> dict:
        """Save the active scene. If path is provided, it must be under Assets/."""

        def _save():
            from Infernux.engine.interaction import (
                DocumentActionStatus,
                DocumentRegistry,
            )
            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            if sfm is None:
                raise RuntimeError("SceneFileManager is not available.")
            registry = DocumentRegistry.instance()
            if path:
                from Infernux.engine.project_context import get_project_root
                save_path = resolve_asset_path(get_project_root(), path)
                result = registry.request_save_to_resource(
                    sfm.document_id,
                    save_path,
                )
            else:
                current_path = getattr(sfm, "current_scene_path", "") or ""
                if current_path:
                    save_path = current_path
                    result = registry.request_save(sfm.document_id)
                else:
                    raise ValueError(
                        "An unnamed scene requires an explicit Assets-relative path."
                    )
            if result.status in {
                DocumentActionStatus.FAILED,
                DocumentActionStatus.REJECTED,
            }:
                raise RuntimeError(result.message or result.status.value)
            ok = result.status in {
                DocumentActionStatus.APPLIED,
                DocumentActionStatus.NO_OP,
            }
            return {
                "saved": ok,
                "path": _project_rel(save_path) if save_path else "",
                "absolute_path": save_path,
                "dirty": bool(getattr(sfm, "is_dirty", False)),
                "status": scene_status(),
            }

        return main_thread("scene_save", _save, arguments={"path": path})

    @mcp.tool(name="scene_status")
    def scene_status_tool() -> dict:
        """Return active scene path, dirty state, and suggested save path."""
        return main_thread("scene_status", scene_status)

    @mcp.tool(name="scene_open")
    def scene_open(path: str) -> dict:
        """Open a .scene file under Assets/.

        If the active scene is dirty, call ``scene_discard(force=True,
        reason=...)`` or ``scene_save`` first.  MCP never discards scene
        authoring data implicitly.
        """

        def _open():
            from Infernux.engine.project_context import get_project_root
            from Infernux.engine.scene_manager import SceneFileManager
            from Infernux.engine.interaction import (
                DocumentKind,
                DocumentOpenStatus,
                EditorInteractionCore,
            )
            scene_path = resolve_asset_path(get_project_root(), path)
            sfm = SceneFileManager.instance()
            if sfm is None:
                raise RuntimeError("SceneFileManager is not available.")
            if getattr(sfm, "is_dirty", False):
                raise RuntimeError("The active scene is dirty. Call scene_save before scene.open.")
            current_path = resolved_path(str(getattr(sfm, "current_scene_path", "") or ""))
            if current_path and same_path(scene_path, current_path):
                return {
                    "accepted": True,
                    "already_open": True,
                    "path": _project_rel(scene_path),
                    "absolute_path": scene_path,
                    "loading": bool(getattr(sfm, "is_loading", False)),
                    "status": scene_status(),
                }
            core = EditorInteractionCore.instance()
            if core is None:
                raise RuntimeError("EditorInteractionCore is not available.")
            result = core.document_open.open_resource(
                DocumentKind.SCENE,
                scene_path,
                title=os.path.splitext(os.path.basename(scene_path))[0],
            )
            if result.status is DocumentOpenStatus.FAILED:
                raise RuntimeError(result.message or "Scene document open failed")
            return {
                "accepted": result.status in {
                    DocumentOpenStatus.READY,
                    DocumentOpenStatus.PENDING,
                },
                "already_open": False,
                "path": _project_rel(scene_path),
                "absolute_path": scene_path,
                "loading": (
                    result.status is DocumentOpenStatus.PENDING
                    or bool(getattr(sfm, "is_loading", False))
                ),
            }

        return main_thread("scene_open", _open, arguments={"path": path})

    @mcp.tool(name="scene_new")
    def scene_new(force: bool = False, reason: str = "") -> dict:
        """Create a new empty scene through SceneFileManager.

        A dirty scene must be explicitly saved or discarded first.
        """

        def _new():
            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            if sfm is None:
                raise RuntimeError("SceneFileManager is not available.")
            if not force:
                raise ValueError("scene_new is destructive. Pass force=true and a short reason to create a new empty scene.")
            if not reason:
                raise ValueError("scene_new requires a reason when force=true.")
            if getattr(sfm, "is_dirty", False):
                raise RuntimeError("The active scene is dirty. Call scene_save before scene.new.")
            sfm.new_scene()
            return {"accepted": True, "loading": bool(getattr(sfm, "is_loading", False)), "status": scene_status()}

        return main_thread("scene_new", _new, arguments={"force": force, "reason": reason})

    @mcp.tool(name="scene_discard")
    def scene_discard(force: bool = False, reason: str = "") -> dict:
        """Discard the active scene's dirty authoring state explicitly.

        A path-backed scene is reloaded from its current durable file.  An
        unnamed scene is replaced with a fresh, clean default scene.  The
        operation is destructive and therefore requires both ``force=true``
        and a non-empty ``reason``; MCP never silently throws away a scene.
        """

        def _discard():
            from Infernux.engine.interaction import (
                DocumentActionStatus,
                DocumentRegistry,
            )
            from Infernux.engine.scene_manager import SceneFileManager

            if not force:
                raise ValueError(
                    "scene_discard is destructive. Pass force=true and a short reason."
                )
            if not str(reason or "").strip():
                raise ValueError(
                    "scene_discard requires a reason when force=true."
                )
            sfm = SceneFileManager.instance()
            if sfm is None:
                raise RuntimeError("SceneFileManager is not available.")
            if not getattr(sfm, "is_dirty", False):
                return {
                    "accepted": True,
                    "discarded": False,
                    "no_op": True,
                    "status": scene_status(),
                }

            registry = DocumentRegistry.instance()
            document_id = str(getattr(sfm, "document_id", "") or "")
            if not document_id:
                raise RuntimeError("The active scene is not bound to a document.")

            if not getattr(sfm, "current_scene_path", None):
                # SceneFileManager's normal new-scene transaction is the same
                # authority used by the visible editor.  Establishing the
                # loaded baseline afterwards makes the unnamed result
                # explicitly clean instead of leaving a fresh default scene
                # dirty merely because it contains the default camera/light.
                if not bool(sfm._do_new_scene()):
                    raise RuntimeError("The unnamed scene could not be reset.")
                registry.establish_loaded_baseline(sfm.document_id)
                return {
                    "accepted": True,
                    "discarded": True,
                    "reset": "clean_unnamed_scene",
                    "status": scene_status(),
                }

            result = registry.request_discard(document_id)
            if result.status in {
                DocumentActionStatus.FAILED,
                DocumentActionStatus.REJECTED,
            }:
                raise RuntimeError(result.message or "Scene discard failed.")
            return {
                "accepted": True,
                "discarded": True,
                "reset": "durable_scene_file",
                "status": scene_status(),
            }

        return main_thread(
            "scene_discard",
            _discard,
            arguments={"force": force, "reason": reason},
        )

    @mcp.tool(name="scene_serialize")
    def scene_serialize() -> dict:
        """Return the active scene document."""

        def _serialize():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            return {
                "scene": getattr(scene, "name", ""),
                "document": scene.serialize_document(),
            }

        return main_thread("scene_serialize", _serialize)

    @mcp.tool(name="scene_inspect")
    def scene_inspect(depth: int = 2, include_components: bool = True) -> dict:
        """Return a compact scene summary for agents."""

        def _inspect():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            objects = list(scene.get_all_objects() or [])
            roots = list(scene.get_root_objects() or [])
            component_counts: dict[str, int] = {}
            cameras = []
            lights = []
            scripts = []
            for obj in objects:
                comps = _all_components(obj)
                for comp in comps:
                    type_name = comp.get("type", "")
                    component_counts[type_name] = component_counts.get(type_name, 0) + 1
                    if type_name == "Camera":
                        cameras.append({"id": int(obj.id), "name": str(obj.name)})
                    elif type_name == "Light":
                        lights.append({"id": int(obj.id), "name": str(obj.name)})
                    elif comp.get("python"):
                        scripts.append({
                            "object_id": int(obj.id),
                            "object_name": str(obj.name),
                            "type": type_name,
                            "script_guid": comp.get("script_guid", ""),
                        })
            return {
                "scene": getattr(scene, "name", ""),
                "status": scene_status(),
                "object_count": len(objects),
                "root_count": len(roots),
                "component_counts": component_counts,
                "cameras": cameras,
                "lights": lights,
                "scripts": scripts,
                "roots": [
                    _serialize_object(
                        root,
                        depth=max(int(depth), 0),
                        include_components=bool(include_components),
                        include_inactive=True,
                    )
                    for root in roots
                ],
            }

        return main_thread("scene_inspect", _inspect)

    @mcp.tool(name="gameobject_add_component")
    def gameobject_add_component(
        object_id: int,
        component_type: str,
        script_path: str = "",
        fields: dict[str, Any] | None = None,
        knowledge_token: str = "",
    ) -> dict:
        """Add a native, built-in wrapper, registered Python, or script component."""

        def _add():
            _require_creatable_component_type(component_type)
            _require_component_knowledge(component_type, knowledge_token)
            obj = find_game_object(object_id)
            instance = None

            if script_path:
                from Infernux.components import load_and_create_component
                from Infernux.mcp import capabilities
                from Infernux.mcp.tools.common import (
                    get_asset_database,
                    resolve_asset_path,
                )

                resolved_script_path = resolve_asset_path(
                    capabilities.project_path(), script_path
                )
                instance = load_and_create_component(
                    resolved_script_path,
                    asset_database=get_asset_database(),
                    type_name=component_type,
                )
                if instance is None:
                    raise RuntimeError(f"Script did not create component '{component_type}'.")
            comp = _add_component_through_editor_transaction(
                obj,
                component_type,
                python_instance=instance,
                fields=fields,
            )
            return {
                "object_id": int(obj.id),
                "component": serialize_component(comp),
                "components": _all_components(obj),
            }

        return main_thread("gameobject_add_component", _add, arguments={"object_id": object_id, "component_type": component_type, "knowledge_token": knowledge_token})

    @mcp.tool(name="gameobject_create_from_model")
    def gameobject_create_from_model(
        model_path: str,
        parent_id: int = 0,
        name: str = "",
        select: bool = True,
    ) -> dict:
        """Instantiate an imported model asset through the Editor's normal model path."""

        def _create():
            from Infernux.engine.project_context import get_project_root
            from Infernux.mcp.tools.common import get_asset_database

            absolute_path = resolve_asset_path(get_project_root(), model_path)
            asset_database = get_asset_database()
            guid = str(asset_database.get_guid_from_path(absolute_path) or "")
            if not guid:
                raise FileNotFoundError(
                    f"Imported model asset was not found in AssetDatabase: {model_path}"
                )

            obj = _scene_object_service().create_model_object(
                guid,
                parent_id=int(parent_id or 0),
                is_guid=True,
                name=str(name or ""),
                select=bool(select),
                selection_owner_id="automation",
                selection_reason="mcp_create_model",
            )
            if obj is None:
                raise RuntimeError(f"Failed to instantiate model asset: {model_path}")

            value = _serialize_object(
                obj,
                depth=2,
                include_components=True,
                include_inactive=True,
            )
            value["model_path"] = _project_rel(absolute_path)
            value["model_guid"] = guid
            return value

        return main_thread(
            "gameobject_create_from_model",
            _create,
            arguments={
                "model_path": model_path,
                "parent_id": parent_id,
                "name": name,
                "select": select,
            },
        )

    @mcp.tool(name="gameobject_get")
    def gameobject_get(object_id: int, depth: int = 1, include_components: bool = True) -> dict:
        """Return a GameObject snapshot by id."""

        def _get():
            obj = find_game_object(object_id)
            return _serialize_object(
                obj,
                depth=max(int(depth), 0),
                include_components=bool(include_components),
                include_inactive=True,
            )

        return main_thread("gameobject_get", _get)

    @mcp.tool(name="gameobject_get_children")
    def gameobject_get_children(object_id: int = 0, include_components: bool = False) -> dict:
        """Return root objects (object_id=0) or direct children of a GameObject."""

        def _children():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            if int(object_id) == 0:
                children = list(scene.get_root_objects() or [])
                parent_id = 0
            else:
                obj = find_game_object(object_id)
                children = list(obj.get_children() or [])
                parent_id = int(obj.id)
            return {
                "parent_id": parent_id,
                "children": [
                    _serialize_object(
                        child,
                        depth=0,
                        include_components=bool(include_components),
                        include_inactive=True,
                    )
                    for child in children
                ],
            }

        return main_thread("gameobject_get_children", _children)

    @mcp.tool(name="gameobject_find")
    def gameobject_find(
        name: str = "",
        path: str = "",
        tag: str = "",
        component_type: str = "",
        include_inactive: bool = True,
        limit: int = 50,
    ) -> dict:
        """Find GameObjects by name, tag, and/or component type."""

        def _find():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            matches = []
            for obj in list(scene.get_all_objects() or []):
                if not include_inactive and not bool(getattr(obj, "active", True)):
                    continue
                if name and name.lower() not in str(obj.name).lower():
                    continue
                if path and path.lower() not in _object_path(obj).lower():
                    continue
                if tag and str(getattr(obj, "tag", "")) != tag:
                    continue
                if component_type and _find_component(obj, component_type, 0) is None:
                    continue
                data = _serialize_object(obj, depth=0, include_components=True, include_inactive=True)
                data["path"] = _object_path(obj)
                matches.append(data)
                if len(matches) >= max(int(limit), 1):
                    break
            return {"matches": matches}

        return main_thread("gameobject_find", _find)

    @mcp.tool(name="scene_find")
    def scene_find(query: dict[str, Any], limit: int = 50) -> dict:
        """Search scene objects by name/path/tag/layer/component."""

        def _scene_find():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            criteria = query or {}
            q_name = str(criteria.get("name", "")).lower()
            q_path = str(criteria.get("path", "")).lower()
            q_tag = str(criteria.get("tag", ""))
            q_component = str(criteria.get("component_type", ""))
            q_layer = criteria.get("layer", None)
            matches = []
            for obj in list(scene.get_all_objects() or []):
                if q_name and q_name not in str(obj.name).lower():
                    continue
                obj_path = _object_path(obj)
                if q_path and q_path not in obj_path.lower():
                    continue
                if q_tag and str(getattr(obj, "tag", "")) != q_tag:
                    continue
                if q_layer is not None and int(getattr(obj, "layer", -1)) != int(q_layer):
                    continue
                if q_component and _find_component(obj, q_component, 0) is None:
                    continue
                data = _serialize_object(obj, depth=0, include_components=True, include_inactive=True)
                data["path"] = obj_path
                matches.append(data)
                if len(matches) >= max(int(limit), 1):
                    break
            return {"matches": matches}

        return main_thread("scene_find", _scene_find)

    @mcp.tool(name="scene_query_objects")
    def scene_query_objects(query: dict[str, Any] | None = None, limit: int = 50, include_components: bool = True) -> dict:
        """Semantic GameObject search by name/path/component/tag/layer/active filters."""

        def _query_objects():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            criteria = query or {}
            matches = []
            for obj in list(scene.get_all_objects() or []):
                if not _matches_scene_query(obj, criteria):
                    continue
                data = _serialize_object(obj, depth=0, include_components=bool(include_components), include_inactive=True)
                data["path"] = _object_path(obj)
                data["spatial"] = _spatial_summary(obj)
                matches.append(data)
                if len(matches) >= max(int(limit), 1):
                    break
            result = {"query": criteria, "matches": matches, "count": len(matches)}
            if not matches:
                result.update(_empty_query_result(criteria, scene))
            return result

        return main_thread("scene_query_objects", _query_objects, arguments={"query": query or {}, "limit": limit})

    @mcp.tool(name="scene_query_summary")
    def scene_query_summary(include_subjects: bool = True, subject_limit: int = 8) -> dict:
        """Return grouped scene semantics: cameras, lights, renderers, UI, scripts, and likely subjects."""

        def _summary():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            objects = list(scene.get_all_objects() or [])
            groups = _group_scene_objects(objects)
            return {
                "scene": getattr(scene, "name", ""),
                "status": scene_status(),
                "object_count": len(objects),
                "groups": groups,
                "subjects": _rank_subjects(objects, max(int(subject_limit), 1)) if include_subjects else [],
            }

        return main_thread("scene_query_summary", _summary)

    @mcp.tool(name="scene_query_subjects")
    def scene_query_subjects(query: dict[str, Any] | None = None, limit: int = 8) -> dict:
        """Rank likely primary scene subjects for camera framing or gameplay edits."""

        def _subjects():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            objects = [
                obj
                for obj in list(scene.get_all_objects() or [])
                if _matches_scene_query(obj, query or {})
            ]
            subjects = _rank_subjects(objects, max(int(limit), 1))
            result = {"subjects": subjects, "query": query or {}}
            if not subjects:
                result.update(_empty_subject_result(objects, query or {}))
            return result

        return main_thread("scene_query_subjects", _subjects, arguments={"query": query or {}, "limit": limit})

    @mcp.tool(name="gameobject_describe_spatial")
    def gameobject_describe_spatial(object_id: int, include_descendants: bool = True) -> dict:
        """Return transform, hierarchy path, components, and approximate bounds."""

        def _describe_spatial():
            obj = find_game_object(object_id)
            return {
                "object_id": int(obj.id),
                "name": str(obj.name),
                "path": _object_path(obj),
                "components": _component_names(obj),
                "spatial": _spatial_summary(obj, include_descendants=bool(include_descendants)),
                "children_count": len(list(obj.get_children() or [])),
            }

        return main_thread("gameobject_describe_spatial", _describe_spatial, arguments={"object_id": object_id})

    @mcp.tool(name="gameobject_path")
    def gameobject_path(object_id: int) -> dict:
        """Return the hierarchy path for a GameObject."""

        def _path():
            obj = find_game_object(object_id)
            return {"object_id": int(obj.id), "path": _object_path(obj)}

        return main_thread("gameobject_path", _path)

    @mcp.tool(name="gameobject_find_by_path")
    def gameobject_find_by_path(path: str) -> dict:
        """Find a GameObject by exact hierarchy path."""

        def _find_by_path():
            obj = _find_by_path_exact(path)
            if obj is None:
                raise FileNotFoundError(f"GameObject path not found: {path}")
            data = _serialize_object(obj, depth=1, include_components=True, include_inactive=True)
            data["path"] = _object_path(obj)
            return data

        return main_thread("gameobject_find_by_path", _find_by_path)

    @mcp.tool(name="gameobject_ensure_path")
    def gameobject_ensure_path(path: str, kind: str = "empty", select: bool = False) -> dict:
        """Ensure a slash-separated hierarchy path exists."""

        def _ensure_path():
            from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
            if not path or not str(path).strip("/"):
                raise ValueError("path must not be empty.")
            created = []
            parent_id = 0
            current_path = ""
            target_path = portable_path(str(path)).strip("/")
            with _scene_object_service().user_action("Ensure GameObject Path"):
                for part in [p for p in target_path.split("/") if p]:
                    current_path = f"{current_path}/{part}" if current_path else part
                    existing = _find_by_path_exact(current_path)
                    if existing is not None:
                        parent_id = int(existing.id)
                        continue
                    entry = HierarchyCreationService.instance().create(
                        kind if current_path == target_path else "empty",
                        parent_id=parent_id,
                        name=part,
                        select=False,
                    )
                    created.append(entry)
                    parent_id = int(entry["id"])
                if select and parent_id:
                    from Infernux.engine.interaction import SelectionService

                    SelectionService.instance().select_scene_object(
                        parent_id,
                        owner_id="automation",
                        reason="mcp_ensure_path",
                    )
            final = find_game_object(parent_id)
            return {"object_id": parent_id, "path": _object_path(final), "created": created}

        return main_thread("gameobject_ensure_path", _ensure_path)

    @mcp.tool(name="gameobject_set")
    def gameobject_set(object_id: int, values: dict[str, Any]) -> dict:
        """Set GameObject fields: name, active, tag, layer, is_static."""

        def _set():
            obj = find_game_object(object_id)
            allowed = {"name", "active", "tag", "layer", "is_static"}
            changed = {}
            from Infernux.engine.interaction import (
                PropertyTransactionStatus,
                make_attribute_property_transaction,
            )
            normalized = []
            for key, value in (values or {}).items():
                if key not in allowed:
                    raise ValueError(f"Unsupported GameObject field: {key}")
                new_value = int(value) if key == "layer" else bool(value) if key in {"active", "is_static"} else str(value)
                normalized.append((key, new_value))
            with _scene_object_service().user_action("Set GameObject Fields"):
                for key, new_value in normalized:
                    transaction = make_attribute_property_transaction(
                        (obj,),
                        key,
                        property_path=f"GameObject.{key}",
                        description=f"Set GameObject {key}",
                    )
                    if transaction.commit(new_value) is PropertyTransactionStatus.APPLIED:
                        changed[key] = serialize_value(getattr(obj, key))
            return {"object_id": int(obj.id), "changed": changed}

        return main_thread("gameobject_set", _set)

    @mcp.tool(name="gameobject_delete")
    def gameobject_delete(object_id: int) -> dict:
        """Delete a GameObject through the global scene mutation authority."""

        def _delete():
            obj = find_game_object(object_id)
            name = str(obj.name)
            if not _scene_object_service().delete_ids(
                (int(object_id),),
                description="Delete GameObject",
            ):
                raise RuntimeError(f"Failed to delete GameObject {object_id}.")
            return {"deleted": True, "object_id": int(object_id), "name": name}

        return main_thread("gameobject_delete", _delete)

    @mcp.tool(name="gameobject_batch_delete")
    def gameobject_batch_delete(object_ids: list[int]) -> dict:
        """Delete multiple GameObjects."""

        def _batch_delete():
            deleted = []
            for object_id in object_ids or []:
                obj = find_game_object(int(object_id))
                deleted.append({"id": int(obj.id), "name": str(obj.name), "path": _object_path(obj)})
            if deleted and not _scene_object_service().delete_ids(
                (entry["id"] for entry in deleted),
                description="Delete GameObjects",
            ):
                raise RuntimeError("Failed to delete GameObjects.")
            return {"deleted": deleted}

        return main_thread("gameobject_batch_delete", _batch_delete)

    @mcp.tool(name="gameobject_batch_create")
    def gameobject_batch_create(items: list[dict[str, Any]]) -> dict:
        """Create multiple hierarchy objects."""

        def _batch_create():
            from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
            created = []
            with _scene_object_service().user_action("Create GameObjects"):
                for item in items or []:
                    parent_id = int(item.get("parent_id", 0) or 0)
                    parent_path = item.get("parent_path", "")
                    if parent_path:
                        parent = _find_by_path_exact(parent_path)
                        if parent is None:
                            raise FileNotFoundError(f"Parent path not found: {parent_path}")
                        parent_id = int(parent.id)
                    entry = HierarchyCreationService.instance().create(
                        str(item.get("kind", "empty")),
                        parent_id=parent_id,
                        name=item.get("name") or None,
                        select=bool(item.get("select", False)),
                    )
                    obj = find_game_object(int(entry["id"]))
                    entry["path"] = _object_path(obj)
                    created.append(entry)
            return {"created": created}

        return main_thread("gameobject_batch_create", _batch_create)

    @mcp.tool(name="gameobject_duplicate")
    def gameobject_duplicate(object_id: int, parent_id: int = 0, name: str = "", select: bool = True) -> dict:
        """Duplicate a GameObject transactionally."""

        def _duplicate():
            obj = _scene_object_service().duplicate_object(
                int(object_id),
                parent_id=int(parent_id or 0),
                name=str(name or ""),
                select=bool(select),
                selection_owner_id="automation",
                selection_reason="mcp_duplicate_object",
            )
            if obj is None:
                raise RuntimeError("Failed to duplicate GameObject.")
            return _serialize_object(obj, depth=1, include_components=True, include_inactive=True)

        return main_thread("gameobject_duplicate", _duplicate)

    @mcp.tool(name="gameobject_set_parent")
    def gameobject_set_parent(object_id: int, parent_id: int = 0, world_position_stays: bool = True) -> dict:
        """Set or clear a GameObject parent."""

        def _set_parent():
            obj = find_game_object(object_id)
            old_parent = obj.get_parent()
            if not bool(world_position_stays):
                raise ValueError(
                    "Editor hierarchy authoring always preserves world position."
                )
            moved = _scene_object_service().move_hierarchy(
                (int(object_id),),
                "parent" if int(parent_id or 0) else "root",
                target_id=int(parent_id or 0),
            )
            if not moved and int(getattr(old_parent, "id", 0) or 0) != int(parent_id or 0):
                raise RuntimeError("Failed to move GameObject in Hierarchy.")
            return {
                "object_id": int(obj.id),
                "parent_id": int(getattr(obj.get_parent(), "id", 0) or 0),
                "old_parent_id": int(getattr(old_parent, "id", 0) or 0),
            }

        return main_thread("gameobject_set_parent", _set_parent)

    @mcp.tool(name="gameobject_set_sibling_index")
    def gameobject_set_sibling_index(object_id: int, index: int) -> dict:
        """Move a GameObject within its current sibling list."""

        def _set_sibling_index():
            obj = find_game_object(object_id)
            old_index = int(obj.transform.get_sibling_index())
            moved = _scene_object_service().set_sibling_index(
                int(object_id), int(index)
            )
            if not moved and old_index != int(index):
                raise RuntimeError("Failed to change GameObject sibling index.")
            return {
                "object_id": int(obj.id),
                "old_index": old_index,
                "index": int(obj.transform.get_sibling_index()),
                "parent_id": int(getattr(obj.get_parent(), "id", 0) or 0),
            }

        return main_thread("gameobject_set_sibling_index", _set_sibling_index)

    @mcp.tool(name="scene_clear_generated")
    def scene_clear_generated(name_prefix: str = "MCP", root_path: str = "") -> dict:
        """Delete generated scene objects by root path or name prefix."""

        def _clear():
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                raise RuntimeError("No active scene.")
            targets = []
            if root_path:
                root = _find_by_path_exact(root_path)
                if root is not None:
                    targets.append(root)
            else:
                for obj in scene.get_root_objects() or []:
                    if str(obj.name).startswith(name_prefix):
                        targets.append(obj)
            deleted = []
            for obj in targets:
                deleted.append({"id": int(obj.id), "name": str(obj.name), "path": _object_path(obj)})
            if deleted and not _scene_object_service().delete_ids(
                (entry["id"] for entry in deleted),
                description="Clear Generated GameObjects",
            ):
                raise RuntimeError("Failed to clear generated GameObjects.")
            return {"deleted": deleted}

        return main_thread("scene_clear_generated", _clear)

    @mcp.tool(name="transform_set")
    def transform_set(object_id: int, values: dict[str, Any]) -> dict:
        """Set Transform fields such as position, euler_angles, or local_scale."""

        def _set():
            obj = find_game_object(object_id)
            trans = obj.transform
            if trans is None:
                raise RuntimeError(f"GameObject {object_id} has no Transform.")
            allowed = {
                "position",
                "local_position",
                "euler_angles",
                "local_euler_angles",
                "local_scale",
            }
            changed: dict[str, Any] = {}
            from Infernux.engine.interaction import ComponentCommandService
            changes = []
            for key, value in values.items():
                if key not in allowed:
                    raise ValueError(f"Unsupported Transform field: {key}")
                new_value = coerce_vector3(value)
                old_value = getattr(trans, key)
                if serialize_vector(old_value) != serialize_vector(new_value):
                    changes.append(
                        (trans, key, old_value, new_value, f"Set Transform {key}")
                    )
            if changes:
                ComponentCommandService.require().execute_property_changes(
                    changes,
                    description="Set Transform Fields",
                )
                for _target, key, _old_value, _new_value, _description in changes:
                    changed[key] = serialize_vector(getattr(trans, key))
            return {"object_id": int(obj.id), "changed": changed}

        return main_thread("transform_set", _set)

    @mcp.tool(name="component_set_field")
    def component_set_field(
        object_id: int,
        component_type: str,
        field: str,
        value: Any,
        ordinal: int = 0,
        knowledge_token: str = "",
    ) -> dict:
        """Set a field/property on a component attached to a GameObject."""

        def _set():
            _require_component_knowledge(component_type, knowledge_token)
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            old_value = _component_field_value(comp, field)
            new_value = _coerce_component_property_value(comp, field, value, old_value)
            _component_command_service().set_field(
                comp,
                field,
                new_value,
                description=f"Set {component_type}.{field}",
            )
            return {
                "object_id": int(obj.id),
                "component": serialize_component(comp),
                "field": field,
                "value": _serialize_component_field_value(comp, field),
            }

        return main_thread("component_set_field", _set, arguments={"object_id": object_id, "component_type": component_type, "field": field, "knowledge_token": knowledge_token})

    @mcp.tool(name="component_set_fields")
    def component_set_fields(object_id: int, component_type: str, values: dict[str, Any], ordinal: int = 0, knowledge_token: str = "") -> dict:
        """Set multiple fields/properties on a component."""

        def _set_fields():
            _require_component_knowledge(component_type, knowledge_token)
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            changed = _set_component_fields_through_editor_transaction(
                comp,
                component_type,
                values,
            )
            return {"object_id": int(obj.id), "component": serialize_component(comp), "changed": changed}

        return main_thread("component_set_fields", _set_fields, arguments={"object_id": object_id, "component_type": component_type, "knowledge_token": knowledge_token})

    @mcp.tool(name="component_ensure")
    def component_ensure(
        object_id: int,
        component_type: str,
        script_path: str = "",
        fields: dict[str, Any] | None = None,
        knowledge_token: str = "",
    ) -> dict:
        """Return an existing component or add it if missing."""

        def _ensure():
            _require_creatable_component_type(component_type)
            _require_component_knowledge(component_type, knowledge_token)
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, 0)
            created = False
            if comp is None:
                instance = None
                if script_path:
                    from Infernux.components import load_and_create_component
                    from Infernux.mcp import capabilities
                    from Infernux.mcp.tools.common import get_asset_database, resolve_asset_path
                    resolved_script_path = resolve_asset_path(
                        capabilities.project_path(), script_path
                    )
                    instance = load_and_create_component(
                        resolved_script_path,
                        asset_database=get_asset_database(),
                        type_name=component_type,
                    )
                    if instance is None:
                        raise RuntimeError(f"Script did not create component '{component_type}'.")
                comp = _add_component_through_editor_transaction(
                    obj,
                    component_type,
                    python_instance=instance,
                    fields=fields,
                )
                created = True
            else:
                _set_component_fields_through_editor_transaction(
                    comp,
                    component_type,
                    fields,
                )
            return {"object_id": int(obj.id), "created": created, "component": serialize_component(comp), "components": _all_components(obj)}

        return main_thread("component_ensure", _ensure, arguments={"object_id": object_id, "component_type": component_type, "knowledge_token": knowledge_token})

    @mcp.tool(name="component_list_on_object")
    def component_list_on_object(object_id: int) -> dict:
        """List components attached to a GameObject."""

        def _list_on_object():
            obj = find_game_object(object_id)
            return {"object_id": int(obj.id), "components": _all_components(obj)}

        return main_thread("component_list_on_object", _list_on_object)

    @mcp.tool(name="component_get_field")
    def component_get_field(object_id: int, component_type: str, field: str, ordinal: int = 0) -> dict:
        """Read a field/property from a component attached to a GameObject."""

        def _get():
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            return {
                "object_id": int(obj.id),
                "component": serialize_component(comp),
                "field": field,
                "value": serialize_value(getattr(comp, field)),
            }

        return main_thread("component_get_field", _get)

    @mcp.tool(name="component_get")
    def component_get(object_id: int, component_type: str, ordinal: int = 0) -> dict:
        """Return component metadata and serializable field values."""

        def _get_component():
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            return _component_snapshot(obj, comp)

        return main_thread("component_get", _get_component)

    @mcp.tool(name="component_remove")
    def component_remove(object_id: int, component_type: str, ordinal: int = 0) -> dict:
        """Remove a native or Python component from a GameObject."""

        def _remove():
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            type_name = getattr(comp, "type_name", type(comp).__name__)
            removed = _remove_component_through_editor_transaction(obj, comp)
            if not removed:
                raise RuntimeError(
                    f"Component '{type_name}' could not be removed because its "
                    "registered component constraints rejected the operation."
                )
            return {"object_id": int(obj.id), "removed": str(type_name), "components": _all_components(obj)}

        return main_thread("component_remove", _remove)

    @mcp.tool(name="component_list_types")
    def component_list_types() -> dict:
        """List known built-in and Python component types."""

        def _list():
            import Infernux.components.builtin  # noqa: F401 - ensure built-ins register
            from Infernux.components.builtin_component import BuiltinComponent
            from Infernux.components.registry import get_all_types
            builtin = [
                _component_type_entry(name, cls, builtin=True)
                for name, cls in sorted(BuiltinComponent._builtin_registry.items())
            ]
            scripts = [
                _component_type_entry(name, cls, builtin=False)
                for name, cls in sorted(get_all_types().items())
                if not _is_builtin_family_class(cls)
            ]
            return {"builtin": builtin, "scripts": scripts}

        return main_thread("component_list_types", _list)

    @mcp.tool(name="component_describe_type")
    def component_describe_type(component_type: str) -> dict:
        """Describe serialized/inspector fields for a component type."""

        def _describe():
            cls = _component_class_for_name(component_type)
            if cls is None:
                raise FileNotFoundError(f"Component type '{component_type}' was not found.")
            return _component_type_entry(component_type, cls, builtin=_is_builtin_component_class(cls), include_fields=True)

        return main_thread("component_describe_type", _describe)

    @mcp.tool(name="component_describe_field")
    def component_describe_field(component_type: str, field: str) -> dict:
        """Describe one field on a component type."""

        def _describe_field():
            cls = _component_class_for_name(component_type)
            if cls is None:
                raise FileNotFoundError(f"Component type '{component_type}' was not found.")
            for item in _component_field_schema(cls):
                if item["name"] == field:
                    return {"component_type": component_type, "field": item}
            raise FileNotFoundError(f"Field '{field}' was not found on component type '{component_type}'.")

        return main_thread("component_describe_field", _describe_field)

    @mcp.tool(name="component_get_snapshot")
    def component_get_snapshot(object_id: int, component_type: str, ordinal: int = 0) -> dict:
        """Capture a native component document for restore_snapshot."""

        def _snapshot():
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            if not hasattr(comp, "serialize_document"):
                raise ValueError(
                    f"Component '{component_type}' does not support document snapshots."
                )
            return {
                **_component_snapshot(obj, comp),
                "document": comp.serialize_document(),
            }

        return main_thread("component_get_snapshot", _snapshot)

    @mcp.tool(name="component_restore_snapshot")
    def component_restore_snapshot(object_id: int, component_type: str, document: dict, ordinal: int = 0) -> dict:
        """Restore a native component from a typed document snapshot."""

        def _restore():
            obj = find_game_object(object_id)
            comp = _find_component(obj, component_type, int(ordinal))
            if comp is None:
                raise FileNotFoundError(f"Component '{component_type}' was not found on GameObject {object_id}.")
            if not hasattr(comp, "deserialize_document"):
                raise ValueError(
                    f"Component '{component_type}' does not support document snapshots."
                )
            _component_command_service().restore_document(
                comp,
                document,
                description=f"Restore {component_type} Snapshot",
                edit_key=f"snapshot:{component_type}",
            )
            return _component_snapshot(obj, comp)

        return main_thread("component_restore_snapshot", _restore)


def _serialize_object(obj, *, depth: int, include_components: bool, include_inactive: bool) -> dict[str, Any]:
    children = []
    if depth > 0:
        try:
            raw_children = list(obj.get_children() or [])
        except Exception:
            raw_children = []
        children = [
            _serialize_object(
                child,
                depth=depth - 1,
                include_components=include_components,
                include_inactive=include_inactive,
            )
            for child in raw_children
            if include_inactive or bool(getattr(child, "active", True))
        ]

    data = {
        "id": int(obj.id),
        "name": str(obj.name),
        "active": bool(getattr(obj, "active", True)),
        "children": children,
    }
    if include_components:
        data["components"] = _all_components(obj)
    return data


def _all_components(obj) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def _append(comp) -> None:
        data = serialize_component(comp)
        component_id = int(data.get("component_id", 0) or 0)
        key = (str(data.get("type", "")), component_id)
        if component_id and key in seen:
            return
        if component_id:
            seen.add(key)
        items.append(data)

    try:
        for comp in (obj.get_components() or []):
            _append(comp)
    except Exception:
        pass
    try:
        for comp in (obj.get_py_components() or []):
            _append(comp)
    except Exception:
        pass
    return items


def _object_path(obj) -> str:
    parts = []
    current = obj
    while current is not None:
        parts.append(str(current.name))
        try:
            current = current.get_parent()
        except Exception:
            current = None
    return "/".join(reversed(parts))


def _find_by_path_exact(path: str):
    from Infernux.lib import SceneManager
    scene = SceneManager.instance().get_active_scene()
    if not scene:
        raise RuntimeError("No active scene.")
    normalized = portable_path(str(path)).strip("/")
    if not normalized:
        return None
    for obj in list(scene.get_all_objects() or []):
        if _object_path(obj).strip("/") == normalized:
            return obj
    return None


def _find_component(obj, component_type: str, ordinal: int):
    matches = []
    seen_component_ids: set[int] = set()

    def _append(comp, *, promote_native: bool) -> None:
        type_name = getattr(comp, "type_name", type(comp).__name__)
        if type_name != component_type and type(comp).__name__ != component_type:
            return
        try:
            from Infernux.components import BuiltinComponent

            if promote_native and not isinstance(comp, BuiltinComponent):
                wrapper_cls = BuiltinComponent._builtin_registry.get(type_name)
                if wrapper_cls is not None:
                    comp = wrapper_cls._get_or_create_wrapper(comp, obj)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            # Some native-only components intentionally have no public wrapper.
            # Keep those available through their pybind surface.
            pass
        component_id = int(getattr(comp, "component_id", 0) or 0)
        if component_id and component_id in seen_component_ids:
            return
        if component_id:
            seen_component_ids.add(component_id)
        matches.append(comp)

    # Built-in Python wrappers expose the supported public API surface (for
    # example MeshRenderer.material_guid) while sharing the native component
    # identity. Prefer them over the raw pybind object and only fall back to
    # native components when no wrapper exists.
    try:
        for comp in obj.get_py_components() or []:
            _append(comp, promote_native=False)
    except Exception:
        pass
    try:
        for comp in obj.get_components() or []:
            _append(comp, promote_native=True)
    except Exception:
        pass
    if ordinal < 0 or ordinal >= len(matches):
        return None
    return matches[ordinal]


def _component_snapshot(obj, comp) -> dict[str, Any]:
    fields = {}
    try:
        from Infernux.components.fields import FieldType, get_serialized_fields
        from Infernux.components.value_codec import VALUE_CODECS
        from Infernux.components.value_document import make_enum

        for name, metadata in get_serialized_fields(type(comp)).items():
            try:
                metadata = _resolved_component_field_metadata(metadata)
                value = getattr(comp, name)
                if metadata.field_type == FieldType.ENUM:
                    enum_type = metadata.enum_type
                    member_name = str(getattr(value, "name", "") or "")
                    if enum_type is None:
                        raise TypeError(f"{type(comp).__name__}.{name} has no enum type")
                    if member_name not in enum_type.__members__:
                        member_name = enum_type(int(value)).name
                    fields[name] = make_enum(enum_type.__qualname__, member_name)
                    continue
                fields[name] = VALUE_CODECS.encode(
                    value,
                    f"{type(comp).__name__}.{name}",
                )
            except Exception:
                pass
    except Exception:
        pass
    if _is_material_slot_component(comp):
        fields["material"] = _material_slot_document(comp, 0)
        fields["materials"] = [
            _material_slot_document(comp, index)
            for index, _guid in enumerate(_material_slot_guids(comp, all_slots=True))
        ]
    return {
        "object_id": int(obj.id),
        "object_name": str(obj.name),
        "component": serialize_component(comp),
        "fields": fields,
    }


def _coerce_property_value(field: str, value: Any, current_value: Any = None) -> Any:
    if isinstance(value, dict) and {"x", "y", "z"}.issubset(value.keys()):
        return coerce_vector3(value)
    if isinstance(value, (list, tuple)) and current_value is not None:
        if all(hasattr(current_value, axis) for axis in ("x", "y", "z")):
            if hasattr(current_value, "w") and len(value) >= 4:
                try:
                    return type(current_value)(*(float(item) for item in value[:4]))
                except (TypeError, ValueError):
                    pass
            if len(value) >= 3:
                return coerce_vector3(value)
        if all(hasattr(current_value, axis) for axis in ("x", "y")) and len(value) >= 2:
            try:
                return type(current_value)(float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                pass
    return value


def _component_field_value(comp, field: str) -> Any:
    from Infernux.components.fields import get_raw_field_value, get_serialized_fields

    if _is_material_slot_component(comp) and field in {"material", "materials"}:
        return _material_slot_guids(comp, all_slots=field == "materials")

    # CppProperty metadata is part of the serialized-field schema, but its
    # value is owned by the native component.  get_raw_field_value() quite
    # correctly handles Python descriptors; using it for CppProperty would
    # instead fall back to the metadata default (often None), which later
    # makes the shared component command compare Vector3 against None.
    descriptor = next(
        (cls.__dict__.get(field) for cls in type(comp).__mro__ if field in cls.__dict__),
        None,
    )
    if getattr(descriptor, "_is_cpp_property", False):
        return getattr(comp, field)

    if field in get_serialized_fields(type(comp)):
        return get_raw_field_value(comp, field)
    return getattr(comp, field)


def _is_material_slot_component_class(cls) -> bool:
    """Identify the public renderer material-slot contract by capability."""
    return callable(getattr(cls, "get_material_guids", None)) and callable(
        getattr(cls, "set_material", None)
    )


def _is_material_slot_component(comp) -> bool:
    return _is_material_slot_component_class(type(comp))


def _material_slot_guids(comp, *, all_slots: bool) -> list[str] | str:
    try:
        guids = [str(value or "") for value in comp.get_material_guids() or []]
    except Exception:
        guids = []
    if all_slots:
        return guids
    return guids[0] if guids else ""


def _material_asset_identity(value: Any, path: str) -> tuple[str, str]:
    """Resolve a material reference through the active AssetDatabase."""
    if value is None:
        return "", ""
    if isinstance(value, dict):
        if set(value) == {"$type", "asset_type", "guid", "path_hint"}:
            if value.get("$type") != "asset_ref" or value.get("asset_type") != "Material":
                raise TypeError(f"{path}: expected a Material asset reference")
        elif set(value) != {"guid", "path_hint"}:
            raise ValueError(f"{path}: material reference requires guid and path_hint")
        guid = str(value.get("guid") or "").strip()
        path_hint = str(value.get("path_hint") or "").strip()
    elif isinstance(value, str):
        token = value.strip()
        if not token:
            return "", ""
        guid = token
        path_hint = ""
        if token.lower().endswith(".mat") or "/" in token or "\\" in token:
            from Infernux.engine.project_context import get_project_root
            from Infernux.mcp.tools.common import get_asset_database

            root = get_project_root()
            resolved = resolve_asset_path(root, token)
            if not resolved.lower().endswith(".mat"):
                raise TypeError(f"{path}: expected a .mat material asset")
            database = get_asset_database()
            guid = str(database.get_guid_from_path(resolved) or "") if database else ""
            path_hint = _project_rel(resolved)
        elif len(token) < 8:
            raise ValueError(
                f"{path}: material reference must be a GUID or Assets-relative .mat path"
            )
    else:
        guid = str(getattr(value, "guid", "") or "").strip()
        path_hint = str(getattr(value, "path_hint", "") or "").strip()

    if not guid:
        raise ValueError(f"{path}: material reference has no GUID")

    from Infernux.engine.project_context import get_project_root
    from Infernux.mcp.tools.common import get_asset_database

    database = get_asset_database()
    database_path = str(database.get_path_from_guid(guid) or "") if database else ""
    if database is not None:
        if database_path:
            effective_path = resolved_path(database_path)
        elif path_hint:
            effective_path = resolve_asset_path(get_project_root(), path_hint)
            resolved_guid = str(database.get_guid_from_path(effective_path) or "")
            if resolved_guid != guid:
                raise ValueError(
                    f"{path}: material GUID does not match the registered asset path"
                )
        else:
            raise ValueError(f"{path}: material GUID is not registered in AssetDatabase")
    elif path_hint:
        effective_path = resolve_asset_path(get_project_root(), path_hint)
    else:
        effective_path = ""
    if effective_path:
        assets = resolve_asset_path(get_project_root(), "")
        if not is_path_within(effective_path, assets):
            raise ValueError(f"{path}: material must resolve under Assets/")
        if not effective_path.lower().endswith(".mat"):
            raise TypeError(f"{path}: expected a .mat material asset")
        path_hint = _project_rel(effective_path)
    return guid, path_hint


def _coerce_material_slot_reference(value: Any, path: str) -> str | None:
    guid, _path_hint = _material_asset_identity(value, path)
    return guid or None


def _material_slot_document(comp, index: int) -> Any:
    guids = _material_slot_guids(comp, all_slots=True)
    if not isinstance(guids, list) or index < 0 or index >= len(guids):
        return None
    guid = str(guids[index] or "")
    if not guid:
        return None
    from Infernux.mcp.tools.common import get_asset_database

    database = get_asset_database()
    path_hint = str(database.get_path_from_guid(guid) or "") if database else ""
    return {
        "$type": "asset_ref",
        "asset_type": "Material",
        "guid": guid,
        "path_hint": _project_rel(path_hint) if path_hint else "",
    }


def _serialize_component_field_value(comp, field: str) -> Any:
    if _is_material_slot_component(comp) and field == "material":
        return _material_slot_document(comp, 0)
    if _is_material_slot_component(comp) and field == "materials":
        return [
            _material_slot_document(comp, index)
            for index, _guid in enumerate(_material_slot_guids(comp, all_slots=True))
        ]
    return serialize_value(getattr(comp, field))


def _coerce_component_property_value(
    comp,
    field: str,
    value: Any,
    current_value: Any = None,
) -> Any:
    from Infernux.components.fields import (
        coerce_serialized_field_input,
        get_serialized_fields,
    )

    if _is_material_slot_component(comp) and field in {"material", "materials"}:
        if field == "materials":
            if not isinstance(value, list):
                raise TypeError(f"{type(comp).__name__}.materials requires an array")
            return [
                _coerce_material_slot_reference(item, f"{type(comp).__name__}.materials[{index}]")
                for index, item in enumerate(value)
            ]
        return _coerce_material_slot_reference(value, f"{type(comp).__name__}.material")

    metadata = get_serialized_fields(type(comp)).get(field)
    if metadata is not None:
        metadata = _resolved_component_field_metadata(metadata)
        if metadata.readonly:
            raise AttributeError(f"{type(comp).__name__}.{field} is readonly")
        value = _coerce_property_value(field, value, current_value)
        return coerce_serialized_field_input(
            value,
            metadata,
            f"{type(comp).__name__}.{field}",
        )
    return _coerce_property_value(field, value, current_value)


def _resolved_component_field_metadata(metadata):
    """Resolve lazy built-in enum names to their public pybind enum class."""
    from dataclasses import replace

    from Infernux.components.fields import FieldType

    if metadata.field_type != FieldType.ENUM or not isinstance(metadata.enum_type, str):
        return metadata
    from Infernux import lib

    enum_type = getattr(lib, metadata.enum_type, None)
    if enum_type is None or not getattr(enum_type, "__members__", None):
        raise TypeError(f"Unknown built-in enum type: {metadata.enum_type}")
    return replace(metadata, enum_type=enum_type)


def _is_python_script_component(comp) -> bool:
    if getattr(comp, "_cpp_type_name", ""):
        return False
    script_guid = getattr(comp, "_script_guid", None)
    if script_guid:
        return True
    try:
        from Infernux.components.component import InxComponent
        return isinstance(comp, InxComponent) and not getattr(comp, "_cpp_type_name", "")
    except Exception:
        return False


def _is_builtin_component_class(cls) -> bool:
    return bool(getattr(cls, "_cpp_type_name", ""))


def _is_builtin_family_class(cls) -> bool:
    module_name = str(getattr(cls, "__module__", ""))
    return module_name.startswith("Infernux.components.builtin") or bool(getattr(cls, "_cpp_type_name", ""))


def _component_class_for_name(component_type: str):
    import Infernux.components.builtin  # noqa: F401 - ensure built-ins register
    from Infernux.components.builtin_component import BuiltinComponent
    from Infernux.components.registry import get_type

    if component_type in BuiltinComponent._builtin_registry:
        return BuiltinComponent._builtin_registry[component_type]
    for name, cls in BuiltinComponent._builtin_registry.items():
        if cls.__name__ == component_type or name.lower() == component_type.lower():
            return cls
    return get_type(component_type)


def _component_type_entry(name: str, cls, *, builtin: bool, include_fields: bool = False) -> dict[str, Any]:
    type_name = getattr(cls, "_cpp_type_name", "") or getattr(cls, "__name__", name)
    entry = {
        "type": str(type_name),
        "class_name": getattr(cls, "__name__", str(name)),
        "category": str(getattr(cls, "_component_category_", "")),
        "builtin": bool(builtin),
    }
    if include_fields:
        entry["fields"] = _component_field_schema(cls)
    return entry


def _component_field_schema(cls) -> list[dict[str, Any]]:
    from Infernux.components.fields import get_serialized_fields
    from Infernux.components.fields import FieldType

    fields = []
    for name, meta in get_serialized_fields(cls).items():
        meta = _resolved_component_field_metadata(meta)
        enum_values = []
        enum_type = getattr(meta, "enum_type", None)
        if enum_type is not None:
            try:
                # pybind11 enums expose ``__members__`` but are not
                # themselves iterable like Python's Enum class.  Reading the
                # registered members also preserves the native declaration
                # order for the editor and MCP clients.
                members = getattr(enum_type, "__members__", {}) or {}
                labels = list(getattr(meta, "enum_labels", ()) or ())
                enum_values = [
                    {
                        "name": str(name),
                        "value": int(member.value),
                        "label": labels[index] if index < len(labels) else str(name),
                    }
                    for index, (name, member) in enumerate(members.items())
                ]
            except Exception:
                enum_values = []
        fields.append({
            "name": name,
            "type": getattr(getattr(meta, "field_type", None), "name", str(getattr(meta, "field_type", ""))),
            "default": serialize_value(getattr(meta, "default", None)),
            "range": serialize_value(getattr(meta, "range", None)),
            "tooltip": str(getattr(meta, "tooltip", "")),
            "readonly": bool(getattr(meta, "readonly", False)),
            "enum_values": enum_values,
            "enum_type": (
                str(getattr(enum_type, "__name__", "") or getattr(enum_type, "__qualname__", ""))
                if enum_type is not None
                else ""
            ),
            "asset_type": str(getattr(meta, "asset_type", "") or ""),
            "component_type": str(getattr(meta, "component_type", "") or ""),
            "writable": not bool(getattr(meta, "readonly", False)),
        })
    if _is_material_slot_component_class(cls):
        fields.extend((
            {
                "name": "material",
                "type": FieldType.MATERIAL.name,
                "default": None,
                "range": None,
                "tooltip": "Material assigned to slot 0",
                "readonly": False,
                "writable": True,
                "enum_values": [],
                "asset_type": "Material",
                "component_type": "",
                "slot": 0,
            },
            {
                "name": "materials",
                "type": FieldType.LIST.name,
                "element_type": FieldType.MATERIAL.name,
                "default": [],
                "range": None,
                "tooltip": "Materials assigned to all renderer slots",
                "readonly": False,
                "writable": True,
                "enum_values": [],
                "asset_type": "Material",
                "component_type": "",
            },
        ))
    return fields


def _require_component_knowledge(component_type: str, token: str) -> None:
    type_name = str(component_type or "")
    component_class = _component_class_for_name(type_name)
    category = str(getattr(component_class, "_component_category_", "") or "")
    module_name = str(getattr(component_class, "__module__", "") or "")
    if category.casefold() == "audio":
        require_knowledge_token("audio", token, required_tool="audio_guide")
    elif category.casefold() == "ui" or module_name.startswith("Infernux.ui"):
        require_knowledge_token("ui", token, required_tool="api_get")


def _component_descendants(cls):
    for child in cls.__subclasses__():
        yield child
        yield from _component_descendants(child)


def _require_creatable_component_type(component_type: str):
    """Return a type only when its registration describes a creatable type."""
    cls = _component_class_for_name(component_type)
    if cls is None:
        raise FileNotFoundError(f"Component type '{component_type}' was not found.")

    import inspect as _inspect
    from Infernux.components.registry import get_component_constraints

    registration = get_component_constraints(cls)
    explicitly_abstract = bool(
        getattr(cls, "_component_abstract_", False)
        or getattr(cls, "__abstractmethods__", frozenset())
        or _inspect.isabstract(cls)
    )
    menu_path = str(getattr(cls, "_component_menu_path_", "") or "")
    has_concrete_menu_descendant = any(
        bool(str(getattr(child, "_component_menu_path_", "") or ""))
        and not bool(getattr(child, "_component_abstract_", False))
        for child in _component_descendants(cls)
    )
    if (
        explicitly_abstract
        or not bool(registration.user_addable)
        or bool(registration.intrinsic)
        or (not menu_path and has_concrete_menu_descendant)
    ):
        raise TypeError(
            f"Component type '{component_type}' is abstract or not user-creatable. "
            "Attach a concrete registered component type instead."
        )
    return cls


def _matches_scene_query(obj, criteria: dict[str, Any]) -> bool:
    criteria = criteria or {}
    name = str(getattr(obj, "name", ""))
    path = _object_path(obj)
    name_l = name.lower()
    path_l = path.lower()
    if criteria.get("name_exact") and str(criteria["name_exact"]).lower() != name_l:
        return False
    if criteria.get("name") and str(criteria["name"]).lower() not in name_l:
        return False
    if criteria.get("name_contains") and str(criteria["name_contains"]).lower() not in name_l:
        return False
    if criteria.get("path_exact") and str(criteria["path_exact"]).strip("/").lower() != path.strip("/").lower():
        return False
    if criteria.get("path") and str(criteria["path"]).lower() not in path_l:
        return False
    if criteria.get("path_contains") and str(criteria["path_contains"]).lower() not in path_l:
        return False
    if criteria.get("tag") and str(getattr(obj, "tag", "")) != str(criteria["tag"]):
        return False
    if criteria.get("layer") is not None and int(getattr(obj, "layer", -1)) != int(criteria["layer"]):
        return False
    if criteria.get("active") is not None and bool(getattr(obj, "active", True)) != bool(criteria["active"]):
        return False

    names = set(_component_names(obj))
    component_type = str(criteria.get("component_type", "") or "")
    if component_type and component_type not in names:
        return False
    any_components = {str(item) for item in criteria.get("component_any", []) or []}
    if any_components and not names.intersection(any_components):
        return False
    all_components = {str(item) for item in criteria.get("component_all", []) or []}
    if all_components and not all_components.issubset(names):
        return False
    excluded = {str(item) for item in criteria.get("exclude_component_any", []) or []}
    if excluded and names.intersection(excluded):
        return False
    return True


def _empty_query_result(criteria: dict[str, Any], scene) -> dict[str, Any]:
    return {
        "reason": "no_objects_matched_query",
        "stop_repeating": True,
        "message": "No GameObjects matched this query. Do not repeat the same query; broaden filters or inspect scene.query.summary.",
        "query_semantics": {
            "name": "contains match, case-insensitive",
            "name_contains": "contains match, case-insensitive",
            "name_exact": "exact match, case-insensitive",
            "path": "contains match, case-insensitive",
            "path_contains": "contains match, case-insensitive",
            "path_exact": "exact hierarchy path, case-insensitive",
        },
        "scene_object_count": len(list(scene.get_all_objects() or [])),
        "recommended_next_tools": [
            {"tool": "scene_query_summary", "arguments": {"include_subjects": True, "subject_limit": 8}},
            {"tool": "scene_query_objects", "arguments": {"query": {}, "limit": 20}},
        ],
    }


def _empty_subject_result(objects: list[Any], criteria: dict[str, Any]) -> dict[str, Any]:
    groups = _group_scene_objects(objects)
    service_count = len(groups.get("cameras", [])) + len(groups.get("lights", []))
    reason = "only_service_objects_present" if objects and service_count == len(objects) else "no_subject_candidates"
    return {
        "reason": reason,
        "stop_repeating": True,
        "message": (
            "No likely camera/gameplay subjects were found. Do not repeat scene_query_subjects with the same query. "
            "If this is a new/bootstrap scene, create or locate a renderable/gameplay object first."
        ),
        "scene_contains_only": {
            "cameras": groups.get("cameras", []),
            "lights": groups.get("lights", []),
            "renderers": groups.get("renderers", []),
            "ui": groups.get("ui", []),
            "scripts": groups.get("scripts", []),
        },
        "recommended_next_tools": [
            {"tool": "scene_query_summary", "arguments": {"include_subjects": True, "subject_limit": 8}},
            {"tool": "hierarchy_create_object", "arguments": {"kind": "primitive.cube", "name": "Subject"}},
            {"tool": "camera_find_main", "arguments": {}},
        ],
    }


def _group_scene_objects(objects: list[Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "cameras": [],
        "lights": [],
        "renderers": [],
        "ui": [],
        "scripts": [],
        "renderstacks": [],
        "generated_roots": [],
    }
    for obj in objects:
        names = set(_component_names(obj))
        entry = _object_index_entry(obj, names)
        if "Camera" in names:
            groups["cameras"].append(entry)
        if "Light" in names:
            groups["lights"].append(entry)
        if names.intersection({"MeshRenderer", "SpriteRenderer", "SkinnedMeshRenderer"}):
            groups["renderers"].append(entry)
        if names.intersection({"UICanvas", "UIText", "UIButton", "UIImage"}):
            groups["ui"].append(entry)
        if "RenderStack" in names:
            groups["renderstacks"].append(entry)
        if any(name not in {"Transform", "Camera", "Light", "MeshRenderer", "SpriteRenderer", "SkinnedMeshRenderer", "UICanvas", "UIText", "UIButton", "UIImage", "RenderStack"} for name in names):
            groups["scripts"].append(entry)
        if str(getattr(obj, "name", "")).lower().startswith(("mcp", "generated", "runtime")):
            parent = None
            try:
                parent = obj.get_parent()
            except Exception:
                parent = None
            if parent is None:
                groups["generated_roots"].append(entry)
    return groups


def _rank_subjects(objects: list[Any], limit: int) -> list[dict[str, Any]]:
    scored = []
    for obj in objects:
        score, reasons = _subject_score(obj)
        if score <= 0:
            continue
        names = _component_names(obj)
        scored.append((
            score,
            {
                **_object_index_entry(obj, set(names)),
                "score": score,
                "reasons": reasons,
                "spatial": _spatial_summary(obj),
            },
        ))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("path", ""))))
    return [item for _score, item in scored[: max(int(limit), 1)]]


def _subject_score(obj) -> tuple[int, list[str]]:
    names = set(_component_names(obj))
    object_name = str(getattr(obj, "name", ""))
    lower = object_name.lower()
    score = 0
    reasons: list[str] = []
    render_components = {"MeshRenderer", "SpriteRenderer", "SkinnedMeshRenderer"}
    ignored_components = {"Camera", "Light", "UICanvas", "UIText", "UIButton", "UIImage", "RenderStack"}
    if names.intersection(render_components):
        score += 6
        reasons.append("has_renderer")
    if names and not names.issubset(ignored_components | {"Transform"}):
        score += 2
        reasons.append("has_gameplay_or_custom_component")
    for token, weight in {
        "player": 5,
        "hero": 5,
        "main": 4,
        "subject": 4,
        "target": 4,
        "board": 3,
        "root": 2,
        "piece": 2,
        "ball": 2,
    }.items():
        if token in lower:
            score += weight
            reasons.append(f"name_contains:{token}")
    if names.intersection({"Camera", "Light"}) and not names.intersection(render_components):
        score -= 8
        reasons.append("non_subject_service_object")
    return score, reasons


def _object_index_entry(obj, component_names: set[str] | None = None) -> dict[str, Any]:
    return {
        "id": int(obj.id),
        "name": str(obj.name),
        "path": _object_path(obj),
        "active": bool(getattr(obj, "active", True)),
        "components": sorted(component_names if component_names is not None else _component_names(obj)),
    }


def _component_names(obj) -> list[str]:
    names: list[str] = []
    try:
        for comp in obj.get_components() or []:
            names.append(str(getattr(comp, "type_name", type(comp).__name__)))
    except Exception:
        pass
    try:
        for comp in obj.get_py_components() or []:
            type_name = str(getattr(comp, "type_name", type(comp).__name__))
            if type_name not in names:
                names.append(type_name)
    except Exception:
        pass
    return names


def _spatial_summary(obj, *, include_descendants: bool = True) -> dict[str, Any]:
    points = []
    for item in _object_and_descendants(obj) if include_descendants else [obj]:
        points.extend(_approx_object_points(item))
    if not points:
        points = [_transform_position(obj)]
    min_v = [min(point[i] for point in points) for i in range(3)]
    max_v = [max(point[i] for point in points) for i in range(3)]
    center = [(min_v[i] + max_v[i]) * 0.5 for i in range(3)]
    size = [max_v[i] - min_v[i] for i in range(3)]
    return {
        "transform": _transform_snapshot(obj),
        "bounds": {"min": min_v, "max": max_v, "center": center, "size": size},
    }


def _object_and_descendants(obj) -> list[Any]:
    result = [obj]
    try:
        children = list(obj.get_children() or [])
    except Exception:
        children = []
    for child in children:
        result.extend(_object_and_descendants(child))
    return result


def _approx_object_points(obj) -> list[list[float]]:
    pos = _transform_position(obj)
    scale = _transform_scale(obj)
    half = [max(abs(scale[i]) * 0.5, 0.05) for i in range(3)]
    names = set(_component_names(obj))
    if not names.intersection({"MeshRenderer", "SpriteRenderer", "SkinnedMeshRenderer"}):
        half = [0.05, 0.05, 0.05]
    return [
        [pos[0] - half[0], pos[1] - half[1], pos[2] - half[2]],
        [pos[0] + half[0], pos[1] + half[1], pos[2] + half[2]],
    ]


def _transform_snapshot(obj) -> dict[str, Any]:
    trans = getattr(obj, "transform", None)
    if trans is None:
        return {}
    return {
        "position": _vector_list(getattr(trans, "position", None)),
        "local_position": _vector_list(getattr(trans, "local_position", None)),
        "euler_angles": _vector_list(getattr(trans, "euler_angles", None)),
        "local_euler_angles": _vector_list(getattr(trans, "local_euler_angles", None)),
        "local_scale": _vector_list(getattr(trans, "local_scale", None)),
    }


def _transform_position(obj) -> list[float]:
    trans = getattr(obj, "transform", None)
    return _vector_list(getattr(trans, "position", None)) if trans is not None else [0.0, 0.0, 0.0]


def _transform_scale(obj) -> list[float]:
    trans = getattr(obj, "transform", None)
    return _vector_list(getattr(trans, "local_scale", None), default=[1.0, 1.0, 1.0]) if trans is not None else [1.0, 1.0, 1.0]


def _vector_list(value, default: list[float] | None = None) -> list[float]:
    if value is None:
        return list(default or [0.0, 0.0, 0.0])
    return [float(getattr(value, axis, 0.0)) for axis in ("x", "y", "z")]


def _project_rel(path: str | None) -> str:
    if not path:
        return ""
    try:
        from Infernux.engine.project_context import get_project_root
        root = get_project_root()
        if root:
            return relative_path(path, root, allow_root=True)
    except Exception:
        pass
    return str(path)
