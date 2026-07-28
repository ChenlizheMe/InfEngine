"""
Shader file parsing and lookup utilities for the Inspector panel.

All functions are stateless except for an optional *cache* dict parameter
that callers can provide for per-session caching.
"""

import json
import os
import re
from Infernux.engine.path_utils import path_key, portable_path


def _normalize_imported_property(item: dict) -> dict:
    """Normalize importer metadata before it reaches material UI code."""
    import json

    result = dict(item)
    default = result.get("default")
    if isinstance(default, str):
        token = default.strip()
        if token.startswith("[") and "]" in token:
            bracket_end = token.find("]") + 1
            try:
                result["default"] = json.loads(token[:bracket_end])
            except (json.JSONDecodeError, TypeError):
                pass
            flags = token[bracket_end:].lstrip(" ,")
            if flags.upper() == "HDR":
                result["hdr"] = True
        elif result.get("type") == "Float":
            try:
                result["default"] = float(token)
            except ValueError:
                pass
        elif result.get("type") == "Int":
            try:
                result["default"] = int(token)
            except ValueError:
                pass
    return result


def _read_compiled_shader_metadata(filepath: str) -> dict | None:
    """Read the schema emitted by the native shader importer."""
    try:
        from Infernux.core.asset_types import read_meta_file
        metadata = read_meta_file(filepath)
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if not metadata or not isinstance(metadata.get("shader_id"), str):
        return None
    return metadata


def _strip_glsl_comments(source: str) -> str:
    """Remove GLSL comments while preserving quoted ShaderInfo values."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            while index + 1 < len(source) and source[index:index + 2] != "*/":
                index += 1
            index = min(len(source), index + 2)
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _read_source_shader_metadata(filepath: str) -> dict[str, object]:
    """Read catalog fields from a source-level ``ShaderInfo`` declaration."""
    try:
        with open(filepath, "r", encoding="utf-8") as handle:
            source = _strip_glsl_comments(handle.read())
    except OSError:
        return {}

    declaration = re.search(r"\b(?:ShaderInfo|ShadingModelInfo)\s*\{", source)
    if declaration is None:
        return {}
    body_start = declaration.end()
    depth = 1
    cursor = body_start
    in_string = False
    escaped = False
    while cursor < len(source) and depth:
        char = source[cursor]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        cursor += 1
    if depth:
        return {}
    body = source[body_start:cursor - 1]
    name_match = re.search(r'\bName\s+("(?:\\.|[^"\\])*")', body)
    if name_match is None:
        return {}
    try:
        shader_id = json.loads(name_match.group(1))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(shader_id, str) or not shader_id.strip():
        return {}

    result: dict[str, object] = {"shader_id": shader_id.strip()}
    hidden_match = re.search(r"\bHidden\s+(On|Off|True|False)\b", body, re.IGNORECASE)
    if hidden_match is not None:
        result["shader_hidden"] = hidden_match.group(1).casefold() in {"on", "true"}
    shading_model_match = re.search(r'\bShadingModel\s+("(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z0-9_]*)', body)
    if shading_model_match is not None:
        encoded = shading_model_match.group(1)
        result["shading_model"] = json.loads(encoded) if encoded.startswith('"') else encoded
    queue_match = re.search(r"\bQueue\s+(-?\d+)\b", body)
    if queue_match is not None:
        result["queue"] = int(queue_match.group(1))
    for field in ("Imports", "Capabilities"):
        list_match = re.search(rf"\b{field}\s*\[([^\]]*)\]", body)
        if list_match is None:
            continue
        values = re.findall(r'"((?:\\.|[^"\\])*)"|([A-Za-z_][A-Za-z0-9_]*)', list_match.group(1))
        result[field.casefold()] = [json.loads(f'"{quoted}"') if quoted else bare for quoted, bare in values]
    entries = {
        role: function
        for role, function in re.findall(
            r"\bEntry\s+([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)",
            body,
        )
    }
    if entries:
        result["entries"] = entries

    properties_match = re.search(r"\bProperties\s*\{", body)
    if properties_match is not None:
        property_start = properties_match.end()
        property_end = body.find("}", property_start)
        if property_end >= 0:
            properties: list[dict[str, object]] = []
            declaration_pattern = re.compile(
                r"^\s*(Float|Float2|Float3|Float4|Color|Int|Mat4|Texture2D)\s+"
                r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                r"(\[[^\]]*\]|\"(?:\\.|[^\"\\])*\"|[^\s]+)"
                r"(?:\s+Range\(\s*([^,]+)\s*,\s*([^\)]+)\s*\))?"
                r"(?:\s+(HDR))?\s*$",
                re.IGNORECASE,
            )
            for line in body[property_start:property_end].splitlines():
                match = declaration_pattern.match(line)
                if match is None:
                    continue
                prop_type, name, encoded_default, minimum, maximum, hdr = match.groups()
                if prop_type == "Texture2D":
                    default: object = encoded_default.strip('"')
                else:
                    try:
                        default = json.loads(encoded_default)
                    except (json.JSONDecodeError, TypeError):
                        default = encoded_default
                item: dict[str, object] = {
                    "name": name,
                    "type": prop_type,
                    "default": default,
                    "hdr": bool(hdr),
                }
                if minimum is not None and maximum is not None:
                    try:
                        item["range"] = [float(minimum), float(maximum)]
                    except ValueError:
                        pass
                properties.append(item)
            result["properties"] = properties
    return result

# Global generation counter, bumped on every successful shader hot-reload.
# Inspector sync keys include this so that property lists refresh automatically.
_shader_property_generation: int = 0
_shader_catalog_cache: dict[tuple[str, tuple[str, ...]], dict[str, object]] = {}
_shader_properties_cache: dict[tuple[str, str], list] = {}


def bump_shader_property_generation():
    """Increment the property generation counter (called after shader hot-reload)."""
    global _shader_property_generation
    _shader_property_generation += 1
    _shader_catalog_cache.clear()
    _shader_properties_cache.clear()


def get_shader_property_generation() -> int:
    """Return the current property generation counter."""
    return _shader_property_generation


def _get_shader_search_roots() -> list[str]:
    """Return the project and built-in shader roots."""
    from Infernux.engine.project_context import get_project_root

    project_root = get_project_root()
    search_roots = []
    if project_root:
        search_roots.append(os.path.join(project_root, "Assets"))

    from Infernux.resources import resources_path
    builtin_root = os.path.join(resources_path, "shaders")
    search_roots.append(builtin_root)
    return search_roots


def _scan_shader_catalog(ext: str, search_roots: list[str] | None = None) -> dict[str, object]:
    """Scan shader roots once and cache both candidates and id->path mapping."""
    items = []
    seen_shader_ids = set()
    shader_paths = {}

    for root in search_roots if search_roots is not None else _get_shader_search_roots():
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if not fname.lower().endswith(ext):
                    continue
                full_path = os.path.join(dirpath, fname)

                shader_id = parse_shader_id(full_path)
                if not shader_id:
                    continue

                shader_paths.setdefault(shader_id, full_path)
                if shader_id in seen_shader_ids:
                    continue
                seen_shader_ids.add(shader_id)

                if is_shader_hidden(full_path):
                    continue

                items.append((shader_id, shader_id))

    if not items:
        items = [("(No shaders found)", "")]

    return {
        "items": items,
        "paths": shader_paths,
    }


def _get_shader_catalog(ext: str) -> dict[str, object]:
    """Return cached shader catalog for the requested extension."""
    search_roots = _get_shader_search_roots()
    root_signature = tuple(path_key(root) for root in search_roots if root)
    cache_key = (ext, root_signature)
    catalog = _shader_catalog_cache.get(cache_key)
    if catalog is None:
        catalog = _scan_shader_catalog(ext, search_roots)
        _shader_catalog_cache[cache_key] = catalog
    return catalog


def _get_shader_properties_cached(shader_id: str, ext: str) -> list:
    """Return cached ShaderInfo property metadata for a shader id."""
    if not shader_id:
        return []

    cache_key = (shader_id, ext)
    cached = _shader_properties_cache.get(cache_key)
    if cached is not None:
        return cached

    shader_path = get_shader_file_path(shader_id, ext)
    if not shader_path:
        return []

    props = parse_shader_properties(shader_path)
    _shader_properties_cache[cache_key] = props
    return props


def parse_shader_id(filepath: str) -> str:
    """Return the canonical imported ShaderInfo name."""
    metadata = _read_compiled_shader_metadata(filepath)
    if metadata is not None:
        shader_id = metadata.get("shader_id", "").strip()
        if shader_id:
            return shader_id
    source_metadata = _read_source_shader_metadata(filepath)
    shader_id = source_metadata.get("shader_id", "")
    if isinstance(shader_id, str) and shader_id:
        return shader_id
    return None


def parse_shader_properties(filepath: str) -> list:
    """Read the native importer schema, with a structured source fallback.

    Returns list of dicts: [{'name': str, 'type': str, 'default': any, 'hdr': bool}, ...]

    """
    import json
    metadata = _read_compiled_shader_metadata(filepath)
    if metadata is not None:
        encoded = metadata.get("properties")
        if isinstance(encoded, str):
            try:
                properties = json.loads(encoded)
                if isinstance(properties, list):
                    return [
                        _normalize_imported_property(item)
                        for item in properties
                        if isinstance(item, dict)
                    ]
            except (json.JSONDecodeError, TypeError):
                pass

    source_properties = _read_source_shader_metadata(filepath).get("properties", [])
    return source_properties if isinstance(source_properties, list) else []


def is_shader_hidden(filepath: str) -> bool:
    """Check imported visibility, with a ShaderInfo source fallback."""
    metadata = _read_compiled_shader_metadata(filepath)
    if metadata is not None and isinstance(metadata.get("shader_hidden"), bool):
        return metadata["shader_hidden"]
    source_metadata = _read_source_shader_metadata(filepath)
    if isinstance(source_metadata.get("shader_hidden"), bool):
        return source_metadata["shader_hidden"]
    return False


def get_shader_file_path(shader_id: str, ext: str) -> str:
    """Find the file path for a given shader_id by scanning project and built-in dirs."""
    if not shader_id:
        return None
    return _get_shader_catalog(ext).get("paths", {}).get(shader_id)


def shader_ref_id(value) -> str:
    """Return the compiler shader ID from a catalog value or structured reference."""
    if isinstance(value, dict):
        shader_id = value.get("shader_id", "")
        return shader_id.strip() if isinstance(shader_id, str) else ""
    return value.strip() if isinstance(value, str) else ""


def make_shader_reference(value, ext: str) -> dict[str, str]:
    """Build a canonical material shader reference.

    ``value`` may be a catalog shader ID, an asset path, or an existing
    reference. GUID and path are enriched whenever imported metadata is
    available; built-in ID-only references remain valid.
    """
    existing = value if isinstance(value, dict) else {}
    guid = existing.get("guid", "") if isinstance(existing.get("guid", ""), str) else ""
    shader_id = shader_ref_id(value)
    path_hint = existing.get("path_hint", "") if isinstance(existing.get("path_hint", ""), str) else ""

    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lower().endswith(ext) or os.path.isfile(candidate):
            path_hint = candidate
        else:
            shader_id = candidate

    database = None
    try:
        from Infernux.lib import AssetRegistry
        database = AssetRegistry.instance().get_asset_database()
    except (AttributeError, RuntimeError, ValueError):
        pass

    resolved_path = ""
    if guid and database:
        resolved_path = database.get_path_from_guid(guid) or ""
    if not resolved_path and path_hint and os.path.isfile(path_hint):
        resolved_path = path_hint
    if not resolved_path:
        resolved_path = get_shader_file_path(shader_id, ext) or ""

    if resolved_path:
        resolved_path = portable_path(resolved_path)
        metadata = _read_compiled_shader_metadata(resolved_path)
        if metadata is not None:
            imported_id = metadata.get("shader_id", "")
            imported_guid = metadata.get("guid", "")
            if isinstance(imported_id, str) and imported_id.strip():
                shader_id = imported_id.strip()
            if isinstance(imported_guid, str) and imported_guid.strip():
                guid = imported_guid.strip()
        if not shader_id:
            shader_id = parse_shader_id(resolved_path) or ""
        if not guid and database:
            guid = database.get_guid_from_path(resolved_path) or ""
        path_hint = resolved_path

    return {
        "guid": guid,
        "shader_id": shader_id,
        "path_hint": path_hint,
    }


def shader_display_from_value(value, items):
    """Map a shader value to its display string for UI."""
    shader_id = shader_ref_id(value)
    for display, v in items:
        if v == shader_id:
            return display
    return shader_id


def get_shader_candidates(ext: str, cache: dict = None):
    """Collect shader files from project and built-in shader folders.
    Only shaders with valid ShaderInfo names are listed.
    Each unique shader_id appears only once in the list.
    
    If *cache* is provided and already contains entries for *ext*, the
    cached result is returned immediately.
    """
    if cache is not None and cache.get(ext) is not None:
        return cache[ext]

    items = _get_shader_catalog(ext).get("items", [("(No shaders found)", "")])

    if cache is not None:
        cache[ext] = items
    return items


_SHADER_TYPE_MAP = {
    'Float': 0,
    'Float2': 1,
    'Float3': 2,
    'Float4': 3,
    'Color': 7,
    'Int': 4,
    'Mat4': 5,
    'Texture2D': 6,
}


_PROPERTY_VALUE_DEFAULTS = {
    "Float": 0.0,
    "Float2": [0.0, 0.0],
    "Float3": [0.0, 0.0, 0.0],
    "Float4": [0.0, 0.0, 0.0, 0.0],
    "Color": [1.0, 1.0, 1.0, 1.0],
    "Int": 0,
    "Mat4": [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ],
}


def _coerce_property_value(property_type: str, value):
    """Return a canonical material value, or ``None`` when incompatible."""
    try:
        if property_type == "Float":
            if isinstance(value, (list, tuple, dict, bool)):
                return None
            return float(value)
        if property_type == "Int":
            if isinstance(value, (list, tuple, dict, bool)):
                return None
            return int(value)

        lengths = {
            "Float2": 2,
            "Float3": 3,
            "Float4": 4,
            "Color": 4,
            "Mat4": 16,
        }
        expected = lengths.get(property_type)
        if expected is None or not isinstance(value, (list, tuple)) or len(value) != expected:
            return None
        return [float(component) for component in value]
    except (TypeError, ValueError, OverflowError):
        return None


def _apply_shader_props_to_mat(mat_data: dict, all_props: list[dict],
                                remove_unknown: bool = False):
    """Apply a merged list of imported shader property dicts to mat_data.

    Shared implementation for `sync_properties_from_shader` and
    `sync_all_shader_properties`.
    """
    if not all_props:
        return

    props = mat_data.setdefault("properties", {})

    seen_names: set[str] = set()
    ordered_names: list[str] = []
    for sp in all_props:
        name = sp.get('name', '')
        if name and name not in seen_names:
            ordered_names.append(name)
            seen_names.add(name)
    mat_data["_shader_property_order"] = ordered_names

    shader_prop_names: set[str] = set()
    for raw_sp in all_props:
        sp = _normalize_imported_property(raw_sp)
        name = sp.get('name', '')
        ptype_str = sp.get('type', 'Float')
        default = sp.get('default')
        hdr = sp.get('hdr', False)
        authored_range = sp.get('range')

        if not name:
            continue

        shader_prop_names.add(name)
        ptype = _SHADER_TYPE_MAP.get(ptype_str, 0)

        existing = props.get(name)
        if not isinstance(existing, dict):
            existing = {}
            props[name] = existing

        existing['type'] = ptype
        existing['hdr'] = hdr
        if ptype == 6:
            guid = existing.get('guid', '')
            existing['guid'] = guid if isinstance(guid, str) else ''
            existing.pop('value', None)
        else:
            fallback = _coerce_property_value(ptype_str, default)
            if fallback is None:
                fallback = _coerce_property_value(
                    ptype_str, _PROPERTY_VALUE_DEFAULTS.get(ptype_str)
                )
            current = _coerce_property_value(ptype_str, existing.get('value'))
            existing['value'] = current if current is not None else fallback
            existing.pop('guid', None)

        if (
            ptype in (0, 4)
            and isinstance(authored_range, (list, tuple))
            and len(authored_range) == 2
        ):
            if ptype == 4:
                props[name]['range'] = [int(authored_range[0]), int(authored_range[1])]
            else:
                props[name]['range'] = [float(authored_range[0]), float(authored_range[1])]
        else:
            props[name].pop('range', None)

    if remove_unknown:
        for k in [k for k in props if k not in shader_prop_names]:
            del props[k]


def sync_properties_from_shader(mat_data: dict, shader_id: str, ext: str,
                                remove_unknown: bool = False):
    """Sync material properties from ShaderInfo declarations.
    Adds new properties from shader, keeps existing values if property exists.
    If *remove_unknown* is True, removes properties not defined in shader.
    """
    shader_props = _get_shader_properties_cached(shader_id, ext)
    if not shader_props:
        # Shader file may be temporarily incomplete during hot-reload.
        # Do NOT clear properties or ordering metadata — preserve existing
        # state so the inspector doesn't flicker.
        return
    _apply_shader_props_to_mat(mat_data, shader_props, remove_unknown=remove_unknown)


def sync_all_shader_properties(mat_data: dict, vert_shader_id: str, frag_shader_id: str,
                               remove_unknown: bool = False):
    """Sync material properties from both vertex and fragment shader annotations.

    Merges properties from both shader stages. Vertex properties appear
    first in the display order, followed by fragment properties.
    If *remove_unknown* is True, removes properties not defined in either shader.
    """
    all_props: list[dict] = []
    if vert_shader_id:
        all_props.extend(_get_shader_properties_cached(vert_shader_id, ".vert"))
    if frag_shader_id:
        all_props.extend(_get_shader_properties_cached(frag_shader_id, ".frag"))
    _apply_shader_props_to_mat(mat_data, all_props, remove_unknown=remove_unknown)


def get_all_shader_property_names(vert_shader_id: str, frag_shader_id: str) -> list[str]:
    """Return all declared material property names from the active vertex and fragment shaders."""
    ordered_names: list[str] = []
    seen_names: set[str] = set()

    for shader_id, ext in ((vert_shader_id, ".vert"), (frag_shader_id, ".frag")):
        if not shader_id:
            continue
        for sp in _get_shader_properties_cached(shader_id, ext):
            name = sp.get("name", "")
            if name and name not in seen_names:
                ordered_names.append(name)
                seen_names.add(name)

    return ordered_names


def get_material_property_display_order(mat_data: dict) -> list[str]:
    """Return material properties in shader declaration order only.

    Properties not declared in the shader (phantom / stale) are excluded.
    """
    props = mat_data.get("properties", {})
    if not props:
        return []

    shader_order = mat_data.get("_shader_property_order", [])
    shader_set = set(shader_order) if shader_order else None

    ordered = []
    seen = set()
    for name in shader_order:
        if name in props and name not in seen:
            ordered.append(name)
            seen.add(name)

    # Only include extras if there is no shader metadata (e.g. unloaded shader)
    if shader_set is None:
        for name in sorted(props.keys()):
            if name not in seen:
                ordered.append(name)

    return ordered
