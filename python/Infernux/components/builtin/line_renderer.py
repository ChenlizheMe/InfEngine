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


def _vec2_to_list(value):
    return [float(value[0]), float(value[1])]


def _list_to_vec2(value):
    from Infernux.lib import Vector2

    if isinstance(value, (list, tuple)):
        return Vector2(float(value[0]), float(value[1]))
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
    width_multiplier = CppProperty(
        "width_multiplier", FieldType.FLOAT, default=1.0, range=(0.0, 1000.0)
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
        enum_labels=[
            "Stretch",
            "Tile",
            "Distribute Per Segment",
            "Repeat Per Segment",
            "Static",
        ],
    )
    texture_scale = CppProperty(
        "texture_scale",
        FieldType.VEC2,
        default=[1.0, 1.0],
        get_converter=_vec2_to_list,
        set_converter=_list_to_vec2,
    )
    num_corner_vertices = CppProperty(
        "num_corner_vertices", FieldType.INT, default=0, range=(0, 1024)
    )
    num_cap_vertices = CppProperty(
        "num_cap_vertices", FieldType.INT, default=0, range=(0, 1024)
    )
    shadow_bias = CppProperty(
        "shadow_bias", FieldType.FLOAT, default=0.5, range=(0.0, 10.0)
    )
    generate_lighting_data = CppProperty(
        "generate_lighting_data", FieldType.BOOL, default=False
    )

    @property
    def start_width(self) -> float:
        return float(self._require_cpp_component().start_width)

    @start_width.setter
    def start_width(self, value: float) -> None:
        self._require_cpp_component().start_width = float(value)

    @property
    def end_width(self) -> float:
        return float(self._require_cpp_component().end_width)

    @end_width.setter
    def end_width(self, value: float) -> None:
        self._require_cpp_component().end_width = float(value)

    @property
    def start_color(self):
        return _vec4_to_color(self._require_cpp_component().start_color)

    @start_color.setter
    def start_color(self, value) -> None:
        self._require_cpp_component().start_color = _color_to_vec4(value)

    @property
    def end_color(self):
        return _vec4_to_color(self._require_cpp_component().end_color)

    @end_color.setter
    def end_color(self, value) -> None:
        self._require_cpp_component().end_color = _color_to_vec4(value)

    @property
    def width_curve(self):
        from Infernux.graph.ramp import Curve, CurveKey

        cpp = self._require_cpp_component()
        wrap_names = ("clamp", "repeat", "ping_pong")
        keys = tuple(
            CurveKey(key.time, key.value, key.in_tangent, key.out_tangent)
            for key in cpp.width_curve
        )
        return Curve(
            keys,
            wrap_names[int(cpp.width_curve_pre_wrap)],
            wrap_names[int(cpp.width_curve_post_wrap)],
        )

    @width_curve.setter
    def width_curve(self, value) -> None:
        from Infernux.graph.ramp import Curve
        from Infernux.lib import LineCurveWrapMode, LineWidthKey

        curve = value if isinstance(value, Curve) else Curve.from_dict(value)
        cpp = self._require_cpp_component()
        cpp.width_curve = [
            LineWidthKey(key.time, key.value, key.in_tangent, key.out_tangent)
            for key in curve.keys
        ]
        modes = (
            LineCurveWrapMode.Clamp,
            LineCurveWrapMode.Repeat,
            LineCurveWrapMode.PingPong,
        )
        wrap_names = ("clamp", "repeat", "ping_pong")
        cpp.width_curve_pre_wrap = modes[wrap_names.index(curve.pre_wrap)]
        cpp.width_curve_post_wrap = modes[wrap_names.index(curve.post_wrap)]

    @property
    def color_gradient(self):
        from Infernux.graph.ramp import Gradient, GradientKey

        cpp = self._require_cpp_component()
        keys = tuple(
            GradientKey(key.time, tuple(float(channel) for channel in key.color))
            for key in cpp.color_gradient
        )
        return Gradient(
            keys,
            ("linear", "fixed", "perceptual_blend")[
                int(cpp.color_gradient_mode)
            ],
        )

    @color_gradient.setter
    def color_gradient(self, value) -> None:
        from Infernux.graph.ramp import Gradient
        from Infernux.lib import LineColorKey, LineGradientMode

        gradient = value if isinstance(value, Gradient) else Gradient.from_dict(value)
        cpp = self._require_cpp_component()
        cpp.color_gradient = [
            LineColorKey(key.time, _color_to_vec4(key.color)) for key in gradient.keys
        ]
        modes = {
            "linear": LineGradientMode.Linear,
            "fixed": LineGradientMode.Fixed,
            "perceptual_blend": LineGradientMode.PerceptualBlend,
        }
        cpp.color_gradient_mode = modes[gradient.mode]

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

    def bake_mesh(self, target, camera=None, use_transform: bool = False) -> None:
        """Bake a static snapshot into a MeshRenderer.

        ``camera`` controls billboard orientation when alignment is ``View``.
        ``use_transform`` includes this object's transform in the baked data.
        """
        target_cpp = getattr(target, "_cpp_component", target)
        camera_cpp = getattr(camera, "_cpp_component", camera)
        self._require_cpp_component().bake_mesh(
            target_cpp, camera_cpp, bool(use_transform)
        )

    def render_inspector(self, ctx) -> None:
        from Infernux.engine.ui._inspector_undo import _record_property
        from Infernux.engine.ui.inspector_components import render_builtin_via_setters
        from Infernux.engine.ui.inspector_utils import (
            DRAG_SPEED_DEFAULT,
            float_close,
            max_label_w,
        )

        from Infernux.engine.ui.particle_graph_editor_panel import (
            ParticleGraphEditorPanel,
        )

        render_builtin_via_setters(ctx, self, type(self))
        if ctx.collapsing_header("Width Curve"):
            old_curve = self.width_curve
            new_curve_document = ParticleGraphEditorPanel._render_curve_property(
                ctx,
                f"line_renderer_{self.component_id}",
                "width_curve",
                old_curve.to_dict(),
                semantic_prefix=f"inspector.line_renderer.{self.component_id}.width_curve",
            )
            if new_curve_document != old_curve.to_dict():
                from Infernux.graph.ramp import Curve

                _record_property(
                    self,
                    "width_curve",
                    old_curve,
                    Curve.from_dict(new_curve_document),
                    "Set Line Width Curve",
                )
        if ctx.collapsing_header("Color Gradient"):
            old_gradient = self.color_gradient
            new_gradient_document = ParticleGraphEditorPanel._render_gradient_property(
                ctx,
                f"line_renderer_{self.component_id}",
                "color_gradient",
                old_gradient.to_dict(),
                semantic_prefix=f"inspector.line_renderer.{self.component_id}.color_gradient",
            )
            if new_gradient_document != old_gradient.to_dict():
                from Infernux.graph.ramp import Gradient

                _record_property(
                    self,
                    "color_gradient",
                    old_gradient,
                    Gradient.from_dict(new_gradient_document),
                    "Set Line Color Gradient",
                )
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
