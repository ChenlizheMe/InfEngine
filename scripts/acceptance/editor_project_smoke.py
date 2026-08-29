"""Exercise project load, save, Play Mode, and shutdown in the GUI Editor."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Callable

from Infernux import release_engine
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.host.commands import MainThreadCommandQueue
from Infernux.host.editor import EditorAutomationHost


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Project root containing Assets and ProjectSettings")
    parser.add_argument("--scene", required=True, help="Project-relative .scene path")
    parser.add_argument("--play-seconds", type=float, default=5.0)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    parser.add_argument("--transition-timeout", type=float, default=30.0)
    return parser


def _emit(event: str, **payload: object) -> None:
    print(
        json.dumps(
            {"schema": "infernux.editor_project_smoke", "event": event, **payload},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def _wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    label: str,
    interval: float = 0.05,
) -> Any:
    deadline = time.monotonic() + max(float(timeout), 0.01)
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {label}; last value: {last_value!r}")


def _run_smoke(
    project: str,
    scene_path: str,
    *,
    play_seconds: float,
    startup_timeout: float,
    transition_timeout: float,
) -> None:
    queue = MainThreadCommandQueue.instance()
    try:
        if not queue.wait_until_ready(startup_timeout):
            raise TimeoutError("Editor main-thread command queue did not become ready")
        host = EditorAutomationHost.instance()

        def run(name: str, fn: Callable[[], Any]) -> Any:
            return queue.run_sync(
                f"editor-smoke.{name}",
                fn,
                timeout_ms=max(int(transition_timeout * 1000), 1),
            )

        def project_info() -> dict[str, object]:
            return run("project-info", lambda: host.project_info(project))

        _wait_until(
            lambda: project_info().get("active_scene", {}).get("path"),
            timeout=startup_timeout,
            label="initial scene",
        )

        from Infernux.engine.scene_manager import SceneFileManager

        manager = run("scene-manager", SceneFileManager.instance)
        if manager is None:
            raise RuntimeError("SceneFileManager is unavailable")
        if not same_path(manager.current_scene_path or "", scene_path):
            accepted = run("open-scene", lambda: manager.open_scene(scene_path))
            if not accepted:
                raise RuntimeError(f"Editor rejected scene open: {scene_path}")
            _wait_until(
                lambda: (
                    not manager.is_loading
                    and same_path(manager.current_scene_path or "", scene_path)
                ),
                timeout=startup_timeout,
                label="requested scene",
            )

        saved = run("save-scene", manager.save_current_scene)
        if not saved:
            raise RuntimeError(f"Editor failed to save scene: {scene_path}")
        _emit("editor-ready", project=project, scene=scene_path, saved=True)

        entered = run(
            "enter-play",
            lambda: host.runtime_transition("enter_play_mode"),
        )
        if not bool(entered.get("accepted")):
            raise RuntimeError(f"Editor rejected Play Mode: {entered!r}")
        playing = _wait_until(
            lambda: (
                status
                if (status := run("runtime-status", host.runtime_status)).get("state")
                == "playing"
                else None
            ),
            timeout=transition_timeout,
            label="Play Mode",
        )
        _emit("play-entered", runtime=playing)

        time.sleep(max(float(play_seconds), 0.1))
        played = run("runtime-after-play", host.runtime_status)
        if float(played.get("total_play_time", 0.0)) <= 0.0:
            raise RuntimeError(f"Play Mode clock did not advance: {played!r}")

        exited = run(
            "exit-play",
            lambda: host.runtime_transition("exit_play_mode"),
        )
        if not bool(exited.get("accepted")):
            raise RuntimeError(f"Editor rejected Play Mode exit: {exited!r}")
        editing = _wait_until(
            lambda: (
                status
                if (status := run("runtime-status", host.runtime_status)).get("state")
                == "edit"
                else None
            ),
            timeout=transition_timeout,
            label="Edit Mode restore",
        )
        _emit(
            "passed",
            project=project,
            scene=scene_path,
            play_seconds=float(played.get("total_play_time", 0.0)),
            enter_timings_ms=playing.get("transition_timings_ms", {}),
            exit_timings_ms=editing.get("transition_timings_ms", {}),
        )
        run("close", host.request_editor_close)
    except BaseException as exc:
        _emit("failed", error=f"{type(exc).__name__}: {exc}")
        try:
            queue.run_sync(
                "editor-smoke.close-after-failure",
                EditorAutomationHost.instance().request_editor_close,
                timeout_ms=5000,
            )
        except BaseException:
            pass
        os._exit(1)


def main() -> int:
    args = _parser().parse_args()
    project = resolved_path(args.project)
    scene_relative = str(args.scene).replace("\\", "/")
    scene_path = resolved_path(os.path.join(project, *scene_relative.split("/")))
    if not os.path.isdir(project):
        raise FileNotFoundError(project)
    if not os.path.isfile(scene_path):
        raise FileNotFoundError(scene_path)
    if args.play_seconds <= 0:
        raise ValueError("--play-seconds must be positive")

    worker = threading.Thread(
        target=_run_smoke,
        args=(project, scene_path),
        kwargs={
            "play_seconds": args.play_seconds,
            "startup_timeout": args.startup_timeout,
            "transition_timeout": args.transition_timeout,
        },
        name="InfernuxEditorProjectSmoke",
        daemon=True,
    )
    worker.start()
    release_engine(project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
