from types import SimpleNamespace

import pytest


def _property_handle(targets, *, validate=lambda _value: "", publish=None):
    from Infernux.engine.interaction import (
        FieldSchema,
        SerializedObjectView,
        SerializedPropertyBinding,
        SerializedPropertyHandle,
    )
    from Infernux.engine.undo import SetPropertyCommand

    bindings = []
    ids = []
    for index, target in enumerate(targets):
        target_id = f"target:{index}"
        ids.append(target_id)
        bindings.append(
            SerializedPropertyBinding(
                target_id,
                read=lambda target=target: target.value,
                command_factory=lambda old, new, description, target=target: (
                    SetPropertyCommand(target, "value", old, new, description)
                ),
                validate=validate,
            )
        )
    return SerializedPropertyHandle(
        FieldSchema("Probe.value", "float"),
        SerializedObjectView(tuple(ids)),
        tuple(bindings),
        publish=publish,
    )


def test_property_transaction_commits_mixed_targets_as_one_journal_action():
    from Infernux.engine.interaction import PropertyTransaction
    from Infernux.engine.undo import UndoManager

    first = SimpleNamespace(value=1.0)
    second = SimpleNamespace(value=2.0)
    publications = []
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        handle = _property_handle(
            (first, second), publish=lambda: publications.append("publish")
        )
        assert handle.mixed is True
        assert PropertyTransaction(handle, "Set Probe").commit(3.0).value == "applied"
        assert (first.value, second.value) == (3.0, 3.0)
        assert publications == ["publish"]
        assert len(manager.action_journal.applied_entries()) == 1

        manager.undo()
        assert (first.value, second.value) == (1.0, 2.0)
        assert publications == ["publish", "publish"]
    finally:
        UndoManager._instance = previous


def test_property_transaction_validates_all_targets_before_any_write():
    from Infernux.engine.interaction import PropertyTransaction
    from Infernux.engine.undo import UndoManager

    first = SimpleNamespace(value=1.0)
    second = SimpleNamespace(value=2.0)
    rejected = []
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        handle = _property_handle(
            (first, second),
            validate=lambda value: "must be non-negative" if value < 0 else "",
        )
        result = PropertyTransaction(
            handle, "Set Probe", on_rejected=rejected.append
        ).commit(-1.0)
        assert result.value == "rejected"
        assert (first.value, second.value) == (1.0, 2.0)
        assert len(manager.action_journal.applied_entries()) == 0
        assert "must be non-negative" in rejected[0]
    finally:
        UndoManager._instance = previous


def test_snapshot_property_transaction_commits_and_replays_one_aggregate():
    from Infernux.engine.interaction import SnapshotPropertyTransaction
    from Infernux.engine.undo import UndoManager

    state = {"value": {"position": [1.0, 2.0, 3.0], "scale": [1.0, 1.0, 1.0]}}
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        transaction = SnapshotPropertyTransaction(
            "Transform:17",
            lambda: state["value"],
            lambda value: state.__setitem__("value", value),
            "Edit Transform",
        )
        transaction.commit_or_raise(
            {"position": [4.0, 5.0, 6.0], "scale": [1.0, 1.0, 1.0]}
        )
        assert state["value"]["position"] == [4.0, 5.0, 6.0]
        assert len(manager.action_journal.applied_entries()) == 1

        manager.undo()
        assert state["value"]["position"] == [1.0, 2.0, 3.0]
        manager.redo()
        assert state["value"]["position"] == [4.0, 5.0, 6.0]
    finally:
        UndoManager._instance = previous


def test_snapshot_property_transaction_merges_continuous_same_target_edits():
    from Infernux.engine.interaction import SnapshotPropertyTransaction
    from Infernux.engine.undo import UndoManager

    state = {"value": 1.0}
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        for value in (2.0, 3.0):
            SnapshotPropertyTransaction(
                "Transform:17",
                lambda: state["value"],
                lambda candidate: state.__setitem__("value", candidate),
                "Edit Transform",
            ).commit_or_raise(value)

        assert state["value"] == 3.0
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert state["value"] == 1.0
    finally:
        UndoManager._instance = previous


def test_snapshot_property_transaction_normalizes_and_clears_aggregate():
    from Infernux.engine.interaction import SnapshotPropertyTransaction
    from Infernux.engine.undo import UndoManager

    state = {"value": {"asset": "old", "subresource": "old-frame"}}
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        transaction = SnapshotPropertyTransaction(
            "SpriteRenderer:17:sprite",
            lambda: state["value"],
            lambda value: state.__setitem__("value", value),
            "Set Sprite",
            normalize=lambda candidate: (
                {"asset": candidate, "subresource": f"{candidate}-frame"}
                if candidate
                else {"asset": "", "subresource": ""}
            ),
            clear_value="",
        )
        transaction.commit_or_raise("new")
        assert state["value"] == {"asset": "new", "subresource": "new-frame"}

        transaction.clear_or_raise()
        assert state["value"] == {"asset": "", "subresource": ""}
        manager.undo()
        assert state["value"] == {"asset": "new", "subresource": "new-frame"}
    finally:
        UndoManager._instance = previous


def test_asset_reference_drawer_is_owned_by_property_drawer_registry():
    from Infernux.engine.interaction import (
        AssetReferenceFieldModel,
        property_drawer_registry,
    )

    model = property_drawer_registry.create(
        "asset_reference",
        field_id="texture",
        display_text="None",
        type_hint="Texture",
    )
    assert isinstance(model, AssetReferenceFieldModel)


def test_attribute_property_transactions_merge_continuous_edits():
    from Infernux.engine.interaction import make_attribute_property_transaction
    from Infernux.engine.undo import UndoManager

    target = SimpleNamespace(value=1.0)
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        first = make_attribute_property_transaction(
            (target,), "value", description="Set Value"
        )
        second = make_attribute_property_transaction(
            (target,), "value", description="Set Value"
        )
        first.commit_or_raise(2.0)
        second.commit_or_raise(3.0)

        assert target.value == 3.0
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert target.value == 1.0
    finally:
        UndoManager._instance = previous


def test_python_component_multi_edit_is_one_atomic_document_action():
    from Infernux.engine.interaction import (
        make_python_component_property_transaction,
    )
    from Infernux.engine.undo import UndoManager

    class ProbeComponent:
        type_name = "ProbeComponent"

        def __init__(self, component_id, speed):
            self.component_id = component_id
            self.speed = speed
            self.validate_count = 0

        def _serialize_fields_document(self):
            return {
                "__type_name__": type(self).__name__,
                "__component_id__": self.component_id,
                "speed": self.speed,
            }

        def _deserialize_fields_document(self, document):
            self.speed = float(document["speed"])

        def _call_on_validate(self):
            self.validate_count += 1

    first = ProbeComponent(1, 1.0)
    second = ProbeComponent(2, 2.0)
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        transaction = make_python_component_property_transaction(
            (first, second), "speed", value_type="float", description="Set Speed"
        )
        transaction.commit_or_raise(4.0)

        assert (first.speed, second.speed) == (4.0, 4.0)
        assert (first.validate_count, second.validate_count) == (1, 1)
        assert len(manager.action_journal.applied_entries()) == 1

        manager.undo()
        assert (first.speed, second.speed) == (1.0, 2.0)
        assert (first.validate_count, second.validate_count) == (2, 2)
    finally:
        UndoManager._instance = previous


def test_serialized_field_assignment_is_storage_not_an_implicit_editor_command():
    from Infernux.components import InxComponent
    from Infernux.engine.interaction import (
        make_python_component_property_transaction,
    )
    from Infernux.engine.undo import UndoManager

    class ProbeComponent(InxComponent):
        speed: float = 1.0

    previous = UndoManager._instance
    manager = UndoManager()
    component = ProbeComponent()
    try:
        component.speed = 2.0

        assert component.speed == 2.0
        assert manager.action_journal.applied_entries() == ()

        make_python_component_property_transaction(
            (component,),
            "speed",
            value_type="float",
            description="Set Speed",
        ).commit_or_raise(3.0)

        assert component.speed == 3.0
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert component.speed == 2.0
    finally:
        UndoManager._instance = previous


def test_serialized_field_layer_has_no_editor_undo_or_dirty_hook():
    import importlib
    from pathlib import Path

    module = importlib.import_module("Infernux.components.serialized_field")
    bootstrap_module = importlib.import_module("Infernux.engine.bootstrap")

    serialized_source = Path(module.__file__).read_text(encoding="utf-8")
    bootstrap_source = Path(bootstrap_module.__file__).read_text(encoding="utf-8")

    assert "set_field_change_hooks" not in serialized_source
    assert "_on_field_will_change" not in serialized_source
    assert "_inject_field_change_hooks" not in bootstrap_source


def test_multi_target_property_write_rolls_back_when_one_target_rejects():
    from Infernux.engine.interaction import make_attribute_property_transaction
    from Infernux.engine.undo import UndoManager

    class Probe:
        def __init__(self, value, reject=False):
            self._value = value
            self.reject = reject

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, candidate):
            if self.reject and candidate == 3.0:
                raise ValueError("rejected by target")
            self._value = candidate

    first = Probe(1.0)
    second = Probe(2.0, reject=True)
    previous = UndoManager._instance
    manager = UndoManager()
    try:
        transaction = make_attribute_property_transaction(
            (first, second), "value", description="Set Value"
        )
        with pytest.raises(RuntimeError, match="rejected"):
            transaction.commit_or_raise(3.0)

        assert (first.value, second.value) == (1.0, 2.0)
        assert len(manager.action_journal.applied_entries()) == 0
    finally:
        UndoManager._instance = previous
