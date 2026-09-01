#!/usr/bin/env python3
"""Produce a deterministic 040 code-slimming baseline and audit inventory."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "infernux.code_slimming_audit"
SOURCE_SUFFIXES = {
    ".bat",
    ".c",
    ".cc",
    ".cjs",
    ".cmake",
    ".comp",
    ".cpp",
    ".cs",
    ".frag",
    ".glsl",
    ".h",
    ".hpp",
    ".html",
    ".in",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".txt",
    ".vert",
    ".wgsl",
    ".xml",
    ".yaml",
    ".yml",
}
ROOT_SOURCE_FILES = {
    ".gitattributes",
    ".gitignore",
    "CMakeLists.txt",
    "MANIFEST.in",
    "README-zh.md",
    "README.md",
    "pyproject.toml",
    "setup.py",
}
EXCLUDED_PREFIXES = (
    ".git/",
    "docs/wiki/",
    "external/assimp/",
    "external/glm/",
    "external/glslang/",
    "external/imgui/",
    "external/JoltPhysics/",
    "external/SDL/",
    "external/stb/",
    "external/VulkanMemoryAllocator/",
    "generated/",
    "out/",
)
EXCLUDED_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
GENERATED_SUFFIXES = (".min.js", ".min.css")
WORD_RULES = {
    "sha256-token": re.compile(r"\bsha-?256\b", re.IGNORECASE),
    "fallback-token": re.compile(r"\bfallback\b", re.IGNORECASE),
    "legacy-token": re.compile(r"\blegacy\b", re.IGNORECASE),
    "compat-token": re.compile(r"\bcompat(?:ibility)?\b", re.IGNORECASE),
    "deprecated-token": re.compile(r"\bdeprecated\b", re.IGNORECASE),
    "temporary-debug-token": re.compile(
        r"(?:\btemporary\b.*\bdebug\b|\bdebug\b.*\btemporary\b)",
        re.IGNORECASE,
    ),
}
CPP_CATCH_ALL = re.compile(r"\bcatch\s*\(\s*\.\.\.\s*\)")


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def _git_paths(repository: Path) -> set[Path]:
    output = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return {
        repository / item.decode("utf-8", errors="replace")
        for item in output.split(b"\0")
        if item
    }


def _submodule_paths(repository: Path) -> set[Path]:
    paths: set[Path] = set()
    status = _git(repository, "submodule", "status", "--recursive", check=False)
    for raw_line in status.splitlines():
        fields = raw_line.lstrip(" +-U").split()
        if len(fields) < 2:
            continue
        relative = fields[1].replace("\\", "/")
        if not relative.startswith("external/plugins/"):
            continue
        submodule = repository / relative
        if not submodule.is_dir():
            continue
        for item in _git_paths(submodule):
            paths.add(repository / relative / item.relative_to(submodule))
    return paths


def _layer(relative: str) -> str | None:
    if relative.startswith("cpp/infernux/"):
        return "runtime-cpp"
    if relative.startswith("python/Infernux/") or relative in {
        "python/infernux.py",
        "python/infernux.pyi",
    }:
        return "runtime-python"
    if relative.startswith("external/plugins/"):
        return "official-plugins"
    if relative.startswith("python/test/") or relative.startswith("cpp/tests/"):
        return "tests"
    if relative.startswith("packaging/tests/") or relative.startswith("tests/"):
        return "tests"
    if relative.startswith("scripts/") or relative.startswith("dev/tools/"):
        return "tooling"
    if relative.startswith("cmake/") or relative.startswith("packaging/"):
        return "build-packaging"
    if relative.startswith("docs/") or relative in {
        "README.md",
        "README-zh.md",
    }:
        return "documentation"
    if relative in ROOT_SOURCE_FILES or relative.startswith(".github/"):
        return "build-packaging"
    return None


def _language(path: Path) -> str:
    name = path.name
    suffix = path.suffix.lower()
    if name == "CMakeLists.txt" or suffix == ".cmake":
        return "cmake"
    if suffix in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
        return "cpp"
    if suffix == ".c":
        return "c"
    if suffix in {".py", ".pyi"}:
        return "python"
    if suffix in {".glsl", ".vert", ".frag", ".comp", ".wgsl"}:
        return "shader"
    if suffix in {".js", ".cjs"}:
        return "javascript"
    if suffix in {".md", ".html"}:
        return "documentation"
    if suffix in {".json", ".toml", ".xml", ".yaml", ".yml", ".ini"}:
        return "configuration"
    return suffix.lstrip(".") or "text"


def _included(repository: Path, path: Path) -> bool:
    if not path.is_file():
        return False
    relative = path.relative_to(repository).as_posix()
    if relative.endswith(GENERATED_SUFFIXES):
        return False
    if relative.startswith("docs/learn/") and path.suffix.lower() == ".html":
        return False
    if any(relative.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    if any(part in EXCLUDED_PARTS for part in path.relative_to(repository).parts):
        return False
    if path.suffix.lower() not in SOURCE_SUFFIXES and path.name not in ROOT_SOURCE_FILES:
        return False
    return _layer(relative) is not None


def owned_source_paths(repository: Path) -> list[Path]:
    candidates = _git_paths(repository) | _submodule_paths(repository)
    return sorted(path for path in candidates if _included(repository, path))


def _line_metrics(text: str) -> dict[str, int]:
    lines = text.splitlines()
    nonblank = [line for line in lines if line.strip()]
    comment_prefixes = ("#", "//", "/*", "*", "--", "<!--")
    logical = [
        line
        for line in nonblank
        if not line.lstrip().startswith(comment_prefixes)
    ]
    return {
        "physical_lines": len(lines),
        "nonblank_lines": len(nonblank),
        "logical_lines": len(logical),
    }


def _empty_default(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in (None, False, True, "", 0)
    return isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)) and not any(
        ast.iter_child_nodes(node)
    )


def _python_candidates(relative: str, text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return [
            {
                "rule": "python-parse-error",
                "path": relative,
                "line": int(exc.lineno or 0),
                "snippet": str(exc.msg),
                "layer": _layer(relative) or "unknown",
                "classification": "unclassified",
            }
        ]

    candidates: list[dict[str, Any]] = []
    lines = text.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        type_name = ""
        if node.type is None:
            type_name = "bare"
        elif isinstance(node.type, ast.Name):
            type_name = node.type.id
        elif isinstance(node.type, ast.Attribute):
            type_name = node.type.attr
        if type_name in {"bare", "BaseException", "Exception"}:
            candidates.append(
                _candidate(
                    "python-broad-except", relative, node.lineno, lines
                )
            )
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            candidates.append(
                _candidate(
                    "python-silent-except", relative, node.lineno, lines
                )
            )
        if (
            len(node.body) == 1
            and isinstance(node.body[0], ast.Return)
            and _empty_default(node.body[0].value)
        ):
            candidates.append(
                _candidate(
                    "python-default-on-error", relative, node.lineno, lines
                )
            )
    return candidates


def _candidate(
    rule: str, relative: str, line_number: int, lines: list[str]
) -> dict[str, Any]:
    snippet = lines[line_number - 1].strip() if 0 < line_number <= len(lines) else ""
    return {
        "rule": rule,
        "path": relative,
        "line": line_number,
        "snippet": snippet[:240],
        "layer": _layer(relative) or "unknown",
        "classification": "unclassified",
    }


def audit_paths(
    repository: Path, paths: Iterable[Path]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    totals = Counter()
    by_layer: dict[str, Counter[str]] = {}
    by_language: dict[str, Counter[str]] = {}
    candidates: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(repository).as_posix()
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        layer = _layer(relative)
        if layer is None:
            continue
        language = _language(path)
        metrics = Counter(_line_metrics(text))
        metrics.update(files=1, bytes=len(raw))
        totals.update(metrics)
        by_layer.setdefault(layer, Counter()).update(metrics)
        by_language.setdefault(language, Counter()).update(metrics)
        lines = text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            for rule, pattern in WORD_RULES.items():
                if pattern.search(line):
                    candidates.append(_candidate(rule, relative, line_number, lines))
            if CPP_CATCH_ALL.search(line):
                candidates.append(
                    _candidate("cpp-catch-all", relative, line_number, lines)
                )
        if path.suffix.lower() in {".py", ".pyi"}:
            candidates.extend(_python_candidates(relative, text))

    candidates.sort(key=lambda item: (item["path"], item["line"], item["rule"]))
    metrics_payload = {
        "definition": {
            "physical_lines": "Unicode text lines",
            "nonblank_lines": "physical lines containing non-whitespace",
            "logical_lines": (
                "nonblank lines whose first token is not a language comment marker"
            ),
            "bytes": "uncompressed UTF-8 source file bytes",
        },
        "totals": dict(sorted(totals.items())),
        "by_layer": {
            key: dict(sorted(value.items())) for key, value in sorted(by_layer.items())
        },
        "by_language": {
            key: dict(sorted(value.items()))
            for key, value in sorted(by_language.items())
        },
    }
    return metrics_payload, candidates


def _evidence_summary(evidence_root: Path | None) -> dict[str, Any]:
    if evidence_root is None:
        return {"root": "", "reports": [], "artifact_bytes": 0}
    reports: list[dict[str, Any]] = []
    artifact_bytes = 0
    for path in sorted(evidence_root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or "status" not in payload:
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        elapsed = payload.get("elapsed_seconds", result.get("elapsed_seconds", 0.0))
        artifacts = payload.get("artifacts", [])
        report_bytes = sum(
            _artifact_size(item)
            for item in artifacts
            if isinstance(item, dict)
        )
        artifact_bytes += report_bytes
        reports.append(
            {
                "path": path.relative_to(evidence_root).as_posix(),
                "status": str(payload.get("status", "")),
                "target": str(payload.get("target", result.get("game", ""))),
                "elapsed_seconds": float(elapsed or 0.0),
                "artifact_bytes": report_bytes,
                "progress_events": int(
                    payload.get("progress_summary", {}).get("event_count", 0) or 0
                ),
                "retained_log_lines": len(payload.get("log_tail", [])),
            }
        )
    return {
        "root": str(evidence_root.resolve()),
        "reports": reports,
        "artifact_bytes": artifact_bytes,
    }


def _artifact_size(item: dict[str, Any]) -> int:
    declared = int(item.get("size", 0) or 0)
    if declared > 0:
        return declared
    raw_path = str(item.get("path", "") or "").strip()
    if not raw_path:
        return 0
    path = Path(raw_path)
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
    return 0


def build_audit(
    repository: Path, evidence_root: Path | None = None
) -> dict[str, Any]:
    repository = repository.resolve()
    paths = owned_source_paths(repository)
    metrics, candidates = audit_paths(repository, paths)
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    counts = Counter(item["rule"] for item in candidates)
    layer_counts = Counter(item["layer"] for item in candidates)
    layer_rule_counts: dict[str, Counter[str]] = {}
    for item in candidates:
        layer_rule_counts.setdefault(item["layer"], Counter()).update([item["rule"]])
    product_layers = {
        "runtime-cpp",
        "runtime-python",
        "official-plugins",
        "build-packaging",
        "tooling",
    }
    return {
        "$schema": SCHEMA,
        "scope": {
            "repository": str(repository),
            "owned_files": len(paths),
            "excluded": [
                "third-party source under external/ except external/plugins",
                "docs/wiki",
                "generated Learn HTML and minified assets",
                "build, out, dist, caches, and ignored files",
            ],
        },
        "source_state": {
            "branch": _git(repository, "branch", "--show-current").strip(),
            "commit": _git(repository, "rev-parse", "HEAD").strip(),
            "commit_time": _git(repository, "show", "-s", "--format=%cI", "HEAD").strip(),
            "dirty": bool(status.strip()),
        },
        "metrics": metrics,
        "evidence": _evidence_summary(evidence_root),
        "candidate_inventory": {
            "policy": (
                "Static matches are unclassified audit candidates, not deletion orders."
            ),
            "count": len(candidates),
            "product_count": sum(
                1 for item in candidates if item["layer"] in product_layers
            ),
            "by_rule": dict(sorted(counts.items())),
            "by_layer": dict(sorted(layer_counts.items())),
            "by_layer_and_rule": {
                layer: dict(sorted(rules.items()))
                for layer, rules in sorted(layer_rule_counts.items())
            },
            "items": candidates,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    payload = build_audit(arguments.repository, arguments.evidence_root)
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "output": str(output),
                "owned_files": payload["scope"]["owned_files"],
                "physical_lines": payload["metrics"]["totals"]["physical_lines"],
                "logical_lines": payload["metrics"]["totals"]["logical_lines"],
                "candidates": payload["candidate_inventory"]["count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
