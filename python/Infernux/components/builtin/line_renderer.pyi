from __future__ import annotations

from typing import Iterable, List, Sequence

from Infernux.graph.ramp import Curve, Gradient
from Infernux.lib import Camera, LineAlignment, LineTextureMode, Vector3

from .mesh_renderer import MeshRenderer

class LineRenderer(MeshRenderer):
    """Draw a continuous, configurable ribbon through 3D positions."""

    position_count: int
    width_multiplier: float
    start_width: float
    end_width: float
    start_color: List[float]
    end_color: List[float]
    width_curve: Curve
    color_gradient: Gradient
    loop: bool
    use_world_space: bool
    alignment: LineAlignment
    texture_mode: LineTextureMode
    texture_scale: List[float]
    num_corner_vertices: int
    num_cap_vertices: int
    shadow_bias: float
    generate_lighting_data: bool

    @property
    def positions(self) -> List[Vector3]: ...
    @positions.setter
    def positions(self, values: Iterable[Sequence[float]]) -> None: ...
    def get_position(self, index: int) -> Vector3: ...
    def set_position(self, index: int, position: Sequence[float]) -> None: ...
    def get_positions(self) -> List[Vector3]: ...
    def set_positions(self, positions: Iterable[Sequence[float]]) -> None: ...
    def simplify(self, tolerance: float) -> None: ...
    def bake_mesh(
        self,
        target: MeshRenderer,
        camera: Camera | None = ...,
        use_transform: bool = ...,
    ) -> None: ...
