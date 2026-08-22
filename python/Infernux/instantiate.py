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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def Instantiate(original, *args, **kwargs):
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
    *args, **kwargs
        Unity-style scalar overload arguments, or the native batch overload
        (``positions``, optional ``rotations``/``scales``, ``parent``, and
        ``return_objects``) for GameObjects.

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
        return GameObject.instantiate(original, *args, **kwargs)

    # ── GameObjectRef / PrefabRef (have .instantiate()) ──────────────────
    if hasattr(original, "instantiate") and callable(original.instantiate):
        return original.instantiate(*args, **kwargs)

    if args or kwargs:
        raise TypeError(
            f"Instantiate: overload arguments are not supported for "
            f"{type(original).__name__!r}."
        )

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
