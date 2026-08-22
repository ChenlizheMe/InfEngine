from .contexts import FocusService
from .modals import ModalService
from .panels import ExternalDropKind, PanelInteractionRegistry

class ExternalDropStatus(str):
    ACCEPTED: ExternalDropStatus
    UNKNOWN_VIEW: ExternalDropStatus
    UNSUPPORTED: ExternalDropStatus
    HIDDEN: ExternalDropStatus
    NOT_TARGETED: ExternalDropStatus
    MODAL_BLOCKED: ExternalDropStatus
    CAPTURE_BLOCKED: ExternalDropStatus
    INPUT_CONTEXT_BLOCKED: ExternalDropStatus

class ExternalDropDecision:
    status: ExternalDropStatus
    view_id: str
    type_id: str
    @property
    def accepted(self) -> bool: ...

class ExternalDropTargetService:
    def __init__(self, focus: FocusService, modals: ModalService, panels: PanelInteractionRegistry) -> None: ...
    def evaluate(self, view_id: str, kind: ExternalDropKind) -> ExternalDropDecision: ...
    def accepts(self, view_id: str, kind: ExternalDropKind) -> bool: ...
