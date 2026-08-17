"""
MeshCollider — Python BuiltinComponent wrapper for C++ MeshCollider.

Requires sibling ``MeshRenderer`` geometry. Static and kinematic bodies may
use triangle-mesh collision; switching to a dynamic Rigidbody sets the public
``convex`` property before rebuilding the shape.
"""

from __future__ import annotations

from Infernux.components.builtin.collider import Collider
from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType


class MeshCollider(Collider):
    """Python wrapper for the C++ MeshCollider component."""

    _cpp_type_name = "MeshCollider"

    convex = CppProperty(
        "convex",
        FieldType.BOOL,
        default=False,
        tooltip="Use convex hull collision. Required for dynamic rigidbodies.",
    )
    shape_error = CppProperty(
        "shape_error",
        FieldType.STRING,
        default="",
        readonly=True,
    )
    is_cooking = CppProperty(
        "is_cooking",
        FieldType.BOOL,
        default=False,
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Custom inspector: force-check convex when dynamic Rigidbody exists
    # ------------------------------------------------------------------

    def render_inspector(self, ctx) -> None:
        from Infernux.engine.ui.inspector_components import render_builtin_via_setters
        from Infernux.engine.ui.inspector_utils import render_inspector_checkbox

        go = self.game_object
        rb = go.get_component('Rigidbody')
        forced_convex = rb is not None and not rb.is_kinematic

        if forced_convex:
            render_builtin_via_setters(ctx, self, type(self), skip_fields={'convex', 'shape_error', 'is_cooking'})
            ctx.begin_disabled(True)
            render_inspector_checkbox(ctx, "Convex", self.convex)
            ctx.end_disabled()
        else:
            render_builtin_via_setters(ctx, self, type(self), skip_fields={'shape_error', 'is_cooking'})

        shape_error = self.shape_error
        if shape_error:
            from Infernux.engine.ui.theme import ImGuiCol, Theme
            ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx.text_wrapped(shape_error)
            ctx.pop_style_color(1)
