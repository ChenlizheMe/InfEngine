"""Single focus-aware save authority for Editor commands and automation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .documents import DocumentActionResult, DocumentActionStatus, DocumentKind, DocumentRegistry


@dataclass(frozen=True, slots=True)
class FocusedSaveResult:
    result: DocumentActionResult
    target: str = ""
    document_id: str = ""
    panel_id: str = ""
    dirty_before: bool = False

    @property
    def accepted(self) -> bool:
        return self.result.accepted


class EditorSaveService:
    """Resolve and save exactly one focused document through DocumentRegistry."""

    _instance: Optional["EditorSaveService"] = None

    def __init__(self, registry: DocumentRegistry) -> None:
        self._registry = registry
        EditorSaveService._instance = self

    @classmethod
    def instance(cls) -> Optional["EditorSaveService"]:
        return cls._instance

    def save_focused(self, *, save_as: bool = False) -> FocusedSaveResult:
        from .contexts import FocusService
        from .continuous_edits import ContinuousEditService

        focus = FocusService.instance().snapshot
        document_id = str(focus.active_document_id or "")
        document = self._registry.get(document_id) if document_id else None
        if document_id and document is None:
            return FocusedSaveResult(
                DocumentActionResult(
                    DocumentActionStatus.REJECTED,
                    "focused document is no longer registered",
                ),
                panel_id=focus.active_panel_id,
                document_id=document_id,
            )

        if document is None:
            try:
                from Infernux.engine.scene_manager import SceneFileManager

                scene_files = SceneFileManager.instance()
            except (ImportError, RuntimeError):
                scene_files = None
            scene_document_id = str(getattr(scene_files, "document_id", "") or "")
            document = self._registry.get(scene_document_id) if scene_document_id else None
            if document is None:
                return FocusedSaveResult(
                    DocumentActionResult(
                        DocumentActionStatus.REJECTED,
                        "no focused document or active scene document is available",
                    ),
                    panel_id=focus.active_panel_id,
                )

        ContinuousEditService.instance().commit_document(document.document_id)
        dirty_before = bool(document.is_dirty)
        result = self._registry.defer_save(document.document_id, save_as=save_as)
        return FocusedSaveResult(
            result,
            target="scene" if document.kind is DocumentKind.SCENE else "document",
            document_id=document.document_id,
            panel_id=focus.active_panel_id if document.kind is not DocumentKind.SCENE else "",
            dirty_before=dirty_before,
        )

    def shutdown(self) -> None:
        if EditorSaveService._instance is self:
            EditorSaveService._instance = None
