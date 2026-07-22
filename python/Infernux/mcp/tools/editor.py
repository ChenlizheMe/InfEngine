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

    @mcp.tool(name="editor_get_state")
    def editor_get_state() -> dict:
        """Return lightweight editor state."""

        def _read():
            from Infernux.engine.deferred_task import DeferredTaskRunner
            from Infernux.engine.play_mode import PlayModeManager
            from Infernux.engine.scene_manager import SceneFileManager
            from Infernux.engine.ui.selection_manager import SelectionManager

            pmm = PlayModeManager.instance()
            sfm = SceneFileManager.instance()
            sel = SelectionManager.instance()
            runner = DeferredTaskRunner.instance()
            return {
                "play_state": getattr(getattr(pmm, "state", None), "name", "edit").lower() if pmm else "edit",
                "deferred_task_busy": bool(getattr(runner, "is_busy", False)),
                "selected_ids": sel.get_ids() if sel else [],
                "scene_dirty": bool(sfm.is_dirty) if sfm else False,
                "is_prefab_mode": bool(getattr(sfm, "is_prefab_mode", False)) if sfm else False,
                "scene_status": scene_status(),
            }

        return main_thread("editor_get_state", _read)

    @mcp.tool(name="editor_save_focused")
    def editor_save_focused() -> dict:
        """Save the focused document, falling back to the active scene."""

        def _save():
            from Infernux.engine._bootstrap_wiring import BootstrapWiringMixin
            from Infernux.engine.project_context import is_panel_dirty
            from Infernux.engine.scene_manager import SceneFileManager
            from Infernux.engine.ui.closable_panel import ClosablePanel
            from Infernux.engine.ui.window_manager import WindowManager

            window_manager = WindowManager.instance()
            scene_manager = SceneFileManager.instance()
            if window_manager is None or scene_manager is None:
                raise RuntimeError("Editor save services are not available.")

            panel_id = str(ClosablePanel.get_active_panel_id() or "")
            panel = window_manager.get_window_instance(panel_id) if panel_id else None
            handler = getattr(panel, "handle_save_command", None)
            document_target = bool(panel_id and callable(handler))
            dirty_before = (
                bool(is_panel_dirty(panel_id))
                if document_target
                else bool(scene_manager.is_dirty)
            )

            BootstrapWiringMixin._save_focused_document(window_manager, scene_manager)

            dirty_after = (
                bool(is_panel_dirty(panel_id))
                if document_target
                else bool(scene_manager.is_dirty)
            )
            return {
                "target": "document" if document_target else "scene",
                "panel_id": panel_id if document_target else "",
                "dirty_before": dirty_before,
                "dirty_after": dirty_after,
                "saved": not dirty_after,
                "save_as_required": bool(dirty_after),
            }

        return main_thread("editor_save_focused", _save)

    @mcp.tool(name="editor_save_document")
    def editor_save_document(panel_id: str) -> dict:
        """Save one open document panel without taking keyboard focus."""

        def _save():
            from Infernux.engine.project_context import is_panel_dirty
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
            handler = getattr(panel, "handle_save_command", None)
            if not callable(handler):
                raise RuntimeError(
                    f"Editor panel does not own a savable document: {target_id!r}"
                )

            dirty_before = bool(is_panel_dirty(target_id))
            handled = bool(handler(save_as=False))
            dirty_after = bool(is_panel_dirty(target_id))
            return {
                "target": "document",
                "panel_id": target_id,
                "handled": handled,
                "dirty_before": dirty_before,
                "dirty_after": dirty_after,
                "saved": handled and not dirty_after,
                "save_as_required": handled and dirty_after,
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
            from Infernux.engine.ui.event_bus import EditorEvent, EditorEventBus
            from Infernux.engine.ui.selection_manager import SelectionManager
            from Infernux.lib import SceneManager

            sel = SelectionManager.instance()
            ids = [int(i) for i in (object_ids or []) if int(i) > 0]
            if primary_id:
                sel.select(int(primary_id))
            elif ids:
                sel.box_select(ids)
            else:
                sel.clear()
            selected_ids = sel.get_ids()
            primary = int(sel.get_primary() or 0)
            scene = SceneManager.instance().get_active_scene()
            selected = scene.find_by_id(primary) if scene is not None and primary else None
            EditorEventBus.instance().emit(EditorEvent.SELECTION_CHANGED, selected)
            return {"selected_ids": selected_ids}

        return main_thread("editor_select", _select, arguments={"object_ids": object_ids or [], "primary_id": primary_id})
