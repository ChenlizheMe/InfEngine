from __future__ import annotations

import json
import importlib.machinery
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from Infernux.engine.runtime_baseline import (
    RUNTIME_BASELINE_COMPONENT_COUNTS,
    RUNTIME_BASELINE_COUNTERS,
    RUNTIME_BASELINE_LIFECYCLE_CALLBACKS,
    RUNTIME_BASELINE_MUTATION_DOMAINS,
    RUNTIME_BASELINE_SCENARIOS,
    RUNTIME_BASELINE_SCENARIO_GROUPS,
    RUNTIME_BASELINE_SCHEMA_ID,
    RUNTIME_BASELINE_TIMING_FIELDS,
    RUNTIME_BASELINE_WORKLOADS,
    RuntimeBaselineIdentity,
    RuntimeBaselineRecorder,
    RuntimeBaselineRunner,
    collect_runtime_inventory,
    compare_runtime_baselines,
    detect_runtime_identity,
    load_runtime_baseline,
    runtime_baseline_diagnostics,
    summarize_samples,
    write_runtime_baseline_comparison,
)


def _identity(*, role: str = "Editor", build: str = "Debug") -> RuntimeBaselineIdentity:
    return RuntimeBaselineIdentity(
        build_configuration=build,
        application_role=role,
        runtime_mode="Graphical",
        flavor=f"{role}{build}",
        platform="Windows",
        architecture="AMD64",
        python_version="3.12.0",
        native_profile_enabled=build == "Debug",
        engine_version="0.2.9",
        build_preset=build.casefold(),
    )


def _recorder(*, role: str = "Editor", build: str = "Debug") -> RuntimeBaselineRecorder:
    return RuntimeBaselineRecorder(
        _identity(role=role, build=build),
        suite_id="unit",
        project_id="runtime-test",
        capture_id="capture-fixed",
        created_utc="2026-08-10T00:00:00.000Z",
    )


def test_sample_summary_has_stable_percentiles():
    summary = summarize_samples([5.0, 1.0, 4.0, 2.0, 3.0])

    assert summary == {
        "sample_count": 5,
        "avg_ms": 3.0,
        "p50_ms": 3.0,
        "p95_ms": pytest.approx(4.8),
        "p99_ms": pytest.approx(4.96),
        "min_ms": 1.0,
        "max_ms": 5.0,
    }


def test_identity_keeps_debug_release_and_editor_player_axes_separate():
    identity = detect_runtime_identity(
        build_configuration="RelWithDebInfo",
        application_role="player",
        runtime_mode="Graphical",
        build_preset="debug-no-vulkan-validation",
    )

    assert identity.build_configuration == "Debug"
    assert identity.application_role == "Player"
    assert identity.flavor == "PlayerDebug"
    assert identity.build_preset == "debug-no-vulkan-validation"

    flavors = {
        detect_runtime_identity(
            build_configuration=build,
            application_role=role,
            runtime_mode="Graphical",
        ).flavor
        for build in ("Debug", "Release")
        for role in ("Editor", "Player")
    }
    assert flavors == {
        "EditorDebug",
        "EditorRelease",
        "PlayerDebug",
        "PlayerRelease",
    }


def test_recorder_preserves_semantic_order_and_serializes_deterministically(tmp_path):
    recorder = _recorder()
    recorder.record_lifecycle_event(
        frame=4,
        phase="update",
        callback="update",
        component_id=8,
        type_id="Mover",
    )
    recorder.record_lifecycle_event(
        frame=4,
        phase="late_update",
        callback="late_update",
        component_id=8,
        type_id="Mover",
    )
    recorder.record_mutation_visibility(
        frame=4,
        domain="TransformWorld",
        mutation_phase="update",
        observed_phase="late_update",
        visible_same_frame=True,
        stable_id=8,
    )
    recorder.record_inventory(
        services=[{"id": "z"}, {"id": "a"}],
        threads=[{"name": "worker"}],
    )

    first = recorder.to_json()
    second = recorder.to_json()
    target = tmp_path / "baseline.json"
    recorder.write_json(target)
    loaded = load_runtime_baseline(target)

    assert first == second
    assert loaded["$schema"] == RUNTIME_BASELINE_SCHEMA_ID
    events = loaded["semantics"]["lifecycle_events"]
    assert [event["callback"] for event in events] == ["update", "late_update"]
    assert loaded["semantics"]["mutation_visibility"][0]["visible_same_frame"] is True
    assert [item["id"] for item in loaded["runtime_inventory"]["services"]] == [
        "a",
        "z",
    ]


def test_runner_executes_complete_fixed_workload_matrix():
    recorder = _recorder()
    runner = RuntimeBaselineRunner(recorder)
    seen = []

    def execute(request):
        seen.append((request.workload, request.component_count, request.repeat_index))
        return {
            "elapsed_ms": request.component_count / 1000.0 + request.repeat_index,
            "user_script_ms": request.component_count / 2000.0,
            "engine_overhead_ms": 0.25,
            "counters": {
                "python_calls": request.component_count,
                "engine_api_crossings": 1,
            },
            "metadata": {"adapter": "unit"},
        }

    completed = runner.run_workload_matrix(execute, repeats=2)
    document = recorder.snapshot()

    expected_cases = len(RUNTIME_BASELINE_COMPONENT_COUNTS) * len(
        RUNTIME_BASELINE_WORKLOADS
    )
    assert len(completed) == expected_cases
    assert len(document["workloads"]) == expected_cases
    assert len(seen) == expected_cases * 2
    assert {
        (item["workload"], item["component_count"]) for item in document["workloads"]
    } == {
        (workload, count)
        for count in RUNTIME_BASELINE_COMPONENT_COUNTS
        for workload in RUNTIME_BASELINE_WORKLOADS
    }
    assert all(
        item["timing"]["user_script"] is not None for item in document["workloads"]
    )
    assert all(
        item["timing"]["engine_overhead"] is not None for item in document["workloads"]
    )
    assert all(
        set(item["counters"]) >= set(RUNTIME_BASELINE_COUNTERS)
        for item in document["workloads"]
    )


def test_empty_document_exposes_the_complete_contract_without_collecting(monkeypatch):
    import Infernux.engine.runtime_baseline as baseline_module

    monkeypatch.setattr(
        baseline_module,
        "collect_runtime_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a recorder must not collect inventory implicitly")
        ),
    )

    document = _recorder().snapshot()

    assert document["contract"]["component_counts"] == list(
        RUNTIME_BASELINE_COMPONENT_COUNTS
    )
    assert document["contract"]["workloads"] == list(RUNTIME_BASELINE_WORKLOADS)
    assert document["contract"]["lifecycle_callbacks"] == list(
        RUNTIME_BASELINE_LIFECYCLE_CALLBACKS
    )
    assert document["contract"]["mutation_domains"] == list(
        RUNTIME_BASELINE_MUTATION_DOMAINS
    )
    assert document["contract"]["scenario_groups"] == {
        name: list(scenarios)
        for name, scenarios in RUNTIME_BASELINE_SCENARIO_GROUPS.items()
    }
    assert document["contract"]["timing_fields"] == list(RUNTIME_BASELINE_TIMING_FIELDS)
    assert len(document["coverage"]["workload_cases"]) == (
        len(RUNTIME_BASELINE_COMPONENT_COUNTS) * len(RUNTIME_BASELINE_WORKLOADS)
    )
    assert len(document["coverage"]["performance_scenarios"]) == len(
        RUNTIME_BASELINE_SCENARIOS
    )
    assert not any(item["captured"] for item in document["coverage"]["workload_cases"])
    assert document["coverage"]["runtime_inventory"] is False


def test_explicit_inventory_separates_python_and_native_modules(monkeypatch):
    python_name = "_infernux_runtime_baseline_python_probe"
    native_name = "_infernux_runtime_baseline_native_probe"
    native_suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    monkeypatch.setitem(sys.modules, python_name, SimpleNamespace(__file__="probe.py"))
    monkeypatch.setitem(
        sys.modules,
        native_name,
        SimpleNamespace(__file__=f"probe{native_suffix}"),
    )

    inventory = collect_runtime_inventory(
        services=[{"id": "runtime"}],
        watchers=[{"id": "assets"}],
    )
    recorder = _recorder()
    recorder.record_inventory(**inventory)
    document = recorder.snapshot()

    assert any(item["name"] == python_name for item in inventory["python_modules"])
    assert any(item["name"] == native_name for item in inventory["native_modules"])
    assert document["coverage"]["runtime_inventory"] is True
    assert document["runtime_inventory"]["services"] == [{"id": "runtime"}]
    assert document["runtime_inventory"]["watchers"] == [{"id": "assets"}]


def test_runner_executes_fixed_ui_preview_scene_scenario_matrix():
    recorder = _recorder(build="Release")
    runner = RuntimeBaselineRunner(recorder)
    seen = []

    def execute(request):
        seen.append((request.group, request.scenario, request.repeat_index))
        return {
            "cpu_ms": {
                "frame": 1.0 + request.repeat_index,
                "engine_overhead": 0.75,
                "user_script": 0.25,
            },
            "gpu_ms": {"frame": 0.5},
            "counters": {"python_calls": request.repeat_index + 1},
            "metadata": {"source": "unit"},
        }

    completed = runner.run_scenario_matrix(execute, repeats=3)
    document = recorder.snapshot()

    assert len(completed) == len(RUNTIME_BASELINE_SCENARIOS)
    assert len(seen) == len(RUNTIME_BASELINE_SCENARIOS) * 3
    assert {item["name"] for item in completed} == set(RUNTIME_BASELINE_SCENARIOS)
    assert all(
        item["captured"] for item in document["coverage"]["performance_scenarios"]
    )
    first = document["performance_windows"][0]
    assert first["cpu_sections"]["frame"]["avg_ms"] == pytest.approx(2.0)
    assert first["cpu_sections"]["frame"]["p95_ms"] == pytest.approx(2.9)
    assert first["cpu_sections"]["frame"]["p99_ms"] == pytest.approx(2.98)
    assert first["counters"]["python_calls"] == pytest.approx(2.0)
    assert first["counters"]["gil_acquires"] is None


def test_scenario_matrix_rejects_unknown_scenarios_before_execution():
    runner = RuntimeBaselineRunner(_recorder())
    called = False

    def execute(_request):
        nonlocal called
        called = True
        return {}

    with pytest.raises(
        ValueError, match="unknown runtime baseline performance scenario"
    ):
        runner.run_scenario_matrix(execute, scenarios=["unknown"])
    assert called is False


def test_native_window_capture_is_explicit_and_keeps_cpu_gpu_sections():
    class _Native:
        begin_calls = 0
        read_calls = 0

        def begin_renderer_performance_window(self):
            self.begin_calls += 1
            return 100

        def get_renderer_performance_window(self):
            self.read_calls += 1
            return {
                "first_frame": 100,
                "last_frame": 339,
                "sample_count": 240,
                "dropped_sample_count": 0,
                "timings": {
                    "frame": {
                        "sample_count": 240,
                        "avg_ms": 0.5,
                        "p95_ms": 0.7,
                        "p99_ms": 0.8,
                    }
                },
            }

    native = _Native()
    waited = []
    recorder = _recorder(build="Release")
    runner = RuntimeBaselineRunner(recorder)

    runner.capture_native_performance_window(
        native,
        waited.append,
        name="empty-shadow-on",
        frames=240,
        gpu_sections={
            "graphics": {
                "sample_count": 240,
                "avg_ms": 0.2,
                "p95_ms": 0.3,
                "p99_ms": 0.35,
            }
        },
    )

    window = recorder.snapshot()["performance_windows"][0]
    assert waited == [240]
    assert native.begin_calls == 1
    assert native.read_calls == 1
    assert window["cpu_sections"]["frame"]["p99_ms"] == pytest.approx(0.8)
    assert window["gpu_sections"]["graphics"]["p95_ms"] == pytest.approx(0.3)


def test_live_diagnostics_reads_existing_sources_without_starting_a_window():
    class _ProfileOwner:
        @staticmethod
        def profiler_snapshot():
            return {"plan_builds": 2}

    class _Scheduler(_ProfileOwner):
        change_journal = _ProfileOwner()

    class _Native:
        renderer_frame_snapshot = {"frame": 12, "game_render_ms": 0.2}

        @staticmethod
        def begin_renderer_performance_window():
            raise AssertionError("read-only diagnostics must not begin a capture")

        @staticmethod
        def get_renderer_performance_window():
            return {}

    class _Engine:
        _application_role = "editor"
        _mode = "Graphical"
        _runtime_scheduler = _Scheduler()
        _engine = object()
        _play_mode_manager = object()
        _player_runtime = None
        _resources_manager = None
        _render_pipeline = object()

        @staticmethod
        def get_native_engine():
            return _Native()

    recorder = _recorder()
    state = runtime_baseline_diagnostics(_Engine(), recorder=recorder)

    assert state["capabilities"]["read_only"] is True
    assert state["capabilities"]["native_performance_window"] is True
    assert state["live_sources"]["scheduler"]["plan_builds"] == 2
    assert state["live_sources"]["renderer_frame"]["frame"] == 12
    assert set(state["live_sources"]["counter_slots"]) == set(RUNTIME_BASELINE_COUNTERS)
    assert state["live_sources"]["counter_slots"]["gil_acquires"] is None


def test_comparison_reports_regression_and_rejects_flavor_mismatch(tmp_path):
    baseline = _recorder(build="Release")
    candidate = _recorder(build="Release")
    baseline.record_workload(
        workload="no_op",
        component_count=1_000,
        timing={"sample_count": 5, "avg_ms": 1.0, "p95_ms": 1.0, "p99_ms": 1.0},
    )
    candidate.record_workload(
        workload="no_op",
        component_count=1_000,
        timing={"sample_count": 5, "avg_ms": 1.1, "p95_ms": 1.1, "p99_ms": 1.1},
    )

    report = compare_runtime_baselines(baseline.snapshot(), candidate.snapshot())

    assert report["comparable"] is True
    assert report["summary"]["regressed"] == 3
    assert all(
        item["direction"] == "lower"
        for item in report["metrics"]
        if item["path"].endswith("_ms")
    )

    player = _recorder(role="Player", build="Release")
    mismatch = compare_runtime_baselines(baseline.snapshot(), player.snapshot())
    assert mismatch["comparable"] is False
    assert {item["field"] for item in mismatch["identity_mismatches"]} == {
        "application_role",
        "flavor",
    }

    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "comparison.json"
    baseline.write_json(baseline_path)
    candidate.write_json(candidate_path)
    write_runtime_baseline_comparison(baseline_path, candidate_path, output_path)
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted["summary"]["regressed"] == 3


def test_comparison_treats_zero_to_nonzero_cost_as_a_regression():
    baseline = _recorder(build="Release")
    candidate = _recorder(build="Release")
    baseline.record_workload(
        workload="no_op",
        component_count=0,
        timing={"sample_count": 1, "avg_ms": 0.0},
    )
    candidate.record_workload(
        workload="no_op",
        component_count=0,
        timing={"sample_count": 1, "avg_ms": 0.1},
    )

    report = compare_runtime_baselines(baseline.snapshot(), candidate.snapshot())

    metric = next(item for item in report["metrics"] if item["path"].endswith("avg_ms"))
    assert metric["percent"] is None
    assert metric["verdict"] == "regressed"


def test_recorder_rejects_unknown_matrix_cases_and_non_finite_values():
    recorder = _recorder()

    with pytest.raises(ValueError, match="unknown runtime baseline workload"):
        recorder.record_workload(
            workload="mystery",
            component_count=1,
            samples_ms=[1.0],
        )
    with pytest.raises(
        ValueError, match="unsupported runtime baseline component count"
    ):
        recorder.record_workload(
            workload="no_op",
            component_count=2,
            samples_ms=[1.0],
        )
    with pytest.raises(ValueError, match="finite"):
        recorder.record_workload(
            workload="no_op",
            component_count=1,
            samples_ms=[float("nan")],
        )


def test_distributed_json_schemas_validate_generated_documents():
    jsonschema = pytest.importorskip("jsonschema")
    schema_root = Path(__file__).parents[1] / "Infernux" / "resources" / "schemas"
    baseline = _recorder(build="Release").snapshot()
    comparison = compare_runtime_baselines(
        baseline, _recorder(build="Release").snapshot()
    )

    jsonschema.validate(
        baseline,
        json.loads(
            (schema_root / "runtime-baseline-v1.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    jsonschema.validate(
        comparison,
        json.loads(
            (schema_root / "runtime-baseline-comparison-v1.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
