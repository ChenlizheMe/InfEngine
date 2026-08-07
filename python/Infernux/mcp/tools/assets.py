"""Asset/resource creation MCP tools."""

from __future__ import annotations

import os
import json

from Infernux.engine.path_utils import relative_path, same_path
from Infernux.mcp.tools.common import (
    get_asset_database,
    ensure_not_active_scene_file,
    main_thread,
    notify_asset_changed,
    require_existing_parent_directory,
    require_external_source_edit_path,
    require_knowledge_token,
    resolve_project_dir,
    resolve_project_path,
    track_project_path_before_change,
    write_external_source_text,
)


def _project_asset_commands(project_path: str):
    from Infernux.engine.interaction import ProjectAssetCommandService

    service = ProjectAssetCommandService.instance()
    if service is None:
        raise RuntimeError("Editor Project asset command service is unavailable")
    if not service.configured:
        service.configure(project_path, get_asset_database())
    elif not same_path(service.project_root, project_path):
        raise RuntimeError("Editor Project asset commands are bound to another project")
    return service


def _resolve_asset_identity(project_path: str, path: str, guid: str) -> tuple[str, str]:
    """Resolve one project asset without treating its display path as identity."""

    token = str(guid or "").strip()
    adb = get_asset_database()
    if token:
        if adb is None:
            raise RuntimeError("AssetDatabase is not available to resolve the asset GUID.")
        file_path = str(adb.get_path_from_guid(token) or "").strip()
        if not file_path:
            raise FileNotFoundError(f"Asset GUID was not found: {token}")
        file_path = resolve_project_path(project_path, file_path)
    elif path:
        file_path = resolve_project_path(project_path, path)
        if adb is not None:
            token = str(adb.get_guid_from_path(file_path) or "").strip()
        if not token:
            from Infernux.core.asset_types import read_meta_guid

            token = read_meta_guid(file_path)
    else:
        raise ValueError("Provide path or guid.")
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Asset file not found: {path or guid}")
    return file_path, token


def _sprite_frame_contract(frame, index: int) -> dict:
    return {
        "sprite_frame_id": str(frame.stable_id),
        "index": int(index),
        "name": str(frame.name),
        "rect": {
            "x": int(frame.x),
            "y": int(frame.y),
            "width": int(frame.w),
            "height": int(frame.h),
        },
        "pivot": {"x": float(frame.pivot_x), "y": float(frame.pivot_y)},
    }


def register_asset_tools(mcp, project_path: str) -> None:
    @mcp.tool(name="asset_create_builtin_resource")
    def asset_create_builtin_resource(
        kind: str,
        name: str,
        directory: str = "Assets",
        shader_type: str = "frag",
        knowledge_token: str = "",
    ) -> dict:
        """Create a built-in resource type: folder, script, material, shader, or scene."""

        def _create():
            if str(kind or "").strip().lower() in {"shader", "material"}:
                require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
            return _create_builtin(project_path, kind, name, directory, shader_type)

        return main_thread(
            "asset_create_builtin_resource",
            _create,
            arguments={"kind": kind, "name": name, "directory": directory, "shader_type": shader_type, "knowledge_token": knowledge_token},
        )

    @mcp.tool(name="asset_ensure_folder")
    def asset_ensure_folder(path: str) -> dict:
        """Ensure a project folder exists; succeeds if it already exists."""

        def _ensure_folder():
            folder = resolve_project_path(project_path, path)
            if os.path.exists(folder) and not os.path.isdir(folder):
                raise FileExistsError(f"Path exists but is not a folder: {path}")
            existed = os.path.isdir(folder)
            if not existed:
                current = os.path.dirname(folder)
                while current and not os.path.isdir(current):
                    current = os.path.dirname(current)
                if not current:
                    raise RuntimeError(f"No existing parent for project folder: {path}")

                def _create_folder_tree():
                    os.makedirs(folder)
                    notify_asset_changed(folder, "created")
                    return True, ""

                from Infernux.engine.interaction import ActionOrigin

                _project_asset_commands(project_path).create(
                    current,
                    _create_folder_tree,
                    description="Create Folder",
                    origin=ActionOrigin.AUTOMATION,
                )
            return {
                "path": relative_path(folder, project_path),
                "created": not existed,
                "existed": existed,
            }

        return main_thread("asset_ensure_folder", _ensure_folder, arguments={"path": path})

    @mcp.tool(name="asset_create_script")
    def asset_create_script(name: str, directory: str = "Assets") -> dict:
        """Create a Python component script resource from the editor template."""
        return main_thread(
            "asset_create_script",
            lambda: _create_builtin(project_path, "script", name, directory, "frag"),
            arguments={"name": name, "directory": directory},
        )

    @mcp.tool(name="asset_create_material")
    def asset_create_material(name: str, directory: str = "Assets", knowledge_token: str = "") -> dict:
        """Create a material resource from the editor template."""
        return main_thread(
            "asset_create_material",
            lambda: (
                require_knowledge_token("shader", knowledge_token, required_tool="shader_guide")
                or _create_builtin(project_path, "material", name, directory, "frag")
            ),
            arguments={"name": name, "directory": directory, "knowledge_token": knowledge_token},
        )

    @mcp.tool(name="asset_create_particle_graph")
    def asset_create_particle_graph(name: str, directory: str = "Assets") -> dict:
        """Create and AOT-compile a ParticleGraph through the editor asset pipeline."""
        return main_thread(
            "asset_create_particle_graph",
            lambda: _create_builtin(
                project_path, "particlegraph", name, directory, "frag"
            ),
            arguments={"name": name, "directory": directory},
        )

    @mcp.tool(name="asset_list")
    def asset_list(
        directory: str = "Assets",
        recursive: bool = True,
        include_meta: bool = False,
        limit: int = 500,
    ) -> dict:
        """List files and directories under the project."""

        def _list():
            root = resolve_project_path(project_path, directory or "Assets")
            entries = []
            max_count = max(int(limit), 1)
            adb = get_asset_database()

            def _entry(path: str) -> dict:
                rel = relative_path(path, project_path)
                data = {
                    "path": rel,
                    "name": os.path.basename(path),
                    "directory": os.path.isdir(path),
                }
                if include_meta and adb and os.path.isfile(path):
                    try:
                        data["guid"] = adb.get_guid_from_path(path) or ""
                    except Exception:
                        data["guid"] = ""
                return data

            if recursive:
                for base, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
                    for name in sorted(dirs + files):
                        entries.append(_entry(os.path.join(base, name)))
                        if len(entries) >= max_count:
                            return {"root": relative_path(root, project_path, allow_root=True), "entries": entries}
            else:
                for name in sorted(os.listdir(root)):
                    entries.append(_entry(os.path.join(root, name)))
                    if len(entries) >= max_count:
                        break
            return {"root": relative_path(root, project_path, allow_root=True), "entries": entries}

        return main_thread("asset_list", _list, arguments={"directory": directory, "recursive": recursive, "include_meta": include_meta, "limit": limit})

    @mcp.tool(name="asset_search")
    def asset_search(query: str, directory: str = "Assets", extensions: list[str] | None = None, limit: int = 100) -> dict:
        """Search asset paths by filename substring and optional extensions."""

        def _search():
            root = resolve_project_path(project_path, directory or "Assets")
            needle = (query or "").lower()
            exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in (extensions or [])}
            matches = []
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
                for name in files:
                    if needle and needle not in name.lower():
                        continue
                    if exts and os.path.splitext(name)[1].lower() not in exts:
                        continue
                    path = os.path.join(base, name)
                    matches.append(relative_path(path, project_path))
                    if len(matches) >= max(int(limit), 1):
                        return {"matches": matches}
            return {"matches": matches}

        return main_thread("asset_search", _search, arguments={"query": query, "directory": directory, "extensions": extensions or [], "limit": limit})

    @mcp.tool(name="asset_read_text")
    def asset_read_text(path: str, max_bytes: int = 262144) -> dict:
        """Read a UTF-8 text file inside the project."""

        def _read():
            file_path = resolve_project_path(project_path, path)
            if not os.path.isfile(file_path):
                raise FileNotFoundError(f"File not found: {path}")
            size = os.path.getsize(file_path)
            if size > max(int(max_bytes), 1):
                raise ValueError(f"File is too large ({size} bytes).")
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            return {
                "path": relative_path(file_path, project_path),
                "size": size,
                "text": text,
            }

        return main_thread("asset_read_text", _read)

    @mcp.tool(name="asset_write_text")
    def asset_write_text(path: str, text: str, overwrite: bool = True) -> dict:
        """Write a UTF-8 text file inside the project and notify AssetDatabase."""

        def _write():
            return write_external_source_text(
                project_path,
                path,
                text or "",
                overwrite=overwrite,
            )

        return main_thread("asset_write_text", _write, arguments={"path": path, "text_bytes": len((text or "").encode("utf-8")), "overwrite": overwrite})

    @mcp.tool(name="asset_edit_text")
    def asset_edit_text(path: str, old_text: str, new_text: str, count: int = 1) -> dict:
        """Replace text in a UTF-8 file inside the project."""

        def _edit():
            file_path = require_external_source_edit_path(project_path, path, "edit")
            if not os.path.isfile(file_path):
                raise FileNotFoundError(f"File not found: {path}")
            with open(file_path, "r", encoding="utf-8") as f:
                original = f.read()
            occurrences = original.count(old_text)
            if occurrences == 0:
                raise ValueError("old_text was not found.")
            replace_count = -1 if int(count) <= 0 else int(count)
            updated = original.replace(old_text, new_text, replace_count)
            track_project_path_before_change(project_path, file_path, "edit_text")
            from Infernux.core.document_store import write_document_text
            write_document_text(file_path, updated)
            notify_asset_changed(file_path, "modified")
            return {
                "path": relative_path(file_path, project_path),
                "occurrences": occurrences,
                "replaced": occurrences if replace_count < 0 else min(occurrences, replace_count),
                "bytes": os.path.getsize(file_path),
            }

        return main_thread("asset_edit_text", _edit)

    @mcp.tool(name="asset_delete")
    def asset_delete(path: str) -> dict:
        """Delete a file or directory inside the project."""

        def _delete():
            target = resolve_project_path(project_path, path)
            if same_path(target, project_path):
                raise ValueError("Refusing to delete the project root.")
            if not os.path.exists(target):
                raise FileNotFoundError(f"Path not found: {path}")
            is_dir = os.path.isdir(target)
            ensure_not_active_scene_file(project_path, target, "delete")
            from Infernux.engine.interaction import ActionOrigin

            _project_asset_commands(project_path).delete(
                (target,),
                origin=ActionOrigin.AUTOMATION,
            )
            return {"deleted": True, "path": relative_path(target, project_path), "directory": is_dir}

        return main_thread("asset_delete", _delete)

    @mcp.tool(name="asset_refresh")
    def asset_refresh() -> dict:
        """Refresh the AssetDatabase."""

        def _refresh():
            adb = get_asset_database()
            if adb is None:
                raise RuntimeError("AssetDatabase is not available.")
            adb.refresh()
            return {"refreshed": True}

        return main_thread("asset_refresh", _refresh)

    @mcp.tool(name="asset_resolve")
    def asset_resolve(path: str = "", guid: str = "") -> dict:
        """Resolve between asset path and GUID."""

        def _resolve():
            adb = get_asset_database()
            if adb is None:
                raise RuntimeError("AssetDatabase is not available.")
            resolved_path = ""
            resolved_guid = ""
            if guid:
                resolved_path = adb.get_path_from_guid(guid) or ""
                resolved_guid = guid
            elif path:
                file_path = resolve_project_path(project_path, path)
                resolved_guid = adb.get_guid_from_path(file_path) or ""
                resolved_path = file_path
            else:
                raise ValueError("Provide path or guid.")
            return {
                "path": relative_path(resolved_path, project_path) if resolved_path else "",
                "guid": resolved_guid,
            }

        return main_thread("asset_resolve", _resolve)

    @mcp.tool(name="asset_import")
    def asset_import(path: str) -> dict:
        """Import or re-import one asset path."""

        def _import():
            file_path = resolve_project_path(project_path, path)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Path not found: {path}")
            notify_asset_changed(file_path, "modified")
            return {"path": relative_path(file_path, project_path), "imported": True}

        return main_thread("asset_import", _import)

    @mcp.tool(name="asset_move")
    def asset_move(path: str, new_path: str, overwrite: bool = False) -> dict:
        """Move a file or directory inside the project."""

        def _move():
            src = resolve_project_path(project_path, path)
            dst = resolve_project_path(project_path, new_path)
            if not os.path.exists(src):
                raise FileNotFoundError(f"Path not found: {path}")
            ensure_not_active_scene_file(project_path, dst, "move to")
            if os.path.exists(dst):
                if not overwrite:
                    raise FileExistsError(f"Destination already exists: {new_path}")
                ensure_not_active_scene_file(project_path, dst, "overwrite")
            ensure_not_active_scene_file(project_path, src, "move")
            from Infernux.engine.interaction import ActionOrigin

            dst = _project_asset_commands(project_path).move(
                src,
                dst,
                overwrite=overwrite,
                origin=ActionOrigin.AUTOMATION,
            )
            return {
                "old_path": relative_path(src, project_path),
                "path": relative_path(dst, project_path),
            }

        return main_thread("asset_move", _move)

    @mcp.tool(name="asset_rename")
    def asset_rename(path: str, new_name: str) -> dict:
        """Rename a file or directory in place."""

        def _rename():
            src = resolve_project_path(project_path, path)
            if not os.path.exists(src):
                raise FileNotFoundError(f"Path not found: {path}")
            dst_hint = os.path.join(os.path.dirname(src), new_name)
            ensure_not_active_scene_file(project_path, src, "rename")
            ensure_not_active_scene_file(project_path, dst_hint, "rename to")
            from Infernux.engine.interaction import ActionOrigin

            dst = _project_asset_commands(project_path).rename(
                src,
                new_name,
                origin=ActionOrigin.AUTOMATION,
            )
            return {"path": relative_path(dst, project_path)}

        return main_thread("asset_rename", _rename)

    @mcp.tool(name="asset_copy")
    def asset_copy(path: str, new_path: str, overwrite: bool = False) -> dict:
        """Copy a file or directory inside the project."""

        def _copy():
            src = resolve_project_path(project_path, path)
            dst = resolve_project_path(project_path, new_path)
            if not os.path.exists(src):
                raise FileNotFoundError(f"Path not found: {path}")
            ensure_not_active_scene_file(project_path, src, "copy")
            ensure_not_active_scene_file(project_path, dst, "copy to")
            if os.path.exists(dst):
                if not overwrite:
                    raise FileExistsError(f"Destination already exists: {new_path}")
                ensure_not_active_scene_file(project_path, dst, "overwrite")
            from Infernux.engine.interaction import ActionOrigin

            dst = _project_asset_commands(project_path).copy(
                src,
                dst,
                overwrite=overwrite,
                origin=ActionOrigin.AUTOMATION,
            )
            return {
                "source": relative_path(src, project_path),
                "path": relative_path(dst, project_path),
            }

        return main_thread("asset_copy", _copy)

    @mcp.tool(name="asset_get_meta")
    def asset_get_meta(path: str = "", guid: str = "") -> dict:
        """Return AssetDatabase metadata when available."""

        def _meta():
            adb = get_asset_database()
            if adb is None:
                raise RuntimeError("AssetDatabase is not available.")
            meta = None
            if guid:
                meta = adb.get_meta_by_guid(guid)
            elif path:
                meta = adb.get_meta_by_path(resolve_project_path(project_path, path))
            else:
                raise ValueError("Provide path or guid.")
            if meta is None:
                raise FileNotFoundError("Asset metadata not found.")
            return {
                "guid": str(getattr(meta, "guid", guid or "")),
                "path": str(getattr(meta, "path", path or "")),
                "type": str(getattr(getattr(meta, "type", None), "name", getattr(meta, "type", ""))),
            }

        return main_thread("asset_get_meta", _meta)

    @mcp.tool(name="asset_list_sprite_frames")
    def asset_list_sprite_frames(path: str = "", guid: str = "") -> dict:
        """List SpriteFrame subresources by stable sprite_frame_id.

        ``index`` and ``name`` are display metadata only. Pass
        ``sprite_frame_id`` to any operation that targets a sprite subresource.
        """

        def _list_sprite_frames():
            from Infernux.core.asset_types import TextureType, read_texture_import_settings

            file_path, asset_guid = _resolve_asset_identity(project_path, path, guid)
            settings = read_texture_import_settings(file_path)
            if settings.texture_type is not TextureType.SPRITE:
                raise ValueError(
                    "Asset is not imported as a Sprite texture and has no SpriteFrame subresources."
                )
            frames = [
                _sprite_frame_contract(frame, index)
                for index, frame in enumerate(settings.sprite_frames)
            ]
            return {
                "guid": asset_guid,
                "path": relative_path(file_path, project_path),
                "identity_field": "sprite_frame_id",
                "frame_count": len(frames),
                "frames": frames,
            }

        return main_thread(
            "asset_list_sprite_frames",
            _list_sprite_frames,
            arguments={"path": path, "guid": guid},
        )

    @mcp.tool(name="asset_inspect_animation_clip_2d")
    def asset_inspect_animation_clip_2d(path: str = "", guid: str = "") -> dict:
        """Inspect AnimationClip2D stable frame references and missing subresources."""

        def _inspect_animation_clip_2d():
            from Infernux.core.animation_clip import AnimationClip
            from Infernux.core.asset_types import TextureType, read_texture_import_settings

            file_path, asset_guid = _resolve_asset_identity(project_path, path, guid)
            if os.path.splitext(file_path)[1].lower() != ".animclip2d":
                raise ValueError("Asset is not an AnimationClip2D (.animclip2d).")
            with open(file_path, "r", encoding="utf-8") as stream:
                clip = AnimationClip.from_dict(json.load(stream))

            diagnostics: list[dict] = []
            adb = get_asset_database()
            source_path = ""
            source_guid = str(clip.authoring_texture_guid or "").strip()
            source_hint = str(clip.authoring_texture_path or "").strip()
            if source_guid and adb is not None:
                source_path = str(adb.get_path_from_guid(source_guid) or "").strip()
                if not source_path:
                    diagnostics.append({
                        "severity": "warning",
                        "code": "source_texture_guid_unresolved",
                        "message": f"Authoring texture GUID is not registered: {source_guid}",
                        "texture_guid": source_guid,
                    })
            if not source_path and source_hint:
                try:
                    source_path = resolve_project_path(project_path, source_hint)
                except ValueError as exc:
                    diagnostics.append({
                        "severity": "error",
                        "code": "source_texture_path_invalid",
                        "message": str(exc),
                        "path_hint": source_hint,
                    })
            if source_path:
                source_path = resolve_project_path(project_path, source_path)

            source_frames: list[dict] = []
            if clip.frames and (not source_path or not os.path.isfile(source_path)):
                diagnostics.append({
                    "severity": "error",
                    "code": "source_texture_missing",
                    "message": "AnimationClip2D frames require an existing authoring Sprite texture.",
                    "texture_guid": source_guid,
                    "path_hint": source_hint,
                })
            elif source_path and os.path.isfile(source_path):
                try:
                    settings = read_texture_import_settings(source_path)
                    if settings.texture_type is not TextureType.SPRITE:
                        diagnostics.append({
                            "severity": "error",
                            "code": "source_texture_not_sprite",
                            "message": "AnimationClip2D authoring texture is not imported as Sprite.",
                            "path": relative_path(source_path, project_path),
                        })
                    else:
                        source_frames = [
                            _sprite_frame_contract(frame, index)
                            for index, frame in enumerate(settings.sprite_frames)
                        ]
                except (OSError, TypeError, ValueError) as exc:
                    diagnostics.append({
                        "severity": "error",
                        "code": "source_texture_import_settings_invalid",
                        "message": str(exc),
                        "path": relative_path(source_path, project_path),
                    })

            source_by_id = {
                frame["sprite_frame_id"]: frame for frame in source_frames
            }
            sequence = []
            missing_ids = []
            for index, frame in enumerate(clip.frames):
                source = source_by_id.get(frame.sprite_frame_id)
                sequence.append({
                    "animation_frame_id": str(frame.stable_id),
                    "sprite_frame_id": str(frame.sprite_frame_id),
                    "index": index,
                    "resolved": source is not None,
                    "source_frame": source,
                })
                if source is None:
                    missing_ids.append(str(frame.sprite_frame_id))
                    diagnostics.append({
                        "severity": "error",
                        "code": "sprite_frame_missing",
                        "message": "Animation frame references a missing SpriteFrame subresource.",
                        "animation_frame_id": str(frame.stable_id),
                        "sprite_frame_id": str(frame.sprite_frame_id),
                        "index": index,
                    })

            unique_missing_ids = list(dict.fromkeys(missing_ids))
            return {
                "guid": asset_guid,
                "path": relative_path(file_path, project_path),
                "name": clip.name,
                "fps": clip.fps,
                "loop": clip.loop,
                "frame_count": len(sequence),
                "frames": sequence,
                "source_texture": {
                    "guid": source_guid,
                    "path_hint": source_hint,
                    "path": (
                        relative_path(source_path, project_path)
                        if source_path and os.path.isfile(source_path)
                        else ""
                    ),
                    "identity_field": "sprite_frame_id",
                    "frames": source_frames,
                },
                "valid": not any(item["severity"] == "error" for item in diagnostics),
                "missing_sprite_frame_ids": unique_missing_ids,
                "diagnostics": diagnostics,
            }

        return main_thread(
            "asset_inspect_animation_clip_2d",
            _inspect_animation_clip_2d,
            arguments={"path": path, "guid": guid},
        )

    @mcp.tool(name="asset_list_by_type")
    def asset_list_by_type(asset_type: str, limit: int = 500) -> dict:
        """List AssetDatabase resources by resource type name."""

        def _list_by_type():
            adb = get_asset_database()
            if adb is None:
                raise RuntimeError("AssetDatabase is not available.")
            matches = []
            for guid in adb.get_all_guids():
                path = adb.get_path_from_guid(guid)
                type_name = ""
                try:
                    type_name = getattr(adb.get_resource_type(path), "name", str(adb.get_resource_type(path)))
                except Exception:
                    pass
                if asset_type and str(type_name).lower() != str(asset_type).lower():
                    continue
                matches.append({"guid": guid, "path": relative_path(path, project_path), "type": type_name})
                if len(matches) >= max(int(limit), 1):
                    break
            return {"assets": matches}

        return main_thread("asset_list_by_type", _list_by_type)

    @mcp.tool(name="asset_read_json")
    def asset_read_json(path: str, max_bytes: int = 262144) -> dict:
        """Read and parse a JSON text asset."""

        def _read_json():
            file_path = resolve_project_path(project_path, path)
            if not os.path.isfile(file_path):
                raise FileNotFoundError(f"File not found: {path}")
            if os.path.getsize(file_path) > max(int(max_bytes), 1):
                raise ValueError("JSON file is too large.")
            with open(file_path, "r", encoding="utf-8") as f:
                return {"path": relative_path(file_path, project_path), "json": json.load(f)}

        return main_thread("asset_read_json", _read_json)

    @mcp.tool(name="asset_write_json")
    def asset_write_json(path: str, value: dict | list, overwrite: bool = True, indent: int = 2) -> dict:
        """Write a JSON text asset."""

        def _write_json():
            file_path = require_external_source_edit_path(project_path, path, "write JSON to")
            existed = os.path.exists(file_path)
            if existed and not overwrite:
                raise FileExistsError(f"File already exists: {path}")
            require_existing_parent_directory(project_path, file_path)
            track_project_path_before_change(project_path, file_path, "write_json")
            from Infernux.core.document_store import write_document_text
            write_document_text(file_path, json.dumps(value, ensure_ascii=False, indent=int(indent)) + "\n")
            notify_asset_changed(file_path, "modified" if existed else "created")
            return {"path": relative_path(file_path, project_path), "bytes": os.path.getsize(file_path), "created": not existed}

        return main_thread("asset_write_json", _write_json, arguments={"path": path, "overwrite": overwrite, "indent": indent})

    @mcp.tool(name="asset_patch_text")
    def asset_patch_text(path: str, replacements: list[dict[str, str]]) -> dict:
        """Apply a sequence of exact text replacements to a file."""

        def _patch_text():
            file_path = require_external_source_edit_path(project_path, path, "patch")
            if not os.path.isfile(file_path):
                raise FileNotFoundError(f"File not found: {path}")
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            trace = []
            for item in replacements or []:
                old = item.get("old", "")
                new = item.get("new", "")
                count = int(item.get("count", 1) or 1)
                hits = text.count(old)
                if hits <= 0:
                    raise ValueError(f"Patch text was not found: {old[:80]!r}")
                text = text.replace(old, new, -1 if count <= 0 else count)
                trace.append({"old": old[:80], "hits": hits, "replaced": hits if count <= 0 else min(hits, count)})
            track_project_path_before_change(project_path, file_path, "patch_text")
            from Infernux.core.document_store import write_document_text
            write_document_text(file_path, text)
            notify_asset_changed(file_path, "modified")
            return {"path": relative_path(file_path, project_path), "replacements": trace}

        return main_thread("asset_patch_text", _patch_text)



def _create_builtin(project_path: str, kind: str, name: str, directory: str, shader_type: str) -> dict:
    from Infernux.engine.ui import project_file_ops as ops
    from Infernux.engine.interaction import ActionOrigin

    target_dir = resolve_project_dir(project_path, directory)
    adb = get_asset_database()
    normalized = kind.strip().lower()
    description = "Create Asset"
    creator = None
    if normalized == "folder":
        path = os.path.join(target_dir, name.strip())
        if os.path.isdir(path):
            success, message = True, "Folder already exists."
            existed = True
        elif os.path.exists(path):
            rel_path = relative_path(path, project_path)
            raise FileExistsError(f"Path exists but is not a folder: {rel_path}")
        else:
            description = "Create Folder"
            creator = lambda: ops.create_folder(target_dir, name)
            existed = False
    elif normalized == "script":
        file_name = name if name.endswith(".py") else name + ".py"
        description = "Create Script"
        creator = lambda: ops.create_script(target_dir, name, adb)
        path = os.path.join(target_dir, file_name)
        existed = False
    elif normalized == "material":
        base = name[:-4] if name.endswith(".mat") else name
        description = "Create Material"
        creator = lambda: ops.create_material(target_dir, name, adb)
        path = os.path.join(target_dir, base + ".mat")
        existed = False
    elif normalized == "shader":
        base = name
        for ext in (".vert", ".frag", ".glsl"):
            if base.endswith(ext):
                base = base[: -len(ext)]
                break
        description = "Create Shader"
        creator = lambda: ops.create_shader(target_dir, name, shader_type, adb)
        path = os.path.join(target_dir, base + "." + shader_type)
        existed = False
    elif normalized == "particlegraph":
        suffix = ".particlegraph"
        base = name[: -len(suffix)] if name.lower().endswith(suffix) else name
        path = os.path.join(target_dir, base + suffix)
        description = "Create Particle Graph"
        creator = lambda: ops.create_particlegraph(target_dir, name, adb)
        existed = False
    elif normalized == "scene":
        raise ValueError("MCP agents must manage .scene files through scene_save/open/new, not asset_create_builtin_resource(kind='scene').")
    else:
        raise ValueError(
            "kind must be one of: folder, script, material, shader, particlegraph, scene"
        )

    if not existed:
        command_root = target_dir
        while not os.path.isdir(command_root):
            parent = os.path.dirname(command_root)
            if not parent or same_path(parent, command_root):
                raise FileNotFoundError(
                    f"No existing project directory can own '{relative_path(target_dir, project_path)}'"
                )
            command_root = parent
        low_level_creator = creator

        def _create_in_target_directory():
            os.makedirs(target_dir, exist_ok=True)
            return low_level_creator()

        result = _project_asset_commands(project_path).create(
            command_root,
            _create_in_target_directory,
            description=description,
            origin=ActionOrigin.AUTOMATION,
        )
        if isinstance(result, tuple):
            success = bool(result and result[0])
            message = str(result[1] if len(result) > 1 else "")
        else:
            success = bool(result)
            message = ""

    if not success:
        raise RuntimeError(message or f"Failed to create {kind}.")

    guid = ""
    if adb and path and os.path.isfile(path):
        try:
            guid = adb.get_guid_from_path(path) or ""
        except Exception:
            guid = ""
    return {
        "kind": normalized,
        "name": name,
        "path": relative_path(path, project_path) if path else "",
        "absolute_path": path,
        "guid": guid,
        "created": not existed,
        "existed": existed,
        "message": message,
    }
