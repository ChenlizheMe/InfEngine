from __future__ import annotations

from types import SimpleNamespace

from Infernux.engine.interaction import EditorInteractionCore
from Infernux.engine.interaction.modals import ModalService
from Infernux.engine.ui.asset_import_progress import AssetImportProgressService
from Infernux.engine.ui.plugin_panel import InxPackageImportPanel
from Infernux.plugins import PluginManager


def test_inxpackage_import_waits_for_visible_progress_before_install(monkeypatch):
    previous_core = EditorInteractionCore._instance
    previous_progress = AssetImportProgressService._instance
    previous_manager = PluginManager._instance
    calls = []

    class _Manager:
        def install_package(self, path, *, selected):
            calls.append((path, tuple(selected)))
            return SimpleNamespace(reference="materials")

    EditorInteractionCore._instance = SimpleNamespace(modals=ModalService())
    AssetImportProgressService._instance = None
    PluginManager._instance = _Manager()
    monkeypatch.setattr(
        "Infernux.engine.ui.plugin_panel.t",
        lambda key: key,
    )
    try:
        panel = InxPackageImportPanel()
        panel.package_path = "C:/Desktop/Materials.inxpkg"
        panel._selected = {"Assets/Plugins/materials/Neon.mat": True}

        assert panel._begin_import()
        progress = AssetImportProgressService.instance()
        transaction = progress._transaction
        assert transaction is not None
        assert transaction.owner_id == "inxpackage_import"
        assert transaction.message == "inxpackage.import_progress.preparing"
        assert calls == []

        transaction.presented_phase = "opening"
        progress.post_present_tick()
        assert transaction.phase == "importing"
        assert calls == []

        transaction.presented_phase = "importing"
        progress.post_present_tick()
        assert transaction.phase == "complete"
        assert calls == [
            (
                "C:/Desktop/Materials.inxpkg",
                ("Assets/Plugins/materials/Neon.mat",),
            )
        ]

        transaction.presented_phase = "complete"
        progress.post_present_tick()
        assert panel._message == "inxpackage.imported"
        assert not progress.is_active
    finally:
        EditorInteractionCore._instance = previous_core
        AssetImportProgressService._instance = previous_progress
        PluginManager._instance = previous_manager
