from __future__ import annotations

from pathlib import Path

import pytest

from Infernux.engine.interaction import (
    AuthoringMutationService,
    DocumentKind,
    DocumentRegistry,
)
from Infernux.engine.undo import UndoCommand, UndoManager


class _AuthoringController:
    def __init__(self) -> None:
        self.state = {"items": [1]}

    def capture_authoring_snapshot(self):
        return {"items": list(self.state["items"])}

    def restore_authoring_snapshot(self, snapshot) -> None:
        self.state = {"items": list(snapshot["items"])}


class _SetValueCommand(UndoCommand):
    def __init__(self, target: dict, old_value: int, new_value: int) -> None:
        super().__init__("Set value")
        self._target = target
        self._old_value = old_value
        self._new_value = new_value

    def execute(self) -> None:
        self._target["value"] = self._new_value

    def undo(self) -> None:
        self._target["value"] = self._old_value


def test_explicit_authoring_mutation_records_only_committed_user_intent():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_service = AuthoringMutationService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller = _AuthoringController()
    document = registry.create(
        DocumentKind.ANIMATION_CLIP,
        "Clip",
        controller=controller,
    )
    service = AuthoringMutationService(registry)
    try:
        assert service.apply(
            document.document_id,
            "Add frame",
            lambda: controller.state["items"].append(2),
            view_id="clip-editor",
        )
        assert controller.state == {"items": [1, 2]}
        assert document.revision == 1
        assert len(manager.action_journal.entries) == 1

        manager.undo()
        assert controller.state == {"items": [1]}
        assert document.revision == 0
        manager.redo()
        assert controller.state == {"items": [1, 2]}
        assert document.revision == 1

        assert not service.apply(
            document.document_id,
            "No-op redraw",
            lambda: None,
            view_id="clip-editor",
        )
        assert len(manager.action_journal.entries) == 1
    finally:
        manager.clear()
        AuthoringMutationService._instance = previous_service
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry


def test_explicit_authoring_mutation_rolls_back_failed_callbacks():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_service = AuthoringMutationService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller = _AuthoringController()
    document = registry.create(
        DocumentKind.ANIMATION_CLIP,
        "Clip",
        controller=controller,
    )
    service = AuthoringMutationService(registry)

    def fail_after_mutation() -> None:
        controller.state["items"].append(2)
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            service.apply(
                document.document_id,
                "Broken edit",
                fail_after_mutation,
                view_id="clip-editor",
            )
        assert controller.state == {"items": [1]}
        assert document.revision == 0
        assert manager.action_journal.entries == ()
    finally:
        manager.clear()
        AuthoringMutationService._instance = previous_service
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry


def test_authoring_command_service_owns_revision_reservation_and_execution():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_service = AuthoringMutationService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller = _AuthoringController()
    document = registry.create(
        DocumentKind.TIMELINE,
        "Timeline",
        controller=controller,
    )
    service = AuthoringMutationService(registry)
    target = {"value": 1}
    revisions = []
    try:
        assert service.execute_command(
            document.document_id,
            lambda before, after: (
                revisions.append((before, after))
                or _SetValueCommand(target, 1, 2)
            ),
            view_id="timeline-editor",
        )
        assert revisions == [(0, 1)]
        assert target == {"value": 2}
        assert document.revision == 1

        manager.undo()
        assert target == {"value": 1}
        manager.redo()
        assert target == {"value": 2}
    finally:
        manager.clear()
        AuthoringMutationService._instance = previous_service
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry


def test_applied_authoring_command_rolls_back_when_journal_is_unavailable():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_service = AuthoringMutationService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    controller = _AuthoringController()
    document = registry.create(
        DocumentKind.TIMELINE,
        "Timeline",
        controller=controller,
    )
    service = AuthoringMutationService(registry)
    target = {"value": 2}
    before_revision = document.revision
    after_revision = registry.mark_changed(
        document.document_id,
        view_id="timeline-editor",
    )
    manager.enabled = False
    try:
        assert not service.record_applied_command(
            document.document_id,
            _SetValueCommand(target, 1, 2),
            view_id="timeline-editor",
            before_revision=before_revision,
            after_revision=after_revision,
            rollback=lambda: target.__setitem__("value", 1),
        )
        assert target == {"value": 1}
        assert document.revision == 0
        assert manager.action_journal.entries == ()
    finally:
        manager.enabled = True
        manager.clear()
        AuthoringMutationService._instance = previous_service
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry


def test_authoring_panels_cannot_restore_private_history_or_save_authorities():
    ui_root = Path(__file__).resolve().parents[1] / "Infernux" / "engine" / "ui"
    panel_names = (
        "node_graph_editor_panel.py",
        "particle_graph_editor_panel.py",
        "animfsm_editor_panel.py",
        "animtimeline_editor_panel.py",
        "animclip2d_editor_panel.py",
    )
    sources = {
        name: (ui_root / name).read_text(encoding="utf-8")
        for name in panel_names
    }

    for name, source in sources.items():
        assert "UndoManager" not in source, f"{name} bypasses AuthoringMutationService"
        if name != "node_graph_editor_panel.py":
            assert "controller=self," not in source, f"{name} registers its Panel as controller"
    for name in (
        "particle_graph_editor_panel.py",
        "animfsm_editor_panel.py",
        "animtimeline_editor_panel.py",
    ):
        assert "begin_save(" not in sources[name], f"{name} creates private SaveTickets"

    clip_source = sources["animclip2d_editor_panel.py"]
    for legacy_token in (
        "_last_saved_signature",
        "_last_saved_authoring_snapshot",
        "_published_edit_signature",
        "_recompute_dirty",
        "saved_texture_guid",
        "saved_texture_path",
        "saved_path",
    ):
        assert legacy_token not in clip_source
