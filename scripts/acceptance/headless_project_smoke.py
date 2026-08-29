"""Load and play an Infernux project without creating a desktop window."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
import time
from typing import Any

from Infernux import run_headless
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.lib import SceneManager as NativeSceneManager
from Infernux.scene import SceneManager


@dataclass
class _State:
    requested: bool = False
    active: bool = False
    played_frames: int = 0
    error: str = ""
    object_names: list[str] | None = None
    component_names: list[str] | None = None


def _walk_objects(roots: list[Any]) -> list[Any]:
    pending = list(reversed(roots))
    result: list[Any] = []
    while pending:
        current = pending.pop()
        result.append(current)
        pending.extend(
            current.get_child(index)
            for index in range(current.get_child_count() - 1, -1, -1)
        )
    return result


def _component_name(component: Any) -> str:
    python_name = getattr(component, "get_py_type_name", None)
    if callable(python_name):
        value = str(python_name() or "")
        if value:
            return value.rsplit(".", 1)[-1]
    return str(getattr(component, "type_name", "") or type(component).__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Project root containing Assets and ProjectSettings")
    parser.add_argument("--scene", required=True, help="Project-relative .scene path")
    parser.add_argument("--play-frames", type=int, default=120)
    parser.add_argument("--load-timeout-frames", type=int, default=600)
    parser.add_argument("--require-object", action="append", default=[])
    parser.add_argument("--require-component", action="append", default=[])
    return parser


def main() -> int:
    args = _parser().parse_args()
    project = resolved_path(args.project)
    scene_relative = str(args.scene).replace("\\", "/")
    scene_path = resolved_path(os.path.join(project, *scene_relative.split("/")))
    if not os.path.isdir(project):
        raise FileNotFoundError(project)
    if not os.path.isfile(scene_path):
        raise FileNotFoundError(scene_path)
    if args.play_frames <= 0 or args.load_timeout_frames <= 0:
        raise ValueError("frame limits must be positive")

    state = _State()

    def update(_engine: Any, _frame: int) -> bool:
        if not state.requested:
            state.requested = True
            if not SceneManager.load_scene(scene_relative):
                state.error = f"failed to request scene load: {scene_relative}"
                return False

        manager = SceneFileManager.instance()
        if not state.active:
            pending = SceneManager.is_scene_load_pending()
            loading = bool(manager is not None and manager.is_loading)
            if not pending and not loading and manager is not None:
                if manager.current_scene_path and same_path(manager.current_scene_path, scene_path):
                    native_manager = NativeSceneManager.instance()
                    if native_manager is None:
                        state.error = "native SceneManager is unavailable after scene load"
                        return False
                    native_manager.play()
                    state.active = True
                elif _frame + 1 >= args.load_timeout_frames:
                    state.error = f"scene did not become active: {scene_relative}"
                    return False
            if not state.active:
                # Asset import and scene preparation can use worker threads.
                time.sleep(0.001)
                return True

        state.played_frames += 1
        if state.played_frames < args.play_frames:
            return True
        scene = NativeSceneManager.instance().get_active_scene()
        if scene is None:
            state.error = "no active scene after headless project smoke"
            return False
        objects = _walk_objects(list(scene.get_root_objects()))
        state.object_names = [str(obj.name) for obj in objects]
        state.component_names = [
            _component_name(component)
            for obj in objects
            for component in obj.get_components()
        ]
        return False

    run_headless(
        project,
        update,
        max_frames=args.load_timeout_frames + args.play_frames,
    )
    if state.error:
        raise RuntimeError(state.error)
    if not state.active or state.played_frames < args.play_frames:
        raise RuntimeError("headless project smoke ended before the requested play interval")

    object_names = state.object_names or []
    component_names = state.component_names or []
    missing_objects = sorted(set(args.require_object).difference(object_names))
    missing_components = sorted(set(args.require_component).difference(component_names))
    result = {
        "schema": "infernux.headless_project_smoke",
        "status": "passed" if not missing_objects and not missing_components else "failed",
        "project": project,
        "scene": scene_relative,
        "play_frames": state.played_frames,
        "object_count": len(object_names),
        "component_count": len(component_names),
        "objects": object_names,
        "components": sorted(set(component_names)),
        "missing_objects": missing_objects,
        "missing_components": missing_components,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
