"""Transactional MCP tools for long-horizon agent edits."""

from __future__ import annotations

from Infernux.mcp.project_tools import transactions
from Infernux.mcp.tools.common import (
    EXTERNAL_SOURCE_TRANSACTION_OPERATIONS,
    main_thread,
    notify_asset_changed,
    ok,
    register_tool_metadata,
    require_external_source_edit_path,
)


def _validate_external_source_rollback(project_path: str) -> None:
    """Fail closed if a transaction contains editor-owned/structural paths."""
    state = transactions.status()
    if not state.get("active"):
        return
    for event in state.get("events", ()):
        operation = str(event.get("operation", "") or "")
        path = str(event.get("path", "") or "")
        if bool(event.get("directory")) or operation not in EXTERNAL_SOURCE_TRANSACTION_OPERATIONS:
            raise RuntimeError(
                f"Refusing MCP file snapshot rollback for structural or editor-owned event '{operation}: {path}'. "
                "Project assets must be reverted through the global Editor Undo history."
            )
        try:
            require_external_source_edit_path(project_path, path, "roll back")
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Refusing MCP file snapshot rollback for '{path}': {exc}"
            ) from exc


def register_transaction_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="transaction_begin")
    def transaction_begin(label: str = "") -> dict:
        """Start a snapshot transaction for external source/text edits."""
        return ok(transactions.begin(project_path, label=label))

    @mcp.tool(name="transaction_status")
    def transaction_status() -> dict:
        """Return the active or last MCP transaction status."""
        return ok(transactions.status())

    @mcp.tool(name="transaction_commit")
    def transaction_commit() -> dict:
        """Commit the active MCP transaction."""
        return ok(transactions.commit())

    @mcp.tool(name="transaction_rollback")
    def transaction_rollback() -> dict:
        """Rollback tracked external source/text mutations."""
        _validate_external_source_rollback(project_path)

        def _rollback() -> dict:
            result = transactions.rollback()
            for relative in result.get("restored", ()):
                notify_asset_changed(
                    require_external_source_edit_path(
                        project_path, relative, "publish rollback"
                    ),
                    "modified",
                )
            for relative in result.get("removed", ()):
                notify_asset_changed(
                    require_external_source_edit_path(
                        project_path, relative, "publish rollback"
                    ),
                    "deleted",
                )
            return result

        return main_thread("transaction_rollback", _rollback)


def _register_metadata() -> None:
    metadata = {
        "transaction_begin": "Start a snapshot transaction for external source/text mutations.",
        "transaction_status": "Inspect active/last MCP transaction state.",
        "transaction_commit": "Accept tracked MCP mutations.",
        "transaction_rollback": "Restore tracked external source/text paths from the active MCP transaction.",
    }
    for name, summary in metadata.items():
        register_tool_metadata(
            name,
            summary=summary,
            side_effects=["Reads or mutates MCP transaction bookkeeping under .infernux/mcp_transactions."],
            recovery=["Use transaction_status before rollback; use global Editor Undo for structural or document-owned asset operations."],
            concepts={"MCP Transaction": "A file snapshot recovery boundary only for externally authored source/text files."},
            next_suggested_tools=["transaction_status", "mcp_trace_current"],
        )
