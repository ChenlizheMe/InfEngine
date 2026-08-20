"""Sixty-cycle scene load/unload residency stability gate."""

from __future__ import annotations

import ctypes
import gc
import os
import sys
import tempfile
import traceback
from pathlib import Path

from Infernux import Engine
from Infernux.lib import (
    InxMaterial,
    LogLevel,
    Physics,
    PrimitiveType,
    SceneManager,
    Vector3,
)


CYCLES = 60
WARMUP_CYCLES = 10
MIB = 1024 * 1024


if sys.platform == "win32":
    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    _GET_CURRENT_PROCESS = ctypes.windll.kernel32.GetCurrentProcess
    _GET_CURRENT_PROCESS.restype = ctypes.c_void_p
    _GET_PROCESS_MEMORY_INFO = ctypes.windll.psapi.GetProcessMemoryInfo
    _GET_PROCESS_MEMORY_INFO.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCountersEx),
        ctypes.c_ulong,
    ]
    _GET_PROCESS_MEMORY_INFO.restype = ctypes.c_int


def _process_memory_bytes() -> tuple[int, int]:
    if sys.platform == "win32":
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        process = _GET_CURRENT_PROCESS()
        if not _GET_PROCESS_MEMORY_INFO(
            process, ctypes.byref(counters), counters.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.working_set_size), int(counters.private_usage)

    statm = Path("/proc/self/statm")
    if statm.is_file():
        fields = statm.read_text(encoding="ascii").split()
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(fields[1]) * page_size, int(fields[5]) * page_size

    import resource

    resident = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = int(resident if sys.platform == "darwin" else resident * 1024)
    return rss, rss


def _assert_stable(name: str, values: list[int], tolerance: int) -> None:
    tail = values[WARMUP_CYCLES:]
    if len(tail) != CYCLES - WARMUP_CYCLES:
        raise AssertionError(f"{name}: incomplete samples ({len(values)}/{CYCLES})")
    first_window_peak = max(tail[:10])
    final_window_peak = max(tail[-10:])
    if final_window_peak > first_window_peak + tolerance:
        raise AssertionError(
            f"{name}: final window grew from {first_window_peak} to "
            f"{final_window_peak} (tolerance={tolerance})"
        )
    if max(tail[-10:]) - min(tail[-10:]) > tolerance:
        raise AssertionError(f"{name}: final window did not stabilize: {tail[-10:]}")


def _assert_process_memory_stable(name: str, values: list[int]) -> None:
    tail = values[WARMUP_CYCLES:]
    if len(tail) != CYCLES - WARMUP_CYCLES:
        raise AssertionError(f"{name}: incomplete samples ({len(values)}/{CYCLES})")
    first_window_peak = max(tail[:10])
    previous_window_peak = max(tail[-20:-10])
    final_window = tail[-10:]
    final_window_peak = max(final_window)
    if final_window_peak > first_window_peak + 4 * MIB:
        raise AssertionError(
            f"{name}: bounded process residency grew from {first_window_peak} to "
            f"{final_window_peak}"
        )
    if final_window_peak > previous_window_peak + MIB // 2:
        raise AssertionError(
            f"{name}: process residency kept growing from {previous_window_peak} to "
            f"{final_window_peak} in the final two windows"
        )
    # Allocators may return a whole region at any point in the final window.
    # A downward step is healthy and its timing is scheduler-dependent, so it
    # must not fail a leak gate. The bounded and consecutive peak checks above
    # still reject both long-term and late-window growth.


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="infernux-scene-soak-") as root:
        project = Path(root)
        assets = project / "Assets"
        assets.mkdir()
        (project / "ProjectSettings").mkdir()

        mesh_path = assets / "SoakTriangle.obj"
        mesh_path.write_text(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\n"
            "vn 0 0 1\nf 1/1/1 2/2/1 3/3/1\n",
            encoding="ascii",
        )
        texture_path = assets / "SoakTexture.ppm"
        texture_path.write_bytes(
            b"P6\n2 2\n255\n"
            + bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255))
        )

        frontend = Engine()
        engine = frontend.get_native_engine()
        engine.set_log_level(LogLevel.Warn)
        try:
            try:
                frontend.init_renderer(64, 64, str(project))
            except (OSError, RuntimeError) as exception:
                print(f"Scene residency soak skipped: {exception}")
                return 77

            database = frontend.get_asset_database()
            mesh_guid = database.get_guid_from_path(str(mesh_path))
            texture_guid = database.get_guid_from_path(str(texture_path))
            assert mesh_guid and texture_guid

            manager = SceneManager.instance()
            initial_scene = manager.get_active_scene()
            if initial_scene is not None:
                manager.unload_scene(initial_scene)

            state: dict[str, object] = {
                "phase": "create",
                "cycle": 0,
                "settle_frames": 0,
                "scene": None,
            }
            rss_samples: list[int] = []
            private_samples: list[int] = []
            python_block_samples: list[int] = []
            python_object_samples: list[int] = []
            scene_count_samples: list[int] = []
            device_samples: list[int] = []
            allocation_count_samples: list[int] = []
            cpu_samples: list[int] = []
            gpu_asset_samples: list[int] = []
            material_ubo_samples: list[int] = []
            material_render_data_samples: list[int] = []
            material_descriptor_samples: list[int] = []
            retired_material_descriptor_samples: list[int] = []
            material_descriptor_pool_samples: list[int] = []
            material_pipeline_samples: list[int] = []
            runtime_material_samples: list[int] = []
            asset_material_samples: list[int] = []
            runtime_mesh_samples: list[int] = []
            physics_body_samples: list[int] = []
            peak_physics_body_count = 0

            def run_cycle() -> None:
                nonlocal peak_physics_body_count
                phase = state["phase"]
                if phase == "create":
                    cycle = int(state["cycle"])
                    scene = manager.create_scene(f"ResidencySoak{cycle}")
                    manager.set_active_scene(scene)
                    model = scene.create_from_model(mesh_guid, f"SoakModel{cycle}")
                    assert model is not None
                    renderer = model.get_component("MeshRenderer")
                    assert renderer is not None
                    material = InxMaterial.create_default_lit()
                    material.set_texture_guid("texSampler", texture_guid)
                    renderer.set_material(0, material)

                    floor = scene.create_game_object(f"SoakFloor{cycle}")
                    floor.transform.position = Vector3(0.0, -0.5, 0.0)
                    floor_collider = floor.add_component("BoxCollider")
                    floor_collider.size = Vector3(20.0, 1.0, 20.0)

                    for index in range(4):
                        primitive = scene.create_primitive(
                            PrimitiveType.Cube, f"RuntimePrimitive{cycle}_{index}"
                        )
                        primitive.transform.position = Vector3(
                            float(index) * 1.25 - 1.875, 1.0 + index * 0.25, 0.0
                        )
                        rigidbody = primitive.add_component("Rigidbody")
                        rigidbody.use_gravity = False
                        primitive.add_component("BoxCollider")
                        primitive_renderer = primitive.get_component("MeshRenderer")
                        runtime_material = InxMaterial.create_default_lit()
                        runtime_material.set_texture_guid("texSampler", texture_guid)
                        primitive_renderer.set_material(0, runtime_material)

                    manager.play()
                    state["scene"] = scene
                    state["settle_frames"] = 3
                    state["phase"] = "settle"
                    return

                if phase == "settle":
                    peak_physics_body_count = max(
                        peak_physics_body_count, int(Physics.body_count)
                    )
                    remaining = int(state["settle_frames"]) - 1
                    state["settle_frames"] = remaining
                    if remaining == 0:
                        manager.stop()
                        active_scene = manager.get_active_scene()
                        if active_scene is not None:
                            manager.unload_scene(active_scene)
                        state["scene"] = None
                        state["phase"] = "empty_frame"
                    return

                gc.collect()
                physics_body_count = int(Physics.body_count)
                if physics_body_count != 0:
                    raise AssertionError(
                        f"Jolt bodies survived scene unload: {physics_body_count}"
                    )
                scene_count = int(manager.scene_count)
                if scene_count != 0:
                    raise AssertionError(f"scenes survived scene unload: {scene_count}")
                residency = engine.gpu_residency_snapshot
                runtime_records = engine.asset_runtime_records
                stale_bytes = sum(record.stale_gpu_bytes for record in runtime_records)
                if stale_bytes != 0:
                    raise AssertionError(f"stale GPU asset residency after unload: {stale_bytes}")

                rss_bytes, private_bytes = _process_memory_bytes()
                rss_samples.append(rss_bytes)
                private_samples.append(private_bytes)
                python_block_samples.append(sys.getallocatedblocks())
                python_object_samples.append(len(gc.get_objects()))
                scene_count_samples.append(scene_count)
                device_samples.append(residency["device_local_allocation_bytes"])
                allocation_count_samples.append(residency["allocator_allocation_count"])
                cpu_samples.append(sum(record.cpu_bytes for record in runtime_records))
                gpu_asset_samples.append(
                    sum(record.gpu_resident_bytes for record in runtime_records)
                )
                material_ubo_samples.append(residency["material_ubo_bytes"])
                material_render_data_samples.append(
                    residency["material_render_data_count"]
                )
                material_descriptor_samples.append(
                    residency["material_descriptor_set_count"]
                )
                retired_material_descriptor_samples.append(
                    residency["retired_material_descriptor_set_count"]
                )
                material_descriptor_pool_samples.append(
                    residency["material_descriptor_pool_count"]
                )
                material_pipeline_samples.append(residency["material_pipeline_count"])
                runtime_material_samples.append(residency["runtime_material_count"])
                asset_material_samples.append(residency["asset_material_count"])
                runtime_mesh_samples.append(residency["runtime_mesh_bytes"])
                physics_body_samples.append(physics_body_count)

                cycle = int(state["cycle"]) + 1
                state["cycle"] = cycle
                if cycle == CYCLES:
                    engine.exit()
                else:
                    state["phase"] = "create"

                if cycle % 10 == 0:
                    print(
                        f"Scene residency soak progress: {cycle}/{CYCLES}, "
                        f"rss={rss_samples[-1]}, device={device_samples[-1]}, "
                        f"private={private_samples[-1]}, pyblocks={python_block_samples[-1]}, "
                        f"allocations={allocation_count_samples[-1]}",
                        flush=True,
                    )

            callback_failures: list[BaseException] = []

            def post_draw() -> None:
                try:
                    run_cycle()
                except BaseException as exception:
                    traceback.print_exc()
                    callback_failures.append(exception)
                    engine.exit()

            engine.set_post_draw_callback(post_draw)
            engine.run()
            if callback_failures:
                raise callback_failures[0]

            _assert_process_memory_stable("RSS", rss_samples)
            _assert_process_memory_stable("private bytes", private_samples)
            _assert_stable("Python allocated blocks", python_block_samples, 1024)
            # CPython and pybind can alternate between a few equivalent GC
            # bookkeeping objects after collection. The window-growth check
            # still rejects a leak; this tolerance only permits that bounded
            # steady-state oscillation.
            _assert_stable("Python GC objects", python_object_samples, 8)
            _assert_stable("loaded scenes", scene_count_samples, 0)
            _assert_stable("device-local allocation", device_samples, 8 * MIB)
            _assert_stable("VMA allocation count", allocation_count_samples, 4)
            _assert_stable("asset CPU bytes", cpu_samples, 1 * MIB)
            _assert_stable("asset GPU bytes", gpu_asset_samples, 1 * MIB)
            _assert_stable("material UBO bytes", material_ubo_samples, 4096)
            _assert_stable("material render data", material_render_data_samples, 2)
            _assert_stable("material descriptor sets", material_descriptor_samples, 2)
            _assert_stable("retired material descriptor sets", retired_material_descriptor_samples, 0)
            _assert_stable("material descriptor pools", material_descriptor_pool_samples, 0)
            if max(retired_material_descriptor_samples) > 32:
                raise AssertionError(
                    "material descriptor retirement queue exceeded its frame-bound capacity: "
                    f"{retired_material_descriptor_samples}"
                )
            # Descriptor ownership is intentionally split into six arenas
            # (persistent, update-after-bind, frame, view, ImGui and the single
            # device-global bindless table). The soak guards against page
            # growth, not against that ownership split.
            if max(material_descriptor_pool_samples) > 6:
                raise AssertionError(
                    f"material descriptor arenas allocated extra pool pages: "
                    f"{material_descriptor_pool_samples}"
                )
            _assert_stable("material pipelines", material_pipeline_samples, 2)
            _assert_stable("runtime materials", runtime_material_samples, 2)
            _assert_stable("asset materials", asset_material_samples, 2)
            _assert_stable("runtime mesh bytes", runtime_mesh_samples, 1 * MIB)
            if any(physics_body_samples):
                raise AssertionError(
                    f"physics body count did not return to zero: {physics_body_samples}"
                )
            if peak_physics_body_count < 5:
                raise AssertionError(
                    f"mixed soak never created the expected Jolt bodies: {peak_physics_body_count}"
                )

            print(
                "Scene residency soak passed: "
                f"cycles={CYCLES}, rss_tail={rss_samples[-10:]}, "
                f"private_tail={private_samples[-10:]}, "
                f"pyblocks_tail={python_block_samples[-10:]}, "
                f"pyobjects_tail={python_object_samples[-10:]}, "
                f"device_tail={device_samples[-10:]}, "
                f"alloc_count_tail={allocation_count_samples[-10:]}, "
                f"cpu_tail={cpu_samples[-10:]}, gpu_tail={gpu_asset_samples[-10:]}, "
                f"material_ubo_tail={material_ubo_samples[-10:]}, "
                f"material_data_tail={material_render_data_samples[-10:]}, "
                f"descriptor_tail={material_descriptor_samples[-10:]}, "
                f"retired_descriptor_tail={retired_material_descriptor_samples[-10:]}, "
                f"descriptor_pool_tail={material_descriptor_pool_samples[-10:]}, "
                f"pipeline_tail={material_pipeline_samples[-10:]}, "
                f"runtime_material_tail={runtime_material_samples[-10:]}, "
                f"asset_material_tail={asset_material_samples[-10:]}, "
                f"runtime_mesh_tail={runtime_mesh_samples[-10:]}, "
                f"peak_physics_bodies={peak_physics_body_count}"
            )
            return 0
        finally:
            engine.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
