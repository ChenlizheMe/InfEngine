"""Type stubs for Infernux.renderstack.digital_glitch_effect."""

from typing import ClassVar

from Infernux.renderstack.fullscreen_effect import FullScreenEffect


class DigitalGlitchEffect(FullScreenEffect):
    name: ClassVar[str]
    injection_point: ClassVar[str]
    default_order: ClassVar[int]
    menu_path: ClassVar[str]

    intensity: float
    block_size: float
    color_shift: float
    scanline_strength: float
    def get_shader_list(self) -> list[str]: ...
    def setup_passes(self, graph, bus) -> None: ...
