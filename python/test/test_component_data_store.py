from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from Infernux import lib
from Infernux.batch import batch_read, batch_write, create_batch_handle, create_scene_batch_handle
from Infernux.components import InxComponent
from Infernux.components._cds_bridge import get_class_id


def test_cds_generational_handles_reject_stale_access():
    class_id = lib._cds_register_class("python.tests:GenerationalHandle")
    field_id = lib._cds_register_field(class_id, "value", 0)
    first = lib._cds_alloc(class_id)
    assert len(first) == 2
    lib._cds_set(class_id, field_id, first, 0, 4.5)
    assert lib._cds_get(class_id, field_id, first, 0) == pytest.approx(4.5)
    assert lib._cds_is_alive(class_id, first) is True

    lib._cds_free(class_id, first)
    assert lib._cds_is_alive(class_id, first) is False
    replacement = lib._cds_alloc(class_id)
    assert replacement[0] == first[0]
    assert replacement[1] != first[1]
    with pytest.raises(RuntimeError, match="stale or invalid"):
        lib._cds_get(class_id, field_id, first, 0)
    with pytest.raises(RuntimeError, match="stale or invalid"):
        lib._cds_free(class_id, first)
    lib._cds_free(class_id, replacement)


def test_cds_batch_validates_handle_and_data_shapes():
    class_id = lib._cds_register_class("python.tests:BatchValidation")
    field_id = lib._cds_register_field(class_id, "position", 4)
    handles = [lib._cds_alloc(class_id), lib._cds_alloc(class_id)]
    handle_array = np.asarray(handles, dtype=np.uint32)
    values = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)

    lib._cds_batch_scatter(class_id, field_id, 4, handle_array, values)
    np.testing.assert_array_equal(
        lib._cds_batch_gather(class_id, field_id, 4, handle_array), values
    )

    with pytest.raises(ValueError, match=r"shape \(N, 2\)"):
        lib._cds_batch_gather(
            class_id, field_id, 4, np.asarray([handles[0][0]], dtype=np.uint32)
        )
    with pytest.raises(ValueError, match=r"shape \(N, 3\)"):
        lib._cds_batch_scatter(
            class_id, field_id, 4, handle_array, np.zeros((2, 2), dtype=np.float32)
        )
    with pytest.raises(ValueError, match="field type mismatch"):
        lib._cds_batch_gather(class_id, field_id, 0, handle_array)

    for handle in handles:
        lib._cds_free(class_id, handle)


def test_transform_batch_handle_rejects_destroyed_transform(scene):
    game_object = scene.create_game_object("batch_handle_target")
    handle = create_batch_handle([game_object.transform])
    assert batch_read(handle, "local_position").shape == (1, 3)

    scene.destroy_game_object(game_object)
    scene.process_pending_destroys()
    with pytest.raises(RuntimeError, match="stale transform"):
        batch_read(handle, "local_position")


def test_transform_batch_handle_compacts_stale_transforms_with_mask(scene):
    first = scene.create_game_object("compact_stale")
    second = scene.create_game_object("compact_live")
    handle = create_batch_handle([first.transform, second.transform], mode="compact")

    scene.destroy_game_object(first)
    scene.process_pending_destroys()

    values, mask = batch_read(handle, "local_position")
    assert values.shape == (1, 3)
    np.testing.assert_array_equal(mask, np.asarray([False, True]))

    write_mask = batch_write(
        handle,
        np.asarray([[90.0, 90.0, 90.0], [4.0, 5.0, 6.0]], dtype=np.float32),
        "local_position",
    )
    np.testing.assert_array_equal(write_mask, mask)
    np.testing.assert_allclose(
        batch_read([second.transform], "local_position"),
        np.asarray([[4.0, 5.0, 6.0]], dtype=np.float32),
    )

    with pytest.raises(ValueError, match="mode must be"):
        create_batch_handle([second.transform], mode="lenient")


def test_scene_batch_handle_filters_in_native_code(scene):
    first = scene.create_game_object("WaveCube_0")
    second = scene.create_game_object("Other")
    third = scene.create_game_object("WaveCube_1")
    first.transform.position = lib.Vector3(1.0, 2.0, 3.0)
    second.transform.position = lib.Vector3(4.0, 5.0, 6.0)
    third.transform.position = lib.Vector3(7.0, 8.0, 9.0)

    handle = create_scene_batch_handle(scene, name_prefix="WaveCube_")
    positions = batch_read(handle, "position")

    assert len(handle) == 2
    np.testing.assert_allclose(
        positions,
        np.asarray([[1.0, 2.0, 3.0], [7.0, 8.0, 9.0]], dtype=np.float32),
    )


def test_transform_store_bumps_serial_when_a_world_matrix_becomes_dirty():
    source = (
        Path(__file__).resolve().parents[2]
        / "cpp/infernux/function/scene/TransformECSStore.cpp"
    ).read_text(encoding="utf-8")
    marker = source.index("void TransformECSStore::MarkWorldMatrixDirty")
    body = source[marker : source.index("void TransformECSStore::SetCachedWorldPosition")]
    assert "newlyDirty" in body
    assert "++m_globalTransformSerial" in body


def test_transform_batch_write_bumps_global_transform_serial(scene):
    first = scene.create_game_object("serial_a")
    second = scene.create_game_object("serial_b")
    manager = lib.SceneManager.instance()
    before = int(manager.get_global_transform_serial())

    batch_write(
        [first.transform, second.transform],
        np.asarray([[0.0, 15.0, 0.0], [0.0, 45.0, 0.0]], dtype=np.float32),
        "local_euler_angles",
    )

    after = int(manager.get_global_transform_serial())
    assert after > before
    np.testing.assert_allclose(
        batch_read([first.transform, second.transform], "local_euler_angles"),
        np.asarray([[0.0, 15.0, 0.0], [0.0, 45.0, 0.0]], dtype=np.float32),
        atol=1e-4,
    )


def test_component_class_can_reserve_numeric_storage():
    class ReservedComponent(InxComponent):
        value: float = 0.0

    ReservedComponent.reserve_instances(257)
    class_id = get_class_id(ReservedComponent)
    assert lib._cds_capacity(class_id) >= 257
    assert lib._cds_alive_count(class_id) == 0

    with pytest.raises(ValueError, match="non-negative integer"):
        ReservedComponent.reserve_instances(-1)


def test_component_cleanup_tolerates_an_already_released_slot():
    class CleanupComponent(InxComponent):
        value: float = 1.0

    component = CleanupComponent()
    class_id = component._cds_class_id
    slot = component._cds_slot
    assert class_id is not None and slot is not None

    lib._cds_free(class_id, slot)
    component._call_on_destroy()

    assert component._cds_slot is None
    assert component._cds_class_id is None


def test_script_replacement_detach_releases_the_old_numeric_slot():
    class ReplacedComponent(InxComponent):
        value: float = 1.0

    component = ReplacedComponent()
    class_id = component._cds_class_id
    slot = component._cds_slot
    assert class_id is not None and slot is not None
    assert lib._cds_is_alive(class_id, slot) is True

    component._detach_native_binding_for_replacement()

    assert component._cds_slot is None
    assert lib._cds_is_alive(class_id, slot) is False


def test_reserve_rejects_component_without_numeric_storage():
    class TextOnlyComponent(InxComponent):
        label: str = "text"

    with pytest.raises(TypeError, match="no CDS-backed numeric fields"):
        TextOnlyComponent.reserve_instances(10)


def test_component_layout_revision_gets_distinct_storage():
    class ReloadedComponent(InxComponent):
        value: float = 0.0

    first_class_id = get_class_id(ReloadedComponent)

    class ReloadedComponent(InxComponent):
        value: int = 0

    second_class_id = get_class_id(ReloadedComponent)
    assert first_class_id != second_class_id
