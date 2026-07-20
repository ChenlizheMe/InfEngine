"""
Unified Instantiate — Unity-style Object.Instantiate for all asset types.

Usage::

    from Infernux import Instantiate

    # Clone a GameObject (deep copy of hierarchy + components)
    clone = Instantiate(original_go)
    clone = Instantiate(original_go, parent=some_parent)

    # Clone a Material (deep copy of all properties)
    mat_clone = Instantiate(original_material)

    # Clone a Python Material wrapper
    from Infernux.core import Material
    mat = Material.create_lit("Gold")
    mat_clone = Instantiate(mat)
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


def Instantiate(original, parent=None):
    """Clone an object.  Dispatches by type — mirrors Unity's ``Object.Instantiate``.

    Supported types:

    * **GameObject** — deep-copies the hierarchy, all C++ components, and
      queues Python component restoration.  Optionally reparents under *parent*.
    * **Material** (Python wrapper) — deep-copies all shader properties,
      render state, and overrides.  Texture/shader references are shared.
    * **InxMaterial** (C++ native) — same as Material but returns the raw
      C++ ``InxMaterial`` object.
    * **GameObjectRef** / **PrefabRef** — delegates to their ``.instantiate()``
      method.

    Parameters
    ----------
    original : GameObject | Material | InxMaterial | GameObjectRef | PrefabRef
        The object to clone.
    parent : GameObject | None
        (GameObject only) Optional parent for the cloned object.

    Returns
    -------
    The cloned object (same wrapper type as *original*), or ``None`` on failure.

    Examples
    --------
    >>> clone = Instantiate(cube)
    >>> mat2 = Instantiate(gold_material)
    """
    if original is None:
        return None

    # ── Python Material wrapper ──────────────────────────────────────────
    from Infernux.core.material import Material
    if isinstance(original, Material):
        return original.clone()

    # ── C++ InxMaterial (raw) ────────────────────────────────────────────
    from Infernux.lib import InxMaterial
    if isinstance(original, InxMaterial):
        return original.clone()

    # ── C++ GameObject ───────────────────────────────────────────────────
    from Infernux.lib import GameObject
    if isinstance(original, GameObject):
        if parent is None:
            return GameObject.instantiate(original)
        return GameObject.instantiate(original, parent)

    # ── GameObjectRef / PrefabRef (have .instantiate()) ──────────────────
    if hasattr(original, "instantiate") and callable(original.instantiate):
        if parent is None:
            return original.instantiate()
        return original.instantiate(parent=parent)

    raise TypeError(
        f"Instantiate: unsupported type {type(original).__name__!r}. "
        f"Expected GameObject, Material, InxMaterial, GameObjectRef, or PrefabRef."
    )


def Destroy(obj, delay: float = 0.0):
    """Destroy a GameObject.  Mirrors Unity's ``Object.Destroy``.

    Parameters
    ----------
    obj : GameObject
        The GameObject to destroy.
    delay : float
        Unused (reserved for future delayed-destroy support).
    """
    from Infernux.lib import GameObject
    if isinstance(obj, GameObject):
        GameObject.destroy(obj)
    else:
        raise TypeError(
            f"Destroy: unsupported type {type(obj).__name__!r}. "
            f"Expected GameObject."
        )
