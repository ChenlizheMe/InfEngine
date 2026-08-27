"""Python facade for the native camera-facing LineRenderer component."""

from __future__ import annotations

from Infernux.components.builtin.mesh_renderer import MeshRenderer
from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType


def _vec4_to_color(value):
    return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]


def _color_to_vec4(value):
    from Infernux.lib import vec4f

    if isinstance(value, (list, tuple)) and len(value) >= 4:
        return vec4f(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    return value


class LineRenderer(MeshRenderer):
    """Draw a continuous 3D ribbon through an ordered list of positions."""

    _cpp_type_name = "LineRenderer"
    _component_category_ = "Rendering"
    _component_menu_path_ = "Rendering/Line Renderer"
    _display_name_key = "component.line_renderer"

    position_count = CppProperty(
        "position_count",
        FieldType.INT,
        default=2,
        range=(0, 1_000_000),
        tooltip="Number of control points in the line",
    )
    start_width = CppProperty(
        "start_width", FieldType.FLOAT, default=0.1, range=(0.0, 1000.0)
    )
    end_width = CppProperty(
        "end_width", FieldType.FLOAT, default=0.1, range=(0.0, 1000.0)
    )
    width_multiplier = CppProperty(
        "width_multiplier", FieldType.FLOAT, default=1.0, range=(0.0, 1000.0)
    )
    start_color = CppProperty(
        "start_color",
        FieldType.COLOR,
        default=[1.0, 1.0, 1.0, 1.0],
        get_converter=_vec4_to_color,
        set_converter=_color_to_vec4,
    )
    end_color = CppProperty(
        "end_color",
        FieldType.COLOR,
        default=[1.0, 1.0, 1.0, 1.0],
        get_converter=_vec4_to_color,
        set_converter=_color_to_vec4,
    )
    loop = CppProperty("loop", FieldType.BOOL, default=False)
    use_world_space = CppProperty("use_world_space", FieldType.BOOL, default=False)
    alignment = CppProperty(
        "alignment",
        FieldType.ENUM,
        default=None,
        enum_type="LineAlignment",
        enum_labels=["View", "Transform Z"],
    )
    texture_mode = CppProperty(
        "texture_mode",
        FieldType.ENUM,
        default=None,
        enum_type="LineTextureMode",
        enum_labels=["Stretch", "Tile"],
    )
    texture_scale = CppProperty(
        "texture_scale", FieldType.FLOAT, default=1.0, range=(0.0, 1000.0)
    )

    @property
    def positions(self):
        return list(self._require_cpp_component().get_positions())

    @positions.setter
    def positions(self, values):
        from Infernux.lib import Vector3

        converted = []
        for value in values:
            if isinstance(value, Vector3):
                converted.append(value)
            else:
                converted.append(Vector3(float(value[0]), float(value[1]), float(value[2])))
        self._require_cpp_component().set_positions(converted)

    def get_position(self, index: int):
        return self._require_cpp_component().get_position(index)

    def set_position(self, index: int, position) -> None:
        from Infernux.lib import Vector3

        if not isinstance(position, Vector3):
            position = Vector3(float(position[0]), float(position[1]), float(position[2]))
        self._require_cpp_component().set_position(index, position)

    def set_positions(self, positions) -> None:
        self.positions = positions

    def get_positions(self):
        return self.positions

    def simplify(self, tolerance: float) -> None:
        self._require_cpp_component().simplify(float(tolerance))

    def render_inspector(self, ctx) -> None:
        from Infernux.engine.ui._inspector_undo import _record_property
        from Infernux.engine.ui.inspector_components import render_builtin_via_setters
        from Infernux.engine.ui.inspector_utils import (
            DRAG_SPEED_DEFAULT,
            float_close,
            max_label_w,
        )

        render_builtin_via_setters(ctx, self, type(self))
        if not ctx.collapsing_header("Positions"):
            return

        old_positions = self.positions
        label_width = max_label_w(ctx, [f"Position {index}" for index in range(len(old_positions))])
        new_positions = list(old_positions)
        changed = False
        for index, position in enumerate(old_positions):
            value = ctx.vector3(
                f"Position {index}",
                float(position[0]),
                float(position[1]),
                float(position[2]),
                DRAG_SPEED_DEFAULT,
                label_width,
            )
            candidate = tuple(float(channel) for channel in value)
            original = tuple(float(position[channel]) for channel in range(3))
            if any(not float_close(a, b) for a, b in zip(candidate, original)):
                new_positions[index] = candidate
                changed = True
        if changed:
            _record_property(self, "positions", old_positions, new_positions, "Set Line Positions")
