"""Context-menu creation callbacks for the Hierarchy panel."""

from __future__ import annotations

from Infernux.engine.hierarchy_creation_service import (
    HierarchyCreationService,
)


def wire_creation_callbacks(ctx):
    """Wire all object-creation callbacks onto the hierarchy panel."""
    svc = HierarchyCreationService.instance()
    svc.configure(
        selection_service=ctx.selection,
        navigation_service=ctx.bs.interaction_core.navigation,
    )
