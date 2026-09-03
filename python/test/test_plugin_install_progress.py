from __future__ import annotations

import time

from Infernux.engine.interaction import EditorInteractionCore
from Infernux.engine.interaction.modals import ModalService
from Infernux.engine.ui.plugin_install_confirmation import (
    PluginInstallConfirmationCoordinator,
)
from Infernux.engine.ui.plugin_install_progress import PluginInstallProgressService


def test_plugin_install_progress_presents_before_running_full_task():
    previous_core = EditorInteractionCore._instance
    previous_progress = PluginInstallProgressService._instance
    EditorInteractionCore._instance = type("_Core", (), {"modals": ModalService()})()
    PluginInstallProgressService._instance = None
    calls = []
    completed = []
    try:
        service = PluginInstallProgressService.instance()
        assert service.begin(
            label="https://example.test/plugin.git",
            work=lambda report: (
                calls.append("work"),
                report("clone_repository", 0.2),
                report("write_assets", 0.8),
                "installed",
            )[-1],
            complete=lambda *args: completed.append(args),
        )

        service.post_present_tick()
        assert calls == []
        service._transaction.presented_phase = "opening"
        service.post_present_tick()
        assert service._transaction.phase == "running"
        assert service._transaction.worker_done.wait(1.0)

        service._transaction.presented_phase = "running"
        service.post_present_tick()
        assert calls == ["work"]
        assert service._transaction.phase == "complete"
        assert service._transaction.progress == 1.0
        assert len(service._transaction.history) >= 3

        service._transaction.presented_phase = "complete"
        service._transaction.completed_at -= 1.0
        service.post_present_tick()
        assert completed == [(True, "installed", "")]
    finally:
        EditorInteractionCore._instance = previous_core
        PluginInstallProgressService._instance = previous_progress


def test_plugin_install_worker_keeps_editor_ticks_non_blocking_and_reports_detail():
    previous_core = EditorInteractionCore._instance
    previous_progress = PluginInstallProgressService._instance
    EditorInteractionCore._instance = type("_Core", (), {"modals": ModalService()})()
    PluginInstallProgressService._instance = None
    release = None
    try:
        service = PluginInstallProgressService.instance()

        def work(report):
            nonlocal release
            while release is None:
                time.sleep(0.005)
            report("download_package", 0.5, "8.0 MiB / 16.0 MiB")
            return "installed"

        assert service.begin(label="package", work=work, complete=lambda *_args: None)
        service._transaction.presented_phase = "opening"
        started = time.monotonic()
        service.post_present_tick()
        assert time.monotonic() - started < 0.1
        assert service._transaction.phase == "running"

        release = True
        assert service._transaction.worker_done.wait(1.0)
        service._transaction.presented_phase = "running"
        service.post_present_tick()
        assert service._transaction.phase == "complete"
    finally:
        release = True
        EditorInteractionCore._instance = previous_core
        PluginInstallProgressService._instance = previous_progress


def test_external_install_confirmation_has_no_side_effect_before_acceptance():
    previous_core = EditorInteractionCore._instance
    previous_confirmation = PluginInstallConfirmationCoordinator._instance
    EditorInteractionCore._instance = type("_Core", (), {"modals": ModalService()})()
    PluginInstallConfirmationCoordinator._instance = None
    calls = []

    class Context:
        closed = False

        def close_current_popup(self):
            self.closed = True

    try:
        coordinator = PluginInstallConfirmationCoordinator.instance()
        assert coordinator.request(
            "source",
            "https://example.test/plugin.git",
            lambda: calls.append("install"),
        )
        assert calls == []
        ctx = Context()
        coordinator._confirm(ctx)
        assert ctx.closed is True
        assert calls == ["install"]
        assert coordinator.is_active is False
    finally:
        EditorInteractionCore._instance = previous_core
        PluginInstallConfirmationCoordinator._instance = previous_confirmation
