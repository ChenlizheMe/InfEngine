from enum import IntEnum

from Infernux.renderstack.fullscreen_effect import FullScreenEffect


class PixelationSampling(IntEnum):
    CENTER: int
    BALANCED: int
    HIGH_QUALITY: int


class PixelationEffect(FullScreenEffect):
    name: str
    injection_point: str
    default_order: int
    menu_path: str
    pixel_size: int
    pixel_aspect: float
    grid_offset_x: float
    grid_offset_y: float
    intensity: float
    sampling: PixelationSampling
    preserve_alpha_coverage: bool
    def set_params_dict(self, params) -> None: ...
    def get_shader_list(self): ...
    def setup_passes(self, graph, bus) -> None: ...
