"""Isolated smoke test for the native no-window runtime composition."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from pathlib import Path

from Infernux import Engine, RigidbodyInterpolation, Time, run_headless
from Infernux import resources as engine_resources
from Infernux.lib import AssetRegistry, Physics, RuntimeMode, SceneManager, Vector3
from Infernux.physics.settings import DEFAULT_PHYSICS_SETTINGS, save as save_physics_settings

assert not any(
    name == "Infernux.engine.ui" or name.startswith("Infernux.engine.ui.")
    for name in sys.modules
)


def _owned_window_count() -> int:
    if os.name != "nt":
        return 0

    import ctypes
    from ctypes import wintypes

    count = 0
    current_pid = os.getpid()
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def visit(hwnd, _lparam):
        nonlocal count
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == current_pid:
            count += 1
        return True

    ctypes.windll.user32.EnumWindows(visit, 0)
    return count


def main() -> None:
    initial_windows = _owned_window_count()
    package_resources = Path(engine_resources.get_package_resources_path())
    builtin_meta_before = {str(path) for path in package_resources.rglob("*.meta")}
    builtin_shader = Path(engine_resources.resources_path) / "shaders" / "standard.vert"

    with tempfile.TemporaryDirectory(prefix="infernux-headless-") as root:
        project = Path(root)
        (project / "Assets").mkdir()
        (project / "ProjectSettings").mkdir()
        save_physics_settings(
            str(project),
            {
                **DEFAULT_PHYSICS_SETTINGS,
                "gravity": [1.0, -12.0, 3.0],
                "fixed_delta_time": 0.01,
                "max_fixed_delta_time": 0.04,
            },
        )

        engine = Engine(mode=RuntimeMode.Headless)
        native = engine.get_native_engine()
        assert native.runtime_mode == RuntimeMode.Headless
        assert not native.has_renderer

        try:
            engine.init_headless(str(project))
            assert not native.has_renderer
            assert _owned_window_count() == initial_windows
            try:
                engine.request_render_target_readback()
            except RuntimeError:
                pass
            else:
                raise AssertionError("headless runtime accepted a GPU image readback")
            first_builtin_guid = engine.get_asset_database().get_guid_from_path(str(builtin_shader))
            assert first_builtin_guid

            scene = SceneManager.instance().get_active_scene()
            assert scene is not None
            assert scene.name == "Headless Scene"
            gravity = Physics.get_gravity()
            assert (gravity.x, gravity.y, gravity.z) == (1.0, -12.0, 3.0)
            assert math.isclose(SceneManager.instance().get_fixed_time_step(), 0.01, abs_tol=1e-7)
            assert math.isclose(SceneManager.instance().get_max_fixed_delta_time(), 0.04, abs_tol=1e-7)
            engine.tick(0.02)

            mover = scene.create_game_object("KinematicInterpolationProbe")
            mover.transform.position = Vector3(0.0, 2.0, 0.0)
            rigidbody = mover.add_component("Rigidbody")
            rigidbody.is_kinematic = True
            rigidbody.interpolation = RigidbodyInterpolation.Interpolate
            mover.add_component("SphereCollider")
            scene_manager = SceneManager.instance()
            scene_manager.play()

            Time.time_scale = 0.5
            engine.tick(0.02)
            scaled_profile = scene_manager.get_last_frame_profile()
            assert scaled_profile["fixed_steps"] == 1.0
            assert math.isclose(Time.delta_time, 0.01, abs_tol=1e-7)
            assert math.isclose(Time.fixed_time, 0.01, abs_tol=1e-7)
            assert math.isclose(Time.fixed_unscaled_time, 0.02, abs_tol=1e-7)

            Time.time_scale = 0.0
            fixed_time_before_pause = Time.fixed_time
            engine.tick(0.02)
            assert scene_manager.get_last_frame_profile()["fixed_steps"] == 0.0
            assert math.isclose(Time.delta_time, 0.0, abs_tol=1e-7)
            assert math.isclose(Time.fixed_time, fixed_time_before_pause, abs_tol=1e-7)

            Time.time_scale = 1.0
            engine.tick(0.01)

            rigidbody.move_position(Vector3(4.0, 2.0, 0.0))
            assert math.isclose(mover.transform.position.x, 0.0, abs_tol=1e-5)
            engine.tick(0.01)
            assert math.isclose(rigidbody.position.x, 4.0, abs_tol=1e-4)
            assert math.isclose(mover.transform.position.x, 0.0, abs_tol=1e-4)
            engine.tick(0.005)
            assert math.isclose(mover.transform.position.x, 2.0, abs_tol=1e-3)
            scene_manager.stop()

            try:
                engine.tick(math.nan)
            except ValueError:
                pass
            else:
                raise AssertionError("NaN delta was accepted")

            try:
                engine.init_renderer(1, 1, str(project))
            except RuntimeError:
                pass
            else:
                raise AssertionError("headless runtime accepted renderer initialization")

            database = engine.get_asset_database()
            database.refresh()
            cpu_model = project / "Assets" / "shutdown-cpu-load.obj"
            cpu_model.write_text(
                "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                encoding="ascii",
            )
            cpu_model_guid = database.import_asset(str(cpu_model)).guid
            assert cpu_model_guid
            shutdown_load = AssetRegistry.instance().begin_load_mesh_by_guid(cpu_model_guid)

            pending_model = project / "Assets" / "shutdown-pending.obj"
            pending_model.write_text(
                "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                encoding="ascii",
            )
            database.begin_refresh()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if database.try_commit_refresh():
                    raise AssertionError("pending importer finalized before the shutdown barrier")
                if database.last_refresh_importer_task_count > 0:
                    break
                time.sleep(0.001)
            else:
                raise AssertionError("headless refresh never entered its importer phase")
            assert database.refresh_pending

            engine.request_exit()
            assert native.exit_requested
        finally:
            engine.exit()
        assert shutdown_load.complete
        assert shutdown_load.produced_on_worker

        second_lifetime_guids = []
        second_lifetime_models = []

        def update(second_engine, frame):
            if frame == 0:
                active_builtin_shader = (
                    Path(engine_resources.resources_path) / "shaders" / "standard.vert"
                )
                second_lifetime_guids.append(
                    second_engine.get_asset_database().get_guid_from_path(
                        str(active_builtin_shader)
                    )
                )
                model_meta = second_engine.get_asset_database().get_meta_by_path(str(pending_model))
                second_lifetime_models.append(
                    (model_meta.get_guid(), model_meta.get_int("mesh_count"))
                )
            return frame < 2

        frames = run_headless(
            str(project),
            update=update,
            fixed_delta=0.01,
        )
        assert frames == 2
        assert second_lifetime_guids == [first_builtin_guid], (
            first_builtin_guid,
            second_lifetime_guids,
        )
        assert second_lifetime_models[0][0]
        assert second_lifetime_models[0][1] == 1

        class ExpectedUpdateFailure(RuntimeError):
            pass

        def fail_update(_engine, _frame):
            raise ExpectedUpdateFailure("intentional headless callback failure")

        try:
            run_headless(str(project), update=fail_update, max_frames=1)
        except ExpectedUpdateFailure:
            pass
        else:
            raise AssertionError("headless update exception was swallowed")

        restarted_frames = run_headless(
            str(project),
            update=lambda _engine, _frame: True,
            fixed_delta=0.01,
            max_frames=1,
        )
        assert restarted_frames == 1

    builtin_meta_after = {str(path) for path in package_resources.rglob("*.meta")}
    assert builtin_meta_after == builtin_meta_before

    print("Headless runtime smoke test passed")


if __name__ == "__main__":
    main()
