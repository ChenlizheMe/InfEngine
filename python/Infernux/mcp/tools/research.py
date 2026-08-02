"""Research-grade MCP meta-tools: config, contracts, and evolution hints."""

from __future__ import annotations

import os
from typing import Any

from Infernux.engine.path_utils import relative_path
from Infernux.mcp import capabilities
from Infernux.mcp.project_tools.trace import current_trace, last_trace, list_traces
from Infernux.mcp.tools.common import get_tool_metadata, list_tool_metadata, ok, register_tool_metadata


def register_research_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="mcp_config_get")
    def mcp_config_get() -> dict:
        """Return the active configurable MCP capability profile."""
        return ok({
            "config": capabilities.current_config(),
            "config_path": capabilities.config_path(project_path),
            "restart_required_for_registration_changes": True,
        })

    @mcp.tool(name="mcp_config_write_default")
    def mcp_config_write_default() -> dict:
        """Materialize the default-on MCP capability config file."""
        path = capabilities.write_default_config(project_path)
        return ok({"path": _rel(project_path, path), "written_or_existing": bool(path)})

    @mcp.tool(name="mcp_config_set_feature")
    def mcp_config_set_feature(name: str, enabled: bool, persist: bool = True) -> dict:
        """Enable or disable one MCP feature flag."""
        config = capabilities.set_feature(name, enabled)
        path = capabilities.save_config(config, project_path) if persist else ""
        return ok({
            "config": config,
            "persisted_path": _rel(project_path, path) if path else "",
            "restart_required_for_registration_changes": True,
        })

    @mcp.tool(name="mcp_config_set_tool_group")
    def mcp_config_set_tool_group(name: str, enabled: bool, persist: bool = True) -> dict:
        """Enable or disable one MCP tool group for the next server registration."""
        config = capabilities.set_tool_group(name, enabled)
        path = capabilities.save_config(config, project_path) if persist else ""
        return ok({
            "config": config,
            "persisted_path": _rel(project_path, path) if path else "",
            "restart_required_for_registration_changes": True,
        })

    @mcp.tool(name="mcp_config_set_tool")
    def mcp_config_set_tool(name: str, enabled: bool, persist: bool = True) -> dict:
        """Enable or disable one specific tool name in config metadata."""
        config = capabilities.set_tool_enabled(name, enabled)
        path = capabilities.save_config(config, project_path) if persist else ""
        return ok({
            "config": config,
            "persisted_path": _rel(project_path, path) if path else "",
            "restart_required_for_registration_changes": True,
        })

    @mcp.tool(name="mcp_contracts_list")
    def mcp_contracts_list() -> dict:
        """Return executable contract metadata for every known tool."""
        return ok({"contracts": [_contract(meta) for meta in _visible_metadata()]})

    @mcp.tool(name="mcp_contracts_get")
    def mcp_contracts_get(tool_name: str) -> dict:
        """Return the executable contract metadata for one tool."""
        return ok({"contract": _contract(get_tool_metadata(tool_name))})

    @mcp.tool(name="mcp_contracts_validate")
    def mcp_contracts_validate() -> dict:
        """Grade tool metadata for self-description and recovery quality."""
        contracts = [_contract(meta) for meta in _visible_metadata()]
        threshold = float((capabilities.current_config().get("contracts") or {}).get("grade_threshold", 0.70))
        failing = [item for item in contracts if item["grade"] < threshold]
        return ok({
            "passed": not failing,
            "threshold": threshold,
            "tool_count": len(contracts),
            "failing": failing,
            "summary": {
                "average_grade": round(sum(item["grade"] for item in contracts) / max(len(contracts), 1), 3),
                "missing_recovery": [item["name"] for item in contracts if not item["recovery"]],
                "missing_side_effects": [item["name"] for item in contracts if _looks_mutating(item["name"]) and not item["side_effects"]],
            },
        })

    @mcp.tool(name="mcp_evolution_suggest_tools")
    def mcp_evolution_suggest_tools(use_last_trace: bool = True, min_sequence_length: int = 3) -> dict:
        """Suggest project-defined tools from failed or repetitive trace patterns."""
        if not capabilities.feature_enabled("trace_to_tool_evolution"):
            return ok({"enabled": False, "suggestions": []})
        trace = (last_trace().get("trace") if use_last_trace else current_trace().get("trace")) or {}
        suggestions = _suggest_tools_from_trace(trace, min_sequence_length=min_sequence_length)
        return ok({
            "enabled": True,
            "trace_id": trace.get("trace_id", ""),
            "suggestions": suggestions,
            "generated_tool_root": (capabilities.current_config().get("evolution") or {}).get("generated_tool_root", "Assets/AgentTools/generated"),
            "saved_traces": list_traces(project_path, limit=10),
        })

    @mcp.tool(name="mcp_research_profile")
    def mcp_research_profile() -> dict:
        """Return the research claims this MCP configuration is designed to support."""
        return ok({
            "profile": capabilities.profile_name(),
            "claims": [
                "Self-describing tools reduce engine-specific hallucinations.",
                "Executable contracts make long-horizon tool use easier to validate and repair.",
                "Trace-driven project tools close capability gaps inside the target environment.",
                "Transactions reduce partial-state damage during multi-step game generation.",
                "Runtime observation grounds generated games in executable behavior.",
            ],
            "recommended_ablations": [
                "Disable executable_contracts.",
                "Disable trace_to_tool_evolution.",
                "Disable transactions.",
                "Disable runtime_observation.",
                "Compare low-level tools only vs semantic workflows.",
            ],
        })


def _contract(meta: dict[str, Any]) -> dict[str, Any]:
    name = str(meta.get("name", ""))
    checks = {
        "has_summary": bool(meta.get("summary")),
        "has_recovery": bool(meta.get("recovery")),
        "has_next_tools": bool(meta.get("next_suggested_tools")),
        "has_concepts": bool(meta.get("concepts")),
        "has_side_effects_when_mutating": bool(meta.get("side_effects")) if _looks_mutating(name) else True,
    }
    grade = sum(1 for value in checks.values() if value) / max(len(checks), 1)
    return {
        "name": name,
        "summary": meta.get("summary", ""),
        "preconditions": meta.get("preconditions", []),
        "postconditions": meta.get("postconditions", []),
        "side_effects": meta.get("side_effects", []),
        "recovery": meta.get("recovery", []),
        "next_suggested_tools": meta.get("next_suggested_tools", []),
        "concepts": meta.get("concepts", {}),
        "invariants": meta.get("invariants", []),
        "risk_level": meta.get("risk_level", "medium"),
        "feature": meta.get("feature", ""),
        "checks": checks,
        "grade": round(grade, 3),
    }


def _suggest_tools_from_trace(trace: dict[str, Any], *, min_sequence_length: int) -> list[dict[str, Any]]:
    steps = trace.get("steps") or []
    suggestions = []
    failed = [step for step in steps if not step.get("ok")]
    if failed:
        tool_names = sorted({str(step.get("tool", "")) for step in failed if step.get("tool")})
        suggestions.append({
            "kind": "failure_recovery_tool",
            "name": "project.recover_" + "_".join(tool_names[:2]).replace(".", "_"),
            "summary": "Project-specific recovery helper for repeated MCP failures.",
            "source_trace": trace.get("trace_id", ""),
            "evidence": failed[-5:],
            "template": _tool_template("project.recover_generated_failure", "Recover from failures observed in MCP trace."),
        })
    repeated = _find_repeated_sequence([str(step.get("tool", "")) for step in steps], max(int(min_sequence_length), 2))
    if repeated:
        suggestions.append({
            "kind": "workflow_compression_tool",
            "name": "project.workflow_" + "_then_".join(item.split(".")[-1] for item in repeated[:3]),
            "summary": "Compress a repeated MCP tool sequence into one project-defined semantic tool.",
            "source_trace": trace.get("trace_id", ""),
            "sequence": repeated,
            "template": _tool_template("project.generated_workflow", "Run a repeated workflow discovered from an MCP trace."),
        })
    return suggestions


def _find_repeated_sequence(names: list[str], min_len: int) -> list[str]:
    clean = [name for name in names if name]
    for width in range(min(8, len(clean) // 2), min_len - 1, -1):
        seen: dict[tuple[str, ...], int] = {}
        for index in range(0, len(clean) - width + 1):
            seq = tuple(clean[index:index + width])
            seen[seq] = seen.get(seq, 0) + 1
            if seen[seq] >= 2:
                return list(seq)
    return []


def _tool_template(name: str, summary: str) -> str:
    return (
        "from Infernux.mcp.project_tools import agent_tool\n\n\n"
        f"@agent_tool(name=\"{name}\", summary=\"{summary}\", generated=True)\n"
        "def generated_tool() -> dict:\n"
        "    return {\"ok\": True, \"message\": \"Implement project-specific workflow here.\"}\n"
    )


def _looks_mutating(name: str) -> bool:
    verbs = (".create", ".write", ".edit", ".delete", ".move", ".rename", ".copy", ".set", ".add", ".remove", ".save", ".open", ".new", ".play", ".stop", ".begin", ".rollback", ".commit")
    return any(verb in name for verb in verbs)


def _visible_metadata() -> list[dict[str, Any]]:
    return [meta for meta in list_tool_metadata() if capabilities.tool_enabled(meta["name"])]


def _rel(project_path: str, path: str) -> str:
    if not path:
        return ""
    try:
        return relative_path(path, project_path, allow_root=True)
    except Exception:
        return path


def _register_metadata() -> None:
    for name, summary in {
        "mcp_config_get": "Return active MCP capability toggles.",
        "mcp_config_write_default": "Write the default-on MCP capability config.",
        "mcp_config_set_feature": "Toggle one MCP feature flag.",
        "mcp_config_set_tool_group": "Toggle one MCP tool group.",
        "mcp_config_set_tool": "Enable or disable one tool by name.",
        "mcp_contracts_list": "List executable/self-description contracts for tools.",
        "mcp_contracts_get": "Inspect one tool contract.",
        "mcp_contracts_validate": "Grade MCP tool contract completeness.",
        "mcp_evolution_suggest_tools": "Suggest project tools from trace failure/repetition patterns.",
        "mcp_research_profile": "Return research claims and ablation plan for this MCP layer.",
    }.items():
        register_tool_metadata(
            name,
            summary=summary,
            side_effects=["May read or update ProjectSettings/mcp_capabilities.json." if name.startswith("mcp.config.") else "No editor-scene mutation."],
            recovery=["Use mcp_config_get to inspect current toggles.", "Restart the MCP server after changing registration-level tool groups."],
            concepts={"MCP Capability": "A feature or tool group that can be enabled or disabled through project config."},
            next_suggested_tools=["mcp_config_get", "mcp_capabilities"],
            feature="executable_contracts",
        )
