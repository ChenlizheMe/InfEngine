#!/usr/bin/env python3
"""Build one Infernux Player target and write machine-readable evidence."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import MutableMapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_EDITORS = {
    "windows": REPOSITORY_ROOT / "external" / "plugins" / "infernux_windows" / "package" / "editor",
    "linux": REPOSITORY_ROOT / "external" / "plugins" / "infernux_linux" / "package" / "editor",
    "android": REPOSITORY_ROOT / "external" / "plugins" / "infernux_android" / "package" / "editor",
    "web": REPOSITORY_ROOT / "external" / "plugins" / "infernux_web" / "package" / "editor",
}
EXPORTERS = {
    "android-arm64": ("android", "infernux_android", "AndroidPlatformExporter"),
    "android-x64-emulator": (
        "android",
        "infernux_android",
        "AndroidPlatformExporter",
    ),
    "web-wasm32": ("web", "infernux_web", "WebPlatformExporter"),
}


def _desktop_target_for_host() -> str | None:
    machine = platform.machine().strip().casefold()
    if machine not in {"amd64", "x86_64"}:
        return None
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform.startswith("linux"):
        return "linux-x64"
    return None


DESKTOP_TARGET = _desktop_target_for_host()
if DESKTOP_TARGET == "windows-x64":
    EXPORTERS[DESKTOP_TARGET] = (
        "windows",
        "infernux_windows",
        "WindowsPlatformExporter",
    )
elif DESKTOP_TARGET == "linux-x64":
    EXPORTERS[DESKTOP_TARGET] = (
        "linux",
        "infernux_linux",
        "LinuxPlatformExporter",
    )
SUPPORTED_TARGETS = tuple(sorted(EXPORTERS))


def _parse_option(value: str) -> tuple[str, object]:
    key, separator, encoded = value.partition("=")
    key = key.strip()
    if not separator or not key:
        raise argparse.ArgumentTypeError("build options must use KEY=JSON syntax")
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(
            f"invalid JSON value for build option {key!r}: {error.msg}"
        ) from error
    return key, decoded


def _load_exporter(target: str):
    try:
        plugin, module_name, class_name = EXPORTERS[target]
    except KeyError as error:
        supported = ", ".join(SUPPORTED_TARGETS)
        raise ValueError(f"unsupported Player target {target!r}; choose {supported}") from error
    editor_root = str(PLUGIN_EDITORS[plugin])
    if editor_root not in sys.path:
        sys.path.insert(0, editor_root)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def _configure_source_player_host(
    environment: MutableMapping[str, str] | None = None,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path | None:
    """Bind the host owned by this source tree's canonical release preset."""
    accepted_environment = os.environ if environment is None else environment
    configured = accepted_environment.get("INFERNUX_PLAYER_HOST_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    host_name = (
        "InfernuxPlayerHost.exe" if sys.platform == "win32" else "InfernuxPlayerHost"
    )
    preset = (
        "windows-msvc-release"
        if sys.platform == "win32"
        else "linux-clang-release"
    )
    candidate = (
        repository_root.expanduser().resolve()
        / "out"
        / "build"
        / preset
        / "player-runtime"
        / host_name
    )
    if not candidate.is_file():
        return None
    accepted_environment["INFERNUX_PLAYER_HOST_PATH"] = str(candidate)
    return candidate


def _diagnostic_payload(item) -> dict[str, object]:
    return {
        "severity": item.severity.value,
        "code": item.code,
        "message": item.message,
        "source": item.source,
        "detail": dict(item.detail),
    }


def _is_verbose_progress(item) -> bool:
    """Return whether an event is raw subprocess output rather than a build phase."""
    source = str(dict(item.detail).get("source", "")).casefold()
    return source in {"cmake", "gradle"}


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="Infernux project root")
    parser.add_argument("target", choices=SUPPORTED_TARGETS)
    parser.add_argument("output", type=Path, help="published Player output directory")
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON evidence path (default: <output>/build-evidence.json)",
    )
    parser.add_argument(
        "--configuration",
        choices=("development", "release"),
        default="development",
    )
    parser.add_argument("--debug-symbols", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--compress-resources", action=argparse.BooleanOptionalAction, default=False
    )
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        type=_parse_option,
        metavar="KEY=JSON",
        help="exporter option; repeat for multiple values",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    project = arguments.project.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    report_path = (
        arguments.report.expanduser().resolve()
        if arguments.report is not None
        else output / "build-evidence.json"
    )
    if not (project / "Assets").is_dir() or not (project / "ProjectSettings").is_dir():
        payload = {
            "schema": "infernux.build_evidence",
            "status": "invalid-project",
            "project": str(project),
            "target": arguments.target,
            "diagnostics": [
                {
                    "severity": "error",
                    "code": "build.project.invalid",
                    "message": "Project root must contain Assets and ProjectSettings directories.",
                    "source": "scripts/acceptance/build_player.py",
                    "detail": {},
                }
            ],
        }
        _write_report(report_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    python_root = str(REPOSITORY_ROOT / "python")
    if python_root not in sys.path:
        sys.path.insert(0, python_root)
    _configure_source_player_host()
    from Infernux.engine.build import (
        BuildConfiguration,
        BuildExporterRegistry,
        BuildProfile,
        BuildRequest,
        BuildService,
        BuildUnavailableError,
    )

    progress: list[dict[str, object]] = []
    progress_phase_counts: Counter[str] = Counter()
    progress_event_count = 0
    omitted_verbose_progress = 0

    def on_progress(item) -> None:
        nonlocal progress_event_count, omitted_verbose_progress
        record = {
            "phase": item.phase,
            "completed": item.completed,
            "total": item.total,
            "message": item.message,
            "detail": dict(item.detail),
        }
        progress_event_count += 1
        progress_phase_counts[str(item.phase)] += 1
        if _is_verbose_progress(item):
            omitted_verbose_progress += 1
        else:
            progress.append(record)
        print(f"[{item.phase}] {item.message}", flush=True)

    def progress_summary() -> dict[str, object]:
        return {
            "event_count": progress_event_count,
            "retained_count": len(progress),
            "omitted_verbose_count": omitted_verbose_progress,
            "phase_counts": dict(sorted(progress_phase_counts.items())),
        }

    options = dict(arguments.option)
    request = BuildRequest(
        str(project),
        arguments.target,
        str(output),
        BuildProfile(
            configuration=BuildConfiguration(arguments.configuration),
            debug_symbols=arguments.debug_symbols,
            compress_resources=arguments.compress_resources,
            options=options,
        ),
        progress=on_progress,
    )
    exporter = _load_exporter(arguments.target)
    registry = BuildExporterRegistry()
    registry.register("scripts/acceptance/build-player", exporter)
    service = BuildService(registry)
    try:
        plan = service.create_plan(request)
        result = service.execute(request, plan)
    except BuildUnavailableError as error:
        payload = {
            "schema": "infernux.build_evidence",
            "status": "doctor-failed",
            "project": str(project),
            "target": arguments.target,
            "output": str(output),
            "options": options,
            "diagnostics": [_diagnostic_payload(item) for item in error.diagnostics],
            "progress": progress,
            "progress_summary": progress_summary(),
        }
        _write_report(report_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    payload = {
        "schema": "infernux.build_evidence",
        "status": "passed" if result.success else "failed",
        "project": str(project),
        "target": str(result.target),
        "output": str(output),
        "configuration": arguments.configuration,
        "debug_symbols": arguments.debug_symbols,
        "compress_resources": arguments.compress_resources,
        "options": options,
        "elapsed_seconds": result.elapsed_seconds,
        "manifest": dict(result.manifest),
        "artifacts": [
            {
                "path": item.path,
                "kind": item.kind,
                "size": item.size,
            }
            for item in result.artifacts
        ],
        "diagnostics": [_diagnostic_payload(item) for item in result.diagnostics],
        "progress": progress,
        "progress_summary": progress_summary(),
        "log_tail": list(result.logs[-300:]),
    }
    _write_report(report_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
