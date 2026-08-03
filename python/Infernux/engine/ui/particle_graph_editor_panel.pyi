from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
from Infernux.particle.asset import ParticleGraphAsset


class ParticleGraphEditorPanel(NodeGraphEditorPanel):
    window_id: str
    @property
    def asset(self) -> ParticleGraphAsset: ...
    def _open_particlegraph(self, file_path: str) -> bool: ...
    def handle_save_command(self, save_as: bool = False) -> bool: ...
