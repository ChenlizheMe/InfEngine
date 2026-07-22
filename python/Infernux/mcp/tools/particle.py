"""ParticleGraph editor authoring tools for MCP developer sessions."""

from __future__ import annotations

from Infernux.engine.path_utils import relative_path
from Infernux.mcp.tools.common import (
    main_thread,
    register_tool_metadata,
    resolve_asset_path,
)


def register_particle_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="particle_graph_inspect_editor")
    def particle_graph_inspect_editor() -> dict:
        """Inspect the ParticleGraph document currently open in its editor."""

        def _inspect():
            panel = _require_particle_graph_panel()
            return _portable_snapshot(panel.authoring_snapshot(), project_path)

        return main_thread("particle_graph_inspect_editor", _inspect)

    @mcp.tool(name="particle_graph_set_node_asset")
    def particle_graph_set_node_asset(
        node_uid: str,
        property_name: str,
        asset_path: str,
    ) -> dict:
        """Set a Mesh or Material AssetRef on a node in the live ParticleGraph editor."""

        def _set():
            panel = _require_particle_graph_panel()
            target = resolve_asset_path(project_path, asset_path)
            reference = panel.set_node_asset_reference(
                str(node_uid), str(property_name), target
            )
            snapshot = _portable_snapshot(panel.authoring_snapshot(), project_path)
            return {
                "node_uid": str(node_uid),
                "property_name": str(property_name),
                "asset": reference,
                "editor": snapshot,
            }

        return main_thread(
            "particle_graph_set_node_asset",
            _set,
            arguments={
                "node_uid": node_uid,
                "property_name": property_name,
                "asset_path": asset_path,
            },
        )

    @mcp.tool(name="particle_graph_set_rendering_output")
    def particle_graph_set_rendering_output(node_uid: str) -> dict:
        """Connect the Rendering root stream to exactly one output node."""

        def _set():
            panel = _require_particle_graph_panel()
            result = panel.set_rendering_output(str(node_uid))
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_set_rendering_output",
            _set,
            arguments={"node_uid": node_uid},
        )

    @mcp.tool(name="particle_graph_reload_editor")
    def particle_graph_reload_editor() -> dict:
        """Reload the open ParticleGraph from disk after a successful save."""

        def _reload():
            panel = _require_particle_graph_panel()
            if not panel.reload_from_disk():
                raise RuntimeError("Particle Graph editor could not reload its asset")
            return _portable_snapshot(panel.authoring_snapshot(), project_path)

        return main_thread("particle_graph_reload_editor", _reload)


def _require_particle_graph_panel():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    manager = WindowManager.instance()
    panel = (
        manager.get_window_instance("particle_graph_editor")
        if manager is not None
        else None
    )
    if not isinstance(panel, ParticleGraphEditorPanel) or not bool(panel.is_open):
        raise RuntimeError(
            "Particle Graph Editor is not open. Open a .particlegraph asset first."
        )
    return panel


def _portable_snapshot(snapshot: dict, project_path: str) -> dict:
    result = dict(snapshot)
    file_path = str(result.get("file_path") or "")
    if file_path:
        result["file_path"] = relative_path(file_path, project_path)
    return result


def _register_metadata() -> None:
    register_tool_metadata(
        "particle_graph_inspect_editor",
        summary="Inspect the live ParticleGraph editor document and node properties.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "vfx"],
        aliases=["particle graph nodes", "粒子图节点", "粒子图检查"],
        preconditions=["A .particlegraph asset must be open in Particle Graph Editor."],
        recovery=["Open the ParticleGraph asset in the editor, then retry."],
        next_suggested_tools=["particle_graph_set_node_asset", "editor_save_focused"],
    )
    register_tool_metadata(
        "particle_graph_set_node_asset",
        summary="Set a Mesh or Material reference through the live ParticleGraph editor model.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "mesh", "material"],
        aliases=["set mesh output", "particle material", "设置粒子网格"],
        preconditions=[
            "A .particlegraph asset must be open.",
            "asset_path must identify an imported asset inside Assets/.",
        ],
        side_effects=[
            "Updates the live editor document, records Undo, marks the panel dirty, and republishes its in-memory draft."
        ],
        recovery=[
            "Call particle_graph_inspect_editor to verify node_uid and AssetRef property names."
        ],
        next_suggested_tools=[
            "particle_graph_inspect_editor",
            "particle_graph_set_rendering_output",
            "editor_save_document",
        ],
    )
    register_tool_metadata(
        "particle_graph_set_rendering_output",
        summary="Route one emitter's Rendering stream to a selected output node.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "output"],
        aliases=["set particle output", "connect mesh output", "设置粒子输出"],
        preconditions=[
            "A .particlegraph asset must be open.",
            "node_uid must identify a Rendering output in the live editor document.",
        ],
        side_effects=[
            "Replaces the Rendering root output connection, records Undo, and marks the document dirty."
        ],
        recovery=[
            "Call particle_graph_inspect_editor and inspect links before retrying."
        ],
        next_suggested_tools=["editor_save_document", "particle_graph_reload_editor"],
    )
    register_tool_metadata(
        "particle_graph_reload_editor",
        summary="Reload the clean ParticleGraph editor document from its saved asset.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "reload", "persistence"],
        aliases=["reopen particle graph", "verify particle save", "重载粒子图"],
        preconditions=["The open ParticleGraph document must be clean and have a source path."],
        recovery=["Save the document with editor_save_document, then retry."],
        next_suggested_tools=["particle_graph_inspect_editor"],
    )
