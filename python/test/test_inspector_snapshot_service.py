from __future__ import annotations

from pathlib import Path

from Infernux.engine.runtime_change_journal import (
    RuntimeChangeDomain,
    RuntimeChangeJournal,
    RuntimeChangeSet,
)
from Infernux.engine.ui.inspector_snapshot import (
    InspectorSnapshotService,
    InspectorTarget,
    invalidate_rebuilt_scene,
    invalidate_scene_transforms,
    sync_selected_transforms_from_native_serial,
    target_for_component,
)


class _Owner:
    def __init__(self, object_id: int) -> None:
        self.id = object_id


class _Component:
    def __init__(self, object_id: int, component_id: int) -> None:
        self.game_object = _Owner(object_id)
        self.component_id = component_id
        self.speed = 1.0


class _LifecycleCheckedComponent:
    def __init__(self, object_id: int | None, component_id: int = 919) -> None:
        self._game_object = _Owner(object_id) if object_id is not None else None
        self._component_id = component_id

    @property
    def game_object(self):
        raise RuntimeError("public lifecycle accessor must not be evaluated")

    @property
    def component_id(self):
        raise RuntimeError("public component id accessor must not be evaluated")


def _service() -> InspectorSnapshotService:
    service = InspectorSnapshotService.instance()
    service.reset_for_tests()
    return service


def test_component_target_uses_passive_owner_during_unbind_windows() -> None:
    service = _service()
    live = _LifecycleCheckedComponent(73)
    assert target_for_component(live) == (
        InspectorTarget.scene_object(73)
    )
    assert service.component_snapshot(live).target == InspectorTarget.scene_object(73)
    assert target_for_component(_LifecycleCheckedComponent(None)) == InspectorTarget.none()


def test_retired_component_renderer_never_reads_lifecycle_properties() -> None:
    from Infernux.engine.ui import inspector_components as components_ui

    class _Retired:
        def __init__(self) -> None:
            self._is_builtin_component_wrapper = True
            self._cpp_component = None
            self._component_id = 920

        @property
        def type_name(self):
            raise RuntimeError("retired type getter must not be evaluated")

        @property
        def component_id(self):
            raise RuntimeError("retired id getter must not be evaluated")

    retired = _Retired()
    assert components_ui._get_component_cache_id(retired) == 920
    components_ui.render_component(None, retired)


def test_unregistered_component_field_does_not_fall_back_to_global_refresh() -> None:
    service = _service()
    unrelated = InspectorTarget.scene_object(88)
    unrelated_before = service.snapshot(unrelated)

    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("late-component", start_at_current=False)
    journal.publish_component_field("Mover", 909, "speed")
    service.consume_changes(journal.consume(cursor))

    assert service.snapshot(unrelated).value_revision == unrelated_before.value_revision

    component = _Component(90, 909)
    attached = service.component_snapshot(component)
    assert attached.value_revision > unrelated_before.value_revision
    assert service.field_revision(component, "speed") > service.field_revision(
        component, "color"
    )


def test_layered_revisions_do_not_cross_invalidate() -> None:
    service = _service()
    target = InspectorTarget.scene_object(41)
    before = service.snapshot(target)

    service.invalidate_preview(target)
    after_preview = service.snapshot(target)

    assert after_preview.preview_revision > before.preview_revision
    assert after_preview.target_revision == before.target_revision
    assert after_preview.schema_revision == before.schema_revision
    assert after_preview.value_revision == before.value_revision

    service.invalidate_schema(target)
    after_schema = service.snapshot(target)
    assert after_schema.schema_revision > after_preview.schema_revision
    assert after_schema.value_revision == after_preview.value_revision


def test_field_change_only_invalidates_owning_component() -> None:
    service = _service()
    left = _Component(7, 101)
    right = _Component(7, 202)
    left_before = service.component_snapshot(left)
    right_before = service.component_snapshot(right)

    service.invalidate_value(
        InspectorTarget.scene_object(7),
        component_id=101,
        field_id="speed",
    )

    assert service.component_snapshot(left).value_revision > left_before.value_revision
    assert service.component_snapshot(right).value_revision == right_before.value_revision
    assert service.field_revision(left, "speed") > service.field_revision(left, "color")


def test_runtime_change_set_projects_component_field_without_second_journal() -> None:
    service = _service()
    component = _Component(9, 303)
    other = _Component(9, 404)
    service.component_snapshot(component)
    other_before = service.component_snapshot(other)

    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("inspector", start_at_current=False)
    journal.publish_component_field("Mover", 303, "speed")
    changes = journal.consume(cursor)

    assert service.consume_changes(changes) is True
    assert service.consumed_journal_revision() == changes.revision
    assert service.field_revision(component, "speed") > service.field_revision(
        component, "color"
    )
    assert service.component_snapshot(other).value_revision == other_before.value_revision

    sequence = service.revision()
    assert service.consume_changes(changes) is False
    assert service.revision() == sequence


def test_component_structure_refreshes_owner_without_broad_value_invalidation() -> None:
    service = _service()
    component = _Component(12, 606)
    sibling = _Component(12, 707)
    target = InspectorTarget.scene_object(12)
    service.register_target_components(target, (component, sibling))
    owner_before = service.snapshot(target)
    sibling_before = service.component_snapshot(sibling)

    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("structure", start_at_current=False)
    journal.publish(RuntimeChangeDomain.COMPONENT_STRUCTURE, stable_id=(606, 1))
    service.consume_changes(journal.consume(cursor))

    owner_after = service.snapshot(target)
    sibling_after = service.component_snapshot(sibling)
    assert owner_after.schema_revision > owner_before.schema_revision
    assert sibling_after.value_revision == sibling_before.value_revision


def test_empty_full_resync_invalidates_all_layers() -> None:
    service = _service()
    before = service.snapshot(InspectorTarget.scene_object(20))
    changes = RuntimeChangeSet(
        from_revision=0,
        revision=9,
        domain_revisions={},
        changes={},
        full_resync=True,
    )

    assert service.consume_changes(changes) is True
    after = service.snapshot(InspectorTarget.scene_object(20))
    assert all(current > previous for current, previous in zip(after.token(), before.token()))


def test_runtime_preview_domain_targets_only_the_changed_asset(tmp_path) -> None:
    service = _service()
    changed_path = tmp_path / "changed.mat"
    other_path = tmp_path / "other.mat"
    changed_target = InspectorTarget.asset(str(changed_path))
    other_target = InspectorTarget.asset(str(other_path))
    changed_before = service.snapshot(changed_target)
    other_before = service.snapshot(other_target)

    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("preview", start_at_current=False)
    journal.publish(
        RuntimeChangeDomain.PREVIEW_SOURCE,
        stable_id=str(changed_path),
    )

    service.consume_changes(journal.consume(cursor))

    assert service.snapshot(changed_target).preview_revision > changed_before.preview_revision
    assert service.snapshot(other_target).preview_revision == other_before.preview_revision


def test_transform_change_maps_to_only_its_scene_object() -> None:
    service = _service()
    changed = InspectorTarget.scene_object(42)
    other = InspectorTarget.scene_object(43)
    changed_before = service.snapshot(changed)
    other_before = service.snapshot(other)

    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("transform", start_at_current=False)
    journal.publish(RuntimeChangeDomain.TRANSFORM_WORLD, stable_id=42)
    service.consume_changes(journal.consume(cursor))

    assert service.snapshot(changed).value_revision > changed_before.value_revision
    assert service.snapshot(other).value_revision == other_before.value_revision


def test_immediate_transform_invalidation_targets_only_changed_objects() -> None:
    service = _service()
    first = InspectorTarget.scene_object(51)
    second = InspectorTarget.scene_object(52)
    other = InspectorTarget.scene_object(53)
    first_before = service.snapshot(first)
    second_before = service.snapshot(second)
    other_before = service.snapshot(other)

    invalidate_scene_transforms((51, 52, 51, 0, -1))

    assert service.snapshot(first).value_revision > first_before.value_revision
    assert service.snapshot(second).value_revision > second_before.value_revision
    assert service.snapshot(other).value_revision == other_before.value_revision


def test_scene_rebuild_invalidates_schema_and_values() -> None:
    service = _service()
    target = InspectorTarget.scene_object(61)
    before = service.snapshot(target)

    invalidate_rebuilt_scene()

    after = service.snapshot(target)
    assert after.schema_revision > before.schema_revision
    assert after.value_revision > before.value_revision
    assert after.target_revision == before.target_revision
    assert after.preview_revision == before.preview_revision


def test_selection_advances_only_target_layer() -> None:
    service = _service()
    target = InspectorTarget.scene_object(15)
    before = service.snapshot(target)
    service.set_active_target(target)
    after = service.snapshot(target)

    assert after.target_revision > before.target_revision
    assert after.schema_revision == before.schema_revision
    assert after.value_revision == before.value_revision
    assert after.preview_revision == before.preview_revision


def test_component_value_cache_skips_unchanged_getters(monkeypatch) -> None:
    from Infernux.engine.ui import inspector_components as components_ui

    service = _service()
    component = _Component(11, 505)
    components_ui._COMPONENT_VALUE_CACHE.clear()
    monkeypatch.setattr(components_ui, "_is_in_play_mode", lambda: False)
    calls = {"speed": 0, "color": 0}

    def _read(field):
        calls[field] += 1
        return calls[field]

    entry, rebuild, refresh_all = components_ui._begin_component_value_cache(
        "test", component
    )
    assert rebuild and refresh_all
    components_ui._get_cached_component_value(
        entry, refresh_all, "speed", lambda: _read("speed")
    )
    components_ui._get_cached_component_value(
        entry, refresh_all, "color", lambda: _read("color")
    )

    entry, rebuild, refresh_all = components_ui._begin_component_value_cache(
        "test", component
    )
    assert not rebuild and not refresh_all
    components_ui._get_cached_component_value(
        entry, refresh_all, "speed", lambda: _read("speed")
    )
    components_ui._get_cached_component_value(
        entry, refresh_all, "color", lambda: _read("color")
    )
    assert calls == {"speed": 1, "color": 1}

    service.invalidate_value(
        InspectorTarget.scene_object(11),
        component_id=505,
        field_id="speed",
    )
    entry, rebuild, refresh_all = components_ui._begin_component_value_cache(
        "test", component
    )
    assert rebuild and not refresh_all
    components_ui._get_cached_component_value(
        entry, refresh_all, "speed", lambda: _read("speed")
    )
    components_ui._get_cached_component_value(
        entry, refresh_all, "color", lambda: _read("color")
    )
    assert calls == {"speed": 2, "color": 1}


def test_failed_changed_field_getter_preserves_sibling_cache(monkeypatch) -> None:
    from Infernux.engine.ui import inspector_components as components_ui

    service = _service()
    component = _Component(17, 515)
    components_ui._COMPONENT_VALUE_CACHE.clear()
    monkeypatch.setattr(components_ui, "_is_in_play_mode", lambda: False)
    calls = {"speed": 0, "color": 0}

    entry, _rebuild, refresh_all = components_ui._begin_component_value_cache(
        "failure", component
    )
    components_ui._get_cached_component_value(
        entry, refresh_all, "speed", lambda: calls.__setitem__("speed", 1) or 1.0
    )
    components_ui._get_cached_component_value(
        entry, refresh_all, "color", lambda: calls.__setitem__("color", 1) or "red"
    )
    service.invalidate_value(
        InspectorTarget.scene_object(17), component_id=515, field_id="speed"
    )
    entry, _rebuild, refresh_all = components_ui._begin_component_value_cache(
        "failure", component
    )

    def _fail_speed():
        calls["speed"] += 1
        raise ReferenceError("component retired during snapshot read")

    try:
        components_ui._get_cached_component_value(
            entry, refresh_all, "speed", _fail_speed
        )
    except ReferenceError:
        pass
    else:
        raise AssertionError("failed getter must propagate to the guarded renderer")

    assert components_ui._get_cached_component_value(
        entry,
        refresh_all,
        "color",
        lambda: calls.__setitem__("color", calls["color"] + 1) or "blue",
    ) == "red"
    assert calls == {"speed": 2, "color": 1}

    assert components_ui._get_cached_component_value(
        entry,
        refresh_all,
        "speed",
        lambda: calls.__setitem__("speed", calls["speed"] + 1) or 2.0,
    ) == 2.0
    assert calls == {"speed": 3, "color": 1}


def test_render_plan_invalidation_keeps_precise_field_values() -> None:
    from Infernux.engine.ui import inspector_components as components_ui

    entry = {
        "values": {"speed": 2.0, "color": "red"},
        "field_revisions": {"speed": 7, "color": 4},
        "builtin_plan": object(),
        "py_plan": object(),
    }
    components_ui._invalidate_component_render_cache(entry)

    assert entry["values"] == {"speed": 2.0, "color": "red"}
    assert entry["field_revisions"] == {"speed": 7, "color": 4}
    assert "builtin_plan" not in entry
    assert "py_plan" not in entry


def test_property_undo_redo_keeps_sibling_component_revision_stable() -> None:
    from Infernux.engine.interaction import (
        PropertyTransactionStatus,
        make_attribute_property_transaction,
    )
    from Infernux.engine.undo import UndoManager

    service = _service()
    # Detached probes keep SetPropertyCommand on its direct weak target; live
    # scene resolution belongs to the existing Undo integration tests.
    edited = _Component(0, 801)
    sibling = _Component(0, 802)
    manager = UndoManager()
    sibling_before = service.component_snapshot(sibling)
    transaction = make_attribute_property_transaction(
        (edited,),
        "speed",
        value_type="float",
    )

    assert transaction.commit_or_raise(2.0) is PropertyTransactionStatus.APPLIED
    assert edited.speed == 2.0
    assert service.component_snapshot(sibling).value_revision == sibling_before.value_revision

    manager.undo()
    assert edited.speed == 1.0
    assert service.component_snapshot(sibling).value_revision == sibling_before.value_revision

    manager.redo()
    assert edited.speed == 2.0
    assert service.component_snapshot(sibling).value_revision == sibling_before.value_revision


def test_native_inspector_uses_layered_snapshot_without_value_ttl() -> None:
    root = Path(__file__).resolve().parents[2]
    header = (root / "cpp/infernux/function/editor/InspectorPanel.h").read_text(
        encoding="utf-8"
    )
    source = (root / "cpp/infernux/function/editor/InspectorPanel.cpp").read_text(
        encoding="utf-8"
    )

    assert "struct RevisionSnapshot" in header
    assert "getRevisionSnapshot" in header
    assert "VALUE_CACHE_TTL" not in header
    assert "m_cachedTransformValueRevision" in source
    assert "getRevisionSnapshot()" in source
    assert source.count("getRevisionSnapshot()") == 1
    assert "retaining the previous packet" in source
    assert "catch (const std::exception &error)" in source
    assert "m_cachedComponentValueRevision" not in header + source
    assert "m_cachedMultiComponentValueRevision" not in header + source


def test_play_compatibility_poll_is_scoped_to_visible_component_bodies() -> None:
    root = Path(__file__).resolve().parents[2]
    bootstrap = (
        root / "python/Infernux/engine/bootstrap_inspector/_wire.py"
    ).read_text(encoding="utf-8")
    components = (
        root / "python/Infernux/engine/ui/inspector_components.py"
    ).read_text(encoding="utf-8")
    native = (
        root / "cpp/infernux/function/editor/InspectorPanel.cpp"
    ).read_text(encoding="utf-8")

    snapshot_callback = bootstrap[
        bootstrap.index("def _get_revision_snapshot():") :
        bootstrap.index("ip.get_revision_snapshot = _get_revision_snapshot")
    ]
    assert "monotonic" not in snapshot_callback
    assert "_RUNTIME_VISIBLE_VALUE_POLL_S = 1.0 / 60.0" in components
    assert native.index("if (header.open)") < native.index(
        "renderComponentBody(ctx, objId", native.index("if (header.open)")
    )
    assert bootstrap.index("is_virtualized_region_visible") < bootstrap.index(
        "_render_component_body_live(ctx_arg", bootstrap.index("def _render_component_body(")
    )
