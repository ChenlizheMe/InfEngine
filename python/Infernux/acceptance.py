"""Public Editor/Player runtime acceptance orchestration.

The runner deliberately owns only orchestration: manifest validation, scene
sequencing, timeout handling, result aggregation, and atomic persistence.
Project components remain responsible for domain-specific assertions and
report them through :class:`RuntimeAcceptance`.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import PurePosixPath
import threading
from typing import Any, Mapping, Optional

from Infernux.application import Application
from Infernux.debug import Debug
from Infernux.engine.path_utils import (
    is_path_within,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
)


_MANIFEST_SCHEMA = "infernux.runtime_acceptance"
_RESULT_SCHEMA = "infernux.runtime_acceptance_result"


def _relative_asset_path(value: Any, label: str, suffix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty project-relative path")
    raw = value.strip()
    if os.path.isabs(raw):
        raise ValueError(f"{label} must stay inside the project")
    normalized = portable_path(raw)
    path = PurePosixPath(normalized)
    if path.is_absolute() or ":" in path.parts[0] or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the project")
    if path.suffix.casefold() != suffix:
        raise ValueError(f"{label} must reference a {suffix} file")
    return path.as_posix()


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{label} must be a finite positive number")
    return result


@dataclass(frozen=True)
class RuntimeAcceptanceTest:
    """One immutable test entry from a runtime acceptance manifest."""

    id: str
    scene: str
    run_seconds: float
    timeout_seconds: float
    document: dict[str, Any]


@dataclass(frozen=True)
class RuntimeAcceptanceManifest:
    """Validated acceptance manifest shared by Editor and Standalone."""

    name: str
    path: str
    tests: tuple[RuntimeAcceptanceTest, ...]

    @staticmethod
    def load(path: str, *, project_root: str = "") -> "RuntimeAcceptanceManifest":
        root = resolved_path(project_root or Application.data_path())
        if not root:
            raise RuntimeError("runtime acceptance requires an active project")
        manifest_path = resolved_path(path if os.path.isabs(path) else os.path.join(root, path))
        if not is_path_within(manifest_path, root, allow_root=False):
            raise ValueError("runtime acceptance manifest must stay inside the project")
        if not os.path.isfile(manifest_path):
            raise FileNotFoundError(manifest_path)

        with open(manifest_path, "r", encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict) or document.get("$schema") != _MANIFEST_SCHEMA:
            raise ValueError(f"runtime acceptance manifest must use schema {_MANIFEST_SCHEMA!r}")

        name = document.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("runtime acceptance manifest name must be a non-empty string")
        raw_tests = document.get("tests")
        if not isinstance(raw_tests, list) or not raw_tests:
            raise ValueError("runtime acceptance manifest tests must be a non-empty array")

        tests: list[RuntimeAcceptanceTest] = []
        ids: set[str] = set()
        for index, raw in enumerate(raw_tests):
            label = f"tests[{index}]"
            if not isinstance(raw, dict):
                raise ValueError(f"{label} must be an object")
            test_id = raw.get("id")
            if not isinstance(test_id, str) or not test_id.strip():
                raise ValueError(f"{label}.id must be a non-empty string")
            test_id = test_id.strip()
            if test_id in ids:
                raise ValueError(f"duplicate runtime acceptance test id: {test_id}")
            ids.add(test_id)

            scene = _relative_asset_path(raw.get("scene"), f"{label}.scene", ".scene")
            run_seconds = _positive_number(raw.get("run_seconds"), f"{label}.run_seconds")
            timeout_seconds = _positive_number(
                raw.get("timeout_seconds", max(run_seconds * 2.0, run_seconds + 10.0)),
                f"{label}.timeout_seconds",
            )
            if timeout_seconds < run_seconds:
                raise ValueError(f"{label}.timeout_seconds cannot be shorter than run_seconds")
            tests.append(
                RuntimeAcceptanceTest(
                    id=test_id,
                    scene=scene,
                    run_seconds=run_seconds,
                    timeout_seconds=timeout_seconds,
                    document=deepcopy(raw),
                )
            )
        return RuntimeAcceptanceManifest(name=name.strip(), path=manifest_path, tests=tuple(tests))


class _RuntimeAcceptanceSession:
    def __init__(self, manifest: RuntimeAcceptanceManifest, result_path: str):
        self.manifest = manifest
        self.result_path = result_path
        self.index = 0
        self.phase = "pending_load"
        self.elapsed = 0.0
        self.results: list[dict[str, Any]] = []
        self.finished = False
        self.exit_code = 0
        self._write_result()

    @property
    def current(self) -> Optional[RuntimeAcceptanceTest]:
        if self.finished or self.index >= len(self.manifest.tests):
            return None
        return self.manifest.tests[self.index]

    def tick(self, delta_time: float) -> None:
        current = self.current
        if current is None:
            return
        if self.phase == "pending_load":
            self._begin_scene_load(current)
            return
        if self.phase == "loading":
            self.elapsed += max(0.0, float(delta_time))
            self._poll_scene_load(current)
            return
        if self.phase != "running":
            return
        self.elapsed += max(0.0, float(delta_time))
        if self.elapsed > current.timeout_seconds:
            self.complete(
                "failed",
                error=f"test timed out after {current.timeout_seconds:g} seconds",
                details={"phase": "running", "elapsed_seconds": self.elapsed},
            )

    def _begin_scene_load(self, current: RuntimeAcceptanceTest) -> None:
        from Infernux.engine.scene_manager import SceneFileManager
        from Infernux.scene import SceneManager

        manager = SceneFileManager.instance()
        target = resolved_path(os.path.join(Application.data_path(), current.scene))
        if manager is not None and manager.current_scene_path and same_path(manager.current_scene_path, target):
            self.phase = "running"
            self.elapsed = 0.0
            self._write_result()
            return
        if not SceneManager.load_scene(current.scene):
            self.complete("failed", error=f"failed to load acceptance scene: {current.scene}")
            return
        self.phase = "loading"
        self.elapsed = 0.0
        self._write_result()

    def _poll_scene_load(self, current: RuntimeAcceptanceTest) -> None:
        from Infernux.engine.scene_manager import SceneFileManager
        from Infernux.scene import SceneManager

        manager = SceneFileManager.instance()
        target = resolved_path(os.path.join(Application.data_path(), current.scene))
        if manager is not None and manager.current_scene_path and same_path(manager.current_scene_path, target):
            self.phase = "running"
            self.elapsed = 0.0
            self._write_result()
            return
        if manager is None or manager.is_loading or SceneManager.is_scene_load_pending():
            return
        if self.elapsed > current.timeout_seconds:
            self.complete("failed", error=f"acceptance scene did not become active: {current.scene}")

    def complete(
        self,
        status: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
        error: str = "",
    ) -> None:
        current = self.current
        if current is None:
            raise RuntimeError("runtime acceptance has no active test")
        normalized = str(status).strip().lower()
        if normalized not in {"passed", "failed"}:
            raise ValueError("runtime acceptance result must be 'passed' or 'failed'")
        result: dict[str, Any] = {
            "id": current.id,
            "scene": current.scene,
            "status": normalized,
            "elapsed_seconds": self.elapsed,
            "details": deepcopy(dict(details or {})),
        }
        if error:
            result["error"] = str(error)
        self.results.append(result)
        if normalized == "failed":
            self.finished = True
            self.phase = "finished"
            self.exit_code = 1
        else:
            self.index += 1
            self.elapsed = 0.0
            if self.index >= len(self.manifest.tests):
                self.finished = True
                self.phase = "finished"
            else:
                self.phase = "pending_load"
        self._write_result()

    def snapshot(self) -> dict[str, Any]:
        completed = {result["id"]: result for result in self.results}
        tests = []
        for test in self.manifest.tests:
            tests.append(
                deepcopy(
                    completed.get(
                        test.id,
                        {
                            "id": test.id,
                            "scene": test.scene,
                            "status": "running" if self.current is test and self.phase == "running" else "pending",
                        },
                    )
                )
            )
        passed = sum(result["status"] == "passed" for result in self.results)
        failed = sum(result["status"] == "failed" for result in self.results)
        status = "failed" if failed else ("passed" if self.finished else "running")
        return {
            "$schema": _RESULT_SCHEMA,
            "name": self.manifest.name,
            "environment": "player" if Application.is_player() else "editor",
            "manifest": relative_path(
                self.manifest.path,
                Application.data_path(),
                allow_root=False,
            ),
            "status": status,
            "summary": {
                "total": len(self.manifest.tests),
                "passed": passed,
                "failed": failed,
                "skipped": 0,
                "pending": len(self.manifest.tests) - passed - failed,
            },
            "tests": tests,
        }

    def _write_result(self) -> None:
        os.makedirs(os.path.dirname(self.result_path), exist_ok=True)
        temporary = self.result_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(self.snapshot(), stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.result_path)


class RuntimeAcceptance:
    """Process-wide public acceptance API used by project assertion components."""

    _lock = threading.RLock()
    _session: Optional[_RuntimeAcceptanceSession] = None
    _completion_consumed = False

    @classmethod
    def begin(cls, manifest_path: str, result_path: str = "") -> dict[str, Any]:
        with cls._lock:
            if cls._session is not None and not cls._session.finished:
                raise RuntimeError("a runtime acceptance session is already active")
            manifest = RuntimeAcceptanceManifest.load(manifest_path)
            output = cls._resolve_result_path(manifest, result_path)
            cls._session = _RuntimeAcceptanceSession(manifest, output)
            cls._completion_consumed = False
            Debug.log(f"[RuntimeAcceptance] begin name={manifest.name!r} tests={len(manifest.tests)} result={output}")
            return cls._session.snapshot()

    @classmethod
    def tick(cls, delta_time: float) -> dict[str, Any]:
        with cls._lock:
            if cls._session is None:
                return {}
            cls._session.tick(delta_time)
            return cls._session.snapshot()

    @classmethod
    def current_test(cls) -> dict[str, Any]:
        with cls._lock:
            current = cls._require_session().current
            return deepcopy(current.document) if current is not None else {}

    @classmethod
    def pass_current(cls, details: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        with cls._lock:
            session = cls._require_session()
            session.complete("passed", details=details)
            return session.snapshot()

    @classmethod
    def fail_current(
        cls,
        error: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with cls._lock:
            session = cls._require_session()
            session.complete("failed", details=details, error=str(error))
            return session.snapshot()

    @classmethod
    def status(cls) -> dict[str, Any]:
        with cls._lock:
            return cls._session.snapshot() if cls._session is not None else {}

    @classmethod
    def is_active(cls) -> bool:
        with cls._lock:
            return cls._session is not None and not cls._session.finished

    @classmethod
    def _consume_completion(cls) -> dict[str, Any]:
        """Return a finished session exactly once to the owning Engine."""
        with cls._lock:
            if (
                cls._session is None
                or not cls._session.finished
                or cls._completion_consumed
            ):
                return {}
            cls._completion_consumed = True
            return cls._session.snapshot()

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._session = None
            cls._completion_consumed = False

    @classmethod
    def _require_session(cls) -> _RuntimeAcceptanceSession:
        if cls._session is None:
            raise RuntimeError("runtime acceptance has not been started")
        return cls._session

    @staticmethod
    def _resolve_result_path(manifest: RuntimeAcceptanceManifest, result_path: str) -> str:
        root = resolved_path(Application.persistent_data_path())
        if not root:
            raise RuntimeError("runtime acceptance requires a persistent data path")
        if result_path:
            output = resolved_path(result_path if os.path.isabs(result_path) else os.path.join(root, result_path))
        else:
            stem = os.path.splitext(os.path.basename(manifest.path))[0]
            output = resolved_path(os.path.join(root, "Logs", f"{stem}.result.json"))
        if not is_path_within(output, root, allow_root=False):
            raise ValueError("runtime acceptance result must stay inside persistent_data_path")
        if os.path.splitext(output)[1].casefold() != ".json":
            raise ValueError("runtime acceptance result must be a .json file")
        return output


__all__ = [
    "RuntimeAcceptance",
    "RuntimeAcceptanceManifest",
    "RuntimeAcceptanceTest",
]
