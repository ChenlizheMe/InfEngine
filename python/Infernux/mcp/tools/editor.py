"""Editor-state MCP tools."""

from __future__ import annotations

from Infernux.mcp.tools.common import main_thread, register_tool_metadata, scene_status


def register_editor_tools(mcp) -> None:
    register_tool_metadata(
        "editor_save_focused",
        summary="Save the active editor document through the same focus-aware route as Ctrl+S.",
        category="editor/documents",
        tags=["editor", "save", "document", "asset"],
        aliases=["save focused", "save active document", "保存当前文档"],
        preconditions=["The document or scene to save must be open in the Editor."],
        side_effects=[
            "Saves the focused editor document, or the active scene when no document panel owns focus."
        ],
        recovery=[
            "Focus the intended editor panel and retry; inspect dirty_after when a Save As dialog is required."
        ],
        next_suggested_tools=["editor_get_state", "scene_status"],
    )
    register_tool_metadata(
        "editor_save_document",
        summary="Save a named open editor document without changing user focus.",
        category="editor/documents",
        tags=["editor", "save", "document", "background"],
        aliases=["save panel", "save document by id", "后台保存文档"],
        preconditions=[
            "panel_id must identify an open editor panel with a document save handler."
        ],
        side_effects=["Saves the specified editor document through its normal panel handler."],
        recovery=["Inspect the authoring tool snapshot for its panel_id and retry."],
        next_suggested_tools=["editor_get_state"],
    )
    register_tool_metadata(
        "editor_focus_panel",
        summary="Focus an already-open Editor panel without manipulating the operating-system window.",
        category="editor/windows",
        tags=["editor", "panel", "focus", "view"],
        aliases=["focus panel", "select dock tab", "切换编辑器面板"],
        preconditions=["panel_id must identify an already-open Editor panel."],
        side_effects=["Changes the active docked panel inside the Editor."],
        recovery=["Open the panel through the normal Editor Window menu, then retry."],
        next_suggested_tools=["editor_get_state", "runtime_renderer_state"],
    )

    @mcp.tool(name="editor_get_state")
    def editor_get_state() -> dict:
        """Return lightweight editor state."""

        def _read():
            from Infernux.engine.deferred_task import DeferredTaskRunner
            from Infernux.engine.play_mode import PlayModeManager
            from Infernux.engine.scene_manager import SceneFileManager
            from Infernux.engine.interaction import SelectionService

            pmm = PlayModeManager.instance()
            sfm = SceneFileManager.instance()
            selection = SelectionService.instance()
            runner = DeferredTaskRunner.instance()
            return {
                "play_state": getattr(getattr(pmm, "state", None), "name", "edit").lower() if pmm else "edit",
                "deferred_task_busy": bool(getattr(runner, "is_busy", False)),
                "selected_ids": list(selection.scene_object_ids()),
                "scene_dirty": bool(sfm.is_dirty) if sfm else False,
                "is_prefab_mode": bool(getattr(sfm, "is_prefab_mode", False)) if sfm else False,
                "scene_status": scene_status(),
            }

        return main_thread("editor_get_state", _read)

    @mcp.tool(name="editor_focus_panel")
    def editor_focus_panel(panel_id: str) -> dict:
        """Focus an open docked panel without moving or resizing the Editor window."""

        def _focus():
            from Infernux.engine.ui.window_manager import WindowManager

            target_id = str(panel_id).strip()
            if not target_id:
                raise ValueError("panel_id is required")
            manager = WindowManager.instance()
            if manager is None:
                raise RuntimeError("WindowManager is not available.")
            manager.focus_window(target_id)
            return {
                "panel_id": target_id,
                "focus_requested": True,
                "window_state": manager.get_window_state(target_id).name.lower(),
            }

        return main_thread(
            "editor_focus_panel", _focus, arguments={"panel_id": panel_id}
        )

    @mcp.tool(name="editor_save_focused")
    def editor_save_focused() -> dict:
        """Save the focused document, falling back to the active scene."""

        def _save():
            from Infernux.engine.interaction import EditorInteractionCore

            core = EditorInteractionCore.instance()
            if core is None:
                raise RuntimeError("Editor save service is not available.")
            saved = core.saving.save_focused()
            document = core.documents.get(saved.document_id)
            dirty_after = bool(document.is_dirty) if document else saved.dirty_before
            return {
                "target": saved.target,
                "panel_id": saved.panel_id,
                "document_id": saved.document_id,
                "dirty_before": saved.dirty_before,
                "dirty_after": dirty_after,
                "accepted": saved.accepted,
                "status": saved.result.status.value,
                "saved": saved.result.status.value in {"applied", "no_op"},
                "save_as_required": bool(dirty_after and not document.resource_path) if document else False,
                "message": saved.result.message,
            }

        return main_thread("editor_save_focused", _save)

    @mcp.tool(name="editor_save_document")
    def editor_save_document(panel_id: str) -> dict:
        """Save one open document panel without taking keyboard focus."""

        def _save():
            from Infernux.engine.interaction import (
                ContinuousEditService,
                DocumentRegistry,
            )
            from Infernux.engine.ui.window_manager import WindowManager

            target_id = str(panel_id).strip()
            if not target_id:
                raise ValueError("panel_id is required")
            window_manager = WindowManager.instance()
            if window_manager is None:
                raise RuntimeError("WindowManager is not available.")
            panel = window_manager.get_window_instance(target_id)
            if panel is None or not window_manager.is_window_open(target_id):
                raise RuntimeError(f"Editor document panel is not open: {target_id!r}")
            registry = DocumentRegistry.instance()
            document = registry.document_for_view(target_id)
            if document is None:
                raise RuntimeError(
                    f"Editor panel does not own a savable document: {target_id!r}"
                )

            dirty_before = document.is_dirty
            ContinuousEditService.instance().commit_document(document.document_id)
            result = registry.request_save(document.document_id)
            dirty_after = document.is_dirty
            return {
                "target": "document",
                "panel_id": target_id,
                "document_id": document.document_id,
                "handled": result.accepted,
                "dirty_before": dirty_before,
                "dirty_after": dirty_after,
                "saved": result.accepted and not dirty_after,
                "save_as_required": result.accepted and dirty_after,
            }

        return main_thread(
            "editor_save_document", _save, arguments={"panel_id": panel_id}
        )

    @mcp.tool(name="editor_play")
    def editor_play() -> dict:
        """Enter Play Mode."""

        def _play():
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm is None:
                raise RuntimeError("PlayModeManager is not available.")
            status = scene_status()
            if status["play_state"] != "edit":
                raise RuntimeError("Play Mode is already active.")
            if status["loading"]:
                raise RuntimeError("Cannot enter Play Mode while scene loading is pending.")
            try:
                from Infernux.engine.deferred_task import DeferredTaskRunner
                runner = DeferredTaskRunner.instance()
                if runner and runner.is_busy:
                    raise RuntimeError("Cannot enter Play Mode while a deferred editor task is running.")
            except RuntimeError:
                raise
            except Exception:
                pass
            if not status["saved_to_file"]:
                raise RuntimeError("Cannot enter Play Mode until the active scene is saved. Call scene_save first.")
            if status["dirty"]:
                raise RuntimeError("Cannot enter Play Mode while the active scene is dirty. Call scene_save first.")
            accepted = bool(pmm.enter_play_mode())
            return {
                "accepted": accepted,
                "state": pmm.state.name.lower(),
                "requested_state": "playing" if accepted else pmm.state.name.lower(),
                "deferred": bool(accepted),
                "preflight": status,
                "next_suggested_tools": ["runtime_wait", "mcp_health", "runtime_read_errors"] if accepted else ["scene_status"],
            }

        return main_thread("editor_play", _play)

    @mcp.tool(name="editor_stop")
    def editor_stop() -> dict:
        """Exit Play Mode."""

        def _stop():
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm is None:
                raise RuntimeError("PlayModeManager is not available.")
            if pmm.state.name.lower() == "edit":
                return {"accepted": True, "already_stopped": True, "state": "edit"}
            return {"accepted": bool(pmm.exit_play_mode()), "already_stopped": False, "state": pmm.state.name.lower()}

        return main_thread("editor_stop", _stop)

    @mcp.tool(name="editor_pause")
    def editor_pause() -> dict:
        """Pause Play Mode."""

        def _pause():
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm is None:
                raise RuntimeError("PlayModeManager is not available.")
            if pmm.state.name.lower() != "playing":
                raise RuntimeError("editor_pause requires Play Mode to be playing.")
            return {"accepted": bool(pmm.pause()), "state": pmm.state.name.lower()}

        return main_thread("editor_pause", _pause)

    @mcp.tool(name="editor_resume")
    def editor_resume() -> dict:
        """Resume from paused Play Mode."""

        def _resume():
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm is None:
                raise RuntimeError("PlayModeManager is not available.")
            if pmm.state.name.lower() != "paused":
                raise RuntimeError("editor_resume requires Play Mode to be paused.")
            return {"accepted": bool(pmm.resume()), "state": pmm.state.name.lower()}

        return main_thread("editor_resume", _resume)

    @mcp.tool(name="editor_step")
    def editor_step() -> dict:
        """Step one frame while Play Mode is paused."""

        def _step():
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm is None:
                raise RuntimeError("PlayModeManager is not available.")
            if pmm.state.name.lower() != "paused":
                raise RuntimeError("editor_step requires paused Play Mode. Call editor_pause after editor_play before stepping.")
            pmm.step_frame()
            return {"state": pmm.state.name.lower()}

        return main_thread("editor_step", _step)

    @mcp.tool(name="editor_select")
    def editor_select(object_ids: list[int] | None = None, primary_id: int = 0) -> dict:
        """Set the current editor selection."""

        def _select():
            from Infernux.engine.interaction import SelectionService

            selection = SelectionService.instance()
            ids = [int(i) for i in (object_ids or []) if int(i) > 0]
            if primary_id:
                selection.select_scene_object(
                    int(primary_id),
                    owner_id="automation",
                    reason="mcp_editor_select",
                )
            elif ids:
                selection.replace_scene_objects(
                    ids,
                    owner_id="automation",
                    reason="mcp_editor_select",
                )
            else:
                selection.clear(reason="mcp_editor_select")
            selected_ids = list(selection.scene_object_ids())
            return {"selected_ids": selected_ids}

        return main_thread("editor_select", _select, arguments={"object_ids": object_ids or [], "primary_id": primary_id})
