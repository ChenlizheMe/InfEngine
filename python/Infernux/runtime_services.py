"""Small process-local registry for platform-owned runtime capabilities."""

from __future__ import annotations

from threading import RLock
from typing import Any


_lock = RLock()
_services: dict[str, Any] = {}


def install_runtime_service(name: str, service: Any) -> None:
    identity = str(name).strip().casefold()
    if not identity or service is None:
        raise ValueError("runtime service name and implementation are required")
    with _lock:
        existing = _services.get(identity)
        if existing is not None and existing is not service:
            raise RuntimeError(f"runtime service is already installed: {identity}")
        _services[identity] = service


def get_runtime_service(name: str) -> Any | None:
    identity = str(name).strip().casefold()
    if not identity:
        return None
    with _lock:
        return _services.get(identity)


def remove_runtime_service(name: str, service: Any | None = None) -> bool:
    identity = str(name).strip().casefold()
    if not identity:
        return False
    with _lock:
        existing = _services.get(identity)
        if existing is None or (service is not None and existing is not service):
            return False
        del _services[identity]
        return True


__all__ = [
    "get_runtime_service",
    "install_runtime_service",
    "remove_runtime_service",
]
