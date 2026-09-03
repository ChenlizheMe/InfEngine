"""Load and play an Infernux project without creating a desktop window."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
import time
from typing import Any

import infernux as inx
from Infernux import run_headless
from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.lib import SceneManager as NativeSceneManager
from Infernux.scene import SceneManager


@dataclass
class _State:
    requested: bool = False
    active: bool = False
    play_start_frame: int | None = None
    played_frames: int = 0
    error: str = ""
    object_names: list[str] | None = None
    component_names: list[str] | None = None
    renderer_materials: dict[str, list[dict[str, Any]]] | None = None
    trajectory: list[dict[str, Any]] | None = None


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


def _vector3(value: Any) -> list[float]:
    return [round(float(getattr(value, axis)), 6) for axis in ("x", "y", "z")]


def _capture_renderer_materials(objects: list[Any]) -> dict[str, list[dict[str, Any]]]:
    captured: dict[str, list[dict[str, Any]]] = {}
    for obj in objects:
        slots: list[dict[str, Any]] = []
        # Native GameObject.get_component is intentionally string-based.  The
        # public Python wrapper accepts component classes, but this acceptance
        # path walks the native scene directly so it must use native type names.
        renderer = obj.get_component("SkinnedMeshRenderer")
        if renderer is None:
            renderer = obj.get_component("MeshRenderer")
        if renderer is not None:
            renderer = getattr(renderer, "_cpp_component", renderer)
            get_material = getattr(renderer, "get_effective_material", None)
            if callable(get_material):
                material_count = max(1, int(getattr(renderer, "material_count", 0)))
                for slot in range(material_count):
                    material = get_material(slot)
                    if material is None:
                        continue
                    color = material.get_color("baseColor")
                    slots.append(
                        {
                            "component": _component_name(renderer),
                            "slot": slot,
                            "name": str(getattr(material, "name", "")),
                            "base_color": [round(float(value), 6) for value in color],
                        }
                    )
        if slots:
            captured[str(obj.name)] = slots
    return captured


def _capture_trajectory_sample(
    objects: list[Any], tracked_names: list[str], frame: int, fixed_delta: float
) -> dict[str, Any]:
    objects_by_name: dict[str, list[Any]] = {}
    for obj in objects:
        objects_by_name.setdefault(str(obj.name), []).append(obj)

    samples: dict[str, Any] = {}
    for name in tracked_names:
        matches = objects_by_name.get(name, [])
        if len(matches) != 1:
            samples[name] = {"status": "missing" if not matches else "ambiguous"}
            continue
        obj = matches[0]
        sample: dict[str, Any] = {"position": _vector3(obj.transform.position)}
        for component in obj.get_components():
            if _component_name(component) != "Rigidbody":
                continue
            for attribute in ("position", "velocity", "angular_velocity"):
                try:
                    sample[attribute] = _vector3(getattr(component, attribute))
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
            break
        samples[name] = sample
    return {
        "frame": int(frame),
        "time_seconds": round(float(frame) * fixed_delta, 6),
        "objects": samples,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Project root containing Assets and ProjectSettings")
    parser.add_argument("--scene", required=True, help="Project-relative .scene path")
    parser.add_argument("--play-frames", type=int, default=120)
    parser.add_argument("--fixed-delta", type=float, default=1.0 / 60.0)
    parser.add_argument("--load-timeout-frames", type=int, default=600)
    parser.add_argument("--require-object", action="append", default=[])
    parser.add_argument("--require-component", action="append", default=[])
    parser.add_argument(
        "--track-object",
        action="append",
        default=[],
        help="Record one uniquely named object's transform and Rigidbody state",
    )
    parser.add_argument("--sample-every", type=int, default=30)
    parser.add_argument(
        "--trajectory-output",
        help="Optional path for the same structured result printed to stdout",
    )
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
    if args.fixed_delta <= 0.0 or args.sample_every <= 0:
        raise ValueError("--fixed-delta and --sample-every must be positive")

    tracked_names = list(dict.fromkeys(str(name) for name in args.track_object))
    state = _State(trajectory=[])

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
                    state.play_start_frame = _frame
                    objects = _walk_objects(list(native_manager.get_active_scene().get_root_objects()))
                    if tracked_names:
                        state.trajectory.append(
                            _capture_trajectory_sample(
                                objects, tracked_names, 0, args.fixed_delta
                            )
                        )
                    return True
                elif _frame + 1 >= args.load_timeout_frames:
                    state.error = f"scene did not become active: {scene_relative}"
                    return False
            if not state.active:
                # Asset import and scene preparation can use worker threads.
                time.sleep(0.001)
                return True

        state.played_frames = _frame - int(state.play_start_frame or 0)
        if (
            tracked_names
            and state.played_frames > 0
            and (
                state.played_frames % args.sample_every == 0
                or state.played_frames == args.play_frames
            )
        ):
            scene = NativeSceneManager.instance().get_active_scene()
            if scene is None:
                state.error = "no active scene while sampling headless trajectory"
                return False
            state.trajectory.append(
                _capture_trajectory_sample(
                    _walk_objects(list(scene.get_root_objects())),
                    tracked_names,
                    state.played_frames,
                    args.fixed_delta,
                )
            )
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
        state.renderer_materials = _capture_renderer_materials(objects)
        return False

    run_headless(
        project,
        update,
        fixed_delta=args.fixed_delta,
        max_frames=args.load_timeout_frames + args.play_frames,
    )
    if state.error:
        raise RuntimeError(state.error)
    if not state.active or state.played_frames < args.play_frames:
        raise RuntimeError("headless project smoke ended before the requested play interval")

    object_names = state.object_names or []
    component_names = state.component_names or []
    missing_objects = sorted(
        (set(args.require_object) | set(tracked_names)).difference(object_names)
    )
    missing_components = sorted(set(args.require_component).difference(component_names))
    result = {
        "schema": "infernux.headless_project_smoke",
        "status": "passed" if not missing_objects and not missing_components else "failed",
        "project": project,
        "scene": scene_relative,
        "fixed_delta": args.fixed_delta,
        "play_frames": state.played_frames,
        "object_count": len(object_names),
        "component_count": len(component_names),
        "objects": object_names,
        "components": sorted(set(component_names)),
        "renderer_materials": state.renderer_materials or {},
        "missing_objects": missing_objects,
        "missing_components": missing_components,
        "trajectory": state.trajectory,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.trajectory_output:
        output_path = resolved_path(args.trajectory_output)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
