"""Reversible content projections used by asset relocation transactions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import json
import os
import re
from typing import Optional

from Infernux.core.asset_types import (
    ANIMCLIP3D_EXTENSIONS,
    ANIMCLIP_EXTENSIONS,
    ANIMFSM_EXTENSIONS,
    ANIMTIMELINE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    FONT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MATERIAL_EXTENSIONS,
    MESH_EXTENSIONS,
    PARTICLE_GRAPH_EXTENSIONS,
    PHYSIC_MATERIAL_EXTENSIONS,
    PREFAB_EXTENSIONS,
    RENDER_EFFECT_EXTENSIONS,
    SHADER_EXTENSIONS,
    TIMELINEFSM_EXTENSIONS,
)
from Infernux.engine.path_utils import path_key, relative_path, resolved_path


AssetRenameTransform = Callable[[str, str, str], str]


_JSON_ASSET_EXTENSIONS = frozenset(
    {
        ".animclip2d",
        ".animclip3d",
        ".animfsm",
        ".animtimeline",
        ".effect",
        ".effectgroup",
        ".mat",
        ".particlegraph",
        ".physicmaterial",
        ".prefab",
        ".scene",
        ".timelinefsm",
    }
)
_REFERENCEABLE_EXTENSIONS = frozenset(
    {
        ".py",
        ".scene",
        *ANIMCLIP3D_EXTENSIONS,
        *ANIMCLIP_EXTENSIONS,
        *ANIMFSM_EXTENSIONS,
        *ANIMTIMELINE_EXTENSIONS,
        *AUDIO_EXTENSIONS,
        *FONT_EXTENSIONS,
        *IMAGE_EXTENSIONS,
        *MATERIAL_EXTENSIONS,
        *MESH_EXTENSIONS,
        *PARTICLE_GRAPH_EXTENSIONS,
        *PHYSIC_MATERIAL_EXTENSIONS,
        *PREFAB_EXTENSIONS,
        *RENDER_EFFECT_EXTENSIONS,
        *SHADER_EXTENSIONS,
        *TIMELINEFSM_EXTENSIONS,
    }
)


@dataclass(frozen=True, slots=True)
class AssetReferenceContentPatch:
    """One durable JSON document changed by an asset relocation."""

    source_path: str
    destination_path: str
    original: str
    updated: str


@dataclass(frozen=True, slots=True)
class _RelocatedAssetIdentity:
    guid: str
    old_path: str
    new_path: str
    old_tokens: frozenset[str]
    new_hint: str


def _portable_token(value: str) -> str:
    return str(value or "").replace("\\", "/").removeprefix("./").casefold()


def _portable_hint(path: str, project_root: str) -> str:
    if project_root:
        try:
            return relative_path(path, project_root).replace("\\", "/")
        except ValueError:
            pass
    return resolved_path(path).replace("\\", "/")


def _reference_tokens(path_hint: str, document_path: str, project_root: str) -> set[str]:
    hint = str(path_hint or "").strip()
    if not hint:
        return set()
    result = {_portable_token(hint)}
    if os.path.isabs(hint):
        result.add(path_key(hint))
        return result
    if project_root:
        result.add(path_key(os.path.join(project_root, hint)))
    if document_path:
        result.add(path_key(os.path.join(os.path.dirname(document_path), hint)))
    return result


def _rewrite_reference_nodes(
    value,
    *,
    document_path: str,
    project_root: str,
    identities: tuple[_RelocatedAssetIdentity, ...],
) -> bool:
    changed = False
    if isinstance(value, dict):
        guid = value.get("guid")
        path_hint = value.get("path_hint")
        if isinstance(guid, str) and isinstance(path_hint, str):
            identity = None
            if not guid.strip() and path_hint.strip():
                tokens = _reference_tokens(path_hint, document_path, project_root)
                matches = [item for item in identities if tokens & item.old_tokens]
                if len(matches) == 1:
                    identity = matches[0]
            if identity is not None:
                if value["guid"] != identity.guid:
                    value["guid"] = identity.guid
                    changed = True
                if value["path_hint"] != identity.new_hint:
                    value["path_hint"] = identity.new_hint
                    changed = True
        for child in value.values():
            changed = (
                _rewrite_reference_nodes(
                    child,
                    document_path=document_path,
                    project_root=project_root,
                    identities=identities,
                )
                or changed
            )
    elif isinstance(value, list):
        for child in value:
            changed = (
                _rewrite_reference_nodes(
                    child,
                    document_path=document_path,
                    project_root=project_root,
                    identities=identities,
                )
                or changed
            )
    return changed


class AssetReferenceRelocationPlanner:
    """Upgrade path-only references before an asset move commits.

    References which already carry a GUID are deliberately left untouched,
    exactly like Unity's GUID-based references.  Their path hint is display
    metadata and the current path is resolved from the database at runtime.
    """

    @staticmethod
    def _candidate_paths(
        database,
        project_root: str,
        identities: tuple[_RelocatedAssetIdentity, ...],
    ) -> tuple[str, ...]:
        paths: set[str] = set()
        try:
            from Infernux.lib import AssetDependencyGraph

            graph = AssetDependencyGraph.instance()
            get_path = getattr(database, "get_path_from_guid", None)
            if callable(get_path):
                for identity in identities:
                    for dependent_guid in graph.get_dependents(identity.guid):
                        dependent_path = str(get_path(str(dependent_guid)) or "")
                        if dependent_path:
                            paths.add(dependent_path)
        except (ImportError, RuntimeError, TypeError, ValueError):
            pass
        if not paths:
            get_paths = getattr(database, "get_all_asset_paths", None)
            if callable(get_paths):
                paths.update(str(path) for path in get_paths() if path)
        assets_root = os.path.join(project_root, "Assets") if project_root else ""
        if not paths and assets_root and os.path.isdir(assets_root):
            for directory, _subdirs, filenames in os.walk(assets_root):
                paths.update(os.path.join(directory, name) for name in filenames)
        return tuple(sorted((resolved_path(path) for path in paths), key=path_key))

    @classmethod
    def build_patches(
        cls,
        entries: Iterable[tuple[str, str, str]],
        *,
        database,
        project_root: str = "",
        source_text_patches: Mapping[str, tuple[str, str]] | None = None,
    ) -> tuple[AssetReferenceContentPatch, ...]:
        root = resolved_path(project_root) if project_root else ""
        source_overrides = {
            path_key(path): patch for path, patch in (source_text_patches or {}).items()
        }
        identities: list[_RelocatedAssetIdentity] = []
        destinations: dict[str, str] = {}
        for source, destination, guid in entries:
            old_path = resolved_path(source)
            new_path = resolved_path(destination)
            identities.append(
                _RelocatedAssetIdentity(
                    str(guid or "").strip(),
                    old_path,
                    new_path,
                    frozenset(
                        {
                            path_key(old_path),
                            _portable_token(_portable_hint(old_path, root)),
                        }
                    ),
                    _portable_hint(new_path, root),
                )
            )
            destinations[path_key(old_path)] = new_path
        usable = tuple(item for item in identities if item.guid)
        if not usable or not any(
            os.path.splitext(item.old_path)[1].casefold() in _REFERENCEABLE_EXTENSIONS
            for item in usable
        ):
            return ()

        candidates = set(cls._candidate_paths(database, root, usable))
        candidates.update(item.old_path for item in usable)
        patches: list[AssetReferenceContentPatch] = []
        for source_path in sorted(candidates, key=path_key):
            if os.path.splitext(source_path)[1].casefold() not in _JSON_ASSET_EXTENSIONS:
                continue
            if not os.path.isfile(source_path):
                continue
            with open(source_path, "r", encoding="utf-8") as stream:
                original = stream.read()
            override = source_overrides.get(path_key(source_path))
            working = override[1] if override is not None else original
            try:
                payload = json.loads(working)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not _rewrite_reference_nodes(
                payload,
                document_path=source_path,
                project_root=root,
                identities=usable,
            ):
                continue
            patches.append(
                AssetReferenceContentPatch(
                    source_path,
                    destinations.get(path_key(source_path), source_path),
                    original,
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                )
            )
        return tuple(patches)


def _rename_top_level_json_name(content: str, _source: str, destination: str) -> str:
    payload = json.loads(content)
    if not isinstance(payload, dict) or "name" not in payload:
        return content
    payload["name"] = os.path.splitext(os.path.basename(destination))[0]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _rename_single_component_class(content: str, source: str, destination: str) -> str:
    old_stem = os.path.splitext(os.path.basename(source))[0]
    new_stem = os.path.splitext(os.path.basename(destination))[0]
    if not old_stem.isidentifier() or not new_stem.isidentifier() or old_stem == new_stem:
        return content

    def pascal_case(stem: str) -> str:
        return "".join(part[:1].upper() + part[1:] for part in stem.split("_") if part)

    candidates = []
    for old_class_name, new_class_name in {
        old_stem: new_stem,
        pascal_case(old_stem): pascal_case(new_stem),
    }.items():
        pattern = re.compile(rf"^class\s+{re.escape(old_class_name)}\b", re.MULTILINE)
        candidates.extend((pattern, new_class_name) for _match in pattern.finditer(content))
    if len(candidates) != 1:
        return content
    pattern, new_class_name = candidates[0]
    return pattern.sub(f"class {new_class_name}", content, count=1)


class AssetRenameContentRegistry:
    """Own extension-specific, pure rename projections.

    Adapters only transform text. Workspace writes, rollback, catalog mutation,
    document projection, and Undo identity remain owned by the relocation
    transaction.
    """

    _instance: Optional["AssetRenameContentRegistry"] = None

    def __init__(self) -> None:
        self._adapters: dict[str, AssetRenameTransform] = {}
        self.register((".mat", ".particlegraph"), _rename_top_level_json_name)
        self.register((".py",), _rename_single_component_class)
        AssetRenameContentRegistry._instance = self

    @classmethod
    def instance(cls) -> "AssetRenameContentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, extensions, transform: AssetRenameTransform) -> None:
        if not callable(transform):
            raise TypeError("asset rename content adapter must be callable")
        for extension in extensions:
            normalized = str(extension or "").strip().lower()
            if not normalized.startswith("."):
                raise ValueError("asset rename content extension must start with '.'")
            self._adapters[normalized] = transform

    def unregister(self, extension: str) -> None:
        self._adapters.pop(str(extension or "").strip().lower(), None)

    def build_patch(self, source: str, destination: str) -> tuple[str, str] | None:
        if not os.path.isfile(source):
            return None
        transform = self._adapters.get(os.path.splitext(source)[1].lower())
        if transform is None:
            return None
        with open(source, "r", encoding="utf-8") as stream:
            original = stream.read()
        updated = transform(original, source, destination)
        if not isinstance(updated, str):
            raise TypeError("asset rename content adapter must return text")
        return (original, updated) if updated != original else None

    def shutdown(self) -> None:
        self._adapters.clear()
        if AssetRenameContentRegistry._instance is self:
            AssetRenameContentRegistry._instance = None


__all__ = [
    "AssetReferenceContentPatch",
    "AssetReferenceRelocationPlanner",
    "AssetRenameContentRegistry",
    "AssetRenameTransform",
]
