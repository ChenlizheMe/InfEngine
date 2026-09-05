import json
import keyword
import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional, Protocol
from Infernux.debug import Debug
from Infernux.engine.path_utils import is_path_within, portable_path, relative_path, resolved_path

class _RuntimeAssetResolver(Protocol):
    def __call__(self, path: str, /, *, allow_directory: bool = False) -> Optional[str]: ...


_project_root: Optional[str] = None
_runtime_asset_resolver: Optional[_RuntimeAssetResolver] = None
_guid_manifest: Optional[dict] = None
_guid_manifest_loaded: bool = False
_package_registry_cache: tuple[str, int, int, frozenset[str]] | None = None

def set_project_root(path: Optional[str]) -> None:
    """Set the current project root for path normalization."""
    global _project_root, _runtime_asset_resolver
    _project_root = resolved_path(path) if path else None
    _runtime_asset_resolver = None


def set_runtime_asset_resolver(
    resolver: Optional[_RuntimeAssetResolver],
) -> None:
    """Install the immutable packaged-asset resolver for the active Player."""
    global _runtime_asset_resolver
    if resolver is not None and not callable(resolver):
        raise TypeError("runtime asset resolver must be callable")
    _runtime_asset_resolver = resolver


def resolve_asset_path(
    path: str,
    *,
    project_root: Optional[str] = None,
    allow_directory: bool = False,
) -> Optional[str]:
    """Resolve one project asset in Editor or from the cooked Player catalog."""
    raw = os.fspath(path)
    if not raw:
        return None
    if _runtime_asset_resolver is not None:
        return _runtime_asset_resolver(raw, allow_directory=allow_directory)
    root = project_root or _project_root
    if not root:
        return None
    candidate = resolved_path(
        raw if os.path.isabs(raw) else os.path.join(root, raw)
    )
    asset_roots = get_project_script_roots(root)
    if not any(
        is_path_within(candidate, root, allow_root=False)
        for root in asset_roots
    ):
        return None
    if os.path.isfile(candidate) or (allow_directory and os.path.isdir(candidate)):
        return candidate
    return None


def get_project_root() -> Optional[str]:
    """Get the current project root if set."""
    return _project_root


@contextmanager
def using_project_root(path: Optional[str]) -> Iterator[Optional[str]]:
    """Bind ``get_project_root()`` for one compile or cook interval."""
    previous = get_project_root()
    previous_resolver = _runtime_asset_resolver
    set_project_root(path)
    try:
        yield get_project_root()
    finally:
        set_project_root(previous)
        set_runtime_asset_resolver(previous_resolver)


def get_assets_root() -> Optional[str]:
    """Return the project's Assets directory when available."""
    if not _project_root:
        return None
    assets_root = os.path.join(_project_root, "Assets")
    if os.path.isdir(assets_root):
        return assets_root
    return None


def get_project_script_roots(project_root: Optional[str] = None) -> tuple[str, ...]:
    """Return the authoritative Editor source roots for one project."""

    root = resolved_path(project_root or _project_root) if (project_root or _project_root) else ""
    if not root:
        return ()
    return (
        resolved_path(os.path.join(root, "Assets")),
        resolved_path(os.path.join(root, "Packages")),
    )


def package_script_role(path: str, project_root: Optional[str] = None) -> str:
    """Return ``runtime`` or ``editor`` for an installed package script.

    The nearest package manifest owns the layout boundary.  This avoids
    guessing where a namespaced package reference ends when a reference itself
    contains a segment named ``runtime`` or ``editor``.
    """

    _, role, _ = _package_script_layout(path, project_root)
    return role


def package_script_reference(path: str, project_root: Optional[str] = None) -> str:
    """Return the project-relative package identity that owns one script."""

    package_root, role, _ = _package_script_layout(path, project_root)
    roots = get_project_script_roots(project_root)
    if not package_root or not role or len(roots) != 2:
        return ""
    return portable_path(relative_path(package_root, roots[1])).strip("/")


def _package_script_layout(
    path: str,
    project_root: Optional[str] = None,
) -> tuple[str, str, str]:
    """Return ``(package_root, role, role_relative_path)`` for package code."""

    roots = get_project_script_roots(project_root)
    if len(roots) != 2:
        return "", "", ""
    packages_root = roots[1]
    candidate = resolved_path(path)
    if not is_path_within(candidate, packages_root, allow_root=False):
        return "", "", ""

    current = os.path.dirname(candidate)
    while is_path_within(current, packages_root, allow_root=False):
        try:
            has_manifest = any(
                entry.is_file()
                and entry.name.casefold() in {"inx_package.json", "inxpackage.json"}
                for entry in os.scandir(current)
            )
        except OSError:
            has_manifest = False
        if has_manifest:
            logical = portable_path(relative_path(candidate, current))
            first, separator, remainder = logical.partition("/")
            role = first.casefold()
            if separator and role in {"runtime", "editor"}:
                return current, role, remainder
            return "", "", ""
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    # Player builds intentionally omit package control manifests.  The staged
    # runtime registry is then the authoritative package boundary, especially
    # for namespaced references such as ``vendor/tool``.
    registry_path = os.path.join(
        resolved_path(project_root or _project_root),
        "ProjectSettings",
        "InxPlugins.json",
    )
    try:
        with open(registry_path, "r", encoding="utf-8") as stream:
            installed = json.load(stream).get("installed", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        installed = []
    registered_roots: list[tuple[int, str]] = []
    for package in installed:
        if not isinstance(package, dict):
            continue
        reference = str(package.get("reference", "")).strip("/")
        parts = reference.split("/")
        if not reference or any(part in {"", ".", ".."} for part in parts):
            continue
        package_root = resolved_path(os.path.join(packages_root, *parts))
        if is_path_within(candidate, package_root, allow_root=False):
            registered_roots.append((len(parts), package_root))
    for _depth, package_root in sorted(registered_roots, reverse=True):
        logical = portable_path(relative_path(candidate, package_root))
        first, separator, remainder = logical.partition("/")
        role = first.casefold()
        if separator and role in {"runtime", "editor"}:
            return package_root, role, remainder
    # A local author may develop a simple package directly as
    # Packages/<name>/{runtime,editor}/... without writing a manifest.  The
    # first directory is then the package identity boundary; namespaced
    # references use an explicit manifest to remove that ambiguity.
    relative = portable_path(relative_path(candidate, packages_root))
    parts = relative.split("/")
    role = parts[1].casefold() if len(parts) >= 2 else ""
    if len(parts) >= 3 and role in {"runtime", "editor"}:
        package_root = resolved_path(os.path.join(packages_root, parts[0]))
        return package_root, role, "/".join(parts[2:])
    return "", "", ""


def _package_role_root(package_root: str, role: str) -> str:
    """Return the on-disk role directory while normalizing its identity."""

    try:
        for entry in os.scandir(package_root):
            if entry.is_dir() and entry.name.casefold() == role:
                return resolved_path(entry.path)
    except OSError:
        pass
    return resolved_path(os.path.join(package_root, role))


def is_project_component_script(
    path: str,
    project_root: Optional[str] = None,
) -> bool:
    """Return whether *path* belongs to the project's gameplay script domain."""

    roots = get_project_script_roots(project_root)
    if not roots:
        return False
    root = resolved_path(project_root or _project_root)
    candidate = resolved_path(path)
    if is_path_within(candidate, roots[0], allow_root=False):
        return True
    package_root, role, _ = _package_script_layout(candidate, project_root)
    if role != "runtime":
        return False
    reference = portable_path(relative_path(package_root, roots[1])).casefold()
    return reference not in _disabled_package_references(root)


def _disabled_package_references(project_root: str) -> frozenset[str]:
    """Return explicitly disabled installed packages from the durable registry."""

    global _package_registry_cache
    registry = resolved_path(
        os.path.join(project_root, "ProjectSettings", "InxPlugins.json")
    )
    try:
        stat = os.stat(registry)
    except FileNotFoundError:
        return frozenset()
    cache = _package_registry_cache
    if (
        cache is not None
        and cache[0] == registry
        and cache[1] == stat.st_mtime_ns
        and cache[2] == stat.st_size
    ):
        return cache[3]
    try:
        with open(registry, "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Plugin registry is unreadable: {registry}") from exc
    installed = document.get("installed") if isinstance(document, dict) else None
    if not isinstance(installed, list):
        raise ValueError(f"Plugin registry has no installed package list: {registry}")
    disabled = frozenset(
        portable_path(str(item.get("reference", ""))).strip("/").casefold()
        for item in installed
        if isinstance(item, dict)
        and not bool(item.get("enabled", True))
        and str(item.get("reference", "")).strip()
    )
    _package_registry_cache = (registry, stat.st_mtime_ns, stat.st_size, disabled)
    return disabled


def _is_valid_module_segment(segment: str) -> bool:
    return bool(segment) and segment.isidentifier() and not keyword.iskeyword(segment)


def _package_reference_module_segment(segment: str) -> str:
    """Encode one package-reference segment as a reversible Python identifier."""

    encoded = []
    for byte in segment.encode("utf-8"):
        char = chr(byte)
        if char.isascii() and char.isalnum():
            encoded.append(char)
        else:
            encoded.append(f"_{byte:02x}")
    value = "".join(encoded)
    if not value or value[0].isdigit() or keyword.iskeyword(value):
        value = "p_" + value
    return value


def get_script_module_name(
    path: Optional[str],
    project_root: Optional[str] = None,
) -> Optional[str]:
    """Return the canonical Python module name for a user script.

    Scripts inside ``Assets/`` map to import names relative to that folder:
    - ``Assets/a2.py`` -> ``a2``
    - ``Assets/scripts/foo.py`` -> ``scripts.foo``

    Installed package code uses an isolated, deterministic namespace:
    ``Packages/vendor/tool/runtime/foo.py`` maps to
    ``_infernux_packages.vendor.tool.runtime.foo``.  The package namespace
    prevents two plugins with the same filenames from sharing ``sys.modules``.

    Returns ``None`` when the script is outside the gameplay/editor script
    domains or its code-relative path is not a valid Python module name.
    """
    active_root = resolved_path(project_root or _project_root) if (project_root or _project_root) else ""
    if path and project_root and not os.path.isabs(path):
        resolved = resolved_path(os.path.join(active_root, path))
    else:
        resolved = resolve_script_path(path) if path else None
    if not resolved:
        return None

    assets_root = resolved_path(os.path.join(active_root, "Assets")) if active_root else None
    resolved_abs = resolved_path(resolved)
    rel_path = ""
    prefix: list[str] = []
    if assets_root and is_path_within(resolved_abs, assets_root):
        rel_path = relative_path(resolved_abs, assets_root)
    else:
        package_root, role, role_relative = _package_script_layout(
            resolved_abs,
            active_root,
        )
        roots = get_project_script_roots(active_root)
        if not package_root or not role or len(roots) != 2:
            return None
        reference = portable_path(relative_path(package_root, roots[1]))
        reference_parts = [
            _package_reference_module_segment(part)
            for part in reference.split("/")
            if part
        ]
        if not reference_parts:
            return None
        prefix = ["_infernux_packages", *reference_parts, role]
        rel_path = role_relative

    module_path, ext = os.path.splitext(rel_path)
    if ext not in (".py", ".pyc"):
        return None

    parts = portable_path(module_path).split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return ".".join(prefix) if prefix else None
    if any(not _is_valid_module_segment(part) for part in parts):
        return None
    return ".".join((*prefix, *parts))


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

    package_root, role, _ = _package_script_layout(resolved_abs, project_root)
    if package_root and role:
        roots.append(_package_role_root(package_root, role))
        roots.append(package_root)

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
