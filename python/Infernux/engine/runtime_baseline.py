"""Stable, opt-in runtime baseline recording and comparison.

The recorder never samples the engine on its own.  A benchmark, acceptance
script, or MCP diagnostic explicitly asks a runner to read existing counters.
Normal Editor and Player frames therefore pay no baseline-recording cost.
"""

from __future__ import annotations

import argparse
import copy
import importlib.machinery
import json
import math
import os
import platform
import sys
import threading
import uuid
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from time import perf_counter_ns
from typing import Any


RUNTIME_BASELINE_SCHEMA_VERSION = 1
RUNTIME_BASELINE_SCHEMA_ID = (
    "https://infernux-engine.com/schemas/runtime-baseline-v1.schema.json"
)
RUNTIME_BASELINE_REPORT_SCHEMA_ID = (
    "https://infernux-engine.com/schemas/runtime-baseline-comparison-v1.schema.json"
)

RUNTIME_BASELINE_COMPONENT_COUNTS = (0, 1, 100, 1_000, 10_000)
RUNTIME_BASELINE_LIFECYCLE_CALLBACKS = (
    "awake",
    "start",
    "on_enable",
    "fixed_update",
    "physics_callback",
    "update",
    "late_update",
    "on_disable",
    "on_destroy",
)
RUNTIME_BASELINE_MUTATION_DOMAINS = (
    "component_data",
    "transform_local",
    "transform_world",
    "rigidbody",
    "hierarchy",
    "render_extraction",
)
RUNTIME_BASELINE_WORKLOADS = (
    "no_op",
    "cds_field",
    "transform",
    "coroutine",
    "exception",
    "batch",
    "jit",
)
RUNTIME_BASELINE_SCENARIO_GROUPS = {
    "ui": (
        "editor-empty-panels",
        "inspector-static-target",
        "inspector-runtime-fields",
        "inspector-material",
        "inspector-render-stack",
        "hidden-panels",
        "file-manager",
    ),
    "preview": (
        "material-preview",
        "mesh-preview",
        "prefab-preview",
    ),
    "scene": (
        "empty-shadow-off",
        "empty-shadow-on",
        "balance-game",
        "animation-stress",
        "gpu-particle-stress",
        "gizmo-selection",
        "ui-scene",
    ),
}
RUNTIME_BASELINE_SCENARIOS = tuple(
    scenario
    for group in ("ui", "preview", "scene")
    for scenario in RUNTIME_BASELINE_SCENARIO_GROUPS[group]
)
RUNTIME_BASELINE_TIMING_FIELDS = (
    "sample_count",
    "avg_ms",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "min_ms",
    "max_ms",
)
RUNTIME_BASELINE_COUNTERS = (
    "gil_acquires",
    "python_calls",
    "engine_api_crossings",
    "tree_object_visits",
    "tree_component_visits",
    "scheduler_plan_builds",
    "scheduler_plan_hits",
    "scheduler_phase_dispatches",
    "scheduler_phase_skips",
    "scheduler_phase_errors",
    "journal_publishes",
    "journal_coalesced_changes",
    "descriptor_updates",
    "pipeline_builds",
    "command_buffer_submissions",
    "fence_waits",
    "resource_upload_bytes",
)

_IDENTITY_FIELDS = (
    "build_configuration",
    "application_role",
    "runtime_mode",
    "flavor",
    "platform",
    "architecture",
    "python_version",
    "native_profile_enabled",
    "engine_version",
    "build_preset",
)
_LOWER_IS_BETTER_SUFFIXES = (
    "_ms",
    "_bytes",
    "_count",
    "_calls",
    "_visits",
    "_acquires",
    "_crossings",
)
_HIGHER_IS_BETTER_SUFFIXES = ("_fps", "_throughput")
_ENGINE_RECORDERS: weakref.WeakKeyDictionary[Any, "RuntimeBaselineRecorder"] = (
    weakref.WeakKeyDictionary()
)
_ENGINE_RECORDERS_LOCK = threading.RLock()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _clean_text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _finite_number(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _non_negative_int(value: Any, name: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("baseline documents cannot contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_stable_value(item) for item in value), key=repr)
    return str(value)


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * percentile
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return float(sorted_values[lower])
    weight = rank - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def summarize_samples(values: Iterable[float]) -> dict[str, float | int]:
    samples = sorted(_finite_number(value, "sample") for value in values)
    if not samples:
        return {
            "sample_count": 0,
            "avg_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
        }
    return {
        "sample_count": len(samples),
        "avg_ms": fmean(samples),
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
        "min_ms": samples[0],
        "max_ms": samples[-1],
    }


@dataclass(frozen=True)
class RuntimeBaselineIdentity:
    build_configuration: str
    application_role: str
    runtime_mode: str
    flavor: str
    platform: str
    architecture: str
    python_version: str
    native_profile_enabled: bool
    engine_version: str = ""
    build_preset: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_configuration": self.build_configuration,
            "application_role": self.application_role,
            "runtime_mode": self.runtime_mode,
            "flavor": self.flavor,
            "platform": self.platform,
            "architecture": self.architecture,
            "python_version": self.python_version,
            "native_profile_enabled": self.native_profile_enabled,
            "engine_version": self.engine_version,
            "build_preset": self.build_preset,
        }


@dataclass(frozen=True)
class RuntimeWorkloadRequest:
    workload: str
    component_count: int
    repeat_index: int


@dataclass(frozen=True)
class RuntimeScenarioRequest:
    scenario: str
    group: str
    repeat_index: int


def detect_runtime_identity(
    engine: Any = None,
    *,
    build_configuration: str | None = None,
    application_role: str | None = None,
    runtime_mode: str | None = None,
    build_preset: str | None = None,
) -> RuntimeBaselineIdentity:
    native_profile_enabled = False
    try:
        from Infernux.lib import is_frame_profile_enabled

        native_profile_enabled = bool(is_frame_profile_enabled())
    except (ImportError, AttributeError, RuntimeError):
        pass

    configured_build = _clean_text(
        build_configuration
        or os.environ.get("INFERNUX_BUILD_CONFIGURATION")
        or os.environ.get("_INFERNUX_BUILD_CONFIGURATION")
        or ("Debug" if native_profile_enabled else "Release")
    )
    role = _clean_text(
        application_role
        or getattr(engine, "_application_role", "")
        or ("Player" if os.environ.get("_INFERNUX_PLAYER_MODE") else "Editor")
    ).title()
    mode_value = runtime_mode or getattr(engine, "_mode", "Graphical")
    mode = _clean_text(getattr(mode_value, "name", mode_value), fallback="Graphical")
    preset = _clean_text(
        build_preset
        or os.environ.get("INFERNUX_BUILD_PRESET")
        or os.environ.get("_INFERNUX_BUILD_PRESET")
    )
    engine_version = ""
    try:
        from importlib.metadata import version

        engine_version = version("infernux")
    except Exception:
        pass

    build_label = (
        "Debug"
        if configured_build.casefold()
        in {
            "debug",
            "relwithdebinfo",
            "debug-no-vulkan-validation",
            "debug-profile",
        }
        else "Release"
    )
    return RuntimeBaselineIdentity(
        build_configuration=build_label,
        application_role=role,
        runtime_mode=mode,
        flavor=f"{role}{build_label}",
        platform=platform.system() or sys.platform,
        architecture=platform.machine(),
        python_version=platform.python_version(),
        native_profile_enabled=native_profile_enabled,
        engine_version=engine_version,
        build_preset=preset,
    )


class RuntimeBaselineRecorder:
    """Thread-safe owner of one deterministic baseline document."""

    def __init__(
        self,
        identity: RuntimeBaselineIdentity,
        *,
        suite_id: str = "runtime-r0-r6",
        project_id: str = "",
        capture_id: str | None = None,
        created_utc: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._capture = {
            "capture_id": _clean_text(capture_id, fallback=uuid.uuid4().hex),
            "suite_id": _clean_text(suite_id, fallback="runtime-r0-r6"),
            "project_id": _clean_text(project_id),
            "created_utc": _clean_text(created_utc, fallback=_utc_now()),
            "identity": identity.to_dict(),
        }
        self._lifecycle_events: list[dict[str, Any]] = []
        self._mutations: list[dict[str, Any]] = []
        self._workloads: dict[tuple[str, int], dict[str, Any]] = {}
        self._performance_windows: dict[str, dict[str, Any]] = {}
        self._inventory = {
            "services": [],
            "threads": [],
            "watchers": [],
            "python_modules": [],
            "native_modules": [],
        }
        self._inventory_captured = False
        self._diagnostics: list[dict[str, Any]] = []
        self._sequence = 0

    @property
    def identity(self) -> RuntimeBaselineIdentity:
        data = self._capture["identity"]
        return RuntimeBaselineIdentity(**data)

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def record_lifecycle_event(
        self,
        *,
        frame: int,
        callback: str,
        phase: str = "",
        object_id: int | str = "",
        component_id: int | str = "",
        type_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._lifecycle_events.append(
                {
                    "sequence": self._next_sequence(),
                    "frame": _non_negative_int(frame, "frame"),
                    "phase": _clean_text(phase),
                    "callback": _clean_text(callback, fallback="unknown"),
                    "object_id": object_id,
                    "component_id": component_id,
                    "type_id": _clean_text(type_id),
                    "details": _stable_value(details or {}),
                }
            )

    def record_mutation_visibility(
        self,
        *,
        frame: int,
        domain: str,
        mutation_phase: str,
        observed_phase: str,
        visible_same_frame: bool,
        stable_id: int | str = "",
        field_id: int | str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._mutations.append(
                {
                    "sequence": self._next_sequence(),
                    "frame": _non_negative_int(frame, "frame"),
                    "domain": _clean_text(domain, fallback="unknown"),
                    "mutation_phase": _clean_text(mutation_phase, fallback="unknown"),
                    "observed_phase": _clean_text(observed_phase, fallback="unknown"),
                    "visible_same_frame": bool(visible_same_frame),
                    "stable_id": stable_id,
                    "field_id": field_id,
                    "details": _stable_value(details or {}),
                }
            )

    def record_workload(
        self,
        *,
        workload: str,
        component_count: int,
        samples_ms: Iterable[float] | None = None,
        user_script_samples_ms: Iterable[float] | None = None,
        engine_overhead_samples_ms: Iterable[float] | None = None,
        timing: Mapping[str, Any] | None = None,
        counters: Mapping[str, int | float] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        name = _clean_text(workload)
        if name not in RUNTIME_BASELINE_WORKLOADS:
            raise ValueError(f"unknown runtime baseline workload: {workload}")
        count = _non_negative_int(component_count, "component_count")
        if count not in RUNTIME_BASELINE_COMPONENT_COUNTS:
            raise ValueError(f"unsupported runtime baseline component count: {count}")
        if (samples_ms is None) == (timing is None):
            raise ValueError("provide exactly one of samples_ms or timing")
        if timing is None:
            normalized_timing = {
                "total": summarize_samples(samples_ms or ()),
                "user_script": (
                    summarize_samples(user_script_samples_ms)
                    if user_script_samples_ms is not None
                    else None
                ),
                "engine_overhead": (
                    summarize_samples(engine_overhead_samples_ms)
                    if engine_overhead_samples_ms is not None
                    else None
                ),
            }
        else:
            normalized_timing = _normalize_workload_timing(timing)
        with self._lock:
            self._workloads[(name, count)] = {
                "workload": name,
                "component_count": count,
                "timing": normalized_timing,
                "counters": _normalize_counters(counters),
                "metadata": _stable_value(metadata or {}),
            }

    def record_performance_window(
        self,
        *,
        name: str,
        sample_count: int,
        cpu_sections: Mapping[str, Mapping[str, Any]],
        gpu_sections: Mapping[str, Mapping[str, Any]] | None = None,
        counters: Mapping[str, int | float] | None = None,
        first_frame: int = 0,
        last_frame: int = 0,
        dropped_sample_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        window_name = _clean_text(name, fallback="runtime")
        if window_name not in RUNTIME_BASELINE_SCENARIOS:
            raise ValueError(
                f"unknown runtime baseline performance scenario: {window_name}"
            )
        with self._lock:
            self._performance_windows[window_name] = {
                "name": window_name,
                "sample_count": _non_negative_int(sample_count, "sample_count"),
                "first_frame": _non_negative_int(first_frame, "first_frame"),
                "last_frame": _non_negative_int(last_frame, "last_frame"),
                "dropped_sample_count": _non_negative_int(
                    dropped_sample_count, "dropped_sample_count"
                ),
                "cpu_sections": _normalize_sections(cpu_sections),
                "gpu_sections": _normalize_sections(gpu_sections or {}),
                "counters": _normalize_counters(counters),
                "metadata": _stable_value(metadata or {}),
            }

    def record_inventory(
        self,
        *,
        services: Iterable[Any] = (),
        threads: Iterable[Any] = (),
        watchers: Iterable[Any] = (),
        python_modules: Iterable[Any] = (),
        native_modules: Iterable[Any] = (),
    ) -> None:
        with self._lock:
            self._inventory = {
                "services": _stable_records(services),
                "threads": _stable_records(threads),
                "watchers": _stable_records(watchers),
                "python_modules": _stable_records(python_modules),
                "native_modules": _stable_records(native_modules),
            }
            self._inventory_captured = True

    def add_diagnostic(
        self,
        code: str,
        message: str,
        *,
        severity: str = "info",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._diagnostics.append(
                {
                    "code": _clean_text(code, fallback="runtime.baseline"),
                    "message": _clean_text(message),
                    "severity": _clean_text(severity, fallback="info"),
                    "details": _stable_value(details or {}),
                }
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            workload_coverage = [
                {
                    "workload": workload,
                    "component_count": component_count,
                    "captured": (workload, component_count) in self._workloads,
                }
                for component_count in RUNTIME_BASELINE_COMPONENT_COUNTS
                for workload in RUNTIME_BASELINE_WORKLOADS
            ]
            scenario_coverage = [
                {
                    "name": scenario,
                    "group": _scenario_group(scenario),
                    "captured": scenario in self._performance_windows,
                }
                for scenario in RUNTIME_BASELINE_SCENARIOS
            ]
            document = {
                "$schema": RUNTIME_BASELINE_SCHEMA_ID,
                "schema_version": RUNTIME_BASELINE_SCHEMA_VERSION,
                "capture": copy.deepcopy(self._capture),
                "contract": {
                    "component_counts": list(RUNTIME_BASELINE_COMPONENT_COUNTS),
                    "lifecycle_callbacks": list(RUNTIME_BASELINE_LIFECYCLE_CALLBACKS),
                    "mutation_domains": list(RUNTIME_BASELINE_MUTATION_DOMAINS),
                    "workloads": list(RUNTIME_BASELINE_WORKLOADS),
                    "scenario_groups": {
                        name: list(scenarios)
                        for name, scenarios in RUNTIME_BASELINE_SCENARIO_GROUPS.items()
                    },
                    "timing_fields": list(RUNTIME_BASELINE_TIMING_FIELDS),
                    "counter_slots": list(RUNTIME_BASELINE_COUNTERS),
                    "timing_split": ["total", "user_script", "engine_overhead"],
                },
                "coverage": {
                    "lifecycle_events": bool(self._lifecycle_events),
                    "mutation_visibility": bool(self._mutations),
                    "workload_cases": workload_coverage,
                    "performance_scenarios": scenario_coverage,
                    "runtime_inventory": self._inventory_captured,
                },
                "semantics": {
                    "lifecycle_events": sorted(
                        copy.deepcopy(self._lifecycle_events),
                        key=lambda item: (item["frame"], item["sequence"]),
                    ),
                    "mutation_visibility": sorted(
                        copy.deepcopy(self._mutations),
                        key=lambda item: (item["frame"], item["sequence"]),
                    ),
                },
                "workloads": [
                    copy.deepcopy(self._workloads[key])
                    for key in sorted(
                        self._workloads, key=lambda item: (item[1], item[0])
                    )
                ],
                "runtime_inventory": copy.deepcopy(self._inventory),
                "performance_windows": [
                    copy.deepcopy(self._performance_windows[key])
                    for key in sorted(self._performance_windows)
                ],
                "diagnostics": sorted(
                    copy.deepcopy(self._diagnostics),
                    key=lambda item: (item["severity"], item["code"], item["message"]),
                ),
            }
        return _stable_value(document)

    def summary(self) -> dict[str, Any]:
        document = self.snapshot()
        return {
            "$schema": document["$schema"],
            "schema_version": document["schema_version"],
            "capture": document["capture"],
            "lifecycle_event_count": len(document["semantics"]["lifecycle_events"]),
            "mutation_observation_count": len(
                document["semantics"]["mutation_visibility"]
            ),
            "workload_count": len(document["workloads"]),
            "performance_window_count": len(document["performance_windows"]),
            "diagnostic_count": len(document["diagnostics"]),
            "coverage": document["coverage"],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.snapshot(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
            separators=(",", ":") if indent is None else None,
        )

    def write_json(self, path: str | os.PathLike[str]) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return str(target.resolve())


class RuntimeBaselineRunner:
    """Explicit execution adapter for the fixed R0 workload matrix."""

    def __init__(self, recorder: RuntimeBaselineRecorder) -> None:
        self.recorder = recorder

    @classmethod
    def for_engine(
        cls,
        engine: Any,
        *,
        suite_id: str = "runtime-r0-r6",
        project_id: str = "",
        build_configuration: str | None = None,
        build_preset: str | None = None,
        new_capture: bool = True,
    ) -> "RuntimeBaselineRunner":
        recorder = runtime_baseline_recorder(
            engine,
            suite_id=suite_id,
            project_id=project_id,
            build_configuration=build_configuration,
            build_preset=build_preset,
            replace=new_capture,
        )
        return cls(recorder)

    def run_workload_matrix(
        self,
        executor: Callable[[RuntimeWorkloadRequest], Mapping[str, Any] | float],
        *,
        workloads: Sequence[str] = RUNTIME_BASELINE_WORKLOADS,
        component_counts: Sequence[int] = RUNTIME_BASELINE_COMPONENT_COUNTS,
        repeats: int = 5,
    ) -> list[dict[str, Any]]:
        repeat_count = max(1, int(repeats))
        completed = []
        for component_count in component_counts:
            for workload in workloads:
                durations = []
                user_script_durations = []
                engine_overhead_durations = []
                counter_totals: dict[str, float] = {}
                metadata: dict[str, Any] = {}
                for repeat_index in range(repeat_count):
                    request = RuntimeWorkloadRequest(
                        workload=str(workload),
                        component_count=int(component_count),
                        repeat_index=repeat_index,
                    )
                    started = perf_counter_ns()
                    result = executor(request)
                    elapsed_ms = (perf_counter_ns() - started) / 1_000_000.0
                    if isinstance(result, Mapping):
                        elapsed_ms = _finite_number(
                            result.get("elapsed_ms", elapsed_ms), "elapsed_ms"
                        )
                        if result.get("user_script_ms") is not None:
                            user_script_durations.append(
                                _finite_number(
                                    result["user_script_ms"], "user_script_ms"
                                )
                            )
                        if result.get("engine_overhead_ms") is not None:
                            engine_overhead_durations.append(
                                _finite_number(
                                    result["engine_overhead_ms"],
                                    "engine_overhead_ms",
                                )
                            )
                        for key, value in (result.get("counters") or {}).items():
                            counter_totals[str(key)] = counter_totals.get(
                                str(key), 0.0
                            ) + _finite_number(value, f"counter {key}")
                        metadata.update(_stable_value(result.get("metadata") or {}))
                    elif result is not None:
                        elapsed_ms = _finite_number(result, "elapsed_ms")
                    durations.append(elapsed_ms)
                counters = {
                    key: value / repeat_count
                    for key, value in sorted(counter_totals.items())
                }
                self.recorder.record_workload(
                    workload=str(workload),
                    component_count=int(component_count),
                    samples_ms=durations,
                    user_script_samples_ms=(
                        user_script_durations
                        if len(user_script_durations) == repeat_count
                        else None
                    ),
                    engine_overhead_samples_ms=(
                        engine_overhead_durations
                        if len(engine_overhead_durations) == repeat_count
                        else None
                    ),
                    counters=counters,
                    metadata={"repeats": repeat_count, **metadata},
                )
                completed.append(
                    {
                        "workload": str(workload),
                        "component_count": int(component_count),
                    }
                )
        return completed

    def run_scenario_matrix(
        self,
        executor: Callable[[RuntimeScenarioRequest], Mapping[str, Any]],
        *,
        scenarios: Sequence[str] = RUNTIME_BASELINE_SCENARIOS,
        repeats: int = 5,
    ) -> list[dict[str, Any]]:
        """Execute the fixed UI/preview/scene matrix on explicit request.

        The executor returns one sample with optional ``cpu_ms`` and ``gpu_ms``
        section mappings, fixed counter values, and metadata. This method is
        never scheduled by the Engine and therefore adds no normal-frame work.
        """

        repeat_count = max(1, int(repeats))
        completed: list[dict[str, Any]] = []
        for scenario_value in scenarios:
            scenario = _clean_text(scenario_value)
            group = _scenario_group(scenario)
            cpu_samples: dict[str, list[float]] = {}
            gpu_samples: dict[str, list[float]] = {}
            counter_totals: dict[str, float] = {}
            metadata: dict[str, Any] = {}
            for repeat_index in range(repeat_count):
                result = executor(
                    RuntimeScenarioRequest(
                        scenario=scenario,
                        group=group,
                        repeat_index=repeat_index,
                    )
                )
                if not isinstance(result, Mapping):
                    raise TypeError("runtime scenario executor must return a mapping")
                _append_section_samples(
                    cpu_samples,
                    result.get("cpu_ms"),
                    field_name="cpu_ms",
                )
                _append_section_samples(
                    gpu_samples,
                    result.get("gpu_ms"),
                    field_name="gpu_ms",
                )
                for key, value in (result.get("counters") or {}).items():
                    counter_totals[str(key)] = counter_totals.get(str(key), 0.0) + (
                        _finite_number(value, f"counter {key}")
                    )
                metadata.update(_stable_value(result.get("metadata") or {}))
            cpu_sections = {
                section: summarize_samples(samples)
                for section, samples in sorted(cpu_samples.items())
            }
            gpu_sections = {
                section: summarize_samples(samples)
                for section, samples in sorted(gpu_samples.items())
            }
            counters = {
                key: value / repeat_count
                for key, value in sorted(counter_totals.items())
            }
            self.recorder.record_performance_window(
                name=scenario,
                sample_count=repeat_count,
                first_frame=0,
                last_frame=0,
                cpu_sections=cpu_sections,
                gpu_sections=gpu_sections,
                counters=counters,
                metadata={"group": group, "repeats": repeat_count, **metadata},
            )
            completed.append({"name": scenario, "group": group})
        return completed

    def capture_native_performance_window(
        self,
        native_engine: Any,
        wait_frames: Callable[[int], Any],
        *,
        name: str,
        frames: int = 240,
        gpu_sections: Mapping[str, Mapping[str, Any]] | None = None,
        counters: Mapping[str, int | float] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        frame_count = max(1, int(frames))
        start_frame = int(native_engine.begin_renderer_performance_window())
        wait_frames(frame_count)
        window = dict(native_engine.get_renderer_performance_window())
        self.recorder.record_performance_window(
            name=name,
            sample_count=int(window.get("sample_count", 0) or 0),
            first_frame=int(window.get("first_frame", start_frame) or 0),
            last_frame=int(window.get("last_frame", 0) or 0),
            dropped_sample_count=int(window.get("dropped_sample_count", 0) or 0),
            cpu_sections=window.get("timings") or {},
            gpu_sections=gpu_sections or {},
            counters=counters,
            metadata={"requested_frames": frame_count, **(metadata or {})},
        )
        return window

    def capture_runtime_inventory(
        self,
        engine: Any = None,
        *,
        services: Iterable[Any] | None = None,
        watchers: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        inventory = collect_runtime_inventory(
            engine,
            services=services,
            watchers=watchers,
        )
        self.recorder.record_inventory(**inventory)
        return inventory

    def diagnostics(self, engine: Any = None) -> dict[str, Any]:
        return runtime_baseline_diagnostics(engine, recorder=self.recorder)

    @staticmethod
    def compare(
        baseline: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        regression_threshold_percent: float = 2.0,
    ) -> dict[str, Any]:
        return compare_runtime_baselines(
            baseline,
            candidate,
            regression_threshold_percent=regression_threshold_percent,
        )


def runtime_baseline_recorder(
    engine: Any,
    *,
    suite_id: str = "runtime-r0-r6",
    project_id: str = "",
    build_configuration: str | None = None,
    build_preset: str | None = None,
    replace: bool = False,
) -> RuntimeBaselineRecorder:
    if engine is None:
        raise RuntimeError("runtime baseline recording requires a live Engine")
    with _ENGINE_RECORDERS_LOCK:
        recorder = _ENGINE_RECORDERS.get(engine)
        if recorder is None or replace:
            recorder = RuntimeBaselineRecorder(
                detect_runtime_identity(
                    engine,
                    build_configuration=build_configuration,
                    build_preset=build_preset,
                ),
                suite_id=suite_id,
                project_id=project_id or _active_project_id(),
            )
            _ENGINE_RECORDERS[engine] = recorder
        return recorder


def collect_runtime_inventory(
    engine: Any = None,
    *,
    services: Iterable[Any] | None = None,
    watchers: Iterable[Any] | None = None,
) -> dict[str, list[Any]]:
    service_records = list(services or _engine_service_records(engine))
    watcher_records = list(watchers or _engine_watcher_records(engine))
    thread_records = [
        {
            "name": thread.name,
            "daemon": bool(thread.daemon),
            "alive": bool(thread.is_alive()),
        }
        for thread in threading.enumerate()
    ]
    python_modules = []
    native_modules = []
    extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    for name, module in tuple(sys.modules.items()):
        if module is None:
            continue
        path = _clean_text(getattr(module, "__file__", ""))
        record = {"name": name, "path": path}
        if path and path.endswith(extension_suffixes):
            native_modules.append(record)
        else:
            python_modules.append(record)
    return {
        "services": _stable_records(service_records),
        "threads": _stable_records(thread_records),
        "watchers": _stable_records(watcher_records),
        "python_modules": _stable_records(python_modules),
        "native_modules": _stable_records(native_modules),
    }


def runtime_baseline_diagnostics(
    engine: Any,
    *,
    recorder: RuntimeBaselineRecorder | None = None,
) -> dict[str, Any]:
    identity = detect_runtime_identity(engine)
    recorder = recorder or runtime_baseline_recorder(engine)
    scheduler = getattr(engine, "_runtime_scheduler", None)
    scheduler_profile = _safe_mapping_call(scheduler, "profiler_snapshot")
    journal = getattr(scheduler, "change_journal", None)
    journal_profile = _safe_mapping_call(journal, "profiler_snapshot")
    scene_profile: dict[str, Any] = {}
    try:
        from Infernux.lib import SceneManager

        manager = SceneManager.instance()
        if manager is not None:
            scene_profile = dict(manager.get_last_frame_profile())
    except (ImportError, AttributeError, RuntimeError):
        pass
    native_frame: dict[str, Any] = {}
    native = engine.get_native_engine() if engine is not None else None
    if native is not None:
        try:
            native_frame = dict(native.renderer_frame_snapshot)
        except (AttributeError, RuntimeError):
            pass
    inventory = collect_runtime_inventory(engine)
    live_counter_slots = _collect_live_counter_slots(
        scheduler_profile,
        journal_profile,
        scene_profile,
        native_frame,
    )
    return {
        "$schema": RUNTIME_BASELINE_SCHEMA_ID,
        "schema_version": RUNTIME_BASELINE_SCHEMA_VERSION,
        "identity": identity.to_dict(),
        "capture": recorder.summary(),
        "capabilities": {
            "component_counts": list(RUNTIME_BASELINE_COMPONENT_COUNTS),
            "workloads": list(RUNTIME_BASELINE_WORKLOADS),
            "scenario_groups": {
                name: list(scenarios)
                for name, scenarios in RUNTIME_BASELINE_SCENARIO_GROUPS.items()
            },
            "timing_fields": list(RUNTIME_BASELINE_TIMING_FIELDS),
            "counters": list(RUNTIME_BASELINE_COUNTERS),
            "native_performance_window": bool(
                native is not None
                and callable(getattr(native, "begin_renderer_performance_window", None))
                and callable(getattr(native, "get_renderer_performance_window", None))
            ),
            "read_only": True,
        },
        "live_sources": {
            "scheduler": scheduler_profile,
            "change_journal": journal_profile,
            "scene_frame": _stable_value(scene_profile),
            "renderer_frame": _stable_value(native_frame),
            "counter_slots": live_counter_slots,
            "inventory_counts": {key: len(value) for key, value in inventory.items()},
        },
    }


def compare_runtime_baselines(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    regression_threshold_percent: float = 2.0,
) -> dict[str, Any]:
    threshold = abs(_finite_number(regression_threshold_percent, "threshold"))
    _validate_document_header(baseline)
    _validate_document_header(candidate)
    baseline_identity = dict((baseline.get("capture") or {}).get("identity") or {})
    candidate_identity = dict((candidate.get("capture") or {}).get("identity") or {})
    mismatches = [
        {
            "field": field,
            "baseline": baseline_identity.get(field),
            "candidate": candidate_identity.get(field),
        }
        for field in _IDENTITY_FIELDS
        if baseline_identity.get(field) != candidate_identity.get(field)
    ]
    baseline_metrics = _comparison_metrics(baseline)
    candidate_metrics = _comparison_metrics(candidate)
    comparisons = []
    for path in sorted(set(baseline_metrics) | set(candidate_metrics)):
        before = baseline_metrics.get(path)
        after = candidate_metrics.get(path)
        if before is None or after is None:
            comparisons.append(
                {
                    "path": path,
                    "baseline": before,
                    "candidate": after,
                    "delta": None,
                    "percent": None,
                    "direction": _metric_direction(path),
                    "verdict": "added" if before is None else "removed",
                }
            )
            continue
        delta = after - before
        percent = (
            None
            if before == 0.0 and delta != 0.0
            else (0.0 if before == 0.0 else delta / abs(before) * 100.0)
        )
        direction = _metric_direction(path)
        verdict = "stable"
        beyond_threshold = percent is None or abs(percent) > threshold
        if direction != "neutral" and beyond_threshold:
            better = delta > 0.0 if direction == "higher" else delta < 0.0
            verdict = "improved" if better else "regressed"
        comparisons.append(
            {
                "path": path,
                "baseline": before,
                "candidate": after,
                "delta": delta,
                "percent": percent,
                "direction": direction,
                "verdict": verdict,
            }
        )
    counts = {
        verdict: sum(item["verdict"] == verdict for item in comparisons)
        for verdict in ("improved", "regressed", "stable", "added", "removed")
    }
    return _stable_value(
        {
            "$schema": RUNTIME_BASELINE_REPORT_SCHEMA_ID,
            "schema_version": RUNTIME_BASELINE_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "threshold_percent": threshold,
            "comparable": not mismatches,
            "baseline_identity": baseline_identity,
            "candidate_identity": candidate_identity,
            "identity_mismatches": mismatches,
            "summary": counts,
            "metrics": comparisons,
        }
    )


def load_runtime_baseline(path: str | os.PathLike[str]) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_document_header(document)
    return document


def write_runtime_baseline_comparison(
    baseline_path: str | os.PathLike[str],
    candidate_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    regression_threshold_percent: float = 2.0,
) -> str:
    report = compare_runtime_baselines(
        load_runtime_baseline(baseline_path),
        load_runtime_baseline(candidate_path),
        regression_threshold_percent=regression_threshold_percent,
    )
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(target.resolve())


def _normalize_timing(timing: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {name: None for name in RUNTIME_BASELINE_TIMING_FIELDS}
    for key, value in timing.items():
        key_text = str(key)
        if value is None:
            normalized[key_text] = None
            continue
        if key_text == "sample_count":
            normalized[key_text] = _non_negative_int(value, key_text)
        else:
            normalized[key_text] = _finite_number(value, key_text)
    return dict(sorted(normalized.items()))


def _normalize_workload_timing(timing: Mapping[str, Any]) -> dict[str, Any]:
    if "total" not in timing:
        return {
            "total": _normalize_timing(timing),
            "user_script": None,
            "engine_overhead": None,
        }
    total = timing.get("total")
    if not isinstance(total, Mapping):
        raise ValueError("workload timing.total must be a timing mapping")
    normalized = {"total": _normalize_timing(total)}
    for name in ("user_script", "engine_overhead"):
        value = timing.get(name)
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"workload timing.{name} must be a timing mapping")
        normalized[name] = None if value is None else _normalize_timing(value)
    return normalized


def _normalize_sections(
    sections: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(name): _normalize_timing(stats)
        for name, stats in sorted(sections.items(), key=lambda pair: str(pair[0]))
    }


def _normalize_counters(
    counters: Mapping[str, int | float] | None,
) -> dict[str, int | float | None]:
    source = counters or {}
    normalized: dict[str, int | float | None] = {}
    for name in RUNTIME_BASELINE_COUNTERS:
        value = source.get(name)
        normalized[name] = None if value is None else _finite_number(value, name)
    for name, value in source.items():
        key = str(name)
        if key not in normalized:
            normalized[key] = _finite_number(value, key)
    return dict(sorted(normalized.items()))


def _scenario_group(scenario: str) -> str:
    for group, names in RUNTIME_BASELINE_SCENARIO_GROUPS.items():
        if scenario in names:
            return group
    raise ValueError(f"unknown runtime baseline performance scenario: {scenario}")


def _append_section_samples(
    target: dict[str, list[float]],
    values: Any,
    *,
    field_name: str,
) -> None:
    if values is None:
        return
    if not isinstance(values, Mapping):
        raise TypeError(f"runtime scenario {field_name} must be a mapping")
    for section, value in values.items():
        target.setdefault(str(section), []).append(
            _finite_number(value, f"{field_name}.{section}")
        )


def _stable_records(values: Iterable[Any]) -> list[Any]:
    records = [_stable_value(value) for value in values]
    return sorted(
        records,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _engine_service_records(engine: Any) -> list[dict[str, Any]]:
    if engine is None:
        return []
    service_fields = {
        "engine": "_engine",
        "runtime_execution_scheduler": "_runtime_scheduler",
        "play_mode_manager": "_play_mode_manager",
        "player_runtime_session": "_player_runtime",
        "resources_manager": "_resources_manager",
        "render_pipeline": "_render_pipeline",
    }
    records = []
    for service_id, field in service_fields.items():
        value = getattr(engine, field, None)
        records.append(
            {
                "id": service_id,
                "active": value is not None,
                "type": "" if value is None else type(value).__name__,
            }
        )
    try:
        from Infernux.mcp.server import is_running

        records.append(
            {"id": "mcp_server", "active": bool(is_running()), "type": "MCP"}
        )
    except ImportError:
        pass
    return records


def _active_project_id() -> str:
    try:
        from Infernux.engine.project_context import get_project_root

        root = _clean_text(get_project_root())
    except (ImportError, AttributeError, RuntimeError):
        root = ""
    return Path(root).name if root else ""


def _engine_watcher_records(engine: Any) -> list[dict[str, Any]]:
    manager = (
        getattr(engine, "_resources_manager", None) if engine is not None else None
    )
    if manager is None:
        return []
    observer = getattr(manager, "_observer", None)
    thread = getattr(manager, "_thread", None)
    return [
        {
            "id": "asset_resources",
            "active": bool(
                thread is not None and getattr(thread, "is_alive", lambda: False)()
            ),
            "observer_active": bool(
                observer is not None and getattr(observer, "is_alive", lambda: False)()
            ),
        }
    ]


def _safe_mapping_call(owner: Any, method_name: str) -> dict[str, Any]:
    method = getattr(owner, method_name, None)
    if not callable(method):
        return {}
    try:
        return _stable_value(dict(method()))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return {}


def _collect_live_counter_slots(*sources: Mapping[str, Any]) -> dict[str, float | None]:
    slots: dict[str, float | None] = {name: None for name in RUNTIME_BASELINE_COUNTERS}

    def visit(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for key, item in value.items():
            name = str(key)
            if name in slots and isinstance(item, (int, float)):
                slots[name] = _finite_number(item, name)
            elif isinstance(item, Mapping):
                visit(item)

    for source in sources:
        visit(source)
    return slots


def _validate_document_header(document: Mapping[str, Any]) -> None:
    if document.get("$schema") != RUNTIME_BASELINE_SCHEMA_ID:
        raise ValueError("runtime baseline document uses an unsupported schema")
    if int(document.get("schema_version", 0) or 0) != RUNTIME_BASELINE_SCHEMA_VERSION:
        raise ValueError("runtime baseline document uses an unsupported version")
    if not isinstance(document.get("capture"), Mapping):
        raise ValueError("runtime baseline document is missing capture identity")


def _comparison_metrics(document: Mapping[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for workload in document.get("workloads") or []:
        prefix = (
            f"workloads.{workload.get('workload')}[{workload.get('component_count')}]"
        )
        for timing_name, timing in (workload.get("timing") or {}).items():
            if not isinstance(timing, Mapping):
                continue
            for name, value in timing.items():
                if name == "sample_count" or not isinstance(value, (int, float)):
                    continue
                metrics[f"{prefix}.timing.{timing_name}.{name}"] = float(value)
        for name, value in (workload.get("counters") or {}).items():
            if isinstance(value, (int, float)):
                metrics[f"{prefix}.counters.{name}"] = float(value)
    for window in document.get("performance_windows") or []:
        prefix = f"performance.{window.get('name')}"
        for domain in ("cpu_sections", "gpu_sections"):
            for section, stats in (window.get(domain) or {}).items():
                for name, value in stats.items():
                    if name == "sample_count" or not isinstance(value, (int, float)):
                        continue
                    metrics[f"{prefix}.{domain}.{section}.{name}"] = float(value)
        for name, value in (window.get("counters") or {}).items():
            if isinstance(value, (int, float)):
                metrics[f"{prefix}.counters.{name}"] = float(value)
    return metrics


def _metric_direction(path: str) -> str:
    name = path.rsplit(".", 1)[-1].casefold()
    if name.endswith(_HIGHER_IS_BETTER_SUFFIXES):
        return "higher"
    if name.endswith(_LOWER_IS_BETTER_SUFFIXES):
        return "lower"
    return "neutral"


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Infernux runtime baseline tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("baseline")
    compare_parser.add_argument("candidate")
    compare_parser.add_argument("--output", required=True)
    compare_parser.add_argument("--threshold", type=float, default=2.0)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("document")
    args = parser.parse_args(argv)
    if args.command == "validate":
        load_runtime_baseline(args.document)
        return 0
    write_runtime_baseline_comparison(
        args.baseline,
        args.candidate,
        args.output,
        regression_threshold_percent=args.threshold,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "RUNTIME_BASELINE_COMPONENT_COUNTS",
    "RUNTIME_BASELINE_COUNTERS",
    "RUNTIME_BASELINE_LIFECYCLE_CALLBACKS",
    "RUNTIME_BASELINE_MUTATION_DOMAINS",
    "RUNTIME_BASELINE_REPORT_SCHEMA_ID",
    "RUNTIME_BASELINE_SCENARIOS",
    "RUNTIME_BASELINE_SCENARIO_GROUPS",
    "RUNTIME_BASELINE_SCHEMA_ID",
    "RUNTIME_BASELINE_SCHEMA_VERSION",
    "RUNTIME_BASELINE_TIMING_FIELDS",
    "RUNTIME_BASELINE_WORKLOADS",
    "RuntimeBaselineIdentity",
    "RuntimeBaselineRecorder",
    "RuntimeBaselineRunner",
    "RuntimeScenarioRequest",
    "RuntimeWorkloadRequest",
    "collect_runtime_inventory",
    "compare_runtime_baselines",
    "detect_runtime_identity",
    "load_runtime_baseline",
    "runtime_baseline_diagnostics",
    "runtime_baseline_recorder",
    "summarize_samples",
    "write_runtime_baseline_comparison",
]
