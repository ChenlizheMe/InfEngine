"""Mode-bound MCP tools for remote project sessions and blocker feedback."""

from __future__ import annotations

from typing import Any, Literal

from Infernux.mcp import session
from Infernux.mcp.tools.common import fail, main_thread, ok, register_tool_metadata


BlockerCategory = Literal[
    "project_bug",
    "editor_ui_bug",
    "engine_bug",
    "mcp_bug",
    "public_api_gap",
    "nonportable_workaround",
    "policy_violation",
    "inconclusive",
]


def register_session_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="mcp_session_status")
    def mcp_session_status() -> dict:
        """Return the active project, mode, build profile, and recording policy."""
        return ok(session.status())

    @mcp.tool(name="mcp_checkpoint_status")
    def mcp_checkpoint_status(checkpoint: str) -> dict:
        """Verify a Supervisor-managed checkpoint and compare it with current project files."""
        return ok(session.checkpoint_status(checkpoint))

    @mcp.tool(name="mcp_checkpoint_list")
    def mcp_checkpoint_list() -> dict:
        """List payload-verified managed checkpoints before selecting one for an attempt."""
        return ok({"checkpoints": session.list_checkpoints()})

    @mcp.tool(name="mcp_supervisor_shutdown")
    def mcp_supervisor_shutdown(lease_token: str) -> dict:
        """Ask the leased Supervisor-owned Editor to close through its normal Editor lifecycle."""
        try:
            active = session.require_supervisor_lease(lease_token)
        except session.McpPolicyError as exc:
            return fail(
                "error.supervisor_lease",
                str(exc),
                hint="Only the local Supervisor that launched this Editor may request a handoff shutdown.",
            )

        def _request_close() -> dict[str, Any]:
            from Infernux.engine.scene_manager import SceneFileManager

            manager = SceneFileManager.instance()
            if manager is None:
                raise RuntimeError("SceneFileManager is not available for a normal Editor shutdown.")
            manager.request_close()
            return {
                "close_requested": True,
                "editor_instance_id": active.editor_instance_id,
            }

        # The token is intentionally omitted from the nested trace arguments.
        return main_thread("mcp_supervisor_shutdown", _request_close, arguments={})

    @mcp.tool(name="mcp_attempt_start")
    def mcp_attempt_start(task: str, checkpoint: str = "") -> dict:
        """Start a replayable attempt; managed sessions require an explicit checkpoint."""
        return ok(session.start_attempt(task, checkpoint))

    @mcp.tool(name="mcp_attempt_stop")
    def mcp_attempt_stop() -> dict:
        """Stop and save the active project attempt trace."""
        return ok(session.stop_attempt())

    if session.current().mode == "developer_assist":
        @mcp.tool(name="public_api_validate_script")
        def public_api_validate_script(content: str, path: str = "Assets/NewScript.py") -> dict:
            """Validate a proposed project script against the public API policy."""
            return ok(session.validate_script(content, filename=path))

        @mcp.tool(name="project_script_read")
        def project_script_read(path: str) -> dict:
            """Read one project-local Python script under Assets/."""
            return ok(session.read_project_script(path))

        @mcp.tool(name="project_script_write")
        def project_script_write(path: str, content: str) -> dict:
            """Write a lint-clean project-local Python script under Assets/."""
            return ok(session.write_project_script(path, content))

        @mcp.tool(name="release_whl_read_source")
        def release_whl_read_source(wheel_path: str, member: str) -> dict:
            """Read an allowlisted text member from a wheel in release exploration only."""
            return ok(session.read_release_wheel_source(wheel_path, member))

    if session.current().mode == "global_validation":
        @mcp.tool(name="mcp_blocker_template")
        def mcp_blocker_template() -> dict:
            """Return the exact trace and evidence contract for a Repair Agent blocker report."""
            session.require_mode("global_validation")
            return ok(session.blocker_report_contract())

        @mcp.tool(name="mcp_report_blocker")
        def mcp_report_blocker(
            category: BlockerCategory,
            title: str,
            expected: str,
            actual: str,
            normal_workflow: list[str],
            logic_evidence: dict[str, Any],
            persistence_proof: str,
            severity: str = "medium",
            notes: str = "",
        ) -> dict:
            """Persist a reproducible blocker report after mcp_attempt_stop has saved its trace."""
            report = {
                "category": category,
                "title": title,
                "expected": expected,
                "actual": actual,
                "normal_workflow": normal_workflow,
                "logic_evidence": logic_evidence,
                "persistence_proof": persistence_proof,
                "severity": severity,
            }
            if notes.strip():
                report["notes"] = notes
            return ok(session.write_blocker(report))


def _register_metadata() -> None:
    for name, summary, category, side_effects in (
        (
            "mcp_session_status",
            "Read active MCP mode, build profile, project root, and recording policy.",
            "session/status",
            [],
        ),
        (
            "mcp_supervisor_shutdown",
            "Request normal Editor shutdown for a verified local Supervisor lease.",
            "session/supervisor",
            ["Requests the Editor's normal close lifecycle; it never force-terminates the process."],
        ),
        (
            "mcp_checkpoint_status",
            "Verify a Supervisor-managed checkpoint and compare its Scene/Asset ledger with the current project.",
            "session/checkpoint",
            [],
        ),
        (
            "mcp_checkpoint_list",
            "List payload-verified managed checkpoints without scanning current project files.",
            "session/checkpoint",
            [],
        ),
        (
            "public_api_validate_script",
            "Lint a proposed project script for disallowed internal/reflection imports.",
            "developer_assist/api",
            [],
        ),
        (
            "project_script_read",
            "Read one project script under Assets/ in developer_assist mode.",
            "developer_assist/scripts",
            ["Reads a project-local file."],
        ),
        (
            "project_script_write",
            "Write one lint-clean project script under Assets/ in developer_assist mode.",
            "developer_assist/scripts",
            ["Creates or changes a project-local file under Assets/."],
        ),
        (
            "release_whl_read_source",
            "Read an audited allowlisted wheel text member in release exploration.",
            "developer_assist/release",
            ["Reads an allowlisted wheel member and appends an audit event."],
        ),
        (
            "mcp_blocker_template",
            "Return the required trace sequence, categories, and evidence schema for blocker reports.",
            "global_validation/reporting",
            [],
        ),
        (
            "mcp_attempt_start",
            "Start a checkpoint-bound project attempt and trace in either constrained mode.",
            "session/attempt",
            ["Starts an MCP trace for the active validation project."],
        ),
        (
            "mcp_attempt_stop",
            "Stop and save the active project attempt trace.",
            "session/attempt",
            ["Writes a replay trace under the project MCP trace directory."],
        ),
        (
            "mcp_report_blocker",
            "Save a trace-backed blocker report for Repair Agent triage after mcp_attempt_stop.",
            "global_validation/reporting",
            ["Writes a JSON blocker report under the session artifact directory."],
        ),
    ):
        register_tool_metadata(
            name,
            summary=summary,
            category=category,
            parameters=(
                {
                    "category": {
                        "type": "string",
                        "enum": [
                            "project_bug",
                            "editor_ui_bug",
                            "engine_bug",
                            "mcp_bug",
                            "public_api_gap",
                            "nonportable_workaround",
                            "policy_violation",
                            "inconclusive",
                        ],
                        "description": "Use editor_ui_bug for missing or broken Project/Hierarchy/Inspector interactions.",
                    },
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                }
                if name == "mcp_report_blocker"
                else None
            ),
            side_effects=side_effects,
            next_suggested_tools=["mcp_report_blocker"] if name == "mcp_attempt_stop" else [],
            recovery=(
                ["Call mcp_blocker_template and choose exactly one allowed category before retrying."]
                if name == "mcp_report_blocker"
                else []
            ),
            risk_level="low" if not side_effects else "medium",
        )
