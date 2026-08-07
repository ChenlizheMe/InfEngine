import json
import keyword
import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional
from Infernux.debug import Debug
from Infernux.engine.path_utils import is_path_within, portable_path, relative_path, resolved_path

_project_root: Optional[str] = None
_guid_manifest: Optional[dict] = None
_guid_manifest_loaded: bool = False

def set_project_root(path: Optional[str]) -> None:
    """Set the current project root for path normalization."""
    global _project_root
    _project_root = resolved_path(path) if path else None


def get_project_root() -> Optional[str]:
    """Get the current project root if set."""
    return _project_root


def get_assets_root() -> Optional[str]:
    """Return the project's Assets directory when available."""
    if not _project_root:
        return None
    assets_root = os.path.join(_project_root, "Assets")
    if os.path.isdir(assets_root):
        return assets_root
    return None


def _is_valid_module_segment(segment: str) -> bool:
    return bool(segment) and segment.isidentifier() and not keyword.iskeyword(segment)


def get_script_module_name(path: Optional[str]) -> Optional[str]:
    """Return the canonical Python module name for a user script.

    Scripts inside ``Assets/`` map to import names relative to that folder:
    - ``Assets/a2.py`` -> ``a2``
    - ``Assets/scripts/foo.py`` -> ``scripts.foo``

    Returns ``None`` when the script is outside ``Assets/`` or its path cannot
    be expressed as a valid Python module name.
    """
    resolved = resolve_script_path(path) if path else None
    if not resolved:
        return None

    assets_root = get_assets_root()
    resolved_abs = resolved_path(resolved)
    if not assets_root:
        return None

    if not is_path_within(resolved_abs, assets_root):
        return None

    rel_path = relative_path(resolved_abs, assets_root)
    module_path, ext = os.path.splitext(rel_path)
    if ext not in (".py", ".pyc"):
        return None

    parts = portable_path(module_path).split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    if any(not _is_valid_module_segment(part) for part in parts):
        return None
    return ".".join(parts)


def get_script_import_paths(path: Optional[str] = None) -> list[str]:
    """Return Python import roots for a user script.

    Rules:
    - Scripts directly under ``Assets/`` can import siblings as ``import foo``.
    - Scripts under subfolders use asset-root-relative imports such as
      ``from scripts.foo import Bar``.
    """
    resolved = resolve_script_path(path) if path else None
    resolved_abs = resolved_path(resolved) if resolved else ""
    assets_root = get_assets_root()
    project_root = get_project_root()

    roots: list[str] = []

    if assets_root and resolved_abs:
        if is_path_within(resolved_abs, assets_root):
            roots.append(assets_root)
            parent_dir = os.path.dirname(resolved_abs)
            if parent_dir and parent_dir not in roots:
                roots.append(parent_dir)
            return roots

    if assets_root:
        roots.append(assets_root)
    if project_root and project_root not in roots:
        roots.append(project_root)
    if resolved_abs:
        parent_dir = os.path.dirname(resolved_abs)
        if parent_dir and parent_dir not in roots:
            roots.append(parent_dir)
    return roots


@contextmanager
def temporary_script_import_paths(path: Optional[str]) -> Iterator[None]:
    """Temporarily prepend the relevant import roots for a user script."""
    old_path = sys.path.copy()
    for import_path in reversed(get_script_import_paths(path)):
        if import_path and import_path not in sys.path:
            sys.path.insert(0, import_path)
    try:
        yield
    finally:
        sys.path = old_path


def resolve_script_path(path: Optional[str]) -> Optional[str]:
    """Resolve a possibly relative script path to an absolute path.

    In packaged builds the original ``.py`` sources are compiled to
    ``.pyc`` and removed.  If the resolved ``.py`` path does not exist
    but a corresponding ``.pyc`` does, the ``.pyc`` path is returned
    so that callers transparently load the compiled version.
    """
    if not path:
        return path
    if os.path.isabs(path):
        resolved = resolved_path(path)
    elif _project_root:
        resolved = resolved_path(os.path.join(_project_root, path))
    else:
        resolved = resolved_path(path)

    # Fallback: .py → .pyc for packaged builds
    if not os.path.exists(resolved) and resolved.endswith('.py'):
        pyc = resolved + 'c'
        if os.path.exists(pyc):
            return pyc
    return resolved


def resolve_guid_to_path(guid: str) -> Optional[str]:
    """Resolve a script GUID using the build-time manifest.

    In packaged builds the original ``.py`` sources are compiled to
    ``.pyc`` and removed.  The C++ ``AssetDatabase`` cannot register
    ``.pyc`` files, so GUID look-ups return empty.  At build time a
    ``_script_guid_map.json`` manifest is written that maps GUIDs to
    relative ``.pyc`` paths.  This function loads and queries it.
    """
    global _guid_manifest, _guid_manifest_loaded
    if not _guid_manifest_loaded:
        _guid_manifest_loaded = True
        if _project_root:
            manifest = os.path.join(_project_root, "_script_guid_map.json")
            if os.path.isfile(manifest):
                try:
                    with open(manifest, "r", encoding="utf-8") as f:
                        _guid_manifest = json.load(f)
                except (json.JSONDecodeError, OSError) as exc:
                    Debug.log_suppressed("project_context.load_guid_manifest", exc)
    if _guid_manifest and guid and guid in _guid_manifest:
        rel = _guid_manifest[guid]
        if _project_root:
            return os.path.join(_project_root, rel)
    return None
