from Infernux.engine.interaction.command_palette import CommandPaletteService

class CommandPalettePresenter:
    def __init__(self, service: CommandPaletteService, modal_service: object) -> None: ...
    def render(self, ctx: object) -> None: ...

__all__: list[str]
