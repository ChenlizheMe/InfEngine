"""Context-menu creation callbacks for the Hierarchy panel."""

from __future__ import annotations

from Infernux.debug import Debug
from Infernux.engine.hierarchy_creation_service import (
    HierarchyCreationService,
    LIGHT_INDEX,
    PRIMITIVE_INDEX,
)


def wire_creation_callbacks(ctx):
    """Wire all object-creation callbacks onto the hierarchy panel."""
    hp = ctx.hp
    svc = HierarchyCreationService.instance()
    svc.configure(selection_manager=ctx.sel, undo_tracker=ctx.undo, hierarchy_panel=hp)

    def _create(kind: str, parent_id: int) -> None:
        try:
            svc.create(kind, parent_id=parent_id)
        except Exception as exc:
            Debug.log_error(f"Hierarchy create failed ({kind}): {exc}")

    hp.create_primitive = lambda type_idx, parent_id: _create(
        PRIMITIVE_INDEX.get(type_idx, ""), parent_id
    )
    hp.create_light = lambda type_idx, parent_id: _create(
        LIGHT_INDEX.get(type_idx, ""), parent_id
    )
    hp.create_empty = lambda parent_id: _create("empty", parent_id)

    def _create_empty_parent() -> None:
        try:
            ids = list(ctx.sel.get_ids()) if ctx.sel is not None else []
            svc.create_empty_parent(ids)
        except Exception as exc:
            Debug.log_error(f"Hierarchy create empty parent failed: {exc}")

    hp.create_empty_parent = _create_empty_parent

    # Data-driven entries for Hierarchy context menus.
    hp.clear_create_entries()
    hp.add_create_entry(
        "Camera",
        "hierarchy.camera",
        lambda parent_id: _create("rendering.camera", parent_id),
    )
    hp.add_create_entry(
        "PostProcessing",
        "hierarchy.render_stack",
        lambda parent_id: _create("rendering.render_stack", parent_id),
    )
    hp.add_create_entry(
        "2D",
        "hierarchy.sprite_renderer",
        lambda parent_id: _create("rendering.sprite_renderer", parent_id),
    )
    hp.add_create_entry(
        "Effect",
        "hierarchy.particle_system",
        lambda parent_id: _create("effect.particle_system", parent_id),
    )
    # Hierarchy UI create menu only offers Canvas; Image/Text/Button are
    # authored under a Canvas via the UI editor / Add Component.
    hp.add_create_entry(
        "UI",
        "hierarchy.ui_canvas",
        lambda parent_id: _create("ui.canvas", parent_id),
    )
