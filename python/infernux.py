"""Public, lower-case Infernux scripting namespace.

Game scripts should use ``import infernux as inx`` and access engine types
through that explicit namespace.  The implementation continues to live in
the historical ``Infernux`` package while the engine is in active migration.
"""

from __future__ import annotations

import importlib

import Infernux as _api

_SUBMODULES = (
    "components",
    "core",
    "input",
    "rendergraph",
    "renderstack",
    "scene",
    "ui",
)

__all__ = tuple(dict.fromkeys((*_api.__all__, *_SUBMODULES)))
__version__ = _api.__version__

for _name in __all__:
    if _name not in _SUBMODULES:
        globals()[_name] = getattr(_api, _name)


def __getattr__(name: str):
    if name in _SUBMODULES:
        module = importlib.import_module(f"Infernux.{name}")
        globals()[name] = module
        return module
    return getattr(_api, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_api)))
