"""Public, lower-case Infernux scripting namespace.

Game scripts should use ``import infernux as inx`` and access engine types
through that explicit namespace.  The implementation continues to live in
the historical ``Infernux`` package while the engine is in active migration.
"""

from __future__ import annotations

import Infernux as _api

__all__ = tuple(_api.__all__)
__version__ = _api.__version__

for _name in __all__:
    globals()[_name] = getattr(_api, _name)


def __getattr__(name: str):
    return getattr(_api, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_api)))
