"""Isolated real-Vulkan smoke test for mesh and texture GPU upload tickets."""

from __future__ import annotations

import tempfile
import sys
import math
import struct
import threading
import numpy as np
from pathlib import Path

from Infernux import Engine
from Infernux.engine.path_utils import resolved_path
from Infernux.lib import (
    ImageReadbackStatus,
    InxMaterial,
    PrimitiveType,
    SceneManager,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="infernux-gpu-upload-") as root:
        project = Path(root)
        (project / "Assets").mkdir()
        (project / "ProjectSettings").mkdir()
        texture_path = project / "Assets" / "GpuUploadProbe.ppm"
        texture_bytes = (
            b"P6\n4 2\n255\n"
            + bytes(
                (
                    255,
                    0,
                    0,
                    0,
                    255,
                    0,
                    0,
                    0,
                    255,
                    255,
                    255,
                    255,
                    255,
                    255,
                    0,
                    0,
                    255,
                    255,
                    255,
                    0,
                    255,
                    64,
                    64,
                    64,
                )
            )
        )
        texture_path.write_bytes(texture_bytes)
        second_texture_path = project / "Assets" / "GpuUploadProbeSecond.ppm"
        second_texture_bytes = bytearray(texture_bytes)
        second_texture_bytes[-1] ^= 0x7F
        second_texture_path.write_bytes(second_texture_bytes)
        skinned_source = project / "Assets" / "GpuSkinnedProbe.fbx"
        skinned_fixture = (
            Path(resolved_path(__file__)).parents[3]
            / "external"
            / "assimp"
            / "test"
            / "models"
            / "FBX"
            / "animation_with_skeleton.fbx"
        )
        skinned_source.write_bytes(skinned_fixture.read_bytes())
        lit_shader_path = Path(resolved_path(__file__)).parents[1] / "resources" / "shaders" / "lit.frag"
        assert lit_shader_path.is_file()

        frontend = Engine()
        engine = frontend.get_native_engine()
        shutdown_readback = None
        try:
            try:
                frontend.init_renderer(64, 64, str(project))
            except (OSError, RuntimeError) as exception:
                print(f"GPU upload ticket smoke test skipped: {exception}")
                return 77
            scene = SceneManager.instance().get_active_scene()
            assert scene is not None
            frontend.resize_scene_render_target(32, 24)
            wrong_thread_errors: list[BaseException] = []

            def request_from_worker() -> None:
                try:
                    frontend.request_render_target_readback(False)
                except BaseException as exception:
                    wrong_thread_errors.append(exception)

            worker = threading.Thread(target=request_from_worker)
            worker.start()
            worker.join()
            assert len(wrong_thread_errors) == 1
            assert isinstance(wrong_thread_errors[0], RuntimeError)
            assert "owner thread" in str(wrong_thread_errors[0])
            probe = scene.create_primitive(PrimitiveType.Cube, "GpuUploadProbe")
            disposable_probe = scene.create_primitive(PrimitiveType.Sphere, "DisposableGpuMeshProbe")
            skinned_guid = frontend.get_asset_database().get_guid_from_path(str(skinned_source))
            assert skinned_guid
            skinned_probe = scene.create_from_model(skinned_guid, "GpuSkinnedProbe")
            assert skinned_probe is not None
            renderer = probe.get_component("MeshRenderer")
            assert renderer is not None
            texture_guid = frontend.get_asset_database().get_guid_from_path(str(texture_path))
            assert texture_guid
            second_texture_guid = frontend.get_asset_database().get_guid_from_path(str(second_texture_path))
            assert second_texture_guid
            material = InxMaterial.create_default_lit()
            material.set_texture_guid("texSampler", texture_guid)
            renderer.set_material(0, material)

            try:
                engine.submit_particle_instances(701, np.zeros((2, 8), dtype=np.float32))
                raise AssertionError("invalid particle instance shape was accepted")
            except ValueError:
                pass
            try:
                non_contiguous_particles = np.zeros((3, 18), dtype=np.float32)[:, ::2]
                engine.submit_particle_instances(701, non_contiguous_particles)
                raise AssertionError("non-contiguous particle instances were accepted")
            except ValueError:
                pass

            particle_instances = np.array(
                [
                    [-0.7, 0.2, 0.0, 0.5, 1.0, 0.1, 0.1, 0.9, 0.0],
                    [0.0, 0.4, 0.0, 0.6, 0.1, 1.0, 0.2, 0.8, 0.2],
                    [0.7, 0.2, 0.0, 0.5, 0.2, 0.4, 1.0, 0.9, -0.2],
                ],
                dtype=np.float32,
            )
            non_finite_particles = particle_instances.copy()
            non_finite_particles[0, 0] = np.nan
            try:
                engine.submit_particle_instances(701, non_finite_particles)
                raise AssertionError("non-finite particle instances were accepted")
            except ValueError:
                pass
            negative_size_particles = particle_instances.copy()
            negative_size_particles[0, 3] = -1.0
            try:
                engine.submit_particle_instances(701, negative_size_particles)
                raise AssertionError("negative particle size was accepted")
            except ValueError:
                pass
            try:
                engine.submit_particle_instances(701, particle_instances, "missing-particle-material")
                raise AssertionError("missing particle material GUID was accepted")
            except ValueError:
                pass
            engine.submit_particle_instances(701, particle_instances)
            assert engine.gpu_residency_snapshot["particle_bytes"] == 3 * 64

            gui_pixels = bytes(
                channel
                for y in range(8)
                for x in range(8)
                for channel in ((x * 31) & 0xFF, (y * 31) & 0xFF, 127, 255)
            )
            try:
                engine.submit_imgui_texture("invalid-byte-count", b"\x00\x01\x02", 8, 8)
                raise AssertionError("invalid RGBA8 byte count was accepted")
            except ValueError:
                pass
            try:
                engine.set_imgui_texture_budget_bytes(0)
                raise AssertionError("zero ImGui texture budget was accepted")
            except ValueError:
                pass
            try:
                engine.set_gpu_residency_budget_bytes(0)
                raise AssertionError("zero total GPU residency budget was accepted")
            except ValueError:
                pass
            pinned_gui_version = engine.submit_imgui_texture(
                "gpu-smoke-pinned", gui_pixels, 8, 8, pinned=True
            )
            evictable_gui_version = engine.submit_imgui_texture(
                "gpu-smoke-evictable", gui_pixels, 8, 8
            )
            assert pinned_gui_version > 0
            assert evictable_gui_version > 0
            assert engine.query_or_schedule_texture_preview(
                "gpu-smoke-preview-job", str(texture_path), 1, pump=False
            ) == (0, 0, 0)
            material_preview_json = material.serialize()
            assert (
                engine.query_or_schedule_material_preview(
                    "gpu-smoke-material-preview", "", material_preview_json, 1
                )
                == 0
            )
            assert (
                engine.query_or_schedule_mesh_preview(
                    "gpu-smoke-mesh-preview", str(skinned_source), 1
                )
                == 0
            )
            assert (
                engine.render_timeline_cube_preview(
                    0.0, 0.0, 0.0, 0.0, 20.0, 0.0, 1.0, 1.0, 1.0, 0.7, -0.35, 5.0, 96
                )
                == 0
            )

            frames = 0
            switched_texture = False
            budget_applied_frame: int | None = None
            eviction_count_before_budget = 0
            cache_entries_before_budget = 0
            resident_bytes_before_budget = 0
            mesh_eviction_count_before_budget = 0
            mesh_cache_entries_before_budget = 0
            mesh_resident_bytes_before_budget = 0
            replacement_gui_version = 0
            first_gui_texture_id = 0
            imgui_budget_applied_frame: int | None = None
            imgui_eviction_count_before_budget = 0
            imgui_uploads_complete = False
            preview_job_complete = False
            readback_ticket = None
            cancelled_readback_ticket = None
            readback_complete = False
            observed_timeline_publication_in_flight = False
            texture_reload_requested = False
            texture_reload_wait_idle_count = 0
            shader_reload_requested = False
            shader_reload_wait_idle_count = 0
            shader_retirement_count_before_reload = 0
            timeline_cube_preview_complete = False
            observed_async_graphics_in_flight = False
            material_preview_complete = False
            mesh_preview_complete = False
            observed_thumbnail_readback_in_flight = False

            def finish_after_uploads() -> None:
                nonlocal frames, switched_texture, budget_applied_frame
                nonlocal eviction_count_before_budget, cache_entries_before_budget
                nonlocal resident_bytes_before_budget
                nonlocal mesh_eviction_count_before_budget, mesh_cache_entries_before_budget
                nonlocal mesh_resident_bytes_before_budget
                nonlocal replacement_gui_version, first_gui_texture_id
                nonlocal imgui_budget_applied_frame, imgui_eviction_count_before_budget
                nonlocal imgui_uploads_complete
                nonlocal preview_job_complete
                nonlocal readback_ticket, cancelled_readback_ticket, readback_complete
                nonlocal observed_timeline_publication_in_flight
                nonlocal texture_reload_requested, texture_reload_wait_idle_count
                nonlocal shader_reload_requested, shader_reload_wait_idle_count
                nonlocal shader_retirement_count_before_reload
                nonlocal timeline_cube_preview_complete, observed_async_graphics_in_flight
                nonlocal material_preview_complete, mesh_preview_complete
                nonlocal observed_thumbnail_readback_in_flight
                frames += 1
                engine.pump_preview_tasks()
                transfer_residency = engine.gpu_residency_snapshot
                if (
                    transfer_residency["upload_timeline_enabled"]
                    and transfer_residency["timeline_upload_publication_count"] > 0
                    and transfer_residency["pending_gpu_transfer_count"] > 0
                ):
                    observed_timeline_publication_in_flight = True
                if transfer_residency["pending_async_graphics_submission_count"] > 0:
                    observed_async_graphics_in_flight = True
                if frames < 3 and transfer_residency["pending_readback_count"] > 0:
                    observed_thumbnail_readback_in_flight = True
                material_preview_complete = (
                    material_preview_complete
                    or engine.query_or_schedule_material_preview(
                        "gpu-smoke-material-preview", "", material_preview_json, 1
                    )
                    != 0
                )
                mesh_preview_complete = (
                    mesh_preview_complete
                    or engine.query_or_schedule_mesh_preview(
                        "gpu-smoke-mesh-preview", str(skinned_source), 1
                    )
                    != 0
                )
                timeline_cube_preview_complete = (
                    timeline_cube_preview_complete
                    or engine.render_timeline_cube_preview(
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        20.0,
                        0.0,
                        1.0,
                        1.0,
                        1.0,
                        0.7,
                        -0.35,
                        5.0,
                        96,
                    )
                    != 0
                )
                preview_job_complete = (
                    preview_job_complete
                    or engine.get_texture_preview_texture_id("gpu-smoke-preview-job") != 0
                )

                if frames == 3:
                    readback_ticket = frontend.request_render_target_readback(False)
                    cancelled_readback_ticket = frontend.request_render_target_readback(False)
                    cancelled_readback_ticket.cancel()
                    assert not readback_ticket.done
                    assert readback_ticket.status == ImageReadbackStatus.Pending
                    try:
                        readback_ticket.result_bytes()
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("pending GPU readback exposed result bytes")
                    residency = engine.gpu_residency_snapshot
                    assert residency["pending_readback_count"] == 2, residency
                    assert residency["pending_readback_bytes"] == 2 * 32 * 24 * 4 * 2, residency
                elif readback_ticket is not None and readback_ticket.done and not readback_complete:
                    assert readback_ticket.status == ImageReadbackStatus.Completed
                    assert readback_ticket.width == 32
                    assert readback_ticket.height == 24
                    assert readback_ticket.channel_count == 4
                    assert readback_ticket.element_type == "float16"
                    assert readback_ticket.byte_size == 32 * 24 * 4 * 2
                    readback_bytes = readback_ticket.result_bytes()
                    assert len(readback_bytes) == readback_ticket.byte_size
                    readback_values = [value[0] for value in struct.iter_unpack("<e", readback_bytes)]
                    assert all(math.isfinite(value) for value in readback_values)
                    assert any(value != 0.0 for value in readback_values)
                    readback_array = readback_ticket.result_numpy()
                    assert readback_array.shape == (24, 32, 4)
                    assert readback_array.dtype == np.float16
                    assert readback_array.flags.c_contiguous
                    assert not readback_array.flags.writeable
                    assert readback_array.tobytes() == readback_bytes
                    assert cancelled_readback_ticket.status == ImageReadbackStatus.Cancelled
                    readback_complete = True

                if (
                    replacement_gui_version == 0
                    and engine.get_imgui_texture_version("gpu-smoke-pinned") >= pinned_gui_version
                    and engine.get_imgui_texture_version("gpu-smoke-evictable") >= evictable_gui_version
                ):
                    first_gui_texture_id = engine.get_imgui_texture_id("gpu-smoke-evictable")
                    assert first_gui_texture_id != 0
                    replacement_pixels = bytearray(gui_pixels)
                    replacement_pixels[0] ^= 0xFF
                    replacement_gui_version = engine.submit_imgui_texture(
                        "gpu-smoke-evictable", replacement_pixels, 8, 8
                    )
                    assert replacement_gui_version > evictable_gui_version
                elif (
                    replacement_gui_version > 0
                    and imgui_budget_applied_frame is None
                    and preview_job_complete
                    and material_preview_complete
                    and mesh_preview_complete
                    and engine.get_imgui_texture_version("gpu-smoke-evictable") >= replacement_gui_version
                ):
                    assert engine.get_imgui_texture_id("gpu-smoke-evictable") != first_gui_texture_id
                    imgui_eviction_count_before_budget = engine.imgui_texture_eviction_count
                    engine.set_imgui_texture_budget_bytes(1)
                    imgui_budget_applied_frame = frames
                elif (
                    imgui_budget_applied_frame is not None
                    and frames >= imgui_budget_applied_frame + 10
                    and engine.get_imgui_texture_id("gpu-smoke-evictable") == 0
                    and engine.get_imgui_texture_id("gpu-smoke-pinned") != 0
                    and engine.imgui_texture_eviction_count > imgui_eviction_count_before_budget
                ):
                    imgui_uploads_complete = True

                submitted = engine.submitted_mesh_gpu_upload_count
                completed = engine.completed_mesh_gpu_upload_count
                texture_submitted = engine.submitted_texture_gpu_upload_count
                texture_completed = engine.completed_texture_gpu_upload_count
                uploads_complete = (
                    submitted > 0
                    and completed == submitted
                    and engine.pending_mesh_gpu_upload_count == 0
                    and texture_submitted > 0
                    and texture_completed == texture_submitted
                    and engine.pending_texture_cpu_load_count == 0
                    and engine.pending_texture_gpu_upload_count == 0
                )
                if uploads_complete and not switched_texture:
                    material.set_texture_guid("texSampler", second_texture_guid)
                    switched_texture = True
                elif (
                    uploads_complete
                    and switched_texture
                    and texture_submitted >= 2
                    and engine.staging_buffer_reuse_count > 0
                    and not texture_reload_requested
                ):
                    texture_reload_wait_idle_count = engine.gpu_residency_snapshot["device_wait_idle_count"]
                    engine.reload_texture(str(second_texture_path))
                    assert (
                        engine.gpu_residency_snapshot["device_wait_idle_count"]
                        == texture_reload_wait_idle_count
                    )
                    texture_reload_requested = True
                elif (
                    uploads_complete
                    and switched_texture
                    and texture_reload_requested
                    and texture_submitted >= 3
                    and engine.staging_buffer_reuse_count > 0
                    and not shader_reload_requested
                ):
                    shader_residency = engine.gpu_residency_snapshot
                    shader_reload_wait_idle_count = shader_residency["device_wait_idle_count"]
                    shader_retirement_count_before_reload = shader_residency[
                        "shader_hot_reload_retirement_count"
                    ]
                    error = engine.reload_shader_runtime(str(lit_shader_path), "lit")
                    assert error == "", error
                    shader_residency = engine.gpu_residency_snapshot
                    assert shader_residency["device_wait_idle_count"] == shader_reload_wait_idle_count
                    assert (
                        shader_residency["shader_hot_reload_retirement_count"]
                        > shader_retirement_count_before_reload
                    )
                    shader_reload_requested = True
                elif (
                    uploads_complete
                    and switched_texture
                    and texture_reload_requested
                    and shader_reload_requested
                    and texture_submitted >= 3
                    and engine.staging_buffer_reuse_count > 0
                    and material_preview_complete
                    and mesh_preview_complete
                    and budget_applied_frame is None
                ):
                    eviction_count_before_budget = engine.texture_gpu_eviction_count
                    cache_entries_before_budget = engine.texture_gpu_cache_entry_count
                    resident_bytes_before_budget = engine.texture_gpu_resident_bytes
                    mesh_eviction_count_before_budget = engine.mesh_gpu_eviction_count
                    mesh_cache_entries_before_budget = engine.mesh_gpu_cache_entry_count
                    mesh_resident_bytes_before_budget = engine.mesh_gpu_resident_bytes
                    scene.destroy_game_object(disposable_probe)
                    engine.set_gpu_residency_budget_bytes(1)
                    engine.set_texture_gpu_budget_bytes(1)
                    engine.set_mesh_gpu_budget_bytes(1)
                    budget_applied_frame = frames
                elif (
                    budget_applied_frame is not None
                    and frames >= budget_applied_frame + 3
                    and engine.texture_gpu_eviction_count > eviction_count_before_budget
                    and engine.texture_gpu_cache_entry_count < cache_entries_before_budget
                    and engine.mesh_gpu_eviction_count > mesh_eviction_count_before_budget
                    and engine.mesh_gpu_cache_entry_count < mesh_cache_entries_before_budget
                    and imgui_uploads_complete
                    and readback_complete
                    and material_preview_complete
                    and mesh_preview_complete
                ):
                    engine.exit()
                elif frames >= 240:
                    engine.exit()

            engine.set_post_draw_callback(finish_after_uploads)
            engine.run()

            diagnostics = (
                f"frames={frames}, submitted={engine.submitted_mesh_gpu_upload_count}, "
                f"completed={engine.completed_mesh_gpu_upload_count}, "
                f"pending={engine.pending_mesh_gpu_upload_count}, async={engine.async_mesh_gpu_upload_count}, "
                f"texture_submitted={engine.submitted_texture_gpu_upload_count}, "
                f"texture_completed={engine.completed_texture_gpu_upload_count}, "
                f"texture_cpu_pending={engine.pending_texture_cpu_load_count}, "
                f"texture_gpu_pending={engine.pending_texture_gpu_upload_count}, "
                f"texture_async={engine.async_texture_gpu_upload_count}, "
                f"staging_pool_bytes={engine.staging_pool_bytes}, "
                f"staging_pool_buffers={engine.staging_pool_buffer_count}, "
                f"staging_allocations={engine.staging_buffer_allocation_count}, "
                f"staging_reuses={engine.staging_buffer_reuse_count}, "
                f"staging_discards={engine.staging_buffer_discard_count}, "
                f"imgui_submitted={engine.submitted_imgui_texture_upload_count}, "
                f"imgui_completed={engine.completed_imgui_texture_upload_count}, "
                f"imgui_pending={engine.pending_imgui_texture_upload_count}, "
                f"imgui_pending_bytes={engine.pending_imgui_texture_upload_bytes}, "
                f"imgui_async={engine.async_imgui_texture_upload_count}, "
                f"imgui_resident={engine.imgui_texture_resident_bytes}, "
                f"imgui_budget={engine.imgui_texture_budget_bytes}, "
                f"imgui_entries={engine.imgui_texture_entry_count}, "
                f"imgui_evictions={engine.imgui_texture_eviction_count}, "
                f"preview_job_complete={preview_job_complete}, "
                f"readback_complete={readback_complete}, "
                f"material_preview_complete={material_preview_complete}, "
                f"mesh_preview_complete={mesh_preview_complete}, "
                f"thumbnail_readback_in_flight={observed_thumbnail_readback_in_flight}, "
                f"pending_readbacks={engine.gpu_residency_snapshot['pending_readback_count']}, "
                f"pending_graphics={engine.gpu_residency_snapshot['pending_async_graphics_submission_count']}, "
                f"particle_bytes={engine.gpu_residency_snapshot['particle_bytes']}, "
                f"texture_gpu_resident={engine.texture_gpu_resident_bytes}, "
                f"texture_gpu_budget={engine.texture_gpu_budget_bytes}, "
                f"texture_gpu_entries={engine.texture_gpu_cache_entry_count}, "
                f"texture_gpu_retired_leases={engine.retired_texture_gpu_lease_count}, "
                f"texture_gpu_evictions={engine.texture_gpu_eviction_count}, "
                f"budget_applied_frame={budget_applied_frame}, "
                f"resident_before_budget={resident_bytes_before_budget}, "
                f"entries_before_budget={cache_entries_before_budget}"
                f", mesh_gpu_resident={engine.mesh_gpu_resident_bytes}, "
                f"mesh_gpu_budget={engine.mesh_gpu_budget_bytes}, "
                f"mesh_gpu_entries={engine.mesh_gpu_cache_entry_count}, "
                f"mesh_gpu_retired_leases={engine.retired_mesh_gpu_lease_count}, "
                f"mesh_gpu_evictions={engine.mesh_gpu_eviction_count}, "
                f"mesh_resident_before_budget={mesh_resident_bytes_before_budget}, "
                f"mesh_entries_before_budget={mesh_cache_entries_before_budget}"
            )
            assert frames < 240, diagnostics
            assert engine.submitted_mesh_gpu_upload_count >= 3
            assert engine.completed_mesh_gpu_upload_count == engine.submitted_mesh_gpu_upload_count
            assert engine.pending_mesh_gpu_upload_count == 0
            assert engine.submitted_texture_gpu_upload_count >= 3
            assert engine.completed_texture_gpu_upload_count == engine.submitted_texture_gpu_upload_count
            assert engine.pending_texture_cpu_load_count == 0
            assert engine.pending_texture_gpu_upload_count == 0
            assert engine.staging_pool_bytes <= 64 * 1024 * 1024
            assert engine.staging_pool_buffer_count > 0
            assert engine.staging_buffer_reuse_count > 0
            assert replacement_gui_version > evictable_gui_version, diagnostics
            assert preview_job_complete, diagnostics
            assert timeline_cube_preview_complete, diagnostics
            assert observed_async_graphics_in_flight, diagnostics
            assert material_preview_complete, diagnostics
            assert mesh_preview_complete, diagnostics
            assert observed_thumbnail_readback_in_flight, diagnostics
            assert readback_complete, diagnostics
            assert imgui_uploads_complete, diagnostics
            assert engine.submitted_imgui_texture_upload_count >= 3, diagnostics
            assert (
                engine.completed_imgui_texture_upload_count
                == engine.submitted_imgui_texture_upload_count
            ), diagnostics
            assert engine.pending_imgui_texture_upload_count == 0, diagnostics
            assert engine.pending_imgui_texture_upload_bytes == 0, diagnostics
            assert engine.imgui_texture_budget_bytes == 1, diagnostics
            assert engine.get_imgui_texture_id("gpu-smoke-evictable") == 0, diagnostics
            assert engine.get_imgui_texture_id("gpu-smoke-pinned") != 0, diagnostics
            assert budget_applied_frame is not None, diagnostics
            assert resident_bytes_before_budget > 0, diagnostics
            assert cache_entries_before_budget >= 4, diagnostics
            assert engine.texture_gpu_budget_bytes == 1, diagnostics
            assert engine.texture_gpu_resident_bytes > engine.texture_gpu_budget_bytes, diagnostics
            assert engine.texture_gpu_cache_entry_count < cache_entries_before_budget, diagnostics
            assert engine.texture_gpu_eviction_count > eviction_count_before_budget, diagnostics
            assert mesh_resident_bytes_before_budget > 0, diagnostics
            assert mesh_cache_entries_before_budget >= 3, diagnostics
            assert engine.mesh_gpu_budget_bytes == 1, diagnostics
            assert engine.mesh_gpu_resident_bytes > engine.mesh_gpu_budget_bytes, diagnostics
            assert engine.mesh_gpu_cache_entry_count < mesh_cache_entries_before_budget, diagnostics
            assert engine.mesh_gpu_eviction_count > mesh_eviction_count_before_budget, diagnostics
            residency = engine.gpu_residency_snapshot
            assert texture_reload_requested, diagnostics
            assert shader_reload_requested, diagnostics
            assert residency["device_wait_idle_count"] == texture_reload_wait_idle_count, residency
            assert residency["device_wait_idle_count"] == shader_reload_wait_idle_count, residency
            assert (
                residency["shader_hot_reload_retirement_count"]
                > shader_retirement_count_before_reload
            ), residency
            assert residency["pending_readback_count"] == 0, residency
            assert residency["pending_readback_bytes"] == 0, residency
            assert residency["pending_gpu_transfer_count"] == 0, residency
            assert residency["pending_async_graphics_submission_count"] == 0, residency
            assert residency["async_graphics_submission_count"] > 0, residency
            if residency["upload_timeline_enabled"]:
                assert residency["timeline_upload_publication_count"] > 0, residency
                assert residency["required_upload_timeline_value"] > 0, residency
                assert observed_timeline_publication_in_flight, residency
            else:
                assert residency["timeline_upload_publication_count"] == 0, residency
                assert residency["required_upload_timeline_value"] == 0, residency
            assert engine.gpu_residency_budget_bytes == 1, residency
            assert residency["budget_bytes"] == 1, residency
            assert residency["allocator_allocation_bytes"] > 0, residency
            assert residency["allocator_block_bytes"] >= residency["allocator_allocation_bytes"], residency
            assert residency["allocator_allocation_count"] > 0, residency
            assert residency["device_local_allocation_bytes"] > 0, residency
            assert residency["device_local_budget_bytes"] > 0, residency
            assert residency["render_target_bytes"] > 0, residency
            assert residency["particle_bytes"] == 3 * 64, residency
            assert residency["tracked_bytes"] >= residency["render_target_bytes"], residency
            assert residency["effective_allocation_bytes"] <= residency["allocator_allocation_bytes"], residency
            assert residency["over_budget_bytes"] > 0, residency
            engine.trim_gpu_residency_budget()
            runtime_records = engine.asset_runtime_records
            current_gpu_records = [
                record
                for record in runtime_records
                if record.gpu_resident_bytes > 0 or record.gpu_pending_bytes > 0
            ]
            assert current_gpu_records, runtime_records
            assert all(record.runtime_version > 0 for record in current_gpu_records), runtime_records
            assert all(record.gpu_version_synchronized for record in current_gpu_records), runtime_records
            assert all(record.stale_gpu_bytes == 0 for record in current_gpu_records), runtime_records
            shutdown_readback = frontend.request_render_target_readback(False)
            assert not shutdown_readback.done
            assert engine.schedule_texture_preview_from_memory(
                "gpu-smoke-shutdown-drain", texture_bytes, 1
            )
            engine.remove_particle_batch(701)
            assert engine.gpu_residency_snapshot["particle_bytes"] == 0
        finally:
            engine.cleanup()
        assert shutdown_readback is not None
        assert shutdown_readback.status == ImageReadbackStatus.Completed
        assert len(shutdown_readback.result_bytes()) == shutdown_readback.byte_size

    print("GPU upload ticket smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
