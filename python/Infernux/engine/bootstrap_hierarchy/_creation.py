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
        "UI",
        "hierarchy.ui_canvas",
        lambda parent_id: _create("ui.canvas", parent_id),
    )
    hp.add_create_entry(
        "UI",
        "hierarchy.ui_image",
        lambda parent_id: _create("ui.image", parent_id),
    )
    hp.add_create_entry(
        "UI",
        "hierarchy.ui_text",
        lambda parent_id: _create("ui.text", parent_id),
    )
    hp.add_create_entry(
        "UI",
        "hierarchy.ui_button",
        lambda parent_id: _create("ui.button", parent_id),
    )
