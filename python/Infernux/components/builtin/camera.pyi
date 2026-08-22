from __future__ import annotations

from typing import Any, List, Optional, Tuple, Union

from Infernux.components.builtin_component import BuiltinComponent

class Camera(BuiltinComponent):
    """A Camera component that renders a view of the scene."""

    _cpp_type_name: str
    _component_category_: str
    _always_show: bool
    _gizmo_icon_color: tuple[float, float, float]

    # ---- CppProperty fields as properties ----

    @property
    def projection_mode(self) -> int:
        """The projection mode (0 = Perspective, 1 = Orthographic)."""
        ...
    @projection_mode.setter
    def projection_mode(self, value: int) -> None: ...

    @property
    def field_of_view(self) -> float:
        """The vertical field of view in degrees."""
        ...
    @field_of_view.setter
    def field_of_view(self, value: float) -> None: ...

    @property
    def orthographic_size(self) -> float:
        """Half-size of the camera in orthographic mode."""
        ...
    @orthographic_size.setter
    def orthographic_size(self, value: float) -> None: ...

    @property
    def aspect_ratio(self) -> float:
        """The aspect ratio of the camera (width / height)."""
        ...

    @property
    def near_clip(self) -> float:
        """The near clipping plane distance."""
        ...
    @near_clip.setter
    def near_clip(self, value: float) -> None: ...

    @property
    def far_clip(self) -> float:
        """The far clipping plane distance."""
        ...
    @far_clip.setter
    def far_clip(self, value: float) -> None: ...

    @property
    def depth(self) -> float:
        """The rendering order of the camera."""
        ...
    @depth.setter
    def depth(self, value: float) -> None: ...

    @property
    def culling_mask(self) -> int:
        """The layer mask used for culling objects."""
        ...

    @property
    def clear_flags(self) -> int:
        """How the camera clears the background before rendering."""
        ...
    @clear_flags.setter
    def clear_flags(self, value: int) -> None: ...

    @property
    def background_color(self) -> List[float]:
        """The background color used when clear flags is set to solid color."""
        ...
    @background_color.setter
    def background_color(self, value: Union[List[float], tuple[float, ...]]) -> None: ...

    @property
    def stop_nans(self) -> bool:
        """Whether invalid final-output pixels are replaced before display encoding."""
        ...
    @stop_nans.setter
    def stop_nans(self, value: bool) -> None: ...

    @property
    def dithering(self) -> bool:
        """Whether display-space dithering is applied before output quantization."""
        ...
    @dithering.setter
    def dithering(self, value: bool) -> None: ...

    # ---- Read-only delegate properties ----

    @property
    def pixel_width(self) -> int:
        """The width of the camera's render target in pixels."""
        ...
    @property
    def pixel_height(self) -> int:
        """The height of the camera's render target in pixels."""
        ...

    # ---- Coordinate conversion ----

    def screen_to_world_point(
        self, x: float, y: float, depth: float = ...
    ) -> Optional[Tuple[float, float, float]]:
        """Convert a screen-space point to world coordinates."""
        ...
    def world_to_screen_point(
        self, x: float, y: float, z: float
    ) -> Optional[Tuple[float, float]]:
        """Convert a world-space point to screen coordinates."""
        ...
    def screen_point_to_ray(
        self, x: float, y: float
    ) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
        """Cast a ray from a screen-space point into the scene."""
        ...

    # ---- Serialization ----

    def serialize(self) -> str:
        """Serialize the component to a JSON string."""
        ...
    def deserialize(self, json_str: str) -> bool:
        """Deserialize the component from a JSON string."""
        ...

    # ---- Gizmos ----

    def on_draw_gizmos_selected(self) -> None:
        """Draw the camera frustum gizmo when selected in the editor."""
        ...
