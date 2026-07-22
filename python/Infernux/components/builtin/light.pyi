from __future__ import annotations

from typing import Any, List, Optional, Union

from Infernux.components.builtin_component import BuiltinComponent

class Light(BuiltinComponent):
    """A Light component that illuminates the scene."""

    _cpp_type_name: str
    _component_category_: str
    _always_show: bool
    _gizmo_icon_color: tuple[float, float, float]

    # ---- CppProperty fields as properties ----

    @property
    def light_type(self) -> int:
        """The type of light (Directional, Point, or Spot)."""
        ...
    @light_type.setter
    def light_type(self, value: int) -> None: ...

    @property
    def color(self) -> List[float]:
        """The color of the light."""
        ...
    @color.setter
    def color(self, value: Union[List[float], tuple[float, ...]]) -> None: ...

    @property
    def intensity(self) -> float:
        """The brightness of the light."""
        ...
    @intensity.setter
    def intensity(self, value: float) -> None: ...

    @property
    def range(self) -> float:
        """The range of the light in world units."""
        ...
    @range.setter
    def range(self, value: float) -> None: ...

    @property
    def spot_angle(self) -> float:
        """The inner cone angle of the spot light in degrees."""
        ...
    @spot_angle.setter
    def spot_angle(self, value: float) -> None: ...

    @property
    def outer_spot_angle(self) -> float:
        """The outer cone angle of the spot light in degrees."""
        ...
    @outer_spot_angle.setter
    def outer_spot_angle(self, value: float) -> None: ...

    @property
    def shadows(self) -> int:
        """The shadow casting mode of the light."""
        ...
    @shadows.setter
    def shadows(self, value: int) -> None: ...

    @property
    def shadow_strength(self) -> float:
        """The strength of the shadows cast by this light."""
        ...
    @shadow_strength.setter
    def shadow_strength(self, value: float) -> None: ...

    @property
    def shadow_bias(self) -> float:
        """Bias value to reduce shadow acne artifacts."""
        ...
    @shadow_bias.setter
    def shadow_bias(self, value: float) -> None: ...

    @property
    def shadow_normal_bias(self) -> float: ...
    @shadow_normal_bias.setter
    def shadow_normal_bias(self, value: float) -> None: ...

    @property
    def affect_geometry(self) -> bool: ...
    @affect_geometry.setter
    def affect_geometry(self, value: bool) -> None: ...

    @property
    def affect_particles(self) -> bool: ...
    @affect_particles.setter
    def affect_particles(self, value: bool) -> None: ...

    # ---- Methods ----

    def get_light_view_matrix(self) -> Any:
        """Return the light's view matrix for shadow mapping."""
        ...
    def get_light_projection_matrix(
        self,
        shadow_extent: float = ...,
        near_plane: float = ...,
        far_plane: float = ...,
    ) -> Any:
        """Return the light's projection matrix for shadow mapping."""
        ...
    def serialize(self) -> str:
        """Serialize the component to a JSON string."""
        ...

    def on_draw_gizmos_selected(self) -> None:
        """Draw a type-specific gizmo when the light is selected."""
        ...
