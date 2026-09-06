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


def _get_width_curve(cpp):
    from Infernux.graph.ramp import AnimationCurve, Keyframe

    wrap_names = ("clamp", "repeat", "ping_pong")
    return AnimationCurve(
        tuple(
            Keyframe(key.time, key.value, key.in_tangent, key.out_tangent)
            for key in cpp.width_curve
        ),
        wrap_names[int(cpp.width_curve_pre_wrap)],
        wrap_names[int(cpp.width_curve_post_wrap)],
    )


def _set_width_curve(cpp, value) -> None:
    from Infernux.graph.ramp import AnimationCurve
    from Infernux.lib import LineCurveWrapMode, LineWidthKey

    curve = (
        value
        if isinstance(value, AnimationCurve)
        else AnimationCurve.from_dict(value)
    )
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


def _get_color_gradient(cpp):
    from Infernux.graph.ramp import Gradient, GradientKey

    return Gradient(
        tuple(
            GradientKey(key.time, tuple(float(channel) for channel in key.color))
            for key in cpp.color_gradient
        ),
        ("linear", "fixed", "perceptual_blend")[int(cpp.color_gradient_mode)],
    )


def _set_color_gradient(cpp, value) -> None:
    from Infernux.graph.ramp import Gradient
    from Infernux.lib import LineColorKey, LineGradientMode

    gradient = value if isinstance(value, Gradient) else Gradient.from_dict(value)
    cpp.color_gradient = [
        LineColorKey(key.time, _color_to_vec4(key.color)) for key in gradient.keys
    ]
    cpp.color_gradient_mode = {
        "linear": LineGradientMode.Linear,
        "fixed": LineGradientMode.Fixed,
        "perceptual_blend": LineGradientMode.PerceptualBlend,
    }[gradient.mode]


def _get_positions(cpp):
    return list(cpp.get_positions())


def _set_positions(cpp, values) -> None:
    from Infernux.lib import Vector3

    cpp.set_positions(
        [
            value
            if isinstance(value, Vector3)
            else Vector3(float(value[0]), float(value[1]), float(value[2]))
            for value in values
        ]
    )


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
    positions = CppProperty(
        "positions",
        FieldType.LIST,
        default=[],
        element_type=FieldType.VEC3,
        native_getter=_get_positions,
        native_setter=_set_positions,
    )
    width_multiplier = CppProperty(
        "width_multiplier", FieldType.FLOAT, default=1.0, range=(0.0, 1000.0)
    )
    width_curve = CppProperty(
        "width_curve",
        FieldType.ANIMATION_CURVE,
        default=None,
        native_getter=_get_width_curve,
        native_setter=_set_width_curve,
        curve_non_negative=True,
    )
    color_gradient = CppProperty(
        "color_gradient",
        FieldType.GRADIENT,
        default=None,
        native_getter=_get_color_gradient,
        native_setter=_set_color_gradient,
        hdr=True,
    )
    loop = CppProperty("loop", FieldType.BOOL, default=False)
    use_world_space = CppProperty("use_world_space", FieldType.BOOL, default=True)
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
