from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.interaction import EditorInteractionCore
from Infernux.engine.interaction.modals import ModalService
from Infernux.engine.ui.plugin_reload_progress import PluginReloadProgressService


class _Manager:
    def __init__(self, errors=()) -> None:
        self.calls: list[str] = []
        self.errors = set(errors)

    def reload(self, reference: str):
        self.calls.append(reference)
        error = "broken preload" if reference in self.errors else ""
        return SimpleNamespace(reference=reference, loaded=not error, error=error)


def test_plugin_reload_progress_presents_before_work_and_steps_plugins():
    previous_core = EditorInteractionCore._instance
    previous_progress = PluginReloadProgressService._instance
    EditorInteractionCore._instance = type("_Core", (), {"modals": ModalService()})()
    PluginReloadProgressService._instance = None
    completed = []
    try:
        manager = _Manager()
        progress = PluginReloadProgressService.instance()
        assert progress.begin(
            manager=manager,
            references=("first.plugin", "second.plugin"),
            complete=lambda *args: completed.append(args),
        )

        progress.post_present_tick()
        assert manager.calls == []

        progress._transaction.presented_phase = "opening"
        progress._transaction.phase_started_at -= 1.0
        progress.post_present_tick()
        assert progress._transaction.phase == "reloading"
        assert manager.calls == []

        progress._transaction.presented_phase = "reloading"
        progress._transaction.last_step_at -= 1.0
        progress.post_present_tick()
        assert manager.calls == ["first.plugin"]
        assert progress._transaction.progress == 0.5

        progress._transaction.last_step_at -= 1.0
        progress.post_present_tick()
        assert manager.calls == ["first.plugin", "second.plugin"]
        assert progress._transaction.phase == "complete"
        assert progress._transaction.progress == 1.0

        progress._transaction.presented_phase = "complete"
        progress._transaction.phase_started_at -= 1.0
        progress.post_present_tick()
        assert completed and completed[0][0] is True
        assert progress.is_active is False
    finally:
        EditorInteractionCore._instance = previous_core
        PluginReloadProgressService._instance = previous_progress


def test_plugin_reload_progress_reports_preload_errors():
    previous_core = EditorInteractionCore._instance
    previous_progress = PluginReloadProgressService._instance
    EditorInteractionCore._instance = type("_Core", (), {"modals": ModalService()})()
    PluginReloadProgressService._instance = None
    completed = []
    try:
        progress = PluginReloadProgressService.instance()
        assert progress.begin(
            manager=_Manager(errors={"bad.plugin"}),
            references=("bad.plugin",),
            complete=lambda *args: completed.append(args),
        )
        progress._transaction.presented_phase = "opening"
        progress._transaction.phase_started_at -= 1.0
        progress.post_present_tick()
        progress._transaction.presented_phase = "reloading"
        progress._transaction.last_step_at -= 1.0
        progress.post_present_tick()
        progress._transaction.presented_phase = "complete"
        progress._transaction.phase_started_at -= 1.0
        progress.post_present_tick()

        assert completed[0][0] is False
        assert "bad.plugin: broken preload" in completed[0][2]
    finally:
        EditorInteractionCore._instance = previous_core
        PluginReloadProgressService._instance = previous_progress
