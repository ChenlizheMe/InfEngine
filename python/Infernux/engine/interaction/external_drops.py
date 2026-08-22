"""Central ownership checks for pointer-targeted external editor drops."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .contexts import FocusService
from .modals import ModalService
from .panels import ExternalDropKind, PanelInteractionRegistry


class ExternalDropStatus(str, Enum):
    ACCEPTED = "accepted"
    UNKNOWN_VIEW = "unknown_view"
    UNSUPPORTED = "unsupported"
    HIDDEN = "hidden"
    NOT_TARGETED = "not_targeted"
    MODAL_BLOCKED = "modal_blocked"
    CAPTURE_BLOCKED = "capture_blocked"
    INPUT_CONTEXT_BLOCKED = "input_context_blocked"


@dataclass(frozen=True, slots=True)
class ExternalDropDecision:
    status: ExternalDropStatus
    view_id: str
    type_id: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is ExternalDropStatus.ACCEPTED


class ExternalDropTargetService:
    """Resolve one external drop target from shared editor interaction state.

    A panel type must declare the payload kind in its interaction descriptor.
    Its live view must then prove that its dock content is both presented and
    pointer-targeted in the current frame. Modal, explicit capture and blocking
    input contexts retain precedence over external OS gestures.
    """

    def __init__(
        self,
        focus: FocusService,
        modals: ModalService,
        panels: PanelInteractionRegistry,
    ) -> None:
        if not isinstance(focus, FocusService):
            raise TypeError("external drop targets require FocusService")
        if not isinstance(modals, ModalService):
            raise TypeError("external drop targets require ModalService")
        if not isinstance(panels, PanelInteractionRegistry):
            raise TypeError("external drop targets require PanelInteractionRegistry")
        self._focus = focus
        self._modals = modals
        self._panels = panels

    def evaluate(
        self,
        view_id: str,
        kind: ExternalDropKind,
    ) -> ExternalDropDecision:
        target_view = str(view_id or "").strip()
        target_kind = ExternalDropKind(kind)
        target_type = self._panels.type_id_for_view(target_view)
        instance = self._panels.instance_for_view(target_view)
        if not target_view or not target_type or instance is None:
            return ExternalDropDecision(
                ExternalDropStatus.UNKNOWN_VIEW,
                target_view,
                target_type,
            )
        if not self._panels.accepts_external_drop(target_view, target_kind):
            return ExternalDropDecision(
                ExternalDropStatus.UNSUPPORTED,
                target_view,
                target_type,
            )
        if not self._panel_state(instance, "is_content_visible"):
            return ExternalDropDecision(
                ExternalDropStatus.HIDDEN,
                target_view,
                target_type,
            )
        if not self._panel_state(instance, "is_content_hovered"):
            return ExternalDropDecision(
                ExternalDropStatus.NOT_TARGETED,
                target_view,
                target_type,
            )
        if self._modals.active_modal_id:
            return ExternalDropDecision(
                ExternalDropStatus.MODAL_BLOCKED,
                target_view,
                target_type,
            )

        snapshot = self._focus.snapshot
        if snapshot.capture_owner_id:
            return ExternalDropDecision(
                ExternalDropStatus.CAPTURE_BLOCKED,
                target_view,
                target_type,
            )

        target_owners = {target_view, target_type}
        for context in self._focus.input_contexts.ordered():
            if context.blocks_lower or context.owner_id not in target_owners:
                return ExternalDropDecision(
                    ExternalDropStatus.INPUT_CONTEXT_BLOCKED,
                    target_view,
                    target_type,
                )
        return ExternalDropDecision(
            ExternalDropStatus.ACCEPTED,
            target_view,
            target_type,
        )

    def accepts(self, view_id: str, kind: ExternalDropKind) -> bool:
        return self.evaluate(view_id, kind).accepted

    @staticmethod
    def _panel_state(instance: object, attribute: str) -> bool:
        value: Optional[object] = getattr(instance, attribute, None)
        try:
            return bool(value() if callable(value) else value)
        except (AttributeError, ReferenceError, RuntimeError):
            return False
