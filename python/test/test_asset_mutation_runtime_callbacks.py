"""R3 one-shot and persistent asset callback contract tests."""

from __future__ import annotations

import gc
import weakref

from Infernux.components import InxComponent
from Infernux.engine.interaction import (
    AssetContentChange,
    AssetMutation,
    AssetMutationKind,
    AssetMutationService,
    DocumentRegistry,
    SelectionService,
)
from Infernux.engine.runtime_dispatch import RuntimeRevisionEpoch, publish_runtime_dispatch_epoch


def _change(tmp_path):
    mutation = AssetMutation(
        AssetMutationKind.MODIFIED,
        str(tmp_path / "asset.bin"),
    )
    return AssetContentChange(mutation, revision=1)


class _PersistentOwner(InxComponent):
    def handle(self, _change):
        self.calls.append("old")


def _service():
    previous = AssetMutationService.instance()
    if previous is not None:
        previous.shutdown()
    return AssetMutationService(DocumentRegistry(), SelectionService())


def test_component_callback_resolves_new_body_after_runtime_epoch_publish(tmp_path):
    service = _service()
    owner = _PersistentOwner()
    owner.calls = []
    service.add_component_listener(owner.handle)
    service._notify(_change(tmp_path))
    assert owner.calls == ["old"]

    old_method = _PersistentOwner.handle

    def replacement(self, _change):
        self.calls.append("new")

    _PersistentOwner.handle = replacement
    publication = publish_runtime_dispatch_epoch((_PersistentOwner,))
    publication.commit()
    try:
        service._notify(_change(tmp_path))
        assert owner.calls == ["old", "new"]
    finally:
        publication.rollback()
        _PersistentOwner.handle = old_method
        service.shutdown()


def test_deleted_callback_is_reported_and_never_runs_old_body(tmp_path):
    service = _service()
    owner = _PersistentOwner()
    owner.calls = []
    service.add_component_listener(owner.handle)
    current = RuntimeRevisionEpoch(
        10000,
        {},
    )

    import Infernux.engine.interaction.asset_mutations as asset_mutations

    original = asset_mutations.current_runtime_epoch
    asset_mutations.current_runtime_epoch = lambda: current
    try:
        service._notify(_change(tmp_path))
        assert owner.calls == []
        assert service.listener_diagnostics["component_callbacks"] == 1
    finally:
        asset_mutations.current_runtime_epoch = original
        service.shutdown()


def test_destroyed_and_collected_owners_are_not_kept_or_called(tmp_path):
    service = _service()
    destroyed = _PersistentOwner()
    destroyed.calls = []
    service.add_component_listener(destroyed.handle)
    destroyed._is_destroyed = True
    service._notify(_change(tmp_path))
    assert service.listener_diagnostics["component_callbacks"] == 0

    collected = _PersistentOwner()
    collected.calls = []
    service.add_component_listener(collected.handle)
    collected_ref = weakref.ref(collected)
    del collected
    gc.collect()
    service._notify(_change(tmp_path))
    assert collected_ref() is None
    assert service.listener_diagnostics["component_callbacks"] == 0
    service.shutdown()


def test_editor_observer_is_pinned_and_one_shot_is_consumed(tmp_path):
    service = _service()
    calls = []

    class Observer:
        def receive(self, _change):
            calls.append("observer")

    observer = Observer()
    service.add_observer(observer.receive)
    service.add_one_shot_listener(lambda _change: calls.append("once"))
    assert service.listener_diagnostics == {
        "editor_observers": 1,
        "component_callbacks": 0,
        "one_shot_callbacks": 1,
    }
    service._notify(_change(tmp_path))
    service._notify(_change(tmp_path))
    assert calls == ["observer", "once", "observer"]
    assert service.listener_diagnostics["one_shot_callbacks"] == 0
    service.shutdown()


def test_component_subscription_is_deduplicated_and_explicitly_removed(tmp_path):
    service = _service()
    owner = _PersistentOwner()
    owner.calls = []
    service.add_component_listener(owner.handle)
    service.add_component_listener(owner.handle)
    assert service.listener_diagnostics["component_callbacks"] == 1
    service._notify(_change(tmp_path))
    assert owner.calls == ["old"]
    service.remove_component_listener(owner.handle)
    assert service.listener_diagnostics["component_callbacks"] == 0
    service._notify(_change(tmp_path))
    assert owner.calls == ["old"]
    service.shutdown()


def test_asset_batch_captures_runtime_epoch_once(tmp_path, monkeypatch):
    service = _service()
    owner = _PersistentOwner()
    owner.calls = []
    service.add_component_listener(owner.handle)

    import Infernux.engine.interaction.asset_mutations as asset_mutations

    calls = []
    epoch = RuntimeRevisionEpoch(20000, {})
    monkeypatch.setattr(
        asset_mutations,
        "current_runtime_epoch",
        lambda: calls.append("capture") or epoch,
    )
    service._notify(_change(tmp_path))
    assert calls == ["capture"]
    assert owner.calls == []
    service.shutdown()
