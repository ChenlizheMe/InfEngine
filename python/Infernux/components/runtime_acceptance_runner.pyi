from Infernux.components.component import InxComponent

class RuntimeAcceptanceRunner(InxComponent):
    manifest_path: str
    result_path: str
    def start(self) -> None: ...
    def update(self, delta_time: float) -> None: ...
