"""inspector_material — material property rendering for the Inspector."""

from __future__ import annotations


def render_material_property(
    ctx: object,
    prop_name: str,
    prop: object,
    ptype: str,
    value: object,
    plw: float,
    wid_prefix: str = "mp",
    reference_cache: object = None,
) -> bool:
    """Render a single material property widget.

    Returns:
        ``True`` if the value was changed by the user.
    """
    ...

def render_material_body(ctx: object, panel: object, state: object) -> None:
    """Render the full material inspector body (shader selector + properties)."""
    ...

def render_inline_material_body(
    ctx: object,
    panel: object,
    native_mat: object,
    cache_key: str | None = None,
) -> None:
    """Render a MeshRenderer-linked material using the shared material inspector UI."""
    ...
