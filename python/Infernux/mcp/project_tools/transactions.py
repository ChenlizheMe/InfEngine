"""Best-effort transaction tracking for long-horizon MCP mutations."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from typing import Any

from Infernux.engine.path_utils import is_path_within, relative_path, resolved_path

_ACTIVE: "MCPTransaction | None" = None
_LAST: dict[str, Any] | None = None


class MCPTransaction:
    def __init__(self, project_path: str, label: str = "") -> None:
        self.project_path = resolved_path(project_path)
        self.transaction_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.label = str(label or "")
        self.started_at = time.time()
        self.events: list[dict[str, Any]] = []
        self._snapshots: dict[str, str] = {}
        self._root = os.path.join(self.project_path, ".infernux", "mcp_transactions", self.transaction_id)
        self._backup_root = os.path.join(self._root, "backups")

    def record_path_before_change(self, path: str, operation: str = "modify") -> None:
        file_path = self._resolve(path)
        rel_path = self._rel(file_path)
        if rel_path in self._snapshots:
            return
        exists = os.path.exists(file_path)
        backup_path = ""
        kind = "created"
        if exists:
            kind = "modified" if operation != "delete" else "deleted"
            backup_path = self._snapshot(file_path, rel_path)
        self._snapshots[rel_path] = backup_path
        self.events.append({
            "type": kind,
            "operation": str(operation or ""),
            "path": rel_path,
            "backup": self._rel(backup_path) if backup_path else "",
            "directory": bool(os.path.isdir(file_path)) if exists else False,
        })

    def status(self) -> dict[str, Any]:
        return {
            "active": True,
            "transaction_id": self.transaction_id,
            "label": self.label,
            "started_at": self.started_at,
            "elapsed_seconds": max(0.0, time.time() - self.started_at),
            "tracked_path_count": len(self.events),
            "events": list(self.events),
            "notes": [
                "File and asset mutations are restorable when tools record their paths.",
                "Scene hierarchy changes rely on editor undo support and may require explicit undo tools in future iterations.",
            ],
        }

    def commit(self) -> dict[str, Any]:
        return {
            **self.status(),
            "active": False,
            "committed": True,
            "ended_at": time.time(),
        }

    def rollback(self) -> dict[str, Any]:
        restored = []
        removed = []
        failures = []
        for event in reversed(self.events):
            try:
                target = self._resolve(event["path"])
                if event["type"] == "created":
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                        removed.append(event["path"])
                    elif os.path.isfile(target):
                        os.remove(target)
                        removed.append(event["path"])
                else:
                    backup = event.get("backup") or ""
                    if not backup:
                        continue
                    backup_path = self._resolve(backup)
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    elif os.path.isfile(target):
                        os.remove(target)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if os.path.isdir(backup_path):
                        shutil.copytree(backup_path, target)
                    else:
                        shutil.copy2(backup_path, target)
                    restored.append(event["path"])
            except Exception as exc:
                failures.append({"path": event.get("path", ""), "error": f"{type(exc).__name__}: {exc}"})
        return {
            **self.status(),
            "active": False,
            "rolled_back": not failures,
            "restored": restored,
            "removed": removed,
            "failures": failures,
            "ended_at": time.time(),
        }

    def _snapshot(self, file_path: str, rel_path: str) -> str:
        digest = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]
        backup_path = os.path.join(self._backup_root, digest)
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        if os.path.isdir(file_path):
            shutil.copytree(file_path, backup_path)
        else:
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            shutil.copy2(file_path, backup_path)
        return backup_path

    def _resolve(self, path: str) -> str:
        raw = resolved_path(path if os.path.isabs(path) else os.path.join(self.project_path, path))
        if not is_path_within(raw, self.project_path):
            raise ValueError("Transaction path must stay inside the project.")
        return raw

    def _rel(self, path: str) -> str:
        return relative_path(path, self.project_path, allow_root=True)


def begin(project_path: str, label: str = "") -> dict[str, Any]:
    global _ACTIVE
    if _ACTIVE is not None:
        raise RuntimeError(f"Transaction already active: {_ACTIVE.transaction_id}")
    _ACTIVE = MCPTransaction(project_path, label=label)
    return _ACTIVE.status()


def status() -> dict[str, Any]:
    if _ACTIVE is None:
        return {"active": False, "last": _LAST}
    return _ACTIVE.status()


def commit() -> dict[str, Any]:
    global _ACTIVE, _LAST
    if _ACTIVE is None:
        return {"active": False, "committed": False, "message": "No active transaction."}
    result = _ACTIVE.commit()
    _LAST = result
    _ACTIVE = None
    return result


def rollback() -> dict[str, Any]:
    global _ACTIVE, _LAST
    if _ACTIVE is None:
        return {"active": False, "rolled_back": False, "message": "No active transaction."}
    result = _ACTIVE.rollback()
    _LAST = result
    _ACTIVE = None
    return result


def record_path_before_change(project_path: str, path: str, operation: str = "modify") -> None:
    if _ACTIVE is None:
        return
    if not is_path_within(project_path, _ACTIVE.project_path):
        return
    _ACTIVE.record_path_before_change(path, operation=operation)
