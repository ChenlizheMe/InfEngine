from collections.abc import Callable, Iterable
from dataclasses import dataclass
from Infernux.lib import InxGUIContext

@dataclass(frozen=True, slots=True)
class GraphDetailContributor:
    contributor_id: str
    priority: int
    is_active: Callable[[], bool]
    render: Callable[[InxGUIContext], None]

class GraphDetailHost:
    @staticmethod
    def ordered(contributors: Iterable[GraphDetailContributor]) -> tuple[GraphDetailContributor, ...]: ...
    @classmethod
    def render(cls, ctx: InxGUIContext, contributors: Iterable[GraphDetailContributor]) -> str: ...
