from __future__ import annotations

from Infernux.engine.interaction import (
    ExternalDropKind,
    ExternalDropStatus,
    ExternalDropTargetService,
    FocusService,
    InputContext,
    ModalService,
    PanelInteractionDescriptor,
    PanelInteractionRegistry,
)
from Infernux.engine.ui.core_panel_interactions import project_panel_interaction


class _Panel:
    def __init__(self, *, visible: bool = True, hovered: bool = True) -> None:
        self.visible = visible
        self.hovered = hovered

    def is_content_visible(self) -> bool:
        return self.visible

    def is_content_hovered(self) -> bool:
        return self.hovered


def _service(
    panel: _Panel,
    *,
    accepts_files: bool = True,
) -> tuple[ExternalDropTargetService, FocusService, ModalService]:
    focus = FocusService()
    modals = ModalService()
    panels = PanelInteractionRegistry()
    kinds = (
        frozenset({ExternalDropKind.FILES})
        if accepts_files
        else frozenset()
    )
    panels.register_type(
        "project",
        PanelInteractionDescriptor(external_drop_kinds=kinds),
    )
    panels.bind_view("project", "project", panel)
    return ExternalDropTargetService(focus, modals, panels), focus, modals


def test_external_drop_requires_declared_visible_pointer_target() -> None:
    panel = _Panel()
    service, _focus, _modals = _service(panel)

    assert service.evaluate("project", ExternalDropKind.FILES).accepted

    panel.visible = False
    assert (
        service.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.HIDDEN
    )

    panel.visible = True
    panel.hovered = False
    assert (
        service.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.NOT_TARGETED
    )

    unsupported, _focus, _modals = _service(_Panel(), accepts_files=False)
    assert (
        unsupported.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.UNSUPPORTED
    )
    assert (
        unsupported.evaluate("missing", ExternalDropKind.FILES).status
        is ExternalDropStatus.UNKNOWN_VIEW
    )


def test_external_drop_is_blocked_by_modal_capture_and_input_contexts() -> None:
    service, focus, modals = _service(_Panel())
    modals.register(
        "test.modal",
        is_active=lambda: True,
        render=lambda _ctx: None,
        cancel=lambda: None,
    )
    assert modals.activate("test.modal", owner_id="project")
    assert (
        service.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.MODAL_BLOCKED
    )

    modals.deactivate("test.modal")
    focus.set_capture_owner("scene_view")
    assert (
        service.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.CAPTURE_BLOCKED
    )

    focus.set_capture_owner("")
    focus.input_contexts.push(
        InputContext("scene.drag", "scene_view", priority=100)
    )
    assert (
        service.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.INPUT_CONTEXT_BLOCKED
    )

    focus.input_contexts.remove("scene.drag")
    focus.input_contexts.push(
        InputContext(
            "project.rename",
            "project",
            priority=100,
            blocks_lower=True,
        )
    )
    assert (
        service.evaluate("project", ExternalDropKind.FILES).status
        is ExternalDropStatus.INPUT_CONTEXT_BLOCKED
    )


def test_target_owned_nonblocking_input_context_keeps_drop_ownership() -> None:
    service, focus, _modals = _service(_Panel())
    focus.input_contexts.push(
        InputContext("project.browser", "project", priority=100)
    )

    assert service.accepts("project", ExternalDropKind.FILES)


def test_project_panel_descriptor_declares_external_file_drop() -> None:
    descriptor = project_panel_interaction(object(), object(), object())

    assert descriptor.external_drop_kinds == frozenset(
        {ExternalDropKind.FILES}
    )
