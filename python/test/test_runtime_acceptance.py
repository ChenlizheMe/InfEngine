from __future__ import annotations

import json
from pathlib import Path

import pytest

from Infernux.acceptance import RuntimeAcceptance, RuntimeAcceptanceManifest
from Infernux.application import Application


def _write_manifest(path: Path, tests=None, *, cycles: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_acceptance",
                "name": "Rendering and VFX",
                "cycles": cycles,
                "tests": tests
                if tests is not None
                else [
                    {
                        "id": "render.forward",
                        "scene": "Assets/Acceptance/Forward.scene",
                        "run_seconds": 2.0,
                        "expected": {"draws": 1},
                    },
                    {
                        "id": "particle.sprite",
                        "scene": "Assets/Acceptance/Sprite.scene",
                        "run_seconds": 3.0,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _reset_runtime_acceptance():
    RuntimeAcceptance.reset()
    yield
    RuntimeAcceptance.reset()


def test_manifest_is_strict_and_preserves_domain_document(tmp_path):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "All.json"
    _write_manifest(manifest_path)

    manifest = RuntimeAcceptanceManifest.load(str(manifest_path), project_root=str(tmp_path))

    assert manifest.name == "Rendering and VFX"
    assert manifest.cycles == 1
    assert [test.id for test in manifest.tests] == ["render.forward", "particle.sprite"]
    assert manifest.tests[0].document["expected"] == {"draws": 1}
    assert manifest.tests[0].timeout_seconds == 12.0


@pytest.mark.parametrize(
    "tests, message",
    [
        ([], "non-empty array"),
        (
            [
                {"id": "same", "scene": "Assets/A.scene", "run_seconds": 1},
                {"id": "same", "scene": "Assets/B.scene", "run_seconds": 1},
            ],
            "duplicate",
        ),
        ([{"id": "bad", "scene": "../escape.scene", "run_seconds": 1}], "inside"),
        ([{"id": "bad", "scene": "Assets/A.scene", "run_seconds": 0}], "positive"),
    ],
)
def test_manifest_rejects_ambiguous_documents(tmp_path, tests, message):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "All.json"
    _write_manifest(manifest_path, tests=tests)

    with pytest.raises(ValueError, match=message):
        RuntimeAcceptanceManifest.load(str(manifest_path), project_root=str(tmp_path))


def test_session_aggregates_same_schema_and_atomically_advances(monkeypatch, tmp_path):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "All.json"
    _write_manifest(manifest_path)
    current_scene = {"path": ""}

    class _SceneFileManager:
        current_scene_path = ""
        is_loading = False

        @classmethod
        def instance(cls):
            cls.current_scene_path = current_scene["path"]
            return cls

    class _SceneManager:
        @staticmethod
        def load_scene(path):
            current_scene["path"] = str((tmp_path / path).resolve())
            return True

        @staticmethod
        def is_scene_load_pending():
            return False

    monkeypatch.setattr(Application, "data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr(Application, "persistent_data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr("Infernux.engine.scene_manager.SceneFileManager", _SceneFileManager)
    monkeypatch.setattr("Infernux.scene.SceneManager", _SceneManager)

    initial = RuntimeAcceptance.begin(str(manifest_path))
    assert initial["summary"] == {
        "total": 2,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "pending": 2,
    }

    RuntimeAcceptance.tick(0.1)
    RuntimeAcceptance.tick(0.1)
    assert RuntimeAcceptance.current_test()["id"] == "render.forward"
    RuntimeAcceptance.pass_current({"draws": 1})

    RuntimeAcceptance.tick(0.1)
    RuntimeAcceptance.tick(0.1)
    RuntimeAcceptance.pass_current({"alive": 100000})
    final = RuntimeAcceptance.status()

    assert final["status"] == "passed"
    assert final["summary"] == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
    }
    assert [test["id"] for test in final["tests"]] == ["render.forward", "particle.sprite"]
    assert final["tests"][1]["details"] == {"alive": 100000}
    result_path = tmp_path / "Logs" / "All.result.json"
    assert json.loads(result_path.read_text(encoding="utf-8")) == final
    assert not (tmp_path / "Logs" / "All.result.json.tmp").exists()


def test_session_runs_every_test_for_each_manifest_cycle(monkeypatch, tmp_path):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "Soak.json"
    _write_manifest(
        manifest_path,
        tests=[{"id": "render.soak", "scene": "Assets/Soak.scene", "run_seconds": 1}],
        cycles=2,
    )
    target_scene = str((tmp_path / "Assets/Soak.scene").resolve())

    class _SceneFileManager:
        current_scene_path = target_scene
        is_loading = False

        @classmethod
        def instance(cls):
            return cls

    scene_loads = []

    def _load_scene(path):
        scene_loads.append(path)
        return True

    monkeypatch.setattr(Application, "data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr(Application, "persistent_data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr("Infernux.engine.scene_manager.SceneFileManager", _SceneFileManager)
    monkeypatch.setattr("Infernux.scene.SceneManager.load_scene", staticmethod(_load_scene))
    monkeypatch.setattr(
        "Infernux.scene.SceneManager.is_scene_load_pending", staticmethod(lambda: False)
    )

    initial = RuntimeAcceptance.begin(str(manifest_path))
    assert initial["cycles"] == 2
    assert initial["summary"]["total"] == 2
    assert initial["started_at_unix"] > 0.0
    assert initial["elapsed_wall_seconds"] >= 0.0

    RuntimeAcceptance.tick(0.1)
    assert RuntimeAcceptance.current_test()["$acceptance"] == {"cycle": 1, "cycles": 2}
    RuntimeAcceptance.pass_current({"iteration": 1})
    RuntimeAcceptance.tick(0.1)
    assert scene_loads == ["Assets/Soak.scene"]
    RuntimeAcceptance.tick(0.1)
    assert RuntimeAcceptance.current_test()["$acceptance"] == {"cycle": 2, "cycles": 2}
    final = RuntimeAcceptance.pass_current({"iteration": 2})

    assert final["status"] == "passed"
    assert final["elapsed_wall_seconds"] >= initial["elapsed_wall_seconds"]
    assert final["summary"] == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
    }
    assert [test["cycle"] for test in final["tests"]] == [1, 2]
    assert [test["details"]["iteration"] for test in final["tests"]] == [1, 2]


def test_session_failure_is_fail_fast_and_keeps_full_test_set(monkeypatch, tmp_path):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "All.json"
    _write_manifest(manifest_path)
    target_scene = str((tmp_path / "Assets/Acceptance/Forward.scene").resolve())

    class _SceneFileManager:
        current_scene_path = target_scene
        is_loading = False

        @classmethod
        def instance(cls):
            return cls

    monkeypatch.setattr(Application, "data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr(Application, "persistent_data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr("Infernux.engine.scene_manager.SceneFileManager", _SceneFileManager)

    RuntimeAcceptance.begin(str(manifest_path))
    RuntimeAcceptance.tick(0.1)
    final = RuntimeAcceptance.fail_current("renderer submission failed", {"draws": 0})

    assert final["status"] == "failed"
    assert final["summary"]["failed"] == 1
    assert final["summary"]["pending"] == 1
    assert final["tests"][0]["error"] == "renderer submission failed"
    assert final["tests"][1]["status"] == "pending"


def test_finished_session_is_consumed_by_engine_exactly_once(monkeypatch, tmp_path):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "All.json"
    _write_manifest(
        manifest_path,
        tests=[{"id": "only", "scene": "Assets/Only.scene", "run_seconds": 1}],
    )
    target_scene = str((tmp_path / "Assets/Only.scene").resolve())

    class _SceneFileManager:
        current_scene_path = target_scene
        is_loading = False

        @classmethod
        def instance(cls):
            return cls

    monkeypatch.setattr(Application, "data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr(Application, "persistent_data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr("Infernux.engine.scene_manager.SceneFileManager", _SceneFileManager)

    RuntimeAcceptance.begin(str(manifest_path))
    RuntimeAcceptance.tick(0.1)
    RuntimeAcceptance.pass_current()

    assert RuntimeAcceptance._consume_completion()["status"] == "passed"
    assert RuntimeAcceptance._consume_completion() == {}


def test_result_publication_retries_short_lived_reader_lock(monkeypatch, tmp_path):
    manifest_path = tmp_path / "Assets" / "Acceptance" / "All.json"
    _write_manifest(manifest_path)
    real_replace = __import__("os").replace
    attempts = {"count": 0}

    def _replace_with_reader_lock(source, destination):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(13, "result is temporarily open by a reader", destination)
        real_replace(source, destination)

    monkeypatch.setattr(Application, "data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr(Application, "persistent_data_path", staticmethod(lambda: str(tmp_path)))
    monkeypatch.setattr("Infernux.acceptance.os.replace", _replace_with_reader_lock)
    monkeypatch.setattr("Infernux.acceptance.time.sleep", lambda _seconds: None)

    status = RuntimeAcceptance.begin(str(manifest_path))

    assert status["status"] == "running"
    assert attempts["count"] == 3
    assert (tmp_path / "Logs" / "All.result.json").is_file()
