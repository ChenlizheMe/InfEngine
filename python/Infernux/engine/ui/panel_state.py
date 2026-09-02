"""
Panel state persistence — saves / loads per-panel settings to a JSON file
inside the project's layout directory (Documents/Infernux/{project}/).
"""
import json
import os
import threading

_state_path: str = ""
_state: dict = {}
_lock = threading.Lock()


def init(layout_dir: str) -> None:
    """Set the directory for panel_state.json and load existing state."""
    global _state_path, _state
    _state_path = os.path.join(layout_dir, "panel_state.json")
    if os.path.isfile(_state_path):
        with open(_state_path, "r", encoding="utf-8") as f:
            loaded_state = json.load(f)
        if not isinstance(loaded_state, dict):
            raise ValueError("panel_state.json must contain a JSON object")
        _state = loaded_state
    else:
        _state = {}


def get(panel_id: str) -> dict:
    """Return the saved state dict for a panel, or empty dict."""
    with _lock:
        return dict(_state.get(panel_id, {}))


def put(panel_id: str, data: dict) -> None:
    """Update the state for a panel (merged)."""
    with _lock:
        _state[panel_id] = data


def delete(panel_id: str) -> None:
    """Remove a panel state entry if it exists."""
    with _lock:
        _state.pop(panel_id, None)


def keys(prefix: str = "") -> tuple[str, ...]:
    """Return a stable snapshot of persisted state keys."""
    requested_prefix = str(prefix or "")
    with _lock:
        values = tuple(str(key) for key in _state)
    if not requested_prefix:
        return values
    return tuple(key for key in values if key.startswith(requested_prefix))


def prune_document_view_states(
    *,
    is_document_backed,
    has_restorable_document,
) -> tuple[str, ...]:
    """Remove panel payloads that have no authoritative document snapshot.

    Document-backed panels may persist presentation state, but their authoring
    content belongs exclusively to ``DocumentRegistry``. Keeping a private
    panel payload after its document was discarded could otherwise resurrect
    an unsaved draft during the next Editor startup.
    """
    if not callable(is_document_backed) or not callable(has_restorable_document):
        raise TypeError("document panel pruning requires callable predicates")
    with _lock:
        candidates = tuple(str(key) for key in _state if str(key).startswith("panel:"))
    removable: list[tuple[str, str]] = []
    for key in candidates:
        view_id = key[len("panel:") :]
        if not view_id or not bool(is_document_backed(view_id)):
            continue
        if bool(has_restorable_document(view_id)):
            continue
        removable.append((key, view_id))
    with _lock:
        removed = tuple(
            view_id
            for key, view_id in removable
            if _state.pop(key, None) is not None
        )
    return removed


def save() -> None:
    """Write the current state to disk."""
    if not _state_path:
        return
    with _lock:
        snapshot = dict(_state)
    os.makedirs(os.path.dirname(_state_path), exist_ok=True)
    from Infernux.core.document_store import write_document_text
    write_document_text(
        _state_path,
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
    )
