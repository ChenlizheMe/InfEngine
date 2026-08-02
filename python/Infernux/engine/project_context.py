import json
import keyword
import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional, Callable, Any
from Infernux.debug import Debug
from Infernux.engine.path_utils import is_path_within, portable_path, relative_path, resolved_path

_project_root: Optional[str] = None
_guid_manifest: Optional[dict] = None
_guid_manifest_loaded: bool = False


class _LegacyPanelDocumentController:
    """Temporary adapter while authoring panels migrate to DocumentRegistry."""

    def __init__(self) -> None:
        self.save_handler: Optional[Callable[[], Any]] = None
        self.save_pending_handler: Optional[Callable[[], bool]] = None
        self.discard_handler: Optional[Callable[[], Any]] = None

    def save(self, *, ticket, save_as: bool = False) -> Any:
        if not callable(self.save_handler):
            return False
        result = self.save_handler()
        if callable(self.save_pending_handler) and self.save_pending_handler():
            from Infernux.engine.interaction import (
                DocumentActionResult,
                DocumentActionStatus,
            )

            return DocumentActionResult(DocumentActionStatus.PENDING)
        return result

    def discard(self) -> Any:
        if not callable(self.discard_handler):
            return False
        return self.discard_handler()

    def poll_save(self, _ticket) -> Optional[bool]:
        if callable(self.save_pending_handler) and self.save_pending_handler():
            return None
        return False


def _legacy_document_id(panel_id: str) -> str:
    return f"legacy-panel:{panel_id}"


def _legacy_panel_document(panel_id: str, *, create: bool = False, title: str = ""):
    from Infernux.engine.interaction import DocumentKind, DocumentRegistry

    registry = DocumentRegistry.instance()
    document = registry.document_for_view(panel_id)
    if document is not None or not create:
        return document
    document = registry.create(
        DocumentKind.GENERIC,
        title or panel_id,
        document_id=_legacy_document_id(panel_id),
        controller=_LegacyPanelDocumentController(),
    )
    registry.attach_view(document.document_id, panel_id)
    return document


def _legacy_capabilities(controller: _LegacyPanelDocumentController):
    from Infernux.engine.interaction import DocumentCapability

    capabilities = DocumentCapability.NONE
    if callable(controller.save_handler):
        capabilities |= DocumentCapability.SAVE
    if callable(controller.discard_handler):
        capabilities |= DocumentCapability.DISCARD
    return capabilities


def set_project_root(path: Optional[str]) -> None:
    """Set the current project root for path normalization."""
    global _project_root
    _project_root = resolved_path(path) if path else None


def get_project_root() -> Optional[str]:
    """Get the current project root if set."""
    return _project_root


def set_panel_dirty(
    panel_id: str,
    is_dirty: bool,
    *,
    title: str = "",
    save_handler: Optional[Callable[[], Any]] = None,
    save_pending_handler: Optional[Callable[[], bool]] = None,
    discard_handler: Optional[Callable[[], Any]] = None,
) -> None:
    """Compatibility adapter for panels not yet bound as real documents."""
    pid = (panel_id or "").strip()
    if not pid:
        return
    from Infernux.engine.interaction import DocumentRegistry

    registry = DocumentRegistry.instance()
    document = _legacy_panel_document(pid, create=True, title=title)
    controller = document.controller
    if not isinstance(controller, _LegacyPanelDocumentController):
        raise TypeError(f"panel view '{pid}' is bound to a non-legacy document")
    if save_handler is not None:
        controller.save_handler = save_handler
    if save_pending_handler is not None:
        controller.save_pending_handler = save_pending_handler
    if discard_handler is not None:
        controller.discard_handler = discard_handler
    registry.update_metadata(
        document.document_id,
        title=(title or document.title),
        capabilities=_legacy_capabilities(controller),
    )
    if is_dirty and not document.is_dirty:
        registry.mark_changed(document.document_id)
    elif not is_dirty and document.is_dirty:
        ticket = registry.active_save_ticket(document.document_id)
        if ticket is not None:
            registry.complete_save(ticket.ticket_id, success=True)
        else:
            registry.mark_saved(document.document_id)


def is_panel_dirty(panel_id: str) -> bool:
    """Return whether a panel is currently marked dirty."""
    pid = (panel_id or "").strip()
    if not pid:
        return False
    document = _legacy_panel_document(pid)
    return bool(document and document.is_dirty)


def any_panel_dirty() -> bool:
    """Return whether any editor panel currently has unsaved changes."""
    from Infernux.engine.interaction import DocumentRegistry

    return bool(DocumentRegistry.instance().dirty_documents())


def get_dirty_panels() -> list[str]:
    """Return IDs of all panels currently marked dirty."""
    from Infernux.engine.interaction import DocumentRegistry

    result: list[str] = []
    for document in DocumentRegistry.instance().dirty_documents():
        result.extend(sorted(document.view_ids))
    return result


def set_panel_save_handler(panel_id: str, save_handler: Optional[Callable[[], Any]]) -> None:
    """Set or clear the save callback used by unified dirty confirmation."""
    pid = (panel_id or "").strip()
    if not pid:
        return
    from Infernux.engine.interaction import DocumentRegistry

    document = _legacy_panel_document(pid, create=True)
    controller = document.controller
    if not isinstance(controller, _LegacyPanelDocumentController):
        raise TypeError(f"panel view '{pid}' is bound to a non-legacy document")
    controller.save_handler = save_handler
    DocumentRegistry.instance().update_metadata(
        document.document_id,
        capabilities=_legacy_capabilities(controller),
    )


def set_panel_title(panel_id: str, title: str) -> None:
    """Set display title for a panel in unified dirty confirmation dialogs."""
    pid = (panel_id or "").strip()
    ttl = (title or "").strip()
    if not pid or not ttl:
        return
    from Infernux.engine.interaction import DocumentRegistry

    document = _legacy_panel_document(pid, create=True, title=ttl)
    DocumentRegistry.instance().update_metadata(document.document_id, title=ttl)


def clear_panel_tracking(panel_id: str) -> None:
    """Remove all dirty tracking metadata for a panel."""
    pid = (panel_id or "").strip()
    if not pid:
        return
    from Infernux.engine.interaction import DocumentRegistry

    registry = DocumentRegistry.instance()
    document_id = registry.detach_view(pid)
    if document_id:
        document = registry.get(document_id)
        if document is not None and not document.view_ids:
            registry.unregister(document_id)


def get_dirty_panel_entries() -> list[dict]:
    """Return legacy view records backed by the authoritative registry."""
    from Infernux.engine.interaction import DocumentRegistry

    registry = DocumentRegistry.instance()
    entries: list[dict] = []
    for document in registry.dirty_documents():
        if not document.view_ids:
            continue
        for pid in sorted(document.view_ids):
            entries.append({
                "panel_id": pid,
                "document_id": document.document_id,
                "title": document.title,
                "save_handler": lambda did=document.document_id: registry.request_save(did).accepted,
                "save_pending_handler": lambda did=document.document_id: registry.is_save_pending(did),
                "discard_handler": lambda did=document.document_id: registry.request_discard(did).accepted,
            })
    return entries


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
