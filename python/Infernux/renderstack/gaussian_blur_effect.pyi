from Infernux.renderstack.fullscreen_effect import FullScreenEffect

class GaussianBlurEffect(FullScreenEffect):
    radius: int
    sigma: float
    def get_shader_list(self) -> list[str]: ...
    def setup_passes(self, graph, bus) -> None: ...
