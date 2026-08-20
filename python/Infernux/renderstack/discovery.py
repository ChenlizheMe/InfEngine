"""
Pipeline and Pass Auto-Discovery

Scans the project for RenderPipeline subclasses and RenderPass subclasses,
providing registration dictionaries for the Editor UI.

Discovery strategies:
    - ``discover_pipelines()``: scans ``__subclasses__()`` recursively,
      after importing any user scripts that reference ``RenderPipeline``
    - ``discover_passes()``: scans ``RenderPass.__subclasses__()`` recursively,
      after importing any user scripts that reference ``RenderPass``
    - Both exclude abstract bases and internal classes (prefixed with ``_``)
"""

from __future__ import annotations

import ast
import importlib.util
import os
from Infernux.engine.path_utils import path_key, relative_path
import sys
from dataclasses import dataclass
from typing import Dict, Optional, Set

from Infernux.engine.project_context import (
    get_assets_root,
    get_project_root,
    temporary_script_import_paths,
)


_pipeline_cache: Optional[Dict[str, type]] = None
_pass_cache: Optional[Dict[str, type]] = None
_effect_feature_scripts_loaded = False
_pipeline_source_classification: dict[str, tuple[int, int, bool]] = {}
_source_inheritance_cache: dict[str, tuple[int, int, "_SourceInheritance"]] = {}
_script_import_failures: dict[str, str] = {}
_catalog_class_names: set[str] = {
    "RenderPipeline",
    "RenderPass",
    "GeometryPass",
    "FullScreenEffect",
}


@dataclass(frozen=True)
class _SourceInheritance:
    classes: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _loaded_inheritance_roots(base: type) -> set[str]:
    """Return names from one already-loaded engine inheritance tree."""
    names = {base.__name__}
    pending = [base]
    while pending:
        current = pending.pop()
        for subclass in current.__subclasses__():
            if subclass.__name__ in names:
                continue
            names.add(subclass.__name__)
            pending.append(subclass)
    return names


def _refresh_catalog_class_names() -> None:
    """Include built-in intermediate bases used by project render scripts."""
    # Import the built-in pipelines explicitly. Discovery may be the first
    # render-stack operation in a fresh process, so relying on __subclasses__
    # alone would miss these intermediate bases until some unrelated UI path
    # happened to import them.
    from Infernux.renderstack.default_deferred_pipeline import DefaultDeferredPipeline
    from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline
    from Infernux.renderstack.default_forward_plus_pipeline import DefaultForwardPlusPipeline
    from Infernux.renderstack.render_pass import RenderPass
    from Infernux.renderstack.render_pipeline import RenderPipeline

    _catalog_class_names.update(
        {
            DefaultDeferredPipeline.__name__,
            DefaultForwardPipeline.__name__,
            DefaultForwardPlusPipeline.__name__,
        }
    )
    _catalog_class_names.update(_loaded_inheritance_roots(RenderPipeline))
    _catalog_class_names.update(_loaded_inheritance_roots(RenderPass))


def _expression_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id).rsplit(".", 1)[-1]
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value, aliases)
    return ""


def _read_source_inheritance(file_path: str) -> _SourceInheritance:
    normalized = path_key(file_path)
    try:
        stat = os.stat(file_path)
    except OSError:
        return _SourceInheritance()
    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    cached = _source_inheritance_cache.get(normalized)
    if cached is not None and cached[:2] == signature:
        return cached[2]
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as stream:
            tree = ast.parse(stream.read(), filename=file_path)
    except (OSError, SyntaxError, ValueError):
        result = _SourceInheritance()
    else:
        aliases: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        aliases[alias.asname or alias.name] = alias.name
        classes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = tuple(
                    name
                    for base in node.bases
                    if (name := _expression_name(base, aliases))
                )
                classes.append((node.name, bases))
        result = _SourceInheritance(tuple(classes))
    _source_inheritance_cache[normalized] = (*signature, result)
    return result


def _candidate_source_paths(search_root: str, roots: set[str]) -> list[str]:
    """Return project sources in inheritance-depth order.

    The static pass only reasons about class declarations. It therefore avoids
    importing ordinary component scripts while still finding subclasses whose
    source never spells RenderPipeline/RenderPass directly.
    """
    sources: list[tuple[str, _SourceInheritance]] = []
    bytecode: list[str] = []
    for dirpath, dirs, filenames in os.walk(search_root):
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".")
            and name not in {"__pycache__", "build", "dist", ".venv", "venv", ".runtime"}
        ]
        for filename in filenames:
            if filename.startswith("_"):
                continue
            full = os.path.join(dirpath, filename)
            if filename.endswith(".py"):
                sources.append((full, _read_source_inheritance(full)))
            elif filename.endswith(".pyc"):
                bytecode.append(full)

    known = set(roots)
    selected: list[str] = []
    selected_paths: set[str] = set()
    changed = True
    while changed:
        changed = False
        for file_path, inheritance in sources:
            for class_name, bases in inheritance.classes:
                if class_name in known or not any(base in known for base in bases):
                    continue
                known.add(class_name)
                _catalog_class_names.add(class_name)
                normalized = path_key(file_path)
                if normalized not in selected_paths:
                    selected_paths.add(normalized)
                    selected.append(file_path)
                changed = True
    # Packaged projects contain only curated bytecode, which cannot be
    # inspected cheaply. Preserve the existing unconditional player behavior.
    selected.extend(bytecode)
    return selected


def invalidate_discovery_cache() -> None:
    """Clear cached pipeline/pass discovery results."""
    global _pipeline_cache, _pass_cache, _effect_feature_scripts_loaded
    _pipeline_cache = None
    _pass_cache = None
    _effect_feature_scripts_loaded = False


def discovery_import_failures() -> dict[str, str]:
    """Return current project script import failures keyed by source path.

    Discovery remains tolerant in the Editor so one broken authoring script
    does not hide every built-in pipeline.  Player pipeline resolution uses
    this diagnostic map to reject a missing custom provider explicitly rather
    than silently substituting another rendering contract.
    """

    return dict(_script_import_failures)


def script_may_affect_pipeline_catalog(file_path: str, event_type: str = "modified") -> bool:
    """Return whether one script mutation can change the pipeline catalog.

    Ordinary component scripts must not trigger project-wide pipeline
    discovery.  Besides being unrelated work, discovery walks and opens user
    scripts on the Editor thread, which made create/save/delete operations
    visibly stall in larger projects.
    """
    normalized = path_key(file_path) if file_path else ""
    if not normalized:
        return False
    _refresh_catalog_class_names()
    if normalized in _loaded_scripts:
        # A deleted or moved pipeline source can no longer be inspected, but
        # the discovery cache still remembers that it previously contributed.
        return True
    if str(event_type or "").lower() == "deleted" or not os.path.isfile(file_path):
        _pipeline_source_classification.pop(normalized, None)
        return False
    try:
        stat = os.stat(file_path)
    except OSError:
        return False
    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    cached = _pipeline_source_classification.get(normalized)
    if cached is not None and cached[:2] == signature:
        return cached[2]
    inheritance = _read_source_inheritance(file_path)
    result = any(
        any(base in _catalog_class_names for base in bases)
        for _class_name, bases in inheritance.classes
    )
    _pipeline_source_classification[normalized] = (*signature, result)
    return result


def discover_pipelines() -> Dict[str, type]:
    """Scan all loaded RenderPipeline subclasses.

    Also scans user project scripts for ``RenderPipeline`` references and
    imports them so that ``__subclasses__()`` can find them.

    Returns:
        ``{pipeline.name: pipeline_class}`` dictionary.
        Excludes classes whose ``name`` starts with ``"_"``.
    """
    global _pipeline_cache
    if _pipeline_cache is not None:
        return dict(_pipeline_cache)

    from Infernux.renderstack.render_pipeline import RenderPipeline

    _refresh_catalog_class_names()
    roots = _loaded_inheritance_roots(RenderPipeline)
    _catalog_class_names.update(roots)
    _ensure_user_scripts_loaded(*roots)

    result: Dict[str, type] = {}
    _collect_subclasses(RenderPipeline, result, name_attr="name")
    _pipeline_cache = result
    return dict(result)


def discover_passes() -> Dict[str, type]:
    """Scan all loaded RenderPass subclasses.

    Also scans user project scripts for ``RenderPass`` (or subclass)
    references and imports them.

    Returns:
        ``{pass.name: pass_class}`` dictionary.
        Excludes abstract bases (GeometryPass)
        and classes whose ``name`` is empty or starts with ``"_"``.
    """
    global _pass_cache
    if _pass_cache is not None:
        return dict(_pass_cache)

    from Infernux.renderstack.render_pass import RenderPass

    _refresh_catalog_class_names()
    roots = _loaded_inheritance_roots(RenderPass)
    _catalog_class_names.update(roots)
    _ensure_user_scripts_loaded(*roots)

    result: Dict[str, type] = {}
    _collect_subclasses(RenderPass, result, name_attr="name")
    _pass_cache = result
    return dict(result)


def discover_effect_features() -> None:
    """Import project modules that declare or register ``.effect`` features.

    Effect documents are compiled independently from the active scene.  Their
    feature implementations therefore cannot depend on a RenderStack having
    instantiated a particular pipeline first.
    """
    global _effect_feature_scripts_loaded
    if _effect_feature_scripts_loaded:
        return
    search_root = get_assets_root()
    if not search_root or not os.path.isdir(search_root):
        return
    candidates: list[str] = []
    for dirpath, dirs, filenames in os.walk(search_root):
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".")
            and name not in {"__pycache__", "build", "dist", ".venv", "venv", ".runtime"}
        ]
        for filename in filenames:
            if filename.startswith("_"):
                continue
            full = os.path.join(dirpath, filename)
            if filename.endswith(".pyc"):
                # A Player deliberately contains curated bytecode rather than
                # authoring source.  Pipeline discovery already imports that
                # bytecode; effect discovery must do the same so a standalone
                # .effect feature never silently vanishes after cooking.
                candidates.append(full)
                continue
            if not filename.endswith(".py"):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as stream:
                    source = stream.read()
                    if (
                        "render_effect_feature" in source
                        or "register_render_effect_feature" in source
                    ):
                        candidates.append(full)
            except OSError:
                continue
    _import_source_paths(candidates)
    _effect_feature_scripts_loaded = True


# -- Internal helpers -------------------------------------------------------

_loaded_scripts: Set[str] = set()
_loaded_script_modules: Dict[str, str] = {}
_loaded_script_mtime: Dict[str, float] = {}


def _ensure_user_scripts_loaded(*keywords: str) -> None:
    """Import project sources belonging to the requested inheritance trees.

    Each file is imported at most once across the lifetime of the process.
    """
    project_root = get_project_root()
    search_root = get_assets_root()
    if not project_root or not search_root or not os.path.isdir(search_root):
        return

    _import_source_paths(_candidate_source_paths(search_root, set(keywords)))


def _import_source_paths(paths) -> None:
    """Import explicit project source paths through the shared discovery cache."""
    _prune_deleted_loaded_scripts()
    for full in paths:
        fn = os.path.basename(full)
        is_pyc = fn.endswith(".pyc")
        norm = path_key(full)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            mtime = 0.0

        if norm in _loaded_scripts and _loaded_script_mtime.get(norm) == mtime:
            continue

        _loaded_scripts.add(norm)
        mod_name = _loaded_script_modules.get(norm)
        if not mod_name:
            stem = fn[:-4] if is_pyc else fn[:-3]
            mod_name = f"_infernux_disc_{stem}_{id(full) & 0xFFFF:04x}"
        spec = importlib.util.spec_from_file_location(mod_name, full)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            try:
                with temporary_script_import_paths(full):
                    spec.loader.exec_module(mod)
            except Exception as exc:
                _loaded_scripts.discard(norm)
                _script_import_failures[norm] = f"{type(exc).__name__}: {exc}"
                continue
            sys.modules[mod_name] = mod
            _loaded_script_modules[norm] = mod_name
            _loaded_script_mtime[norm] = mtime
            _script_import_failures.pop(norm, None)


def _prune_deleted_loaded_scripts() -> None:
    """Drop cache entries for scripts removed from disk."""
    for norm in list(_loaded_scripts):
        if os.path.exists(norm):
            continue
        _loaded_scripts.discard(norm)
        _loaded_script_mtime.pop(norm, None)
        _script_import_failures.pop(norm, None)
        mod_name = _loaded_script_modules.pop(norm, "")
        if mod_name:
            sys.modules.pop(mod_name, None)


def _collect_subclasses(
    base: type,
    out: Dict[str, type],
    name_attr: str,
) -> None:
    """Recursively collect concrete subclasses into *out*."""
    for cls in base.__subclasses__():
        name = getattr(cls, name_attr, "")
        if name and not name.startswith("_") and _is_live_class(cls):
            out[name] = cls
        # Recurse into deeper subclasses
        _collect_subclasses(cls, out, name_attr)


def _is_live_class(cls: type) -> bool:
    """Return True when a discovered class still points to a live module file.

    This filters stale classes left in ``__subclasses__()`` after users delete
    or move pipeline/pass scripts at runtime.
    """
    mod = sys.modules.get(getattr(cls, "__module__", ""))
    if mod is None:
        return False
    # In player/packaged builds there is no hot-reload, so every loaded
    # class is live by definition.
    if os.environ.get("_INFERNUX_PLAYER_MODE"):
        return True
    src = getattr(mod, "__file__", "")
    if not src:
        return True
    return os.path.exists(src)
