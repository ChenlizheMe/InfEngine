from __future__ import annotations

from Infernux.core.asset_types import SpriteFrame, TextureImportSettings, TextureType
from Infernux.engine.interaction import AuthoringMutationService, DocumentRegistry
from Infernux.engine.interaction import EditorInteractionCore
from Infernux.engine.interaction.modals import ModalService
from Infernux.engine.undo import UndoManager
from Infernux.engine.ui import asset_details_renderer as details
from Infernux.engine.ui.asset_import_progress import AssetImportProgressService


class _ExecutionLayer:
    def __init__(self) -> None:
        self.applied = []
        self.file_path = ""

    def refresh_binding(self, _category, file_path) -> None:
        self.file_path = file_path

    def apply_import_settings(self, settings) -> bool:
        self.applied.append(settings.copy())
        return True


def test_import_settings_are_document_backed_and_undoable(monkeypatch):
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_mutations = AuthoringMutationService._instance
    previous_core = EditorInteractionCore._instance
    previous_progress = AssetImportProgressService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    core = type("_TestInteractionCore", (), {"modals": ModalService()})()
    EditorInteractionCore._instance = core
    AssetImportProgressService._instance = None
    monkeypatch.setattr(
        "Infernux.core.assets.AssetManager.flush_pending_gpu_texture_reloads",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "Infernux.engine.ui.asset_resource_preview.ensure_imported_texture_preview",
        lambda _path: True,
    )
    try:
        details._ensure_categories()
        state = details._State()
        state.file_path = "C:/Project/Assets/checker.png"
        state.category = "texture"
        state.meta = {"guid": "abc123"}
        state.settings = TextureImportSettings(srgb=True)
        state.disk_settings = state.settings.copy()
        state.exec_layer = _ExecutionLayer()

        details._bind_import_settings_document(
            state,
            details._categories["texture"],
        )
        document = registry.require(state.document_id)
        assert not document.is_dirty

        assert details._edit_import_settings(
            state,
            "srgb",
            lambda settings: setattr(settings, "srgb", False),
            "Set sRGB",
        )
        assert state.settings.srgb is False
        assert registry.require(state.document_id).is_dirty
        assert len(manager.action_journal.applied_entries()) == 1

        manager.undo()
        assert state.settings.srgb is True
        assert not registry.require(state.document_id).is_dirty

        manager.redo()
        assert state.settings.srgb is False
        assert registry.require(state.document_id).is_dirty

        result = registry.request_save(state.document_id)
        assert result.accepted
        progress = AssetImportProgressService.instance()
        assert progress._transaction is not None
        progress.post_present_tick()
        assert not state.exec_layer.applied

        progress._transaction.presented_phase = "opening"
        progress.post_present_tick()
        assert progress._transaction.phase == "importing"
        assert not state.exec_layer.applied

        progress.post_present_tick()
        assert not state.exec_layer.applied
        progress._transaction.presented_phase = "importing"
        progress.post_present_tick()
        assert state.exec_layer.applied[-1].srgb is False
        assert progress._transaction.phase == "complete"
        assert registry.require(state.document_id).is_dirty

        progress._transaction.presented_phase = "complete"
        progress.post_present_tick()
        assert not registry.require(state.document_id).is_dirty

        manager.undo()
        assert state.settings.srgb is True
        assert registry.require(state.document_id).is_dirty

        result = registry.request_discard(state.document_id)
        assert result.accepted
        assert state.settings.srgb is False
        assert not registry.require(state.document_id).is_dirty
    finally:
        DocumentRegistry._instance = previous_registry
        UndoManager._instance = previous_manager
        AuthoringMutationService._instance = previous_mutations
        EditorInteractionCore._instance = previous_core
        AssetImportProgressService._instance = previous_progress


def test_import_settings_edit_fails_closed_without_a_document():
    settings = TextureImportSettings(srgb=True)
    state = details._State()
    state.settings = settings
    state.import_controller = None
    state.document_id = ""

    assert not details._edit_import_settings(
        state,
        "srgb",
        lambda value: setattr(value, "srgb", False),
        "Set sRGB",
    )
    assert settings.srgb is True


def test_import_settings_save_fails_closed_without_a_document():
    settings = TextureImportSettings(srgb=True)
    execution = _ExecutionLayer()
    state = details._State()
    state.settings = settings
    state.disk_settings = settings.copy()
    state.exec_layer = execution
    state.document_id = ""

    assert details._auto_save_sprite(state) is False
    assert execution.applied == []
    assert state.disk_settings == settings


def test_import_settings_edit_does_not_reserve_revision_without_journal():
    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_mutations = AuthoringMutationService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    try:
        details._ensure_categories()
        state = details._State()
        state.file_path = "C:/Project/Assets/checker.png"
        state.category = "texture"
        state.meta = {"guid": "abc123"}
        state.settings = TextureImportSettings(srgb=True)
        state.disk_settings = state.settings.copy()
        state.exec_layer = _ExecutionLayer()
        details._bind_import_settings_document(
            state,
            details._categories["texture"],
        )
        document = registry.require(state.document_id)
        manager.enabled = False

        assert not details._edit_import_settings(
            state,
            "srgb",
            lambda value: setattr(value, "srgb", False),
            "Set sRGB",
        )
        assert state.settings.srgb is True
        assert document.revision == 0
        assert not document.is_dirty
        assert manager.action_journal.entries == ()
    finally:
        manager.enabled = True
        AuthoringMutationService._instance = previous_mutations
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry


def test_sprite_reslice_undo_restores_subresource_identity_and_selection():
    from Infernux.engine.interaction import (
        ContextRestoreStatus,
        EditorContextSnapshot,
        SelectionService,
        SelectionTarget,
    )

    previous_registry = DocumentRegistry._instance
    previous_manager = UndoManager._instance
    previous_mutations = AuthoringMutationService._instance
    previous_selection = SelectionService._instance
    registry = DocumentRegistry()
    manager = UndoManager()
    selection = SelectionService()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, _phase: (
            selection.apply_snapshot(
                context.selection,
                reason="test_sprite_reslice_restore",
                record_history=False,
            ),
            ContextRestoreStatus.READY,
        )[1],
    )
    try:
        details._ensure_categories()
        source = SpriteFrame(name="full", x=0, y=0, w=128, h=64)
        state = details._State()
        state.file_path = "C:/Project/Assets/checker.png"
        state.category = "texture"
        state.meta = {"guid": "abc123"}
        state.settings = TextureImportSettings(
            texture_type=TextureType.SPRITE,
            sprite_frames=[source],
        )
        state.disk_settings = state.settings.copy()
        state.exec_layer = _ExecutionLayer()
        details._bind_import_settings_document(
            state,
            details._categories["texture"],
        )
        selected_source = SelectionTarget.asset_subresource(
            state.file_path,
            source.stable_id,
            sub_kind="sprite_frame",
        )
        selection.select(
            selected_source,
            owner_id="inspector",
            record_history=False,
        )
        sprite_state = details._SpriteEditorState()
        sprite_state.tex_w = 128
        sprite_state.tex_h = 64
        sprite_state.slice_rows = 1
        sprite_state.slice_cols = 2

        assert details._edit_import_settings(
            state,
            "sprite_frames.auto_slice",
            lambda draft: details._auto_slice(draft, sprite_state),
            "Auto Slice Sprite",
            selection_after=lambda _old, new, before: details._sprite_selection_after(
                state,
                new,
                before,
            ),
        )
        assert selection.snapshot.primary == SelectionTarget.asset(state.file_path)
        assert len(state.settings.sprite_frames) == 2

        manager.undo()
        assert [frame.stable_id for frame in state.settings.sprite_frames] == [
            source.stable_id
        ]
        assert selection.snapshot.primary == selected_source

        manager.redo()
        assert len(state.settings.sprite_frames) == 2
        assert selection.snapshot.primary == SelectionTarget.asset(state.file_path)
    finally:
        SelectionService._instance = previous_selection
        AuthoringMutationService._instance = previous_mutations
        UndoManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry
